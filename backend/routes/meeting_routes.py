from typing import Dict, Any, Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.services.challenge import (
    generate_meeting_challenge,
    get_meeting_challenge,
    verify_challenge_response,
    issue_or_verify_secondary_confirmation,
)
from backend.services.quorum import evaluate_quorum, REQUIRED_ROLES
from backend.services.audit import record_audit_event, get_meeting_audit_history

router = APIRouter(prefix="/api/meeting", tags=["Meeting, Quorum & Audit"])

# In-memory meeting state keyed by meeting_id
_MEETING_STATE: Dict[str, Dict[str, Any]] = {}


def _get_or_init_meeting(meeting_id: str) -> Dict[str, Any]:
    mid = meeting_id.strip()
    if mid not in _MEETING_STATE:
        _MEETING_STATE[mid] = {
            "meeting_id": mid,
            "participants": {
                role: {
                    "approved": None,
                    "voice_identity_status": "pending",
                    "audio_authenticity_status": "pending",
                    "video_status": "pending",
                    "challenge_completed": False,
                    "secondary_required": False,
                    "secondary_confirmed": False,
                    "eidos_request_id": None,
                }
                for role in REQUIRED_ROLES
            },
            "last_decision": None,
        }
    return _MEETING_STATE[mid]


class StartMeetingRequest(BaseModel):
    meeting_id: str = Field(..., example="CG-2026-001")
    ttl_minutes: int = Field(default=15, ge=1, le=120)


class ChallengeVerifyRequest(BaseModel):
    meeting_id: str = Field(..., example="CG-2026-001")
    role: str = Field(..., example="CFO")
    submitted_phrase: str = Field(..., example="ORION - 7392")


class SecondaryConfirmRequest(BaseModel):
    meeting_id: str = Field(..., example="CG-2026-001")
    role: str = Field(..., example="CFO")
    submitted_code: Optional[str] = Field(default=None, example="SEC-123456")


class ParticipantUpdateRequest(BaseModel):
    meeting_id: str = Field(..., example="CG-2026-001")
    role: str = Field(..., example="CFO")
    approved: Optional[bool] = None
    voice_identity_status: Optional[str] = Field(default=None, example="match")
    audio_authenticity_status: Optional[str] = Field(default=None, example="real")
    video_status: Optional[str] = Field(default=None, example="no_suspicious_indicators")
    secondary_required: Optional[bool] = None
    eidos_request_id: Optional[str] = None


class EvaluateQuorumRequest(BaseModel):
    meeting_id: str = Field(..., example="CG-2026-001")
    require_audio_authenticity: bool = True
    require_video_authenticity: bool = True
    require_secondary_confirmation: bool = False
    require_voice_identity: bool = True
    require_challenge: bool = True


@router.post("/challenge/start")
def start_meeting_challenge(payload: StartMeetingRequest):
    meeting = _get_or_init_meeting(payload.meeting_id)
    challenge = generate_meeting_challenge(
        meeting_id=payload.meeting_id,
        ttl_minutes=payload.ttl_minutes,
    )
    for role in REQUIRED_ROLES:
        meeting["participants"][role]["challenge_completed"] = False
        meeting["participants"][role]["secondary_confirmed"] = False

    record_audit_event(
        meeting_id=payload.meeting_id,
        event_type="CHALLENGE_GENERATED",
        reason=f"Generated live challenge phrase '{challenge['phrase']}'.",
        metadata={"expires_at": challenge["expires_at"]},
    )
    return challenge


@router.get("/challenge/{meeting_id}")
def fetch_meeting_challenge(meeting_id: str):
    challenge = get_meeting_challenge(meeting_id)
    if not challenge:
        raise HTTPException(status_code=404, detail=f"No active challenge for meeting '{meeting_id}'.")
    return challenge


@router.post("/challenge/verify")
def submit_challenge_response(payload: ChallengeVerifyRequest):
    role = payload.role.strip().upper()
    if role not in REQUIRED_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{payload.role}'.")

    result = verify_challenge_response(
        meeting_id=payload.meeting_id,
        role=role,
        submitted_phrase=payload.submitted_phrase,
    )
    meeting = _get_or_init_meeting(payload.meeting_id)
    if result["valid"]:
        meeting["participants"][role]["challenge_completed"] = True

    record_audit_event(
        meeting_id=payload.meeting_id,
        event_type="CHALLENGE_SUBMISSION",
        role=role,
        approval_status="CHALLENGE_PASSED" if result["valid"] else "CHALLENGE_FAILED",
        reason=result["reason"],
    )
    if not result["valid"]:
        raise HTTPException(status_code=400, detail=result["reason"])
    return result


@router.post("/secondary-confirm")
def handle_secondary_confirmation(payload: SecondaryConfirmRequest):
    role = payload.role.strip().upper()
    if role not in REQUIRED_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{payload.role}'.")

    res = issue_or_verify_secondary_confirmation(
        meeting_id=payload.meeting_id,
        role=role,
        submitted_code=payload.submitted_code,
    )
    meeting = _get_or_init_meeting(payload.meeting_id)
    if res.get("confirmed"):
        meeting["participants"][role]["secondary_confirmed"] = True

    if payload.submitted_code is not None:
        record_audit_event(
            meeting_id=payload.meeting_id,
            event_type="SECONDARY_CONFIRMATION",
            role=role,
            approval_status="CONFIRMED" if res.get("confirmed") else "FAILED",
            reason=res.get("reason"),
        )
        if not res.get("confirmed"):
            raise HTTPException(status_code=400, detail=res.get("reason"))

    return res


@router.post("/participant/update")
def update_participant_signals(payload: ParticipantUpdateRequest):
    """
    Records verification outcomes from Person 1's Eidos voice check, Person 2's video check,
    or the executive's explicit approve/reject action, and writes a tamper-evident audit entry.
    """
    role = payload.role.strip().upper()
    if role not in REQUIRED_ROLES:
        raise HTTPException(status_code=400, detail=f"Invalid role '{payload.role}'.")

    meeting = _get_or_init_meeting(payload.meeting_id)
    p_record = meeting["participants"][role]

    if payload.approved is not None:
        p_record["approved"] = payload.approved
    if payload.voice_identity_status is not None:
        p_record["voice_identity_status"] = payload.voice_identity_status.strip().lower()
    if payload.audio_authenticity_status is not None:
        p_record["audio_authenticity_status"] = payload.audio_authenticity_status.strip().lower()
    if payload.video_status is not None:
        p_record["video_status"] = payload.video_status.strip().lower()
    if payload.secondary_required is not None:
        p_record["secondary_required"] = payload.secondary_required
    if payload.eidos_request_id is not None:
        p_record["eidos_request_id"] = payload.eidos_request_id

    audit_entry = record_audit_event(
        meeting_id=payload.meeting_id,
        event_type="PARTICIPANT_SIGNAL_RECORDED",
        role=role,
        eidos_request_id=p_record["eidos_request_id"],
        identity_result=p_record["voice_identity_status"],
        audio_result=p_record["audio_authenticity_status"],
        video_result=p_record["video_status"],
        approval_status=(
            "APPROVED" if p_record["approved"] is True
            else "REJECTED" if p_record["approved"] is False
            else "PENDING"
        ),
        reason=f"Updated verification state for {role}.",
    )

    return {
        "meeting_id": payload.meeting_id.strip(),
        "role": role,
        "participant_state": p_record,
        "audit_hash": audit_entry["event_hash"],
    }


@router.post("/quorum/evaluate")
def run_quorum_evaluation(payload: EvaluateQuorumRequest):
    meeting = _get_or_init_meeting(payload.meeting_id)
    decision_report = evaluate_quorum(
        participants=meeting["participants"],
        require_audio_authenticity=payload.require_audio_authenticity,
        require_video_authenticity=payload.require_video_authenticity,
        require_secondary_confirmation=payload.require_secondary_confirmation,
        require_voice_identity=payload.require_voice_identity,
        require_challenge=payload.require_challenge,
    )
    meeting["last_decision"] = decision_report

    audit_entry = record_audit_event(
        meeting_id=payload.meeting_id,
        event_type="QUORUM_DECISION",
        approval_status=decision_report["decision"],
        reason=decision_report["summary"],
        metadata={
            "verified_roles": decision_report["verified_roles"],
            "suspicious_roles": decision_report["suspicious_roles"],
            "unresolved_roles": decision_report["unresolved_roles"],
        },
    )
    return {
        **decision_report,
        "audit_sequence_id": audit_entry["sequence_id"],
        "audit_hash": audit_entry["event_hash"],
    }


@router.get("/state/{meeting_id}")
def get_meeting_state(meeting_id: str):
    return _get_or_init_meeting(meeting_id)


@router.get("/audit/{meeting_id}")
def get_audit_trail(meeting_id: str):
    return get_meeting_audit_history(meeting_id)


@router.get("/audit/{meeting_id}/download")
def download_audit_report(meeting_id: str):
    report = get_meeting_audit_history(meeting_id)
    filename = f"consensusguard_audit_{meeting_id.strip()}.json"
    return JSONResponse(
        content=report,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )