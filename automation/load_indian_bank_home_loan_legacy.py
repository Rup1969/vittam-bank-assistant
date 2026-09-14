"""One-time loader: populate Indian Bank's loan_rates (home_loan) from
a rate card the user supplied directly as a docx file — NOT a live
fetch, same treatment as load_sbi_home_loan_legacy.py and
load_indian_bank_education_legacy.py.

Why this exists: Indian Bank's live site has real, working bot-defense
that blocks automated fetches entirely (confirmed via both plain HTTP
and a headless browser — see fetch_and_track.py / project memory), and
the existing legacy home-loan RAG source
(data/legacy_indian_bank_source/loan_home.txt) had no numeric rate at
all ("Linked to RLLR — floating", no spread/effective figure) —
confirmed as a genuine, pre-existing gap on 2026-08-24 (see
PROJECT_STATUS.md §16f). This was the exact gap flagged there; the
user supplied the source docx directly in the very next session.

PROVENANCE: this session independently opened and read the actual
source file (`Indian Bank_Interest rate.docx`, via python-docx) before
trusting it — not taken as given from a paraphrase, unlike the
education-loan load's own documented caveat. The file's own Education
Loan rows (PM Vidyalaxmi 7.25%-8.45%, Education Loan 7.05%-10.15%, IB
SKILL Loan 9.45%) were cross-checked against what's already in
loan_rates and matched exactly — real, independent confirmation the
source is genuine and consistent, not just a rate list found elsewhere.

The full document covers many more product families (vehicle, personal,
jewel, pension, mortgage, gold bond, deposit-backed loans, etc.) beyond
home_loan — only the home-loan-family rows are loaded here, since
loan_rates' schema only tracks fd/home_loan/education_loan today.
Everything else is left for a future session if wanted.

Run once (or again if the source rate card changes):

    python automation/load_indian_bank_home_loan_legacy.py
"""

from __future__ import annotations

from datetime import datetime

from db import get_connection

SOURCE_URL = (
    "Indian Bank_Interest rate.docx — official Indian Bank retail lending "
    "rate card, user-supplied 2026-08-24, independently read by this "
    "session via python-docx before loading (see module docstring)"
)

_ROWS = [
    {"rate_min": 7.15, "rate_max": 8.55, "tier_label": "IB Home Loan / IB Home Loan (NRI)"},
    {"rate_min": 7.65, "rate_max": 9.05, "tier_label": "IB Home Loan (CRE)"},
    {"rate_min": 7.75, "rate_max": 9.15, "tier_label": "Home Loan for EWS, LIG & MIG Individuals (Urban Areas)"},
    {"rate_min": 7.60, "rate_max": 9.10, "tier_label": "Home Loan to Corporate Entity"},
    {"rate_min": 8.15, "rate_max": 9.55, "tier_label": "Plot Loan / Plot Loan (NRI)"},
    {"rate_min": 8.15, "rate_max": 9.55, "tier_label": "IB Home Improve"},
    {"rate_min": 8.15, "rate_max": 8.70, "tier_label": "IB Home Enrich"},
    {"rate_min": 7.65, "rate_max": 8.75, "tier_label": "IB Home Loan Plus"},
]


def run() -> None:
    con = get_connection()
    con.execute(
        "DELETE FROM loan_rates WHERE bank_id = 'indian_bank' AND loan_type = 'home_loan'"
    )  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in _ROWS:
        con.execute(
            """
            INSERT INTO loan_rates
            (bank_id, loan_type, rate_type, benchmark_type, benchmark_rate,
             rate_min, rate_max, tier_type, tier_label, processing_fee, max_ltv,
             effective_date, source_url, fetched_at, data_source)
            VALUES ('indian_bank', 'home_loan', 'floating', NULL, NULL, ?, ?, 'scheme', ?, NULL, NULL, NULL, ?, ?, 'manual_load_frozen')
            """,
            [row["rate_min"], row["rate_max"], row["tier_label"], SOURCE_URL, fetched_at],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["indian_bank_home_loan", fetched_at,
         f"{len(_ROWS)} rows loaded from a user-supplied docx rate card, independently "
         "read by this session (not a live fetch — Indian Bank's site is blocked, and "
         "the legacy RAG source had no numeric rate; see module docstring)"],
    )
    con.close()

    print(f"Loaded {len(_ROWS)} Indian Bank home_loan rows (manual/frozen):")
    for row in _ROWS:
        rng = (f"{row['rate_min']}%" if row["rate_min"] == row["rate_max"]
               else f"{row['rate_min']}% - {row['rate_max']}%")
        print(f"  {row['tier_label']:<55} {rng}")


if __name__ == "__main__":
    run()
