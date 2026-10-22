"""Runs the Streamlit dashboard headless in demo mode and drives its main actions."""

import sys

import pytest
from streamlit.testing.v1 import AppTest

from mail_sorter import actions, db, demo, rules
from mail_sorter.db import ActionLog, Email, get_session
from mail_sorter.rules import load_rules, parse_rules, save_rules

DEMO_COUNT = 200


@pytest.fixture
def app(tmp_path, monkeypatch):
    demo_db = str(tmp_path / "demo.db")
    monkeypatch.setattr(demo, "DEMO_DB_PATH", demo_db)
    monkeypatch.setattr(demo, "DEMO_BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(demo, "DEMO_RULES_PATH", tmp_path / "rules.toml")
    # the app redirects these to the demo paths, undo it after the test
    monkeypatch.setattr(actions, "BACKUP_DIR", actions.BACKUP_DIR)
    monkeypatch.setattr(rules, "RULES_PATH", rules.RULES_PATH)
    monkeypatch.setattr(sys, "argv", ["app.py", "--demo"])

    previous_db = db.DB_PATH
    db.configure(demo_db)
    db.init_db()
    with get_session() as session:
        session.add_all(Email(**data) for data in demo.generate_emails(DEMO_COUNT))

    at = AppTest.from_file("../ui/app.py", default_timeout=30)
    yield at.run()
    db.get_engine().dispose()
    db.configure(previous_db)


def open_page(at: AppTest, page: str, **state) -> AppTest:
    at.session_state["page"] = page
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def click(at: AppTest, label: str) -> AppTest:
    return next(b for b in at.button if b.label == label).click().run()


def count(status: str, sender: str | None = None) -> int:
    with get_session() as session:
        query = session.query(Email).filter_by(status=status)
        if sender:
            query = query.filter_by(sender_email=sender)
        return query.count()


def demo_count(**filters) -> int:
    return sum(all(d[k] == v for k, v in filters.items()) for d in demo.generate_emails(DEMO_COUNT))


def test_overview_renders_without_errors(app):
    assert not app.exception
    assert app.metric[0].value == str(DEMO_COUNT)


@pytest.mark.parametrize("page", ["Senders", "Categories", "Rules", "History"])
def test_every_page_renders(app, page):
    open_page(app, page)
    assert not app.exception


def test_move_sender_to_folder_and_save_rule(app):
    open_page(app, "Senders", sender_pick="riotgames.com")
    key = "sender_domain_riotgames.com"
    # the folder is suggested from the sender name
    assert app.text_input(key=f"{key}_folder").value == "Riot Games"

    app.text_input(key=f"{key}_folder").set_value("Jeux/Riot").run()
    app.button(key=f"{key}_move").click().run()
    click(app, "Confirm")

    assert not app.exception
    expected = demo_count(sender_email="noreply@riotgames.com")
    assert count("moved") == expected
    assert app.success[0].value.startswith(f"{expected} emails moved to Jeux/Riot.")
    (rule,) = load_rules()
    assert rule.senders == ["riotgames.com"]
    assert rule.folder == "Jeux/Riot"


def test_delete_old_emails_of_a_sender_only(app):
    open_page(app, "Senders", sender_pick="zalando.fr", sender_age="Older than 1 year")
    key = "sender_domain_zalando.fr"
    app.checkbox(key=f"{key}_save_rule").uncheck().run()
    app.button(key=f"{key}_delete").click().run()
    click(app, "Confirm")

    deleted = count("deleted")
    assert 0 < deleted < demo_count(sender_email="info@zalando.fr")
    assert not rules.RULES_PATH.exists()


def test_delete_category_then_restore_from_history(app):
    open_page(app, "Categories", category_pick="SPAM")
    app.checkbox(key="cat_SPAM_all").check().run()
    app.button(key="cat_SPAM_delete").click().run()
    click(app, "Confirm")

    spam = demo_count(category="SPAM")
    assert count("deleted") == spam
    with get_session() as session:
        assert session.query(ActionLog).one().action == "delete"

    open_page(app, "History")
    click(app, "Restore to inbox")
    click(app, "Confirm")
    assert count("deleted") == 0
    assert app.success[0].value == f"{spam} emails restored to the inbox."


def test_apply_rules_from_rules_page(app):
    save_rules(
        parse_rules(
            {
                "rules": [
                    {"folder": "Jeux/Steam", "senders": ["steampowered.com"]},
                    {"action": "delete", "senders": ["lucky-prize.xyz"]},
                ]
            }
        )
    )
    open_page(app, "Rules")
    total = demo_count(sender_email="noreply@steampowered.com") + demo_count(
        sender_email="winner@lucky-prize.xyz"
    )
    click(app, f"Apply all rules ({total} emails)")
    click(app, "Confirm")

    assert not app.exception
    assert count("moved", "noreply@steampowered.com") == demo_count(
        sender_email="noreply@steampowered.com"
    )
    assert count("deleted", "winner@lucky-prize.xyz") == demo_count(
        sender_email="winner@lucky-prize.xyz"
    )


def test_cancel_closes_dialog_without_action(app):
    open_page(app, "Categories", category_pick="PROMO")
    app.checkbox(key="cat_PROMO_all").check().run()
    app.button(key="cat_PROMO_delete").click().run()
    click(app, "Cancel")

    assert "pending" not in app.session_state
    assert count("deleted") == 0
