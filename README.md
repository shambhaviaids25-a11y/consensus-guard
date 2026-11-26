# consensus-guard

## Backend voice enrollment

From the repository root, install the backend dependencies and add the four speaker
ID variables shown in `backend\.env.example` to the existing `backend\.env`. Keep its
existing API key entry; fill the IDs from the existing Eidos profiles:

```powershell
python -m pip install -r backend\requirements.txt
python -m uvicorn backend.main:app --reload
```

Python 3.14 is the conservative recommended runtime. The pinned OpenCV release
provides a CPython stable-ABI wheel that I verified installs and runs on Python
3.15, but its published Python classifiers currently stop at 3.14, so 3.15 is
not yet explicitly claimed by OpenCV.

The API listens at `http://127.0.0.1:8000`. Enrollment is available at `POST /api/enroll`
as `multipart/form-data` with a `role` (`CFO`, `COO`, `VP` or `HOL`) and an `audio` file.
Eidos validates that decoded enrollment audio is 15–60 seconds and contains at least
10 seconds of speech. Each role maps to its existing Eidos profile using its matching
`EIDOS_*_SPEAKER_ID` value. These IDs and the API key must stay in `backend\.env` and
must not be put in frontend or extension code. Missing or invalid speaker IDs are
rejected before an Eidos request is made.
The existing UI role values `VP_SECURITY` and `LEGAL` remain accepted as aliases for
`VP` and `HOL`, respectively.

`backend\main.py` loads `backend\.env` at startup. The local `.env` file is ignored by
Git; only the empty variable names are shown in the example file.

To try the endpoint from PowerShell after starting the server:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/enroll `
  -F "role=CFO" `
  -F "audio=@C:\path\to\enrollment.wav;type=audio/wav"
```

The enrollment response omits the private Eidos speaker ID. Eidos API errors are
returned with their HTTP status and sanitized error body; configure a key with the
`speakers:enroll` scope and sufficient credits.

## Chrome extension tab-audio verification

The Manifest V3 extension is in `extension\`. It captures audio only from the
active supported Google Meet, Microsoft Teams, or Zoom tab after you click
**Start Scan**. It does not request microphone access or capture video. The
selected role (CFO, COO, VP, or HOL) is the comparison target; Chrome tab capture
provides the tab's mixed audio and does not isolate a particular speaker. Per the
[Eidos documentation](https://platform.detectif.ai/docs/speaker-verification/),
verification accepts 3–60 seconds of audio with at least 2 seconds of speech, so a
10-second segment containing too little speech is rejected rather than matched.

DetectifAI's [API documentation](https://platform.detectif.ai/docs/) says successfully
read recordings are retained for 30 days and enrollment and verification consume
account credits. Submit only recordings whose speakers have consented.

To load or reload it in Chrome:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Click **Load unpacked** (or **Reload** for an already-loaded copy) and select
   `C:\Users\punya\consensus-guard\extension`.
3. Ensure the FastAPI backend is running at `http://localhost:8000`. Open a
   supported meeting tab and refresh it if it was open before loading the extension.
4. Open the ConsensusGuard popup, confirm the backend says **Connected**, select
   the executive role, and click **Start Scan**.
5. Confirm Chrome shows tab-audio capture and meeting audio remains audible.
   The extension records standalone WebM audio segments of about 10 seconds and
   sends them sequentially as multipart `role`, `audio`, and
   `detect_deepfake=false` fields to `POST /api/verify/audio`. It also sends the
   measured segment duration to the backend for diagnostics; that field is not
   forwarded to Eidos.
6. Review the backend decision, similarity, request/operation IDs, and returned
   response in the popup. Click **Stop Scan** to stop recording, end the audio
   tracks, close the AudioContext, and stop further uploads.

Unsupported tabs and capture failures are reported in the popup. If the backend
disconnects or rejects a segment, capture continues and that segment is shown as
inconclusive rather than as a successful verification. This prototype does not
separate speakers or automatically detect overlapping speech; a backend
`no_match` result on mixed audio is not proof that the selected executive did
not speak.