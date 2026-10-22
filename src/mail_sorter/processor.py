"""Inbox indexing: syncs Outlook metadata into the local SQLite database."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from mail_sorter.db import IN_INBOX, Email, Status
from mail_sorter.graph import GraphClient


@dataclass
class IndexStats:
    added: int = 0
    updated: int = 0
    gone: int = 0


def parse_message(raw: dict) -> dict:
    """Map a Graph message (metadata fields only) to Email column values."""
    sender = (raw.get("from") or {}).get("emailAddress") or {}

    received_at = None
    if raw.get("receivedDateTime"):
        # Stored as naive UTC, SQLite has no timezone support
        received_at = (
            datetime.fromisoformat(raw["receivedDateTime"]).astimezone(UTC).replace(tzinfo=None)
        )

    size = None
    for prop in raw.get("singleValueExtendedProperties") or []:
        if str(prop.get("value", "")).isdigit():
            size = int(prop["value"])

    return {
        "message_id": raw["id"],
        "subject": raw.get("subject") or "",
        "sender_email": (sender.get("address") or "").lower(),
        "sender_name": sender.get("name") or "",
        "received_at": received_at,
        "is_read": bool(raw.get("isRead")),
        "has_attachments": bool(raw.get("hasAttachments")),
        "importance": raw.get("importance") or "normal",
        "size_bytes": size,
    }


def clear_inbox_index(session: Session) -> None:
    """Forget inbox emails and their classification, but keep the emails this tool
    moved or deleted, otherwise their actions could no longer be restored.
    """
    kept = (Status.MOVED, Status.DELETED)
    session.query(Email).filter(Email.status.not_in(kept)).delete()


def index_inbox(
    client: GraphClient,
    session: Session,
    on_progress: Callable[[], None] | None = None,
) -> IndexStats:
    """Full sync of the inbox. The inbox is the source of truth:
    emails no longer there are marked "gone", emails moved back are reactivated.
    """
    stats = IndexStats()
    existing = {email.message_id: email for email in session.query(Email)}
    seen: set[str] = set()

    for raw in client.iter_inbox():
        data = parse_message(raw)
        seen.add(data["message_id"])
        email = existing.get(data["message_id"])

        if email is None:
            email = Email(**data)
            session.add(email)
            existing[email.message_id] = email
            stats.added += 1
        else:
            for field in ("subject", "is_read", "importance", "has_attachments", "size_bytes"):
                setattr(email, field, data[field])
            if email.status not in IN_INBOX:
                email.back_to_inbox()
            stats.updated += 1

        if on_progress:
            on_progress()

    for message_id, email in existing.items():
        if message_id not in seen and email.status in IN_INBOX:
            email.status = Status.GONE
            stats.gone += 1

    session.flush()
    return stats
