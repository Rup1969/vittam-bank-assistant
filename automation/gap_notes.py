"""VITTAM UI helper — bank id normalization, curated gap reasons, and
freshness labeling. Sits alongside compare_rates.py/eligibility.py as a
read-only helper over the same DuckDB tables; touches nothing else.

Two id schemes exist across this project and were never reconciled:
FAISS/chunks.json use settings.py's upper-snake codes (KOTAK_MAHINDRA,
UNION_BANK, INDIAN_BANK...), while config/banks.json and every DuckDB
table use lowercase ids (kotak, union_bank, indian_bank...). Every code
maps via a plain .lower() except Kotak Mahindra, which is KOTAK_MAHINDRA
in FAISS but "kotak" (not "kotak_mahindra") in the automation pipeline —
that one needs an explicit override, everything else doesn't.

GAP_REASONS is hand-written, not derived from config/banks.json's
note/note_home_loan fields — those notes are long, written for a future
engineer (mentions pdfplumber exceptions, Playwright wait strategies,
etc.), and would read as noise in a customer-facing UI. These are the
same underlying facts, just said in one plain sentence.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from db import get_connection

_ID_OVERRIDES = {"KOTAK_MAHINDRA": "kotak"}


def to_automation_id(faiss_bank_code: str) -> str:
    """'KOTAK_MAHINDRA' -> 'kotak', 'UNION_BANK' -> 'union_bank', etc."""
    return _ID_OVERRIDES.get(faiss_bank_code, faiss_bank_code.lower())


_REVERSE_ID_OVERRIDES = {v: k for k, v in _ID_OVERRIDES.items()}


def to_faiss_code(automation_id: str) -> str:
    """'kotak' -> 'KOTAK_MAHINDRA', 'union_bank' -> 'UNION_BANK', etc. —
    the mirror of to_automation_id(), for looking up a bank's real display
    name/color in app.py's BANK_UI (keyed by FAISS code) starting from a
    DuckDB row's bank_id (automation id)."""
    return _REVERSE_ID_OVERRIDES.get(automation_id, automation_id.upper())


# Every id below is an automation-pipeline id (lowercase), not a FAISS code.
GAP_REASONS: dict[str, dict[str, str]] = {
    "fd_rates": {
        # RESOLVED 2026-08-24 — Cloudflare block re-confirmed live one more
        # time (still blocks every automated access attempt), but rather
        # than leave BOI as the sole no-data bank, loaded real rates
        # cross-verified across 3 independent secondary aggregators
        # (BankBazaar/Scripbox/Paisabazaar, all agreeing on every
        # overlapping figure) via load_boi_fd_legacy.py. First fd_rates
        # entry in this project sourced this way — flagged honestly in
        # both the DB row's source_url and the RAG doc, not silently
        # treated as primary-sourced. See PROJECT_STATUS.md §21.
    },
    "home_loan": {
        # union_bank and iob RESOLVED 2026-08-19 (loan_rates coverage
        # extension) — union_bank's PDF link, previously broken (HTTP
        # 500), now fetches cleanly on retry; iob's rate table, previously
        # not locatable within the earlier bounded search, was found on a
        # fresh search of the exact same hub URL. Both removed from here.
        # sbi and canara ALSO RESOLVED, same day, second pass: sbi loaded
        # manually (data_source='manual_load_frozen', same treatment as
        # Indian Bank's FD rates — real page is genuinely image-only, a
        # user-supplied PDF conversion of the bank's own official rate
        # card was visually verified before loading); canara was found on
        # a real, previously-unassessed URL and extracted live. Both
        # removed from here too.
        # indian_bank RESOLVED 2026-08-24 — user supplied the official
        # retail lending rate card directly as a docx (see
        # automation/load_indian_bank_home_loan_legacy.py); 8 real
        # home-loan-family tiers loaded, same manual_load_frozen
        # treatment as Indian Bank's FD/education loan rates. Removed
        # from here.
        "boi": "Bank of India's site blocks automated access (Cloudflare) — no data available.",
    },
}


def _fetch_log_latest(source_id: str):
    con = get_connection()
    row = con.execute(
        "SELECT fetched_at FROM fetch_log WHERE source_id = ? AND error IS NULL "
        "ORDER BY fetched_at DESC LIMIT 1",
        [source_id],
    ).fetchone()
    con.close()
    return row[0] if row else None


def freshness_label(bank_id: str, category: str, data_source: str | None = None,
                     source_id: str | None = None) -> dict:
    """category is 'rate' (7-day threshold) or 'content' (30-day threshold).

    `data_source` (from fd_rates/loan_rates) short-circuits everything else:
    a manually-loaded row is always "Frozen", regardless of age — there is
    no refresh cycle to measure it against, so calling it "stale" would
    imply a freshness process that doesn't exist for that row.

    Returns {"label": str, "state": "fresh"|"stale"|"frozen"|"unknown", "detail": str}
    — `state` drives the badge color, `label`/`detail` are the display text.
    """
    if data_source == "manual_load_frozen":
        return {"label": "Frozen — manual snapshot", "state": "frozen", "detail": ""}

    sid = source_id or bank_id
    fetched_at = _fetch_log_latest(sid)
    if fetched_at is None:
        return {"label": "No verified source", "state": "unknown", "detail": ""}

    threshold_days = 7 if category == "rate" else 30
    age = datetime.now() - fetched_at
    is_stale = age > timedelta(days=threshold_days)
    date_str = fetched_at.strftime("%d %b %Y")
    if is_stale:
        return {"label": f"stale — {date_str}", "state": "stale", "detail": f"{age.days} days old"}
    return {"label": f"verified {date_str}", "state": "fresh", "detail": ""}


def content_freshness(date_str: str | None) -> dict:
    """Same as freshness_label but for a RAG chunk's own DATE field
    (YYYY-MM-DD string) rather than a fetch_log timestamp — the RAG corpus
    and the automation DB are separately maintained, so this reads a
    different source of truth on purpose."""
    if not date_str:
        return {"label": "No verified source", "state": "unknown", "detail": ""}
    try:
        fetched_at = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return {"label": "No verified source", "state": "unknown", "detail": ""}
    age = datetime.now() - fetched_at
    is_stale = age > timedelta(days=30)
    date_disp = fetched_at.strftime("%d %b %Y")
    if is_stale:
        return {"label": f"stale — {date_disp}", "state": "stale", "detail": f"{age.days} days old"}
    return {"label": f"verified {date_disp}", "state": "fresh", "detail": ""}


def coverage(product: str) -> dict:
    """product is 'fd_rates' or 'home_loan'. Returns
    {"available": [bank_ids...], "missing": [{"bank_id", "reason"}]} across
    all 17 banks, so the UI can show 'N of 17' with named reasons instead
    of missing banks just silently not appearing."""
    from gap_notes import GAP_REASONS  # noqa: avoid shadow confusion in callers

    # Updated 2026-08-19 to the post-PSB-expansion 17-bank roster (was
    # stuck at the pre-expansion 13 — central_bank/punjab_sind/bom/uco
    # were missing here even though all 4 have had real fd_rates data
    # since the PSB rollout, and now have loan_rates data too).
    all_ids = ["indian_bank", "sbi", "bob", "canara", "pnb", "boi", "union_bank",
               "iob", "icici", "hdfc", "kotak", "axis", "indusind",
               "central_bank", "punjab_sind", "bom", "uco"]
    table = "fd_rates" if product == "fd_rates" else "loan_rates"

    con = get_connection()
    present = {r[0] for r in con.execute(f"SELECT DISTINCT bank_id FROM {table}").fetchall()}
    con.close()

    reasons = GAP_REASONS.get(product, {})
    missing = [
        {"bank_id": b, "reason": reasons.get(b, "No data available yet.")}
        for b in all_ids if b not in present
    ]
    return {"available": sorted(present), "missing": missing, "total": len(all_ids)}
