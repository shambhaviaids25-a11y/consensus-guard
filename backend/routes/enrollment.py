import hashlib
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from backend.services.eidos_service import EidosServiceError, enroll_speaker


router = APIRouter()
logger = logging.getLogger(__name__)

ROLES = {
    "CFO": ("EIDOS_CFO_SPEAKER_ID", "CFO"),
    "COO": ("EIDOS_COO_SPEAKER_ID", "COO"),
    "VP": ("EIDOS_VP_SPEAKER_ID", "VP"),
    "HOL": ("EIDOS_HOL_SPEAKER_ID", "HOL"),
    # Keep accepting role values sent by the existing extension.
    "VP_SECURITY": ("EIDOS_VP_SPEAKER_ID", "VP"),
    "LEGAL": ("EIDOS_HOL_SPEAKER_ID", "HOL"),
}
MAX_AUDIO_BYTES = 100_000_000
ALLOWED_AUDIO_EXTENSIONS = {
    ".wav",
    ".mp3",
    ".flac",
    ".ogg",
    ".opus",
    ".webm",
    ".m4a",
    ".aac",
    ".aiff",
    ".aif",
}


class SpeakerMappingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def normalize_role(role: str) -> str:
    return role.strip().upper()


def log_speaker_mapping_configuration() -> None:
    for role in ("CFO", "COO", "VP", "HOL"):
        env_name, _ = ROLES[role]
        configured = bool(os.getenv(env_name, "").strip())
        logger.info(
            "Eidos speaker mapping configuration role=%s configured=%s",
            role,
            configured,
        )


def get_speaker_id_for_role(role: str) -> str:
    role = normalize_role(role)
    if role not in ROLES:
        raise SpeakerMappingError(
            "unknown_role",
            "Unknown role. Choose CFO, COO, VP or HOL (VP_SECURITY and LEGAL are also accepted).",
        )

    env_name, _ = ROLES[role]
    speaker_id = os.getenv(env_name, "").strip()
    if not speaker_id:
        raise SpeakerMappingError(
            "speaker_id_not_configured",
            f"No speaker ID is configured for role {role}; set {env_name} in backend/.env.",
        )
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", speaker_id):
        raise SpeakerMappingError(
            "invalid_speaker_id_config",
            f"{env_name} must contain a valid Eidos speaker ID (1-64 allowed characters).",
        )
    return speaker_id


def _invalid_request(code: str, message: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


@router.post("/api/enroll")
async def enroll_executive(
    role: str = Form(...),
    audio: UploadFile = File(...),
) -> JSONResponse:
    role = normalize_role(role)
    if role not in ROLES:
        return _invalid_request(
            "invalid_role",
            "role must be CFO, COO, VP or HOL (VP_SECURITY and LEGAL are also accepted).",
        )

    filename = Path(audio.filename or "").name
    extension = Path(filename).suffix.lower()
    content_type = (audio.content_type or "").lower()
    if (
        not filename
        or extension not in ALLOWED_AUDIO_EXTENSIONS
        or not (content_type.startswith("audio/") or content_type in {
            "application/octet-stream",
            "application/ogg",
        })
    ):
        return _invalid_request(
            "invalid_audio_file",
            "Upload an audio file supported by Eidos (WAV, MP3, FLAC, OGG/Opus, WebM, M4A/AAC or AIFF).",
            status_code=415,
        )

    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if not audio_bytes:
        return _invalid_request("empty_audio_file", "The uploaded audio file is empty.")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return _invalid_request(
            "audio_file_too_large", "The uploaded audio file exceeds the 100 MB limit.", 413
        )

    try:
        speaker_id = get_speaker_id_for_role(role)
    except SpeakerMappingError as exc:
        return _invalid_request(
            exc.code,
            str(exc),
            status_code=503,
        )

    display_name = ROLES[role][1]
    audio_digest = hashlib.sha256(audio_bytes).hexdigest()
    idempotency_key = hashlib.sha256(
        f"{speaker_id}:{display_name}:{filename}:{content_type}:{audio_digest}".encode(
            "utf-8"
        )
    ).hexdigest()

    try:
        status_code, response = await enroll_speaker(
            speaker_id=speaker_id,
            display_name=display_name,
            filename=filename,
            content_type=content_type,
            audio=audio_bytes,
            idempotency_key=idempotency_key,
        )
    except EidosServiceError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.payload)

    return JSONResponse(status_code=status_code, content=response)
