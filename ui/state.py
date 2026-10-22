"""Data loading, confirmed actions and long-running jobs shared by every dashboard view."""

import sys
from dataclasses import dataclass, field

import httpx
import pandas as pd
import streamlit as st

from mail_sorter import db
from mail_sorter import rules as rules_module
from mail_sorter.actions import delete_emails, move_emails, restore
from mail_sorter.classifier import OllamaError, check_ollama, classify_emails
from mail_sorter.db import IN_INBOX, Action, Email, Status, get_session, sender_domain
from mail_sorter.demo import DemoGraph
from mail_sorter.graph import GraphClient, GraphError, NotLoggedInError
from mail_sorter.processor import index_inbox
from mail_sorter.rules import Rule, RuleError, load_rules, save_rules, upsert_rule

DEMO = "--demo" in sys.argv
UNKNOWN = "(unknown)"
UNCLASSIFIED = "(not classified)"


def configure_demo() -> None:
    """In demo mode, keep the database, rules and backups apart from the real ones."""
    if DEMO:
        from mail_sorter import actions, demo

        db.configure(demo.DEMO_DB_PATH)
        rules_module.RULES_PATH = demo.DEMO_RULES_PATH
        actions.BACKUP_DIR = demo.DEMO_BACKUP_DIR


@st.cache_data
def _load_inbox(db_path: str) -> pd.DataFrame:
    with get_session() as session:
        rows = [e.to_dict() for e in session.query(Email).filter(Email.status.in_(IN_INBOX))]
    df = pd.DataFrame(rows, columns=list(Email().to_dict().keys()))
    df["sender_email"] = df["sender_email"].fillna("").replace("", UNKNOWN)
    df["domain"] = df["sender_email"].map(sender_domain).replace("", UNKNOWN)
    df["category"] = df["category"].fillna(UNCLASSIFIED)
    df["received_at"] = pd.to_datetime(df["received_at"])
    df["size_bytes"] = df["size_bytes"].fillna(0)
    df["is_read"] = df["is_read"].fillna(False).astype(bool)
    return df.sort_values("received_at", ascending=False)


def load_inbox() -> pd.DataFrame:
    # keyed by path so the demo and the real database never share a cache entry
    return _load_inbox(db.DB_PATH)


def refresh() -> None:
    _load_inbox.clear()


def current_rules() -> list[Rule]:
    if not rules_module.RULES_PATH.exists():
        return []
    try:
        return load_rules()
    except RuleError as exc:
        st.error(f"rules.toml: {exc}")
        return []


def graph_client():
    return DemoGraph() if DEMO else GraphClient()


def flash(kind: str, message: str) -> None:
    """Message shown at the top of the page after the next rerun."""
    st.session_state.flash = (kind, message)


@dataclass
class PendingAction:
    action: Action
    count: int
    ids: list[str] = field(default_factory=list)
    target: str | None = None  # folder for a move, label for a delete
    log_id: int | None = None  # action to undo for a restore
    rule: Rule | None = None  # saved to rules.toml once the action succeeded


CONFIRM_TEXT = {
    Action.DELETE: "Move **{count} emails** to Deleted Items",
    Action.MOVE: "Move **{count} emails** to the folder **{target}**",
    Action.RESTORE: "Move **{count} emails** back to the inbox",
}

DONE_TEXT = {
    Action.DELETE: "{n} emails moved to the trash.",
    Action.MOVE: "{n} emails moved to {target}.",
    Action.RESTORE: "{n} emails restored to the inbox.",
}


def ask_confirmation(actions: list[PendingAction]) -> None:
    # kept in session state so the dialog survives the reruns its own buttons trigger
    st.session_state.pending = actions


def clear_pending() -> None:
    st.session_state.pop("pending", None)


def _execute(pending: PendingAction, on_progress) -> str:
    client = graph_client()
    with get_session() as session:
        if pending.action == Action.RESTORE:
            result = restore(client, session, pending.log_id, on_progress)
        else:
            emails = session.query(Email).filter(Email.message_id.in_(pending.ids)).all()
            if pending.action == Action.DELETE:
                result = delete_emails(client, session, emails, pending.target, on_progress)
            else:
                result = move_emails(client, session, emails, pending.target, on_progress)

    message = DONE_TEXT[pending.action].format(n=result.succeeded, target=pending.target)
    if result.failed:
        message += f" {result.failed} failed (already moved or deleted in Outlook?)."
    if pending.rule:
        save_rules(upsert_rule(current_rules(), pending.rule))
        message += " Rule saved for future emails."
    return message


@st.dialog("Confirm", on_dismiss=clear_pending)
def confirm(actions: list[PendingAction]) -> None:
    for pending in actions:
        st.markdown("- " + CONFIRM_TEXT[pending.action].format(**vars(pending)))
    if any(p.action == Action.DELETE for p in actions):
        st.caption("Deleted emails go to Deleted Items, nothing is permanently deleted.")
    st.caption("A JSON backup is written first, every action can be undone from History.")
    for pending in actions:
        if pending.rule:
            st.caption(
                f"Rule saved too: {', '.join(pending.rule.senders)} -> {pending.rule.label}."
            )

    ok, cancel = st.columns(2)
    if ok.button("Confirm", type="primary", width="stretch"):
        total = sum(p.count for p in actions)
        bar = st.progress(0.0, text="Working...")
        done = 0

        def on_progress(n: int) -> None:
            nonlocal done
            done += n
            bar.progress(min(done / max(total, 1), 1.0), text=f"{done} / {total}")

        messages = []
        try:
            for pending in actions:
                messages.append(_execute(pending, on_progress))
            flash("success", " ".join(messages))
        except (GraphError, NotLoggedInError, ValueError) as exc:
            flash("error", " ".join([*messages, f"Stopped: {exc}"]))
        refresh()
        clear_pending()
        st.rerun()
    if cancel.button("Cancel", width="stretch"):
        clear_pending()
        st.rerun()


def show_pending_dialog() -> None:
    if actions := st.session_state.get("pending"):
        confirm(actions)


def sync_inbox() -> None:
    """Re-index the inbox from Outlook, with a progress bar in the sidebar."""
    client = GraphClient()
    try:
        total = client.inbox_count()
        bar = st.sidebar.progress(0.0, text="Syncing...")
        seen = 0

        def on_progress() -> None:
            nonlocal seen
            seen += 1
            if seen % 100 == 0 or seen == total:
                bar.progress(min(seen / max(total, 1), 1.0), text=f"{seen} / {total}")

        with get_session() as session:
            stats = index_inbox(client, session, on_progress)
        flash(
            "success",
            f"Inbox synced: {stats.added} new, {stats.updated} refreshed, "
            f"{stats.gone} no longer in the inbox.",
        )
    except (GraphError, NotLoggedInError) as exc:
        flash("error", f"Sync failed: {exc}")
    refresh()
    st.rerun()


def classify_pending(limit: int) -> None:
    """Classify the most recent unclassified emails with the local Ollama model."""
    http = httpx.Client()
    try:
        check_ollama(http)
    except OllamaError as exc:
        flash("error", str(exc))
        st.rerun()

    bar = st.sidebar.progress(0.0, text="Classifying...")
    with get_session() as session:
        emails = (
            session.query(Email)
            .filter(Email.status == Status.PENDING)
            .order_by(Email.received_at.desc())
            .limit(limit)
            .all()
        )
        done = 0

        def on_progress(n: int) -> None:
            nonlocal done
            done += n
            bar.progress(min(done / max(len(emails), 1), 1.0), text=f"{done} / {len(emails)}")

        try:
            stats = classify_emails(session, http, emails, on_progress=on_progress)
            flash("success", f"{stats.classified} emails classified, {stats.failed} failed.")
        except httpx.HTTPError as exc:
            flash("error", f"Ollama error: {exc}")
    refresh()
    st.rerun()
