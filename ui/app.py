"""
ui/app.py
─────────
Streamlit chat interface — pure UI layer.
All agent logic lives in the FastAPI backend (api/main.py).
This module calls the API, renders messages, and handles HITL dialogs.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
import streamlit as st

from config import settings

st.set_page_config(
    page_title=settings.APP_TITLE,
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

_CSS_PATH = Path(__file__).parent / "styles.css"
if _CSS_PATH.exists():
    st.markdown(f"<style>{_CSS_PATH.read_text()}</style>", unsafe_allow_html=True)

logger = logging.getLogger(__name__)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
_TIMEOUT = httpx.Timeout(120.0)

QUICK_QUESTIONS = [
    "What are the top 10 countries by revenue?",
    "Show monthly revenue trend over time",
    "What are the 10 best-selling products by quantity?",
    "How many unique customers are there per country?",
    "What's the average order value by country?",
    "Which products are most frequently cancelled?",
]


# ── Session state ──────────────────────────────────────────────────────────────

def init_session():
    if "thread_id" not in st.session_state:
        st.session_state.thread_id = str(uuid.uuid4())
    if "display_messages" not in st.session_state:
        st.session_state.display_messages = []
    if "pending_hitl" not in st.session_state:
        st.session_state.pending_hitl = None
    if "pending_question" not in st.session_state:
        st.session_state.pending_question = None


# ── API helpers ────────────────────────────────────────────────────────────────

def api_chat(message: str) -> dict:
    resp = httpx.post(
        f"{BACKEND_URL}/chat",
        json={"thread_id": st.session_state.thread_id, "message": message},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def api_hitl(decision: str) -> dict:
    resp = httpx.post(
        f"{BACKEND_URL}/hitl/respond",
        json={"thread_id": st.session_state.thread_id, "decision": decision},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=300, show_spinner=False)
def get_table_stats() -> dict[str, int]:
    try:
        resp = httpx.get(f"{BACKEND_URL}/tables", timeout=30.0)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {}


# ── Sidebar ────────────────────────────────────────────────────────────────────

def render_sidebar():
    with st.sidebar:
        st.markdown(
            "<div style='font-size:1.1rem;font-weight:700;color:#1A1A2E;margin-bottom:4px'>"
            "📊 SQL Data Analyst</div>"
            "<div style='font-size:0.78rem;color:#9CA3AF;margin-bottom:1rem'>"
            f"Model: <code>{settings.PRIMARY_MODEL}</code></div>",
            unsafe_allow_html=True,
        )

        st.markdown('<div class="sidebar-section-title">Database</div>', unsafe_allow_html=True)
        for table, count in get_table_stats().items():
            st.markdown(
                f'<div class="table-badge">'
                f'  <span class="table-name">{table}</span>'
                f'  <span class="row-count">{count:,} rows</span>'
                f'</div>',
                unsafe_allow_html=True,
            )

        st.divider()
        if st.button("🗑️ New conversation", use_container_width=True, type="secondary"):
            st.session_state.thread_id = str(uuid.uuid4())
            st.session_state.display_messages = []
            st.session_state.pending_hitl = None
            st.session_state.pending_question = None
            st.rerun()


# ── Welcome screen (shown when chat is empty) ──────────────────────────────────

def render_welcome():
    st.markdown(
        "<div style='text-align:center;padding:3rem 0 1.5rem'>"
        "<div style='font-size:2.5rem'>📊</div>"
        "<div style='font-size:1.4rem;font-weight:700;color:#1A1A2E;margin:0.6rem 0 0.3rem'>SQL Data Analyst</div>"
        "<div style='color:#6B7280;font-size:0.92rem'>Ask anything about the e-commerce dataset</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    st.markdown(
        "<div style='text-align:center;color:#9CA3AF;font-size:0.72rem;"
        "font-weight:600;letter-spacing:0.09em;text-transform:uppercase;"
        "margin:2rem 0 0.8rem'>Try asking</div>",
        unsafe_allow_html=True,
    )

    cols = st.columns(2)
    for i, q in enumerate(QUICK_QUESTIONS):
        with cols[i % 2]:
            if st.button(q, key=f"welcome_{i}", use_container_width=True):
                st.session_state.pending_question = q
                st.rerun()


# ── Chat messages ──────────────────────────────────────────────────────────────

def render_messages():
    for msg in st.session_state.display_messages:
        if msg["role"] == "user":
            # User bubble — right-aligned
            st.markdown(
                f'<div class="user-message">'
                f'<div class="user-bubble">{msg["content"]}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
        else:
            # Assistant bubble — left-aligned with avatar
            st.markdown(
                '<div class="agent-message">'
                '<div class="agent-avatar">AI</div>'
                '<div class="agent-bubble">',
                unsafe_allow_html=True,
            )
            # Render the model's markdown response (tables, bold, etc.) natively
            st.markdown(msg["content"])
            st.markdown('</div></div>', unsafe_allow_html=True)

            # SQL expander
            if msg.get("sql"):
                with st.expander("🔍 SQL query", expanded=False):
                    st.code(msg["sql"], language="sql")

            # Raw results expander (optional, handy for debugging)
            if msg.get("result_str"):
                with st.expander("📄 Raw results", expanded=False):
                    st.text(msg["result_str"][:2000])  # cap at 2000 chars


# ── HITL dialog ────────────────────────────────────────────────────────────────

def render_hitl_dialog(payload: dict):
    st.markdown(
        f'<div class="hitl-card">'
        f'  <div class="hitl-title">⚠️  Write operation requested</div>'
        f'  <div class="hitl-reason">{payload.get("reason", "")}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    with st.expander("Proposed SQL", expanded=True):
        st.code(payload.get("proposed_sql", ""), language="sql")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("✅ Approve", type="primary", use_container_width=True):
            _resume_hitl("approved")
    with col2:
        if st.button("❌ Deny", type="secondary", use_container_width=True):
            _resume_hitl("denied")


def _resume_hitl(decision: str):
    try:
        data = api_hitl(decision)
        st.session_state.pending_hitl = None
        if response := data.get("response", ""):
            st.session_state.display_messages.append({
                "role": "assistant",
                "content": response,
                "sql": None,
                "result_str": None,
            })
    except Exception as exc:
        st.error(f"HITL error: {exc}")
    st.rerun()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    init_session()
    render_sidebar()

    # Show welcome or chat history
    if not st.session_state.display_messages:
        render_welcome()
    else:
        render_messages()

    # HITL confirmation dialog sits above the input
    if st.session_state.pending_hitl:
        render_hitl_dialog(st.session_state.pending_hitl)

    # Chat input (always visible at bottom)
    if prompt := st.chat_input("Ask a question about the data…"):
        st.session_state.pending_question = prompt
        st.rerun()

    # Process pending question (from input box or welcome buttons)
    if st.session_state.pending_question:
        q = st.session_state.pending_question
        st.session_state.pending_question = None

        # Append user message immediately so it shows
        st.session_state.display_messages.append({
            "role": "user",
            "content": q,
        })

        with st.spinner("Thinking…"):
            try:
                data = api_chat(q)
                st.session_state.display_messages.append({
                    "role": "assistant",
                    "content": data.get("response", "_(no response)_"),
                    "sql": data.get("sql"),
                    "result_str": data.get("result_str"),
                })
                if data.get("hitl_payload"):
                    st.session_state.pending_hitl = data["hitl_payload"]
            except Exception as exc:
                st.error(f"⚠️ Backend error: {exc}")

        st.rerun()


main()