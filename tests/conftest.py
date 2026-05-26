"""Shared pytest fixtures — in-memory SQLite database and sample email data."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from mail_sorter.db import Base, Email


@pytest.fixture
def in_memory_engine():
    """Fresh in-memory SQLite engine per test — never touches mail_sorter.db."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)


@pytest.fixture
def db_session(in_memory_engine):
    """SQLAlchemy session on the in-memory engine, rolled back after each test."""
    Session = sessionmaker(bind=in_memory_engine)
    session = Session()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def sample_emails() -> list[dict]:
    """Representative emails covering the key business rules:
    - importance=high must stay A_TRAITER or PROFESSIONNEL
    - has_attachments=True must never be NEWSLETTER or SPAM
    """
    return [
        {
            "message_id": "msg_promo_001",
            "subject": "50% off this weekend only",
            "sender_email": "promo@example.com",
            "sender_name": "Example Shop",
            "is_read": False,
            "has_attachments": False,
            "importance": "normal",
            "size_bytes": 12_000,
        },
        {
            "message_id": "msg_facture_002",
            "subject": "Your invoice for November 2024",
            "sender_email": "billing@example.com",
            "sender_name": "Example Telecom",
            "is_read": True,
            "has_attachments": True,  # must never be SPAM
            "importance": "normal",
            "size_bytes": 85_000,
        },
        {
            "message_id": "msg_pro_003",
            "subject": "URGENT: Client meeting tomorrow 9am",
            "sender_email": "alice@example.com",
            "sender_name": "Alice Martin",
            "is_read": False,
            "has_attachments": False,
            "importance": "high",  # must stay A_TRAITER or PROFESSIONNEL
            "size_bytes": 3_200,
        },
        {
            "message_id": "msg_newsletter_004",
            "subject": "This week in tech",
            "sender_email": "newsletter@example.com",
            "sender_name": "Example Newsletter",
            "is_read": True,
            "has_attachments": False,
            "importance": "normal",
            "size_bytes": 45_000,
        },
        {
            "message_id": "msg_notif_005",
            "subject": "You have a new message",
            "sender_email": "noreply@example-social.com",
            "sender_name": "Example Social",
            "is_read": False,
            "has_attachments": False,
            "importance": "normal",
            "size_bytes": 8_500,
        },
    ]
