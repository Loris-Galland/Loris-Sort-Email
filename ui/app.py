"""Streamlit dashboard to sort, move and delete Outlook emails visually.

Run:        uv run streamlit run ui/app.py
Demo mode:  uv run streamlit run ui/app.py -- --demo
"""

import sys
from pathlib import Path

# streamlit run puts ui/ on the import path, the test runner does not
sys.path.insert(0, str(Path(__file__).parent))

import streamlit as st
from state import DEMO, configure_demo, current_rules, load_inbox, show_pending_dialog
from views import PAGES, VIEWS, sidebar

from mail_sorter.db import init_db

st.set_page_config(page_title="Mail Sorter", layout="wide")
configure_demo()
init_db()

data = load_inbox()
rules = current_rules()

logged_in = DEMO
if not DEMO:
    from mail_sorter.auth import get_valid_access_token

    logged_in = get_valid_access_token() is not None
sidebar(data, logged_in)

if flash := st.session_state.pop("flash", None):
    kind, message = flash
    (st.success if kind == "success" else st.error)(message)

page = st.segmented_control(
    "Page", PAGES, default="Overview", required=True, key="page", label_visibility="collapsed"
)

if data.empty and page not in ("Rules", "History"):
    st.info(
        "No email indexed yet. Log in with `uv run mail-sorter auth login`, "
        "then click **Sync inbox** in the sidebar."
    )
else:
    VIEWS[page](data, rules)

show_pending_dialog()
