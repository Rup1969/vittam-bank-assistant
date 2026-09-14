"""Home loan comparison — mirrors compare_rates.py's shape for the newer
loan_rates table (Stage 3). Loans rank lowest-rate-first (unlike FD, where
higher is better), since a lower starting rate is what a borrower wants.

Run standalone for a demo against the real current data:

    python automation/compare_loan_rates.py
"""

from __future__ import annotations

from db import get_connection


def compare_loan(loan_type: str = "home_loan") -> list[dict]:
    """Best (lowest rate_min) row per bank for the given loan_type, ranked
    cheapest first. One row per bank — a bank with several CIBIL/amount
    tiers is represented by its single best-case tier, same "one row per
    bank" shape compare_rates.py uses for FD tenures."""
    con = get_connection()
    rows = con.execute(
        "SELECT bank_id, rate_min, rate_max, benchmark_type, tier_label, "
        "source_url FROM loan_rates WHERE loan_type = ?",
        [loan_type],
    ).fetchall()
    con.close()

    best_per_bank: dict[str, dict] = {}
    for bank_id, rate_min, rate_max, benchmark_type, tier_label, source_url in rows:
        current = best_per_bank.get(bank_id)
        if current is None or rate_min < current["rate_min"]:
            best_per_bank[bank_id] = {
                "bank_id": bank_id,
                "rate_min": rate_min,
                "rate_max": rate_max,
                "benchmark_type": benchmark_type,
                "tier_label": tier_label,
                "source_url": source_url,
            }

    return sorted(best_per_bank.values(), key=lambda r: r["rate_min"])


def compare_home_loan() -> list[dict]:
    """Kept as a thin wrapper — app.py's Compare tab imports this name
    directly; compare_loan("home_loan") is the same query, generalized
    for education_loan and any future loan_type."""
    return compare_loan("home_loan")


if __name__ == "__main__":
    import sys

    loan_type = sys.argv[1] if len(sys.argv) > 1 else "home_loan"
    print("=" * 70)
    print(f"{loan_type} — best starting rate per bank:")
    print("=" * 70)
    for row in compare_loan(loan_type):
        rng = f"{row['rate_min']}% - {row['rate_max']}%" if row["rate_max"] != row["rate_min"] else f"{row['rate_min']}%"
        basis = row["benchmark_type"] or "—"
        print(f"  {row['bank_id']:<12} {rng:<18} basis={basis:<8} ({row['tier_label'] or 'n/a'})")
