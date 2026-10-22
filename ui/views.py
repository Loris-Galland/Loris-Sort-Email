"""The dashboard pages and the sidebar."""

import pandas as pd
import streamlit as st
from state import (
    DEMO,
    UNCLASSIFIED,
    UNKNOWN,
    PendingAction,
    ask_confirmation,
    classify_pending,
    flash,
    sync_inbox,
)
from widgets import action_bar, age_filter, bar_chart, email_table, filter_by_age

from mail_sorter.actions import is_restored
from mail_sorter.db import Action, ActionLog, Email, get_session
from mail_sorter.rules import Rule, RuleError, first_rule_for, match_rules, parse_rules, save_rules

PAGES = ["Overview", "Senders", "Categories", "Rules", "History"]


def go_to(page: str, **state) -> None:
    """Switch page from a callback, e.g. when a chart bar is clicked."""
    st.session_state.page = page
    st.session_state.update(state)
    # a new key clears the row selected in the senders table
    st.session_state.sender_table_version = st.session_state.get("sender_table_version", 0) + 1


def _rule_label(rule: Rule | None) -> str:
    if rule is None:
        return ""
    label = "trash" if rule.delete else rule.folder
    if rule.older_than_days:
        label += f" (after {rule.older_than_days} days)"
    return label


def _address_for(sender: str, by: str) -> str:
    # a domain is matched against rules through a fake address on that domain
    return f"x@{sender}" if by == "domain" else sender


def sidebar(df: pd.DataFrame, logged_in: bool) -> None:
    with st.sidebar:
        st.header("Mail Sorter")
        if DEMO:
            st.info("Demo mode: fake data, nothing is sent to Outlook.")
        elif logged_in:
            st.success("Connected to Outlook")
        else:
            st.error("Not logged in. In a terminal run:\n\n`uv run mail-sorter auth login`")

        st.subheader("Sync")
        st.caption("Fetch the latest emails of your inbox (metadata only).")
        if st.button("Sync inbox", disabled=DEMO or not logged_in, width="stretch"):
            sync_inbox()

        st.subheader("Classify with AI")
        pending = int((df["category"] == UNCLASSIFIED).sum()) if not df.empty else 0
        st.caption(f"{pending} emails not classified yet. Runs locally with Ollama.")
        limit = st.number_input("Emails to classify", min_value=10, value=200, step=50)
        if st.button("Classify", disabled=DEMO or not pending, width="stretch"):
            classify_pending(int(limit))


def overview(df: pd.DataFrame, rules: list[Rule]) -> None:
    total = len(df)
    classified = int((df["category"] != UNCLASSIFIED).sum())
    cols = st.columns(4)
    cols[0].metric("Emails in inbox", f"{total:,}".replace(",", " "))
    cols[1].metric("Unread", f"{int((~df['is_read']).sum()):,}".replace(",", " "))
    cols[2].metric("Classified", f"{classified / total:.0%}" if total else "0%")
    cols[3].metric("Size", f"{df['size_bytes'].sum() / 1_048_576:.0f} MB")

    st.caption("Click a bar to open that sender or category.")
    left, right = st.columns(2)
    with left:
        bar_chart(
            df["domain"].value_counts().head(15),
            "Top 15 senders",
            "chart_senders",
            on_click=lambda d: go_to("Senders", sender_pick=d, sender_by="domain"),
        )
    with right:
        bar_chart(
            df["category"].value_counts(),
            "Emails by category",
            "chart_categories",
            on_click=lambda c: go_to("Categories", category_pick=c),
        )


def _sender_table(df: pd.DataFrame, by: str, rules: list[Rule]) -> pd.DataFrame:
    table = (
        df.groupby(by)
        .agg(
            name=("sender_name", lambda names: names.mode().iat[0] if len(names) else ""),
            emails=("message_id", "count"),
            unread=("is_read", lambda read: int((~read).sum())),
            size_mb=("size_bytes", lambda size: round(size.sum() / 1_048_576, 1)),
            latest=("received_at", "max"),
        )
        .reset_index()
        .rename(columns={by: "sender"})
        .sort_values("emails", ascending=False)
    )
    table["rule"] = [
        _rule_label(first_rule_for(_address_for(s, by), rules)) for s in table["sender"]
    ]
    return table


def senders(df: pd.DataFrame, rules: list[Rule]) -> None:
    st.caption(
        "Pick a sender, check its emails, then move them to a folder or delete them. "
        "Saving a rule sorts its future emails the same way (Rules page)."
    )
    by = st.segmented_control(
        "Group by",
        ["domain", "sender_email"],
        format_func={"domain": "Domain", "sender_email": "Address"}.get,
        default="domain",
        required=True,
        key="sender_by",
    )
    search_col, toggle_col = st.columns([2, 1], vertical_alignment="bottom")
    search = search_col.text_input("Search", placeholder="riot, amazon...", key="sender_search")
    hide_ruled = toggle_col.toggle("Hide senders that have a rule", key="sender_hide_ruled")

    table = _sender_table(df, by, rules)
    if search:
        mask = table["sender"].str.contains(search, case=False, regex=False)
        mask |= table["name"].fillna("").str.contains(search, case=False, regex=False)
        table = table[mask]
    if hide_ruled:
        table = table[table["rule"] == ""]

    event = st.dataframe(
        table,
        key=f"sender_table_{st.session_state.get('sender_table_version', 0)}",
        on_select="rerun",
        selection_mode="single-row",
        hide_index=True,
        height=300,
        column_config={
            "sender": "Sender",
            "name": "Name",
            "emails": st.column_config.ProgressColumn(
                "Emails", format="%d", min_value=0, max_value=int(table["emails"].max() or 1)
            ),
            "unread": "Unread",
            "size_mb": st.column_config.NumberColumn("Size", format="%.1f MB"),
            "latest": st.column_config.DatetimeColumn("Latest", format="YYYY-MM-DD"),
            "rule": "Rule",
        },
    )
    if event.selection.rows:
        st.session_state.sender_pick = table.iloc[event.selection.rows[0]]["sender"]

    picked = st.session_state.get("sender_pick")
    if not picked or picked not in set(df[by]):
        st.info("Click a row in the table to see that sender's emails.")
        return

    subset = df[df[by] == picked]
    rule = first_rule_for(_address_for(picked, by), rules)
    st.subheader(f"{picked}, {len(subset)} emails")
    if rule:
        st.caption(f"Current rule: {_rule_label(rule)}")

    older_than = age_filter("sender_age")
    subset = filter_by_age(subset, older_than)
    if subset.empty:
        st.info("No email matches this filter.")
        return

    names = subset["sender_name"].replace("", pd.NA).dropna()
    default_folder = (
        rule.folder if rule and rule.folder else (names.mode().iat[0] if len(names) else "")
    )
    key = f"sender_{by}_{picked}"
    selected = email_table(subset, key, select_all_default=True)
    action_bar(
        selected,
        key,
        label=picked,
        default_folder=default_folder,
        rule_senders=None if picked == UNKNOWN else [picked],
        older_than_days=older_than,
    )


def categories(df: pd.DataFrame, rules: list[Rule]) -> None:
    counts = df["category"].value_counts()
    if st.session_state.get("category_pick") not in counts.index:
        st.session_state.category_pick = counts.index[0]
    category = st.segmented_control(
        "Category",
        list(counts.index),
        format_func=lambda c: f"{c} ({counts[c]})",
        required=True,
        key="category_pick",
    )
    subset = df[df["category"] == category]

    conf_col, age_col = st.columns([1, 2])
    if category != UNCLASSIFIED:
        min_conf = conf_col.slider("Minimum AI confidence", 0, 100, 0, step=5, key="cat_conf")
        subset = subset[subset["confidence"].fillna(0) >= min_conf]
    with age_col:
        subset = filter_by_age(subset, age_filter("cat_age"))

    if subset.empty:
        st.info("No email matches these filters.")
        return
    key = f"cat_{category}"
    action_bar(email_table(subset, key), key, label=category)


def _rules_frame(rules: list[Rule]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "senders": ", ".join(r.senders),
                "action": "delete" if r.delete else "move",
                "folder": r.folder or "",
                "older_than_days": r.older_than_days,
                "subject_contains": ", ".join(r.subject_contains),
            }
            for r in rules
        ],
        columns=["senders", "action", "folder", "older_than_days", "subject_contains"],
    )


def _split(text) -> list[str]:
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def _rules_from_frame(frame: pd.DataFrame) -> list[Rule]:
    raw_rules = []
    for row in frame.to_dict("records"):
        if not _split(row["senders"]):
            continue
        raw = {
            "senders": _split(row["senders"]),
            "subject_contains": _split(row["subject_contains"]),
        }
        if row["action"] == "delete":
            raw["action"] = "delete"
        else:
            raw["folder"] = str(row["folder"] or "")
        if pd.notna(row["older_than_days"]):
            raw["older_than_days"] = int(row["older_than_days"])
        raw_rules.append(raw)
    return parse_rules({"rules": raw_rules})


def rules_page(df: pd.DataFrame, rules: list[Rule]) -> None:
    st.caption(
        "Rules sort emails by sender, the first matching rule wins. A domain like "
        "riotgames.com also matches its subdomains. Edit the table, save, then apply."
    )
    edited = st.data_editor(
        _rules_frame(rules),
        key="rules_editor",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "senders": st.column_config.TextColumn(
                "Senders", help="Domains or addresses, comma separated", width="large"
            ),
            "action": st.column_config.SelectboxColumn(
                "Action", options=["move", "delete"], default="move", required=True
            ),
            "folder": st.column_config.TextColumn("Folder", help="e.g. Jeux/Riot"),
            "older_than_days": st.column_config.NumberColumn(
                "Older than (days)", min_value=0, step=1
            ),
            "subject_contains": st.column_config.TextColumn(
                "Subject contains", help="Optional words, comma separated"
            ),
        },
    )
    if not rules:
        st.info(
            'No rule yet. Add a row above, or tick "Also save as a rule" when you move '
            "or delete a sender's emails on the Senders page."
        )
    if st.button("Save rules"):
        try:
            save_rules(_rules_from_frame(edited))
            flash("success", "Rules saved.")
            st.rerun()
        except RuleError as exc:
            st.error(str(exc))

    if not rules or df.empty:
        return

    st.subheader("What the saved rules would do now")
    emails = [
        Email(
            message_id=row["message_id"],
            sender_email=row["sender_email"],
            subject=row["subject"],
            received_at=row["received_at"].to_pydatetime()
            if pd.notna(row["received_at"])
            else None,
        )
        for row in df[["message_id", "sender_email", "subject", "received_at"]].to_dict("records")
    ]
    matches = match_rules(emails, rules)
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Senders": ", ".join(rule.senders),
                    "Does": _rule_label(rule),
                    "Emails": len(found),
                }
                for rule, found in matches
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    actions = [
        PendingAction(
            Action.DELETE if rule.delete else Action.MOVE,
            len(found),
            [e.message_id for e in found],
            ", ".join(rule.senders) if rule.delete else rule.folder,
        )
        for rule, found in matches
        if found
    ]
    total = sum(a.count for a in actions)
    if st.button(f"Apply all rules ({total} emails)", type="primary", disabled=not total):
        ask_confirmation(actions)


def history(df: pd.DataFrame, rules: list[Rule]) -> None:
    with get_session() as session:
        logs = session.query(ActionLog).order_by(ActionLog.id.desc()).all()
        rows = [
            {
                "Id": log.id,
                "Date (UTC)": log.timestamp,
                "Action": log.action,
                "Target": log.target,
                "Emails": log.count,
                "Undone": is_restored(session, log.id),
                "Backup": log.backup_file,
            }
            for log in logs
        ]
    if not rows:
        st.info("No action yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    undoable = [r for r in rows if r["Action"] in (Action.MOVE, Action.DELETE) and not r["Undone"]]
    if not undoable:
        return
    choice = st.selectbox(
        "Undo an action",
        undoable,
        format_func=lambda r: (
            f"#{r['Id']} {r['Action']} {r['Target'] or ''} ({r['Emails']} emails)"
        ),
    )
    if st.button("Restore to inbox"):
        ask_confirmation([PendingAction(Action.RESTORE, choice["Emails"], log_id=choice["Id"])])


VIEWS = {
    "Overview": overview,
    "Senders": senders,
    "Categories": categories,
    "Rules": rules_page,
    "History": history,
}
