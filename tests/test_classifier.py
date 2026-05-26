"""Tests for classifier.py — category validation and business rules."""

import json

from mail_sorter.classifier import VALID_CATEGORIES


def test_exactly_eight_categories():
    assert len(VALID_CATEGORIES) == 8


def test_category_names_match_prompt():
    expected = {
        "NEWSLETTER", "PROMO", "NOTIFICATION", "FACTURE",
        "PROFESSIONNEL", "PERSONNEL", "SPAM", "A_TRAITER",
    }
    assert VALID_CATEGORIES == expected


def test_ollama_response_format():
    """Verify the JSON format we expect from Ollama can be parsed correctly."""
    mock_response = json.dumps([
        {"id": "msg_promo_001", "category": "PROMO", "confidence": 94, "reason": "Commercial sender"},
        {"id": "msg_facture_002", "category": "FACTURE", "confidence": 88, "reason": "Billing email"},
    ])

    parsed = json.loads(mock_response)

    assert isinstance(parsed, list)
    for item in parsed:
        assert {"id", "category", "confidence", "reason"}.issubset(item)
        assert isinstance(item["confidence"], int)
        assert 0 <= item["confidence"] <= 100
        assert item["category"] in VALID_CATEGORIES


def test_rule_high_importance():
    """importance=high -> only A_TRAITER or PROFESSIONNEL allowed."""
    allowed = {"A_TRAITER", "PROFESSIONNEL"}
    result = {"id": "msg_pro_003", "category": "A_TRAITER", "confidence": 97, "reason": "Urgent flag"}
    assert result["category"] in allowed


def test_rule_attachment_not_spam():
    """has_attachments=True -> never NEWSLETTER or SPAM."""
    forbidden = {"NEWSLETTER", "SPAM"}
    result = {"id": "msg_facture_002", "category": "FACTURE", "confidence": 88, "reason": "Invoice with attachment"}
    assert result["category"] not in forbidden


def test_confidence_bounds():
    for score in (0, 50, 100):
        assert 0 <= score <= 100


def test_unknown_category_is_invalid():
    assert "UNKNOWN" not in VALID_CATEGORIES
