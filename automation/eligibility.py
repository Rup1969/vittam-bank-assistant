"""Phase C — eligibility/suitability reasoning (spec section C4).

Explicit if/then rules against the structured fd_rates table — deliberately
NOT an LLM guessing at a match. Every result carries a `reasons` list
explaining exactly why it qualified (or would have been excluded), which is
the auditability spec C4 asks for.

Honest limitation, not hidden: `min_deposit` (a MINIMUM deposit floor, e.g.
"must deposit at least Rs.1,000") is only populated for Indian Bank's rows
today — the extraction pipeline never reliably pulls a minimum-deposit
figure off any live-fetched bank's page. So the min_deposit rule below can
only EXCLUDE a product when that specific number is actually known; every
other result says so explicitly rather than silently treating "unknown" as
"no minimum." Separately, `deposit_ceiling` (a MAXIMUM this specific rate
applies up to — different concept, added Stage 2) IS populated for 8 banks
whose real page splits rates by deposit amount and whose retail table's own
upper bound this pipeline could confirm from the page text itself — see
extract_structured.py's `_DEPOSIT_CEILING_BY_BANK`.

Run standalone for a demo against the real current data:

    python automation/eligibility.py
"""

from __future__ import annotations

from compare_rates import _rate_column, VALID_CUSTOMER_TYPES
from db import get_connection
from tenure_utils import tenure_covers


def check_eligibility(
    deposit_amount: float,
    customer_type: str,
    tenure_days: int,
) -> dict:
    """Return {"qualifying": [...], "excluded": [...]} for the given
    deposit amount, customer type, and tenure.

    `qualifying` is ranked by rate (highest first), each with a `reasons`
    list explaining why it passed every rule.

    `excluded` accounts for every product whose TENURE matched but that
    was disqualified for a specific, stated reason (no published rate for
    the requested customer type, or amount below a known minimum) — this
    exists because an earlier version silently dropped these with `continue`
    and no trace anywhere, confirmed via a synthetic test row that vanished
    from the results with zero indication. Tenure-mismatches are NOT
    included in `excluded` (there are far too many of those across the
    whole table to be useful — every row for every other tenure would
    show up as "excluded" for an unrelated reason); only genuine
    tenure-matching candidates that failed a later rule are recorded.
    """
    if customer_type not in VALID_CUSTOMER_TYPES:
        raise ValueError(f"customer_type must be one of {VALID_CUSTOMER_TYPES}, got {customer_type!r}")

    col = _rate_column(customer_type)
    con = get_connection()
    rows = con.execute(
        f"SELECT bank_id, tenure_label, {col}, min_deposit, source_url, deposit_ceiling FROM fd_rates"
    ).fetchall()
    con.close()

    qualifying = []
    excluded = []
    for bank_id, tenure_label, rate, min_deposit, source_url, deposit_ceiling in rows:
        # Rule 1: tenure must be covered by this product's band. Not a
        # tenure-match at all -> not a candidate, skip without recording
        # (this is the one skip that stays silent, deliberately).
        if not tenure_covers(tenure_label, tenure_days):
            continue

        # Rule 2: the requested customer type must have a stated rate.
        if rate is None:
            excluded.append({
                "bank_id": bank_id,
                "tenure_label": tenure_label,
                "source_url": source_url,
                "reason": (
                    f"Tenure matches, but no {customer_type} rate is "
                    f"published for this product — verify with the bank."
                ),
            })
            continue

        # Rule 3: deposit amount vs this product's known upper ceiling.
        # Distinct from Rule 4's min_deposit check below — this isn't "we
        # don't know the minimum," it's "we DO know this exact rate only
        # applies below a specific amount" (several banks publish a
        # separate, lower bulk/wholesale rate at or above ~Rs. 3 crore that
        # this pipeline's parsers never captured — see
        # extract_structured.py's _DEPOSIT_CEILING_BY_BANK). Without this
        # check, a large-deposit query would show the retail rate as fully
        # qualifying with no warning that it doesn't actually apply at that
        # amount — confirmed as a real gap via a Rs. 4 crore test query
        # against ICICI/Axis/Kotak before this rule existed.
        if deposit_ceiling is not None and deposit_amount >= deposit_ceiling:
            excluded.append({
                "bank_id": bank_id,
                "tenure_label": tenure_label,
                "source_url": source_url,
                "reason": (
                    f"This rate only applies to deposits below "
                    f"Rs.{deposit_ceiling:,.0f} — Rs.{deposit_amount:,.0f} falls "
                    f"in this bank's bulk/wholesale tier, which is priced "
                    f"differently and not captured in current data. Contact "
                    f"the bank directly for the applicable bulk rate."
                ),
            })
            continue

        # Rule 4: deposit amount vs minimum, where actually known.
        reasons = [f"Tenure {tenure_days} days falls within '{tenure_label}'.",
                   f"{customer_type.capitalize()} rate published: {rate}%."]
        if min_deposit is not None:
            if deposit_amount < min_deposit:
                excluded.append({
                    "bank_id": bank_id,
                    "tenure_label": tenure_label,
                    "source_url": source_url,
                    "reason": (
                        f"Deposit amount Rs.{deposit_amount:,.0f} is below "
                        f"the stated minimum of Rs.{min_deposit:,.0f}."
                    ),
                })
                continue
            reasons.append(
                f"Deposit amount Rs.{deposit_amount:,.0f} meets the stated "
                f"minimum of Rs.{min_deposit:,.0f}."
            )
        else:
            reasons.append(
                "Minimum deposit requirement not available in current data — "
                "verify with the bank before relying on this qualification."
            )

        qualifying.append({
            "bank_id": bank_id,
            "tenure_label": tenure_label,
            "rate": rate,
            "source_url": source_url,
            "reasons": reasons,
        })

    return {
        "qualifying": sorted(qualifying, key=lambda r: r["rate"], reverse=True),
        "excluded": excluded,
    }


if __name__ == "__main__":
    scenarios = [
        (500000, "general", 365),
        (500000, "senior", 365),
        (100000, "general", 1825),
    ]
    for amount, ctype, days in scenarios:
        print("=" * 70)
        print(f"Deposit Rs.{amount:,}, {ctype} public, {days}-day tenure")
        print("=" * 70)
        result = check_eligibility(amount, ctype, days)
        if not result["qualifying"]:
            print("  No qualifying products found.")
        for r in result["qualifying"]:
            print(f"  {r['bank_id']:<10} {r['rate']}%  ({r['tenure_label']})")
            for reason in r["reasons"]:
                print(f"      - {reason}")
        if result["excluded"]:
            print("  Excluded (tenure matched, but disqualified):")
            for e in result["excluded"]:
                print(f"      {e['bank_id']:<10} ({e['tenure_label']}): {e['reason']}")
        print()
