from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

REQUIRED_ROLES = ("CFO", "VP_SECURITY", "LEGAL")
QUORUM_THRESHOLD = 2


def evaluate_participant(
    role: str,
    record: Optional[Dict[str, Any]],
    require_audio_authenticity: bool = True,
    require_video_authenticity: bool = True,
    require_secondary_confirmation: bool = False,
) -> Dict[str, Any]:
    """
    Evaluates a single executive's verification evidence.
    Fails closed: missing, inconclusive, unavailable, or suspicious checks
    prevent the participant from counting toward quorum.
    """
    normalized_role = role.strip().upper()
    if normalized_role not in REQUIRED_ROLES:
        return {
            "role": normalized_role,
            "verified": False,
            "status": "INVALID_ROLE",
            "reasons": [f"Unknown executive role '{role}'."],
            "checks": {},
        }

    if not record or not isinstance(record, dict):
        return {
            "role": normalized_role,
            "verified": False,
            "status": "PENDING",
            "reasons": ["No verification data submitted for this executive yet."],
            "checks": {},
        }

    reasons: List[str] = []
    suspicious_flags: List[str] = []

    # 1. Explicit Executive Approval
    explicit_approval = bool(record.get("approved") is True)
    if record.get("approved") is False:
        reasons.append("Executive explicitly rejected the transaction.")
    elif not explicit_approval:
        reasons.append("Explicit executive approval has not been granted.")

    # 2. Eidos Speaker Identity Verification
    voice_status = str(record.get("voice_identity_status") or "missing").strip().lower()
    voice_passed = voice_status == "match"
    if not voice_passed:
        if voice_status in {"no_match", "mismatch", "spoof", "suspicious"}:
            suspicious_flags.append(f"Voice identity check failed ({voice_status}).")
        else:
            reasons.append(f"Voice identity unresolved ({voice_status}).")

    # 3. Audio Synthetic-Media Authenticity (when enabled)
    audio_status = str(record.get("audio_authenticity_status") or "missing").strip().lower()
    if require_audio_authenticity:
        audio_passed = audio_status == "real"
        if not audio_passed:
            if audio_status in {"fake", "synthetic", "deepfake", "suspicious"}:
                suspicious_flags.append(f"Audio authenticity flagged synthetic media ({audio_status}).")
            else:
                reasons.append(f"Audio authenticity unresolved ({audio_status}).")
    else:
        audio_passed = audio_status not in {"fake", "synthetic", "deepfake", "suspicious"}
        if not audio_passed:
            suspicious_flags.append(f"Audio authenticity flagged synthetic media ({audio_status}).")

    # 4. Video Deepfake Analysis (when required)
    video_status = str(record.get("video_status") or "missing").strip().lower()
    if require_video_authenticity:
        video_passed = video_status == "no_suspicious_indicators"
        if not video_passed:
            if video_status == "suspicious":
                suspicious_flags.append("Video analysis detected suspicious manipulation indicators.")
            else:
                reasons.append(f"Video authenticity unresolved ({video_status}).")
    else:
        video_passed = video_status != "suspicious"
        if not video_passed:
            suspicious_flags.append("Video analysis detected suspicious manipulation indicators.")

    # 5. Live Meeting Challenge
    challenge_completed = bool(record.get("challenge_completed") is True)
    if not challenge_completed:
        reasons.append("Live meeting challenge has not been completed.")

    # 6. Secondary Out-of-Band Confirmation (required when triggered or policy-enforced)
    secondary_required = require_secondary_confirmation or bool(record.get("secondary_required") is True)
    secondary_completed = bool(record.get("secondary_confirmed") is True)
    if secondary_required and not secondary_completed:
        reasons.append("Secondary confirmation code has not been verified.")
    secondary_passed = (not secondary_required) or secondary_completed

    all_reasons = suspicious_flags + reasons
    is_verified = (
        explicit_approval
        and voice_passed
        and audio_passed
        and video_passed
        and challenge_completed
        and secondary_passed
        and len(suspicious_flags) == 0
    )

    if suspicious_flags:
        participant_status = "SUSPICIOUS"
    elif record.get("approved") is False:
        participant_status = "EXPLICITLY_REJECTED"
    elif is_verified:
        participant_status = "VERIFIED_APPROVAL"
    else:
        participant_status = "UNRESOLVED"

    return {
        "role": normalized_role,
        "verified": is_verified,
        "status": participant_status,
        "reasons": all_reasons,
        "checks": {
            "explicit_approval": explicit_approval,
            "voice_identity": voice_passed,
            "audio_authenticity": audio_passed,
            "video_authenticity": video_passed,
            "challenge_completed": challenge_completed,
            "secondary_confirmation": secondary_passed,
        },
    }


def evaluate_quorum(
    participants: Dict[str, Dict[str, Any]],
    require_audio_authenticity: bool = True,
    require_video_authenticity: bool = True,
    require_secondary_confirmation: bool = False,
) -> Dict[str, Any]:
    """
    Evaluates the 2-of-3 executive quorum policy across CFO, VP_SECURITY, and LEGAL.
    Returns APPROVED, REJECTED, or ON_HOLD with explainable reasons.
    """
    safe_participants = participants if isinstance(participants, dict) else {}
    normalized_map = {str(k).strip().upper(): v for k, v in safe_participants.items()}

    evaluations: Dict[str, Dict[str, Any]] = {}
    verified_roles: List[str] = []
    suspicious_roles: List[str] = []
    rejected_roles: List[str] = []
    unresolved_roles: List[str] = []

    for role in REQUIRED_ROLES:
        res = evaluate_participant(
            role=role,
            record=normalized_map.get(role),
            require_audio_authenticity=require_audio_authenticity,
            require_video_authenticity=require_video_authenticity,
            require_secondary_confirmation=require_secondary_confirmation,
        )
        evaluations[role] = res

        if res["verified"]:
            verified_roles.append(role)
        elif res["status"] == "SUSPICIOUS":
            suspicious_roles.append(role)
        elif res["status"] == "EXPLICITLY_REJECTED":
            rejected_roles.append(role)
        else:
            unresolved_roles.append(role)

    verified_count = len(verified_roles)
    remaining_possible = verified_count + len(unresolved_roles)

    # Decision policy:
    # 1. Any active security/deepfake/spoof alert blocks immediate approval -> ON_HOLD (or REJECTED if quorum impossible)
    # 2. If 2+ verified approvals and zero suspicious flags -> APPROVED
    # 3. If fewer than 2 approvals remain mathematically possible -> REJECTED
    # 4. Otherwise -> ON_HOLD awaiting pending/unresolved verification
    if remaining_possible < QUORUM_THRESHOLD:
        decision = "REJECTED"
        summary = (
            f"Transaction REJECTED: Only {verified_count} verified approval(s) achieved, and "
            f"fewer than {QUORUM_THRESHOLD} eligible executives remain."
        )
    elif suspicious_roles:
        decision = "ON_HOLD"
        flagged = ", ".join(suspicious_roles)
        summary = (
            f"Transaction ON HOLD: Security anomaly detected for {flagged}. "
            f"Requires secondary verification or investigation before authorization."
        )
    elif verified_count >= QUORUM_THRESHOLD:
        decision = "APPROVED"
        summary = (
            f"Transaction APPROVED: Quorum satisfied ({verified_count}/3 verified approvals: "
            f"{', '.join(verified_roles)})."
        )
    else:
        decision = "ON_HOLD"
        summary = (
            f"Transaction ON HOLD: {verified_count}/{QUORUM_THRESHOLD} required verified approvals completed. "
            f"Awaiting unresolved checks for: {', '.join(unresolved_roles)}."
        )

    return {
        "decision": decision,  # APPROVED | ON_HOLD | REJECTED
        "quorum_required": QUORUM_THRESHOLD,
        "verified_count": verified_count,
        "verified_roles": verified_roles,
        "suspicious_roles": suspicious_roles,
        "rejected_roles": rejected_roles,
        "unresolved_roles": unresolved_roles,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "participants": evaluations,
    }