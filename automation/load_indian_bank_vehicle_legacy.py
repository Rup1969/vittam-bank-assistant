"""One-time loader: populate Indian Bank's loan_rates (vehicle_loan)
from a rate card the user manually sourced and extracted from an
official docx — NOT a live fetch, same treatment as
load_indian_bank_education_legacy.py gives Indian Bank's education loan.

Why this exists: Indian Bank's live site has real, working bot-defense
that blocks automated fetches entirely (confirmed repeatedly — see
fetch_and_track.py / project memory), and the existing legacy vehicle-
loan RAG source (data/legacy_indian_bank_source/loan_vehicle.txt) has
no numeric rate at all ("Linked to RLLR — floating rate", no spread/
effective figure) — no manual-load candidate existed until the user
supplied one directly, from the same Indian_Bank_Interest_rate.docx
already used for the home_loan and education_loan fixes.

PROVENANCE NOTE, same caveat as load_indian_bank_education_legacy.py:
the user supplied the already-extracted rates directly in chat (sourced
from an official docx they verified themselves) — this session did NOT
independently re-read the source docx for this specific product. The
five rows below are taken as given, not re-verified against the raw
file. If the source docx is ever made available to this session,
re-verify and remove this note.

Unlike every OTHER bank's vehicle_loan row in this project (flagship-
only scoping — see run_vehicle() in extract_loan_rates.py), all 5 real
named products from the docx are loaded here, matching education_loan's
own precedent of loading every real named scheme rather than picking
one representative row — Indian Bank's manually-sourced docx data has
consistently been loaded in full for this bank, not flagship-trimmed
like the live-fetch/RAG-doc-derived banks.

Run once (or again if the source rate card changes):

    python automation/load_indian_bank_vehicle_legacy.py
"""

from __future__ import annotations

from datetime import datetime

from db import get_connection

SOURCE_URL = (
    "Indian Bank Vehicle Loan rate card (Indian_Bank_Interest_rate.docx, "
    "official docx, user-supplied — same source as the home_loan and "
    "education_loan fixes; not independently re-fetched or re-read by "
    "this session for this product — see module docstring)"
)

_ROWS = [
    {
        "rate_type": "floating",
        "rate_min": 7.55,
        "rate_max": 8.85,
        "tier_type": "scheme",
        "tier_label": "IB Vehicle Loan",
    },
    {
        "rate_type": "floating",
        "rate_min": 7.50,
        "rate_max": 8.75,
        "tier_type": "scheme",
        "tier_label": "IB Vehicle Loan (Eco Vahan)",
    },
    {
        "rate_type": "floating",
        "rate_min": 9.55,
        "rate_max": 11.65,
        "tier_type": "scheme",
        "tier_label": "IB Vehicle Loan (Used Car)",
    },
    {
        "rate_type": "floating",
        "rate_min": 8.90,
        "rate_max": 10.40,
        "tier_type": "scheme",
        "tier_label": "2-Wheeler",
    },
    {
        "rate_type": "floating",
        "rate_min": 8.75,
        "rate_max": 9.40,
        "tier_type": "scheme",
        "tier_label": "Van/Minibus/Bus/Ambulance (institutional)",
    },
]


def run() -> None:
    con = get_connection()
    con.execute(
        "DELETE FROM loan_rates WHERE bank_id = 'indian_bank' AND loan_type = 'vehicle_loan'"
    )  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in _ROWS:
        con.execute(
            """
            INSERT INTO loan_rates
            (bank_id, loan_type, rate_type, benchmark_type, benchmark_rate,
             rate_min, rate_max, tier_type, tier_label, processing_fee, max_ltv,
             effective_date, source_url, fetched_at, data_source)
            VALUES ('indian_bank', 'vehicle_loan', ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 'manual_load_frozen')
            """,
            [
                row["rate_type"], row["rate_min"], row["rate_max"],
                row["tier_type"], row["tier_label"], SOURCE_URL, fetched_at,
            ],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["indian_bank_vehicle_loan", fetched_at,
         f"{len(_ROWS)} rows loaded from a user-supplied docx rate card (not a "
         "live fetch — Indian Bank's site is blocked, and the legacy RAG source "
         "had no numeric rate; see load_indian_bank_vehicle_legacy.py docstring)"],
    )
    con.close()

    print(f"Loaded {len(_ROWS)} Indian Bank vehicle_loan rows (manual/frozen):")
    for row in _ROWS:
        rng = (f"{row['rate_min']}%" if row["rate_min"] == row["rate_max"]
               else f"{row['rate_min']}% - {row['rate_max']}%")
        print(f"  {row['tier_label']:<42} {rng}")


if __name__ == "__main__":
    run()
