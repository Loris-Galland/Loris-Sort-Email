"""Tests for sender rules and the safe bulk actions (move, delete, restore)."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from mail_sorter import actions
from mail_sorter.actions import delete_emails, move_emails, restore
from mail_sorter.db import ActionLog, Email
from mail_sorter.rules import (
    RuleError,
    build_template,
    first_rule_for,
    load_rules,
    match_rules,
    parse_rules,
    save_rules,
)


def email(msg_id: str, sender: str, subject: str = "", days_old: int = 0) -> Email:
    return Email(
        message_id=msg_id,
        sender_email=sender,
        subject=subject,
        received_at=datetime(2026, 1, 1) - timedelta(days=days_old),
    )


class TestRules:
    def test_domain_matches_subdomains_but_not_lookalikes(self):
        (rule,) = parse_rules({"rules": [{"folder": "Riot", "senders": ["riotgames.com"]}]})
        assert rule.matches_sender("news@riotgames.com")
        assert rule.matches_sender("no-reply@mail.RiotGames.com")
        assert not rule.matches_sender("x@notriotgames.com")
        assert not rule.matches_sender("")

    def test_full_address_matches_exactly(self):
        (rule,) = parse_rules({"rules": [{"action": "delete", "senders": ["promo@shop.com"]}]})
        assert rule.delete
        assert rule.matches_sender("promo@shop.com")
        assert not rule.matches_sender("orders@shop.com")

    def test_subject_and_age_filters(self):
        (rule,) = parse_rules(
            {
                "rules": [
                    {
                        "action": "delete",
                        "senders": ["shop.com"],
                        "subject_contains": ["Promo"],
                        "older_than_days": 30,
                    }
                ]
            }
        )
        now = datetime(2026, 1, 1)
        assert rule.matches(email("a", "x@shop.com", "Big PROMO", days_old=40), now)
        assert not rule.matches(email("b", "x@shop.com", "Big PROMO", days_old=5), now)
        assert not rule.matches(email("c", "x@shop.com", "Your order", days_old=40), now)

    def test_first_matching_rule_wins(self):
        rules = parse_rules(
            {
                "rules": [
                    {"folder": "Riot/Esport", "senders": ["esports@riotgames.com"]},
                    {"folder": "Riot", "senders": ["riotgames.com"]},
                ]
            }
        )
        emails = [email("a", "esports@riotgames.com"), email("b", "news@riotgames.com")]
        (r1, m1), (r2, m2) = match_rules(emails, rules)
        assert [e.message_id for e in m1] == ["a"]
        assert [e.message_id for e in m2] == ["b"]

    @pytest.mark.parametrize(
        "raw",
        [
            {"folder": "X", "senders": []},
            {"senders": ["a.com"]},
            {"folder": "X", "action": "delete", "senders": ["a.com"]},
            {"action": "archive", "senders": ["a.com"]},
            {"folder": "X", "senders": ["a.com"], "older_than_days": "30"},
        ],
    )
    def test_invalid_rules_are_rejected(self, raw):
        with pytest.raises(RuleError):
            parse_rules({"rules": [raw]})

    def test_template_is_valid_toml_with_suggestions(self):
        import tomllib

        text = build_template([("riotgames.com", 120)])
        assert "riotgames.com" in text
        assert len(parse_rules(tomllib.loads(text))) == 1


class FakeGraph:
    """Records moves; message "fail" can never be moved."""

    def __init__(self):
        self.moves = []

    def ensure_folder(self, path):
        return f"folder:{path}"

    def move_messages(self, ids, destination, on_progress=None):
        self.moves.append((list(ids), destination))
        return {i: (None if i == "fail" else f"{i}@{destination}") for i in ids}


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "BACKUP_DIR", tmp_path)
    return tmp_path


def test_move_updates_status_logs_and_backs_up(db_session, backup_dir):
    emails = [email("a", "x@riotgames.com"), email("fail", "y@riotgames.com")]
    db_session.add_all(emails)
    db_session.flush()

    result = move_emails(FakeGraph(), db_session, emails, "Jeux/Riot")

    assert (result.succeeded, result.failed) == (1, 1)
    assert emails[0].status == "moved"
    assert emails[0].action_taken == "moved:Jeux/Riot"
    assert emails[0].message_id == "a@folder:Jeux/Riot"
    assert emails[1].status == "pending"

    log = db_session.get(ActionLog, result.log_id)
    assert log.action == "move"
    assert log.get_message_ids() == ["a@folder:Jeux/Riot"]

    backup = json.loads(Path(result.backup_file).read_text(encoding="utf-8"))
    assert {e["message_id"] for e in backup["emails"]} == {"a", "fail"}


def test_delete_goes_to_trash_and_can_be_restored_once(db_session, backup_dir):
    target = email("a", "promo@shop.com")
    target.category = "PROMO"
    db_session.add(target)
    db_session.flush()

    graph = FakeGraph()
    result = delete_emails(graph, db_session, [target], "PROMO")
    assert graph.moves[-1][1] == "deleteditems"
    assert target.status == "deleted"

    restored = restore(graph, db_session, result.log_id)
    assert restored.succeeded == 1
    assert graph.moves[-1][1] == "inbox"
    assert target.status == "classified"
    assert target.action_taken is None

    with pytest.raises(ValueError, match="already restored"):
        restore(graph, db_session, result.log_id)


def test_restore_counts_emails_missing_locally_as_failed(db_session, backup_dir):
    kept, lost = email("a", "x@shop.com"), email("b", "y@shop.com")
    db_session.add_all([kept, lost])
    db_session.flush()
    graph = FakeGraph()
    result = delete_emails(graph, db_session, [kept, lost])
    db_session.delete(lost)
    db_session.flush()

    restored = restore(graph, db_session, result.log_id)
    assert (restored.succeeded, restored.failed) == (1, 1)
    assert kept.status == "pending"


def test_save_rules_round_trip(tmp_path):
    rules = parse_rules(
        {
            "rules": [
                {"folder": "Jeux/Riot", "senders": ["riotgames.com"]},
                {
                    "action": "delete",
                    "senders": ["promo@shop.com"],
                    "older_than_days": 30,
                    "subject_contains": ['soldes "été"'],
                },
            ]
        }
    )
    path = tmp_path / "rules.toml"
    save_rules(rules, path)
    assert load_rules(path) == rules
    assert first_rule_for("news@mail.riotgames.com", rules).folder == "Jeux/Riot"
    assert first_rule_for("someone@else.com", rules) is None


def test_empty_selection_does_nothing(db_session, backup_dir):
    graph = FakeGraph()
    result = delete_emails(graph, db_session, [])
    assert result.succeeded == 0
    assert graph.moves == []
    assert list(backup_dir.iterdir()) == []
