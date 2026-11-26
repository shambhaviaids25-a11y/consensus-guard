import hashlib
import hmac
import logging
import math
import os
import re
import secrets
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)
_LOG_REFERENCE_SALT = secrets.token_bytes(32)

EIDOS_BASE_URL = "https://platform.detectif.ai"
ENROLLMENT_PATH = "/v1/speakers/enroll"
VERIFICATION_PATH = "/v1/speakers/verify"
REQUEST_TIMEOUT = httpx.Timeout(130.0, connect=10.0)


def safe_speaker_reference(speaker_id: str) -> str:
    api_key = os.getenv("DETECTIF_API_KEY")
    key = api_key.encode("utf-8") if api_key else _LOG_REFERENCE_SALT
    return hmac.new(
        key,
        speaker_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def _audio_duration(
    audio: bytes, capture_duration_seconds: Optional[float]
) -> tuple[Optional[float], str]:
    if (
        isinstance(capture_duration_seconds, (int, float))
        and not isinstance(capture_duration_seconds, bool)
        and math.isfinite(capture_duration_seconds)
        and 0 < capture_duration_seconds <= 86_400
    ):
        return round(float(capture_duration_seconds), 3), "client_reported"

    if len(audio) < 12 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        return None, "unavailable"

    byte_rate = None
    data_size = None
    offset = 12
    while offset + 8 <= len(audio):
        chunk_id = audio[offset:offset + 4]
        chunk_size = int.from_bytes(audio[offset + 4:offset + 8], "little")
        chunk_start = offset + 8
        if chunk_id == b"fmt " and chunk_size >= 12 and chunk_start + 12 <= len(audio):
            byte_rate = int.from_bytes(audio[chunk_start + 8:chunk_start + 12], "little")
        elif chunk_id == b"data":
            data_size = min(chunk_size, len(audio) - chunk_start)
        offset = chunk_start + chunk_size + (chunk_size & 1)

    if byte_rate and data_size is not None:
        return round(data_size / byte_rate, 3), "wav_header"
    return None, "unavailable"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return "other"


def _response_field_types(payload: Any) -> str:
    fields: list[str] = []

    def visit(value: Any, path: str, depth: int) -> None:
        if len(fields) >= 50:
            return
        fields.append(f"{path or '$'}:{_json_type(value)}")
        if depth >= 3:
            return
        if isinstance(value, dict):
            for key, child in list(value.items())[:20]:
                safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:40]
                visit(child, f"{path}.{safe_key}" if path else safe_key, depth + 1)
                if len(fields) >= 50:
                    break
        elif isinstance(value, list) and value:
            visit(value[0], f"{path}[]", depth + 1)

    visit(payload, "", 0)
    return ",".join(fields)


def _response_error_code(payload: Any) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", code):
        return code
    return "none"


class EidosServiceError(Exception):
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload["error"]["message"])


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _redact_private_values(
    value: Any, api_key: str, speaker_ids: list[str]
) -> Any:
    if isinstance(value, str):
        value = value.replace(api_key, "[REDACTED]")
        for speaker_id in speaker_ids:
            if value == speaker_id:
                return "[REDACTED]"
            if len(speaker_id) >= 8:
                value = re.sub(
                    rf"(?<![A-Za-z0-9_.:-]){re.escape(speaker_id)}(?![A-Za-z0-9_.:-])",
                    "[REDACTED]",
                    value,
                )
        return value
    if isinstance(value, list):
        return [
            _redact_private_values(item, api_key, speaker_ids)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in {"speaker_id", "speakerid"}
                else _redact_private_values(item, api_key, speaker_ids)
            )
            for key, item in value.items()
        }
    return value


async def _post_audio_request(
    *,
    endpoint: str,
    action: str,
    form_data: dict[str, str],
    filename: str,
    content_type: str,
    audio: bytes,
    idempotency_key: str,
    client: Optional[httpx.AsyncClient] = None,
    capture_duration_seconds: Optional[float] = None,
) -> tuple[int, Any]:
    speaker_ref = safe_speaker_reference(form_data.get("speaker_id", ""))
    duration_seconds, duration_source = _audio_duration(
        audio, capture_duration_seconds
    )
    safe_content_type = re.sub(r"[^A-Za-z0-9!#$&^_.+-/]", "?", content_type)[:80]
    api_key = os.getenv("DETECTIF_API_KEY")
    if not api_key:
        logger.warning(
            "eidos_request_failed action=%s speaker_ref=%s http_status=503 "
            "error_code=eidos_not_configured audio_mime=%s "
            "audio_duration_seconds=%s duration_source=%s audio_bytes=%d",
            action,
            speaker_ref,
            safe_content_type or "unknown",
            duration_seconds,
            duration_source,
            len(audio),
        )
        raise EidosServiceError(
            503,
            _error_payload(
                "eidos_not_configured",
                f"{action} is unavailable because the Eidos API key is not configured.",
            ),
        )

    logger.info(
        "eidos_request action=%s method=POST endpoint=%s speaker_ref=%s "
        "audio_mime=%s audio_duration_seconds=%s duration_source=%s audio_bytes=%d",
        action,
        endpoint,
        speaker_ref,
        safe_content_type or "unknown",
        duration_seconds,
        duration_source,
        len(audio),
    )

    owns_client = client is None
    active_client = (
        client if client is not None else httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    )

    try:
        response = await active_client.post(
            f"{EIDOS_BASE_URL}{endpoint}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Idempotency-Key": idempotency_key,
            },
            data=form_data,
            files={"file": (filename, audio, content_type)},
        )
    except httpx.TimeoutException as exc:
        logger.warning(
            "eidos_request_failed action=%s speaker_ref=%s error_code=eidos_timeout "
            "audio_mime=%s audio_duration_seconds=%s audio_bytes=%d",
            action,
            speaker_ref,
            safe_content_type or "unknown",
            duration_seconds,
            len(audio),
        )
        raise EidosServiceError(
            504,
            _error_payload(
                "eidos_timeout",
                f"The Eidos {action.lower()} request timed out. Retry with the same audio.",
            ),
        ) from exc
    except httpx.RequestError as exc:
        logger.warning(
            "eidos_request_failed action=%s speaker_ref=%s error_code=eidos_unavailable "
            "audio_mime=%s audio_duration_seconds=%s audio_bytes=%d",
            action,
            speaker_ref,
            safe_content_type or "unknown",
            duration_seconds,
            len(audio),
        )
        raise EidosServiceError(
            502,
            _error_payload("eidos_unavailable", "Could not reach the Eidos service."),
        ) from exc
    finally:
        if owns_client:
            await active_client.aclose()

    try:
        speaker_ids = [
            value
            for name, value in os.environ.items()
            if name.startswith("EIDOS_")
            and name.endswith("_SPEAKER_ID")
            and value
        ]
        payload = _redact_private_values(response.json(), api_key, speaker_ids)
    except ValueError as exc:
        logger.warning(
            "eidos_response_invalid_json action=%s speaker_ref=%s http_status=%d "
            "audio_mime=%s audio_duration_seconds=%s audio_bytes=%d",
            action,
            speaker_ref,
            response.status_code,
            safe_content_type or "unknown",
            duration_seconds,
            len(audio),
        )
        raise EidosServiceError(
            502,
            _error_payload("invalid_eidos_response", "Eidos returned an invalid response."),
        ) from exc

    result = payload.get("result") if isinstance(payload, dict) else None
    eidos_decision = (
        result.get("decision")
        if isinstance(result, dict)
        and result.get("decision") in {"match", "no_match"}
        else "unavailable"
    )
    result_duration = (
        result.get("duration_seconds")
        if isinstance(result, dict)
        else None
    )
    if (
        not isinstance(result_duration, (int, float))
        or isinstance(result_duration, bool)
        or not math.isfinite(result_duration)
    ):
        result_duration = duration_seconds

    logger.info(
        "eidos_response action=%s speaker_ref=%s http_status=%d error_code=%s "
        "response_fields=%s eidos_decision=%s result_duration_seconds=%s",
        action,
        speaker_ref,
        response.status_code,
        _response_error_code(payload),
        _response_field_types(payload),
        eidos_decision,
        result_duration,
    )

    return response.status_code, payload


async def enroll_speaker(
    *,
    speaker_id: str,
    display_name: str,
    filename: str,
    content_type: str,
    audio: bytes,
    idempotency_key: str,
    client: Optional[httpx.AsyncClient] = None,
    capture_duration_seconds: Optional[float] = None,
) -> tuple[int, Any]:
    return await _post_audio_request(
        endpoint=ENROLLMENT_PATH,
        action="Voice enrollment",
        form_data={"speaker_id": speaker_id, "display_name": display_name},
        filename=filename,
        content_type=content_type,
        audio=audio,
        idempotency_key=idempotency_key,
        capture_duration_seconds=capture_duration_seconds,
        client=client,
    )


async def verify_speaker(
    *,
    speaker_id: str,
    filename: str,
    content_type: str,
    audio: bytes,
    detect_deepfake: bool,
    idempotency_key: str,
    client: Optional[httpx.AsyncClient] = None,
    capture_duration_seconds: Optional[float] = None,
) -> tuple[int, Any]:
    return await _post_audio_request(
        endpoint=VERIFICATION_PATH,
        action="Speaker verification",
        form_data={
            "speaker_id": speaker_id,
            "detect_deepfake": str(detect_deepfake).lower(),
        },
        filename=filename,
        content_type=content_type,
        audio=audio,
        idempotency_key=idempotency_key,
        capture_duration_seconds=capture_duration_seconds,
        client=client,
    )
