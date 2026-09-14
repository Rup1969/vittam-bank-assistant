"""Insights & Tools, Step 2 — FD maturity + loan EMI calculators.

Both functions read the exact same real, already-verified rate data
Compare/digest already trust (`compare_rates.compare_for_tenure()` for
FD, `compare_loan_rates.compare_loan()` for home_loan/education_loan)
— this module adds the ARITHMETIC on top, it doesn't re-derive or
duplicate rate-matching logic.

Explicit, stated assumptions (not hidden inside the math):
- FD maturity: QUARTERLY compounding. Real per-bank compounding
  frequency isn't published anywhere in fd_rates' source data (every
  bank parser this project has built extracts a flat annual rate, never
  a compounding schedule) — quarterly is the industry-standard
  convention for Indian bank FDs, stated in every result, not silently
  assumed.
- Loan EMI: standard reducing-balance (amortizing) EMI formula, using
  the bank's BEST (lowest) published rate for that loan_type — the same
  "one row per bank, best case" convention `compare_loan()` already
  uses for ranking. A real bank tier system (CIBIL score, loan amount,
  institute category, etc.) means the applicant's actual approved rate
  can be higher; the tier_label the best rate came from is always
  returned alongside the number, not hidden.

Run standalone for a demo against real current data:

    python automation/calculator.py
"""

from __future__ import annotations

import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from db import get_connection  # noqa: E402

FD_COMPOUNDING_PERIODS_PER_YEAR = 4  # quarterly — see module docstring


def all_known_banks() -> list[str]:
    """Every bank_id with ANY real data (fd_rates or loan_rates) — used
    to give the chat-shortcut's LLM extraction step a closed set of
    valid bank ids to pick from, rather than letting it invent one."""
    con = get_connection()
    ids = sorted({
        *[r[0] for r in con.execute("SELECT DISTINCT bank_id FROM fd_rates").fetchall()],
        *[r[0] for r in con.execute("SELECT DISTINCT bank_id FROM loan_rates").fetchall()],
    })
    con.close()
    return ids


def fd_banks() -> list[str]:
    """Every bank_id with any real fd_rates data, for a calculator
    bank-selection dropdown."""
    con = get_connection()
    ids = [r[0] for r in con.execute("SELECT DISTINCT bank_id FROM fd_rates ORDER BY bank_id").fetchall()]
    con.close()
    return ids


def loan_banks(loan_type: str) -> list[str]:
    """Every bank_id with real loan_rates data for the given loan_type."""
    con = get_connection()
    ids = [
        r[0] for r in con.execute(
            "SELECT DISTINCT bank_id FROM loan_rates WHERE loan_type = ? ORDER BY bank_id",
            [loan_type],
        ).fetchall()
    ]
    con.close()
    return ids


def fd_maturity(
    bank_id: str, tenure_name: str, amount: float, customer_type: str = "general",
) -> dict | None:
    """Real compound-interest FD maturity value. Returns None if this
    bank has no published rate matching tenure_name (a real,
    reportable "no data" case, not silently coerced to some default
    rate)."""
    from compare_rates import compare_for_tenure
    from digest import TENURE_MILESTONES

    target_days = TENURE_MILESTONES.get(tenure_name)
    if target_days is None:
        return None
    rows = compare_for_tenure(target_days, customer_type)
    row = next((r for r in rows if r["bank_id"] == bank_id), None)
    if row is None:
        return None

    rate = row["rate"]
    years = target_days / 365.0
    n = FD_COMPOUNDING_PERIODS_PER_YEAR
    maturity = amount * (1 + rate / 100 / n) ** (n * years)

    return {
        "bank_id": bank_id,
        "tenure_label": row["tenure_label"],
        "variant_label": row["variant_label"],
        "rate": rate,
        "customer_type": customer_type,
        "principal": amount,
        "maturity_value": round(maturity, 2),
        "interest_earned": round(maturity - amount, 2),
        "compounding": "quarterly",
        "years": round(years, 3),
        "source_url": row["source_url"],
    }


def _amortize(amount: float, annual_rate: float, tenure_years: float) -> dict:
    """Standard reducing-balance EMI arithmetic for an arbitrary rate —
    factored out so both `loan_emi()` (looks up a bank's real published
    rate) and `switch.py` (needs the same formula applied to a
    customer's own current rate, which isn't looked up from any table)
    share one formula, not two copies of it."""
    monthly_rate = annual_rate / 12 / 100
    n_months = max(1, round(tenure_years * 12))

    if monthly_rate == 0:
        emi = amount / n_months
    else:
        factor = (1 + monthly_rate) ** n_months
        emi = amount * monthly_rate * factor / (factor - 1)

    total_payment = emi * n_months
    return {
        "tenure_months": n_months,
        "emi": round(emi, 2),
        "total_payment": round(total_payment, 2),
        "total_interest": round(total_payment - amount, 2),
    }


def _capitalize_moratorium_interest(amount: float, annual_rate: float, moratorium_years: float) -> dict:
    """Real Indian education/some other loans carry a moratorium period
    (course duration + grace, typically) before EMI repayment begins.
    Confirmed 2026-08-24 (a real user-reported EMI discrepancy traced to
    this): simple interest keeps accruing during the moratorium, and
    when not paid as it accrues, is CAPITALIZED — added to the
    principal — before the repayment-phase EMI is computed on the
    larger balance. Ignoring this understates EMI substantially (a
    ~43% understatement on the real case that surfaced it: Rs.750,000
    at 9.70% with a 54-month/4.5-year moratorium). moratorium_years=0
    (the default everywhere this is called) leaves the principal
    unchanged — this only changes behavior when a moratorium is
    explicitly given, so it never silently alters an existing figure."""
    if moratorium_years <= 0:
        return {"capitalized_principal": round(amount, 2), "moratorium_interest": 0.0}
    moratorium_interest = amount * (annual_rate / 100) * moratorium_years
    return {
        "capitalized_principal": round(amount + moratorium_interest, 2),
        "moratorium_interest": round(moratorium_interest, 2),
    }


def _amortize_with_moratorium(
    amount: float, annual_rate: float, repayment_years: float, moratorium_years: float = 0,
) -> dict:
    """EMI over the REPAYMENT period only (moratorium_years is not part
    of the EMI-paying tenure — it happens before repayment starts), but
    computed on the moratorium-capitalized principal when a moratorium
    is given, not the original disbursed amount."""
    cap = _capitalize_moratorium_interest(amount, annual_rate, moratorium_years)
    result = _amortize(cap["capitalized_principal"], annual_rate, repayment_years)
    return {
        "disbursed_principal": round(amount, 2),
        "moratorium_years": moratorium_years,
        "moratorium_interest": cap["moratorium_interest"],
        "capitalized_principal": cap["capitalized_principal"],
        **result,
    }


def loan_emi(
    bank_id: str, loan_type: str, amount: float, tenure_years: float,
    moratorium_years: float = 0,
) -> dict | None:
    """Real reducing-balance EMI using the bank's best published rate
    for this loan_type. `tenure_years` is the REPAYMENT period (after
    any moratorium, not including it — see _amortize_with_moratorium).
    `moratorium_years` defaults to 0 (no moratorium modeled) for
    backward compatibility; set it explicitly for loans (typically
    education loans) that have a real moratorium period with accruing,
    capitalized interest. Returns None if this bank has no loan_rates
    data for loan_type."""
    from compare_loan_rates import compare_loan

    rows = compare_loan(loan_type)
    row = next((r for r in rows if r["bank_id"] == bank_id), None)
    if row is None:
        return None

    result = _amortize_with_moratorium(amount, row["rate_min"], tenure_years, moratorium_years)
    return {
        "bank_id": bank_id,
        "loan_type": loan_type,
        "tier_label": row["tier_label"],
        "rate": row["rate_min"],
        "principal": amount,
        "tenure_years": tenure_years,
        "source_url": row["source_url"],
        **result,
    }


def loan_emi_at_rate(
    amount: float, annual_rate: float, tenure_years: float, moratorium_years: float = 0,
) -> dict:
    """EMI at a rate the caller supplies directly, not looked up from any
    bank's published data. `loan_emi()`'s best-published-tier rate is
    only ever a starting estimate — a real applicant's actually-approved
    rate depends on eligibility (CIBIL score, loan amount, institute
    category, etc., see `loan_emi()`'s own docstring) and commonly
    differs from it. Same formula as `loan_emi()` (`_amortize_with_
    moratorium`, reused directly — not a second copy), same shape as
    `switch.py`'s `_switch_loan()`, which has always taken the
    customer's own current rate this same way."""
    result = _amortize_with_moratorium(amount, annual_rate, tenure_years, moratorium_years)
    return {
        "rate": annual_rate,
        "principal": amount,
        "tenure_years": tenure_years,
        **result,
    }


if __name__ == "__main__":
    print("=" * 70)
    print("FD maturity — Rs. 5,00,000 for 1 year, general, real banks:")
    print("=" * 70)
    for bank_id in ("bob", "sbi", "hdfc"):
        r = fd_maturity(bank_id, "1 year", 500000, "general")
        if r:
            print(f"  {bank_id:<10} rate={r['rate']}%  maturity=Rs.{r['maturity_value']:,.2f}  "
                  f"interest=Rs.{r['interest_earned']:,.2f}  ({r['compounding']} compounding)")
        else:
            print(f"  {bank_id:<10} no data")

    print()
    print("=" * 70)
    print("Home loan EMI — Rs. 30,00,000 for 20 years, real banks:")
    print("=" * 70)
    for bank_id in ("bob", "pnb", "hdfc"):
        r = loan_emi(bank_id, "home_loan", 3000000, 20)
        if r:
            print(f"  {bank_id:<10} rate={r['rate']}%  EMI=Rs.{r['emi']:,.2f}  "
                  f"total_interest=Rs.{r['total_interest']:,.2f}  (tier: {r['tier_label']})")
        else:
            print(f"  {bank_id:<10} no data")
