import os
import re
from typing import Any, Optional

import httpx


EIDOS_BASE_URL = "https://platform.detectif.ai"
ENROLLMENT_PATH = "/v1/speakers/enroll"
VERIFICATION_PATH = "/v1/speakers/verify"
REQUEST_TIMEOUT = httpx.Timeout(130.0, connect=10.0)


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
) -> tuple[int, Any]:
    api_key = os.getenv("DETECTIF_API_KEY")
    if not api_key:
        raise EidosServiceError(
            503,
            _error_payload(
                "eidos_not_configured",
                f"{action} is unavailable because the Eidos API key is not configured.",
            ),
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
        raise EidosServiceError(
            504,
            _error_payload(
                "eidos_timeout",
                f"The Eidos {action.lower()} request timed out. Retry with the same audio.",
            ),
        ) from exc
    except httpx.RequestError as exc:
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
        raise EidosServiceError(
            502,
            _error_payload("invalid_eidos_response", "Eidos returned an invalid response."),
        ) from exc

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
) -> tuple[int, Any]:
    return await _post_audio_request(
        endpoint=ENROLLMENT_PATH,
        action="Voice enrollment",
        form_data={"speaker_id": speaker_id, "display_name": display_name},
        filename=filename,
        content_type=content_type,
        audio=audio,
        idempotency_key=idempotency_key,
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
        client=client,
    )
