"""Email classification via Ollama (local LLM, no API key, no data leaves the machine).

Calls the Ollama REST API on localhost with batches of email metadata
and writes category, confidence and reason back to the SQLite database.
"""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

from mail_sorter.db import Email, Status

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral")

# Valid categories, must match the prompt exactly
VALID_CATEGORIES = {
    "NEWSLETTER",
    "PROMO",
    "NOTIFICATION",
    "FACTURE",
    "PROFESSIONNEL",
    "PERSONNEL",
    "SPAM",
    "A_TRAITER",
}

SYSTEM_PROMPT = """You sort the emails of a personal Outlook inbox (French and English).
You only see metadata: sender, subject, date and flags. Pick exactly one category per email:

NEWSLETTER    regular editorial content the user subscribed to (news, blogs, digests)
PROMO         marketing, sales, discounts, product announcements
NOTIFICATION  automatic messages: account alerts, social networks, shipping, security codes
FACTURE       invoices, receipts, payment confirmations, bank or tax documents
PROFESSIONNEL work, school, job applications, administration
PERSONNEL     written by a real person the user knows
SPAM          unsolicited, scam, phishing
A_TRAITER     needs an action or answer from the user, or you are unsure

Reply with JSON only, no text around it:
{"results": [{"i": <email number>, "category": "<CATEGORY>", "confidence": <0-100>,
"reason": "<5 words max>"}]}"""


class OllamaError(Exception):
    pass


@dataclass
class ClassifyStats:
    classified: int = 0
    failed: int = 0


def check_local_url(url: str) -> None:
    # Hard guard: email metadata must never be sent to a remote model
    host = urlparse(url).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise OllamaError(f"OLLAMA_URL must point to this machine, got {host!r}")


def check_ollama(http: httpx.Client, model: str = OLLAMA_MODEL, url: str = OLLAMA_URL) -> None:
    check_local_url(url)
    try:
        resp = http.get(f"{url}/api/tags", timeout=5)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise OllamaError(
            f"Ollama is not reachable on {url}. Install it from https://ollama.com "
            "and make sure it is running."
        ) from exc
    names = {m.get("name", "").split(":")[0] for m in resp.json().get("models", [])}
    if model.split(":")[0] not in names:
        raise OllamaError(f"Model {model!r} is not installed. Run: ollama pull {model}")


def build_prompt(emails: list[Email]) -> str:
    lines = []
    for i, email in enumerate(emails):
        flags = []
        if not email.is_read:
            flags.append("unread")
        if email.has_attachments:
            flags.append("attachment")
        if email.importance == "high":
            flags.append("high importance")
        date = email.received_at.strftime("%Y-%m-%d") if email.received_at else "?"
        sender = (
            f"{email.sender_name} <{email.sender_email}>"
            if email.sender_name
            else email.sender_email
        )
        line = f"{i} | from: {sender} | subject: {email.subject or '(none)'} | {date}"
        if flags:
            line += f" | {', '.join(flags)}"
        lines.append(line)
    return "Classify these emails:\n" + "\n".join(lines)


def parse_response(text: str, count: int) -> dict[int, tuple[str, int, str]]:
    """Return {email number: (category, confidence, reason)}, ignoring invalid entries."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    items = data.get("results", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {}

    parsed = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            i = int(item.get("i", item.get("id")))
            confidence = max(0, min(100, int(item.get("confidence", 50))))
        except (TypeError, ValueError):
            continue
        category = str(item.get("category", "")).upper().strip()
        if 0 <= i < count and category in VALID_CATEGORIES:
            parsed[i] = (category, confidence, str(item.get("reason", ""))[:100])
    return parsed


def apply_business_rules(email: Email, category: str, confidence: int) -> tuple[str, int]:
    """Rules enforced on top of the LLM answer, to never bury something important."""
    if email.importance == "high" and category not in ("A_TRAITER", "PROFESSIONNEL"):
        return "A_TRAITER", min(confidence, 50)
    if email.has_attachments and category in ("NEWSLETTER", "SPAM"):
        return "A_TRAITER", min(confidence, 50)
    return category, confidence


def classify_batch(
    http: httpx.Client, emails: list[Email], model: str = OLLAMA_MODEL, url: str = OLLAMA_URL
) -> dict[int, tuple[str, int, str]]:
    check_local_url(url)
    resp = http.post(
        f"{url}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(emails)},
            ],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0},
        },
        # CPU-only machines can take minutes per batch
        timeout=600,
    )
    resp.raise_for_status()
    return parse_response(resp.json().get("message", {}).get("content", ""), len(emails))


def _group_key(email: Email) -> tuple[str, str]:
    return (email.sender_email or "", email.subject or "")


def classify_emails(
    session: Session,
    http: httpx.Client,
    emails: list[Email],
    batch_size: int = 30,
    model: str = OLLAMA_MODEL,
    on_progress: Callable[[int], None] | None = None,
) -> ClassifyStats:
    """Classify emails batch by batch, committing after each batch.

    Emails with the same sender and subject are classified once, which removes
    most repeated notifications and newsletters from the LLM workload.
    """
    groups: dict[tuple[str, str], list[Email]] = {}
    for email in emails:
        groups.setdefault(_group_key(email), []).append(email)
    representatives = [group[0] for group in groups.values()]

    stats = ClassifyStats()
    for start in range(0, len(representatives), batch_size):
        batch = representatives[start : start + batch_size]
        results = classify_batch(http, batch, model)
        for i, rep in enumerate(batch):
            group = groups[_group_key(rep)]
            if i not in results:
                stats.failed += len(group)
                continue
            category, confidence, reason = results[i]
            for email in group:
                email.category, email.confidence = apply_business_rules(email, category, confidence)
                email.reason = reason
                email.status = Status.CLASSIFIED
            stats.classified += len(group)
        session.commit()
        if on_progress:
            on_progress(sum(len(groups[_group_key(e)]) for e in batch))
    return stats
