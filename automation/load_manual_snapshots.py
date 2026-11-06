"""Loads manual FD-rate snapshots from data/manual/*.csv into fd_rates,
replacing (never accumulating) each (bank_id, product_type) pair's rows.

Why this exists: Union Bank's FD page went from an intermittent WAF
block (confirmed bypassed as of 2026-08-19) to a persistent one
(~60 consecutive fetch errors, confirmed 2026-11-07 -- a standalone
Playwright fetch of the same URL returns the WAF's own block page
verbatim, not a wait-strategy or URL problem, see PROJECT_STATUS.md
§64/§65 and config/banks.json's union_bank note). automation.duckdb
still needs a current FD rate for this bank; the live pipeline can no
longer get one. Distinct from load_union_bank_legacy.py (which parses
prose out of a RAG document's own text, and is already superseded/kept
only for reference) -- this reads a plain, hand-copied CSV instead,
meant to be quick to refresh by re-pasting the bank's own published
table, not a one-off text parse.

Run by the daily workflow (daily-refresh.yml), after the live extract
steps and before the data commit, so a manual snapshot's rows always
reflect the latest file in data/manual/ -- same "replace, don't
accumulate on rerun" discipline every other fd_rates writer in this
project already follows (extract_structured.py's own DELETE-then-INSERT,
load_union_bank_legacy.py, load_indian_bank_legacy.py, ...).

A validation failure on ANY row of a CSV aborts loading THAT file
entirely (prints a clear error, leaves the bank's existing fd_rates rows
untouched) rather than partially replacing good data with a half-valid
set -- same principle as extract_structured.py never DELETEing a bank's
rows until it has a full new set ready to insert.

Run standalone:

    python automation/load_manual_snapshots.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(AUTOMATION_DIR))

from db import get_connection  # noqa: E402

MANUAL_DIR = PROJECT_ROOT / "data" / "manual"

# Same plausibility bound FDRateRecord already enforces on the one other
# place an LLM output reaches a real rate number (SBI's Groq extraction
# path, extract_structured.py) -- reused here for a different reason: a
# hand-typed CSV is just as capable of a fat-fingered digit (e.g. "62.0"
# instead of "6.20") as a bad LLM call is, and this is the only
# validation a manual snapshot gets before it's live in Compare/the
# chat shortcut.
_RATE_MIN, _RATE_MAX = 0.0, 15.0


def _validate_rate(raw_value: str, context: str) -> float | None:
    """None for a genuinely blank cell (no rate published/found for this
    tenure/customer type -- e.g. Union Bank's senior column, left blank
    rather than assuming a premium that wasn't actually confirmed) --
    never coerced to 0.0, which would be a real, wrong rate."""
    if raw_value is None or raw_value.strip() == "":
        return None
    rate = float(raw_value)
    if not (_RATE_MIN <= rate <= _RATE_MAX):
        raise ValueError(
            f"{context}: rate {rate}% is outside the plausible "
            f"{_RATE_MIN:g}-{_RATE_MAX:g}% bound -- refusing to load"
        )
    return rate


def load_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for i, raw in enumerate(reader, start=2):  # row 1 is the header
            context = f"{path.name} row {i} ({raw.get('tenure_label', '?')!r})"
            rows.append({
                "bank_id": raw["bank_id"],
                "product_type": raw["product_type"],
                "tenure_label": raw["tenure_label"],
                "interest_rate_general": _validate_rate(raw["interest_rate_general"], context),
                "interest_rate_senior": _validate_rate(raw["interest_rate_senior"], context),
                "effective_date": raw["effective_date"] or None,
                "fetched_at": raw["fetched_at"],
                "source_url": raw["source_url"],
                "data_source": raw["data_source"],
            })
    return rows


def run() -> None:
    con = get_connection()
    csv_paths = sorted(MANUAL_DIR.glob("*.csv"))
    if not csv_paths:
        print(f"No manual snapshot CSVs found in {MANUAL_DIR} -- nothing to load.")
        con.close()
        return

    for path in csv_paths:
        try:
            rows = load_csv(path)
        except (ValueError, KeyError) as exc:
            print(f"{path.name}: REJECTED, not loaded -- {type(exc).__name__}: {exc}")
            continue

        if not rows:
            print(f"{path.name}: 0 rows parsed -- not touching fd_rates (never replace real data with nothing)")
            continue

        # One CSV is expected to cover exactly one (bank_id, product_type)
        # pair, but this loops over whatever pairs are actually present
        # rather than assuming -- a CSV covering more than one pair still
        # replaces each of its own pairs correctly, nothing else.
        pairs = sorted({(r["bank_id"], r["product_type"]) for r in rows})
        for bank_id, product_type in pairs:
            con.execute(
                "DELETE FROM fd_rates WHERE bank_id = ? AND product_type = ?",
                [bank_id, product_type],
            )
        for r in rows:
            con.execute(
                """
                INSERT INTO fd_rates
                (bank_id, product_type, tenure_label, tenure_days_min, tenure_days_max,
                 interest_rate_general, interest_rate_senior, min_deposit,
                 effective_date, source_url, fetched_at, data_source, deposit_ceiling, deposit_variant)
                VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL, ?, ?, ?, ?, NULL, 'retail')
                """,
                [
                    r["bank_id"], r["product_type"], r["tenure_label"],
                    r["interest_rate_general"], r["interest_rate_senior"],
                    r["effective_date"], r["source_url"], r["fetched_at"], r["data_source"],
                ],
            )
        print(f"{path.name}: {len(rows)} row(s) loaded for {pairs}")

    con.close()


if __name__ == "__main__":
    run()
