"""
market_suite.py  —  Unified Indian Market Announcements Dashboard
═══════════════════════════════════════════════════════════════════════
(Formerly market_web_report.py, now with the Idea Board embedded under
the BSE Equity tab — see idea_board_streamlit_02.py, merged in below.)

Four source databases, one app:

  • BSE Equity   →  bse_equity.db   (table: announcements; also the source
                     for the embedded Idea Board — idea_groups / idea_types /
                     announcement_idea_scores, written by
                     announcement_ideas_pipeline.py)
  • BSE SME      →  bse_sme.db      (tables: announcements, corp_actions)
  • NSE Equity   →  nse_equity.db   (table: announcements)
  • NSE SME      →  nse_sme.db      (table: announcements)

Pages (sidebar):
  1. Announcements  — search / filter per-DB, clickable PDF links
                       · BSE Equity tab has two sub-tabs:
                         "Announcements" (as before) and "Idea Board"
                         (category-scored announcement ideas)
  2. Charts         — daily volume, category breakdown, timeline per DB
  3. Insights       — keyword freq · trigger flags · clusters · AI digest
  4. My Activity    — per-user view & search history

Install (one-time):
    pip install streamlit pandas plotly anthropic streamlit-option-menu streamlit-authenticator

Run:
    streamlit run market_suite.py

Cron / unattended:
    The app reads the DBs read-only; run market_announcements.py and
    announcement_ideas_pipeline.py separately to populate them.
"""

# ─── IMPORTS ─────────────────────────────────────────────────────────────────

import json
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path


import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    from streamlit_option_menu import option_menu
except ImportError:
    st.error("Run:  pip install streamlit-option-menu")
    st.stop()

try:
    import streamlit_authenticator as stauth
except ImportError:
    st.error("Run:  pip install streamlit-authenticator")
    st.stop()

try:
    import anthropic
    _ANTHROPIC_OK = True
except ImportError:
    _ANTHROPIC_OK = False

# ─── GUIDED ACTIVITY deps — scraper + idea-scoring taxonomy ──────────────────
# market_announcements.py  → does the actual BSE/NSE scraping (fetch_bse_equity,
#                             fetch_bse_sme, fetch_nse). Importing it (rather than
#                             shelling out) lets "Guided Activity" call it in-process.
# idea_rules.py             → single source of truth for the idea taxonomy
#                             (GROUPS / IDEA_TYPES / weights) used to score BSE
#                             Equity rows. Both files must sit next to this one.
try:
    import market_announcements as ma
    _SCRAPER_OK, _SCRAPER_ERR = True, ""
except Exception as e:
    _SCRAPER_OK, _SCRAPER_ERR = False, str(e)

try:
    import idea_rules
    _IDEA_RULES_OK, _IDEA_RULES_ERR = True, ""
except Exception as e:
    _IDEA_RULES_OK, _IDEA_RULES_ERR = False, str(e)

# announcement_ideas_pipeline.py → same taxonomy engine, but also owns
# ensure_source_view(), which normalizes each source DB's native schema
# (BSE Equity / BSE SME / NSE Equity / NSE SME all differ) onto one common
# shape so the Idea Board + Tracker below can be a single set of functions
# reused across all four sources.
try:
    import announcement_ideas_pipeline as idea_pipeline
    _IDEA_PIPELINE_OK, _IDEA_PIPELINE_ERR = True, ""
except Exception as e:
    _IDEA_PIPELINE_OK, _IDEA_PIPELINE_ERR = False, str(e)

IDEA_SOURCE_VIEW = "v_idea_source"  # matches announcement_ideas_pipeline.SOURCE_VIEW

# ─── DB PATHS  (edit if your files live elsewhere) ───────────────────────────

DB_PATHS = {
    "BSE Equity": "bse_equity.db",
    "BSE SME":    "bse_sme.db",
    "NSE Equity": "nse_equity.db",
    "NSE SME":    "nse_sme.db",
}
BSE_ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
MAX_AI_ROWS     = 50

# ─── TRIGGER TAXONOMY (shared across all sources) ────────────────────────────

TRIGGER_TAXONOMY = {
    "Acquisition / Merger":  ["acquisition","merger","amalgamation","takeover","scheme of arrangement","business transfer"],
    "Buyback":               ["buyback","buy-back","share repurchase","capital reduction"],
    "Dividend":              ["dividend","interim dividend","final dividend","special dividend"],
    "Fundraise / Capital":   ["rights issue","preferential allotment","qip","ncd","fpo","ipo","private placement","debenture"],
    "Board / Management":    ["resignation","appointment","ceo","managing director","key managerial","change in director","new appointment"],
    "Financial Results":     ["financial results","quarterly results","unaudited","audited","q1 ","q2 ","q3 ","q4 ","fy2","half year"],
    "Regulatory / Legal":    ["sebi","nclt","nclat","court order","penalty","show cause","adjudication","regulatory"],
    "Pledge / Encumbrance":  ["pledge","pledged","encumbrance","invocation","release of pledge"],
    "Insider / UPSI":        ["upsi","insider trading","trading window","price sensitive"],
    "Credit Rating":         ["rating","upgrade","downgrade","reaffirm","crisil","icra","care rating","india ratings"],
    "Capex / Expansion":     ["capex","expansion","capacity","new plant","greenfield","brownfield","capital expenditure"],
    "Subsidiary / JV":       ["subsidiary","joint venture","associate","stake","divestment","step-down subsidiary"],
}
TRIGGER_COLORS = {
    "Acquisition / Merger": "#e63946","Buyback": "#f4a261","Dividend": "#2a9d8f",
    "Fundraise / Capital":  "#457b9d","Board / Management": "#6d6875","Financial Results": "#264653",
    "Regulatory / Legal":   "#e9c46a","Pledge / Encumbrance": "#f77f00","Insider / UPSI": "#d62828",
    "Credit Rating":        "#4cc9f0","Capex / Expansion": "#80b918","Subsidiary / JV": "#b5838d",
}
STOP_WORDS = {
    "the","a","an","and","or","of","in","to","for","is","are","has","have","had","was","were",
    "be","been","being","with","on","at","by","from","as","that","this","its","it","we","our",
    "their","pursuant","under","sub","reg","sebi","bse","nse","ltd","limited","pvt","inc","per",
    "re","no","not","will","shall","herewith","enclosed","submission","submitted","intimation",
    "informed","please","find","attached","copy","regarding","ref","information","disclosure",
    "regulation","act","exchange","company","companies",
}

# ─── DESIGN TOKENS ───────────────────────────────────────────────────────────

INK       = "#0f1923"; INK_SOFT = "#5b6878"; INK_MUTED = "#8a96a3"
LINE      = "#e3e8ee"; LINE_SOFT = "#edf1f5"
SURFACE   = "#ffffff"; SURFACE_1 = "#f6f8fa"; SURFACE_2 = "#eef2f6"
ACCENT    = "#1d5fb0"; ACCENT_DK = "#154a8c"; ACCENT_BG = "#eaf1fb"
SUCCESS   = "#1d8a5e"; WARNING = "#b9740a"; DANGER = "#c0392b"
SHADOW    = "0 1px 2px rgba(15,25,35,0.04),0 2px 8px rgba(15,25,35,0.04)"
SHADOW_MD = "0 2px 4px rgba(15,25,35,0.05),0 6px 16px rgba(15,25,35,0.07)"

SOURCE_COLORS = {
    "BSE Equity": "#1d5fb0",
    "BSE SME":    "#e9c46a",
    "NSE Equity": "#2a9d8f",
    "NSE SME":    "#e63946",
}

# ─── PAGE CONFIG ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Market Announcements Dashboard",
    page_icon="📊",
    layout="wide",
)

# ─── SHARED CSS ──────────────────────────────────────────────────────────────

st.markdown(f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600&display=swap');
:root{{--ink:{INK};--ink-soft:{INK_SOFT};--ink-muted:{INK_MUTED};--line:{LINE};--surface:{SURFACE};--surface-1:{SURFACE_1};--accent:{ACCENT};--accent-bg:{ACCENT_BG};}}
html,body,[class*="css"]{{font-family:'IBM Plex Sans',sans-serif;color:{INK};-webkit-font-smoothing:antialiased;}}
.stApp{{background:{SURFACE};}}
.block-container{{padding-top:1.25rem;padding-bottom:3rem;max-width:1400px;}}
#MainMenu,header[data-testid="stHeader"]{{background:transparent;}}
footer{{visibility:hidden;}}
::-webkit-scrollbar{{width:9px;height:9px;}}
::-webkit-scrollbar-track{{background:transparent;}}
::-webkit-scrollbar-thumb{{background:{LINE};border-radius:8px;}}
::-webkit-scrollbar-thumb:hover{{background:{INK_MUTED};}}
section[data-testid="stSidebar"]{{width:240px!important;min-width:240px!important;background:{SURFACE_2};border-right:1px solid {LINE};}}
section[data-testid="stSidebar"]>div{{padding:1.1rem 0.9rem;}}
.page-head{{margin-bottom:1.1rem;}}
.page-head h1{{font-size:1.35rem;font-weight:600;color:{INK};margin:0;display:flex;align-items:center;gap:8px;letter-spacing:-0.01em;}}
.page-head p{{font-size:0.85rem;color:{INK_MUTED};margin:3px 0 0;}}
.filter-bar{{background:{SURFACE};border:1px solid {LINE};border-radius:12px;padding:0.85rem 1rem 0.55rem;margin-bottom:1.1rem;box-shadow:{SHADOW};}}
.filter-bar-label{{font-size:0.68rem;font-weight:600;color:{INK_MUTED};text-transform:uppercase;letter-spacing:0.08em;margin-bottom:0.55rem;display:flex;align-items:center;gap:6px;}}
.filter-bar-label::before{{content:'';width:3px;height:11px;background:{ACCENT};border-radius:2px;}}
.field-spacer{{height:1.55rem;}}
.metric-card{{background:{SURFACE_1};border:1px solid {LINE};border-radius:10px;padding:0.9rem 1.05rem;text-align:left;transition:border-color 0.15s ease,box-shadow 0.15s ease;}}
.metric-card:hover{{border-color:{INK_MUTED};box-shadow:{SHADOW};}}
.metric-card .val{{font-size:1.55rem;font-weight:600;color:{INK};font-family:'IBM Plex Mono',monospace;line-height:1.1;letter-spacing:-0.01em;}}
.metric-card .lbl{{font-size:0.7rem;color:{INK_MUTED};text-transform:uppercase;letter-spacing:0.07em;margin-top:5px;font-weight:500;}}
.source-badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:0.7rem;font-weight:600;letter-spacing:0.04em;margin-bottom:0.5rem;}}
.stTabs [data-baseweb="tab-list"]{{gap:4px;border-bottom:1px solid {LINE};}}
.stTabs [data-baseweb="tab"]{{font-family:'IBM Plex Sans',sans-serif;font-size:0.82rem;font-weight:500;letter-spacing:0.01em;color:{INK_SOFT};padding:0.55rem 1rem;transition:color 0.15s ease;}}
.stTabs [data-baseweb="tab"]:hover{{color:{ACCENT};}}
.stTabs [aria-selected="true"]{{color:{ACCENT}!important;font-weight:600;}}
.stTabs [data-baseweb="tab-highlight"]{{background-color:{ACCENT}!important;height:2.5px;}}

/* ── Form controls: one consistent, compact height + focus language ──────── */
div[data-testid="stTextInput"] input,
div[data-testid="stDateInput"] input{{
  font-size:0.82rem;height:2.35rem;padding:0 0.65rem;border-radius:8px!important;
  border:1px solid {LINE}!important;background:{SURFACE_1};
  transition:border-color 0.14s ease,box-shadow 0.14s ease,background 0.14s ease;
}}
div[data-testid="stTextInput"] input:focus,
div[data-testid="stDateInput"] input:focus{{
  border-color:{ACCENT}!important;background:{SURFACE};box-shadow:0 0 0 3px {ACCENT_BG}!important;
}}
div[data-testid="stDateInput"] div[data-baseweb="base-input"]{{border-radius:8px!important;}}
div[data-testid="stDateInput"] svg{{width:14px;height:14px;color:{INK_MUTED};}}
div[data-testid="stSelectbox"] div[data-baseweb="select"]>div,
div[data-testid="stMultiSelect"] div[data-baseweb="select"]>div{{
  min-height:2.35rem;font-size:0.82rem;border-radius:8px!important;
  border:1px solid {LINE}!important;background:{SURFACE_1};
  transition:border-color 0.14s ease,box-shadow 0.14s ease;
}}
div[data-testid="stSelectbox"] div[data-baseweb="select"]>div:hover,
div[data-testid="stMultiSelect"] div[data-baseweb="select"]>div:hover{{border-color:{INK_MUTED};}}
div[data-testid="stSelectbox"] div[data-baseweb="select"]:focus-within>div,
div[data-testid="stMultiSelect"] div[data-baseweb="select"]:focus-within>div{{
  border-color:{ACCENT}!important;box-shadow:0 0 0 3px {ACCENT_BG};
}}
.stMultiSelect span[data-baseweb="tag"]{{background:{ACCENT_BG}!important;color:{ACCENT_DK}!important;border-radius:6px!important;font-size:0.76rem!important;}}
label[data-testid="stWidgetLabel"]{{margin-bottom:0.3rem;}}
label[data-testid="stWidgetLabel"] p{{font-size:0.76rem;color:{INK_SOFT};font-weight:500;letter-spacing:0.01em;}}

/* ── Buttons: match input height so rows line up ─────────────────────────── */
.stButton button,.stDownloadButton button{{
  border-radius:8px;border:1px solid {LINE};font-weight:500;font-size:0.82rem;
  color:{INK_SOFT};background:{SURFACE};height:2.35rem;padding:0 0.9rem;
  transition:all 0.14s ease;box-shadow:none;
}}
.stButton button:hover,.stDownloadButton button:hover{{border-color:{ACCENT};color:{ACCENT};background:{ACCENT_BG};}}
.stButton button[kind="primary"],.stButton button[data-testid="baseButton-primary"]{{background:{ACCENT};border-color:{ACCENT};color:#fff;}}
.stButton button[kind="primary"]:hover{{background:{ACCENT_DK};border-color:{ACCENT_DK};color:#fff;}}
div[data-testid="stPopover"] button{{
  border-radius:8px;border:1px solid {LINE};font-size:0.78rem;font-weight:500;
  color:{INK_SOFT};background:{SURFACE};padding:0.3rem 0.7rem;
  transition:all 0.14s ease;box-shadow:none;
}}
div[data-testid="stPopover"] button:hover{{border-color:{ACCENT};color:{ACCENT};background:{ACCENT_BG};}}

div[data-testid="stDataFrame"],div[data-testid="stTable"]{{border:1px solid {LINE};border-radius:10px;overflow:hidden;box-shadow:{SHADOW};}}
div[data-testid="stDataFrame"] [role="columnheader"]{{background:{SURFACE_1}!important;color:{INK_SOFT}!important;font-weight:600!important;font-size:0.78rem!important;text-transform:uppercase;letter-spacing:0.04em;}}
div[data-testid="stDataFrame"] [role="row"]:hover{{background:{ACCENT_BG}!important;}}
div[data-testid="stAlert"]{{border-radius:10px;border:1px solid {LINE};font-size:0.85rem;}}
div[data-testid="stMetricValue"]{{font-family:'IBM Plex Mono',monospace;color:{INK};font-weight:600;}}
div[data-testid="stMetricLabel"]{{color:{INK_MUTED};font-size:0.75rem;text-transform:uppercase;letter-spacing:0.05em;}}
hr{{border-color:{LINE}!important;margin:1.2rem 0;}}
.stCaption,[data-testid="stCaptionContainer"]{{color:{INK_MUTED}!important;}}
a{{color:{ACCENT};text-decoration:none;}}
a:hover{{text-decoration:underline;}}
.main .block-container{{animation:fadeIn 0.25s ease;}}
@keyframes fadeIn{{from{{opacity:0.4;}}to{{opacity:1;}}}}
</style>
""", unsafe_allow_html=True)

# ─── IDEA BOARD CSS (embedded from idea_board_streamlit_02.py, namespaced) ───

st.markdown("""
<style>
.idb-card{background:var(--background-color,#fff);border:1px solid rgba(120,120,120,0.25);
  border-radius:8px;padding:14px;margin-bottom:12px;}
.idb-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;}
.idb-company{font-weight:600;font-size:14px;}
.idb-symbol{font-family:monospace;font-size:11px;opacity:0.65;}
.idb-subject{font-size:13px;opacity:0.85;margin:6px 0;line-height:1.5;}
.idb-score{font-family:monospace;font-weight:700;font-size:13px;padding:2px 8px;border-radius:10px;}
.idb-score.high{background:#E1F5EE;color:#085041;}
.idb-score.mid{background:#FAEEDA;color:#633806;}
.idb-score.low{background:#FAECE7;color:#712B13;}
.idb-kw{display:inline-block;font-family:monospace;font-size:10.5px;background:#E1F5EE;
  color:#085041;padding:2px 6px;border-radius:3px;margin:2px 4px 0 0;}
.idb-kw.cat{background:#FAEEDA;color:#633806;}
.idb-ts{font-family:monospace;font-size:10.5px;opacity:0.55;margin-top:8px;}
</style>
""", unsafe_allow_html=True)

# ═════════════════════════════════════════════════════════════════════════════
#  AUTH  — SQLite-backed user management
#  DB file : market_users.db  (auto-created on first run)
#
#  ┌─────────────────────────────────────────┐
#  │  DEFAULT CREDENTIALS (first run only)   │
#  │  Username : admin                        │
#  │  Password : admin@123                    │
#  │  Role     : admin                        │
#  └─────────────────────────────────────────┘
#  Use the "Create User" tab (admin only) to add more users.
#  Use the "Reset Password" tab to change any password.
# ═════════════════════════════════════════════════════════════════════════════

import hashlib
AUTH_DB = "market_users.db"

def _hash_pwd(pwd: str) -> str:
    return hashlib.sha256(pwd.strip().encode()).hexdigest()

def _auth_conn():
    c = sqlite3.connect(AUTH_DB)
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            username     TEXT    UNIQUE NOT NULL COLLATE NOCASE,
            full_name    TEXT    NOT NULL,
            pwd_hash     TEXT    NOT NULL,
            role         TEXT    NOT NULL DEFAULT 'user',
            created_at   TEXT    DEFAULT (datetime('now','localtime')),
            last_login   TEXT
        )
    """)
    c.commit()
    # Always ensure the default admin exists (idempotent)
    existing = c.execute("SELECT COUNT(*) FROM users WHERE username='admin' COLLATE NOCASE").fetchone()[0]
    if existing == 0:
        c.execute(
            "INSERT OR IGNORE INTO users (username, full_name, pwd_hash, role) VALUES (?,?,?,?)",
            ("admin", "Administrator", _hash_pwd("admin@123"), "admin"),
        )
        c.commit()
    return c

def _verify(username: str, password: str):
    """Return (full_name, role) on success, else None."""
    c = _auth_conn()
    row = c.execute(
        "SELECT full_name, pwd_hash, role FROM users WHERE username=? COLLATE NOCASE",
        (username.strip(),)
    ).fetchone()
    if row and row["pwd_hash"] == _hash_pwd(password):
        c.execute(
            "UPDATE users SET last_login=datetime('now','localtime') WHERE username=? COLLATE NOCASE",
            (username.strip(),)
        )
        c.commit()
        c.close()
        return row["full_name"], row["role"]
    c.close()
    return None

def _create_user(username, full_name, password, role="user"):
    c = _auth_conn()
    try:
        c.execute(
            "INSERT INTO users (username, full_name, pwd_hash, role) VALUES (?,?,?,?)",
            (username.strip().lower(), full_name.strip(), _hash_pwd(password), role),
        )
        c.commit(); c.close()
        return True, ""
    except sqlite3.IntegrityError:
        c.close()
        return False, f"Username '{username}' already exists."

def _reset_password(username, new_password):
    c = _auth_conn()
    n = c.execute(
        "UPDATE users SET pwd_hash=? WHERE username=? COLLATE NOCASE",
        (_hash_pwd(new_password), username.strip())
    ).rowcount
    c.commit(); c.close()
    return n > 0

def _list_users():
    c = _auth_conn()
    rows = c.execute(
        "SELECT username, full_name, role, created_at, last_login FROM users ORDER BY username"
    ).fetchall()
    c.close()
    return [dict(r) for r in rows]

def _delete_user(username):
    c = _auth_conn()
    c.execute("DELETE FROM users WHERE username=? COLLATE NOCASE", (username.strip(),))
    c.commit(); c.close()


def _do_login():
    if st.session_state.get("auth_user"):
        return True

    st.markdown(f"""
    <div class="page-head" style="text-align:center;margin-top:2.5rem;">
      <h1 style="justify-content:center;font-size:1.6rem;">📊 Market Announcements Dashboard</h1>
      <p style="font-size:0.9rem;">BSE Equity · BSE SME · NSE Equity · NSE SME</p>
    </div>""", unsafe_allow_html=True)

    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        tab_login, tab_reset, tab_create = st.tabs(["🔑 Log in", "🔒 Reset Password", "➕ Create User"])

        # ── LOGIN TAB ──────────────────────────────────────────────────────
        with tab_login:
            st.markdown("<br>", unsafe_allow_html=True)
            with st.form("login_form", clear_on_submit=False):
                uname     = st.text_input("Username", placeholder="Enter your username")
                pwd       = st.text_input("Password", type="password", placeholder="Enter your password")
                submitted = st.form_submit_button("🔑  Log in", use_container_width=True, type="primary")

            if submitted:
                if not uname or not pwd:
                    st.error("Please enter both username and password.")
                else:
                    result = _verify(uname, pwd)
                    if result:
                        full_name, role = result
                        st.session_state["auth_user"] = uname.strip().lower()
                        st.session_state["auth_name"] = full_name
                        st.session_state["auth_role"] = role
                        st.rerun()
                    else:
                        st.error("❌ Incorrect username or password. Please try again.")

            st.markdown(f"""
            <div style="margin-top:1.2rem;padding:0.85rem 1rem;background:{ACCENT_BG};
                 border:1px solid #bdd3f0;border-radius:10px;font-size:0.8rem;color:{INK_SOFT};">
              <div style="font-weight:600;color:{ACCENT_DK};margin-bottom:4px;">🔑 Default login credentials</div>
              Username &nbsp;→&nbsp; <code style="background:#fff;padding:1px 6px;border-radius:4px;">admin</code><br>
              Password &nbsp;→&nbsp; <code style="background:#fff;padding:1px 6px;border-radius:4px;">admin@123</code><br>
              <div style="margin-top:6px;color:{WARNING};font-size:0.75rem;">
                ⚠️ Please reset your password after first login.
              </div>
            </div>""", unsafe_allow_html=True)

        # ── RESET PASSWORD TAB ─────────────────────────────────────────────
        with tab_reset:
            st.markdown("<br>", unsafe_allow_html=True)
            st.caption("Verify with your current password, then set a new one.")
            with st.form("reset_form", clear_on_submit=True):
                r_user    = st.text_input("Username")
                r_cur_pwd = st.text_input("Current password", type="password")
                r_new_pwd = st.text_input("New password", type="password",
                                          help="At least 6 characters")
                r_confirm = st.text_input("Confirm new password", type="password")
                r_submit  = st.form_submit_button("🔒  Reset password", use_container_width=True, type="primary")

            if r_submit:
                if not all([r_user, r_cur_pwd, r_new_pwd, r_confirm]):
                    st.error("All four fields are required.")
                elif r_new_pwd != r_confirm:
                    st.error("New passwords do not match.")
                elif len(r_new_pwd) < 6:
                    st.error("New password must be at least 6 characters.")
                elif not _verify(r_user, r_cur_pwd):
                    st.error("❌ Current username or password is incorrect.")
                else:
                    _reset_password(r_user, r_new_pwd)
                    st.success("✅ Password reset successfully. Go to Log in tab to continue.")

        # ── CREATE USER TAB (admin PIN protected) ─────────────────────────
        with tab_create:
            st.markdown("<br>", unsafe_allow_html=True)
            st.caption("Create a new user account. Requires admin username + password to authorise.")
            with st.form("create_form", clear_on_submit=True):
                admin_uname   = st.text_input("Admin username",  placeholder="Your admin username")
                admin_pwd     = st.text_input("Admin password",  type="password", placeholder="Your admin password")
                st.markdown("---")
                new_uname     = st.text_input("New username",    placeholder="e.g. john_doe")
                new_fullname  = st.text_input("Full name",       placeholder="e.g. John Doe")
                new_pwd       = st.text_input("New password",    type="password", help="At least 6 characters")
                new_pwd_conf  = st.text_input("Confirm password", type="password")
                new_role      = st.selectbox("Role", ["user", "admin"])
                c_submit      = st.form_submit_button("➕  Create user", use_container_width=True, type="primary")

            if c_submit:
                admin_res = _verify(admin_uname, admin_pwd)
                if not admin_res or admin_res[1] != "admin":
                    st.error("❌ Admin credentials are incorrect or insufficient.")
                elif not all([new_uname, new_fullname, new_pwd, new_pwd_conf]):
                    st.error("All new-user fields are required.")
                elif new_pwd != new_pwd_conf:
                    st.error("New passwords do not match.")
                elif len(new_pwd) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    ok, msg = _create_user(new_uname, new_fullname, new_pwd, new_role)
                    if ok:
                        st.success(f"✅ User **{new_uname}** created successfully. They can now log in.")
                    else:
                        st.error(f"❌ {msg}")

    return False

if not _do_login():
    st.stop()

current_user = st.session_state["auth_user"]
current_name = st.session_state.get("auth_name", current_user)

# ─── VIEW / SEARCH HISTORY (session-level, per user) ─────────────────────────

if "view_history" not in st.session_state:
    st.session_state["view_history"] = []
if "search_history" not in st.session_state:
    st.session_state["search_history"] = []

def _log_view(source, rec_dict):
    import datetime
    st.session_state["view_history"].append({
        "source": source,
        "logged_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        **rec_dict,
    })

def _log_search(source, filters, result_count):
    import datetime
    st.session_state["search_history"].append({
        "source": source,
        "filters": filters,
        "result_count": result_count,
        "searched_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })

# ═════════════════════════════════════════════════════════════════════════════
#  SIDEBAR
# ═════════════════════════════════════════════════════════════════════════════

with st.sidebar:
    st.markdown(f"""
    <div style="background:{SURFACE};border:1px solid {LINE};border-radius:10px;
         padding:0.6rem 0.75rem;margin-bottom:0.7rem;display:flex;align-items:center;gap:8px;">
      <div style="width:30px;height:30px;border-radius:50%;background:{ACCENT_BG};
           color:{ACCENT_DK};display:flex;align-items:center;justify-content:center;
           font-weight:600;font-size:0.78rem;flex-shrink:0;">
        {(current_name or current_user or "?")[:1].upper()}
      </div>
      <div style="overflow:hidden;">
        <div style="font-size:0.82rem;font-weight:600;color:{INK};white-space:nowrap;text-overflow:ellipsis;overflow:hidden;">
          {current_name or current_user}
        </div>
        <div style="font-size:0.68rem;color:{INK_MUTED};">@{current_user}</div>
      </div>
    </div>""", unsafe_allow_html=True)

    page = option_menu(
        menu_title="Dashboard",
        menu_icon="display",
        options=["Announcements", "Announcement Tracker", "Charts", "Insights", "Calculators", "My Activity", "Guided Activity", "Guided DB Clean-up"],
        icons=["file-earmark-text", "folder-symlink", "bar-chart-line", "lightbulb", "calculator", "clock-history", "compass", "trash3"],
        default_index=0,
        styles={
            "container":      {"padding":"0.9rem 0.8rem","background-color":SURFACE,"border-radius":"14px","box-shadow":SHADOW_MD},
            "menu-title":     {"font-size":"1.1rem","font-weight":"600","color":INK,"padding":"0 0 0.7rem 0.2rem"},
            "icon":           {"font-size":"0.95rem","color":INK_MUTED},
            "nav-link":       {"font-size":"0.88rem","font-weight":"500","color":INK_SOFT,"text-align":"left","margin":"2px 0","padding":"0.6rem 0.7rem","border-radius":"8px"},
            "nav-link-selected": {"background-color":ACCENT_BG,"color":ACCENT_DK,"font-weight":"600"},
        },
    )

    st.markdown("<div style='height:0.6rem'></div>", unsafe_allow_html=True)
    if st.button("Log out", use_container_width=True):
        st.session_state["auth_user"] = None
        st.rerun()

# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS — DB ACCESS
# ═════════════════════════════════════════════════════════════════════════════

def _conn(source: str) -> sqlite3.Connection:
    path = DB_PATHS[source]
    if not Path(path).exists():
        return None
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c

def _df(source: str, sql: str, params=()) -> pd.DataFrame:
    c = _conn(source)
    if c is None:
        return pd.DataFrame()
    try:
        df = pd.read_sql_query(sql, c, params=params)
    except Exception:
        df = pd.DataFrame()
    finally:
        c.close()
    return df

# ─── Per-source normalised loaders ───────────────────────────────────────────

def load_bse_equity(from_dt, to_dt, symbol="", category="", subcategory="") -> pd.DataFrame:
    clauses, p = ["DATE(input_timestamp) BETWEEN ? AND ?"], [str(from_dt), str(to_dt)]
    if symbol:      clauses.append("(symbol LIKE ? OR company_name LIKE ?)"); p += [f"%{symbol}%",f"%{symbol}%"]
    if category:    clauses.append("category = ?");    p.append(category)
    if subcategory: clauses.append("subcategory = ?"); p.append(subcategory)
    sql = f"""
        SELECT id, scrip_code, symbol, company_name, category, subcategory,
               subject, file_name, input_timestamp
        FROM   v_announcements
        WHERE  {' AND '.join(clauses)}
        ORDER  BY input_timestamp DESC
    """
    df = _df("BSE Equity", sql, p)
    if not df.empty and "file_name" in df.columns:
        df["document_url"] = df["file_name"].apply(
            lambda fn: f"{BSE_ATTACH_BASE}{fn}" if fn else "")
    return df


def load_bse_sme_ann(from_dt, to_dt, scrip="", category="", grp="") -> pd.DataFrame:
    clauses, p = ["announce_date BETWEEN ? AND ?"], [str(from_dt), str(to_dt)]
    if scrip:    clauses.append("LOWER(scrip_name) LIKE ?"); p.append(f"%{scrip.lower()}%")
    if category: clauses.append("LOWER(category) LIKE ?");  p.append(f"%{category.lower()}%")
    if grp:      clauses.append("grp = ?");                 p.append(grp.upper())
    sql = f"""
        SELECT id, scrip_code, scrip_name, grp, category, announce_date,
               end_date, purpose, attachment_url
        FROM   announcements
        WHERE  {' AND '.join(clauses)}
        ORDER  BY announce_date DESC
    """
    return _df("BSE SME", sql, p)


def load_bse_sme_corp(from_dt, to_dt, scrip="", category="") -> pd.DataFrame:
    clauses, p = ["ex_date BETWEEN ? AND ?"], [str(from_dt), str(to_dt)]
    if scrip:    clauses.append("LOWER(scrip_name) LIKE ?"); p.append(f"%{scrip.lower()}%")
    if category: clauses.append("LOWER(category) LIKE ?");   p.append(f"%{category.lower()}%")
    sql = f"""
        SELECT scrip_code, scrip_name, grp, category,
               ex_date, record_date, end_date, purpose
        FROM   corp_actions
        WHERE  {' AND '.join(clauses)}
        ORDER  BY ex_date DESC
    """
    return _df("BSE SME", sql, p)


def load_nse(source: str, from_dt, to_dt, symbol="", subject="") -> pd.DataFrame:
    """Works for both nse_equity and nse_sme (same schema)."""
    # ann_date stored as "28-Jun-2026 23:26:37" — convert to ISO in SQL
    clauses = ["""
        (CASE
           WHEN length(ann_date) >= 11 THEN
             substr(ann_date,8,4)||'-'||
             CASE substr(ann_date,4,3)
               WHEN 'Jan' THEN '01' WHEN 'Feb' THEN '02' WHEN 'Mar' THEN '03'
               WHEN 'Apr' THEN '04' WHEN 'May' THEN '05' WHEN 'Jun' THEN '06'
               WHEN 'Jul' THEN '07' WHEN 'Aug' THEN '08' WHEN 'Sep' THEN '09'
               WHEN 'Oct' THEN '10' WHEN 'Nov' THEN '11' WHEN 'Dec' THEN '12'
               ELSE '00' END||'-'||
             substr(ann_date,1,2)
           ELSE ann_date
         END)
         BETWEEN ? AND ?
    """]
    p = [str(from_dt), str(to_dt)]
    if symbol:  clauses.append("(LOWER(symbol) LIKE ? OR LOWER(company_name) LIKE ?)"); p += [f"%{symbol.lower()}%",f"%{symbol.lower()}%"]
    if subject: clauses.append("LOWER(subject) LIKE ?"); p.append(f"%{subject.lower()}%")
    sql = f"""
        SELECT id, symbol, company_name, subject, description, ann_date, attachment_url
        FROM   announcements
        WHERE  {' AND '.join(clauses)}
        ORDER  BY ann_date DESC
    """
    return _df(source, sql, p)


# ─── Trigger / NLP helpers ───────────────────────────────────────────────────

def tokenize(text):
    if not text: return []
    return [w for w in re.findall(r"[a-z]{3,}", text.lower()) if w not in STOP_WORDS]

def bigrams(toks):
    return [f"{toks[i]} {toks[i+1]}" for i in range(len(toks)-1)]

def top_terms(series, n=30, include_bg=True):
    cnt = Counter()
    for t in series.dropna():
        toks = tokenize(t)
        cnt.update(toks)
        if include_bg: cnt.update(bigrams(toks))
    return cnt.most_common(n)

def flag_triggers(desc):
    if not desc: return []
    lo = desc.lower()
    return [lbl for lbl, terms in TRIGGER_TAXONOMY.items() if any(t in lo for t in terms)]

def assign_cluster(desc):
    flags = flag_triggers(desc)
    return flags[0] if flags else "General / Other"

def ai_digest(rows_text: str) -> str:
    if not _ANTHROPIC_OK:
        return "⚠️  anthropic package not installed. Run: pip install anthropic"
    client = anthropic.Anthropic()
    prompt = f"""You are an Indian equity market analyst. Below is a batch of corporate announcements.

Produce a structured digest:
1. **Executive Summary** (3–4 sentences): dominant themes.
2. **Key Corporate Actions** (bullets): M&A, fundraises, buybacks, capex.
3. **Results Season Signals**: companies announcing results, tone cues.
4. **Regulatory / Risk Flags**: SEBI, NCLT, pledging, insider trading.
5. **Analyst Watchlist** (top 3–5 companies, one-line rationale each).

Be concise, precise, investment-relevant. No filler.

Announcements:
{rows_text}"""
    msg = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text

def wildcard_like(pattern):
    if not pattern: return None
    esc = pattern.replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
    if "*" in pattern or "?" in pattern:
        return esc.replace("*","%").replace("?","_")
    return f"%{esc}%"

def _metric(col, val, lbl):
    col.markdown(f"""<div class="metric-card"><div class="val">{val}</div><div class="lbl">{lbl}</div></div>""",
                 unsafe_allow_html=True)

def _source_badge(source):
    col = SOURCE_COLORS.get(source, ACCENT)
    st.markdown(
        f'<span class="source-badge" style="background:{col}20;color:{col};border:1px solid {col}40;">'
        f'{source}</span>', unsafe_allow_html=True)

def _plotly_defaults(fig, height=380):
    fig.update_layout(
        font_family="IBM Plex Sans", plot_bgcolor="#f7f9fb", paper_bgcolor="white",
        margin=dict(l=10, r=10, t=30, b=40), height=height,
        xaxis=dict(gridcolor=LINE, linecolor=LINE),
        yaxis=dict(gridcolor=LINE, linecolor=LINE),
    )
    return fig


# ─── PER-TABLE SETTINGS (⚙️ gear popover) ────────────────────────────────────
# Gives every report list its own column-visibility / sort / row-height /
# density controls, persisted per-user in session_state so choices survive
# reruns (filter changes, tab switches, etc).

def _report_settings(key: str, columns: list, default_cols: list = None,
                      default_height: int = 500, label: str = None):
    """
    Renders a compact '⚙️ Settings' popover for a report table and returns
    the user's chosen (visible_columns, height, sort_col, sort_asc, compact).

    key             unique per-table session key, e.g. "be_table"
    columns         full list of displayable column names (already renamed
                    to their display labels)
    default_cols    columns shown by default (defaults to all)
    default_height  default table height in px
    label           optional caption shown to the left of the gear button
    """
    ss_key = f"_settings::{key}"
    defaults = {
        "columns": default_cols if default_cols is not None else list(columns),
        "height": default_height,
        "sort_col": "(none)",
        "sort_asc": False,
    }
    if ss_key not in st.session_state:
        st.session_state[ss_key] = dict(defaults)
    saved = st.session_state[ss_key]
    # Guard against stale columns (e.g. filters changed which fields exist)
    saved["columns"] = [c for c in saved["columns"] if c in columns] or list(columns)
    if saved["sort_col"] not in (["(none)"] + columns):
        saved["sort_col"] = "(none)"

    hdr_l, hdr_r = st.columns([0.82, 0.18])
    with hdr_l:
        if label:
            st.caption(label)
    with hdr_r:
        with st.popover("⚙️ Settings", use_container_width=True):
            st.markdown("**Table settings**")
            sel_cols = st.multiselect(
                "Visible columns", columns, default=saved["columns"],
                key=f"{key}__cols",
                help="Choose which columns appear in this table.",
            )
            sort_col = st.selectbox(
                "Sort by", ["(none)"] + columns,
                index=(["(none)"] + columns).index(saved["sort_col"]),
                key=f"{key}__sortcol",
            )
            sort_asc = st.checkbox("Ascending", value=saved["sort_asc"], key=f"{key}__sortasc")
            height = st.slider(
                "Table height (px)", 250, 900, saved["height"], step=50, key=f"{key}__height",
            )
            if st.button("↺ Reset to default", key=f"{key}__reset", use_container_width=True):
                st.session_state[ss_key] = dict(defaults)
                st.rerun()

    new_settings = {
        "columns": sel_cols or list(columns),
        "height": height,
        "sort_col": sort_col,
        "sort_asc": sort_asc,
    }
    st.session_state[ss_key] = new_settings
    return (new_settings["columns"], new_settings["height"],
            new_settings["sort_col"], new_settings["sort_asc"])


def _apply_report_settings(disp_df: pd.DataFrame, settings: tuple) -> pd.DataFrame:
    """Apply the (columns, height, sort_col, sort_asc) tuple from
    _report_settings to a display dataframe: reorders/filters columns and
    sorts rows. Height is used directly by the caller when invoking
    st.dataframe."""
    cols, _height, sort_col, sort_asc = settings
    out = disp_df.copy()
    if sort_col != "(none)" and sort_col in out.columns:
        out = out.sort_values(by=sort_col, ascending=sort_asc)
    keep = [c for c in cols if c in out.columns]
    return out[keep] if keep else out


# ═════════════════════════════════════════════════════════════════════════════
#  IDEA BOARD  (embedded from idea_board_streamlit_02.py — generalized to
#  ALL FOUR sources, not just BSE Equity)
#
#  Reads the idea_groups / idea_types / announcement_idea_scores tables that
#  announcement_ideas_pipeline.py writes into whichever DB it's pointed at
#  (bse_equity.db / bse_sme.db / nse_equity.db / nse_sme.db), joined against
#  that DB's normalized `v_idea_source` view (see announcement_ideas_pipeline.
#  ensure_source_view — it maps each source's native schema onto one common
#  shape: id, company_name, symbol, scrip_code, subject, input_timestamp,
#  attachment_url, category, subcategory).
#
#  Every function takes a `kp` (key_prefix) argument — a short per-source tag
#  ("be"/"bs"/"ne"/"ns") — prepended to every widget key and session_state
#  key, so the same functions can be rendered once per source tab in the
#  same Streamlit run without clashing.
# ═════════════════════════════════════════════════════════════════════════════

IDB_PAGE_SIZE = 15
IDB_CACHE_TTL = 60  # seconds; "Refresh data" button bypasses this instantly


def _idb_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _idb_ensure_source_view(db_path: str):
    """Creates/refreshes v_idea_source in db_path (needs a writable
    connection, unlike the rest of the idb_ helpers)."""
    if not _IDEA_PIPELINE_OK:
        return
    conn = sqlite3.connect(db_path)
    try:
        idea_pipeline.ensure_source_view(conn)
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def idb_get_meta(db_path: str, mtime: float):
    """Small, rarely-changing lookup tables — safe to cache longer."""
    conn = _idb_connect(db_path)
    try:
        groups = pd.read_sql_query("SELECT id, name, sort_order FROM idea_groups ORDER BY sort_order", conn)
        types = pd.read_sql_query(
            "SELECT id, group_id, name, description, sort_order FROM idea_types ORDER BY sort_order", conn
        )
    finally:
        conn.close()
    types = types.merge(
        groups.rename(columns={"name": "group_name"}), left_on="group_id", right_on="id", suffixes=("", "_g")
    )
    return groups, types


@st.cache_data(ttl=300, show_spinner=False)
def idb_get_date_bounds(db_path: str, mtime: float):
    conn = _idb_connect(db_path)
    try:
        row = conn.execute(f"SELECT MIN(input_timestamp), MAX(input_timestamp) FROM {IDEA_SOURCE_VIEW}").fetchone()
    finally:
        conn.close()
    if not row or not row[0] or not row[1]:
        return None, None
    try:
        return pd.to_datetime(row[0]).date(), pd.to_datetime(row[1]).date()
    except Exception:
        return None, None


@st.cache_data(ttl=IDB_CACHE_TTL, show_spinner=False)
def idb_get_type_counts(db_path: str, mtime: float, date_start, date_end):
    conn = _idb_connect(db_path)
    try:
        if date_start and date_end:
            q = f"""
                SELECT s.idea_type_id AS id, COUNT(*) AS count, AVG(s.score) AS avg_score
                FROM announcement_idea_scores s
                JOIN {IDEA_SOURCE_VIEW} a ON a.id = s.announcement_id
                WHERE a.input_timestamp >= ? AND a.input_timestamp <= ?
                GROUP BY s.idea_type_id
            """
            df = pd.read_sql_query(q, conn, params=[f"{date_start} 00:00:00", f"{date_end} 23:59:59"])
        else:
            q = "SELECT idea_type_id AS id, COUNT(*) AS count, AVG(score) AS avg_score FROM announcement_idea_scores GROUP BY idea_type_id"
            df = pd.read_sql_query(q, conn)
    finally:
        conn.close()
    return df


@st.cache_data(ttl=IDB_CACHE_TTL, show_spinner=False)
def idb_get_summary_metrics(db_path: str, mtime: float, date_start, date_end):
    conn = _idb_connect(db_path)
    try:
        if date_start and date_end:
            row = conn.execute(
                f"""SELECT COUNT(DISTINCT s.announcement_id), COUNT(*)
                   FROM announcement_idea_scores s JOIN {IDEA_SOURCE_VIEW} a ON a.id = s.announcement_id
                   WHERE a.input_timestamp >= ? AND a.input_timestamp <= ?""",
                [f"{date_start} 00:00:00", f"{date_end} 23:59:59"],
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(DISTINCT announcement_id), COUNT(*) FROM announcement_idea_scores"
            ).fetchone()
    finally:
        conn.close()
    return (row[0] or 0), (row[1] or 0)


def _idb_build_where(date_start, date_end, min_score, search, idea_type_ids):
    clauses = ["s.score >= ?"]
    params = [min_score]
    if date_start and date_end:
        clauses.append("a.input_timestamp >= ? AND a.input_timestamp <= ?")
        params += [f"{date_start} 00:00:00", f"{date_end} 23:59:59"]
    if idea_type_ids:
        placeholders = ",".join("?" for _ in idea_type_ids)
        clauses.append(f"s.idea_type_id IN ({placeholders})")
        params += list(idea_type_ids)
    if search:
        like = f"%{search}%"
        clauses.append("(a.company_name LIKE ? OR a.subject LIKE ? OR a.symbol LIKE ?)")
        params += [like, like, like]
    return " AND ".join(clauses), params


@st.cache_data(ttl=IDB_CACHE_TTL, show_spinner=False)
def idb_count_filtered(db_path, mtime, date_start, date_end, min_score, search, idea_type_ids):
    where_sql, params = _idb_build_where(date_start, date_end, min_score, search, idea_type_ids)
    conn = _idb_connect(db_path)
    try:
        n = conn.execute(
            f"SELECT COUNT(*) FROM announcement_idea_scores s JOIN {IDEA_SOURCE_VIEW} a ON a.id = s.announcement_id WHERE {where_sql}",
            params,
        ).fetchone()[0]
    finally:
        conn.close()
    return n


@st.cache_data(ttl=IDB_CACHE_TTL, show_spinner=False)
def idb_fetch_page(db_path, mtime, date_start, date_end, min_score, search, idea_type_ids, sort_col, sort_asc, limit, offset):
    where_sql, params = _idb_build_where(date_start, date_end, min_score, search, idea_type_ids)
    order_expr = "a.input_timestamp" if sort_col == "input_timestamp" else "s.score"
    order_dir = "ASC" if sort_asc else "DESC"
    query = f"""
        SELECT s.id AS score_id, s.announcement_id, s.idea_type_id, s.score, s.matched_keywords,
               a.company_name, a.symbol, a.scrip_code, a.subject, a.input_timestamp, a.attachment_url
        FROM announcement_idea_scores s
        JOIN {IDEA_SOURCE_VIEW} a ON a.id = s.announcement_id
        WHERE {where_sql}
        ORDER BY {order_expr} {order_dir}
        LIMIT ? OFFSET ?
    """
    conn = _idb_connect(db_path)
    try:
        df = pd.read_sql_query(query, conn, params=params + [limit, offset])
    finally:
        conn.close()
    return df


def idb_score_badge(score: float) -> str:
    band = "high" if score >= 70 else "mid" if score >= 40 else "low"
    return f'<span class="idb-score {band}">{score:.1f}</span>'


def idb_render_card(row) -> str:
    kws = json.loads(row.matched_keywords) if row.matched_keywords else []
    kw_html = "".join(
        f'<span class="idb-kw{" cat" if k.startswith("[category]") else ""}">'
        f'{k.replace("[category] ", "")}</span>'
        for k in kws
    )
    link = (
        f'<a href="{row.attachment_url}" target="_blank">View filing &rarr;</a>'
        if row.attachment_url else ""
    )
    return f"""
    <div class="idb-card">
      <div class="idb-top">
        <div>
          <div class="idb-company">{row.company_name or ""}</div>
          <div class="idb-symbol">{row.symbol or ""} &middot; {row.scrip_code or ""}</div>
        </div>
        {idb_score_badge(row.score)}
      </div>
      <div class="idb-subject">{row.subject or ""}</div>
      <div>{kw_html}</div>
      <div class="idb-ts">{(row.input_timestamp or "")[:16]} &nbsp; {link}</div>
    </div>
    """


def render_idea_board(db_path: str, kp: str, source_label: str = ""):
    """Idea board for one source — reads idea_groups / idea_types /
    announcement_idea_scores (joined via v_idea_source) from db_path. Run
    announcement_ideas_pipeline.py --db <db_path> run (or the in-app
    Guided Activity → ③ Score ideas step) to populate it; this view is
    read-only. `kp` is a short per-source key prefix (e.g. "be"/"bs"/"ne"/"ns")
    so widget/session keys don't collide when several sources' Idea Boards
    exist in the same Streamlit run."""

    st.caption(f"Corporate announcements categorized by key business events and developments · scored from `announcement_ideas_pipeline.py`{(' · ' + source_label) if source_label else ''}")

    with st.expander("⚙️ Data source", expanded=False):
        dcol1, dcol2 = st.columns([5, 1])
        with dcol1:
            idb_db_path = st.text_input("SQLite database path", value=db_path, key=f"{kp}_idb_db_path", label_visibility="collapsed")
        with dcol2:
            idb_refresh = st.button("Refresh data", use_container_width=True, key=f"{kp}_idb_refresh")

    idb_db_file = Path(idb_db_path)
    if not idb_db_file.exists():
        st.error(
            f"Can't find `{idb_db_path}`. Run the pipeline first, e.g.\n\n"
            f"`python3 announcement_ideas_pipeline.py --db {idb_db_path} run`"
        )
        return

    if idb_refresh:
        st.cache_data.clear()

    try:
        _idb_ensure_source_view(str(idb_db_file))
    except Exception as e:
        st.error(f"Couldn't read `{idb_db_path}`'s announcements schema: {e}")
        return

    idb_mtime = idb_db_file.stat().st_mtime

    try:
        groups, types = idb_get_meta(str(idb_db_file), idb_mtime)
    except (sqlite3.OperationalError, pd.errors.DatabaseError) as e:
        st.error(
            f"`{idb_db_path}` doesn't have the idea-board tables yet. Run:\n\n"
            f"`python3 announcement_ideas_pipeline.py --db {idb_db_path} run`\n\n"
            f"(or use **Guided Activity → ③ Score ideas**, with **Source** set to this one)\n\n"
            f"Details: {e}"
        )
        return

    if idb_refresh:
        st.rerun()

    # ---------------- filters ----------------
    bounds_min, bounds_max = idb_get_date_bounds(str(idb_db_file), idb_mtime)

    fcol1, fcol2, fcol3, fcol4 = st.columns([2, 2, 1, 1.3])

    with fcol1:
        if bounds_min and bounds_max:
            default_start = max(bounds_min, bounds_max - pd.Timedelta(days=6))
            date_range = st.date_input(
                "Date range", value=(default_start, bounds_max), min_value=bounds_min, max_value=bounds_max,
                key=f"{kp}_idb_date_range",
            )
            if isinstance(date_range, tuple) and len(date_range) == 2:
                date_start, date_end = date_range
            else:
                date_start, date_end = bounds_min, bounds_max
        else:
            date_start, date_end = None, None
            st.caption("No announcement dates found — date filter disabled.")

    type_counts = idb_get_type_counts(str(idb_db_file), idb_mtime, date_start, date_end)
    types = types.drop(columns=["count", "avg_score"], errors="ignore").merge(
        type_counts, on="id", how="left"
    )
    types["count"] = types["count"].fillna(0).astype(int)
    types["avg_score"] = types["avg_score"].fillna(0).round(1)

    with fcol2:
        search = st.text_input("Search company or subject", "", key=f"{kp}_idb_search")
    with fcol3:
        min_score = st.slider("Minimum score", 0, 100, 0, step=5, key=f"{kp}_idb_min_score")
    with fcol4:
        sort_by = st.selectbox("Sort by", ["Score (high to low)", "Date (newest first)"], key=f"{kp}_idb_sort_by")

    nav_group_key = f"{kp}_idb_nav_group"
    nav_type_key = f"{kp}_idb_nav_type"
    if nav_group_key not in st.session_state:
        st.session_state[nav_group_key] = None  # None = top level (all groups)
    if nav_type_key not in st.session_state:
        st.session_state[nav_type_key] = None   # None = showing the group itself, not one sub-category

    group_counts = types.groupby("group_name")["count"].sum().to_dict()
    groups_sorted = groups.sort_values("sort_order")

    def _idb_nav_row(items, ncols=4):
        for i in range(0, len(items), ncols):
            row = items[i: i + ncols]
            cols = st.columns(len(row))
            for col, (label, key, on_click) in zip(cols, row):
                with col:
                    if st.button(label, use_container_width=True, key=key):
                        on_click()
                        st.rerun()

    if date_start is not None:
        st.caption(f"Scoped to announcements between **{date_start}** and **{date_end}**")

    st.divider()

    # ---------------- category navigation ----------------
    st.markdown("#### Browse by category")

    if st.session_state[nav_group_key] is None:
        total_all = int(types["count"].sum())
        st.markdown(f"**● All groups**  ({total_all})")
        items = []
        for _, g in groups_sorted.iterrows():
            gname = g["name"]
            cnt = int(group_counts.get(gname, 0))

            def _idb_select_group(gname=gname):
                st.session_state[nav_group_key] = gname
                st.session_state[nav_type_key] = None

            items.append((f"{gname}  ({cnt})", f"{kp}_idb_grp_{g['id']}", _idb_select_group))
        _idb_nav_row(items)
        selected_group, selected_type = None, None

    else:
        sub_types = types[types["group_name"] == st.session_state[nav_group_key]].sort_values("sort_order")
        group_total = int(sub_types["count"].sum())

        bcol1, bcol2 = st.columns([1, 5])
        with bcol1:
            if st.button("← All groups", use_container_width=True, key=f"{kp}_idb_back_to_groups"):
                st.session_state[nav_group_key] = None
                st.session_state[nav_type_key] = None
                st.rerun()
        with bcol2:
            crumb = f"**{st.session_state[nav_group_key]}**"
            if st.session_state[nav_type_key]:
                crumb += f"  →  **{st.session_state[nav_type_key]}**"
            st.markdown(crumb)

        items = []
        all_marker = "●" if st.session_state[nav_type_key] is None else "○"
        if st.session_state[nav_type_key] is not None:
            def _idb_select_all():
                st.session_state[nav_type_key] = None
            items.append((f"{all_marker} All in {st.session_state[nav_group_key]}  ({group_total})", f"{kp}_idb_type_all", _idb_select_all))
        else:
            st.markdown(f"{all_marker} **All in {st.session_state[nav_group_key]}**  ({group_total}) — showing below")

        for _, t in sub_types.iterrows():
            selected = st.session_state[nav_type_key] == t["name"]
            marker = "●" if selected else "○"
            tname = t["name"]

            def _idb_select_type(tname=tname):
                st.session_state[nav_type_key] = tname

            items.append((f"{marker} {tname}  ({int(t['count'])})", f"{kp}_idb_type_{t['id']}", _idb_select_type))

        _idb_nav_row(items)

        selected_group = st.session_state[nav_group_key]
        selected_type = st.session_state[nav_type_key]

    if selected_type:
        idea_type_ids = tuple(types.loc[types["name"] == selected_type, "id"].tolist())
    elif selected_group:
        idea_type_ids = tuple(types.loc[types["group_name"] == selected_group, "id"].tolist())
    else:
        idea_type_ids = tuple()

    # ---------------- summary metrics ----------------
    total_announcements, total_matches_in_range = idb_get_summary_metrics(str(idb_db_file), idb_mtime, date_start, date_end)
    active_types = int((types["count"] > 0).sum())

    m1, m2, m3 = st.columns(3)
    _metric(m1, f"{total_announcements:,}", "Announcements with an idea")
    _metric(m2, f"{total_matches_in_range:,}", "Total idea matches")
    _metric(m3, f"{active_types} / {len(types)}", "Idea types with data")

    st.divider()

    # ---------------- pagination state ----------------
    page_key = f"{kp}_idb_page"
    filters_key_key = f"{kp}_idb_filters_key"
    filters_key = f"{search}|{min_score}|{sort_by}|{idea_type_ids}|{date_start}|{date_end}"
    if st.session_state.get(filters_key_key) != filters_key:
        st.session_state[filters_key_key] = filters_key
        st.session_state[page_key] = 1

    sort_col, sort_asc = ("score", False) if sort_by.startswith("Score") else ("input_timestamp", False)

    total_items = idb_count_filtered(str(idb_db_file), idb_mtime, date_start, date_end, min_score, search, idea_type_ids)
    total_pages = max(1, -(-total_items // IDB_PAGE_SIZE))
    idb_page = min(max(st.session_state.get(page_key, 1), 1), total_pages)
    st.session_state[page_key] = idb_page
    offset = (idb_page - 1) * IDB_PAGE_SIZE

    page_df = idb_fetch_page(
        str(idb_db_file), idb_mtime, date_start, date_end, min_score, search, idea_type_ids,
        sort_col, sort_asc, IDB_PAGE_SIZE, offset,
    )

    # ---------------- render, grouped by idea type ----------------
    if page_df.empty:
        st.info("No announcements match these filters. Try lowering the minimum score or clearing the search.")
    else:
        types_to_show = types[types["id"].isin(page_df["idea_type_id"].unique())].sort_values(
            ["group_name", "sort_order"]
        )
        for _, t in types_to_show.iterrows():
            sub = page_df[page_df["idea_type_id"] == t["id"]]
            if sub.empty:
                continue
            st.subheader(f"{t['name']}  ·  {t['group_name']}")
            st.caption(t["description"])
            cols = st.columns(3)
            for i, row in enumerate(sub.itertuples()):
                with cols[i % 3]:
                    st.markdown(idb_render_card(row), unsafe_allow_html=True)

        st.divider()
        pcol1, pcol2, pcol3 = st.columns([1, 2, 1])
        with pcol1:
            if st.button("← Previous", disabled=idb_page <= 1, use_container_width=True, key=f"{kp}_idb_prev"):
                st.session_state[page_key] = idb_page - 1
                st.rerun()
        with pcol2:
            start_n = offset + 1
            end_n = min(offset + IDB_PAGE_SIZE, total_items)
            st.markdown(
                f"<div style='text-align:center;padding-top:6px;font-size:13px;color:var(--text-secondary,#666);'>"
                f"Showing {start_n}–{end_n} of {total_items} &middot; page {idb_page} of {total_pages}</div>",
                unsafe_allow_html=True,
            )
        with pcol3:
            if st.button("Next →", disabled=idb_page >= total_pages, use_container_width=True, key=f"{kp}_idb_next"):
                st.session_state[page_key] = idb_page + 1
                st.rerun()

    _log_search(f"{source_label} — Idea Board" if source_label else "Idea Board", {"search": search, "min_score": min_score, "group": selected_group, "type": selected_type}, total_items)


# ═════════════════════════════════════════════════════════════════════════════
#  TRACKER  (embedded from app.py — generalized to ALL FOUR sources)
#
#  Search + one-click Overview + a status/notes tracker + a starred Watchlist
#  on top of whichever DB db_path points at, read via that DB's normalized
#  `v_idea_source` view. Adds three of its own tables (announcement_notes /
#  announcement_status / watchlist) the first time it runs, IF NOT EXISTS.
#  All keys/functions are prefixed trk_ / _trk_ to avoid clashing with the
#  rest of the dashboard; every widget/session key additionally takes a `kp`
#  (key_prefix) so multiple sources' Trackers can coexist in one run.
# ═════════════════════════════════════════════════════════════════════════════

TRK_STATUS_OPTIONS = ["New", "Watching", "Important", "Actioned", "Ignored"]


def _trk_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _trk_init_db(db_path: str):
    """Creates the tracker's own tables (notes / status / watchlist) if they
    don't exist yet, and (re)builds v_idea_source. Doesn't touch the raw
    announcements table — that's owned by market_announcements.py."""
    if _IDEA_PIPELINE_OK:
        conn = sqlite3.connect(db_path)
        try:
            idea_pipeline.ensure_source_view(conn)
        finally:
            conn.close()
    conn = _trk_conn(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS announcement_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(announcement_id) REFERENCES announcements(id)
            );
            CREATE TABLE IF NOT EXISTS announcement_status (
                announcement_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'New',
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(announcement_id) REFERENCES announcements(id)
            );
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                announcement_id INTEGER NOT NULL UNIQUE,
                company_name TEXT,
                symbol TEXT,
                scrip_code TEXT,
                subject TEXT,
                target_price REAL,
                remarks TEXT,
                status TEXT NOT NULL DEFAULT 'Active',
                added_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(announcement_id) REFERENCES announcements(id)
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _trk_run_query(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _trk_conn(db_path)
    try:
        df = pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()
    return df


def _trk_execute(db_path: str, sql: str, params: tuple = ()):
    conn = _trk_conn(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _trk_delete_announcement(db_path: str, ann_id: int):
    """Cascading delete: removes the announcement and everything that
    references it (idea scores, notes, status, watchlist entry)."""
    conn = _trk_conn(db_path)
    try:
        conn.execute("DELETE FROM announcement_idea_scores WHERE announcement_id = ?", (ann_id,))
        conn.execute("DELETE FROM announcement_notes WHERE announcement_id = ?", (ann_id,))
        conn.execute("DELETE FROM announcement_status WHERE announcement_id = ?", (ann_id,))
        conn.execute("DELETE FROM watchlist WHERE announcement_id = ?", (ann_id,))
        conn.execute("DELETE FROM announcements WHERE id = ?", (ann_id,))
        conn.commit()
    finally:
        conn.close()


def _trk_add_to_watchlist(db_path, ann_id, company_name, symbol, scrip_code, subject):
    _trk_execute(
        db_path,
        """INSERT INTO watchlist (announcement_id, company_name, symbol, scrip_code, subject)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(announcement_id) DO NOTHING""",
        (ann_id, company_name, symbol, scrip_code, subject),
    )


def _trk_remove_from_watchlist(db_path, ann_id):
    _trk_execute(db_path, "DELETE FROM watchlist WHERE announcement_id = ?", (ann_id,))


def _trk_get_watchlist_ids(db_path) -> set:
    try:
        df = _trk_run_query(db_path, "SELECT announcement_id FROM watchlist")
        return set(df["announcement_id"].tolist())
    except Exception:
        return set()


def _trk_get_categories(db_path):
    try:
        df = _trk_run_query(db_path, f"SELECT DISTINCT category AS name FROM {IDEA_SOURCE_VIEW} WHERE category IS NOT NULL AND category != '' ORDER BY category")
        return df
    except Exception:
        return pd.DataFrame(columns=["name"])


def _trk_get_idea_types(db_path):
    try:
        return _trk_run_query(
            db_path,
            """SELECT it.id, it.name, g.name AS group_name
               FROM idea_types it JOIN idea_groups g ON g.id = it.group_id
               ORDER BY g.sort_order, it.sort_order""",
        )
    except Exception:
        return pd.DataFrame(columns=["id", "name", "group_name"])


def _trk_get_status_map(db_path):
    try:
        df = _trk_run_query(db_path, "SELECT announcement_id, status FROM announcement_status")
        return dict(zip(df["announcement_id"], df["status"]))
    except Exception:
        return {}


def _trk_get_note_counts(db_path):
    try:
        df = _trk_run_query(
            db_path,
            "SELECT announcement_id, COUNT(*) AS n FROM announcement_notes GROUP BY announcement_id",
        )
        return dict(zip(df["announcement_id"], df["n"]))
    except Exception:
        return {}


def _trk_go_to_overview(kp, ann_id: int):
    st.session_state[f"{kp}_trk_selected_id"] = ann_id
    st.session_state[f"{kp}_trk_view"] = "overview"


def _trk_go_to_search(kp):
    st.session_state[f"{kp}_trk_view"] = "search"
    st.session_state[f"{kp}_trk_confirm_delete_id"] = None


def _trk_go_to_watchlist(kp):
    st.session_state[f"{kp}_trk_view"] = "watchlist"


def _trk_ask_confirm_delete(kp, ann_id: int):
    st.session_state[f"{kp}_trk_confirm_delete_id"] = ann_id


def _trk_cancel_delete(kp):
    st.session_state[f"{kp}_trk_confirm_delete_id"] = None


def _trk_do_delete(kp, db_path, ann_id: int):
    _trk_delete_announcement(db_path, ann_id)
    st.session_state[f"{kp}_trk_confirm_delete_id"] = None
    if st.session_state[f"{kp}_trk_selected_id"] == ann_id:
        st.session_state[f"{kp}_trk_selected_id"] = None
        st.session_state[f"{kp}_trk_view"] = "search"


def _trk_do_toggle_watchlist(db_path, ann_id, in_wl, company_name, symbol, scrip_code, subject):
    if in_wl:
        _trk_remove_from_watchlist(db_path, ann_id)
    else:
        _trk_add_to_watchlist(db_path, ann_id, company_name, symbol, scrip_code, subject)


# ── Track (Tracker sub-tab row action) ───────────────────────────────────────
# Replaces the old ⭐ Add-to-Watchlist button. One click, one row: embeds
# at_track_announcement()'s company-master ↔ tracked-announcement (1:N) logic
# directly on that single announcement — never the whole result set.

TRK_KP_TO_SOURCE = {"be": "BSE Equity", "bs": "BSE SME", "ne": "NSE Equity", "ns": "NSE SME"}


def _trk_build_track_payload(kp: str, row: dict):
    """Translates one row from the Tracker tab's normalized query (columns:
    id, company_name, symbol, scrip_code, category, subcategory, subject,
    input_timestamp, attachment_url) into the raw field names
    at_track_announcement expects for this source, per AT_SOURCE_FIELD_MAP.
    Returns (source_label, payload_dict) for a SINGLE record only."""
    source = TRK_KP_TO_SOURCE.get(kp, "BSE Equity")
    cfg = AT_SOURCE_FIELD_MAP[source]
    payload = {"id": row.get("id")}
    if cfg["company"]:
        payload[cfg["company"]] = row.get("company_name")
    if cfg["code"]:
        payload[cfg["code"]] = row.get("scrip_code") or row.get("symbol")
    if cfg["bse"]:
        payload[cfg["bse"]] = row.get("scrip_code")
    if cfg["nse"]:
        payload[cfg["nse"]] = row.get("symbol")
    if cfg["category"]:
        payload[cfg["category"]] = row.get("category")
    if cfg["subcategory"]:
        payload[cfg["subcategory"]] = row.get("subcategory")
    if cfg["subject"]:
        payload[cfg["subject"]] = row.get("subject")
    if cfg["link"]:
        payload[cfg["link"]] = row.get("attachment_url")
    if cfg["date"]:
        payload[cfg["date"]] = row.get("input_timestamp")
    return source, payload


def _trk_do_track_single(kp, ann_id, company_name, symbol, scrip_code, category, subcategory, subject, attachment_url, input_timestamp):
    """Tracks exactly this one announcement — finds/creates its company
    master and inserts one tracker row (1:N). No bulk/selection involved."""
    source, payload = _trk_build_track_payload(kp, {
        "id": ann_id, "company_name": company_name, "symbol": symbol, "scrip_code": scrip_code,
        "category": category, "subcategory": subcategory, "subject": subject,
        "attachment_url": attachment_url, "input_timestamp": input_timestamp,
    })
    at_track_announcement(source, payload)


def trk_render_search_page(db_path: str, kp: str):
    categories_df = _trk_get_categories(db_path)
    idea_types_df = _trk_get_idea_types(db_path)

    with st.form(f"{kp}_trk_search_form"):
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            keyword = st.text_input(
                "Search (company, symbol, scrip code, or subject)",
                placeholder="e.g. Reliance, RELIANCE, 500325, buyback...",
                key=f"{kp}_trk_kw",
            )
        with c2:
            category_name = st.selectbox(
                "Category", options=["Any"] + categories_df["name"].tolist(), key=f"{kp}_trk_cat",
            )
        with c3:
            status_filter = st.selectbox("Status", options=["Any"] + TRK_STATUS_OPTIONS, key=f"{kp}_trk_status_f")

        c4, c5, c6 = st.columns([1, 1, 1])
        with c4:
            date_from = st.date_input("From date", value=None, format="YYYY-MM-DD", key=f"{kp}_trk_from")
        with c5:
            date_to = st.date_input("To date", value=None, format="YYYY-MM-DD", key=f"{kp}_trk_to")
        with c6:
            limit = st.number_input("Max results", min_value=10, max_value=5000, value=200, step=10, key=f"{kp}_trk_limit")

        with st.expander("Idea / signal filters (optional)"):
            ic1, ic2 = st.columns([2, 1])
            with ic1:
                idea_labels = (idea_types_df["group_name"] + " → " + idea_types_df["name"]).tolist()
                idea_selection = st.multiselect("Idea types", options=idea_labels, key=f"{kp}_trk_idea_sel")
            with ic2:
                min_score = st.number_input("Min score", value=0.0, step=0.5, key=f"{kp}_trk_min_score")

        st.form_submit_button("Search", type="primary", use_container_width=True)

    where_clauses, params = [], []
    joins = ""

    if keyword:
        where_clauses.append(
            "(a.company_name LIKE ? OR a.symbol LIKE ? OR a.scrip_code LIKE ? OR a.subject LIKE ?)"
        )
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])

    if category_name and category_name != "Any":
        where_clauses.append("a.category = ?")
        params.append(category_name)

    if date_from:
        where_clauses.append("substr(a.input_timestamp, 1, 10) >= ?")
        params.append(date_from.strftime("%Y-%m-%d"))

    if date_to:
        where_clauses.append("substr(a.input_timestamp, 1, 10) <= ?")
        params.append(date_to.strftime("%Y-%m-%d"))

    if idea_selection:
        idea_ids = idea_types_df[
            (idea_types_df["group_name"] + " → " + idea_types_df["name"]).isin(idea_selection)
        ]["id"].tolist()
        joins = "JOIN announcement_idea_scores ais ON ais.announcement_id = a.id"
        placeholders = ",".join(["?"] * len(idea_ids))
        where_clauses.append(f"ais.idea_type_id IN ({placeholders})")
        params.extend(idea_ids)
        where_clauses.append("ais.score >= ?")
        params.append(min_score)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    sql = f"""
        SELECT DISTINCT a.id, a.company_name, a.symbol, a.scrip_code, a.category,
               a.subcategory, a.subject, a.input_timestamp, a.attachment_url
        FROM {IDEA_SOURCE_VIEW} a {joins}
        {where_sql}
        ORDER BY a.input_timestamp DESC
        LIMIT ?
    """
    params.append(int(limit))

    try:
        results = _trk_run_query(db_path, sql, tuple(params))
    except Exception as e:
        st.error(f"Query failed: {e}")
        return

    status_map = _trk_get_status_map(db_path)
    note_counts = _trk_get_note_counts(db_path)

    if status_filter != "Any":
        wanted_ids = [k for k, v in status_map.items() if v == status_filter]
        results = results[results["id"].isin(wanted_ids)]

    st.caption(f"{len(results)} result(s)")

    if results.empty:
        st.info("No announcements match your search.")
        return

    hcols = st.columns([2.1, 1.0, 1.3, 3.1, 1.2, 0.9, 1.7])
    for col, label in zip(
        hcols, ["Company", "Symbol", "Category", "Subject", "Date/Time", "Status", "Actions"]
    ):
        col.markdown(f"**{label}**")
    st.divider()

    confirm_key = f"{kp}_trk_confirm_delete_id"
    for _, row in results.iterrows():
        ann_id = int(row["id"])
        cols = st.columns([2.1, 1.0, 1.3, 3.1, 1.2, 0.9, 1.7])
        cols[0].write(row["company_name"] or "—")
        cols[1].write(row["symbol"] or (row["scrip_code"] or "—"))
        cols[2].write(row["category"] or "—")
        subject_txt = (row["subject"] or "").strip()
        cols[3].write((subject_txt[:90] + "…") if len(subject_txt) > 90 else (subject_txt or "—"))
        cols[4].write(row["input_timestamp"] or "—")

        status_val = status_map.get(ann_id, "New")
        note_n = note_counts.get(ann_id, 0)
        badge = f":blue[{status_val}]" if status_val == "Watching" else (
            f":red[{status_val}]" if status_val == "Important" else (
                f":green[{status_val}]" if status_val == "Actioned" else (
                    f":gray[{status_val}]"
                )
            )
        )
        note_suffix = f" 📝{note_n}" if note_n else ""
        cols[5].markdown(f"{badge}{note_suffix}")

        if st.session_state.get(confirm_key) == ann_id:
            with cols[6]:
                wc1, wc2 = st.columns(2)
                wc1.button(
                    "✅ Confirm", key=f"{kp}_trk_confirm_del_{ann_id}",
                    on_click=_trk_do_delete, args=(kp, db_path, ann_id), use_container_width=True,
                )
                wc2.button(
                    "✖ Cancel", key=f"{kp}_trk_cancel_del_{ann_id}",
                    on_click=_trk_cancel_delete, args=(kp,), use_container_width=True,
                )
        else:
            already_tracked = at_is_tracked(TRK_KP_TO_SOURCE.get(kp, "BSE Equity"), ann_id)
            with cols[6]:
                ac1, ac2, ac3 = st.columns(3)
                ac1.button(
                    "👁️", key=f"{kp}_trk_view_{ann_id}", help="Overview",
                    on_click=_trk_go_to_overview, args=(kp, ann_id), use_container_width=True,
                )
                ac2.button(
                    "🗑️", key=f"{kp}_trk_del_{ann_id}", help="Delete this announcement",
                    on_click=_trk_ask_confirm_delete, args=(kp, ann_id), use_container_width=True,
                )
                ac3.button(
                    "✅" if already_tracked else "📌",
                    key=f"{kp}_trk_track_{ann_id}",
                    help="Already tracked (Company master ↔ tracker)" if already_tracked else "Track this announcement → Company master",
                    on_click=_trk_do_track_single,
                    args=(kp, ann_id, row["company_name"], row["symbol"], row["scrip_code"],
                          row["category"], row["subcategory"], row["subject"],
                          row["attachment_url"], row["input_timestamp"]),
                    use_container_width=True,
                    disabled=already_tracked,
                )
        st.divider()


def trk_render_overview_page(db_path: str, kp: str):
    ann_id = st.session_state[f"{kp}_trk_selected_id"]
    st.button("← Back to search", on_click=_trk_go_to_search, args=(kp,), key=f"{kp}_trk_back_btn")

    ann_df = _trk_run_query(db_path, f"SELECT * FROM {IDEA_SOURCE_VIEW} WHERE id = ?", (ann_id,))
    if ann_df.empty:
        st.error("Announcement not found.")
        return
    ann = ann_df.iloc[0]

    st.subheader(ann["company_name"] or "Unknown company")
    sub_cols = st.columns([1, 1, 1, 1])
    sub_cols[0].metric("Symbol", ann["symbol"] or "—")
    sub_cols[1].metric("Scrip code", ann["scrip_code"] or "—")
    sub_cols[2].metric("Category", ann["category"] or "—")
    sub_cols[3].metric("Subcategory", ann["subcategory"] or "—")

    st.markdown("**Subject**")
    st.write(ann["subject"] or "—")

    st.write(f"**Filed:** {ann['input_timestamp'] or '—'}")

    if ann["attachment_url"]:
        st.link_button("📎 Open attachment / PDF", ann["attachment_url"])

    idea_df = _trk_run_query(
        db_path,
        """SELECT g.name AS idea_group, t.name AS idea_type, s.score, s.matched_keywords
           FROM announcement_idea_scores s
           JOIN idea_types t ON t.id = s.idea_type_id
           JOIN idea_groups g ON g.id = t.group_id
           WHERE s.announcement_id = ?
           ORDER BY s.score DESC""",
        (ann_id,),
    )
    if not idea_df.empty:
        st.markdown("**Idea signal scores**")
        st.dataframe(idea_df, use_container_width=True, hide_index=True)

    st.divider()

    st.markdown("**📌 Status**")
    cur_status_df = _trk_run_query(
        db_path, "SELECT status FROM announcement_status WHERE announcement_id = ?", (ann_id,)
    )
    cur_status = cur_status_df.iloc[0]["status"] if not cur_status_df.empty else "New"

    sc1, sc2 = st.columns([2, 1])
    with sc1:
        new_status = st.selectbox(
            "Track this announcement as",
            TRK_STATUS_OPTIONS,
            index=TRK_STATUS_OPTIONS.index(cur_status) if cur_status in TRK_STATUS_OPTIONS else 0,
            key=f"{kp}_trk_status_sel_{ann_id}",
        )
    with sc2:
        st.write("")
        st.write("")
        if st.button("Save status", use_container_width=True, key=f"{kp}_trk_save_status_{ann_id}"):
            _trk_execute(
                db_path,
                """INSERT INTO announcement_status (announcement_id, status, updated_at)
                   VALUES (?, ?, datetime('now','localtime'))
                   ON CONFLICT(announcement_id) DO UPDATE SET
                       status=excluded.status, updated_at=excluded.updated_at""",
                (ann_id, new_status),
            )
            st.success(f"Status set to '{new_status}'")
            st.rerun()

    st.divider()

    st.markdown("**📝 Notes**")
    with st.form(f"{kp}_trk_note_form_{ann_id}", clear_on_submit=True):
        new_note = st.text_area("Add a note", placeholder="Type a quick note about this announcement...")
        add_clicked = st.form_submit_button("Add note", type="primary")
    if add_clicked and new_note.strip():
        _trk_execute(
            db_path,
            "INSERT INTO announcement_notes (announcement_id, note) VALUES (?, ?)",
            (ann_id, new_note.strip()),
        )
        st.rerun()

    notes_df = _trk_run_query(
        db_path,
        "SELECT id, note, created_at FROM announcement_notes WHERE announcement_id = ? ORDER BY created_at DESC",
        (ann_id,),
    )
    if notes_df.empty:
        st.caption("No notes yet.")
    else:
        for _, n in notes_df.iterrows():
            with st.container(border=True):
                nc1, nc2 = st.columns([5, 1])
                nc1.write(n["note"])
                nc1.caption(n["created_at"])
                if nc2.button("🗑️ Delete", key=f"{kp}_trk_del_note_{n['id']}"):
                    _trk_execute(db_path, "DELETE FROM announcement_notes WHERE id = ?", (int(n["id"]),))
                    st.rerun()


def trk_render_watchlist_page(db_path: str, kp: str):
    st.caption("Announcements you've flagged with the ⭐ icon, tracked separately from the raw feed.")

    wl_df = _trk_run_query(db_path, "SELECT * FROM watchlist ORDER BY added_at DESC")

    if wl_df.empty:
        st.info("Nothing on your watchlist yet. Go to Search and tap ⭐ on an announcement to add it.")
        return

    for _, w in wl_df.iterrows():
        wl_id = int(w["id"])
        ann_id = int(w["announcement_id"])
        with st.container(border=True):
            top = st.columns([3, 1, 1, 1])
            top[0].markdown(f"**{w['company_name'] or '—'}**  ({w['symbol'] or w['scrip_code'] or '—'})")
            top[1].caption(f"Added: {w['added_at']}")
            if top[2].button("Overview →", key=f"{kp}_trk_wl_open_{wl_id}"):
                _trk_go_to_overview(kp, ann_id)
                st.rerun()
            if top[3].button("🗑️ Remove", key=f"{kp}_trk_wl_remove_{wl_id}"):
                _trk_remove_from_watchlist(db_path, ann_id)
                st.rerun()

            st.write(w["subject"] or "—")

            with st.form(f"{kp}_trk_wl_edit_{wl_id}"):
                ec1, ec2, ec3 = st.columns([1, 1, 2])
                target_price = ec1.number_input(
                    "Target price", value=float(w["target_price"]) if w["target_price"] is not None else 0.0,
                    step=0.5, key=f"{kp}_trk_tp_{wl_id}",
                )
                wl_status = ec2.selectbox(
                    "Status", ["Active", "Hit Target", "Closed"],
                    index=["Active", "Hit Target", "Closed"].index(w["status"]) if w["status"] in ["Active", "Hit Target", "Closed"] else 0,
                    key=f"{kp}_trk_wst_{wl_id}",
                )
                remarks = ec3.text_input("Remarks", value=w["remarks"] or "", key=f"{kp}_trk_rm_{wl_id}")
                if st.form_submit_button("Save"):
                    _trk_execute(
                        db_path,
                        "UPDATE watchlist SET target_price = ?, status = ?, remarks = ? WHERE id = ?",
                        (target_price, wl_status, remarks, wl_id),
                    )
                    st.rerun()


def render_tracker(db_path: str, kp: str):
    """Entry point for the embedded Tracker sub-tab — search, one-click
    overview with status/notes, and a starred watchlist, scoped to db_path.
    `kp` is a short per-source key prefix (e.g. "be"/"bs"/"ne"/"ns")."""
    view_key = f"{kp}_trk_view"
    sel_key = f"{kp}_trk_selected_id"
    confirm_key = f"{kp}_trk_confirm_delete_id"
    st.session_state.setdefault(view_key, "search")
    st.session_state.setdefault(sel_key, None)
    st.session_state.setdefault(confirm_key, None)

    try:
        _trk_init_db(db_path)
    except Exception as e:
        st.error(f"Could not initialize tracker tables: {e}")
        return

    if st.session_state[view_key] != "overview":
        nav1, nav2, _ = st.columns([1, 1, 4])
        nav1.button(
            "🔍 Search", use_container_width=True, key=f"{kp}_trk_nav_search",
            type="primary" if st.session_state[view_key] == "search" else "secondary",
            on_click=_trk_go_to_search, args=(kp,),
        )
        nav2.button(
            "⭐ Watchlist", use_container_width=True, key=f"{kp}_trk_nav_watchlist",
            type="primary" if st.session_state[view_key] == "watchlist" else "secondary",
            on_click=_trk_go_to_watchlist, args=(kp,),
        )
        st.divider()

    if st.session_state[view_key] == "overview" and st.session_state[sel_key] is not None:
        trk_render_overview_page(db_path, kp)
    elif st.session_state[view_key] == "watchlist":
        trk_render_watchlist_page(db_path, kp)
    else:
        trk_render_search_page(db_path, kp)


# ═════════════════════════════════════════════════════════════════════════════
#  ANNOUNCEMENT TRACKER  —  Master / Tracker (1:N) + Shadow audit table
#
#  A single cross-source store (announcement_tracker.db) that lets any
#  announcement from any of the four feeds (BSE Equity / BSE SME / NSE Equity
#  / NSE SME) be "tracked" with one click from the Announcements tab.
#
#    announcement_master           — one row per company (name/codes/notes)
#    announcement_tracker          — N rows per master; one per tracked
#                                     announcement, holding category, link,
#                                     the raw announcement JSON, and all the
#                                     "idea-level" analyst flags/notes
#    announcement_tracker_shadow   — append-only audit log of every INSERT /
#                                     UPDATE / DELETE against the tracker
#                                     table (before/after snapshots), i.e.
#                                     the "shadow table for data modulation
#                                     operations"
#
#  DDL note: uses isolation_level=None (autocommit) with one statement per
#  conn.execute() call rather than executescript() — matches the working
#  fix for the SQLite "cannot commit / DDL" errors hit on Windows.
#  All keys/functions are prefixed at_ / atp_ to avoid clashing with the
#  rest of the dashboard.
# ═════════════════════════════════════════════════════════════════════════════

AT_DB_PATH = "announcement_tracker.db"

AT_SPECIAL_SITUATION_TYPES = ["Merger", "Demerger", "OSF", "Buyback", "Other"]
AT_CAPEX_ORDER_TYPES       = ["Capex", "New Order", "Other"]
AT_JV_TECH_TYPES           = ["Joint Venture", "New Tech / Technology Transfer", "Other"]
AT_REG_SENTIMENTS          = ["Neutral", "Positive", "Negative"]

# Per-source field mapping, used both to build the JSON snapshot when an
# announcement is tracked and to know which raw table a "Delete" action
# should remove the source row from.
AT_SOURCE_FIELD_MAP = {
    "BSE Equity": dict(table="announcements", company="company_name", code="scrip_code",
                        bse="scrip_code", nse=None, category="category", subcategory="subcategory",
                        subject="subject", link="document_url", date="input_timestamp"),
    "BSE SME":    dict(table="announcements", company="scrip_name", code="scrip_code",
                        bse="scrip_code", nse=None, category="category", subcategory=None,
                        subject="purpose", link="attachment_url", date="announce_date"),
    "NSE Equity": dict(table="announcements", company="company_name", code="symbol",
                        bse=None, nse="symbol", category=None, subcategory=None,
                        subject="subject", link="attachment_url", date="ann_date"),
    "NSE SME":    dict(table="announcements", company="company_name", code="symbol",
                        bse=None, nse="symbol", category=None, subcategory=None,
                        subject="subject", link="attachment_url", date="ann_date"),
}

AT_MASTER_DDL = [
    """CREATE TABLE IF NOT EXISTS announcement_master (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_name TEXT NOT NULL,
        company_code TEXT,
        bse_code TEXT,
        nse_code TEXT,
        long_text TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_at_master_company ON announcement_master(company_name COLLATE NOCASE);",
    """CREATE TABLE IF NOT EXISTS announcement_tracker (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        master_id INTEGER NOT NULL REFERENCES announcement_master(id),
        source TEXT NOT NULL,
        source_announcement_id INTEGER,
        company_name TEXT,
        company_code TEXT,
        category TEXT,
        subcategory TEXT,
        link TEXT,
        json_data TEXT,
        is_special_situation TEXT DEFAULT 'No',
        special_situation_type TEXT,
        is_capex_or_order TEXT DEFAULT 'No',
        capex_order_type TEXT,
        is_jv_or_tech TEXT DEFAULT 'No',
        jv_tech_type TEXT,
        industry TEXT,
        sub_industry TEXT,
        long_note TEXT,
        regulatory_sentiment TEXT DEFAULT 'Neutral',
        regulatory_notes TEXT,
        risk_notes TEXT,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime'))
    );""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_at_tracker_source_ann ON announcement_tracker(source, source_announcement_id);",
    "CREATE INDEX IF NOT EXISTS idx_at_tracker_master ON announcement_tracker(master_id);",
    """CREATE TABLE IF NOT EXISTS announcement_tracker_shadow (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tracker_id INTEGER,
        master_id INTEGER,
        operation TEXT NOT NULL,
        before_json TEXT,
        after_json TEXT,
        changed_by TEXT,
        changed_at TEXT DEFAULT (datetime('now','localtime'))
    );""",
    "CREATE INDEX IF NOT EXISTS idx_at_shadow_tracker ON announcement_tracker_shadow(tracker_id);",
]


def at_init_db():
    conn = sqlite3.connect(AT_DB_PATH, isolation_level=None)  # autocommit
    try:
        for stmt in AT_MASTER_DDL:
            conn.execute(stmt)
    finally:
        conn.close()


def at_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(AT_DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _at_actor() -> str:
    try:
        return current_user or "unknown"
    except NameError:
        return "unknown"


def _at_shadow_log(conn, operation: str, tracker_id, master_id, before: dict = None, after: dict = None):
    conn.execute(
        "INSERT INTO announcement_tracker_shadow (tracker_id, master_id, operation, before_json, after_json, changed_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tracker_id, master_id, operation,
         json.dumps(before, default=str) if before else None,
         json.dumps(after, default=str) if after else None,
         _at_actor()),
    )


def at_get_or_create_master(company_name: str, company_code: str = "", bse_code: str = "", nse_code: str = "") -> int:
    company_name = (company_name or "Unknown Company").strip()
    conn = at_conn()
    try:
        row = conn.execute(
            "SELECT id FROM announcement_master WHERE company_name = ? COLLATE NOCASE", (company_name,)
        ).fetchone()
        if row:
            master_id = row["id"]
            # Backfill codes if this master was created without them.
            conn.execute(
                "UPDATE announcement_master SET "
                "company_code = COALESCE(NULLIF(company_code,''), ?), "
                "bse_code = COALESCE(NULLIF(bse_code,''), ?), "
                "nse_code = COALESCE(NULLIF(nse_code,''), ?), "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (company_code or "", bse_code or "", nse_code or "", master_id),
            )
            return master_id
        cur = conn.execute(
            "INSERT INTO announcement_master (company_name, company_code, bse_code, nse_code) VALUES (?, ?, ?, ?)",
            (company_name, company_code or "", bse_code or "", nse_code or ""),
        )
        return cur.lastrowid
    finally:
        conn.close()


def at_is_tracked(source: str, source_announcement_id) -> bool:
    if source_announcement_id is None:
        return False
    conn = at_conn()
    try:
        row = conn.execute(
            "SELECT id FROM announcement_tracker WHERE source = ? AND source_announcement_id = ?",
            (source, int(source_announcement_id)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def at_track_announcement(source: str, row: dict) -> int:
    """One-click 'Track' — finds/creates the company master, then inserts a
    tracker row (1:N) for this specific announcement. `row` is the raw
    result-row dict from the source's Announcements listing. Returns the new
    tracker id, or None if this announcement is already tracked."""
    cfg = AT_SOURCE_FIELD_MAP[source]
    company_name = row.get(cfg["company"]) or "Unknown Company"
    code         = row.get(cfg["code"]) or ""
    bse_code     = row.get(cfg["bse"]) if cfg["bse"] else ""
    nse_code     = row.get(cfg["nse"]) if cfg["nse"] else ""
    category     = row.get(cfg["category"]) if cfg["category"] else ""
    subcategory  = row.get(cfg["subcategory"]) if cfg["subcategory"] else ""
    link         = row.get(cfg["link"]) if cfg["link"] else ""
    source_id    = row.get("id")

    if at_is_tracked(source, source_id):
        return None

    master_id = at_get_or_create_master(company_name, code, bse_code, nse_code)

    conn = at_conn()
    try:
        cur = conn.execute(
            """INSERT INTO announcement_tracker
               (master_id, source, source_announcement_id, company_name, company_code,
                category, subcategory, link, json_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (master_id, source, source_id, company_name, code, category, subcategory,
             link, json.dumps(row, default=str)),
        )
        tracker_id = cur.lastrowid
        _at_shadow_log(conn, "INSERT", tracker_id, master_id, after=dict(row))
        return tracker_id
    finally:
        conn.close()


def at_delete_source_announcement(source: str, source_id: int):
    """Deletes the raw announcement row from its own source DB, and drops
    any tracker entry pointing at it (with a shadow log entry first)."""
    cfg = AT_SOURCE_FIELD_MAP[source]
    src_db = DB_PATHS[source]
    try:
        sconn = sqlite3.connect(src_db)
        try:
            sconn.execute(f"DELETE FROM {cfg['table']} WHERE id = ?", (int(source_id),))
            sconn.commit()
        finally:
            sconn.close()
    except Exception:
        pass  # best-effort — source table shape can vary run to run

    conn = at_conn()
    try:
        trow = conn.execute(
            "SELECT * FROM announcement_tracker WHERE source = ? AND source_announcement_id = ?",
            (source, int(source_id)),
        ).fetchone()
        if trow:
            _at_shadow_log(conn, "DELETE", trow["id"], trow["master_id"], before=dict(trow))
            conn.execute("DELETE FROM announcement_tracker WHERE id = ?", (trow["id"],))
    finally:
        conn.close()


def at_update_tracker(tracker_id: int, fields: dict):
    conn = at_conn()
    try:
        before = conn.execute("SELECT * FROM announcement_tracker WHERE id = ?", (tracker_id,)).fetchone()
        if not before:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE announcement_tracker SET {cols}, updated_at = datetime('now','localtime') WHERE id = ?",
            (*fields.values(), tracker_id),
        )
        after = conn.execute("SELECT * FROM announcement_tracker WHERE id = ?", (tracker_id,)).fetchone()
        _at_shadow_log(conn, "UPDATE", tracker_id, before["master_id"], before=dict(before), after=dict(after))
    finally:
        conn.close()


def at_delete_tracker(tracker_id: int):
    conn = at_conn()
    try:
        before = conn.execute("SELECT * FROM announcement_tracker WHERE id = ?", (tracker_id,)).fetchone()
        if not before:
            return
        _at_shadow_log(conn, "DELETE", tracker_id, before["master_id"], before=dict(before))
        conn.execute("DELETE FROM announcement_tracker WHERE id = ?", (tracker_id,))
    finally:
        conn.close()


def at_update_master(master_id: int, fields: dict):
    conn = at_conn()
    try:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(
            f"UPDATE announcement_master SET {cols}, updated_at = datetime('now','localtime') WHERE id = ?",
            (*fields.values(), master_id),
        )
    finally:
        conn.close()


def at_delete_master(master_id: int):
    """Cascading delete: master + every tracker row under it (shadow-logged)."""
    conn = at_conn()
    try:
        for trow in conn.execute("SELECT * FROM announcement_tracker WHERE master_id = ?", (master_id,)).fetchall():
            _at_shadow_log(conn, "DELETE", trow["id"], master_id, before=dict(trow))
        conn.execute("DELETE FROM announcement_tracker WHERE master_id = ?", (master_id,))
        conn.execute("DELETE FROM announcement_master WHERE id = ?", (master_id,))
    finally:
        conn.close()


def at_query(sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = at_conn()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()


# ─── Announcement Tracker page — Search / Overview / Create (full CRUD) ────

def atp_go_search():
    st.session_state["atp_view"] = "search"
    st.session_state["atp_confirm_delete_master"] = None


def atp_go_create():
    st.session_state["atp_view"] = "create"


def atp_go_overview(master_id: int):
    st.session_state["atp_selected_master_id"] = master_id
    st.session_state["atp_view"] = "overview"


def atp_render_search():
    kw = st.text_input(
        "Search (company name, company code, BSE code, NSE code)",
        key="atp_search_kw", placeholder="e.g. Reliance, RELIANCE, 500325...",
    )
    if kw:
        masters = at_query(
            """SELECT * FROM announcement_master
               WHERE company_name LIKE ? OR company_code LIKE ? OR bse_code LIKE ? OR nse_code LIKE ?
               ORDER BY company_name""",
            tuple([f"%{kw}%"] * 4),
        )
    else:
        masters = at_query("SELECT * FROM announcement_master ORDER BY company_name")

    counts = at_query("SELECT master_id, COUNT(*) AS n FROM announcement_tracker GROUP BY master_id")
    count_map = dict(zip(counts["master_id"], counts["n"])) if not counts.empty else {}

    st.caption(f"{len(masters)} companies in the Announcement Master")
    if masters.empty:
        st.info("No companies tracked yet. Use ➕ New, or hit 📌 Track on an announcement in the Announcements tab.")
        return

    hcols = st.columns([2.6, 1.2, 1.0, 1.0, 1.0, 1.6])
    for col, label in zip(hcols, ["Company", "Company code", "BSE code", "NSE code", "Tracked #", "Actions"]):
        col.markdown(f"**{label}**")
    st.divider()

    confirm_key = "atp_confirm_delete_master"
    for _, m in masters.iterrows():
        mid = int(m["id"])
        cols = st.columns([2.6, 1.2, 1.0, 1.0, 1.0, 1.6])
        cols[0].write(m["company_name"])
        cols[1].write(m["company_code"] or "—")
        cols[2].write(m["bse_code"] or "—")
        cols[3].write(m["nse_code"] or "—")
        cols[4].write(str(count_map.get(mid, 0)))

        if st.session_state.get(confirm_key) == mid:
            wc1, wc2 = cols[5].columns(2)
            if wc1.button("✅", key=f"atp_mconf_{mid}"):
                at_delete_master(mid)
                st.session_state[confirm_key] = None
                st.rerun()
            if wc2.button("✖", key=f"atp_mcancel_{mid}"):
                st.session_state[confirm_key] = None
                st.rerun()
        else:
            wc1, wc2 = cols[5].columns(2)
            if wc1.button("👁️ Open", key=f"atp_mopen_{mid}", use_container_width=True):
                atp_go_overview(mid)
                st.rerun()
            if wc2.button("🗑️", key=f"atp_mdel_{mid}", help="Delete company + all tracked entries", use_container_width=True):
                st.session_state[confirm_key] = mid
                st.rerun()
        st.divider()


def atp_render_create():
    st.markdown("**Create a company in the Announcement Master**")
    with st.form("atp_create_master_form"):
        c1, c2 = st.columns(2)
        company_name = c1.text_input("Company name *")
        company_code = c2.text_input("Company code")
        c3, c4 = st.columns(2)
        bse_code = c3.text_input("BSE code")
        nse_code = c4.text_input("NSE code")
        long_text = st.text_area("Long text (company notes / description)", height=100)
        submitted = st.form_submit_button("Create company", type="primary")
        if submitted:
            if not company_name.strip():
                st.error("Company name is required.")
            else:
                mid = at_get_or_create_master(company_name, company_code, bse_code, nse_code)
                if long_text.strip():
                    at_update_master(mid, {"long_text": long_text.strip()})
                st.success(f"Created/updated **{company_name}**.")
                atp_go_overview(mid)
                st.rerun()

    st.divider()
    st.markdown("**Manually add a tracked announcement to an existing company**")
    masters = at_query("SELECT id, company_name FROM announcement_master ORDER BY company_name")
    if masters.empty:
        st.caption("Create a company above first.")
        return
    with st.form("atp_create_tracker_form"):
        target = st.selectbox("Company", masters["company_name"].tolist(), key="atp_ct_company")
        mid = int(masters.loc[masters["company_name"] == target, "id"].iloc[0])
        c1, c2 = st.columns(2)
        source = c1.selectbox("Source", list(DB_PATHS.keys()), key="atp_ct_source")
        category = c2.text_input("Category", key="atp_ct_category")
        subcategory = st.text_input("Sub-category", key="atp_ct_subcategory")
        link = st.text_input("Link (attachment / document URL)", key="atp_ct_link")
        long_note = st.text_area("Long note", height=80, key="atp_ct_note")
        submitted2 = st.form_submit_button("Add tracker entry", type="primary")
        if submitted2:
            conn = at_conn()
            try:
                cur = conn.execute(
                    """INSERT INTO announcement_tracker
                       (master_id, source, company_name, category, subcategory, link, long_note)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (mid, source, target, category, subcategory, link, long_note),
                )
                _at_shadow_log(conn, "INSERT", cur.lastrowid, mid, after={
                    "source": source, "category": category, "subcategory": subcategory,
                    "link": link, "long_note": long_note,
                })
            finally:
                conn.close()
            st.success("Tracker entry added.")
            atp_go_overview(mid)
            st.rerun()


def atp_render_tracker_row(t: pd.Series):
    tid = int(t["id"])
    label = t["category"] or t["source"] or "Tracker entry"
    with st.expander(f"#{tid} · {label} · {t['source'] or '—'}"):
        with st.form(f"atp_trk_edit_{tid}"):
            c1, c2 = st.columns(2)
            category = c1.text_input("Category", value=t["category"] or "", key=f"atp_cat_{tid}")
            subcategory = c2.text_input("Sub-category", value=t["subcategory"] or "", key=f"atp_subcat_{tid}")
            link = st.text_input("Link", value=t["link"] or "", key=f"atp_link_{tid}")

            st.markdown("**Idea level — special situation**")
            sc1, sc2 = st.columns(2)
            is_ss = sc1.selectbox("Is a special situation?", ["No", "Yes"],
                                   index=(1 if t["is_special_situation"] == "Yes" else 0), key=f"atp_isss_{tid}")
            ss_type = sc2.selectbox("Type", AT_SPECIAL_SITUATION_TYPES,
                                     index=(AT_SPECIAL_SITUATION_TYPES.index(t["special_situation_type"])
                                            if t["special_situation_type"] in AT_SPECIAL_SITUATION_TYPES else 0),
                                     key=f"atp_sstype_{tid}")

            st.markdown("**Idea level — capex / new order**")
            cc1, cc2 = st.columns(2)
            is_capex = cc1.selectbox("Is Capex or New Order?", ["No", "Yes"],
                                      index=(1 if t["is_capex_or_order"] == "Yes" else 0), key=f"atp_iscap_{tid}")
            capex_type = cc2.selectbox("Type", AT_CAPEX_ORDER_TYPES,
                                        index=(AT_CAPEX_ORDER_TYPES.index(t["capex_order_type"])
                                               if t["capex_order_type"] in AT_CAPEX_ORDER_TYPES else 0),
                                        key=f"atp_captype_{tid}")

            st.markdown("**Idea level — JV / new tech**")
            jc1, jc2 = st.columns(2)
            is_jv = jc1.selectbox("Is JV or New Tech Transfer?", ["No", "Yes"],
                                   index=(1 if t["is_jv_or_tech"] == "Yes" else 0), key=f"atp_isjv_{tid}")
            jv_type = jc2.selectbox("Type", AT_JV_TECH_TYPES,
                                     index=(AT_JV_TECH_TYPES.index(t["jv_tech_type"])
                                            if t["jv_tech_type"] in AT_JV_TECH_TYPES else 0),
                                     key=f"atp_jvtype_{tid}")

            ic1, ic2 = st.columns(2)
            industry = ic1.text_input("Industry", value=t["industry"] or "", key=f"atp_ind_{tid}")
            sub_industry = ic2.text_input("Sub-industry", value=t["sub_industry"] or "", key=f"atp_subind_{tid}")

            long_note = st.text_area("Long note", value=t["long_note"] or "", height=90, key=f"atp_note_{tid}")

            rc1, rc2 = st.columns([1, 3])
            reg_sent = rc1.selectbox("Regulatory policy", AT_REG_SENTIMENTS,
                                      index=(AT_REG_SENTIMENTS.index(t["regulatory_sentiment"])
                                             if t["regulatory_sentiment"] in AT_REG_SENTIMENTS else 0),
                                      key=f"atp_regsent_{tid}")
            reg_notes = rc2.text_input("Regulatory notes", value=t["regulatory_notes"] or "", key=f"atp_regnotes_{tid}")

            risk_notes = st.text_area("Risk assessment notes", value=t["risk_notes"] or "", height=80, key=f"atp_risk_{tid}")

            if t["json_data"]:
                with st.container(border=True):
                    st.caption("Raw announcement JSON")
                    st.json(t["json_data"], expanded=False)

            save_col, del_col = st.columns([1, 1])
            saved = save_col.form_submit_button("💾 Save", type="primary", use_container_width=True)
            deleted = del_col.form_submit_button("🗑️ Delete entry", use_container_width=True)

            if saved:
                at_update_tracker(tid, {
                    "category": category, "subcategory": subcategory, "link": link,
                    "is_special_situation": is_ss, "special_situation_type": ss_type,
                    "is_capex_or_order": is_capex, "capex_order_type": capex_type,
                    "is_jv_or_tech": is_jv, "jv_tech_type": jv_type,
                    "industry": industry, "sub_industry": sub_industry,
                    "long_note": long_note, "regulatory_sentiment": reg_sent,
                    "regulatory_notes": reg_notes, "risk_notes": risk_notes,
                })
                st.success("Saved.")
                st.rerun()
            if deleted:
                at_delete_tracker(tid)
                st.success("Tracker entry deleted.")
                st.rerun()


def atp_render_overview():
    mid = st.session_state["atp_selected_master_id"]
    st.button("← Back to search", on_click=atp_go_search, key="atp_back_btn")

    m_df = at_query("SELECT * FROM announcement_master WHERE id = ?", (mid,))
    if m_df.empty:
        st.error("Company not found.")
        return
    m = m_df.iloc[0]

    st.subheader(m["company_name"])
    with st.form("atp_master_edit_form"):
        c1, c2, c3 = st.columns(3)
        company_code = c1.text_input("Company code", value=m["company_code"] or "")
        bse_code = c2.text_input("BSE code", value=m["bse_code"] or "")
        nse_code = c3.text_input("NSE code", value=m["nse_code"] or "")
        long_text = st.text_area("Long text", value=m["long_text"] or "", height=90)
        if st.form_submit_button("💾 Save company info"):
            at_update_master(mid, {
                "company_code": company_code, "bse_code": bse_code,
                "nse_code": nse_code, "long_text": long_text,
            })
            st.success("Saved.")
            st.rerun()

    st.divider()
    trackers = at_query("SELECT * FROM announcement_tracker WHERE master_id = ? ORDER BY created_at DESC", (mid,))
    st.markdown(f"**Tracked announcements ({len(trackers)})**")
    if trackers.empty:
        st.info("Nothing tracked for this company yet.")
    else:
        for _, t in trackers.iterrows():
            atp_render_tracker_row(t)


def render_announcement_tracker_page():
    st.markdown("""<div class="page-head">
      <h1>🗂️ Announcement Tracker</h1>
      <p>Company master ↔ tracked announcements (1:N) — idea flags, industry, regulatory & risk notes</p>
    </div>""", unsafe_allow_html=True)

    at_init_db()
    st.session_state.setdefault("atp_view", "search")
    st.session_state.setdefault("atp_selected_master_id", None)
    st.session_state.setdefault("atp_confirm_delete_master", None)

    if st.session_state["atp_view"] != "overview":
        nav1, nav2, _ = st.columns([1, 1, 4])
        nav1.button("🔍 Search", use_container_width=True, key="atp_nav_search",
                    type="primary" if st.session_state["atp_view"] == "search" else "secondary",
                    on_click=atp_go_search)
        nav2.button("➕ New", use_container_width=True, key="atp_nav_create",
                    type="primary" if st.session_state["atp_view"] == "create" else "secondary",
                    on_click=atp_go_create)
        st.divider()

    if st.session_state["atp_view"] == "overview" and st.session_state["atp_selected_master_id"] is not None:
        atp_render_overview()
    elif st.session_state["atp_view"] == "create":
        atp_render_create()
    else:
        atp_render_search()


# ═════════════════════════════════════════════════════════════════════════════
#  GUIDED ACTIVITY — HELPERS
#
#  Wires the two standalone scripts together, in-process:
#    ① scrape BSE/NSE  → market_announcements.py  (fetch_bse_equity / fetch_bse_sme / fetch_nse)
#    ② score BSE Equity rows for "idea" categories → idea_rules.py  (IDEA_TYPES taxonomy)
#
#  Scoring writes into the SAME idea_groups / idea_types / announcement_idea_scores
#  tables that render_idea_board() (Announcements → BSE Equity → Idea Board) reads —
#  so this is the in-app replacement for running announcement_ideas_pipeline.py by hand.
#  All keys/functions are prefixed ga_ to avoid clashing with the rest of the dashboard.
# ═════════════════════════════════════════════════════════════════════════════

GA_SOURCE_LABELS = {
    "bse_equity": "BSE Equity", "bse_sme": "BSE SME",
    "nse_equity": "NSE Equity", "nse_sme": "NSE SME",
}

GA_IDEA_SCORES_DDL = """
CREATE TABLE IF NOT EXISTS idea_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    sort_order INTEGER
);
CREATE TABLE IF NOT EXISTS idea_types (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES idea_groups(id),
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    sort_order  INTEGER
);
CREATE TABLE IF NOT EXISTS announcement_idea_scores (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    announcement_id  INTEGER NOT NULL REFERENCES announcements(id),
    idea_type_id     INTEGER NOT NULL REFERENCES idea_types(id),
    score            REAL NOT NULL,
    matched_keywords TEXT,
    scored_at        TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE (announcement_id, idea_type_id)
);
CREATE INDEX IF NOT EXISTS idx_idea_scores_ann  ON announcement_idea_scores(announcement_id);
CREATE INDEX IF NOT EXISTS idx_idea_scores_type ON announcement_idea_scores(idea_type_id);
"""


def ga_fmt_ddmmyyyy(d) -> str:
    """date/datetime.date -> 'DD-MM-YYYY' (the format market_announcements.py expects)."""
    return d.strftime("%d-%m-%Y")


def ga_run_scrape(sources: list, from_date, to_date, bse_eq_symbols=None,
                   bse_sme_ann=True, bse_sme_corp=True) -> list:
    """Run market_announcements.py's fetchers in-process for the chosen sources.
    Returns a list of result dicts: {source, label, fetched, inserted, db_path, error}."""
    fd, td = ga_fmt_ddmmyyyy(from_date), ga_fmt_ddmmyyyy(to_date)
    results = []
    for src in sources:
        label = GA_SOURCE_LABELS[src]
        try:
            if src == "bse_equity":
                ma.bse_equity_init()
                f, i = ma.fetch_bse_equity(fd, td, bse_eq_symbols or None)
            elif src == "bse_sme":
                ma.bse_sme_init()
                f = ma.fetch_bse_sme(fd, td, do_ann=bse_sme_ann, do_corp=bse_sme_corp)
                i = f  # fetch_bse_sme returns new-rows count directly
            else:  # nse_equity / nse_sme
                f, i = ma.fetch_nse(src, fd, td)
            results.append({"source": src, "label": label, "fetched": f, "inserted": i,
                             "db_path": ma.DB_PATHS[src], "error": ""})
        except Exception as e:
            results.append({"source": src, "label": label, "fetched": 0, "inserted": 0,
                             "db_path": ma.DB_PATHS.get(src, ""), "error": str(e)})
    return results


def ga_ensure_idea_tables(db_path: str):
    """Create idea_groups / idea_types / announcement_idea_scores in the BSE
    Equity DB (if missing) and (re)seed idea_groups/idea_types from idea_rules.py.

    Uses check-then-insert/update rather than `ON CONFLICT` throughout: a DB
    that already has these tables from an older/manual run of
    announcement_ideas_pipeline.py may not have a UNIQUE constraint on `name`
    (CREATE TABLE IF NOT EXISTS doesn't retrofit constraints onto an existing
    table), and ON CONFLICT raises OperationalError when there's no matching
    unique constraint to target. This approach works either way.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(GA_IDEA_SCORES_DDL)
        conn.commit()

        # idea_groups
        group_ids = {}
        for i, gname in enumerate(idea_rules.GROUPS):
            row = conn.execute("SELECT id FROM idea_groups WHERE name = ?", (gname,)).fetchone()
            if row:
                gid = row[0]
                conn.execute("UPDATE idea_groups SET sort_order = ? WHERE id = ?", (i, gid))
            else:
                gid = conn.execute(
                    "INSERT INTO idea_groups (name, sort_order) VALUES (?, ?)", (gname, i)
                ).lastrowid
            group_ids[gname] = gid
        conn.commit()

        # idea_types
        for i, (tname, cfg) in enumerate(idea_rules.IDEA_TYPES.items()):
            gid = group_ids.get(cfg["group"])
            row = conn.execute("SELECT id FROM idea_types WHERE name = ?", (tname,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE idea_types SET group_id = ?, description = ?, sort_order = ? WHERE id = ?",
                    (gid, cfg["description"], i, row[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO idea_types (group_id, name, description, sort_order) VALUES (?, ?, ?, ?)",
                    (gid, tname, cfg["description"], i),
                )
        conn.commit()

        # Best-effort: add a real UNIQUE index going forward, if the data
        # already in the table allows it (silently skip if it doesn't —
        # e.g. pre-existing duplicate names from before this fix).
        for stmt in (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_idea_groups_name ON idea_groups(name)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_idea_types_name ON idea_types(name)",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()


def ga_score_announcements(db_path: str, date_start, date_end, idea_type_names=None,
                            min_score=None, category_bonus=None, force_rescore=False) -> dict:
    """Score a source DB's `announcements` rows (via the normalized
    v_idea_source view — works for BSE Equity, BSE SME, NSE Equity or NSE SME)
    against the idea_rules.py taxonomy and upsert into announcement_idea_scores.

    Rule (matches idea_rules.py's documented scoring exactly):
      raw score = sum(weight) of every keyword phrase found in the subject
                  (case-insensitive substring) + one category_bonus if the
                  category/subcategory text matches any of the idea's
                  category_hints. Any `negative` phrase present zeroes/skips
                  the idea entirely. Rows scoring >= min_score are recorded.
    """
    min_score = idea_rules.MIN_SCORE_THRESHOLD if min_score is None else min_score
    category_bonus = idea_rules.CATEGORY_BONUS_WEIGHT if category_bonus is None else category_bonus
    idea_names = idea_type_names or list(idea_rules.IDEA_TYPES.keys())

    if _IDEA_PIPELINE_OK:
        _view_conn = sqlite3.connect(db_path)
        try:
            idea_pipeline.ensure_source_view(_view_conn)
        finally:
            _view_conn.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        type_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM idea_types")}
        rows = conn.execute(
            f"SELECT id, subject, category, subcategory FROM {IDEA_SOURCE_VIEW} "
            "WHERE DATE(input_timestamp) BETWEEN ? AND ?",
            [str(date_start), str(date_end)],
        ).fetchall()

        n_scores_written, n_rows_matched = 0, 0
        per_type = Counter()

        for row in rows:
            ann_id  = row["id"]
            subject = (row["subject"] or "").lower()
            cat_txt = f'{row["category"] or ""} {row["subcategory"] or ""}'.lower()
            row_matched = False

            for idea_name in idea_names:
                cfg = idea_rules.IDEA_TYPES.get(idea_name)
                type_id = type_ids.get(idea_name)
                if cfg is None or type_id is None:
                    continue

                existing = conn.execute(
                    "SELECT id FROM announcement_idea_scores WHERE announcement_id=? AND idea_type_id=?",
                    (ann_id, type_id),
                ).fetchone()
                if existing and not force_rescore:
                    continue  # already scored — skip unless force re-score

                if any(neg in subject for neg in cfg.get("negative", [])):
                    continue  # disqualified

                score, matched_kws = 0.0, []
                for phrase, weight in cfg["keywords"]:
                    if phrase in subject:
                        score += weight
                        matched_kws.append(phrase)

                for hint in cfg.get("category_hints", []):
                    if hint in cat_txt:
                        score += category_bonus
                        matched_kws.append(f"[category] {hint}")
                        break  # bonus applies once per idea, not per matching hint

                if score >= min_score:
                    if existing:
                        conn.execute(
                            "UPDATE announcement_idea_scores "
                            "SET score=?, matched_keywords=?, scored_at=datetime('now','localtime') WHERE id=?",
                            (score, json.dumps(matched_kws), existing["id"]),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO announcement_idea_scores "
                            "(announcement_id, idea_type_id, score, matched_keywords) VALUES (?, ?, ?, ?)",
                            (ann_id, type_id, score, json.dumps(matched_kws)),
                        )
                    n_scores_written += 1
                    per_type[idea_name] += 1
                    row_matched = True

            if row_matched:
                n_rows_matched += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "rows_considered": len(rows),
        "rows_matched":    n_rows_matched,
        "scores_written":  n_scores_written,
        "per_type":        dict(per_type),
    }


# ═════════════════════════════════════════════════════════════════════════════
#  GUIDED DB CLEAN-UP — HELPERS
#
#  A guided, date-range-scoped housekeeping tool for every DB the app owns:
#    • BSE Equity / BSE SME / NSE Equity / NSE SME  (raw announcements +
#      corp actions, plus the Idea Board & Tracker tables that live inside
#      each of those same DB files)
#    • Announcement Tracker  (announcement_tracker.db — the cross-source
#      master/tracker; its rows are only removed if the raw row they point
#      to is being removed in the same run, and only when explicitly opted
#      into via the "also clear linked Tracker entries" checkbox)
#
#  Flow is dry-run-first: step ② always counts before step ③ can delete
#  anything, and step ③ is gated behind a typed "DELETE" confirmation that
#  is invalidated the moment the scope or date range changes, so a stale
#  preview can never be used to authorize a different delete.
#  All keys/functions are prefixed dbc_ to avoid clashing with the rest of
#  the dashboard.
# ═════════════════════════════════════════════════════════════════════════════

# Which table(s) hold dated rows for each source, which column holds the
# date, and which child tables (same DB file, FK'd on announcement_id) must
# be purged first when their parent "announcements" rows disappear.
DBC_SOURCE_TABLES = {
    "BSE Equity": [
        {"table": "announcements", "date_col": "input_timestamp", "label": "Announcements",
         "children": ["announcement_idea_scores", "announcement_notes", "announcement_status", "watchlist"]},
    ],
    "BSE SME": [
        {"table": "announcements", "date_col": "announce_date", "label": "Announcements",
         "children": ["announcement_idea_scores", "announcement_notes", "announcement_status", "watchlist"]},
        {"table": "corp_actions", "date_col": "ex_date", "label": "Corp Actions", "children": []},
    ],
    "NSE Equity": [
        {"table": "announcements", "date_col": "ann_date", "label": "Announcements",
         "children": ["announcement_idea_scores", "announcement_notes", "announcement_status", "watchlist"]},
    ],
    "NSE SME": [
        {"table": "announcements", "date_col": "ann_date", "label": "Announcements",
         "children": ["announcement_idea_scores", "announcement_notes", "announcement_status", "watchlist"]},
    ],
}

DBC_CLEANUP_LOG_DDL = """
CREATE TABLE IF NOT EXISTS cleanup_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at       TEXT DEFAULT (datetime('now','localtime')),
    actor        TEXT,
    date_start   TEXT,
    date_end     TEXT,
    scope_json   TEXT,
    result_json  TEXT,
    vacuumed     TEXT
);
"""


def dbc_init_log():
    conn = sqlite3.connect(AUTH_DB)
    try:
        conn.executescript(DBC_CLEANUP_LOG_DDL)
        conn.commit()
    finally:
        conn.close()


def _dbc_table_exists(conn, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _dbc_quote_path(path: str) -> str:
    return str(path).replace("'", "''")


def dbc_count_matches(db_path: str, table: str, date_col: str, date_start, date_end) -> int:
    """Dry-run row count for one table within [date_start, date_end] inclusive."""
    if not db_path or not Path(db_path).exists():
        return 0
    conn = sqlite3.connect(db_path)
    try:
        if not _dbc_table_exists(conn, table):
            return 0
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE DATE({date_col}) BETWEEN ? AND ?",
            (str(date_start), str(date_end)),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def dbc_count_children(db_path: str, ann_table: str, date_col: str, date_start, date_end, children: list) -> dict:
    """For each child table FK'd on announcement_id, count rows that would be
    cascaded away if the matching parent rows were deleted."""
    out = {c: 0 for c in children}
    if not db_path or not Path(db_path).exists():
        return out
    conn = sqlite3.connect(db_path)
    try:
        if not _dbc_table_exists(conn, ann_table):
            return out
        for child in children:
            if not _dbc_table_exists(conn, child):
                continue
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {child} WHERE announcement_id IN "
                    f"(SELECT id FROM {ann_table} WHERE DATE({date_col}) BETWEEN ? AND ?)",
                    (str(date_start), str(date_end)),
                ).fetchone()
                out[child] = int(row[0]) if row else 0
            except sqlite3.OperationalError:
                out[child] = 0
    finally:
        conn.close()
    return out


def dbc_count_tracker_links(scope_sources: list, date_start, date_end) -> int:
    """Count rows in announcement_tracker.db's cross-source tracker whose
    `source` is in-scope and whose linked source_announcement_id falls
    inside the date range of that source's own announcements table."""
    if not Path(AT_DB_PATH).exists():
        return 0
    total = 0
    conn = sqlite3.connect(AT_DB_PATH)
    try:
        if not _dbc_table_exists(conn, "announcement_tracker"):
            return 0
        for label in scope_sources:
            src_db = DB_PATHS.get(label)
            fmap = AT_SOURCE_FIELD_MAP.get(label)
            if not src_db or not fmap or not Path(src_db).exists():
                continue
            try:
                conn.execute(f"ATTACH DATABASE '{_dbc_quote_path(src_db)}' AS dbc_src")
                row = conn.execute(
                    f"SELECT COUNT(*) FROM announcement_tracker t WHERE t.source = ? "
                    f"AND t.source_announcement_id IN "
                    f"(SELECT id FROM dbc_src.{fmap['table']} WHERE DATE({fmap['date']}) BETWEEN ? AND ?)",
                    (label, str(date_start), str(date_end)),
                ).fetchone()
                total += int(row[0]) if row else 0
            except sqlite3.OperationalError:
                pass
            finally:
                try:
                    conn.execute("DETACH DATABASE dbc_src")
                except sqlite3.OperationalError:
                    pass
    finally:
        conn.close()
    return total


def dbc_build_preview(scope: dict, date_start, date_end, cascade_tracker: bool) -> pd.DataFrame:
    """scope: {source_label: [table_name, ...]} of tables the user checked."""
    rows = []
    for label, tables in scope.items():
        db_path = DB_PATHS.get(label)
        cfg_by_table = {t["table"]: t for t in DBC_SOURCE_TABLES.get(label, [])}
        for table in tables:
            cfg = cfg_by_table.get(table)
            if not cfg:
                continue
            n = dbc_count_matches(db_path, table, cfg["date_col"], date_start, date_end)
            linked = 0
            if cfg["children"] and n:
                linked = sum(dbc_count_children(db_path, table, cfg["date_col"], date_start, date_end, cfg["children"]).values())
            rows.append({"Source": label, "Table": cfg["label"], "Matching rows": n, "Linked idea/tracker rows": linked})
    df = pd.DataFrame(rows, columns=["Source", "Table", "Matching rows", "Linked idea/tracker rows"])
    if cascade_tracker:
        ann_sources = [lbl for lbl, tabs in scope.items() if "announcements" in tabs]
        tracker_n = dbc_count_tracker_links(ann_sources, date_start, date_end) if ann_sources else 0
        df = pd.concat([df, pd.DataFrame([{
            "Source": "Announcement Tracker", "Table": "Linked tracker rows",
            "Matching rows": tracker_n, "Linked idea/tracker rows": 0,
        }])], ignore_index=True)
    return df


def dbc_scope_fingerprint(scope: dict, date_start, date_end, cascade_tracker: bool, vacuum: bool) -> str:
    payload = {
        "scope": {k: sorted(v) for k, v in sorted(scope.items())},
        "from": str(date_start), "to": str(date_end),
        "cascade_tracker": cascade_tracker, "vacuum": vacuum,
    }
    return json.dumps(payload, sort_keys=True)


def dbc_run_cleanup(scope: dict, date_start, date_end, cascade_tracker: bool, vacuum: bool, actor: str) -> dict:
    """Deletes matching rows (+ cascaded same-DB children) per source, then
    optionally the linked cross-source Tracker rows, then VACUUMs whichever
    DB files were touched. Each DB file's own deletes run inside one
    transaction; a failure on one DB is reported but doesn't roll back
    work already committed against another DB."""
    per_source, errors, vacuumed = [], [], []
    tracker_deleted = 0

    for label, tables in scope.items():
        db_path = DB_PATHS.get(label)
        if not db_path or not Path(db_path).exists():
            continue
        cfg_by_table = {t["table"]: t for t in DBC_SOURCE_TABLES.get(label, [])}
        conn = sqlite3.connect(db_path)
        try:
            touched = False
            for table in tables:
                cfg = cfg_by_table.get(table)
                if not cfg or not _dbc_table_exists(conn, table):
                    continue
                date_col = cfg["date_col"]
                ann_ids = [r[0] for r in conn.execute(
                    f"SELECT id FROM {table} WHERE DATE({date_col}) BETWEEN ? AND ?",
                    (str(date_start), str(date_end)),
                ).fetchall()]
                child_deleted = {}
                if ann_ids and cfg["children"]:
                    placeholders = ",".join("?" * len(ann_ids))
                    for child in cfg["children"]:
                        if not _dbc_table_exists(conn, child):
                            continue
                        cur = conn.execute(f"DELETE FROM {child} WHERE announcement_id IN ({placeholders})", ann_ids)
                        child_deleted[child] = cur.rowcount
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE DATE({date_col}) BETWEEN ? AND ?",
                    (str(date_start), str(date_end)),
                )
                per_source.append({
                    "source": label, "table": table, "table_label": cfg["label"],
                    "rows_deleted": cur.rowcount, "children_deleted": child_deleted, "ids": ann_ids,
                })
                touched = True
            conn.commit()
            if vacuum and touched:
                conn.execute("VACUUM")
                vacuumed.append(label)
        except Exception as e:
            conn.rollback()
            errors.append(f"{label}: {e}")
        finally:
            conn.close()

    if cascade_tracker and Path(AT_DB_PATH).exists():
        deleted_ids_by_source = {}
        for row in per_source:
            if row["table"] == "announcements":
                deleted_ids_by_source.setdefault(row["source"], []).extend(row["ids"])
        if deleted_ids_by_source:
            conn = sqlite3.connect(AT_DB_PATH)
            try:
                touched = False
                for label, ids in deleted_ids_by_source.items():
                    if not ids:
                        continue
                    placeholders = ",".join("?" * len(ids))
                    tr_ids = [r[0] for r in conn.execute(
                        f"SELECT id FROM announcement_tracker WHERE source=? AND source_announcement_id IN ({placeholders})",
                        [label, *ids],
                    ).fetchall()]
                    if tr_ids:
                        tph = ",".join("?" * len(tr_ids))
                        conn.execute(f"DELETE FROM announcement_tracker_shadow WHERE tracker_id IN ({tph})", tr_ids)
                        cur = conn.execute(f"DELETE FROM announcement_tracker WHERE id IN ({tph})", tr_ids)
                        tracker_deleted += cur.rowcount
                        touched = True
                conn.commit()
                if vacuum and touched:
                    conn.execute("VACUUM")
                    vacuumed.append("Announcement Tracker")
            except Exception as e:
                conn.rollback()
                errors.append(f"Announcement Tracker: {e}")
            finally:
                conn.close()

    result = {"per_source": per_source, "tracker_deleted": tracker_deleted, "vacuumed": vacuumed, "errors": errors}

    try:  # best-effort audit log — never let logging break a completed cleanup
        dbc_init_log()
        conn = sqlite3.connect(AUTH_DB)
        conn.execute(
            "INSERT INTO cleanup_log (actor, date_start, date_end, scope_json, result_json, vacuumed) VALUES (?,?,?,?,?,?)",
            (actor, str(date_start), str(date_end), json.dumps(scope), json.dumps(result, default=str), json.dumps(vacuumed)),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: ANNOUNCEMENTS
# ═════════════════════════════════════════════════════════════════════════════

if page == "Announcements":

    st.markdown("""<div class="page-head">
      <h1>🗎 Announcements</h1>
      <p>Search and export corporate filings across all four market databases</p>
    </div>""", unsafe_allow_html=True)

    # ── Global filters ────────────────────────────────────────────────────────
    st.markdown('<div class="filter-bar">', unsafe_allow_html=True)
    st.markdown('<div class="filter-bar-label">🔍 Global Filters</div>', unsafe_allow_html=True)

    f_row1 = st.columns([1, 1, 1, 1])
    from_date = f_row1[0].date_input("From Date", value=date.today() - timedelta(days=7))
    to_date   = f_row1[1].date_input("To Date",   value=date.today())
    keyword   = f_row1[2].text_input("Keyword / Symbol / Company", "")
    f_row1[3].markdown("<div class='field-spacer'></div>", unsafe_allow_html=True)
    if f_row1[3].button("Clear filters", use_container_width=True):
        st.rerun()

    st.markdown('</div>', unsafe_allow_html=True)

    # ── Per-source tabs ───────────────────────────────────────────────────────
    t_be, t_bs, t_ne, t_ns = st.tabs([
        "🔵 BSE Equity", "🟡 BSE SME", "🟢 NSE Equity", "🔴 NSE SME"
    ])

    # ── BSE EQUITY ────────────────────────────────────────────────────────────
    with t_be:
        _source_badge("BSE Equity")

        be_sub_ann, be_sub_idb, be_sub_trk = st.tabs(["📋 Announcements", "💡 Idea Board", "🗂️ Tracker"])

        with be_sub_ann:
            c = _conn("BSE Equity")
            cat_opts  = [""] + ([r["name"] for r in c.execute("SELECT name FROM categories ORDER BY name").fetchall()] if c else [])
            subcat_opts = [""] + ([r["name"] for r in c.execute("SELECT DISTINCT name FROM subcategories WHERE name IS NOT NULL AND name != '' ORDER BY name").fetchall()] if c else [])
            if c: c.close()

            sr1 = st.columns(2)
            be_cat    = sr1[0].selectbox("Category",    cat_opts,   key="be_cat")
            be_subcat = sr1[1].selectbox("Sub-Category", subcat_opts, key="be_subcat")

            df_be = load_bse_equity(from_date, to_date, keyword, be_cat, be_subcat)

            if df_be.empty:
                st.caption(f"{len(df_be):,} record(s)")
                st.info("No records found. Adjust filters above.")
            else:
                disp = df_be.rename(columns={
                    "scrip_code":"Code","symbol":"Symbol","company_name":"Company",
                    "category":"Category","subcategory":"Sub-Category",
                    "subject":"Subject","input_timestamp":"Timestamp","document_url":"Document",
                }).drop(columns=["file_name", "id"], errors="ignore")

                be_settings = _report_settings(
                    "be_table_cfg", list(disp.columns), default_height=500,
                    label=f"{len(df_be):,} record(s)",
                )
                be_view = _apply_report_settings(disp, be_settings)
                _, be_height, _, _ = be_settings

                ev = st.dataframe(
                    be_view, use_container_width=True, height=be_height,
                    on_select="rerun", selection_mode="multi-row", key="be_table",
                    column_config={"Document": st.column_config.LinkColumn("Document", display_text="📄", width="small")},
                )
                for idx in (ev.selection.rows if ev else []):
                    _log_view("BSE Equity", be_view.iloc[idx].to_dict())

                st.download_button("⬇ Download CSV", df_be.to_csv(index=False).encode(), "bse_equity_results.csv", "text/csv")
                _log_search("BSE Equity", {"keyword": keyword, "category": be_cat, "subcategory": be_subcat, "from": str(from_date), "to": str(to_date)}, len(df_be))

        with be_sub_idb:
            render_idea_board(DB_PATHS["BSE Equity"], "be", "BSE Equity")

        with be_sub_trk:
            render_tracker(DB_PATHS["BSE Equity"], "be")

    # ── BSE SME ───────────────────────────────────────────────────────────────
    with t_bs:
        _source_badge("BSE SME")

        bs_sub_ann, bs_sub_idb, bs_sub_trk = st.tabs(["📋 Announcements", "💡 Idea Board", "🗂️ Tracker"])

        with bs_sub_ann:
            sme_view = st.radio("Table", ["Announcements", "Corp Actions"], horizontal=True, key="bsesme_view")

            if sme_view == "Announcements":
                df_bs = load_bse_sme_ann(from_date, to_date, keyword)
                if df_bs.empty:
                    st.caption(f"{len(df_bs):,} record(s)")
                    st.info("No SME announcements found.")
                else:
                    disp_bs = df_bs.rename(columns={"scrip_code":"Code","scrip_name":"Company","grp":"Group","category":"Category","announce_date":"Date","end_date":"End","purpose":"Purpose","attachment_url":"Document"}).drop(columns=["id"], errors="ignore")
                    bs_settings = _report_settings(
                        "bs_ann_table_cfg", list(disp_bs.columns), default_height=500,
                        label=f"{len(df_bs):,} record(s)",
                    )
                    bs_view = _apply_report_settings(disp_bs, bs_settings)
                    _, bs_height, _, _ = bs_settings

                    ev2 = st.dataframe(
                        bs_view, use_container_width=True, height=bs_height,
                        on_select="rerun", selection_mode="multi-row", key="bs_ann_table",
                        column_config={"Document": st.column_config.LinkColumn("Document", display_text="📄", width="small")},
                    )
                    for idx in (ev2.selection.rows if ev2 else []):
                        _log_view("BSE SME", bs_view.iloc[idx].to_dict())
                    st.download_button("⬇ Download CSV", df_bs.to_csv(index=False).encode(), "bse_sme_ann.csv", "text/csv")
            else:
                df_bc = load_bse_sme_corp(from_date, to_date, keyword)
                if df_bc.empty:
                    st.caption(f"{len(df_bc):,} record(s)")
                    st.info("No corp actions found.")
                else:
                    disp_bc = df_bc.rename(columns={"scrip_code":"Code","scrip_name":"Company","grp":"Group","category":"Category","ex_date":"Ex-Date","record_date":"Record Date","end_date":"End","purpose":"Purpose"})
                    bc_settings = _report_settings(
                        "bs_corp_table_cfg", list(disp_bc.columns), default_height=500,
                        label=f"{len(df_bc):,} record(s)",
                    )
                    bc_view = _apply_report_settings(disp_bc, bc_settings)
                    _, bc_height, _, _ = bc_settings

                    st.dataframe(
                        bc_view, use_container_width=True, height=bc_height, hide_index=True,
                    )
                    st.download_button("⬇ Download CSV", df_bc.to_csv(index=False).encode(), "bse_sme_corp.csv", "text/csv")

            _log_search("BSE SME", {"keyword": keyword, "from": str(from_date), "to": str(to_date)}, len(df_bs) if sme_view == "Announcements" else 0)

        with bs_sub_idb:
            render_idea_board(DB_PATHS["BSE SME"], "bs", "BSE SME")

        with bs_sub_trk:
            render_tracker(DB_PATHS["BSE SME"], "bs")

    # ── NSE EQUITY ────────────────────────────────────────────────────────────
    with t_ne:
        _source_badge("NSE Equity")

        ne_sub_ann, ne_sub_idb, ne_sub_trk = st.tabs(["📋 Announcements", "💡 Idea Board", "🗂️ Tracker"])

        with ne_sub_ann:
            ne_sub = st.text_input("Subject filter", "", key="ne_sub")
            df_ne = load_nse("NSE Equity", from_date, to_date, keyword, ne_sub)
            if df_ne.empty:
                st.caption(f"{len(df_ne):,} record(s)")
                st.info("No records found.")
            else:
                disp_ne = df_ne.rename(columns={"symbol":"Symbol","company_name":"Company","subject":"Subject","description":"Description","ann_date":"Date","attachment_url":"Document"}).drop(columns=["id"], errors="ignore")
                ne_settings = _report_settings(
                    "ne_table_cfg", list(disp_ne.columns), default_height=500,
                    label=f"{len(df_ne):,} record(s)",
                )
                ne_view = _apply_report_settings(disp_ne, ne_settings)
                _, ne_height, _, _ = ne_settings

                ev3 = st.dataframe(
                    ne_view, use_container_width=True, height=ne_height,
                    on_select="rerun", selection_mode="multi-row", key="ne_table",
                    column_config={"Document": st.column_config.LinkColumn("Document", display_text="📄", width="small")},
                )
                for idx in (ev3.selection.rows if ev3 else []):
                    _log_view("NSE Equity", ne_view.iloc[idx].to_dict())
                st.download_button("⬇ Download CSV", df_ne.to_csv(index=False).encode(), "nse_equity_results.csv", "text/csv")
            _log_search("NSE Equity", {"keyword": keyword, "subject": ne_sub, "from": str(from_date), "to": str(to_date)}, len(df_ne))

        with ne_sub_idb:
            render_idea_board(DB_PATHS["NSE Equity"], "ne", "NSE Equity")

        with ne_sub_trk:
            render_tracker(DB_PATHS["NSE Equity"], "ne")

    # ── NSE SME ───────────────────────────────────────────────────────────────
    with t_ns:
        _source_badge("NSE SME")

        ns_sub_ann, ns_sub_idb, ns_sub_trk = st.tabs(["📋 Announcements", "💡 Idea Board", "🗂️ Tracker"])

        with ns_sub_ann:
            ns_sub = st.text_input("Subject filter", "", key="ns_sub")
            df_ns = load_nse("NSE SME", from_date, to_date, keyword, ns_sub)
            if df_ns.empty:
                st.caption(f"{len(df_ns):,} record(s)")
                st.info("No records found.")
            else:
                disp_ns = df_ns.rename(columns={"symbol":"Symbol","company_name":"Company","subject":"Subject","description":"Description","ann_date":"Date","attachment_url":"Document"}).drop(columns=["id"], errors="ignore")
                ns_settings = _report_settings(
                    "ns_table_cfg", list(disp_ns.columns), default_height=500,
                    label=f"{len(df_ns):,} record(s)",
                )
                ns_view = _apply_report_settings(disp_ns, ns_settings)
                _, ns_height, _, _ = ns_settings

                ev4 = st.dataframe(
                    ns_view, use_container_width=True, height=ns_height,
                    on_select="rerun", selection_mode="multi-row", key="ns_table",
                    column_config={"Document": st.column_config.LinkColumn("Document", display_text="📄", width="small")},
                )
                for idx in (ev4.selection.rows if ev4 else []):
                    _log_view("NSE SME", ns_view.iloc[idx].to_dict())
                st.download_button("⬇ Download CSV", df_ns.to_csv(index=False).encode(), "nse_sme_results.csv", "text/csv")
            _log_search("NSE SME", {"keyword": keyword, "subject": ns_sub, "from": str(from_date), "to": str(to_date)}, len(df_ns))

        with ns_sub_idb:
            render_idea_board(DB_PATHS["NSE SME"], "ns", "NSE SME")

        with ns_sub_trk:
            render_tracker(DB_PATHS["NSE SME"], "ns")

    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: ANNOUNCEMENT TRACKER
# ═════════════════════════════════════════════════════════════════════════════

elif page == "Announcement Tracker":
    render_announcement_tracker_page()
    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: CHARTS
# ═════════════════════════════════════════════════════════════════════════════

elif page == "Charts":

    st.markdown("""<div class="page-head">
      <h1>📊 Charts</h1>
      <p>Daily volume · category breakdown · source comparison · timeline</p>
    </div>""", unsafe_allow_html=True)

    st.markdown('<div class="filter-bar">', unsafe_allow_html=True)
    st.markdown('<div class="filter-bar-label">📅 Date Range</div>', unsafe_allow_html=True)
    cr = st.columns([1, 1, 2])
    ch_from = cr[0].date_input("From", value=date.today() - timedelta(days=30), key="ch_from")
    ch_to   = cr[1].date_input("To",   value=date.today(), key="ch_to")
    st.markdown('</div>', unsafe_allow_html=True)

    # ── Load counts per source ────────────────────────────────────────────────
    @st.cache_data(ttl=120)
    def chart_daily(src_key, db_key, date_col, from_dt, to_dt, extra_where=""):
        if not Path(DB_PATHS[src_key]).exists():
            return pd.DataFrame()
        sql = f"""
            SELECT DATE({date_col}) AS day, COUNT(*) AS n
            FROM   announcements
            WHERE  DATE({date_col}) BETWEEN ? AND ? {extra_where}
            GROUP  BY day ORDER BY day
        """
        return _df(src_key, sql, (str(from_dt), str(to_dt)))

    @st.cache_data(ttl=120)
    def chart_category(src_key, cat_col, from_dt, to_dt, date_col, extra_where=""):
        if not Path(DB_PATHS[src_key]).exists():
            return pd.DataFrame()
        tbl = "v_announcements" if src_key == "BSE Equity" else "announcements"
        sql = f"""
            SELECT {cat_col} AS category, COUNT(*) AS n
            FROM   {tbl}
            WHERE  DATE({date_col}) BETWEEN ? AND ? {extra_where}
            GROUP  BY {cat_col} ORDER BY n DESC LIMIT 15
        """
        return _df(src_key, sql, (str(from_dt), str(to_dt)))

    # BSE Equity daily
    df_be_d = chart_daily("BSE Equity", "bse_equity.db", "input_timestamp", ch_from, ch_to)
    # NSE Equity daily
    df_ne_d = chart_daily("NSE Equity", "nse_equity.db", "fetched_at", ch_from, ch_to)
    # NSE SME daily
    df_ns_d = chart_daily("NSE SME", "nse_sme.db", "fetched_at", ch_from, ch_to)

    # ── Chart 1: Cross-source daily comparison ────────────────────────────────
    st.markdown("#### Daily Announcement Volume — All Sources")

    frames = []
    for lbl, df_d in [("BSE Equity", df_be_d), ("NSE Equity", df_ne_d), ("NSE SME", df_ns_d)]:
        if not df_d.empty:
            tmp = df_d.copy(); tmp["source"] = lbl
            frames.append(tmp)

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        fig_line = px.line(
            combined, x="day", y="n", color="source",
            color_discrete_map=SOURCE_COLORS,
            labels={"day": "Date", "n": "Announcements", "source": "Source"},
            markers=True,
        )
        _plotly_defaults(fig_line, 360)
        st.plotly_chart(fig_line, use_container_width=True)
    else:
        st.info("No data available for the selected range.")

    st.markdown("---")

    # ── Chart 2: Per-source tabs ──────────────────────────────────────────────
    ct1, ct2, ct3, ct4 = st.tabs(["🔵 BSE Equity", "🟡 BSE SME", "🟢 NSE Equity", "🔴 NSE SME"])

    # BSE Equity
    with ct1:
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### Daily Volume")
            if df_be_d.empty:
                st.info("No data.")
            else:
                fig = px.bar(df_be_d, x="day", y="n", color_discrete_sequence=[SOURCE_COLORS["BSE Equity"]])
                _plotly_defaults(fig, 300)
                st.plotly_chart(fig, use_container_width=True)
        with col_b:
            st.markdown("##### By Category")
            df_be_c = chart_category("BSE Equity", "category", ch_from, ch_to, "input_timestamp")
            if df_be_c.empty:
                st.info("No data.")
            else:
                fig2 = px.pie(df_be_c, names="category", values="n", hole=0.45, height=300)
                fig2.update_layout(font_family="IBM Plex Sans", margin=dict(l=0,r=0,t=20,b=0))
                st.plotly_chart(fig2, use_container_width=True)

        # Summary metrics
        be_total = _df("BSE Equity","SELECT COUNT(*) n FROM announcements WHERE DATE(input_timestamp) BETWEEN ? AND ?",(str(ch_from),str(ch_to)))
        be_co    = _df("BSE Equity","SELECT COUNT(DISTINCT company_name) n FROM announcements WHERE DATE(input_timestamp) BETWEEN ? AND ?",(str(ch_from),str(ch_to)))
        m1,m2,m3 = st.columns(3)
        _metric(m1, int(be_total["n"].iloc[0]) if not be_total.empty else 0, "Announcements")
        _metric(m2, int(be_co["n"].iloc[0]) if not be_co.empty else 0, "Companies")
        _metric(m3, (ch_to - ch_from).days + 1, "Days in Range")

    # BSE SME
    with ct2:
        col_a, col_b = st.columns(2)
        c_sme = _conn("BSE SME")
        if c_sme:
            with col_a:
                st.markdown("##### Corp Actions by Category")
                df_corp_cat = pd.read_sql_query(
                    "SELECT category, COUNT(*) n FROM corp_actions WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC LIMIT 10",
                    c_sme)
                if df_corp_cat.empty: st.info("No corp actions.")
                else:
                    fig3 = px.bar(df_corp_cat, x="n", y="category", orientation="h",
                                  color_discrete_sequence=[SOURCE_COLORS["BSE SME"]], height=300)
                    _plotly_defaults(fig3, 300)
                    st.plotly_chart(fig3, use_container_width=True)
            with col_b:
                st.markdown("##### Announcements by Category")
                df_ann_cat = pd.read_sql_query(
                    "SELECT category, COUNT(*) n FROM announcements WHERE category IS NOT NULL GROUP BY category ORDER BY n DESC LIMIT 10",
                    c_sme)
                if df_ann_cat.empty: st.info("No announcements.")
                else:
                    fig4 = px.pie(df_ann_cat, names="category", values="n", hole=0.45, height=300)
                    fig4.update_layout(font_family="IBM Plex Sans", margin=dict(l=0,r=0,t=20,b=0))
                    st.plotly_chart(fig4, use_container_width=True)

            ann_cnt  = c_sme.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
            corp_cnt = c_sme.execute("SELECT COUNT(*) FROM corp_actions").fetchone()[0]
            c_sme.close()
            m1,m2,m3 = st.columns(3)
            _metric(m1, f"{ann_cnt:,}", "Announcements (total)")
            _metric(m2, f"{corp_cnt:,}", "Corp Actions (total)")
            _metric(m3, "bse_sme.db", "Source DB")
        else:
            st.info("bse_sme.db not found.")

    # NSE Equity
    with ct3:
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### Daily Volume")
            if df_ne_d.empty: st.info("No data.")
            else:
                fig5 = px.bar(df_ne_d, x="day", y="n", color_discrete_sequence=[SOURCE_COLORS["NSE Equity"]])
                _plotly_defaults(fig5, 300)
                st.plotly_chart(fig5, use_container_width=True)
        with col_b:
            st.markdown("##### Top 10 Companies by Announcement Count")
            df_ne_co = _df("NSE Equity",
                "SELECT company_name, COUNT(*) n FROM announcements WHERE DATE(fetched_at) BETWEEN ? AND ? GROUP BY company_name ORDER BY n DESC LIMIT 10",
                (str(ch_from), str(ch_to)))
            if df_ne_co.empty: st.info("No data.")
            else:
                fig6 = px.bar(df_ne_co, x="n", y="company_name", orientation="h",
                              color_discrete_sequence=[SOURCE_COLORS["NSE Equity"]], height=300)
                _plotly_defaults(fig6, 300)
                st.plotly_chart(fig6, use_container_width=True)

        ne_tot = _df("NSE Equity","SELECT COUNT(*) n FROM announcements")
        ne_co  = _df("NSE Equity","SELECT COUNT(DISTINCT company_name) n FROM announcements")
        ne_sub_cnt = _df("NSE Equity","SELECT COUNT(DISTINCT subject) n FROM announcements")
        m1,m2,m3 = st.columns(3)
        _metric(m1, int(ne_tot["n"].iloc[0]) if not ne_tot.empty else 0, "Total in DB")
        _metric(m2, int(ne_co["n"].iloc[0]) if not ne_co.empty else 0, "Companies")
        _metric(m3, int(ne_sub_cnt["n"].iloc[0]) if not ne_sub_cnt.empty else 0, "Unique Subjects")

    # NSE SME
    with ct4:
        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### Daily Volume")
            if df_ns_d.empty: st.info("No data.")
            else:
                fig7 = px.bar(df_ns_d, x="day", y="n", color_discrete_sequence=[SOURCE_COLORS["NSE SME"]])
                _plotly_defaults(fig7, 300)
                st.plotly_chart(fig7, use_container_width=True)
        with col_b:
            st.markdown("##### Top 10 Companies")
            df_ns_co = _df("NSE SME",
                "SELECT company_name, COUNT(*) n FROM announcements GROUP BY company_name ORDER BY n DESC LIMIT 10",())
            if df_ns_co.empty: st.info("No data.")
            else:
                fig8 = px.bar(df_ns_co, x="n", y="company_name", orientation="h",
                              color_discrete_sequence=[SOURCE_COLORS["NSE SME"]], height=300)
                _plotly_defaults(fig8, 300)
                st.plotly_chart(fig8, use_container_width=True)

        ns_tot = _df("NSE SME","SELECT COUNT(*) n FROM announcements")
        ns_co  = _df("NSE SME","SELECT COUNT(DISTINCT company_name) n FROM announcements")
        m1,m2 = st.columns(2)
        _metric(m1, int(ns_tot["n"].iloc[0]) if not ns_tot.empty else 0, "Total in DB")
        _metric(m2, int(ns_co["n"].iloc[0]) if not ns_co.empty else 0, "Companies")

    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: INSIGHTS
# ═════════════════════════════════════════════════════════════════════════════

elif page == "Insights":

    st.markdown("""<div class="page-head">
      <h1>💡 Insights</h1>
      <p>Keyword frequency · Trigger flags · Clusters · AI digest — per source</p>
    </div>""", unsafe_allow_html=True)

    # ── Filter bar ────────────────────────────────────────────────────────────
    st.markdown('<div class="filter-bar">', unsafe_allow_html=True)
    st.markdown('<div class="filter-bar-label">Filters</div>', unsafe_allow_html=True)

    fi1 = st.columns([1, 1, 1.5, 1.5, 1])
    ins_from  = fi1[0].date_input("From", value=date.today() - timedelta(days=30), key="ins_from")
    ins_to    = fi1[1].date_input("To",   value=date.today(), key="ins_to")
    ins_src   = fi1[2].selectbox("Source", list(DB_PATHS.keys()), key="ins_src")
    ins_kw    = fi1[3].text_input("Keyword / Symbol", "", key="ins_kw")
    n_kw      = fi1[4].slider("Top N keywords", 10, 50, 25)

    fi2 = st.columns([2, 1, 1])
    inc_bg = fi2[0].toggle("Include bigrams (2-word phrases)", value=True)
    fi2[1].markdown("<div class='field-spacer'></div>", unsafe_allow_html=True)
    run = fi2[1].button("▶  Run analysis", type="primary", use_container_width=True)
    st.markdown('</div>', unsafe_allow_html=True)

    if not run:
        st.info("Set your filters above and click **Run analysis** to begin.", icon="👈")
        st.stop()

    # ── Load the text column depending on source ──────────────────────────────
    with st.spinner("Loading data…"):
        if ins_src == "BSE Equity":
            df = load_bse_equity(ins_from, ins_to, ins_kw)
            text_col = "subject"
            name_col = "company_name"
            cat_col  = "category"
            sub_col  = "subcategory"
        elif ins_src == "BSE SME":
            df = load_bse_sme_ann(ins_from, ins_to, ins_kw)
            text_col = "purpose"
            name_col = "scrip_name"
            cat_col  = "category"
            sub_col  = "grp"
        else:
            df = load_nse(ins_src, ins_from, ins_to, ins_kw)
            text_col = "description"
            name_col = "company_name"
            cat_col  = "subject"
            sub_col  = "subject"

    if df.empty:
        st.warning("No announcements found. Try broadening the date range or removing filters.")
        st.stop()

    # Enrich
    df["triggers"] = df[text_col].apply(flag_triggers)
    df["cluster"]  = df[text_col].apply(assign_cluster)

    flagged_count    = df["triggers"].apply(bool).sum()
    unique_companies = df[name_col].nunique() if name_col in df.columns else "—"
    cat_count        = df[cat_col].nunique() if cat_col in df.columns else "—"

    _log_search(ins_src, {"from": str(ins_from), "to": str(ins_to), "keyword": ins_kw}, len(df))

    # ── Summary metrics ───────────────────────────────────────────────────────
    m1,m2,m3,m4 = st.columns(4)
    _metric(m1, f"{len(df):,}", "Announcements")
    _metric(m2, str(unique_companies), "Companies")
    _metric(m3, str(cat_count), "Categories / Subjects")
    _metric(m4, f"{flagged_count:,}", "Trigger-Flagged")
    st.markdown("<br>", unsafe_allow_html=True)

    # ── Insight tabs ──────────────────────────────────────────────────────────
    itab1, itab2, itab3, itab4 = st.tabs([
        "📈 Keyword Frequency", "🚩 Trigger Flags", "🗂 Clusters", "🤖 AI Digest"
    ])

    # TAB 1 — Keywords
    with itab1:
        st.markdown(f"#### Top terms in `{text_col}` field")
        st.caption(f"{len(df):,} announcements · stop words removed · source: **{ins_src}**")
        terms = top_terms(df[text_col], n=n_kw, include_bg=inc_bg)
        if not terms:
            st.warning("No tokens extracted. The text field may be empty.")
        else:
            term_df = pd.DataFrame(terms, columns=["term","count"])
            fig_kw = px.bar(
                term_df.sort_values("count"), x="count", y="term", orientation="h",
                color="count", color_continuous_scale="Blues",
                labels={"count":"Frequency","term":""}, height=max(400, n_kw*22),
            )
            fig_kw.update_layout(coloraxis_showscale=False, font_family="IBM Plex Sans",
                                  plot_bgcolor="#f7f9fb", paper_bgcolor="white",
                                  margin=dict(l=10,r=20,t=20,b=20))
            st.plotly_chart(fig_kw, use_container_width=True)

            if cat_col in df.columns and st.checkbox("Break down by category"):
                cats_p = df[cat_col].dropna().unique().tolist()
                sel_c  = st.selectbox("Category", cats_p)
                sub_t  = top_terms(df[df[cat_col]==sel_c][text_col], n=n_kw, include_bg=inc_bg)
                if sub_t:
                    sub_df = pd.DataFrame(sub_t, columns=["term","count"])
                    fig_sub = px.bar(sub_df.sort_values("count"), x="count", y="term", orientation="h",
                                     color="count", color_continuous_scale="Teal",
                                     height=max(300, n_kw*22))
                    fig_sub.update_layout(coloraxis_showscale=False, font_family="IBM Plex Sans",
                                          plot_bgcolor="#f7f9fb", paper_bgcolor="white",
                                          margin=dict(l=10,r=20,t=10,b=10))
                    st.plotly_chart(fig_sub, use_container_width=True)

    # TAB 2 — Triggers
    with itab2:
        st.markdown("#### Announcements matched to corporate-action trigger categories")
        exploded = df[df["triggers"].apply(bool)].copy()
        exploded = exploded.explode("triggers").rename(columns={"triggers":"trigger"})

        if exploded.empty:
            st.info("No trigger keywords found in current filter set.")
        else:
            tc = exploded["trigger"].value_counts().reset_index()
            tc.columns = ["trigger","count"]
            tc["color"] = tc["trigger"].map(TRIGGER_COLORS)

            fig_donut = px.pie(
                tc, names="trigger", values="count", hole=0.52,
                color="trigger", color_discrete_map=TRIGGER_COLORS, height=360,
            )
            fig_donut.update_layout(font_family="IBM Plex Sans",
                                     legend=dict(font=dict(size=11)), margin=dict(l=0,r=0,t=20,b=0))
            fig_donut.update_traces(textposition="inside", textinfo="percent+label")

            cc1, cc2 = st.columns([1,1])
            with cc1:
                st.plotly_chart(fig_donut, use_container_width=True)
            with cc2:
                st.dataframe(tc.drop(columns="color").rename(columns={"trigger":"Trigger","count":"Matches"}),
                             hide_index=True, use_container_width=True)

            st.markdown("---")
            st.markdown("##### Filter by trigger")
            sel_trg = st.selectbox("Trigger", ["— all —"] + sorted(exploded["trigger"].unique().tolist()))
            view_e  = exploded if sel_trg == "— all —" else exploded[exploded["trigger"]==sel_trg]
            cols_show = [c for c in [name_col,"trigger",cat_col,text_col] if c in view_e.columns]
            st.dataframe(view_e[cols_show].rename(columns={name_col:"Company","trigger":"Trigger",cat_col:"Category",text_col:"Text"}),
                         hide_index=True, use_container_width=True, height=400)

    # TAB 3 — Clusters
    with itab3:
        st.markdown("#### Announcements grouped by dominant topic cluster")
        cc = df["cluster"].value_counts().reset_index()
        cc.columns = ["cluster","count"]

        fig_cl = px.bar(cc, x="cluster", y="count", color="cluster",
                        color_discrete_map={**TRIGGER_COLORS,"General / Other":"#adb5bd"},
                        labels={"cluster":"","count":"Announcements"}, height=360)
        fig_cl.update_layout(showlegend=False, font_family="IBM Plex Sans",
                              plot_bgcolor="#f7f9fb", paper_bgcolor="white",
                              xaxis_tickangle=-30, margin=dict(l=10,r=10,t=20,b=60))
        st.plotly_chart(fig_cl, use_container_width=True)

        st.markdown("---")
        sel_cl = st.selectbox("Browse cluster", cc["cluster"].tolist())
        cl_df  = df[df["cluster"]==sel_cl]
        cols_cl = [c for c in [name_col, cat_col, text_col] if c in cl_df.columns]
        st.caption(f"{len(cl_df):,} announcements in **{sel_cl}**")
        ev_cl = st.dataframe(
            cl_df[cols_cl].rename(columns={name_col:"Company",cat_col:"Category",text_col:"Text"}),
            hide_index=True, use_container_width=True, height=400,
            on_select="rerun", selection_mode="multi-row", key="cl_table",
        )
        for idx in (ev_cl.selection.rows if ev_cl else []):
            _log_view(ins_src, cl_df.iloc[idx].to_dict())

    # TAB 4 — AI Digest
    with itab4:
        st.markdown(f"#### AI-generated investment digest — **{ins_src}**")
        if len(df) > MAX_AI_ROWS:
            st.warning(f"⚠️  {len(df):,} items in scope — Claude will process the most recent **{MAX_AI_ROWS}**. Narrow filters for a more targeted digest.")
        else:
            st.caption(f"Will send {len(df)} announcements to Claude.")

        restrict_cl = st.selectbox("Restrict to cluster (optional)",
                                   ["— all clusters —"] + sorted(df["cluster"].unique().tolist()))
        ai_df = df if restrict_cl == "— all clusters —" else df[df["cluster"]==restrict_cl]

        if st.button("🤖  Generate AI Digest", type="primary"):
            if ai_df.empty:
                st.warning("No data to analyse.")
            else:
                with st.spinner(f"Claude is reading {min(len(ai_df), MAX_AI_ROWS)} announcements…"):
                    try:
                        sample = ai_df.head(MAX_AI_ROWS)
                        lines = [
                            f"- [{row.get(name_col,'') or row.get('symbol','')} | {row.get(cat_col,'')}] {row.get(text_col,'')}"
                            for _, row in sample.iterrows()
                        ]
                        digest = ai_digest("\n".join(lines))
                        st.markdown("---")
                        st.markdown(digest)
                        st.markdown("---")
                        st.download_button(
                            "⬇  Download digest as .txt", data=digest,
                            file_name=f"{ins_src.lower().replace(' ','_')}_digest_{ins_from}_{ins_to}.txt",
                            mime="text/plain",
                        )
                    except Exception as e:
                        st.error(f"Claude API error: {e}")

    st.stop()


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: MY ACTIVITY
# ═════════════════════════════════════════════════════════════════════════════

elif page == "Calculators":

    st.markdown("""<div class="page-head">
      <h1>🧮 Calculators</h1>
      <p>Quick-fire investing &amp; tax tools — stock average, SIP, CAGR, capital gains, brokerage, FD, DCF &amp; Reverse DCF</p>
    </div>""", unsafe_allow_html=True)

    calc1, calc2, calc3, calc4, calc5, calc6, calc7, calc8 = st.tabs([
        "📈 Stock Average", "💰 SIP", "📊 CAGR",
        "🧾 Capital Gains", "🏦 Brokerage", "🏛️ FD",
        "🏗️ DCF", "🔄 Reverse DCF",
    ])

    # ── TAB 1 — Stock Average Calculator ────────────────────────────────────
    with calc1:
        st.markdown("#### Average price across single or multiple purchases")
        st.caption("Add each buy lot (quantity + price). The average, total qty and total invested update live.")

        default_lots = pd.DataFrame(
            [{"Quantity": 10, "Price (₹)": 100.0}, {"Quantity": 10, "Price (₹)": 120.0}]
        )
        lots = st.data_editor(
            default_lots, num_rows="dynamic", use_container_width=True,
            key="sa_lots",
            column_config={
                "Quantity":   st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
                "Price (₹)":  st.column_config.NumberColumn(min_value=0.0, step=0.05, format="%.2f"),
            },
        )
        lots = lots.dropna()
        lots = lots[(lots["Quantity"] > 0) & (lots["Price (₹)"] > 0)]

        if lots.empty:
            st.info("Add at least one buy lot above to see the average.")
        else:
            total_qty = float(lots["Quantity"].sum())
            total_inv = float((lots["Quantity"] * lots["Price (₹)"]).sum())
            avg_price = total_inv / total_qty if total_qty else 0.0

            sa1, sa2, sa3 = st.columns(3)
            _metric(sa1, f"{total_qty:,.0f}", "Total quantity")
            _metric(sa2, f"₹{total_inv:,.2f}", "Total invested")
            _metric(sa3, f"₹{avg_price:,.2f}", "Average price")

            st.markdown("---")
            st.markdown("##### 🎯 What if I buy more to bring the average down?")
            tgt1, tgt2 = st.columns(2)
            add_price = tgt1.number_input("Price of additional buy (₹)", min_value=0.0, value=max(avg_price - 10, 0.0), step=0.5, key="sa_add_price")
            target_avg = tgt2.number_input("Target average price (₹)", min_value=0.0, value=max(avg_price - 5, 0.0), step=0.5, key="sa_target_avg")
            if add_price >= target_avg or target_avg <= 0:
                st.caption("Set an additional-buy price below your target average to compute the quantity needed.")
            else:
                qty_needed = (total_qty * (avg_price - target_avg)) / (target_avg - add_price)
                if qty_needed > 0:
                    st.success(f"Buy **{qty_needed:,.0f}** more shares at ₹{add_price:,.2f} to bring your average down to ≈ ₹{target_avg:,.2f}.")
                else:
                    st.info("Your average is already at or below that target.")

    # ── TAB 2 — SIP Calculator ───────────────────────────────────────────────
    with calc2:
        st.markdown("#### Systematic Investment Plan (SIP) future value")
        s1, s2, s3 = st.columns(3)
        sip_amt   = s1.number_input("Monthly investment (₹)", min_value=100.0, value=10000.0, step=500.0, key="sip_amt")
        sip_ret   = s2.number_input("Expected annual return (%)", min_value=0.0, value=12.0, step=0.5, key="sip_ret")
        sip_years = s3.number_input("Tenure (years)", min_value=1, value=10, step=1, key="sip_years")

        r_m = sip_ret / 100 / 12
        n_m = int(sip_years * 12)
        fv  = sip_amt * (((1 + r_m) ** n_m - 1) / r_m) * (1 + r_m) if r_m > 0 else sip_amt * n_m
        invested = sip_amt * n_m
        gain = fv - invested

        r1, r2, r3 = st.columns(3)
        _metric(r1, f"₹{invested:,.0f}", "Total invested")
        _metric(r2, f"₹{gain:,.0f}",     "Wealth gained")
        _metric(r3, f"₹{fv:,.0f}",       "Maturity value")

        yearly = []
        bal = 0.0
        for m in range(1, n_m + 1):
            bal = (bal + sip_amt) * (1 + r_m) if r_m > 0 else bal + sip_amt
            if m % 12 == 0:
                yearly.append({"Year": m // 12, "Invested": sip_amt * m, "Value": bal})
        if yearly:
            yr_df = pd.DataFrame(yearly)
            fig_sip = go.Figure()
            fig_sip.add_trace(go.Scatter(x=yr_df["Year"], y=yr_df["Invested"], name="Invested", line=dict(color=INK_MUTED, dash="dot")))
            fig_sip.add_trace(go.Scatter(x=yr_df["Year"], y=yr_df["Value"], name="Value", line=dict(color=ACCENT, width=3), fill="tonexty"))
            fig_sip.update_layout(xaxis_title="Year", yaxis_title="₹")
            st.plotly_chart(_plotly_defaults(fig_sip, height=340), use_container_width=True)
        st.caption("Assumes a fixed monthly return and consistent contributions — actual market returns will vary.")

    # ── TAB 3 — CAGR Calculator ──────────────────────────────────────────────
    with calc3:
        st.markdown("#### Compound Annual Growth Rate (CAGR)")
        c1, c2, c3 = st.columns(3)
        cagr_begin = c1.number_input("Initial value (₹)", min_value=0.01, value=100000.0, step=1000.0, key="cagr_begin")
        cagr_end   = c2.number_input("Final value (₹)",   min_value=0.01, value=180000.0, step=1000.0, key="cagr_end")
        cagr_yrs   = c3.number_input("Duration (years)",  min_value=0.1,  value=5.0,      step=0.5,    key="cagr_yrs")

        cagr_pct = ((cagr_end / cagr_begin) ** (1 / cagr_yrs) - 1) * 100 if cagr_begin > 0 and cagr_yrs > 0 else 0.0
        abs_gain_pct = ((cagr_end - cagr_begin) / cagr_begin) * 100 if cagr_begin > 0 else 0.0

        g1, g2 = st.columns(2)
        _metric(g1, f"{cagr_pct:,.2f}%", "CAGR")
        _metric(g2, f"{abs_gain_pct:,.2f}%", "Absolute gain")
        st.caption("CAGR = (Final / Initial)^(1 / years) − 1. Smooths out year-to-year volatility into a single annualised rate.")

    # ── TAB 4 — Capital Gains (LTCG / STCG) ─────────────────────────────────
    with calc4:
        st.markdown("#### Capital gains on listed equity / equity mutual funds (India)")
        st.caption("Tax rates below are pre-filled with current defaults but editable — always confirm against the latest Finance Act before relying on this for filing.")
        cg1, cg2, cg3 = st.columns(3)
        cg_buy  = cg1.number_input("Buy price (₹)",  min_value=0.0, value=100.0, step=1.0, key="cg_buy")
        cg_sell = cg2.number_input("Sell price (₹)", min_value=0.0, value=150.0, step=1.0, key="cg_sell")
        cg_qty  = cg3.number_input("Quantity", min_value=1, value=100, step=1, key="cg_qty")
        cg_hold = st.radio("Holding period", ["Short-term (< 12 months)", "Long-term (≥ 12 months)"], horizontal=True, key="cg_hold")

        with st.expander("⚙️ Tax rate assumptions (editable)"):
            e1, e2 = st.columns(2)
            stcg_rate = e1.number_input("STCG rate (%)", min_value=0.0, value=20.0, step=0.5, key="cg_stcg_rate")
            ltcg_rate = e2.number_input("LTCG rate (%)", min_value=0.0, value=12.5, step=0.5, key="cg_ltcg_rate")
            ltcg_exempt = st.number_input("LTCG exemption per FY (₹)", min_value=0.0, value=125000.0, step=5000.0, key="cg_ltcg_exempt")

        gross_gain = (cg_sell - cg_buy) * cg_qty
        if gross_gain <= 0:
            st.warning(f"No capital gains tax applies — this position shows a loss of ₹{abs(gross_gain):,.2f}.")
        elif cg_hold.startswith("Short"):
            tax = gross_gain * stcg_rate / 100
            n1, n2, n3 = st.columns(3)
            _metric(n1, f"₹{gross_gain:,.2f}", "Gross gain")
            _metric(n2, f"₹{tax:,.2f}", f"STCG tax @ {stcg_rate:g}%")
            _metric(n3, f"₹{gross_gain - tax:,.2f}", "Net gain after tax")
        else:
            taxable = max(gross_gain - ltcg_exempt, 0)
            tax = taxable * ltcg_rate / 100
            n1, n2, n3 = st.columns(3)
            _metric(n1, f"₹{gross_gain:,.2f}", "Gross gain")
            _metric(n2, f"₹{tax:,.2f}", f"LTCG tax @ {ltcg_rate:g}% (after ₹{ltcg_exempt:,.0f} exempt)")
            _metric(n3, f"₹{gross_gain - tax:,.2f}", "Net gain after tax")

    # ── TAB 5 — Brokerage & Break-even Calculator ────────────────────────────
    with calc5:
        st.markdown("#### Brokerage, charges &amp; break-even price")
        b1, b2, b3 = st.columns(3)
        br_buy  = b1.number_input("Buy price (₹)",  min_value=0.0, value=100.0, step=1.0, key="br_buy")
        br_sell = b2.number_input("Sell price (₹)", min_value=0.0, value=105.0, step=1.0, key="br_sell")
        br_qty  = b3.number_input("Quantity", min_value=1, value=100, step=1, key="br_qty")

        with st.expander("⚙️ Charges assumptions (editable — delivery trade defaults)"):
            x1, x2, x3 = st.columns(3)
            brok_pct   = x1.number_input("Brokerage (% per side)", min_value=0.0, value=0.0, step=0.01, key="br_brok_pct", help="Many discount brokers charge ₹0–20 flat per order instead — set to 0 and adjust below if so.")
            brok_flat  = x2.number_input("Flat fee per order (₹)", min_value=0.0, value=0.0, step=1.0, key="br_brok_flat")
            stt_pct    = x3.number_input("STT (% on sell side)", min_value=0.0, value=0.1, step=0.01, key="br_stt_pct")
            y1, y2 = st.columns(2)
            other_pct  = y1.number_input("Other charges — exchange/DP/GST/stamp (% per side)", min_value=0.0, value=0.05, step=0.01, key="br_other_pct")

        buy_val  = br_buy * br_qty
        sell_val = br_sell * br_qty
        charges  = (
            (buy_val + sell_val) * brok_pct / 100 + 2 * brok_flat
            + sell_val * stt_pct / 100
            + (buy_val + sell_val) * other_pct / 100
        )
        gross_pnl = sell_val - buy_val
        net_pnl   = gross_pnl - charges
        breakeven = (buy_val + charges) / br_qty if br_qty else 0.0

        p1, p2, p3, p4 = st.columns(4)
        _metric(p1, f"₹{gross_pnl:,.2f}", "Gross P&L")
        _metric(p2, f"₹{charges:,.2f}",   "Total charges")
        _metric(p3, f"₹{net_pnl:,.2f}",   "Net P&L")
        _metric(p4, f"₹{breakeven:,.2f}", "Break-even price")

    # ── TAB 6 — Fixed Deposit Calculator ─────────────────────────────────────
    with calc6:
        st.markdown("#### Fixed Deposit maturity value")
        f1, f2, f3, f4 = st.columns(4)
        fd_principal = f1.number_input("Principal (₹)", min_value=0.0, value=100000.0, step=5000.0, key="fd_principal")
        fd_rate      = f2.number_input("Interest rate (% p.a.)", min_value=0.0, value=7.0, step=0.1, key="fd_rate")
        fd_years     = f3.number_input("Tenure (years)", min_value=0.1, value=5.0, step=0.5, key="fd_years")
        fd_comp      = f4.selectbox("Compounding", ["Quarterly", "Monthly", "Half-yearly", "Annually"], key="fd_comp")

        comp_map = {"Quarterly": 4, "Monthly": 12, "Half-yearly": 2, "Annually": 1}
        n_freq = comp_map[fd_comp]
        fd_maturity = fd_principal * (1 + (fd_rate / 100) / n_freq) ** (n_freq * fd_years)
        fd_interest = fd_maturity - fd_principal

        d1, d2 = st.columns(2)
        _metric(d1, f"₹{fd_interest:,.2f}", "Interest earned")
        _metric(d2, f"₹{fd_maturity:,.2f}", "Maturity value")
        st.caption("Standard compound-interest FD math — actual bank payout may differ slightly by day-count convention.")

    # ── shared DCF math (used by both DCF tabs) ──────────────────────────────
    def _dcf_core(base_fcf, g1, n1, g_t, r, net_debt, shares):
        """Two-stage DCF: explicit growth g1 for n1 years, then Gordon-growth terminal value."""
        n1 = int(n1)
        fcf_list = [base_fcf * (1 + g1) ** t for t in range(1, n1 + 1)]
        pv_list  = [fcf / (1 + r) ** t for t, fcf in zip(range(1, n1 + 1), fcf_list)]
        if r <= g_t:
            return None
        terminal_value = fcf_list[-1] * (1 + g_t) / (r - g_t)
        pv_terminal = terminal_value / (1 + r) ** n1
        ev = sum(pv_list) + pv_terminal
        equity_value = ev - net_debt
        per_share = equity_value / shares if shares else 0.0
        return {
            "fcf_list": fcf_list, "pv_list": pv_list, "terminal_value": terminal_value,
            "pv_terminal": pv_terminal, "ev": ev, "equity_value": equity_value, "per_share": per_share,
        }

    # ── TAB 7 — DCF Calculator ────────────────────────────────────────────────
    with calc7:
        st.markdown("#### Discounted Cash Flow — intrinsic value per share")
        st.caption("Two-stage model: explicit growth for N years, then a Gordon-growth terminal value discounted back at WACC.")

        dc1, dc2, dc3 = st.columns(3)
        dcf_fcf    = dc1.number_input("Base FCF — last 12m (₹ Cr)", min_value=0.0, value=500.0, step=10.0, key="dcf_fcf")
        dcf_shares = dc2.number_input("Shares outstanding (Cr)", min_value=0.01, value=50.0, step=1.0, key="dcf_shares")
        dcf_debt   = dc3.number_input("Net debt (₹ Cr) — negative if net cash", value=200.0, step=10.0, key="dcf_debt")

        dc4, dc5, dc6, dc7 = st.columns(4)
        dcf_g1   = dc4.number_input("Growth rate, explicit period (%)", value=15.0, step=0.5, key="dcf_g1")
        dcf_n1   = dc5.number_input("Explicit period (years)", min_value=1, max_value=20, value=10, step=1, key="dcf_n1")
        dcf_gt   = dc6.number_input("Terminal growth rate (%)", value=4.0, step=0.25, key="dcf_gt")
        dcf_wacc = dc7.number_input("Discount rate / WACC (%)", min_value=0.1, value=11.0, step=0.25, key="dcf_wacc")

        dcf_cmp = st.number_input("Current market price (₹) — optional, for upside/downside", min_value=0.0, value=0.0, step=1.0, key="dcf_cmp")

        if dcf_wacc <= dcf_gt:
            st.error("Discount rate must be greater than the terminal growth rate — adjust the inputs above.")
        else:
            res = _dcf_core(dcf_fcf, dcf_g1/100, dcf_n1, dcf_gt/100, dcf_wacc/100, dcf_debt, dcf_shares)

            m1, m2, m3, m4 = st.columns(4)
            _metric(m1, f"₹{res['ev']:,.0f} Cr", "Enterprise value")
            _metric(m2, f"₹{res['equity_value']:,.0f} Cr", "Equity value")
            _metric(m3, f"₹{res['per_share']:,.2f}", "Intrinsic value / share")
            if dcf_cmp > 0:
                upside = (res["per_share"] - dcf_cmp) / dcf_cmp * 100
                _metric(m4, f"{upside:+,.1f}%", f"vs CMP ₹{dcf_cmp:,.2f}")
            else:
                _metric(m4, f"{res['pv_terminal']/res['ev']*100:,.0f}%", "Value from terminal")

            years = list(range(1, int(dcf_n1) + 1))
            fig_dcf = go.Figure()
            fig_dcf.add_trace(go.Bar(x=years, y=res["fcf_list"], name="Projected FCF", marker_color=INK_MUTED, opacity=0.55))
            fig_dcf.add_trace(go.Bar(x=years, y=res["pv_list"], name="PV of FCF", marker_color=ACCENT))
            fig_dcf.update_layout(xaxis_title="Year", yaxis_title="₹ Cr", barmode="overlay")
            st.plotly_chart(_plotly_defaults(fig_dcf, height=340), use_container_width=True)

            fig_bridge = px.pie(
                names=["PV of explicit-period FCF", "PV of terminal value"],
                values=[sum(res["pv_list"]), res["pv_terminal"]],
                hole=0.55, color_discrete_sequence=[ACCENT, "#adb5bd"], height=320,
            )
            fig_bridge.update_traces(textposition="inside", textinfo="percent+label")
            fig_bridge.update_layout(font_family="IBM Plex Sans", margin=dict(l=0,r=0,t=20,b=0), showlegend=False)
            bc1, bc2 = st.columns([1,1])
            with bc1:
                st.plotly_chart(fig_bridge, use_container_width=True)
            with bc2:
                st.markdown("##### Composition of enterprise value")
                st.dataframe(pd.DataFrame({
                    "Component": ["PV — explicit period", "PV — terminal value", "Enterprise value"],
                    "₹ Cr": [round(sum(res["pv_list"]),1), round(res["pv_terminal"],1), round(res["ev"],1)],
                }), hide_index=True, use_container_width=True)
            st.caption("Terminal value = FCF in final explicit year × (1 + terminal growth) / (WACC − terminal growth), then discounted back N years.")

    # ── TAB 8 — Reverse DCF Calculator ───────────────────────────────────────
    with calc8:
        st.markdown("#### Reverse DCF — what growth is the market pricing in?")
        st.caption("Holds price, WACC and terminal growth fixed, and solves for the explicit-period growth rate that justifies today's price.")

        rc1, rc2, rc3 = st.columns(3)
        rdcf_cmp    = rc1.number_input("Current market price (₹)", min_value=0.01, value=800.0, step=1.0, key="rdcf_cmp")
        rdcf_shares = rc2.number_input("Shares outstanding (Cr)", min_value=0.01, value=50.0, step=1.0, key="rdcf_shares")
        rdcf_debt   = rc3.number_input("Net debt (₹ Cr) — negative if net cash", value=200.0, step=10.0, key="rdcf_debt")

        rc4, rc5, rc6, rc7 = st.columns(4)
        rdcf_fcf  = rc4.number_input("Base FCF — last 12m (₹ Cr)", min_value=0.01, value=500.0, step=10.0, key="rdcf_fcf")
        rdcf_n1   = rc5.number_input("Explicit period (years)", min_value=1, max_value=20, value=10, step=1, key="rdcf_n1")
        rdcf_gt   = rc6.number_input("Terminal growth rate (%)", value=4.0, step=0.25, key="rdcf_gt")
        rdcf_wacc = rc7.number_input("Discount rate / WACC (%)", min_value=0.1, value=11.0, step=0.25, key="rdcf_wacc")

        if rdcf_wacc <= rdcf_gt:
            st.error("Discount rate must be greater than the terminal growth rate — adjust the inputs above.")
        else:
            r_dec, gt_dec = rdcf_wacc/100, rdcf_gt/100
            target = rdcf_cmp

            def _ps(g1):
                out = _dcf_core(rdcf_fcf, g1, rdcf_n1, gt_dec, r_dec, rdcf_debt, rdcf_shares)
                return out["per_share"] if out else float("-inf")

            lo, hi = -0.50, 3.00
            if _ps(lo) > target:
                st.warning("Even a −50% growth assumption exceeds today's price — the market may be pricing in a decline steeper than this model's range.")
            elif _ps(hi) < target:
                st.warning("Even 300% growth doesn't reach today's price with these WACC/terminal assumptions — try lowering WACC or raising terminal growth.")
            else:
                for _ in range(60):
                    mid = (lo + hi) / 2
                    if _ps(mid) < target:
                        lo = mid
                    else:
                        hi = mid
                implied_g = (lo + hi) / 2

                m1, m2 = st.columns(2)
                _metric(m1, f"{implied_g*100:,.2f}%", "Implied growth rate (explicit period)")
                _metric(m2, f"₹{_ps(implied_g):,.2f}", "Model price at that growth")

                g_range = [x/1000 for x in range(-500, 1010, 10)]
                px_vals = [_ps(g) for g in g_range]
                fig_rev = go.Figure()
                fig_rev.add_trace(go.Scatter(x=[g*100 for g in g_range], y=px_vals, name="Intrinsic value", line=dict(color=ACCENT, width=3)))
                fig_rev.add_hline(y=rdcf_cmp, line_dash="dot", line_color=DANGER,
                                   annotation_text=f"CMP ₹{rdcf_cmp:,.0f}", annotation_position="top left")
                fig_rev.add_vline(x=implied_g*100, line_dash="dot", line_color=INK_MUTED)
                fig_rev.add_trace(go.Scatter(x=[implied_g*100], y=[rdcf_cmp], mode="markers",
                                              marker=dict(color=DANGER, size=10), name="Implied growth"))
                fig_rev.update_layout(xaxis_title="Assumed explicit-period growth rate (%)", yaxis_title="Intrinsic value / share (₹)")
                st.plotly_chart(_plotly_defaults(fig_rev, height=360), use_container_width=True)
                st.caption("Where the blue curve crosses the current market price is the growth rate the market is implicitly assuming.")

    st.markdown("<div style='height:0.5rem'></div>", unsafe_allow_html=True)
    st.caption("These calculators are for quick estimation only and are not tax, investment or financial advice .")


elif page == "My Activity":

    st.markdown(f"""<div class="page-head">
      <h1>🕘 My Activity</h1>
      <p>Every record you've viewed and every search you've run — visible only to you</p>
    </div>""", unsafe_allow_html=True)

    views   = st.session_state.get("view_history", [])
    searches = st.session_state.get("search_history", [])

    unique_co = len({r.get("company_name") or r.get("scrip_name") or r.get("symbol","")
                     for r in views if r.get("company_name") or r.get("scrip_name") or r.get("symbol")})
    m1,m2,m3,m4 = st.columns(4)
    _metric(m1, f"{len(views):,}",   "Records viewed")
    _metric(m2, f"{unique_co:,}",    "Unique companies")
    _metric(m3, f"{len(searches):,}","Searches run")
    _metric(m4, current_user,        "Logged-in user")

    st.markdown("<br>", unsafe_allow_html=True)

    act1, act2 = st.tabs(["📄  Viewed records", "🔍  Search history"])

    with act1:
        if not views:
            st.info("No records viewed yet. Select rows in Announcements or Insights to log them here.")
        else:
            v_df = pd.DataFrame(views)
            st.caption(f"{len(v_df):,} record(s) viewed this session")
            st.dataframe(v_df, hide_index=True, use_container_width=True, height=480)
            st.download_button("⬇ Download view history",
                               v_df.to_csv(index=False).encode(),
                               f"{current_user}_view_history.csv", "text/csv")

    with act2:
        if not searches:
            st.info("No searches logged yet — run a search on Announcements or Insights.")
        else:
            rows_out = []
            for s in searches:
                f = s.get("filters", {})
                bits = [f"{k}: {v}" for k, v in f.items() if v not in (None,"","[]",[], False)]
                rows_out.append({
                    "Source":      s.get("source",""),
                    "Filters":     "; ".join(bits) if bits else "(no filters)",
                    "Results":     s.get("result_count",""),
                    "Searched at": s.get("searched_at",""),
                })
            s_df = pd.DataFrame(rows_out)
            st.caption(f"{len(s_df):,} search(es) this session")
            st.dataframe(s_df, hide_index=True, use_container_width=True, height=480)
            st.download_button("⬇ Download search history",
                               s_df.to_csv(index=False).encode(),
                               f"{current_user}_search_history.csv", "text/csv")


elif page == "Guided Activity":

    st.markdown(f"""<div class="page-head">
      <h1>🧭 Guided Activity</h1>
      <p>Run the pipeline end-to-end, right from the browser: scrape BSE/NSE announcements
         (<code>market_announcements.py</code>), then score any source's rows for "idea" categories
         (<code>idea_rules.py</code>) — BSE Equity, BSE SME, NSE Equity, or NSE SME.</p>
    </div>""", unsafe_allow_html=True)

    if not _SCRAPER_OK:
        st.error(f"Couldn't import `market_announcements.py` — make sure it sits next to `market_suite.py`. Details: {_SCRAPER_ERR}")
    if not _IDEA_RULES_OK:
        st.error(f"Couldn't import `idea_rules.py` — make sure it sits next to `market_suite.py`. Details: {_IDEA_RULES_ERR}")

    st.session_state.setdefault("ga_params", {
        "sources":        ["bse_equity"],
        "date_mode":      "Today",
        "from_date":      date.today(),
        "to_date":        date.today(),
        "bse_eq_mode":    "Market-wide (all companies)",
        "bse_eq_symbols": "",
        "bse_sme_ann":    True,
        "bse_sme_corp":   True,
    })
    P = st.session_state.ga_params

    ga_tab1, ga_tab2, ga_tab3 = st.tabs(["① Data parameters", "② Scrape", "③ Score ideas"])

    # ═══════════════════════ ① DATA PARAMETERS ═══════════════════════
    with ga_tab1:
        st.caption("Every option `market_announcements.py fetch` accepts on the command line, exposed here.")

        st.markdown("**Sources to scrape**")
        label_to_key = {v: k for k, v in GA_SOURCE_LABELS.items()}
        src_labels = st.multiselect(
            "Sources", options=list(GA_SOURCE_LABELS.values()),
            default=[GA_SOURCE_LABELS[s] for s in P["sources"]],
            key="ga_src_select", label_visibility="collapsed",
        )
        P["sources"] = [label_to_key[l] for l in src_labels] or ["bse_equity"]

        st.markdown("**Date range**")
        dcol1, dcol2 = st.columns([1.4, 2])
        date_presets = ["Today", "Yesterday", "Last 7 days", "Last 30 days", "Custom range"]
        with dcol1:
            P["date_mode"] = st.radio("Preset", date_presets, index=date_presets.index(P["date_mode"]), key="ga_date_mode")
        today = date.today()
        if P["date_mode"] == "Today":
            P["from_date"], P["to_date"] = today, today
        elif P["date_mode"] == "Yesterday":
            P["from_date"] = P["to_date"] = today - timedelta(days=1)
        elif P["date_mode"] == "Last 7 days":
            P["from_date"], P["to_date"] = today - timedelta(days=6), today
        elif P["date_mode"] == "Last 30 days":
            P["from_date"], P["to_date"] = today - timedelta(days=29), today
        else:
            with dcol2:
                custom = st.date_input("Custom range", value=(P["from_date"], P["to_date"]), key="ga_custom_range")
                if isinstance(custom, tuple) and len(custom) == 2:
                    P["from_date"], P["to_date"] = custom
        st.caption(f"Selected window: **{P['from_date']:%d-%m-%Y} → {P['to_date']:%d-%m-%Y}**")

        st.markdown("**BSE Equity options**")
        beq_c1, beq_c2 = st.columns([1.3, 2])
        beq_modes = ["Market-wide (all companies)", "Watchlist (specific symbols)"]
        with beq_c1:
            P["bse_eq_mode"] = st.radio("Fetch mode", beq_modes, index=beq_modes.index(P["bse_eq_mode"]), key="ga_bseeq_mode")
        with beq_c2:
            if P["bse_eq_mode"] == "Watchlist (specific symbols)":
                P["bse_eq_symbols"] = st.text_input(
                    "Symbols (comma-separated, e.g. TCS, INFY, RELIANCE)",
                    value=P["bse_eq_symbols"], key="ga_bseeq_symbols",
                )
            else:
                st.caption("Fetches BSE's market-wide announcements endpoint — every listed company, no scrip filter.")

        st.markdown("**BSE SME options**")
        sme_c1, sme_c2 = st.columns(2)
        P["bse_sme_ann"]  = sme_c1.checkbox("Fetch announcements", value=P["bse_sme_ann"], key="ga_sme_ann")
        P["bse_sme_corp"] = sme_c2.checkbox("Fetch corp actions",  value=P["bse_sme_corp"], key="ga_sme_corp")

        st.markdown("**NSE options**")
        st.caption("NSE Equity / NSE SME pull from NSE's own index feed — no extra filters beyond the date range above.")

        st.divider()
        st.markdown("**Active parameters**")
        st.json({
            "sources": [GA_SOURCE_LABELS[s] for s in P["sources"]],
            "from_date": str(P["from_date"]), "to_date": str(P["to_date"]),
            "bse_equity_mode": P["bse_eq_mode"],
            "bse_equity_symbols": [s.strip() for s in P["bse_eq_symbols"].split(",") if s.strip()],
            "bse_sme_fetch_announcements": P["bse_sme_ann"],
            "bse_sme_fetch_corp_actions": P["bse_sme_corp"],
        })

    # ═══════════════════════ ② SCRAPE ═══════════════════════
    with ga_tab2:
        st.caption("Runs `market_announcements.py`'s fetchers in-process, using the parameters set in step ①.")
        watchlist_note = f" · watchlist: `{P['bse_eq_symbols']}`" if (P["bse_eq_mode"].startswith("Watchlist") and P["bse_eq_symbols"]) else ""
        st.markdown(
            f"**{len(P['sources'])} source(s)** — {', '.join(GA_SOURCE_LABELS[s] for s in P['sources'])} · "
            f"**{P['from_date']:%d-%m-%Y} → {P['to_date']:%d-%m-%Y}**{watchlist_note}"
        )
        run_scrape = st.button("▶ Run scrape now", type="primary", disabled=not _SCRAPER_OK, key="ga_run_scrape")
        if run_scrape:
            symbols = [s.strip() for s in P["bse_eq_symbols"].split(",") if s.strip()]
            with st.spinner(f"Scraping {', '.join(GA_SOURCE_LABELS[s] for s in P['sources'])} …"):
                results = ga_run_scrape(
                    P["sources"], P["from_date"], P["to_date"], bse_eq_symbols=symbols,
                    bse_sme_ann=P["bse_sme_ann"], bse_sme_corp=P["bse_sme_corp"],
                )
            st.session_state["ga_last_scrape"] = results
            st.cache_data.clear()

        results = st.session_state.get("ga_last_scrape")
        if results:
            st.markdown("**Last run**")
            rcols = st.columns(len(results))
            for c, r in zip(rcols, results):
                if r["error"]:
                    c.error(f"**{r['label']}**\n\nFailed: {r['error']}")
                else:
                    _metric(c, f"{r['inserted']:,}", f"{r['label']} — new rows")
                    c.caption(f"fetched {r['fetched']:,} · `{r['db_path']}`")
            res_df = pd.DataFrame(results)[["label", "fetched", "inserted", "db_path", "error"]]
            res_df.columns = ["Source", "Fetched", "New inserted", "Database", "Error"]
            st.dataframe(res_df, hide_index=True, use_container_width=True)
        else:
            st.info("No scrape run yet this session. Set parameters in step ① then click **Run scrape now**.")

    # ═══════════════════════ ③ SCORE IDEAS ═══════════════════════
    with ga_tab3:
        st.caption(
            "Scores the selected source's rows against every idea type in `idea_rules.py` — same keyword / "
            "category-bonus / negative-phrase rules the file documents — and writes results into that source's "
            "Idea Board tables."
        )

        if not _IDEA_RULES_OK:
            st.warning("idea_rules.py not available — scoring is disabled.")
        elif not _IDEA_PIPELINE_OK:
            st.warning(f"announcement_ideas_pipeline.py not available — scoring is disabled. Details: {_IDEA_PIPELINE_ERR}")
        else:
            st.session_state.setdefault("ga_score_params", {
                "source":         "bse_equity",
                "use_same_range": True,
                "score_from": P["from_date"], "score_to": P["to_date"],
                "groups": list(idea_rules.GROUPS),
                "types": list(idea_rules.IDEA_TYPES.keys()),
                "min_score": idea_rules.MIN_SCORE_THRESHOLD,
                "category_bonus": idea_rules.CATEGORY_BONUS_WEIGHT,
                "force_rescore": False,
            })
            SP = st.session_state.ga_score_params

            st.markdown("**Source to score**")
            src_label_to_key = {v: k for k, v in GA_SOURCE_LABELS.items()}
            score_src_label = st.selectbox(
                "Source", list(GA_SOURCE_LABELS.values()),
                index=list(GA_SOURCE_LABELS.keys()).index(SP["source"]),
                key="ga_sp_source", label_visibility="collapsed",
            )
            SP["source"] = src_label_to_key[score_src_label]

            st.markdown("**Scoring window**")
            SP["use_same_range"] = st.checkbox(
                f"Use the same date range as step ① ({P['from_date']:%d-%m-%Y} → {P['to_date']:%d-%m-%Y})",
                value=SP["use_same_range"], key="ga_sp_same_range",
            )
            if SP["use_same_range"]:
                SP["score_from"], SP["score_to"] = P["from_date"], P["to_date"]
            else:
                custom_s = st.date_input("Scoring date range", value=(SP["score_from"], SP["score_to"]), key="ga_sp_custom_range")
                if isinstance(custom_s, tuple) and len(custom_s) == 2:
                    SP["score_from"], SP["score_to"] = custom_s

            st.markdown("**Idea groups & types to score**")
            gcol1, gcol2 = st.columns(2)
            with gcol1:
                SP["groups"] = st.multiselect("Groups", idea_rules.GROUPS, default=SP["groups"], key="ga_sp_groups")
            eligible_types = [t for t, cfg in idea_rules.IDEA_TYPES.items() if cfg["group"] in SP["groups"]]
            with gcol2:
                SP["types"] = st.multiselect(
                    "Idea types", eligible_types,
                    default=[t for t in SP["types"] if t in eligible_types] or eligible_types,
                    key="ga_sp_types",
                )

            st.markdown("**Scoring thresholds** (defaults come straight from `idea_rules.py`)")
            tcol1, tcol2, tcol3 = st.columns(3)
            SP["min_score"] = tcol1.number_input(
                "Min score threshold", min_value=0.0, max_value=100.0, value=float(SP["min_score"]), step=0.5, key="ga_sp_min_score",
            )
            SP["category_bonus"] = tcol2.number_input(
                "Category bonus weight", min_value=0.0, max_value=20.0, value=float(SP["category_bonus"]), step=0.5, key="ga_sp_cat_bonus",
            )
            SP["force_rescore"] = tcol3.checkbox(
                "Force re-score (overwrite existing scores)", value=SP["force_rescore"], key="ga_sp_force",
            )

            st.divider()
            score_label = GA_SOURCE_LABELS[SP["source"]]
            db_path = DB_PATHS[score_label]
            if not Path(db_path).exists():
                st.warning(f"`{db_path}` not found yet — run step ② (with **{score_label}** selected) first.")
            run_score = st.button(
                "▶ Run scoring now", type="primary", key="ga_run_score",
                disabled=not (Path(db_path).exists() and SP["types"]),
            )
            if run_score:
                with st.spinner(f"Scoring {score_label} announcements against the idea taxonomy …"):
                    ga_ensure_idea_tables(db_path)
                    summary = ga_score_announcements(
                        db_path, SP["score_from"], SP["score_to"],
                        idea_type_names=SP["types"], min_score=SP["min_score"],
                        category_bonus=SP["category_bonus"], force_rescore=SP["force_rescore"],
                    )
                st.session_state["ga_last_score"] = summary
                st.session_state["ga_last_score_label"] = score_label
                st.cache_data.clear()  # so the Idea Board picks up the new scores immediately

            summary = st.session_state.get("ga_last_score")
            if summary:
                ran_label = st.session_state.get("ga_last_score_label", score_label)
                st.markdown(f"**Last run** — {ran_label}")
                m1, m2, m3 = st.columns(3)
                _metric(m1, f"{summary['rows_considered']:,}", "Rows considered")
                _metric(m2, f"{summary['rows_matched']:,}",    "Rows matched ≥1 idea")
                _metric(m3, f"{summary['scores_written']:,}",  "Idea scores written")
                if summary["per_type"]:
                    pt_df = pd.DataFrame(
                        sorted(summary["per_type"].items(), key=lambda x: -x[1]), columns=["Idea type", "Matches"]
                    )
                    st.dataframe(pt_df, hide_index=True, use_container_width=True, height=min(400, 40 + 35 * len(pt_df)))
                st.success(f"Open **Announcements → {ran_label} → 💡 Idea Board** to browse the scored ideas.")
            else:
                st.info("No scoring run yet this session. Set parameters above then click **Run scoring now**.")


# ═════════════════════════════════════════════════════════════════════════════
#  PAGE: GUIDED DB CLEAN-UP
# ═════════════════════════════════════════════════════════════════════════════

elif page == "Guided DB Clean-up":

    st.markdown("""<div class="page-head">
      <h1>🧹 Guided DB Clean-up</h1>
      <p>Purge old rows out of any of the four source databases — plus the Idea Board, Tracker,
         and Announcement Tracker tables that hang off them — for a chosen date range.
         Dry-run counts first, delete only after you type the confirmation.</p>
    </div>""", unsafe_allow_html=True)

    st.session_state.setdefault("dbc_params", {
        "sources": ["BSE Equity"],
        "tables": {"BSE Equity": ["announcements"]},
        "date_mode": "Last 30 days",
        "from_date": date.today() - timedelta(days=30),
        "to_date": date.today() - timedelta(days=1),
        "cascade_tracker": False,
        "vacuum": True,
    })
    DP = st.session_state.dbc_params
    st.session_state.setdefault("dbc_last_preview", None)
    st.session_state.setdefault("dbc_last_fingerprint", None)
    st.session_state.setdefault("dbc_last_result", None)

    dbc_tab1, dbc_tab2, dbc_tab3 = st.tabs(["① Scope & date range", "② Preview", "③ Run clean-up"])

    # ═══════════════════════ ① SCOPE & DATE RANGE ═══════════════════════
    with dbc_tab1:
        st.caption("Pick which databases/tables are in scope, then the date range to purge. "
                   "Nothing is touched here — this only sets up step ②'s dry run.")

        st.markdown("**Databases & tables to clean**")
        for label in DB_PATHS.keys():
            tabs_cfg = DBC_SOURCE_TABLES.get(label, [])
            db_path = DB_PATHS[label]
            exists = Path(db_path).exists()
            with st.container(border=True):
                hcol1, hcol2 = st.columns([3, 1])
                src_on = hcol1.checkbox(f"**{label}**", value=label in DP["sources"], key=f"dbc_src_{label}")
                hcol2.caption(f"`{db_path}`" + ("" if exists else "  · not found yet"))
                if src_on:
                    if label not in DP["sources"]:
                        DP["sources"].append(label)
                    current_tables = DP["tables"].get(label, [t["table"] for t in tabs_cfg])
                    cols = st.columns(len(tabs_cfg)) if tabs_cfg else [st]
                    chosen = []
                    for col, tcfg in zip(cols, tabs_cfg):
                        checked = col.checkbox(
                            tcfg["label"], value=tcfg["table"] in current_tables,
                            key=f"dbc_tbl_{label}_{tcfg['table']}",
                        )
                        if checked:
                            chosen.append(tcfg["table"])
                        if tcfg["children"]:
                            col.caption("cascades: " + ", ".join(tcfg["children"]))
                    DP["tables"][label] = chosen
                else:
                    if label in DP["sources"]:
                        DP["sources"].remove(label)
                    DP["tables"].pop(label, None)

        st.markdown("**Date range**")
        dcol1, dcol2 = st.columns([1.4, 2])
        date_presets = ["Last 30 days", "Last 90 days", "Last 365 days", "Older than 90 days", "Custom range"]
        with dcol1:
            DP["date_mode"] = st.radio("Preset", date_presets, index=date_presets.index(DP["date_mode"]), key="dbc_date_mode")
        today = date.today()
        if DP["date_mode"] == "Last 30 days":
            DP["from_date"], DP["to_date"] = today - timedelta(days=30), today - timedelta(days=1)
        elif DP["date_mode"] == "Last 90 days":
            DP["from_date"], DP["to_date"] = today - timedelta(days=90), today - timedelta(days=1)
        elif DP["date_mode"] == "Last 365 days":
            DP["from_date"], DP["to_date"] = today - timedelta(days=365), today - timedelta(days=1)
        elif DP["date_mode"] == "Older than 90 days":
            DP["from_date"], DP["to_date"] = date(2000, 1, 1), today - timedelta(days=90)
        else:
            with dcol2:
                custom = st.date_input("Custom range", value=(DP["from_date"], DP["to_date"]), key="dbc_custom_range")
                if isinstance(custom, tuple) and len(custom) == 2:
                    DP["from_date"], DP["to_date"] = custom
        st.caption(f"Selected window: **{DP['from_date']:%d-%m-%Y} → {DP['to_date']:%d-%m-%Y}** (inclusive)")

        st.markdown("**Cross-source options**")
        ccol1, ccol2 = st.columns(2)
        DP["cascade_tracker"] = ccol1.checkbox(
            "Also remove Announcement Tracker entries linked to deleted rows",
            value=DP["cascade_tracker"], key="dbc_cascade_tracker",
            help="Only rows tracked from a source announcement that's being deleted in this same run — "
                 "manually-created tracker entries with no source link are never touched.",
        )
        DP["vacuum"] = ccol2.checkbox(
            "VACUUM affected databases afterward (reclaim disk space)",
            value=DP["vacuum"], key="dbc_vacuum",
        )

        if not DP["sources"]:
            st.info("Select at least one database above to continue.")

    # ═══════════════════════ ② PREVIEW ═══════════════════════
    with dbc_tab2:
        scope = {lbl: DP["tables"].get(lbl, []) for lbl in DP["sources"] if DP["tables"].get(lbl)}
        if not scope:
            st.info("Nothing selected yet — pick databases/tables and a date range in step ① first.")
        else:
            st.caption(
                f"**{sum(len(v) for v in scope.values())} table(s)** across **{len(scope)} database(s)** · "
                f"**{DP['from_date']:%d-%m-%Y} → {DP['to_date']:%d-%m-%Y}**"
            )
            run_preview = st.button("🔍 Run preview (dry run — nothing is deleted)", type="primary", key="dbc_run_preview")
            if run_preview:
                with st.spinner("Counting matching rows …"):
                    preview_df = dbc_build_preview(scope, DP["from_date"], DP["to_date"], DP["cascade_tracker"])
                st.session_state["dbc_last_preview"] = preview_df
                st.session_state["dbc_last_fingerprint"] = dbc_scope_fingerprint(
                    scope, DP["from_date"], DP["to_date"], DP["cascade_tracker"], DP["vacuum"]
                )

            preview_df = st.session_state.get("dbc_last_preview")
            if preview_df is not None and not preview_df.empty:
                total_rows = int(preview_df["Matching rows"].sum())
                total_linked = int(preview_df["Linked idea/tracker rows"].sum())
                m1, m2, m3 = st.columns(3)
                _metric(m1, f"{total_rows:,}", "Rows that would be deleted")
                _metric(m2, f"{total_linked:,}", "Linked idea/tracker rows cascaded")
                _metric(m3, DP["to_date"].strftime("%d-%m-%Y"), "Window end")
                st.dataframe(preview_df, hide_index=True, use_container_width=True)
                current_fp = dbc_scope_fingerprint(scope, DP["from_date"], DP["to_date"], DP["cascade_tracker"], DP["vacuum"])
                if current_fp != st.session_state.get("dbc_last_fingerprint"):
                    st.warning("Scope or date range changed since this preview ran — re-run the preview before deleting.")
                elif total_rows == 0:
                    st.info("No matching rows in this window — nothing to clean up.")
                else:
                    st.success("Preview is current for step ③ — you can proceed to **Run clean-up**.")
            elif preview_df is not None:
                st.info("Preview ran but found no matching rows in this window.")
            else:
                st.info("Click **Run preview** to see what would be deleted, before anything is touched.")

    # ═══════════════════════ ③ RUN CLEAN-UP ═══════════════════════
    with dbc_tab3:
        scope = {lbl: DP["tables"].get(lbl, []) for lbl in DP["sources"] if DP["tables"].get(lbl)}
        preview_df = st.session_state.get("dbc_last_preview")
        current_fp = dbc_scope_fingerprint(scope, DP["from_date"], DP["to_date"], DP["cascade_tracker"], DP["vacuum"]) if scope else None
        preview_ok = (
            scope and preview_df is not None and not preview_df.empty
            and current_fp == st.session_state.get("dbc_last_fingerprint")
            and int(preview_df["Matching rows"].sum()) > 0
        )

        if not scope:
            st.info("Set a scope in step ① first.")
        elif not preview_ok:
            st.warning("Run a fresh preview in step ② for the current scope/date range before deleting anything.")
        else:
            total_rows = int(preview_df["Matching rows"].sum())
            st.warning(
                f"This will permanently delete **{total_rows:,} row(s)** (plus linked idea/tracker rows) "
                f"from **{DP['from_date']:%d-%m-%Y} → {DP['to_date']:%d-%m-%Y}**. This cannot be undone."
            )
            st.dataframe(preview_df, hide_index=True, use_container_width=True)
            confirm_text = st.text_input(
                'Type DELETE (in capitals) to arm the button below', key="dbc_confirm_text",
            )
            armed = confirm_text.strip() == "DELETE"
            run_delete = st.button(
                "🗑️ Permanently delete now", type="primary", disabled=not armed, key="dbc_run_delete",
            )
            if run_delete and armed:
                with st.spinner("Deleting matched rows and cascading linked tables …"):
                    result = dbc_run_cleanup(
                        scope, DP["from_date"], DP["to_date"],
                        DP["cascade_tracker"], DP["vacuum"], actor=current_user,
                    )
                st.session_state["dbc_last_result"] = result
                st.session_state["dbc_last_preview"] = None
                st.session_state["dbc_last_fingerprint"] = None
                st.cache_data.clear()

            result = st.session_state.get("dbc_last_result")
            if result:
                st.markdown("**Last run**")
                if result["errors"]:
                    for err in result["errors"]:
                        st.error(err)
                res_rows = [
                    {"Source": r["source"], "Table": r["table_label"], "Rows deleted": r["rows_deleted"],
                     "Linked rows cascaded": sum(r["children_deleted"].values())}
                    for r in result["per_source"]
                ]
                if res_rows:
                    st.dataframe(pd.DataFrame(res_rows), hide_index=True, use_container_width=True)
                if result["tracker_deleted"]:
                    st.caption(f"Announcement Tracker: {result['tracker_deleted']:,} linked entr{'y' if result['tracker_deleted']==1 else 'ies'} removed.")
                if result["vacuumed"]:
                    st.caption("Vacuumed: " + ", ".join(result["vacuumed"]))
                if not result["errors"]:
                    st.success("Clean-up complete.")
            else:
                st.info("No clean-up run yet this session.")
