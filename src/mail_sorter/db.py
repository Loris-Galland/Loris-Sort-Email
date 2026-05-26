"""SQLAlchemy models and database initialization.

Stores email metadata only — never body content or attachments.
"""

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

from dotenv import load_dotenv
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "./mail_sorter.db")

_engine = None
_SessionLocal = None


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


class Email(Base):
    """Metadata for a single Outlook email.

    Only stores what's needed for classification: sender, subject, date, size, flags.
    The category column is populated by classifier.py (Phase 3).
    """

    __tablename__ = "emails"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String, unique=True, nullable=False, index=True)  # Graph API message ID

    subject = Column(String)
    sender_email = Column(String, index=True)
    sender_name = Column(String)
    received_at = Column(DateTime)
    is_read = Column(Boolean, default=False)
    has_attachments = Column(Boolean, default=False)
    importance = Column(String, default="normal")  # low | normal | high
    size_bytes = Column(Integer)

    # Set by classifier.py — null until Phase 3 runs
    # see classifier.py for the full list of valid values
    category = Column(String)
    confidence = Column(Integer) # 0-100
    reason = Column(String)

    # Workflow state: pending -> classified -> deleted | archived
    status = Column(String, default="pending")
    action_taken = Column(String)

    indexed_at = Column(DateTime, default=lambda: datetime.now(UTC))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "message_id": self.message_id,
            "subject": self.subject,
            "sender_email": self.sender_email,
            "sender_name": self.sender_name,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "is_read": self.is_read,
            "has_attachments": self.has_attachments,
            "importance": self.importance,
            "size_bytes": self.size_bytes,
            "category": self.category,
            "confidence": self.confidence,
            "reason": self.reason,
            "status": self.status,
            "action_taken": self.action_taken,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
        }


class ActionLog(Base):
    """Record of every delete/archive/restore action.

    Each entry links to a JSON backup file containing the affected message IDs,
    allowing the user to restore emails from the trash if needed.
    """

    __tablename__ = "action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(UTC))
    action = Column(String, nullable=False)  # delete | archive | restore
    category = Column(String)
    count = Column(Integer)
    backup_file = Column(String)       # relative path to the JSON backup
    message_ids_json = Column(String)  # JSON-serialized list of Graph message IDs

    def get_message_ids(self) -> list[str]:
        return json.loads(self.message_ids_json) if self.message_ids_json else []

    def set_message_ids(self, ids: list[str]) -> None:
        self.message_ids_json = json.dumps(ids)


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            f"sqlite:///{DB_PATH}",
            echo=False,
            connect_args={"check_same_thread": False},  # needed for Streamlit's threading model
        )
    return _engine


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    Base.metadata.create_all(get_engine())


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Context manager that provides a SQLAlchemy session with auto commit/rollback."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)

    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
