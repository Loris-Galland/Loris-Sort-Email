"""Reusable dashboard pieces: charts, filters, the selectable email table and its actions."""

from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st
from state import PendingAction, ask_confirmation

from mail_sorter.db import Action
from mail_sorter.rules import Rule

BAR_COLOR = "#2a78d6"

AGE_FILTERS = {
    "Any age": None,
    "Older than 1 month": 30,
    "Older than 3 months": 90,
    "Older than 6 months": 180,
    "Older than 1 year": 365,
}

TABLE_COLUMNS = [
    "message_id",
    "received_at",
    "sender_name",
    "sender_email",
    "subject",
    "category",
    "confidence",
    "is_read",
]


def bar_chart(
    counts: pd.Series, title: str, key: str, on_click: Callable[[str], None] | None = None
) -> None:
    """Horizontal bar chart, biggest on top. Clicking a bar calls on_click with its label."""
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
        clickmode="event+select",
    )

    def handle_select() -> None:
        points = st.session_state[key].selection.points
        if points and on_click:
            on_click(points[0]["y"])

    st.plotly_chart(
        fig,
        width="stretch",
        key=key,
        on_select=handle_select if on_click else "ignore",
        selection_mode="points",
    )


def age_filter(key: str) -> int | None:
    choice = st.segmented_control(
        "Age", list(AGE_FILTERS), default="Any age", required=True, key=key
    )
    return AGE_FILTERS[choice]


def filter_by_age(df: pd.DataFrame, older_than_days: int | None) -> pd.DataFrame:
    if older_than_days is None:
        return df
    cutoff = datetime.now() - timedelta(days=older_than_days)
    return df[df["received_at"] < cutoff]


def email_table(df: pd.DataFrame, key: str, select_all_default: bool = False) -> list[str]:
    """Selectable email table. Returns the message IDs of the selected rows."""
    select_all = st.checkbox(
        f"Select all {len(df)} emails", value=select_all_default, key=f"{key}_all"
    )
    view = df[TABLE_COLUMNS].copy()
    view.insert(0, "select", select_all)
    edited = st.data_editor(
        view,
        # a new key resets the checkboxes when "select all" is toggled
        key=f"{key}_editor_{select_all}",
        hide_index=True,
        width="stretch",
        height=min(420, 38 + 35 * len(view)),
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
            "is_read": st.column_config.CheckboxColumn("Read"),
        },
    )
    return edited.loc[edited["select"], "message_id"].tolist()


def action_bar(
    selected: list[str],
    key: str,
    label: str,
    default_folder: str = "",
    rule_senders: list[str] | None = None,
    older_than_days: int | None = None,
) -> None:
    """Delete / move buttons for the selected emails, optionally saving a sender rule."""
    count = len(selected)
    folder = st.text_input(
        "Destination folder (use / for subfolders, e.g. Jeux/Riot)",
        value=default_folder,
        key=f"{key}_folder",
    ).strip()

    save_rule = False
    if rule_senders:
        save_rule = st.checkbox(
            "Also save as a rule, to sort future emails from this sender the same way",
            value=True,
            key=f"{key}_save_rule",
        )

    def rule_for(delete: bool) -> Rule | None:
        if not save_rule:
            return None
        return Rule(
            senders=rule_senders,
            folder=None if delete else folder,
            delete=delete,
            older_than_days=older_than_days,
        )

    move_col, delete_col = st.columns(2)
    if move_col.button(
        f"Move {count} emails to {folder or '...'}",
        key=f"{key}_move",
        type="primary",
        disabled=not count or not folder,
        width="stretch",
    ):
        ask_confirmation(
            [PendingAction(Action.MOVE, count, selected, folder, rule=rule_for(False))]
        )
    if delete_col.button(
        f"Delete {count} emails", key=f"{key}_delete", disabled=not count, width="stretch"
    ):
        ask_confirmation(
            [PendingAction(Action.DELETE, count, selected, label, rule=rule_for(True))]
        )
