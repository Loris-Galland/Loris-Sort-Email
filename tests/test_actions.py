"""Tests for database models and constraints."""

import pytest
from sqlalchemy.exc import IntegrityError

from mail_sorter.db import ActionLog, Email


class TestEmailModel:
    def test_bulk_insert(self, db_session, sample_emails):
        for data in sample_emails:
            db_session.add(Email(**data))
        db_session.flush()
        assert db_session.query(Email).count() == len(sample_emails)

    def test_default_status_is_pending(self, db_session):
        db_session.add(Email(message_id="x", sender_email="a@b.com"))
        db_session.flush()
        saved = db_session.query(Email).filter_by(message_id="x").first()
        assert saved.status == "pending"

    def test_category_null_before_classification(self, db_session):
        db_session.add(Email(message_id="y", sender_email="a@b.com"))
        db_session.flush()
        saved = db_session.query(Email).filter_by(message_id="y").first()
        assert saved.category is None
        assert saved.confidence is None

    def test_message_id_unique_constraint(self, db_session):
        db_session.add(Email(message_id="dup"))
        db_session.flush()
        db_session.add(Email(message_id="dup"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_high_importance_stored(self, db_session):
        db_session.add(Email(message_id="hi", sender_email="boss@co.com", importance="high"))
        db_session.flush()
        saved = db_session.query(Email).filter_by(message_id="hi").first()
        assert saved.importance == "high"
        assert saved.category is None  # not classified yet

    def test_to_dict_completeness(self, db_session):
        db_session.add(Email(message_id="td", subject="Test", sender_email="a@b.com"))
        db_session.flush()
        email = db_session.query(Email).filter_by(message_id="td").first()
        d = email.to_dict()
        required = {
            "id", "message_id", "subject", "sender_email", "sender_name",
            "received_at", "is_read", "has_attachments", "importance", "size_bytes",
            "category", "confidence", "reason", "status", "action_taken", "indexed_at",
        }
        assert required.issubset(d.keys())
        assert d["status"] == "pending"


class TestActionLog:
    def test_message_ids_roundtrip(self):
        log = ActionLog(action="delete", category="NEWSLETTER", count=3)
        ids = ["id_001", "id_002", "id_003"]
        log.set_message_ids(ids)
        assert log.get_message_ids() == ids

    def test_empty_message_ids(self):
        log = ActionLog(action="delete")
        assert log.get_message_ids() == []

    def test_insert(self, db_session):
        log = ActionLog(action="delete", category="PROMO", count=150, backup_file="backups/b.json")
        log.set_message_ids(["msg001", "msg002"])
        db_session.add(log)
        db_session.flush()
        saved = db_session.query(ActionLog).filter_by(action="delete").first()
        assert saved.count == 150
        assert "msg001" in saved.get_message_ids()


class TestDatabaseQueries:
    def test_query_by_sender(self, db_session, sample_emails):
        for data in sample_emails:
            db_session.add(Email(**data))
        db_session.flush()
        results = db_session.query(Email).filter_by(sender_email="noreply@example-social.com").all()
        assert len(results) == 1
        assert results[0].message_id == "msg_notif_005"

    def test_filter_unclassified(self, db_session, sample_emails):
        for data in sample_emails:
            db_session.add(Email(**data))
        db_session.flush()
        first = db_session.query(Email).first()
        first.category = "NEWSLETTER"
        first.status = "classified"
        db_session.flush()
        unclassified = db_session.query(Email).filter(Email.category.is_(None)).all()
        assert len(unclassified) == len(sample_emails) - 1
