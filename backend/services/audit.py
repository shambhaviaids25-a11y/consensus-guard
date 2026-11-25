import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
AUDIT_LOG_PATH = os.path.join(DATA_DIR, "audit_log.json")
GENESIS_HASH = "0" * 64


def _ensure_store() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(AUDIT_LOG_PATH):
        with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
            json.dump([], f)


def _read_all_events() -> List[Dict[str, Any]]:
    _ensure_store()
    try:
        with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_all_events(events: List[Dict[str, Any]]) -> None:
    _ensure_store()
    with open(AUDIT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)


def _compute_event_hash(payload_without_hash: Dict[str, Any]) -> str:
    canonical = json.dumps(payload_without_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def record_audit_event(
    meeting_id: str,
    event_type: str,
    role: Optional[str] = None,
    eidos_request_id: Optional[str] = None,
    identity_result: Optional[str] = None,
    audio_result: Optional[str] = None,
    video_result: Optional[str] = None,
    approval_status: Optional[str] = None,
    reason: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Appends a timestamped forensic audit event linked via SHA-256 to the previous event.
    """
    events = _read_all_events()
    prev_hash = events[-1]["event_hash"] if events else GENESIS_HASH
    sequence_id = len(events) + 1

    event_body: Dict[str, Any] = {
        "sequence_id": sequence_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "meeting_id": meeting_id.strip(),
        "event_type": event_type,
        "role": role.strip().upper() if role else None,
        "eidos_request_id": eidos_request_id,
        "identity_result": identity_result,
        "audio_result": audio_result,
        "video_result": video_result,
        "approval_status": approval_status,
        "reason": reason,
        "metadata": metadata or {},
        "prev_hash": prev_hash,
    }

    event_hash = _compute_event_hash(event_body)
    full_event = {**event_body, "event_hash": event_hash}

    events.append(full_event)
    _write_all_events(events)
    return full_event


def get_meeting_audit_history(meeting_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Retrieves audit events (optionally filtered by meeting_id) and verifies
    the integrity of the entire SHA-256 hash chain.
    """
    events = _read_all_events()
    chain_valid = True
    broken_at_sequence: Optional[int] = None
    expected_prev = GENESIS_HASH

    for ev in events:
        if ev.get("prev_hash") != expected_prev:
            chain_valid = False
            broken_at_sequence = ev.get("sequence_id")
            break
        copy_without_hash = {k: v for k, v in ev.items() if k != "event_hash"}
        recomputed = _compute_event_hash(copy_without_hash)
        if ev.get("event_hash") != recomputed:
            chain_valid = False
            broken_at_sequence = ev.get("sequence_id")
            break
        expected_prev = ev["event_hash"]

    if meeting_id:
        target = meeting_id.strip()
        filtered = [ev for ev in events if ev.get("meeting_id") == target]
    else:
        filtered = events

    return {
        "meeting_id": meeting_id,
        "chain_integrity_valid": chain_valid,
        "broken_at_sequence": broken_at_sequence,
        "total_events": len(filtered),
        "events": filtered,
    }