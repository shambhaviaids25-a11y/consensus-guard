import io
import json
import os
import wave
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import UploadFile
from starlette.datastructures import Headers

from backend.routes.audio_routes import _normalize_response, verify_audio
from backend.routes.enrollment import enroll_executive
from backend.services.eidos_service import (
    enroll_speaker,
    verify_speaker,
)


TEST_AUDIO_DIR = Path(__file__).resolve().parents[2] / "test-audio"
TEST_SPEAKER_ID = "voice-pipeline-test-speaker"
MATCH_RESPONSE = {
    "request_id": "req-test",
    "operation_id": "op-test",
    "object": "speaker.verification",
    "speaker_id": TEST_SPEAKER_ID,
    "result": {
        "decision": "match",
        "similarity": 0.7412,
        "duration_seconds": 4.1,
    },
}


def _recording_in_duration_range(minimum: int, maximum: int, exclude=None) -> Path:
    if not TEST_AUDIO_DIR.is_dir():
        raise FileNotFoundError("test-audio directory is unavailable")

    for path in sorted(TEST_AUDIO_DIR.rglob("*.wav")):
        if path == exclude:
            continue
        try:
            with wave.open(str(path), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError, OSError):
            continue
        if minimum <= duration <= maximum:
            return path
    raise FileNotFoundError("no WAV recording fits the requested duration")


def _upload(path: Path) -> UploadFile:
    return UploadFile(
        filename=path.name,
        file=io.BytesIO(path.read_bytes()),
        headers=Headers({"content-type": "audio/wav"}),
    )


class VoiceRouteDiagnosticTests(IsolatedAsyncioTestCase):
    async def test_enrollment_forwards_a_consent_recording_and_role_mapping(self):
        try:
            recording = _recording_in_duration_range(15, 60)
        except FileNotFoundError as error:
            self.skipTest(str(error))

        audio_bytes = recording.read_bytes()
        service_response = {
            "request_id": "req-enroll-test",
            "operation_id": "op-enroll-test",
            "object": "speaker.enrollment",
            "speaker": {"speaker_id": "[REDACTED]", "samples": 1},
            "quality": {"speech_seconds": 17.9, "duration_seconds": 19.2},
        }
        with (
            patch.dict(os.environ, {"EIDOS_CFO_SPEAKER_ID": TEST_SPEAKER_ID}),
            patch(
                "backend.routes.enrollment.enroll_speaker",
                new=AsyncMock(return_value=(200, service_response)),
            ) as enroll_mock,
        ):
            response = await enroll_executive("CFO", _upload(recording), None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), service_response)
        enroll_mock.assert_awaited_once()
        self.assertEqual(enroll_mock.await_args.kwargs["speaker_id"], TEST_SPEAKER_ID)
        self.assertEqual(enroll_mock.await_args.kwargs["audio"], audio_bytes)
        self.assertEqual(enroll_mock.await_args.kwargs["content_type"], "audio/wav")

    async def test_verification_maps_the_documented_match_response(self):
        try:
            enrollment_recording = _recording_in_duration_range(15, 60)
            verification_recording = _recording_in_duration_range(
                3, 60, exclude=enrollment_recording
            )
        except FileNotFoundError as error:
            self.skipTest(str(error))

        audio_bytes = verification_recording.read_bytes()
        with (
            patch.dict(os.environ, {"EIDOS_CFO_SPEAKER_ID": TEST_SPEAKER_ID}),
            patch(
                "backend.routes.audio_routes.verify_speaker",
                new=AsyncMock(return_value=(200, MATCH_RESPONSE)),
            ) as verify_mock,
            self.assertLogs("backend.routes.audio_routes", level="INFO") as captured,
        ):
            response = await verify_audio(
                "CFO", _upload(verification_recording), False, 10.0
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["identity_status"], "match")
        self.assertEqual(body["similarity"], 0.7412)
        verify_mock.assert_awaited_once()
        self.assertEqual(verify_mock.await_args.kwargs["speaker_id"], TEST_SPEAKER_ID)
        self.assertEqual(verify_mock.await_args.kwargs["audio"], audio_bytes)
        self.assertFalse(verify_mock.await_args.kwargs["detect_deepfake"])
        self.assertEqual(
            verify_mock.await_args.kwargs["capture_duration_seconds"], 10.0
        )
        log_output = "\n".join(captured.output)
        self.assertIn("final_decision=match reason=eidos_match", log_output)
        self.assertIn("similarity_present=True", log_output)
        self.assertNotIn(TEST_SPEAKER_ID, log_output)

    def test_match_without_required_similarity_is_malformed_not_a_match(self):
        status_code, body = _normalize_response(
            "CFO",
            False,
            200,
            {"result": {"decision": "match"}},
        )

        self.assertEqual(status_code, 502)
        self.assertEqual(body["identity_status"], "error")
        self.assertEqual(body["errors"][0]["error"]["code"], "missing_similarity")

    async def test_no_match_and_upstream_audio_error_remain_distinct(self):
        try:
            recording = _recording_in_duration_range(3, 60)
        except FileNotFoundError as error:
            self.skipTest(str(error))

        no_match_response = {
            **MATCH_RESPONSE,
            "result": {
                **MATCH_RESPONSE["result"],
                "decision": "no_match",
            },
        }
        with (
            patch.dict(os.environ, {"EIDOS_CFO_SPEAKER_ID": TEST_SPEAKER_ID}),
            patch(
                "backend.routes.audio_routes.verify_speaker",
                new=AsyncMock(return_value=(200, no_match_response)),
            ),
        ):
            no_match = await verify_audio("CFO", _upload(recording), False, None)
        self.assertEqual(json.loads(no_match.body)["identity_status"], "no_match")

        authentication_error = {
            "error": {
                "type": "authentication",
                "code": "invalid_api_key",
                "message": "The API key is invalid.",
            },
            "request_id": "req-auth-error",
        }
        with (
            patch.dict(os.environ, {"EIDOS_CFO_SPEAKER_ID": TEST_SPEAKER_ID}),
            patch(
                "backend.routes.audio_routes.verify_speaker",
                new=AsyncMock(return_value=(401, authentication_error)),
            ),
        ):
            api_error = await verify_audio("CFO", _upload(recording), False, None)

        auth_body = json.loads(api_error.body)
        self.assertEqual(api_error.status_code, 401)
        self.assertEqual(auth_body["identity_status"], "error")
        self.assertEqual(auth_body["errors"][0]["error"]["code"], "invalid_api_key")

        upstream_error = {
            "error": {
                "type": "unprocessable_audio",
                "code": "insufficient_speech",
                "message": "At least 2 seconds of speech are required.",
                "param": "file",
            },
            "request_id": "req-invalid-audio",
        }
        with (
            patch.dict(os.environ, {"EIDOS_CFO_SPEAKER_ID": TEST_SPEAKER_ID}),
            patch(
                "backend.routes.audio_routes.verify_speaker",
                new=AsyncMock(return_value=(422, upstream_error)),
            ),
        ):
            invalid_audio = await verify_audio("CFO", _upload(recording), False, None)

        error_body = json.loads(invalid_audio.body)
        self.assertEqual(invalid_audio.status_code, 422)
        self.assertEqual(error_body["identity_status"], "error")
        self.assertEqual(error_body["errors"][0]["error"]["code"], "insufficient_speech")
        self.assertEqual(error_body["request_id"], "req-invalid-audio")


class EidosRequestContractTests(IsolatedAsyncioTestCase):
    async def test_enrollment_and_verification_use_documented_multipart_fields(self):
        try:
            enrollment_recording = _recording_in_duration_range(15, 60)
            verification_recording = _recording_in_duration_range(
                3, 60, exclude=enrollment_recording
            )
        except FileNotFoundError as error:
            self.skipTest(str(error))

        enrollment_audio = enrollment_recording.read_bytes()
        verification_audio = verification_recording.read_bytes()
        requests_seen = []

        def respond(request: httpx.Request) -> httpx.Response:
            body = request.read()
            requests_seen.append((request, body))
            if request.url.path.endswith("/enroll"):
                return httpx.Response(200, json={
                    "request_id": "req-enroll-test",
                    "operation_id": "op-enroll-test",
                    "object": "speaker.enrollment",
                    "speaker": {"speaker_id": TEST_SPEAKER_ID, "samples": 1},
                })
            return httpx.Response(200, json=MATCH_RESPONSE)

        with (
            patch.dict(os.environ, {"DETECTIF_API_KEY": "test-key-not-a-credential"}),
            self.assertLogs("backend.services.eidos_service", level="INFO") as captured,
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                enrolled = await enroll_speaker(
                    speaker_id=TEST_SPEAKER_ID,
                    display_name="Test Speaker",
                    filename="enrollment.wav",
                    content_type="audio/wav",
                    audio=enrollment_audio,
                    idempotency_key="enrollment-test-key",
                    client=client,
                )
                verified = await verify_speaker(
                    speaker_id=TEST_SPEAKER_ID,
                    filename="verification.wav",
                    content_type="audio/wav",
                    audio=verification_audio,
                    detect_deepfake=False,
                    idempotency_key="verification-test-key",
                    client=client,
                )

        self.assertEqual(enrolled[0], 200)
        self.assertEqual(enrolled[1]["speaker"]["speaker_id"], "[REDACTED]")
        self.assertEqual(verified[0], 200)
        self.assertEqual(verified[1]["speaker_id"], "[REDACTED]")
        self.assertEqual(verified[1]["result"]["decision"], "match")
        self.assertEqual(len(requests_seen), 2)
        for index, (request, body) in enumerate(requests_seen):
            self.assertEqual(request.headers["Authorization"], "Bearer test-key-not-a-credential")
            self.assertIn(b'name="speaker_id"', body)
            self.assertIn(b"Content-Type: audio/wav", body)
            self.assertIn(TEST_SPEAKER_ID.encode(), body)
            filename = "enrollment.wav" if index == 0 else "verification.wav"
            audio_bytes = enrollment_audio if index == 0 else verification_audio
            self.assertIn(f'name="file"; filename="{filename}"'.encode(), body)
            self.assertIn(filename.encode(), body)
            self.assertIn(audio_bytes, body)
            self.assertNotIn(b"capture_duration_seconds", body)
            if index == 0:
                self.assertIn(b'name="display_name"\r\n\r\nTest Speaker', body)
            else:
                self.assertIn(b'name="detect_deepfake"\r\n\r\nfalse', body)
        self.assertTrue(requests_seen[0][0].url.path.endswith("/speakers/enroll"))
        self.assertTrue(requests_seen[1][0].url.path.endswith("/speakers/verify"))

        log_output = "\n".join(captured.output)
        self.assertIn("audio_mime=audio/wav", log_output)
        self.assertIn("duration_source=wav_header", log_output)
        self.assertIn(f"audio_bytes={len(audio_bytes)}", log_output)
        self.assertIn("http_status=200", log_output)
        self.assertIn("result.decision:string", log_output)
        self.assertRegex(log_output, r"speaker_ref=[0-9a-f]{16}")
        self.assertNotIn(TEST_SPEAKER_ID, log_output)
        self.assertNotIn("test-key-not-a-credential", log_output)
        self.assertNotIn(enrollment_recording.name, log_output)
        self.assertNotIn(verification_recording.name, log_output)

    async def test_response_error_code_is_logged_without_error_message(self):
        try:
            recording = _recording_in_duration_range(3, 60)
        except FileNotFoundError as error:
            self.skipTest(str(error))

        error_message = "The audio contains too little speech."
        error_response = {
            "error": {
                "type": "unprocessable_audio",
                "code": "insufficient_speech",
                "message": error_message,
                "param": "file",
            },
            "request_id": "req-error-test",
        }

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json=error_response)

        with (
            patch.dict(os.environ, {"DETECTIF_API_KEY": "test-key-not-a-credential"}),
            self.assertLogs("backend.services.eidos_service", level="INFO") as captured,
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                status_code, _ = await verify_speaker(
                    speaker_id=TEST_SPEAKER_ID,
                    filename="diagnostic.wav",
                    content_type="audio/wav",
                    audio=recording.read_bytes(),
                    detect_deepfake=False,
                    idempotency_key="verification-error-test-key",
                    client=client,
                )

        self.assertEqual(status_code, 422)
        log_output = "\n".join(captured.output)
        self.assertIn("error_code=insufficient_speech", log_output)
        self.assertIn("http_status=422", log_output)
        self.assertNotIn(error_message, log_output)
        self.assertNotIn("test-key-not-a-credential", log_output)
        self.assertNotIn(TEST_SPEAKER_ID, log_output)

    async def test_non_object_json_response_is_classified_as_malformed(self):
        try:
            recording = _recording_in_duration_range(3, 60)
        except FileNotFoundError as error:
            self.skipTest(str(error))

        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["unexpected", "array"])

        with (
            patch.dict(os.environ, {"DETECTIF_API_KEY": "test-key-not-a-credential"}),
            self.assertLogs("backend.services.eidos_service", level="INFO") as captured,
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(respond)
            ) as client:
                status_code, payload = await verify_speaker(
                    speaker_id=TEST_SPEAKER_ID,
                    filename="malformed.wav",
                    content_type="audio/wav",
                    audio=recording.read_bytes(),
                    detect_deepfake=False,
                    idempotency_key="verification-malformed-test-key",
                    client=client,
                )

        status_code, body = _normalize_response("CFO", False, status_code, payload)
        self.assertEqual(status_code, 502)
        self.assertEqual(body["identity_status"], "error")
        self.assertEqual(body["errors"][0]["error"]["code"], "invalid_eidos_response")
        log_output = "\n".join(captured.output)
        self.assertIn("response_fields=$:array", log_output)
        self.assertNotIn("test-key-not-a-credential", log_output)
        self.assertNotIn(TEST_SPEAKER_ID, log_output)
