"""Assign an event identity once, before handing the event to subscribers."""
from datetime import datetime, timezone
from uuid import UUID, uuid4


def ensure_event_identity(event: dict) -> str:
    """Preserve an existing canonical UUID; never replace an invalid identity."""
    if "eventId" not in event:
        event["eventId"] = str(uuid4())
    identity = event["eventId"]
    if not isinstance(identity, str) or str(UUID(identity)) != identity:
        raise ValueError("Invalid event identity")
    event.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    return identity
