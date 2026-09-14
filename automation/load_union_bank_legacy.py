"""One-time loader: populate Union Bank's fd_rates from the already-
verified content in data/raw/union_bank/union_bank_fd_rates.txt — NOT a
live fetch.

Why this exists: unionbankofindia.bank.in is behind a WAF that blocks a
real headless-browser fetch outright ('Web Page Blocked! ... Attack ID'
response — confirmed via Playwright, see config/banks.json's union_bank
note). UNLIKE BOI (which has no precise verified source and stays
unpopulated), a research-time WebFetch call against this same URL DID
succeed and returned a precise, dated (effective 4 August 2026),
tenure-by-tenure table — that's exactly what's already in
union_bank_fd_rates.txt. This loader reads that already-verified content
rather than trying to defeat the WAF (out of scope, not something this
project does), the same approach load_indian_bank_legacy.py uses.

This does NOT get Phase A's automatic freshness re-checking, since it's
not re-fetched live — if the source .txt file's rate section goes stale,
this needs re-running by hand against updated source content.

SUPERSEDED, confirmed 2026-09-01: the WAF block described above no
longer holds. A later live fetch for union_bank succeeded (2026-08-19,
`fetch_log` shows a real `changed=True`/`error=NULL` row against this
same `.../rate-of-interest` URL), and `extract_structured.py`'s own
deterministic parser (`extract_union_bank_rows()`) plus
`extract_loan_rates.py`'s `extract_union_bank_home_loan_rows()` now
fetch Union Bank's FD and home loan rates live and successfully —
`fd_rates`/`loan_rates` currently show `data_source='live_fetch'` for
this bank, not `'manual_load_frozen'`. This script is kept for
reference/rollback only; it is NOT the current data source for Union
Bank and should not be assumed to be when reasoning about which banks
are presently frozen (see PROJECT_STATUS.md §49 — this file's own
docstring was mistakenly read as still-current fact once already).

Run once (or again if the source .txt file's rate section changes):

    python automation/load_union_bank_legacy.py
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from db import get_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = PROJECT_ROOT / "data" / "raw" / "union_bank" / "union_bank_fd_rates.txt"
SOURCE_URL = "https://www.unionbankofindia.bank.in/en/details/rate-of-interest"

_ROW_RE = re.compile(
    r"^-\s*(?P<label>.+?):\s*(?P<general>[\d.]+)%\s*/\s*(?P<senior>[\d.]+)%\s*/\s*[\d.]+%\s*$"
)


def parse_rates(text: str) -> tuple[list[dict], str | None]:
    """Extract the bullet-list FD rate table from union_bank_fd_rates.txt
    (tenure: general% / senior% / super-senior%, one bullet per tenure)."""
    date_match = re.search(r"Effective (\d{1,2}) (\w+) (\d{4})", text)
    effective_date = None
    if date_match:
        try:
            effective_date = datetime.strptime(
                " ".join(date_match.groups()), "%d %B %Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            effective_date = None

    rows = []
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if not m:
            continue
        rows.append({
            "tenure_label": m.group("label"),
            "interest_rate_general": float(m.group("general")),
            "interest_rate_senior": float(m.group("senior")),
        })
    return rows, effective_date


def run() -> None:
    text = SOURCE_FILE.read_text(encoding="utf-8")
    rows, effective_date = parse_rates(text)
    if not rows:
        raise ValueError("Could not find the FD rate bullet list in the source file")

    con = get_connection()
    con.execute("DELETE FROM fd_rates WHERE bank_id = 'union_bank'")  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in rows:
        con.execute(
            """
            INSERT INTO fd_rates
            (bank_id, product_type, tenure_label, tenure_days_min, tenure_days_max,
             interest_rate_general, interest_rate_senior, min_deposit,
             effective_date, source_url, fetched_at, data_source, deposit_ceiling)
            VALUES ('union_bank', 'fixed_deposit', ?, NULL, NULL, ?, ?, NULL, ?, ?, ?, 'manual_load_frozen', 30000000.0)
            """,
            [
                row["tenure_label"], row["interest_rate_general"], row["interest_rate_senior"],
                effective_date, SOURCE_URL, fetched_at,
            ],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["union_bank", fetched_at,
         f"{len(rows)} rows loaded from already-verified data/raw/union_bank/union_bank_fd_rates.txt "
         f"(not a live fetch — union_bank's live site blocks automated access via WAF, see gotchas)"],
    )
    con.close()

    print(f"Loaded {len(rows)} Union Bank FD rate rows (effective_date={effective_date}):")
    for row in rows:
        print(f"  {row['tenure_label']:<25} General: {row['interest_rate_general']}%  "
              f"Senior: {row['interest_rate_senior']}%")


if __name__ == "__main__":
    run()
