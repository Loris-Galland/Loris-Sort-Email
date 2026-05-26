"""Streamlit dashboard for reviewing and deleting classified emails."""

# Phase 4 — implemented in the dashboard phase

import streamlit as st

st.set_page_config(page_title="Mail Sorter", layout="wide")

st.title("Mail Sorter")
st.info(
    "Complete phases 1-3 first:\n\n"
    "```\n"
    "mail-sorter auth login\n"
    "mail-sorter index\n"
    "mail-sorter classify\n"
    "```"
)
