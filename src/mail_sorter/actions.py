"""Safe bulk actions on emails: move to a folder, delete (to trash) and restore.

Deleting always means moving to "Deleted Items", never a permanent delete.
A JSON backup of the affected metadata is written before any bulk operation.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from mail_sorter.db import ActionLog, Email
from mail_sorter.graph import GraphClient

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "./backups"))


@dataclass
class ActionResult:
    succeeded: int
    failed: int
    log_id: int | None = None
    backup_file: str | None = None


def write_backup(emails: list[Email], action: str, target: str | None) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = BACKUP_DIR / f"{stamp}_{action}.json"
    payload = {
        "action": action,
        "target": target,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "emails": [email.to_dict() for email in emails],
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _run(
    client: GraphClient,
    session: Session,
    emails: list[Email],
    action: str,
    destination: str,
    label: str | None,
    on_success: Callable[[Email], None],
    on_progress: Callable[[int], None] | None,
) -> ActionResult:
    if not emails:
        return ActionResult(0, 0)

    backup = write_backup(emails, action, label)
    results = client.move_messages([e.message_id for e in emails], destination, on_progress)

    new_ids = []
    for email in emails:
        new_id = results.get(email.message_id)
        if new_id is None:
            continue
        # the ID can change when a message changes folder
        email.message_id = new_id
        on_success(email)
        new_ids.append(new_id)

    log = ActionLog(action=action, category=label, count=len(new_ids), backup_file=str(backup))
    log.set_message_ids(new_ids)
    session.add(log)
    session.flush()
    return ActionResult(len(new_ids), len(emails) - len(new_ids), log.id, str(backup))


def move_emails(
    client: GraphClient,
    session: Session,
    emails: list[Email],
    folder: str,
    on_progress: Callable[[int], None] | None = None,
) -> ActionResult:
    """Move emails into a folder path like "Jeux/Riot", created if missing."""
    folder_id = client.ensure_folder(folder)

    def mark(email: Email) -> None:
        email.status = "moved"
        email.action_taken = f"moved:{folder}"

    return _run(client, session, emails, "move", folder_id, folder, mark, on_progress)


def delete_emails(
    client: GraphClient,
    session: Session,
    emails: list[Email],
    label: str | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ActionResult:
    """Soft delete: move emails to Deleted Items, where Outlook keeps them for a while."""

    def mark(email: Email) -> None:
        email.status = "deleted"
        email.action_taken = "deleted"

    return _run(client, session, emails, "delete", "deleteditems", label, mark, on_progress)


def restore(
    client: GraphClient,
    session: Session,
    log_id: int,
    on_progress: Callable[[int], None] | None = None,
) -> ActionResult:
    """Move every email from a previous move/delete action back to the inbox."""
    log = session.get(ActionLog, log_id)
    if log is None or log.action not in ("move", "delete"):
        raise ValueError(f"No move or delete action with id {log_id}")
    marker = f"#{log_id}"
    if session.query(ActionLog).filter_by(action="restore", category=marker).first():
        raise ValueError(f"Action #{log_id} was already restored")

    ids = log.get_message_ids()
    emails = session.query(Email).filter(Email.message_id.in_(ids)).all()

    def mark(email: Email) -> None:
        email.status = "classified" if email.category else "pending"
        email.action_taken = None

    return _run(client, session, emails, "restore", "inbox", marker, mark, on_progress)
