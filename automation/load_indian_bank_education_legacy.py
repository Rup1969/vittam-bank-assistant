"""One-time loader: populate Indian Bank's loan_rates (education_loan)
from a rate card the user manually sourced and extracted from an
official docx — NOT a live fetch, same treatment as
load_sbi_home_loan_legacy.py gives SBI's home loan rate.

Why this exists: Indian Bank's live site has real, working bot-defense
that blocks automated fetches entirely (confirmed via both plain HTTP
and a headless browser — see fetch_and_track.py / project memory), and
the existing legacy education-loan RAG source
(data/legacy_indian_bank_source/loan_education.txt) has no numeric rate
at all ("Linked to RLLR — floating", no spread/effective figure) — no
manual-load candidate existed until the user supplied one directly.

PROVENANCE NOTE, different from load_sbi_home_loan_legacy.py: for SBI's
home loan, this session independently read the actual source PDF file
before trusting it. For this data, the user supplied the already-
extracted table directly in chat (sourced from an official docx they
verified themselves) — this session did NOT independently re-read the
source docx. The three rows below are taken as given, not re-verified
against the raw file. If the source docx is ever made available to this
session, re-verify and remove this note.

Run once (or again if the source rate card changes):

    python automation/load_indian_bank_education_legacy.py
"""

from __future__ import annotations

from datetime import datetime

from db import get_connection

SOURCE_URL = (
    "Indian Bank Education Loan rate card (official docx, user-supplied "
    "2026-08-20 — not independently re-fetched or re-read by this session; "
    "see module docstring)"
)

_ROWS = [
    {
        "rate_type": "floating",
        "rate_min": 7.25,
        "rate_max": 8.45,
        "tier_type": "scheme",
        "tier_label": "PM Vidyalaxmi",
    },
    {
        "rate_type": "floating",
        "rate_min": 7.05,
        "rate_max": 10.15,
        "tier_type": "scheme",
        "tier_label": "Education Loan (general)",
    },
    {
        "rate_type": "floating",
        "rate_min": 9.45,
        "rate_max": 9.45,
        "tier_type": "scheme",
        "tier_label": "IB SKILL Loan",
    },
]


def run() -> None:
    con = get_connection()
    con.execute(
        "DELETE FROM loan_rates WHERE bank_id = 'indian_bank' AND loan_type = 'education_loan'"
    )  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in _ROWS:
        con.execute(
            """
            INSERT INTO loan_rates
            (bank_id, loan_type, rate_type, benchmark_type, benchmark_rate,
             rate_min, rate_max, tier_type, tier_label, processing_fee, max_ltv,
             effective_date, source_url, fetched_at, data_source)
            VALUES ('indian_bank', 'education_loan', ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 'manual_load_frozen')
            """,
            [
                row["rate_type"], row["rate_min"], row["rate_max"],
                row["tier_type"], row["tier_label"], SOURCE_URL, fetched_at,
            ],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["indian_bank_education_loan", fetched_at,
         f"{len(_ROWS)} rows loaded from a user-supplied docx rate card (not a "
         "live fetch — Indian Bank's site is blocked, and the legacy RAG source "
         "had no numeric rate; see load_indian_bank_education_legacy.py docstring)"],
    )
    con.close()

    print(f"Loaded {len(_ROWS)} Indian Bank education_loan rows (manual/frozen):")
    for row in _ROWS:
        rng = (f"{row['rate_min']}%" if row["rate_min"] == row["rate_max"]
               else f"{row['rate_min']}% - {row['rate_max']}%")
        print(f"  {row['tier_label']:<28} {rng}")


if __name__ == "__main__":
    run()
