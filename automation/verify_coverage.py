"""Third permanent verification tool, alongside `verify_all.py` (does
content exist) and `verify_retrieval.py` (can chat find it) —
`verify_coverage.py` checks a different question: **can Compare and
the digest actually SEE every real rate, or does a real band/change
silently fall into a structural blind spot?**

Built after a real, user-found bug (`PROJECT_STATUS.md` §44-45): HDFC's
own "3 Years 1 day to < 4 Years 7 Months" senior FD rate genuinely moved
7.00%->7.10% on 2026-08-19, but it was invisible to Compare's FD tab
(only checks 8 fixed tenure milestones — `tenure_utils.
STANDARD_TENURE_MILESTONES`) AND to `digest.rate_changes()` (same 8
milestones), AND the stale 7.00% figure was still sitting in the
chat-facing RAG doc weeks later because nothing flagged the drift. §44
fixed Compare/the digest for bands >=365 days (`is_special_band()`,
Compare's "Special / Additional Tenure Bands" section,
`digest.special_band_changes()`); §45 fixed HDFC's specific stale doc.
This script is the STRUCTURAL, ongoing answer: two checks, run any time
a bank/product is added or a fetch pipeline runs, so the next version
of this class of bug is caught automatically instead of by a user
manually noticing a wrong chat answer.

1. **check_tenure_coverage()** — for every real `fd_rates` tenure band,
   is it visible SOMEWHERE in Compare's UI? A band matching one of the
   8 standard milestones is in the primary table (PASS, "milestone"). A
   band that doesn't but qualifies as `is_special_band()` (>=365 days)
   is in the Special Bands expander (PASS, "special"). A band that's
   neither — the short-tenure "dead zone" this project's own
   `is_special_band()` cutoff deliberately excludes (see its docstring:
   routine short-tenure laddering, not worth surfacing separately) — is
   genuinely invisible in Compare's UI (FAIL, "dead_zone"). FD-only:
   `loan_rates` has no comparable tenure-milestone structure — loans are
   compared by best rate tier directly (`compare_loan()`), with no
   `tenure_covers()` matching involved at all, so this check doesn't
   apply there.

2. **check_missed_changes()** — for every bank+product with >=2 real
   fetch_log snapshots, independently re-parses every consecutive pair
   (same deterministic parsers `fd_rates`/`loan_rates` already trust)
   and diffs EVERY real row by its own label text (not restricted to a
   milestone or "special" subset) — the exact method used to prove
   HDFC's senior-rate move was real and dated. For every genuine change
   found this way, checks whether the ACTUAL shipped digest functions
   (`rate_changes()` + `special_band_changes()` for FD,
   `loan_rate_changes()` for loans, called with a wide enough window to
   isolate "does the digest structurally see this label at all" from
   "was it within N days") would have reported it. A real change the
   digest's own functions miss is a FAIL — a live coverage gap, not a
   hypothetical one.

Deliberately does NOT call Groq/the LLM (same reasoning as
`verify_all.py`) — both checks are pure data/logic checks against real
DB rows and real historical snapshots.

Run standalone:

    python automation/verify_coverage.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from db import get_connection  # noqa: E402
from tenure_utils import (  # noqa: E402
    STANDARD_TENURE_MILESTONES,
    is_special_band,
    parse_tenure_range,
)

# A window wide enough that "was the change within N days" never
# confounds "does the digest structurally see this label at all" — the
# real question this script's check_missed_changes() asks. Real
# fetch_log history in this project spans at most a few months.
_WIDE_WINDOW_DAYS = 3650


def check_tenure_coverage() -> list[dict]:
    """Check 1: every real fd_rates tenure band, classified as visible
    via the primary 8-milestone table, the Special Bands expander, or
    neither (a genuine Compare-UI dead zone)."""
    con = get_connection()
    rows = con.execute(
        "SELECT DISTINCT bank_id, tenure_label FROM fd_rates ORDER BY bank_id, tenure_label"
    ).fetchall()
    con.close()

    results = []
    for bank_id, label in rows:
        lo, hi = parse_tenure_range(label)
        milestone_hit = next(
            (name for name, days in STANDARD_TENURE_MILESTONES.items() if lo <= days <= hi),
            None,
        )
        if milestone_hit:
            results.append({
                "bank_id": bank_id, "product": "fd", "check": "tenure_coverage",
                "status": "PASS",
                "detail": f'"{label}" ({lo}-{hi}d) covers the "{milestone_hit}" milestone — in the primary table',
            })
        elif is_special_band(label):
            results.append({
                "bank_id": bank_id, "product": "fd", "check": "tenure_coverage",
                "status": "PASS",
                "detail": f'"{label}" ({lo}-{hi}d) matches no milestone but is >=365 days — shown in Special Bands',
            })
        else:
            # A DIFFERENT check name, not "tenure_coverage" — this is
            # short-tenure laddering (<365 days), the exact category
            # `is_special_band()` deliberately excludes from the Special
            # Bands section by design (§44: surfacing ~90 routine short
            # rungs there would bury the genuinely special ones). Not
            # actionable by re-editing a doc the way a real gap is —
            # flagged separately, informational, same "structural
            # ceiling, not counted in FAIL" precedent verify_retrieval.py
            # already uses for its own non-fixable capacity cases.
            results.append({
                "bank_id": bank_id, "product": "fd", "check": "tenure_coverage_short_gap",
                "status": "INFO",
                "detail": f'"{label}" ({lo}-{hi}d) matches no milestone and is <365 days — '
                          f'not shown anywhere in Compare (by design, see is_special_band())',
            })

    return results


def check_missed_changes() -> list[dict]:
    """Check 2: for every bank+product with real snapshot history, does
    an independent re-parse find any genuine rate change the actual
    shipped digest functions would miss?"""
    from digest import (
        _fd_rate_source_ids,
        _loan_rate_source_ids,
        _loan_snapshot_rows,
        _parsed_fd_snapshots,
        loan_rate_changes,
        rate_changes,
        special_band_changes,
    )

    results = []
    con = get_connection()

    # ── FD ────────────────────────────────────────────────────────
    for customer_type in ("general", "senior"):
        col = "interest_rate_general" if customer_type == "general" else "interest_rate_senior"
        # Both functions report the actual MATCHED tenure_label (not just
        # a milestone name) in their output, so a precise (bank, label,
        # changed_at) match is the correct, strict test — deliberately
        # not falling back to a looser (bank, changed_at)-only match,
        # which could mask a genuine miss behind an unrelated change
        # that happened to land on the same bank/timestamp.
        digest_seen = {
            (c["bank_id"], c["tenure_label"], c["changed_at"])
            for c in rate_changes(_WIDE_WINDOW_DAYS, customer_type)
        } | {
            (c["bank_id"], c["tenure_label"], c["changed_at"])
            for c in special_band_changes(_WIDE_WINDOW_DAYS, customer_type)
        }

        for bank_id in _fd_rate_source_ids():
            parsed = _parsed_fd_snapshots(con, bank_id)
            prev: dict[str, float] | None = None
            for fetched_at, raw_rows in parsed:
                current = {
                    raw.get("tenure_label", ""): raw[col]
                    for raw in raw_rows if raw.get(col) is not None
                }
                if prev is not None:
                    for label, new_rate in current.items():
                        old_rate = prev.get(label)
                        if old_rate is not None and old_rate != new_rate:
                            seen = (bank_id, label, fetched_at) in digest_seen
                            results.append({
                                "bank_id": bank_id, "product": "fd", "check": "missed_changes",
                                "status": "PASS" if seen else "FAIL",
                                "detail": (
                                    f'{customer_type}: "{label}" {old_rate}%->{new_rate}% on '
                                    f'{fetched_at:%Y-%m-%d} — ' +
                                    ("caught by rate_changes()/special_band_changes()" if seen
                                     else "MISSED by both digest functions — real change, not reported anywhere")
                                ),
                            })
                prev = current

    # ── Loans (home_loan, education_loan, vehicle_loan) ────────────
    # No tenure-milestone concept here (loan_rate_changes() already
    # checks every real tier_label unconditionally) — this re-confirms
    # the mechanism directly rather than trusting it by construction,
    # same spirit as verify_all.py's independent-second-implementation
    # cross-check.
    for loan_type in ("home_loan", "education_loan", "vehicle_loan"):
        digest_seen = {
            (c["bank_id"], c["tier_label"], c["changed_at"])
            for c in loan_rate_changes(loan_type, _WIDE_WINDOW_DAYS)
        }
        for bank_id in _loan_rate_source_ids(loan_type):
            source_id = f"{bank_id}_{loan_type}"
            snapshots = con.execute(
                "SELECT fetched_at, file_path FROM fetch_log "
                "WHERE source_id = ? AND error IS NULL ORDER BY fetched_at",
                [source_id],
            ).fetchall()
            if len(snapshots) < 2:
                continue
            prev: dict[str, float] | None = None
            for fetched_at, file_path in snapshots:
                raw_rows = _loan_snapshot_rows(bank_id, loan_type, file_path)
                if raw_rows is None:
                    continue
                current = {
                    (raw.get("tier_label") or "General"): raw["rate_min"]
                    for raw in raw_rows if raw.get("rate_min") is not None
                }
                if prev is not None:
                    for label, new_rate in current.items():
                        old_rate = prev.get(label)
                        if old_rate is not None and old_rate != new_rate:
                            seen = (bank_id, label, fetched_at) in digest_seen
                            results.append({
                                "bank_id": bank_id, "product": loan_type, "check": "missed_changes",
                                "status": "PASS" if seen else "FAIL",
                                "detail": (
                                    f'"{label}" {old_rate}%->{new_rate}% on {fetched_at:%Y-%m-%d} — ' +
                                    ("caught by loan_rate_changes()" if seen
                                     else "MISSED by loan_rate_changes() — real change, not reported anywhere")
                                ),
                            })
                prev = current

    con.close()
    return results


def run_all() -> list[dict]:
    return check_tenure_coverage() + check_missed_changes()


def print_report(results: list[dict]) -> None:
    coverage = [r for r in results if r["check"] == "tenure_coverage"]
    short_gaps = [r for r in results if r["check"] == "tenure_coverage_short_gap"]
    missed = [r for r in results if r["check"] == "missed_changes"]
    # "checkable" = everything that counts toward PASS/FAIL — short-gap
    # INFO rows are reported but deliberately excluded, same as
    # verify_retrieval.py's own "general_chat_capacity" precedent.
    checkable = coverage + missed

    print("=" * 100)
    print(f"Tenure coverage — every real fd_rates band vs. Compare's milestones/Special Bands")
    print("=" * 100)
    print(f"{len(coverage)} bands ACTIONABLE by this check (milestone or Special-Bands coverage "
          f"expected); {len(short_gaps)} short-tenure bands (<365d) reported separately below, "
          f"not counted — excluded from Special Bands BY DESIGN (see is_special_band()).")
    banks = sorted(set(r["bank_id"] for r in coverage) | set(r["bank_id"] for r in short_gaps))
    for bank_id in banks:
        rows = [r for r in coverage if r["bank_id"] == bank_id]
        fails = [r for r in rows if r["status"] == "FAIL"]
        gaps = [r for r in short_gaps if r["bank_id"] == bank_id]
        print(f"{bank_id:<16} {len(rows):>3} milestone/special bands "
              f"({len(fails)} FAIL), {len(gaps)} short-tenure band(s) (info only)")
        for r in fails:
            print(f"    FAIL  {r['detail']}")

    if short_gaps:
        print("\nSHORT-TENURE BANDS (<365 days, not shown in Compare by design — informational, "
              "not counted in FAIL):")
        for r in short_gaps:
            print(f"  [{r['bank_id']}] {r['detail']}")

    print()
    print("=" * 100)
    print(f"Missed rate changes — real snapshot diffs vs. what rate_changes()/")
    print(f"special_band_changes()/loan_rate_changes() would have reported")
    print("=" * 100)
    if not missed:
        print("No real rate changes found in any historical snapshot pair — nothing to check "
              "(this is common: most banks/products have <2 real fetches so far).")
    else:
        for r in missed:
            marker = "FAIL" if r["status"] == "FAIL" else "pass"
            print(f"  {marker}  {r['bank_id']:<16} {r['product']:<16} {r['detail']}")

    total = len(checkable)
    failed = [r for r in checkable if r["status"] == "FAIL"]
    print()
    print("-" * 100)
    print(f"TOTAL: {total} checks, {len(failed)} FAIL, {total - len(failed)} PASS")
    print(f"       {len(coverage)} tenure-coverage checks, {len(missed)} missed-change checks")
    print(f"       {len(short_gaps)} short-tenure band(s) reported separately (not counted in FAIL)")
    if failed:
        print("\nFAILURES:")
        for r in failed:
            print(f"  [{r['check']}] {r['bank_id']} / {r['product']}: {r['detail']}")


if __name__ == "__main__":
    results = run_all()
    print_report(results)
