"""Tests for classifier.py: response parsing, business rules and the Ollama loop."""

import json

import httpx
import pytest

from mail_sorter.classifier import (
    VALID_CATEGORIES,
    OllamaError,
    apply_business_rules,
    build_prompt,
    check_local_url,
    check_ollama,
    classify_emails,
    parse_response,
)
from mail_sorter.db import Email


def test_exactly_eight_categories():
    assert len(VALID_CATEGORIES) == 8


def test_category_names_match_prompt():
    expected = {
        "NEWSLETTER",
        "PROMO",
        "NOTIFICATION",
        "FACTURE",
        "PROFESSIONNEL",
        "PERSONNEL",
        "SPAM",
        "A_TRAITER",
    }
    assert expected == VALID_CATEGORIES


def test_parse_response_valid_and_invalid_entries():
    text = json.dumps(
        {
            "results": [
                {"i": 0, "category": "promo", "confidence": 94, "reason": "Commercial"},
                {"i": 1, "category": "FACTURE", "confidence": 140, "reason": "Billing"},
                {"i": 2, "category": "UNKNOWN", "confidence": 80},
                {"i": 9, "category": "SPAM", "confidence": 80},
                {"i": "x", "category": "SPAM"},
            ]
        }
    )
    parsed = parse_response(text, count=3)
    assert parsed == {0: ("PROMO", 94, "Commercial"), 1: ("FACTURE", 100, "Billing")}


def test_parse_response_accepts_bare_list_and_rejects_garbage():
    assert parse_response(json.dumps([{"i": 0, "category": "SPAM"}]), 1) == {0: ("SPAM", 50, "")}
    assert parse_response("not json", 1) == {}
    assert parse_response('"text"', 1) == {}


def test_rule_high_importance():
    email = Email(importance="high", has_attachments=False)
    assert apply_business_rules(email, "PROMO", 90) == ("A_TRAITER", 50)
    assert apply_business_rules(email, "PROFESSIONNEL", 90) == ("PROFESSIONNEL", 90)


def test_rule_attachment_not_spam():
    email = Email(importance="normal", has_attachments=True)
    assert apply_business_rules(email, "SPAM", 30) == ("A_TRAITER", 30)
    assert apply_business_rules(email, "FACTURE", 88) == ("FACTURE", 88)


def test_prompt_contains_metadata_only(sample_emails):
    prompt = build_prompt([Email(**data) for data in sample_emails])
    assert "0 | from: Example Shop <promo@example.com> | subject: 50% off" in prompt
    assert "high importance" in prompt


@pytest.mark.parametrize("url", ["http://example.com:11434", "https://api.remote.ai"])
def test_remote_ollama_url_is_refused(url):
    with pytest.raises(OllamaError):
        check_local_url(url)


def test_check_ollama_reports_missing_model():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"models": [{"name": "phi3:latest"}]})
    )
    http = httpx.Client(transport=transport)
    check_ollama(http, "phi3")
    with pytest.raises(OllamaError, match="ollama pull mistral"):
        check_ollama(http, "mistral")


def test_classify_emails_dedupes_and_skips_failures(db_session):
    emails = [
        Email(message_id="a", sender_email="n@shop.com", subject="Promo"),
        Email(message_id="b", sender_email="n@shop.com", subject="Promo"),
        Email(message_id="c", sender_email="friend@mail.fr", subject="Hello"),
    ]
    db_session.add_all(emails)
    db_session.flush()
    prompts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts.append(body["messages"][1]["content"])
        # only the first email of the batch gets an answer
        content = json.dumps({"results": [{"i": 0, "category": "PROMO", "confidence": 90}]})
        return httpx.Response(200, json={"message": {"content": content}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    stats = classify_emails(db_session, http, emails, batch_size=10)

    assert len(prompts) == 1
    assert prompts[0].count("n@shop.com") == 1
    assert (stats.classified, stats.failed) == (2, 1)
    assert emails[1].category == "PROMO"
    assert emails[1].status == "classified"
    assert emails[2].status == "pending"
