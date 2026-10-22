"""Runs the Streamlit dashboard headless in demo mode and drives its main actions."""

import sys

import pytest
from streamlit.testing.v1 import AppTest

from mail_sorter import actions, db, demo
from mail_sorter.db import ActionLog, Email, get_session


@pytest.fixture
def app(tmp_path, monkeypatch):
    demo_db = str(tmp_path / "demo.db")
    monkeypatch.setattr(demo, "DEMO_DB_PATH", demo_db)
    monkeypatch.setattr(actions, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(sys, "argv", ["app.py", "--demo"])

    db.configure(demo_db)
    db.init_db()
    with get_session() as session:
        session.add_all(Email(**data) for data in demo.generate_emails(200))

    at = AppTest.from_file("../ui/app.py", default_timeout=30)
    yield at.run()
    db.get_engine().dispose()
    db.configure(db.DB_PATH)


def test_dashboard_renders_without_errors(app):
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Overview", "By category", "By sender", "History"]
    assert app.metric[0].value == "200"


def test_delete_selected_category_then_restore(app):
    app.selectbox(key="cat_select").set_value("SPAM").run()
    app.checkbox(key="cat_SPAM_all").check().run()
    spam_count = sum(1 for d in demo.generate_emails(200) if d["category"] == "SPAM")

    app.button(key="cat_SPAM_delete").click().run()
    confirm = next(b for b in app.button if b.label == "Confirm")
    confirm.click().run()
    assert not app.exception
    assert app.success[0].value == f"{spam_count} emails moved to the trash."

    with get_session() as session:
        log = session.query(ActionLog).one()
        assert log.action == "delete"
        assert session.query(Email).filter_by(status="deleted").count() == spam_count

    restore = next(b for b in app.button if b.label == "Restore to inbox")
    restore.click().run()
    next(b for b in app.button if b.label == "Confirm").click().run()
    assert app.success[0].value == f"{spam_count} emails restored to the inbox."


def test_move_sender_to_folder(app):
    app.radio(key="sender_by").set_value("domain").run()
    app.selectbox(key="sender_select").set_value("riotgames.com").run()
    assert app.text_input(key="sender_riotgames.com_folder").value == "Riotgames"

    app.text_input(key="sender_riotgames.com_folder").set_value("Jeux/Riot").run()
    app.checkbox(key="sender_riotgames.com_all").check().run()
    app.button(key="sender_riotgames.com_move").click().run()
    next(b for b in app.button if b.label == "Confirm").click().run()

    assert not app.exception
    with get_session() as session:
        moved = session.query(Email).filter_by(status="moved").all()
        assert moved
        assert {e.action_taken for e in moved} == {"moved:Jeux/Riot"}
        assert {e.sender_email for e in moved} == {"noreply@riotgames.com"}


def test_cancel_closes_dialog_without_action(app):
    app.selectbox(key="cat_select").set_value("PROMO").run()
    app.checkbox(key="cat_PROMO_all").check().run()
    app.button(key="cat_PROMO_delete").click().run()
    next(b for b in app.button if b.label == "Cancel").click().run()

    assert "pending" not in app.session_state
    with get_session() as session:
        assert session.query(Email).filter_by(status="deleted").count() == 0
