"""Insights & Tools, Step 4 — "Should I Switch?" comparator.

Given a customer's OWN current product (bank, actual rate, amount,
tenure — which may well differ from that bank's currently published
rate, since a real customer often opened it earlier at a different
rate), ranks the top real alternatives across every tracked bank and
reports the estimated benefit of each.

Covers three product types, sharing one entry point (`should_i_switch`)
but two genuinely different comparison directions — this is the one
detail that must not get flipped:

- **FD/Deposit**: a HIGHER rate is the win. Ranked descending; benefit
  = alternative maturity value minus the customer's own current
  maturity value (positive = more money at maturity by switching).
- **Home Loan / Education Loan**: a LOWER rate is the win. Ranked
  ascending; benefit = the customer's own current total interest minus
  the alternative's total interest (positive = less paid in total by
  switching). Same inverted-direction care this project already applied
  to the digest's card coloring (FD "good" = green on a rate increase,
  loan "good" = green on a rate decrease) and to the Rate Trends chart
  before it was dropped — carried over here for the same reason: mixing
  the two up would recommend the WORSE deal as if it were the better one.

Reuses `compare_rates.compare_for_tenure()` for FD and
`compare_loan_rates.compare_loan()` for loans — the same already-
verified rate lookups Compare/Calculator/digest all trust — plus
`calculator.py`'s `FD_COMPOUNDING_PERIODS_PER_YEAR` and `_amortize()`
(imported, not duplicated) so every "current" and "alternative" figure
uses the identical formula for a fair comparison.

Explicit, stated scope limit (not hidden): processing fees, any
premature-closure penalty on the CURRENT product, and transfer/
paperwork costs are NOT included in any benefit estimate.

Run standalone for a demo against real current data across all three
product types:

    python automation/switch.py
"""

from __future__ import annotations

import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from calculator import FD_COMPOUNDING_PERIODS_PER_YEAR, _amortize_with_moratorium  # noqa: E402

LOAN_PRODUCTS = ("home_loan", "education_loan", "vehicle_loan")

EXCLUDED_COSTS_NOTE = (
    "Estimate only — excludes processing/transfer costs and any "
    "premature-closure penalty on your current account. Weigh those "
    "against the figure shown before actually switching."
)


def _fd_maturity_at_rate(amount: float, rate: float, years: float) -> float:
    n = FD_COMPOUNDING_PERIODS_PER_YEAR
    return amount * (1 + rate / 100 / n) ** (n * years)


def _switch_fd(
    current_bank_id: str, current_rate: float, amount: float,
    tenure_name: str, customer_type: str, top_n: int,
) -> dict | None:
    from compare_rates import compare_for_tenure
    from digest import TENURE_MILESTONES

    target_days = TENURE_MILESTONES.get(tenure_name)
    if target_days is None:
        return None
    rows = compare_for_tenure(target_days, customer_type)
    if not rows:
        return None

    years = target_days / 365.0
    current_value = _fd_maturity_at_rate(amount, current_rate, years)

    # HIGHER rate wins for a deposit — descending sort.
    alternatives = sorted(
        (r for r in rows if r["bank_id"] != current_bank_id),
        key=lambda r: r["rate"], reverse=True,
    )[:top_n]

    ranked = []
    for r in alternatives:
        alt_value = _fd_maturity_at_rate(amount, r["rate"], years)
        ranked.append({
            "bank_id": r["bank_id"],
            "rate": r["rate"],
            "label": r["tenure_label"],
            "alt_value": round(alt_value, 2),
            "benefit": round(alt_value - current_value, 2),
            "rate_diff": round(r["rate"] - current_rate, 3),
        })

    return {
        "product": "fd",
        "current_bank_id": current_bank_id,
        "current_rate": current_rate,
        "current_value": round(current_value, 2),
        "ranked": ranked,
        "banks_compared": len(rows),
        "years": round(years, 3),
        "customer_type": customer_type,
        "compounding": "quarterly",
    }


def _switch_loan(
    loan_type: str, current_bank_id: str, current_rate: float,
    amount: float, tenure_years: float, top_n: int, moratorium_years: float = 0,
) -> dict | None:
    from compare_loan_rates import compare_loan

    rows = compare_loan(loan_type)
    if not rows:
        return None

    # Same moratorium assumption applied to the customer's current loan
    # AND every alternative — loan_rates has no per-bank moratorium data,
    # so this is a stated simplification (a real switch could carry a
    # different moratorium at a different bank), not silently ignored.
    current = _amortize_with_moratorium(amount, current_rate, tenure_years, moratorium_years)

    # LOWER rate wins for a loan — ascending sort. This is the inverted
    # direction vs. FD above — deliberately a separate code path, not a
    # shared "best rate" helper, so the two can never accidentally swap.
    alternatives = sorted(
        (r for r in rows if r["bank_id"] != current_bank_id),
        key=lambda r: r["rate_min"],
    )[:top_n]

    ranked = []
    for r in alternatives:
        alt = _amortize_with_moratorium(amount, r["rate_min"], tenure_years, moratorium_years)
        ranked.append({
            "bank_id": r["bank_id"],
            "rate": r["rate_min"],
            "label": r["tier_label"] or "flat published rate, no tiering",
            "alt_emi": alt["emi"],
            "alt_total_interest": alt["total_interest"],
            "benefit": round(current["total_interest"] - alt["total_interest"], 2),
            "rate_diff": round(current_rate - r["rate_min"], 3),
        })

    return {
        "product": loan_type,
        "current_bank_id": current_bank_id,
        "current_rate": current_rate,
        "current_emi": current["emi"],
        "current_total_interest": current["total_interest"],
        "moratorium_years": moratorium_years,
        "ranked": ranked,
        "banks_compared": len(rows),
        "tenure_years": tenure_years,
    }


def should_i_switch(
    product: str,
    current_bank_id: str,
    current_rate: float,
    amount: float,
    tenure,
    customer_type: str = "general",
    top_n: int = 3,
    moratorium_years: float = 0,
) -> dict | None:
    """`product`: "fd", "home_loan", "education_loan", or "vehicle_loan". `tenure` is a
    tenure-name string (e.g. "1 year") for FD, or the REPAYMENT period
    in years for a loan (moratorium_years is separate — see
    calculator._amortize_with_moratorium — and defaults to 0, i.e. no
    moratorium modeled, for backward compatibility). Returns None if
    the tenure/product has no real data to compare against — a genuine
    "no data" case, never padded."""
    if product == "fd":
        return _switch_fd(current_bank_id, current_rate, amount, tenure, customer_type, top_n)
    if product in LOAN_PRODUCTS:
        return _switch_loan(product, current_bank_id, current_rate, amount, tenure, top_n, moratorium_years)
    raise ValueError(f"Unknown product: {product!r}")


if __name__ == "__main__":
    print("=" * 70)
    print("Should I Switch? — top 3 real alternatives per product")
    print("=" * 70)

    print("\n--- FD: BOB customer at an old 5.75% rate, Rs 5,00,000, 1 year ---")
    r = should_i_switch("fd", "bob", 5.75, 500000, "1 year", "general")
    print(f"  Current: bob at {r['current_rate']}% -> Rs.{r['current_value']:,.2f}")
    for i, alt in enumerate(r["ranked"], 1):
        print(f"  #{i} {alt['bank_id']:<14} {alt['rate']}%  -> Rs.{alt['alt_value']:,.2f}  "
              f"benefit=Rs.{alt['benefit']:+,.2f}  ({alt['rate_diff']:+.2f}pp)")

    print("\n--- Home Loan: HDFC customer at 8.5%, Rs 30,00,000, 20 years ---")
    r = should_i_switch("home_loan", "hdfc", 8.5, 3000000, 20)
    print(f"  Current: hdfc at {r['current_rate']}% -> EMI Rs.{r['current_emi']:,.2f}, "
          f"total interest Rs.{r['current_total_interest']:,.2f}")
    for i, alt in enumerate(r["ranked"], 1):
        print(f"  #{i} {alt['bank_id']:<14} {alt['rate']}%  -> EMI Rs.{alt['alt_emi']:,.2f}  "
              f"total interest Rs.{alt['alt_total_interest']:,.2f}  benefit=Rs.{alt['benefit']:+,.2f}")

    print("\n--- Education Loan: ICICI customer at 12.0%, Rs 5,00,000, 7 years ---")
    r = should_i_switch("education_loan", "icici", 12.0, 500000, 7)
    print(f"  Current: icici at {r['current_rate']}% -> EMI Rs.{r['current_emi']:,.2f}, "
          f"total interest Rs.{r['current_total_interest']:,.2f}")
    for i, alt in enumerate(r["ranked"], 1):
        print(f"  #{i} {alt['bank_id']:<14} {alt['rate']}%  -> EMI Rs.{alt['alt_emi']:,.2f}  "
              f"total interest Rs.{alt['alt_total_interest']:,.2f}  benefit=Rs.{alt['benefit']:+,.2f}")
