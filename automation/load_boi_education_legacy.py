"""One-time loader: populate Bank of India's loan_rates (education_loan)
from a Star Education Loan rate card (Item 8) the user manually sourced
and extracted from an official PDF — NOT a live fetch, same treatment as
load_sbi_home_loan_legacy.py gives SBI's home loan rate.

Why this exists: bankofindia.bank.in is confirmed behind Cloudflare
bot-defense (same class of block as Indian Bank, different vendor — see
config/banks.json's boi entry) — no live fetch has ever succeeded for
this bank, for any product type.

PROVENANCE NOTE, different from load_sbi_home_loan_legacy.py: for SBI's
home loan, this session independently read the actual source PDF file
before trusting it. For this data, the user supplied the already-
extracted table directly in chat (sourced from an official PDF they
verified themselves) — this session did NOT independently re-read the
source PDF. The rows below are taken as given, not re-verified against
the raw file. If the source PDF is ever made available to this session,
re-verify and remove this note.

The "Concessions" line from the source (girl students -0.50%; Engg/
Medical/Management courses -0.50%) is a blanket modifier that can apply
to more than one row above, not a distinct product/tier of its own — not
loaded as a separate row, same "capture as a note, not a per-row field"
convention already used for Punjab & Sind's minimum-ROI-floor footnote
(see extract_punjab_sind_home_loan_rows in extract_loan_rates.py).

Run once (or again if the source rate card changes):

    python automation/load_boi_education_legacy.py
"""

from __future__ import annotations

from datetime import datetime

from db import get_connection

SOURCE_URL = (
    "Bank of India Star Education Loan rate card, Item 8 (official PDF, "
    "user-supplied 2026-08-20 — not independently re-fetched or re-read "
    "by this session; see module docstring)"
)
_EFFECTIVE_DATE = "2026-07-01"  # "effective 01.07.2026" per the source
_BENCHMARK_RATE = 8.10  # RBLR, effective 01.01.2026

_ROWS = [
    {
        "rate_min": 9.80, "rate_max": 9.80,
        "tier_type": "loan_amount",
        "tier_label": "IBA scheme (up to Rs.7.50 Lakh, CGFSEL)",
        "benchmark_type": "RBLR",
    },
    {
        "rate_min": 9.60, "rate_max": 9.60,
        "tier_type": "loan_amount",
        "tier_label": "IBA scheme (above Rs.7.50 Lakh)",
        "benchmark_type": "RBLR",
    },
    {
        "rate_min": 6.85, "rate_max": 6.85,
        "tier_type": "scheme",
        "tier_label": "Star Vidyalaxmi (Category A)",
        "benchmark_type": "RBLR",
    },
    {
        "rate_min": 7.50, "rate_max": 7.50,
        "tier_type": "scheme",
        "tier_label": "Star Vidyalaxmi (Category B)",
        "benchmark_type": None,  # flat figure only, no formula stated in source
    },
    {
        "rate_min": 8.40, "rate_max": 8.40,
        "tier_type": "scheme",
        "tier_label": "Star Vidyalaxmi (Category C)",
        "benchmark_type": "RBLR",
    },
    {
        "rate_min": 9.60, "rate_max": 9.60,
        "tier_type": "scheme",
        "tier_label": "Pradhan Mantri Kaushal Rin Yojana (PMKRY)",
        "benchmark_type": "RBLR",
    },
    {
        "rate_min": 9.80, "rate_max": 9.80,
        "tier_type": "scheme",
        "tier_label": "Star Progressive Education Loan (general)",
        "benchmark_type": "RBLR",
    },
    {
        "rate_min": 8.80, "rate_max": 8.80,
        "tier_type": "scheme",
        "tier_label": "Star Progressive Education Loan (bank staff's children)",
        "benchmark_type": "RBLR",
    },
]


def run() -> None:
    con = get_connection()
    con.execute(
        "DELETE FROM loan_rates WHERE bank_id = 'boi' AND loan_type = 'education_loan'"
    )  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in _ROWS:
        con.execute(
            """
            INSERT INTO loan_rates
            (bank_id, loan_type, rate_type, benchmark_type, benchmark_rate,
             rate_min, rate_max, tier_type, tier_label, processing_fee, max_ltv,
             effective_date, source_url, fetched_at, data_source)
            VALUES ('boi', 'education_loan', 'floating', ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 'manual_load_frozen')
            """,
            [
                row["benchmark_type"],
                _BENCHMARK_RATE if row["benchmark_type"] else None,
                row["rate_min"], row["rate_max"],
                row["tier_type"], row["tier_label"],
                _EFFECTIVE_DATE, SOURCE_URL, fetched_at,
            ],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["boi_education_loan", fetched_at,
         f"{len(_ROWS)} rows loaded from a user-supplied PDF rate card (not a "
         "live fetch — bankofindia.bank.in is Cloudflare-blocked; see "
         "load_boi_education_legacy.py docstring)"],
    )
    con.close()

    print(f"Loaded {len(_ROWS)} BOI education_loan rows (manual/frozen):")
    for row in _ROWS:
        print(f"  {row['tier_label']:<58} {row['rate_min']}%")


if __name__ == "__main__":
    run()
