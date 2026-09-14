"""Phase C — comparison queries (spec section C1).

SQL-backed, parameterized functions rather than free-text SQL generation —
"safer and easier to debug with only 4-5 banks," per the spec's own C1
note. These are meant to be called as tools (by an agent's function-calling
layer, or directly), not as a natural-language-to-SQL interface.

Run standalone for a demo against the real current data:

    python automation/compare_rates.py
"""

from __future__ import annotations

from db import get_connection
from tenure_utils import is_special_band, parse_tenure_range, tenure_covers

VALID_CUSTOMER_TYPES = ("general", "senior")


def _rate_column(customer_type: str) -> str:
    if customer_type not in VALID_CUSTOMER_TYPES:
        raise ValueError(f"customer_type must be one of {VALID_CUSTOMER_TYPES}, got {customer_type!r}")
    return "interest_rate_general" if customer_type == "general" else "interest_rate_senior"


# Human-readable labels for whatever values FDRateRecord.deposit_variant
# takes today or gains in future — keeps the display string out of the
# data layer and in one place callers (app.py) can reuse verbatim.
DEPOSIT_VARIANT_LABELS = {
    "retail": "Retail",
    "non_callable": "Non-Callable TD",
}


def _variant_label(variant: str) -> str:
    return DEPOSIT_VARIANT_LABELS.get(variant, variant.replace("_", " ").title())


def compare_for_tenure(target_days: int, customer_type: str = "general") -> list[dict]:
    """Return every bank's matching FD rate for a given tenure (in days),
    ranked highest rate first. One PRIMARY row per bank.

    A bank publishing more than one real product spanning target_days
    (confirmed 2026-08-18: SBI's plain retail table AND a separate
    Non-Callable Term Deposit table both cover "1 year", at different
    rates — see FDRateRecord.deposit_variant) is resolved as: the plain
    retail product is always the primary rate shown (what a bare "FD
    rate" question means by default, and what's actually comparable
    apples-to-apples across banks), never picked by "whichever number is
    higher." If that bank ALSO has a special-variant row (e.g.
    non_callable) at target_days with a HIGHER rate than the retail one,
    it's attached as `secondary` — surfaced, clearly labeled, not hidden
    and not silently merged into the headline number. A variant row is
    never shown as `secondary` if its rate isn't actually better than
    retail's — that would just be repeating the same information twice.

    This is the direct answer to spec C1's "compare rate by tenure across
    all banks" — e.g. compare_for_tenure(365) answers "who has the best
    1-year FD rate right now?"
    """
    col = _rate_column(customer_type)
    con = get_connection()
    rows = con.execute(
        f"SELECT bank_id, tenure_label, {col}, source_url, deposit_variant "
        f"FROM fd_rates WHERE {col} IS NOT NULL"
    ).fetchall()
    con.close()

    # bank_id -> {"retail": best-retail-row-or-None, "other": [rows]}
    by_bank: dict[str, dict] = {}
    for bank_id, tenure_label, rate, source_url, variant in rows:
        if not tenure_covers(tenure_label, target_days):
            continue
        entry = {
            "bank_id": bank_id, "tenure_label": tenure_label,
            "rate": rate, "source_url": source_url, "deposit_variant": variant,
        }
        bucket = by_bank.setdefault(bank_id, {"retail": None, "other": []})
        if variant == "retail":
            if bucket["retail"] is None or rate > bucket["retail"]["rate"]:
                bucket["retail"] = entry
        else:
            bucket["other"].append(entry)

    results = []
    for bank_id, bucket in by_bank.items():
        # A bank with no retail-tagged row matching this tenure (none seen
        # in real data so far, but not structurally impossible) still
        # needs a primary — fall back to its best non-retail row rather
        # than dropping the bank from the comparison entirely.
        primary = bucket["retail"]
        if primary is None and bucket["other"]:
            primary = max(bucket["other"], key=lambda r: r["rate"])

        result = {
            "bank_id": primary["bank_id"],
            "tenure_label": primary["tenure_label"],
            "rate": primary["rate"],
            "source_url": primary["source_url"],
            "variant_label": _variant_label(primary["deposit_variant"]),
            "secondary": None,
        }

        # Only surface a special-variant row as `secondary` if it beats
        # the primary rate — a lower/equal special rate isn't new
        # information worth showing.
        better_variants = [
            r for r in bucket["other"]
            if r is not primary and r["rate"] > primary["rate"]
        ]
        if better_variants:
            best_variant = max(better_variants, key=lambda r: r["rate"])
            result["secondary"] = {
                "label": _variant_label(best_variant["deposit_variant"]),
                "rate": best_variant["rate"],
                "tenure_label": best_variant["tenure_label"],
            }

        results.append(result)

    return sorted(results, key=lambda r: r["rate"], reverse=True)


def best_bank_for_tenure(target_days: int, customer_type: str = "general") -> dict | None:
    """The single highest-rate bank for a tenure — spec C1's "find
    lowest/highest". Returns None if no bank has a matching tenure row."""
    results = compare_for_tenure(target_days, customer_type)
    return results[0] if results else None


def special_tenure_bands(customer_type: str = "general") -> list[dict]:
    """Real, published FD tenure bands that don't match ANY of Compare's
    8 standard buckets — structurally invisible to compare_for_tenure()
    no matter which of the 8 tenures a user picks. Confirmed 2026-08-26:
    a real HDFC senior-rate move (7.00%->7.10%) on its "3 Years 1 day to
    < 4 Years 7 Months" band was completely absent from Compare AND
    missed by the digest, because both only ever check the 8 milestones.

    Scoped to `is_special_band()` — a genuinely substantial-duration gap
    (>=365 days) a customer might plausibly want to compare, not every
    bank's routine short-tenure laddering (7-14 days, 15-29 days, etc.),
    which fails to match a milestone too but for an uninteresting reason
    (milestones are sparse points; short bands are narrow) — surfacing
    all ~130 of those would bury the ~10 that are actually special.

    One row per qualifying (bank, band) — retail variant only, same
    scope as compare_for_tenure()'s primary rate. Sorted bank-wise
    (alphabetical bank_id, then duration within each bank) — per
    explicit request, easier to scan "what does bank X offer" than a
    flat tenure-only ordering that scatters one bank's own bands apart."""
    col = _rate_column(customer_type)
    con = get_connection()
    rows = con.execute(
        f"SELECT bank_id, tenure_label, {col}, source_url "
        f"FROM fd_rates WHERE {col} IS NOT NULL AND deposit_variant = 'retail'"
    ).fetchall()
    con.close()

    results = []
    for bank_id, tenure_label, rate, source_url in rows:
        if not is_special_band(tenure_label):
            continue
        lo, hi = parse_tenure_range(tenure_label)
        results.append({
            "bank_id": bank_id, "tenure_label": tenure_label,
            "rate": rate, "source_url": source_url,
            "days_min": lo, "days_max": hi,
        })

    return sorted(results, key=lambda r: (r["bank_id"], r["days_min"]))


def rank_all_common_tenures(customer_type: str = "general") -> dict[str, list[dict]]:
    """Comparison across a fixed set of representative tenures (1 month,
    3 months, 6 months, 1/2/3/5 years) rather than one specific day count —
    useful for a broad "how do the banks stack up overall" view."""
    representative_tenures = {
        "1 month": 30, "3 months": 90, "6 months": 180,
        "1 year": 365, "2 years": 730, "3 years": 1095, "5 years": 1825,
    }
    return {
        label: compare_for_tenure(days, customer_type)
        for label, days in representative_tenures.items()
    }


if __name__ == "__main__":
    print("=" * 70)
    print("Best 1-year FD rate, general public:")
    print("=" * 70)
    for row in compare_for_tenure(365, "general"):
        print(f"  {row['bank_id']:<10} {row['rate']}%   ({row['tenure_label']})")

    print()
    print("=" * 70)
    print("Best 1-year FD rate, senior citizen:")
    print("=" * 70)
    for row in compare_for_tenure(365, "senior"):
        print(f"  {row['bank_id']:<10} {row['rate']}%   ({row['tenure_label']})")

    print()
    print("=" * 70)
    print("Winner across common tenures (general public):")
    print("=" * 70)
    for tenure_name, results in rank_all_common_tenures("general").items():
        winner = results[0] if results else None
        if winner:
            print(f"  {tenure_name:<10} -> {winner['bank_id']} at {winner['rate']}%")
        else:
            print(f"  {tenure_name:<10} -> no matching data")
