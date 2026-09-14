from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# settings.py sits in the project root, next to app.py — so parents[0] is enough.
PROJECT_ROOT = Path(__file__).resolve().parents[0]


def _bool_env(name: str, default: bool = False) -> bool:
    """
    os.getenv() always returns a string (or None) — bool("false") is True
    in plain Python, so a naive bool(os.getenv(...)) is a real bug waiting
    to happen. This checks the actual text instead of relying on truthiness.
    """
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


# ── Feature toggle ────────────────────────────────────────────────
# Mirrors NyayaAgent's CHROMA_READY. While this is False, the app keeps
# using the existing single-bank index untouched. Flip it once the
# multi-bank index is actually built and verified — nothing else in the
# app needs to change to make that switch.
MULTI_BANK_READY: bool = _bool_env("MULTI_BANK_READY", default=False)

# ── Paths ────────────────────────────────────────────────────────
RAW_DIR: Path = Path(os.getenv("RAW_DIR", str(PROJECT_ROOT / "data" / "raw")))
INDEX_DIR: Path = Path(os.getenv("INDEX_DIR", str(PROJECT_ROOT / "indexes")))
RAW_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DIR.mkdir(parents=True, exist_ok=True)

# Existing single-bank index — unchanged, keeps working while
# MULTI_BANK_READY is False.
LEGACY_INDEX_PATH: Path = INDEX_DIR / "indian_bank.index"
LEGACY_CHUNKS_PATH: Path = INDEX_DIR / "chunks.json"

# New pools — split into two, deliberately: RBI is a regulator, not a
# commercial bank, and keeping it physically separate matches that and
# sets up clean per-pool querying (see chat — Phase 2 routing).
OTHER_BANKS_INDEX_PATH: Path = INDEX_DIR / "other_banks.index"
OTHER_BANKS_CHUNKS_PATH: Path = INDEX_DIR / "other_banks_chunks.json"

RBI_INDEX_PATH: Path = INDEX_DIR / "rbi.index"
RBI_CHUNKS_PATH: Path = INDEX_DIR / "rbi_chunks.json"

# ── RAG settings (same defaults as your current app.py) ───────────
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
CHUNK_SIZE: int = int(os.getenv("CHUNK_SIZE", "400"))
CHUNK_OVERLAP: int = int(os.getenv("CHUNK_OVERLAP", "50"))
TOP_K: int = int(os.getenv("TOP_K", "4"))
# 450 was tuned for the retired "llama-3.1-8b-instant" model and never
# revisited after switching to "openai/gpt-oss-20b" (2026-08-17, that
# model's own retirement) — gpt-oss-20b is a reasoning model that spends
# hidden "thinking" tokens before it emits any visible content, and those
# count against max_tokens too. Confirmed directly (see repro during a
# 13-bank general-chat query): at 450 the model burned the entire budget
# on reasoning and returned a completely empty answer (finish_reason
# "length", zero visible characters); at 700 it produced real content but
# still got cut off mid-sentence; at 1000 it finished naturally
# (finish_reason "stop") with a complete, accurate, well-structured
# answer. Set with real headroom above that observed minimum.
MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "1200"))

# ── LLM (Groq — same provider you already use) ─────────────────────
# Tries Streamlit secrets first (for the deployed app), falls back to
# .env (so this module also works from a plain CLI script or notebook,
# which st.secrets alone cannot do).
try:
    import streamlit as st

    GROQ_API_KEY: str = st.secrets.get("GROQ_API_KEY", os.getenv("GROQ_API_KEY", ""))
except Exception:
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

# Login accounts for the app's own sign-in screen (settings.check_login() in
# app.py) — same st.secrets-first pattern as GROQ_API_KEY above, so real
# usernames/passwords never sit in source that gets committed. See
# .streamlit/secrets.toml.example for the [allowed_users] table shape.
# Empty dict (not a crash) when secrets.toml doesn't exist yet — e.g. a
# fresh clone before local setup — check_login() shows a clear setup
# message in that case rather than a silent, unexplained lockout.
try:
    import streamlit as st

    ALLOWED_USERS: dict[str, str] = dict(st.secrets.get("allowed_users", {}))
except Exception:
    ALLOWED_USERS: dict[str, str] = {}

# "llama-3.1-8b-instant" was retired from Groq's lineup (confirmed via
# client.models.list() during the VITTAM UI rebuild, 2026-08-17 — every
# chat answer was silently falling back to "Something went wrong" before
# this). "openai/gpt-oss-20b" is the closest available equivalent: a
# small, fast general model, verified working against real RAG-context
# prompts at the existing MAX_TOKENS budget.
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

# ── Known sources / metadata taxonomy ────────────────────────────────
# Three separate axes, deliberately kept independent (see chat):
#   BANK         -> who said it
#   CATEGORY     -> what it's about (shared across bank docs AND RBI docs,
#                   so a loan question can retrieve both at once)
#   SOURCE_TYPE  -> what kind of document / how authoritative it is
#
# BANKS is grouped, not flat — Indian Bank runs on its own untouched
# legacy index (it's a commercial/PSU bank too — kept separate for
# stability and its own audience, not because it's a different category);
# OTHER_BANKS is the peer group being added; RBI is the regulator, kept
# structurally separate from both (see Phase 2).
INDIAN_BANK: str = "INDIAN_BANK"
OTHER_BANKS: list[str] = ["SBI", "BOB", "CANARA", "PNB", "BOI", "UNION_BANK", "IOB", "ICICI", "HDFC", "KOTAK_MAHINDRA", "AXIS", "INDUSIND", "CENTRAL_BANK", "PUNJAB_SIND", "BOM", "UCO"]
RBI: str = "RBI"

BANKS: list[str] = [INDIAN_BANK] + OTHER_BANKS + [RBI]

CATEGORIES: list[str] = [
    "DEPOSIT",
    "LOAN",
    "DIGITAL",
    "RATES",
    "KYC",
    "GRIEVANCE",
    "DEPOSIT_INSURANCE",
    "DIGITAL_LENDING",
    "GENERAL",  # organizational/leadership facts about a bank or RBI —
                # not a product, rate, or regulatory rule in itself
    "PRUDENTIAL",  # capital/risk/asset-quality supervisory rules — capital
                   # adequacy, credit risk management, asset classification,
                   # wilful defaulters — distinct from a customer-facing
                   # product rule (LOAN) or a plain rate (RATES)
]

SOURCE_TYPES: list[str] = [
    "PRODUCT_PAGE",             # bank's own marketing/FAQ content
    "RBI_MASTER_CIRCULAR",      # stable, evergreen topics reissued periodically
    "RBI_MASTER_DIRECTION",     # consolidated, real-time-updated regulatory topics
    "RBI_CITIZENS_CORNER",      # consumer FAQs, grievance/ombudsman guidance
    "RBI_CURRENT_RATES",        # repo/reverse-repo/CRR/SLR — changes frequently,
                                 # needs a refresh cadence, not a one-time curation
    "RBI_WHATS_NEW",            # press releases/notifications — most volatile,
                                 # candidate for live lookup rather than static index
    "UNKNOWN",
]

# RBI's "Regulation" section spans many entity types (NBFCs, co-operative banks,
# payments banks, etc.). Scoped deliberately to Commercial Banking only, since
# every bank in BANKS above is a commercial bank.
RBI_REGULATION_SCOPE: str = "COMMERCIAL_BANKING"


def setup_environment() -> None:
    """Place for any global env tweaks. Empty for now — mirrors NyayaAgent's pattern."""
    pass
