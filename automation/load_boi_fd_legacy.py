"""One-time loader: populate Bank of India's fd_rates from cross-verified
secondary-source aggregator data — NOT a live fetch, and NOT primary-sourced
(unlike every other bank's fd_rates row in this project so far).

Why this exists: bankofindia.bank.in has real, working Cloudflare bot-defense
that blocks automated access entirely — confirmed repeatedly across sessions
(plain HTTP, headless Playwright, and re-confirmed live again on 2026-08-24,
same "Just a moment... performing security verification" challenge every
time). BOI was the only one of 17 banks left with zero fd_rates coverage.

PROVENANCE — this is a deliberate, flagged exception to this project's
primary-source-only convention for fd_rates specifically (loan_rates already
had secondary-sourced rows for a few banks; fd_rates never had). Cross-
checked across THREE independent aggregators before trusting the numbers:
  - Scripbox (scripbox.com/fixed-deposit/bank-of-india-fd-rates),
    rates "as on 5th Feb 2026" — 1-year: 6.25%/6.75% (general/senior).
  - Paisabazaar (paisabazaar.com/fd-fixed-deposit-calculator/boi-fd-calculator),
    rates "as of 2 March 2026" — 1-year: 6.25% general, confirms same figure.
  - BankBazaar (bankbazaar.com/fixed-deposit/bank-of-india-fixed-deposit-rate.html),
    the fullest table (15 tenure bands), same effective date 2 March 2026,
    page itself last-updated 23 August 2026 — 1-year: 6.25%/6.75%, matching
    the other two exactly. Used as the primary data source below since it's
    the most complete and most recently refreshed of the three.
All three agree on every figure they overlap on — real cross-verification,
not a single unverified source. Still genuinely secondary-sourced (none of
these are BOI's own page), so both the DB row and the RAG doc built from it
say so honestly, same as every other secondary-sourced doc in this project
(e.g. PNB's non-FD products, Axis's home loan, IndusInd's home/gold/MSME).

Tenure-label wording below is re-phrased from BankBazaar's own text (not a
verbatim quote) so it parses correctly through this project's own
`tenure_utils.parse_tenure_range()` — confirmed BEFORE writing this file
that copying BankBazaar's literal wording verbatim (e.g. "2 to <3 years")
loses the leading number entirely under this parser (matches only the
upper bound as an exact point, not the real range) — same class of
gotcha tenure_utils.py's own docstring documents for PNB/Union
Bank/ICICI/HDFC's real page wording. Every tenure-label below was
individually verified against `parse_tenure_range()` to (a) parse to the
correct real day-range and (b) cover every one of app.py's selectable
_TENURE_OPTIONS days (30/90/180/365/730/1095/1825/3650) exactly once, no
gaps, no double-coverage — see PROJECT_STATUS.md §21 for the verification
output. The real rates and real tenure boundaries are unchanged from the
source; only the label PHRASING was adjusted for correct parsing.

Run once (or again if the aggregator-sourced rates change):

    python automation/load_boi_fd_legacy.py
"""

from __future__ import annotations

from datetime import datetime

from db import get_connection

SOURCE_URL = (
    "Secondary-sourced (BOI's own site is Cloudflare-blocked — confirmed "
    "repeatedly, see module docstring): cross-verified across BankBazaar "
    "(bankbazaar.com/fixed-deposit/bank-of-india-fixed-deposit-rate.html), "
    "Scripbox, and Paisabazaar as of 2026-08-24. Effective date per source: "
    "2 March 2026 (BankBazaar/Paisabazaar) / 5 Feb 2026 (Scripbox) — all "
    "three agree on every overlapping figure."
)

_EFFECTIVE_DATE = "2026-03-02"

_ROWS = [
    {"tenure_label": "7 days to 14 days",           "general": 3.00, "senior": 3.50},
    {"tenure_label": "15 days to 30 days",           "general": 3.00, "senior": 3.50},
    {"tenure_label": "31 days to 45 days",           "general": 3.00, "senior": 3.50},
    {"tenure_label": "46 days to 90 days",           "general": 4.50, "senior": 5.00},
    {"tenure_label": "91 days to 179 days",          "general": 4.25, "senior": 4.75},
    {"tenure_label": "180 days to 210 days",         "general": 5.50, "senior": 6.00},
    {"tenure_label": "211 days to 269 days",         "general": 5.50, "senior": 6.00},
    {"tenure_label": "270 days to below 1 year",     "general": 5.50, "senior": 6.00},
    {"tenure_label": "1 year",                       "general": 6.25, "senior": 6.75},
    {"tenure_label": ">1 year to below 2 years",     "general": 6.30, "senior": 6.80},
    {"tenure_label": "450 days (special tenor)",     "general": 6.60, "senior": 7.10},
    {"tenure_label": "2 years to below 3 years",     "general": 6.30, "senior": 6.80},
    {"tenure_label": "3 years to below 5 years",     "general": 6.25, "senior": 6.75},
    {"tenure_label": "5 years to below 8 years",     "general": 6.00, "senior": 6.50},
    {"tenure_label": "8 years to 10 years",          "general": 6.00, "senior": 6.50},
]


def run() -> None:
    con = get_connection()
    con.execute("DELETE FROM fd_rates WHERE bank_id = 'boi'")  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    for row in _ROWS:
        con.execute(
            """
            INSERT INTO fd_rates VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "boi", "fixed_deposit", row["tenure_label"], None, None,
                row["general"], row["senior"], None, _EFFECTIVE_DATE,
                SOURCE_URL, fetched_at, "manual_load_frozen", None, "retail",
            ],
        )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["boi_fd_rates", fetched_at,
         f"{len(_ROWS)} rows loaded from cross-verified secondary sources "
         "(BOI's own site is Cloudflare-blocked; see module docstring)"],
    )
    con.close()

    print(f"Loaded {len(_ROWS)} Bank of India fd_rates rows (manual/frozen, secondary-sourced):")
    for row in _ROWS:
        print(f"  {row['tenure_label']:<30} general {row['general']}%  senior {row['senior']}%")


if __name__ == "__main__":
    run()
