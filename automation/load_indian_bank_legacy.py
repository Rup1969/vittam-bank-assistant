"""One-time loader: populate Indian Bank's fd_rates from the already-
existing, already-verified content in
data/legacy_indian_bank_source/deposits_schemes_all.txt — NOT a live fetch.

Why this exists: Indian Bank's live site has real, working bot-defense that
blocks automated fetches entirely (confirmed via both plain HTTP and a
headless browser — see fetch_and_track.py / project memory). Rather than
try to defeat that (out of scope, not something this project does), this
reads the FD rate data that was already hand-researched and verified into
the RAG pipeline's own source files, and structures it the same way
Phase B does for the other banks — without ever touching the live site.

This does NOT get Phase A's automatic freshness re-checking, since it's
not re-fetched live — if the legacy .txt file's rates go stale, this needs
re-running by hand against updated source content, not by rerunning
fetch_and_track.py (which will keep failing for Indian Bank, as expected).

Run once (or again if the source .txt file's rate section changes):

    python automation/load_indian_bank_legacy.py
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from db import get_connection

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILE = PROJECT_ROOT / "data" / "legacy_indian_bank_source" / "deposits_schemes_all.txt"
SOURCE_URL = "https://indianbank.bank.in/category/term-deposits/"

_SENIOR_PREMIUM = 0.50  # "Senior Citizens: Additional 0.50% on all above tenures"
_MIN_DEPOSIT = 1000.0   # "Min Amount: Rs.1,000 (no maximum)"


def parse_rates(text: str) -> tuple[list[dict], str | None]:
    """Extract the flat bullet-list FD rate table this file uses — a
    different shape from the other banks' HTML tables (one General rate
    per tenure, Senior computed as a flat +0.50% premium across the
    board, not a separately-published per-tenure Senior figure)."""
    # Date must come from the FD Interest Rates line specifically — the file
    # has an EARLIER "w.e.f." date for the unrelated Savings Bank rate
    # section, and a global first-match search picked that one up instead
    # (confirmed: produced 2025-07-07, the SB rate's date, when the FD
    # section itself clearly states "w.e.f. 04.02.2026"). Caught by
    # checking the output against the source file, same discipline as
    # every other date/rate bug found in this project.
    fd_header_match = re.search(r"FD Interest Rates\s*\(([^)]+)\)", text)
    date_match = (
        re.search(r"w\.e\.f\.?\s*(\d{2})\.(\d{2})\.(\d{4})", fd_header_match.group(1))
        if fd_header_match else None
    )
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    section_match = re.search(
        r"FD Interest Rates.*?:\n(.*?)\nSenior Citizens", text, re.DOTALL
    )
    if not section_match:
        raise ValueError("Could not find the FD Interest Rates bullet list in the source file")

    rows = []
    for line in section_match.group(1).splitlines():
        m = re.match(r"-\s*(.+?):\s*([\d.]+)%", line.strip())
        if not m:
            continue
        tenure_label, general_rate = m.group(1), float(m.group(2))
        rows.append({
            "tenure_label": tenure_label,
            "interest_rate_general": general_rate,
            "interest_rate_senior": round(general_rate + _SENIOR_PREMIUM, 2),
        })
    return rows, effective_date


def run() -> None:
    text = SOURCE_FILE.read_text(encoding="utf-8")
    rows, effective_date = parse_rates(text)

    con = get_connection()
    con.execute("DELETE FROM fd_rates WHERE bank_id = 'indian_bank'")  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in rows:
        con.execute(
            """
            INSERT INTO fd_rates
            (bank_id, product_type, tenure_label, tenure_days_min, tenure_days_max,
             interest_rate_general, interest_rate_senior, min_deposit,
             effective_date, source_url, fetched_at, data_source)
            VALUES ('indian_bank', 'fixed_deposit', ?, NULL, NULL, ?, ?, ?, ?, ?, ?, 'manual_load_frozen')
            """,
            [
                row["tenure_label"], row["interest_rate_general"], row["interest_rate_senior"],
                _MIN_DEPOSIT, effective_date, SOURCE_URL, fetched_at,
            ],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["indian_bank", fetched_at,
         f"{len(rows)} rows loaded from legacy .txt file (not a live fetch — "
         f"Indian Bank's live site blocks automated access, see gotchas)"],
    )
    con.close()

    print(f"Loaded {len(rows)} Indian Bank FD rate rows (effective_date={effective_date}):")
    for row in rows:
        print(f"  {row['tenure_label']:<25} General: {row['interest_rate_general']}%  "
              f"Senior: {row['interest_rate_senior']}%")


if __name__ == "__main__":
    run()
