import secrets
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional

CODE_WORDS = (
    "ORION", "FALCON", "ATLAS", "NEXUS", "AEGIS",
    "ZENITH", "VORTEX", "COBALT", "SUMMIT", "PRISM",
)
VALID_ROLES = ("CFO", "VP_SECURITY", "LEGAL")

# In-memory store keyed by meeting_id for rapid hackathon prototype access
_MEETING_CHALLENGES: Dict[str, Dict[str, Any]] = {}


def generate_meeting_challenge(
    meeting_id: str,
    ttl_minutes: int = 15,
) -> Dict[str, Any]:
    """
    Generates a fresh, random meeting-specific challenge phrase with an expiry window
    and resets completion state for CFO, VP_SECURITY, and LEGAL.
    """
    normalized_meeting = meeting_id.strip()
    if not normalized_meeting:
        raise ValueError("meeting_id cannot be empty.")

    word = secrets.choice(CODE_WORDS)
    number = secrets.randbelow(9000) + 1000  # 4-digit random code (1000–9999)
    phrase = f"{word} - {number}"

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=ttl_minutes)

    participants: Dict[str, Dict[str, Any]] = {}
    for role in VALID_ROLES:
        sec_code = f"SEC-{secrets.randbelow(900000) + 100000}"
        participants[role] = {
            "completed": False,
            "submitted_phrase": None,
            "completed_at": None,
            "secondary_code": sec_code,
            "secondary_confirmed": False,
            "secondary_confirmed_at": None,
        }

    record = {
        "meeting_id": normalized_meeting,
        "phrase": phrase,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "participants": participants,
    }
    _MEETING_CHALLENGES[normalized_meeting] = record
    return _format_public_challenge(record)


def get_meeting_challenge(meeting_id: str) -> Optional[Dict[str, Any]]:
    record = _MEETING_CHALLENGES.get(meeting_id.strip())
    if not record:
        return None
    return _format_public_challenge(record)


def verify_challenge_response(
    meeting_id: str,
    role: str,
    submitted_phrase: str,
) -> Dict[str, Any]:
    """
    Validates that the meeting challenge exists, has not expired, and matches the submitted phrase.
    """
    normalized_meeting = meeting_id.strip()
    normalized_role = role.strip().upper()

    if normalized_role not in VALID_ROLES:
        return {"valid": False, "reason": f"Invalid role '{role}'."}

    record = _MEETING_CHALLENGES.get(normalized_meeting)
    if not record:
        return {"valid": False, "reason": f"No active challenge found for meeting '{normalized_meeting}'."}

    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(record["expires_at"])
    if now > expires_at:
        return {"valid": False, "reason": "Meeting challenge has expired. Generate a new challenge."}

    expected = _normalize_phrase(record["phrase"])
    actual = _normalize_phrase(submitted_phrase)
    if not actual or actual != expected:
        return {
            "valid": False,
            "reason": "Submitted challenge phrase does not match the active meeting challenge.",
        }

    p_state = record["participants"][normalized_role]
    p_state["completed"] = True
    p_state["submitted_phrase"] = submitted_phrase.strip()
    p_state["completed_at"] = now.isoformat()

    return {
        "valid": True,
        "meeting_id": normalized_meeting,
        "role": normalized_role,
        "completed_at": p_state["completed_at"],
        "reason": "Meeting challenge verified.",
    }


def issue_or_verify_secondary_confirmation(
    meeting_id: str,
    role: str,
    submitted_code: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Prototype secondary out-of-band confirmation helper.
    If submitted_code is None, returns the generated confirmation code to display/dispatch.
    If submitted_code is provided, validates it and marks secondary_confirmed=True.
    """
    normalized_meeting = meeting_id.strip()
    normalized_role = role.strip().upper()

    if normalized_role not in VALID_ROLES:
        return {"confirmed": False, "reason": f"Invalid role '{role}'."}

    record = _MEETING_CHALLENGES.get(normalized_meeting)
    if not record:
        return {"confirmed": False, "reason": f"No active meeting found for '{normalized_meeting}'."}

    p_state = record["participants"][normalized_role]
    if submitted_code is None:
        return {
            "confirmed": p_state["secondary_confirmed"],
            "role": normalized_role,
            "secondary_code": p_state["secondary_code"],
            "note": "Prototype simulated out-of-band confirmation code.",
        }

    if submitted_code.strip().upper() != p_state["secondary_code"]:
        return {
            "confirmed": False,
            "role": normalized_role,
            "reason": "Invalid secondary confirmation code.",
        }

    p_state["secondary_confirmed"] = True
    p_state["secondary_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    return {
        "confirmed": True,
        "role": normalized_role,
        "confirmed_at": p_state["secondary_confirmed_at"],
        "reason": "Secondary confirmation verified.",
    }


def _normalize_phrase(text: str) -> str:
    return "".join(ch for ch in (text or "").upper() if ch.isalnum())


def _format_public_challenge(record: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(record["expires_at"])
    return {
        "meeting_id": record["meeting_id"],
        "phrase": record["phrase"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "is_expired": now > expires_at,
        "participants": {
            role: {
                "completed": info["completed"],
                "completed_at": info["completed_at"],
                "secondary_confirmed": info["secondary_confirmed"],
                "secondary_code": info["secondary_code"],
            }
            for role, info in record["participants"].items()
        },
    }