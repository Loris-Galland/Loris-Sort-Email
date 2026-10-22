"""Streamlit dashboard for reviewing, sorting and deleting emails.

Run:        uv run streamlit run ui/app.py
Demo mode:  uv run streamlit run ui/app.py -- --demo
"""

import sys

import pandas as pd
import plotly.express as px
import streamlit as st

from mail_sorter import db
from mail_sorter.actions import delete_emails, move_emails, restore
from mail_sorter.db import IN_INBOX, ActionLog, Email, get_session, init_db, sender_domain
from mail_sorter.demo import DEMO_DB_PATH, DemoGraph
from mail_sorter.graph import GraphClient, GraphError, NotLoggedInError

DEMO = "--demo" in sys.argv
if DEMO:
    db.configure(DEMO_DB_PATH)

BAR_COLOR = "#2a78d6"
UNCLASSIFIED = "(not classified)"
TABLE_COLUMNS = [
    "message_id",
    "received_at",
    "sender_name",
    "sender_email",
    "subject",
    "category",
    "confidence",
    "reason",
    "is_read",
]

st.set_page_config(page_title="Mail Sorter", layout="wide")
init_db()


@st.cache_data
def load_inbox() -> pd.DataFrame:
    with get_session() as session:
        rows = [e.to_dict() for e in session.query(Email).filter(Email.status.in_(IN_INBOX))]
    df = pd.DataFrame(rows, columns=list(Email().to_dict().keys()))
    df["domain"] = df["sender_email"].map(sender_domain)
    df["category"] = df["category"].fillna(UNCLASSIFIED)
    df["received_at"] = pd.to_datetime(df["received_at"])
    df["size_bytes"] = df["size_bytes"].fillna(0)
    return df.sort_values("received_at", ascending=False)


def graph_client():
    return DemoGraph() if DEMO else GraphClient()


def run_action(ids: list[str], action: str, target: str | None) -> str:
    client = graph_client()
    with get_session() as session:
        if action == "restore":
            result = restore(client, session, int(target))
            done = "restored to the inbox"
        else:
            emails = session.query(Email).filter(Email.message_id.in_(ids)).all()
            if action == "delete":
                result = delete_emails(client, session, emails, target)
                done = "moved to the trash"
            else:
                result = move_emails(client, session, emails, target)
                done = f"moved to {target}"
    load_inbox.clear()
    message = f"{result.succeeded} emails {done}."
    if result.failed:
        message += f" {result.failed} failed (already moved or deleted in Outlook?)."
    return message


def ask_confirmation(ids: list[str], action: str, target: str | None, count: int) -> None:
    # kept in session state so the dialog survives the reruns its own buttons trigger
    st.session_state.pending = {"ids": ids, "action": action, "target": target, "count": count}


def clear_pending() -> None:
    st.session_state.pop("pending", None)


@st.dialog("Confirm", on_dismiss=clear_pending)
def confirm(ids: list[str], action: str, target: str | None, count: int) -> None:
    if action == "delete":
        st.write(f"Move **{count} emails** to Deleted Items?")
        st.caption("A JSON backup is written first. You can undo it from the History tab.")
    elif action == "move":
        st.write(f"Move **{count} emails** to the folder **{target}**?")
        st.caption("The folder is created if it does not exist.")
    else:
        st.write(f"Move **{count} emails** back to the inbox?")

    ok, cancel = st.columns(2)
    if ok.button("Confirm", type="primary", width="stretch"):
        with st.spinner("Working, large selections can take a minute..."):
            try:
                st.session_state.flash = ("success", run_action(ids, action, target))
            except (GraphError, NotLoggedInError, ValueError) as exc:
                st.session_state.flash = ("error", str(exc))
        clear_pending()
        st.rerun()
    if cancel.button("Cancel", width="stretch"):
        clear_pending()
        st.rerun()


def bar_chart(counts: pd.Series, title: str) -> None:
    data = counts.sort_values().rename_axis("label").reset_index(name="emails")
    fig = px.bar(data, x="emails", y="label", orientation="h", text="emails", title=title)
    fig.update_traces(
        marker_color=BAR_COLOR,
        textposition="outside",
        cliponaxis=False,
        hovertemplate="%{y}: %{x} emails<extra></extra>",
    )
    fig.update_layout(
        height=max(260, 30 * len(data) + 70),
        margin={"l": 0, "r": 40, "t": 40, "b": 0},
        barcornerradius=4,
        xaxis={"visible": False},
        yaxis={"title": None},
    )
    st.plotly_chart(fig, width="stretch")


def email_table(df: pd.DataFrame, key: str, default_folder: str = "") -> None:
    """Selectable table with delete / move actions on the selected rows."""
    if df.empty:
        st.info("No email here.")
        return

    select_all = st.checkbox(f"Select all {len(df)} emails", key=f"{key}_all")
    view = df[TABLE_COLUMNS].copy()
    view.insert(0, "select", select_all)
    edited = st.data_editor(
        view,
        # a new key resets the checkboxes when "select all" is toggled
        key=f"{key}_editor_{select_all}",
        hide_index=True,
        width="stretch",
        height=420,
        disabled=TABLE_COLUMNS,
        column_config={
            "message_id": None,
            "select": st.column_config.CheckboxColumn("", width="small"),
            "received_at": st.column_config.DatetimeColumn("Date", format="YYYY-MM-DD"),
            "sender_name": "Name",
            "sender_email": "Sender",
            "subject": st.column_config.TextColumn("Subject", width="large"),
            "category": "Category",
            "confidence": st.column_config.NumberColumn("Conf.", format="%d"),
            "reason": "Reason",
            "is_read": st.column_config.CheckboxColumn("Read"),
        },
    )
    selected = edited.loc[edited["select"], "message_id"].tolist()
    count = len(selected)

    delete_col, folder_col, move_col = st.columns([1, 2, 1], vertical_alignment="bottom")
    if delete_col.button(
        f"Delete {count} selected", key=f"{key}_delete", disabled=not count, width="stretch"
    ):
        ask_confirmation(selected, "delete", key.split("_", 1)[1], count)
    folder = folder_col.text_input(
        "Destination folder (use / for subfolders)",
        value=default_folder,
        key=f"{key}_folder",
        placeholder="Jeux/Riot",
    ).strip()
    if move_col.button(
        f"Move {count} to folder",
        key=f"{key}_move",
        disabled=not count or not folder,
        width="stretch",
    ):
        ask_confirmation(selected, "move", folder, count)


def overview_tab(df: pd.DataFrame) -> None:
    total = len(df)
    classified = int((df["category"] != UNCLASSIFIED).sum())
    cols = st.columns(4)
    cols[0].metric("Emails in inbox", f"{total:,}".replace(",", " "))
    cols[1].metric("Unread", f"{int((~df['is_read'].astype(bool)).sum()):,}".replace(",", " "))
    cols[2].metric("Classified", f"{classified / total:.0%}" if total else "0%")
    cols[3].metric("Size", f"{df['size_bytes'].sum() / 1_048_576:.0f} MB")

    left, right = st.columns(2)
    with left:
        bar_chart(df["category"].value_counts(), "Emails by category")
    with right:
        bar_chart(df["domain"].replace("", "(unknown)").value_counts().head(15), "Top 15 senders")


def category_tab(df: pd.DataFrame) -> None:
    counts = df["category"].value_counts()
    category = st.selectbox(
        "Category", counts.index, format_func=lambda c: f"{c} ({counts[c]})", key="cat_select"
    )
    subset = df[df["category"] == category]
    if category != UNCLASSIFIED:
        min_conf = st.slider("Minimum confidence", 0, 100, 0, step=5, key="cat_conf")
        subset = subset[subset["confidence"].fillna(0) >= min_conf]
    email_table(subset, f"cat_{category}")


def sender_tab(df: pd.DataFrame) -> None:
    by = st.radio("Group by", ["domain", "sender_email"], horizontal=True, key="sender_by")
    counts = df[by].replace("", "(unknown)").value_counts()
    sender = st.selectbox(
        "Sender", counts.index, format_func=lambda s: f"{s} ({counts[s]})", key="sender_select"
    )
    subset = df[df[by].replace("", "(unknown)") == sender]
    # suggest a folder named after the domain, e.g. riotgames.com -> Riotgames
    default = sender_domain(f"x@{sender}" if by == "domain" else sender).split(".")[0].capitalize()
    email_table(subset, f"sender_{sender}", default)


def history_tab() -> None:
    with get_session() as session:
        logs = session.query(ActionLog).order_by(ActionLog.id.desc()).all()
        restored = {log.category for log in logs if log.action == "restore"}
        rows = [
            {
                "Id": log.id,
                "Date (UTC)": log.timestamp,
                "Action": log.action,
                "Target": log.category,
                "Emails": log.count,
                "Backup": log.backup_file,
            }
            for log in logs
        ]
    if not rows:
        st.info("No action yet.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    undoable = [
        r for r in rows if r["Action"] in ("move", "delete") and f"#{r['Id']}" not in restored
    ]
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
        ask_confirmation([], "restore", str(choice["Id"]), choice["Emails"])


st.title("Mail Sorter")
if DEMO:
    st.caption("Demo mode: fake data, nothing is sent to Outlook.")
else:
    from mail_sorter.auth import get_valid_access_token

    if not get_valid_access_token():
        st.warning("Not logged in, actions will fail. Run: mail-sorter auth login")

if flash := st.session_state.pop("flash", None):
    kind, message = flash
    (st.success if kind == "success" else st.error)(message)

data = load_inbox()
if data.empty:
    st.info(
        "No email indexed yet. Run these commands first:\n\n"
        "```\nmail-sorter auth login\nmail-sorter index\nmail-sorter classify\n```"
    )
    history_tab()
    if pending := st.session_state.get("pending"):
        confirm(**pending)
    st.stop()

overview, by_category, by_sender, history_ = st.tabs(
    ["Overview", "By category", "By sender", "History"]
)
with overview:
    overview_tab(data)
with by_category:
    category_tab(data)
with by_sender:
    sender_tab(data)
with history_:
    history_tab()

if pending := st.session_state.get("pending"):
    confirm(**pending)
