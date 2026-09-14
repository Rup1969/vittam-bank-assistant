"""One-time loader: populate SBI's loan_rates (home_loan) from a manually
supplied, image-converted PDF of SBI's own official rate card — NOT a live
fetch, same treatment as load_indian_bank_legacy.py gives Indian Bank's
FD rates.

Why this exists: SBI's home loan rate card
(sbi.bank.in/web/interest-rates/interest-rates/loan-schemes-interest-rates/
home-loans-interest-rates-current) is published ONLY as a JPG image, not
as HTML text or a table — confirmed zero <table> elements and zero
numeric rate matches in the page's extracted text during the Stage 3
pre-flight (2026-08-16), logged as a known OCR-required gap. Rather than
build OCR (out of scope), the user supplied a PDF conversion of that same
official image (saved to
data/scraped/sbi_home_loan/2026-08-19_manual_image_converted.pdf) and
this loader's row values were visually verified against it before being
trusted — see the module docstring pattern below for exactly what was
checked.

Verified 2026-08-19 by reading the actual PDF page image and confirming,
verbatim, against the "HOME & HOME RELATED LOAN INTEREST RATE*" table's
"Home Loan (TL)" row:
  - Rate: "7.25% to 8.55%" — matches rate_min/rate_max exactly.
  - Benchmark: "All Home Loans are linked to External bechmark
    Rate(EBLR) Prevailing EBLR=7.90%" — matches benchmark_type/rate.
  - Footnote: "Additional premium of 10bps for non salaried customers
    with CIBIL score of less than 825 on Home Loans and Home Top up
    Loans" — matches tier_label.
  - Date: "w.e.f. 20.05.2026" — matches effective_date.
Every other row on the same page (Home Loan Maxgain/OD, Top Up Loan,
Top Up OD, Loan Against Property, Reverse Mortgage, YONO Insta Home Top
Up) is a DIFFERENT product, not home_loan (TL) — deliberately not loaded
here, matching the user's explicit single-row request.

This does NOT get Phase A's automatic freshness re-checking, since it's
not re-fetched live — if SBI republishes this rate card, this needs
re-running by hand against the new source, not by rerunning
fetch_and_track.py (which has no home_loan URL configured for SBI, and
should stay that way until OCR or a text-based source becomes available).

Run once (or again if the source PDF's rate changes):

    python automation/load_sbi_home_loan_legacy.py
"""

from __future__ import annotations

from datetime import datetime

from db import get_connection

SOURCE_URL = (
    "https://sbi.bank.in/web/interest-rates/interest-rates/loan-schemes-"
    "interest-rates/home-loans-interest-rates-current (image-only rate "
    "card; manually converted to PDF and visually verified — "
    "data/scraped/sbi_home_loan/2026-08-19_manual_image_converted.pdf)"
)

_ROW = {
    "bank_id": "sbi",
    "loan_type": "home_loan",
    "rate_type": "floating",
    "benchmark_type": "EBLR",
    "benchmark_rate": 7.90,
    "rate_min": 7.25,
    "rate_max": 8.55,
    "tier_type": "credit_score",
    "tier_label": "CIBIL below 825 (non-salaried): +10bps",
    "effective_date": "2026-05-20",
}


def run() -> None:
    con = get_connection()
    con.execute(
        "DELETE FROM loan_rates WHERE bank_id = 'sbi' AND loan_type = 'home_loan'"
    )  # replace, don't duplicate on rerun

    fetched_at = str(datetime.now())
    con.execute(
        """
        INSERT INTO loan_rates
        (bank_id, loan_type, rate_type, benchmark_type, benchmark_rate,
         rate_min, rate_max, tier_type, tier_label, processing_fee, max_ltv,
         effective_date, source_url, fetched_at, data_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, 'manual_load_frozen')
        """,
        [
            _ROW["bank_id"], _ROW["loan_type"], _ROW["rate_type"],
            _ROW["benchmark_type"], _ROW["benchmark_rate"],
            _ROW["rate_min"], _ROW["rate_max"],
            _ROW["tier_type"], _ROW["tier_label"],
            _ROW["effective_date"], SOURCE_URL, fetched_at,
        ],
    )

    con.execute(
        "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
        ["sbi_home_loan", fetched_at,
         "1 row loaded from a manually-supplied, image-converted PDF (not a "
         "live fetch — SBI's home loan rate card is image-only, OCR out of "
         "scope, see load_sbi_home_loan_legacy.py docstring)"],
    )
    con.close()

    print("Loaded 1 SBI home_loan row (manual/frozen):")
    print(f"  rate: {_ROW['rate_min']}% - {_ROW['rate_max']}%  "
          f"basis={_ROW['benchmark_type']} ({_ROW['benchmark_rate']}%)  "
          f"effective {_ROW['effective_date']}")
    print(f"  tier: {_ROW['tier_label']}")


if __name__ == "__main__":
    run()
