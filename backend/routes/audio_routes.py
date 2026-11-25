import hashlib
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse

from backend.routes.enrollment import (
    ALLOWED_AUDIO_EXTENSIONS,
    MAX_AUDIO_BYTES,
    ROLES,
    SpeakerMappingError,
    get_speaker_id_for_role,
    normalize_role,
)
from backend.services.eidos_service import EidosServiceError, verify_speaker


router = APIRouter()


def _response_body(role: str, detect_deepfake: bool) -> dict[str, Any]:
    return {
        "participant": role,
        "identity_status": "error",
        "similarity": None,
        "audio_authenticity_status": "error" if detect_deepfake else "not_checked",
        "audio_authenticity_score": None,
        "request_id": None,
        "operation_id": None,
        "audio_authenticity_operation_id": None,
        "errors": [],
    }


def _append_error(
    body: dict[str, Any], source: str, error: Any
) -> None:
    body["errors"].append({"source": source, "error": error})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _extract_deepfake_result(
    body: dict[str, Any], payload: dict[str, Any], enabled: bool
) -> None:
    if not enabled:
        body["audio_authenticity_status"] = "not_checked"
        return

    deepfake = payload.get("deepfake")
    if not isinstance(deepfake, dict):
        partial = payload.get("partial")
        deepfake = partial.get("deepfake") if isinstance(partial, dict) else None

    if not isinstance(deepfake, dict):
        _append_error(
            body,
            "audio_authenticity",
            {"code": "missing_deepfake_result", "message": "Eidos did not return a deepfake result."},
        )
        return

    body["audio_authenticity_operation_id"] = deepfake.get("operation_id")
    verdict = deepfake.get("verdict")
    if verdict in ("real", "fake"):
        body["audio_authenticity_status"] = verdict
        score = deepfake.get("score")
        if _is_number(score):
            body["audio_authenticity_score"] = score
    elif deepfake.get("error") is not None:
        _append_error(body, "audio_authenticity", deepfake["error"])
    else:
        _append_error(
            body,
            "audio_authenticity",
            {"code": "invalid_deepfake_result", "message": "Eidos returned no valid deepfake verdict."},
        )


def _normalize_response(
    role: str, detect_deepfake: bool, status_code: int, payload: Any
) -> tuple[int, dict[str, Any]]:
    body = _response_body(role, detect_deepfake)
    if not isinstance(payload, dict):
        _append_error(
            body,
            "identity",
            {"code": "invalid_eidos_response", "message": "Eidos returned an invalid response."},
        )
        return 502, body

    body["request_id"] = payload.get("request_id")
    body["operation_id"] = payload.get("operation_id")
    _extract_deepfake_result(body, payload, detect_deepfake)

    upstream_error = payload.get("error")
    if status_code < 200 or status_code >= 300 or upstream_error is not None:
        _append_error(
            body,
            "identity",
            upstream_error if upstream_error is not None else payload,
        )
        return status_code if status_code >= 400 else 502, body

    result = payload.get("result")
    decision = result.get("decision") if isinstance(result, dict) else None
    similarity = result.get("similarity") if isinstance(result, dict) else None
    if decision not in ("match", "no_match"):
        _append_error(
            body,
            "identity",
            {"code": "invalid_verification_result", "message": "Eidos returned no valid identity decision."},
        )
        return 502, body

    body["identity_status"] = decision
    if _is_number(similarity):
        body["similarity"] = similarity
    else:
        _append_error(
            body,
            "identity",
            {"code": "missing_similarity", "message": "Eidos returned no valid similarity score."},
        )
        return 502, body

    return status_code, body


def _request_error_response(
    role: str, detect_deepfake: bool, status_code: int, code: str, message: str
) -> JSONResponse:
    body = _response_body(role, detect_deepfake)
    _append_error(body, "request", {"code": code, "message": message})
    return JSONResponse(status_code=status_code, content=body)


@router.post("/api/verify/audio")
async def verify_audio(
    role: str = Form(""),
    audio: Optional[UploadFile] = File(None),
    detect_deepfake: bool = Form(False),
) -> JSONResponse:
    role = normalize_role(role)
    if role not in ROLES:
        return _request_error_response(
            role,
            detect_deepfake,
            400,
            "invalid_role",
            "role must be CFO, COO, VP or HOL (VP_SECURITY and LEGAL are also accepted).",
        )
    if audio is None:
        return _request_error_response(
            role, detect_deepfake, 400, "missing_audio_file", "An audio file is required."
        )

    filename = Path(audio.filename or "").name
    extension = Path(filename).suffix.lower()
    content_type = (audio.content_type or "").lower()
    if (
        not filename
        or extension not in ALLOWED_AUDIO_EXTENSIONS
        or not (
            content_type.startswith("audio/")
            or content_type in {"application/octet-stream", "application/ogg"}
        )
    ):
        return _request_error_response(
            role,
            detect_deepfake,
            415,
            "invalid_audio_file",
            "Upload an audio file supported by Eidos (WAV, MP3, FLAC, OGG/Opus, WebM, M4A/AAC or AIFF).",
        )

    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if not audio_bytes:
        return _request_error_response(
            role, detect_deepfake, 400, "empty_audio_file", "The uploaded audio file is empty."
        )
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        return _request_error_response(
            role,
            detect_deepfake,
            413,
            "audio_file_too_large",
            "The uploaded audio file exceeds the 100 MB limit.",
        )

    try:
        speaker_id = get_speaker_id_for_role(role)
    except SpeakerMappingError as exc:
        return _request_error_response(
            role,
            detect_deepfake,
            503,
            exc.code,
            str(exc),
        )

    audio_digest = hashlib.sha256(audio_bytes).hexdigest()
    idempotency_key = hashlib.sha256(
        f"{speaker_id}:{filename}:{content_type}:{detect_deepfake}:{audio_digest}".encode(
            "utf-8"
        )
    ).hexdigest()

    try:
        status_code, payload = await verify_speaker(
            speaker_id=speaker_id,
            filename=filename,
            content_type=content_type,
            audio=audio_bytes,
            detect_deepfake=detect_deepfake,
            idempotency_key=idempotency_key,
        )
    except EidosServiceError as exc:
        body = _response_body(role, detect_deepfake)
        error = exc.payload.get("error", exc.payload)
        _append_error(body, "identity", error)
        return JSONResponse(status_code=exc.status_code, content=body)

    status_code, body = _normalize_response(
        role, detect_deepfake, status_code, payload
    )
    return JSONResponse(status_code=status_code, content=body)
