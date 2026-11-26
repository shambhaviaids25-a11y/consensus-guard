# ConsensusGuard

**Real-Time Multimodal Biometric & Deepfake Verification for High-Stakes Video Calls**

ConsensusGuard is a Chrome extension and FastAPI security sidecar designed to prevent real-time executive impersonation and deepfake fraud during live video meetings (such as Google Meet). Before sensitive financial, legal, or operational approvals take place, ConsensusGuard verifies both **who is speaking** (biometric voice identity) and **whether the stream is live and authentic** (visual forensic & audio deepfake detection), enforcing a **2/2 Quorum** policy and generating a tamper-evident **Audit Report**.

---

## Key Features

* **2/2 Multimodal Quorum Verification:** Requires both **Voice Identity (`Match`)** and **Video Authenticity (`Authentic`)** to pass simultaneously before granting `APPROVED (2/2)` status—preventing single-modality bypasses (voice-only clones or silent face swaps).
* **Executive Role-Based Biometric Verification:** Maps privileged meeting roles (`CFO`, `COO`, `VP`, `HOL`) to enrolled speaker voiceprints via the **Eidos** verification API, with audio deepfake detection and graceful fallback handling.
* **Lightweight Real-Time Video Forensics:** Analyzes captured video frames using **OpenCV** and **NumPy**—combining **2D Fast Fourier Transform (2D-FFT)** frequency spectrum analysis (to catch screen-replay moiré and generative upsampling artifacts) with facial ROI skin-texture consistency checks.
* **Consent-First Chrome Extension (`Manifest V3`):** Captures tab audio and video only after the user explicitly selects a role and clicks **Start Scan**, displaying live per-modality status (`Loading...`, `Match`, `Authentic`, `APPROVED (2/2)`) and a persistent `Capture active` indicator.
* **Compliance Audit Trail:** Logs verification metadata (`request_id`, `operation_id`, role, timestamps, similarity scores, and Quorum decisions) without storing raw call media, allowing one-click **Audit Report** downloads.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Google Meet Browser Tab                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │ User clicks "Start Scan" (Role)
                                ▼
┌─────────────────────────────────────────────────────────────────┐
│             ConsensusGuard Chrome Extension (MV3)               │
│  • popup.html / popup.js  → Live UI & Quorum status dashboard   │
│  • background.js          → Session orchestration & API calls   │
│  • offscreen.js           → Tab audio/video segment capture     │
└───────────────────┬───────────────────────────┬─────────────────┘
                    │ POST /api/verify/audio    │ POST /api/verify/video
                    ▼                           ▼
┌─────────────────────────────────────────────────────────────────┐
│                   FastAPI Security Backend                      │
│  • audio_routes.py  → Role mapping & Eidos voice verification   │
│  • video_routes.py  → Frame extraction & 2D-FFT/ROI forensics   │
│  • Quorum Engine    → Combines Voice + Video into 2/2 verdict   │
│  • Audit Endpoint   → /api/meeting/audit/{meeting_id}/download  │
└─────────────────────────────────────────────────────────────────┘


consensus-guard/
├── backend/
│   ├── main.py                  # FastAPI application entrypoint & router setup
│   ├── routes/
│   │   ├── audio_routes.py      # /api/verify/audio endpoint & Eidos integration
│   │   ├── video_routes.py      # /api/verify/video endpoint & Quorum state updates
│   │   └── enrollment.py        # Executive role-to-speaker ID mapping & validation
│   └── services/
│       ├── eidos_service.py     # Upstream Eidos biometric & deepfake API client
│       └── video_service.py     # OpenCV / NumPy 2D-FFT & facial ROI forensic checks
├── extension/
│   ├── manifest.json            # Chrome Extension Manifest V3 configuration
│   ├── popup.html / popup.js    # Extension UI, role selector & live status rendering
│   ├── background.js            # Service worker managing scan lifecycle & state
│   └── offscreen.js             # Offscreen document for tabCapture media recording
├── .env.example                 # Environment variable template
└── requirements.txt             # Python dependencies



Getting Started
1. Prerequisites
Python 3.10+
Google Chrome (for loading the unpacked Manifest V3 extension)
Eidos / Detectif API Key (configured in .env)

2. Backend Setup
Clone the repository and navigate to the project root:
Bash
git clone [https://github.com/](https://github.com/)<your-org>/consensus-guard.git
cd consensus-guard
Create your local environment file from .env.example:

Bash
cp .env.example .env
Configure your API key and enrolled executive speaker IDs inside .env:

Code snippet
DETECTIF_API_KEY=your_api_key_here
EIDOS_CFO_SPEAKER_ID=cfo
EIDOS_COO_SPEAKER_ID=coo
EIDOS_VP_SPEAKER_ID=vp
EIDOS_HOL_SPEAKER_ID=hol
Install Python dependencies:

Bash
pip install -r requirements.txt
pip install opencv-python-headless numpy fastapi uvicorn python-multipart httpx
Start the FastAPI server from the repository root:

Bash
python -m uvicorn backend.main:app --reload
The backend will run at http://127.0.0.1:8000 (API docs available at http://127.0.0.1:8000/docs).

3. Chrome Extension Setup
Open chrome://extensions in Google Chrome.
Enable Developer mode (top-right toggle).
Click Load unpacked and select the extension/ folder from this repository.
Pin ConsensusGuard to your Chrome toolbar.

Usage
Join an active Google Meet call.
Click the ConsensusGuard extension icon and confirm Backend connection: Connected.
Select the Executive role to verify (e.g., CFO, COO, VP, or HOL).
Click Start Scan while the participant speaks on camera:
Voice identity evaluates biometric speaker match.
Audio authenticity checks for synthetic/cloned voice artifacts.
Video analysis verifies live webcam authenticity against replay/deepfake artifacts.
Quorum updates to APPROVED (2/2) once both voice and video checks pass.
Click Download Audit Report to export the session's verification record.

Privacy & Security Design
Explicit User Consent: Media capture only begins when a participant clicks Start Scan and stops immediately on Stop Scan.
Ephemeral Processing: Audio and video segments are processed in memory and discarded after scoring; raw audio/video streams are never persisted to disk.
Enterprise Scalability Path: Designed to transition from client-side tab capture to server-side SFU media taps triggered by active-speaker Voice Activity Detection (VAD) and privileged role registries for large enterprise calls.
