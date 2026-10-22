"""Sender-based sorting rules, read from a user-editable rules.toml file.

No AI involved: a rule matches emails by sender address or domain, optionally
by subject keywords and age, then moves them to a folder or to the trash.
"""

import json
import os
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mail_sorter.db import Email

RULES_PATH = Path(os.getenv("RULES_PATH", "./rules.toml"))

TEMPLATE = """\
# Sorting rules for mail-sorter. Rules are checked in order, the first match wins.
#
# senders: full addresses ("news@shop.com") or domains ("riotgames.com").
#          A domain also matches its subdomains (mail.riotgames.com).
# folder:  destination folder, created if missing. Use "/" for subfolders.
# action:  "delete" instead of folder to send matching emails to the trash.
# Optional filters:
#   subject_contains = ["invoice", "facture"]   any of these words in the subject
#   older_than_days = 30                         only emails older than this
#
# Preview with:  mail-sorter rules apply
# Execute with:  mail-sorter rules apply --execute

[[rules]]
folder = "Jeux/Riot"
senders = ["riotgames.com", "leagueoflegends.com"]

# [[rules]]
# action = "delete"
# senders = ["promo@example.com"]
# older_than_days = 30
"""


def _utcnow() -> datetime:
    # received_at is stored as naive UTC
    return datetime.now(UTC).replace(tzinfo=None)


class RuleError(Exception):
    pass


@dataclass
class Rule:
    senders: list[str]
    folder: str | None = None
    delete: bool = False
    subject_contains: list[str] = field(default_factory=list)
    older_than_days: int | None = None

    @property
    def label(self) -> str:
        return "trash" if self.delete else f"folder {self.folder}"

    def to_toml(self) -> str:
        # json strings and lists are valid TOML values
        lines = ["[[rules]]"]
        if self.delete:
            lines.append('action = "delete"')
        else:
            lines.append(f"folder = {json.dumps(self.folder, ensure_ascii=False)}")
        lines.append(f"senders = {json.dumps(self.senders, ensure_ascii=False)}")
        if self.subject_contains:
            lines.append(
                f"subject_contains = {json.dumps(self.subject_contains, ensure_ascii=False)}"
            )
        if self.older_than_days is not None:
            lines.append(f"older_than_days = {self.older_than_days}")
        return "\n".join(lines)

    def matches_sender(self, address: str | None) -> bool:
        address = (address or "").lower()
        if "@" not in address:
            return False
        domain = address.rsplit("@", 1)[1]
        for pattern in self.senders:
            if "@" in pattern:
                if address == pattern:
                    return True
            elif domain == pattern or domain.endswith("." + pattern):
                return True
        return False

    def matches(self, email: Email, now: datetime | None = None) -> bool:
        if not self.matches_sender(email.sender_email):
            return False
        if self.subject_contains:
            subject = (email.subject or "").lower()
            if not any(word in subject for word in self.subject_contains):
                return False
        if self.older_than_days is not None:
            cutoff = (now or _utcnow()) - timedelta(days=self.older_than_days)
            if not email.received_at or email.received_at > cutoff:
                return False
        return True


def parse_rules(data: dict) -> list[Rule]:
    rules = []
    for i, raw in enumerate(data.get("rules", []), start=1):
        senders = [s.strip().lower() for s in raw.get("senders", []) if s.strip()]
        if not senders:
            raise RuleError(f"Rule {i}: 'senders' must list at least one address or domain")

        action = raw.get("action")
        folder = (raw.get("folder") or "").strip() or None
        if action not in (None, "delete"):
            raise RuleError(f"Rule {i}: unknown action {action!r}, only 'delete' is supported")
        if bool(folder) == (action == "delete"):
            raise RuleError(f"Rule {i}: set either 'folder' or action = \"delete\", not both")

        older = raw.get("older_than_days")
        if older is not None and (not isinstance(older, int) or older < 0):
            raise RuleError(f"Rule {i}: 'older_than_days' must be a positive integer")

        rules.append(
            Rule(
                senders=senders,
                folder=folder,
                delete=action == "delete",
                subject_contains=[w.lower() for w in raw.get("subject_contains", [])],
                older_than_days=older,
            )
        )
    return rules


def load_rules(path: Path | None = None) -> list[Rule]:
    path = path or RULES_PATH
    if not path.exists():
        raise RuleError(f"{path} not found. Create it with: mail-sorter rules init")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RuleError(f"{path} is not valid TOML: {exc}") from exc
    return parse_rules(data)


def save_rules(rules: list[Rule], path: Path | None = None) -> None:
    path = path or RULES_PATH
    header = TEMPLATE.split("[[rules]]")[0].rstrip()
    body = "\n\n".join(rule.to_toml() for rule in rules)
    path.write_text(f"{header}\n\n{body}\n", encoding="utf-8")


def upsert_rule(rules: list[Rule], rule: Rule) -> list[Rule]:
    """Replace the rule targeting the same senders, or append the new one."""
    same = [i for i, r in enumerate(rules) if sorted(r.senders) == sorted(rule.senders)]
    if same:
        return [rule if i == same[0] else r for i, r in enumerate(rules) if i not in same[1:]]
    return [*rules, rule]


def first_rule_for(address: str, rules: list[Rule]) -> Rule | None:
    """First rule whose senders match this address, ignoring subject and age filters."""
    return next((rule for rule in rules if rule.matches_sender(address)), None)


def match_rules(emails: list[Email], rules: list[Rule]) -> list[tuple[Rule, list[Email]]]:
    """Assign each email to the first rule it matches. Rules with no match are kept."""
    now = _utcnow()
    buckets: list[list[Email]] = [[] for _ in rules]
    for email in emails:
        for i, rule in enumerate(rules):
            if rule.matches(email, now):
                buckets[i].append(email)
                break
    return list(zip(rules, buckets, strict=True))


def build_template(suggested_domains: list[tuple[str, int]]) -> str:
    """Rules template, followed by the biggest senders as commented suggestions."""
    if not suggested_domains:
        return TEMPLATE
    lines = ["", "# Your biggest senders, uncomment and adapt:"]
    for domain, count in suggested_domains:
        lines += [
            "",
            f"# {count} emails",
            "# [[rules]]",
            '# folder = ""',
            f'# senders = ["{domain}"]',
        ]
    return TEMPLATE + "\n".join(lines) + "\n"
