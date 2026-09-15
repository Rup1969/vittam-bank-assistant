"""Insights & Tools, Step 1 — rate-change digest.

Detects REAL rate movements within a lookback window, not raw fetch_log
"changed" noise. `changed=True` is a whole-page-text-hash flag — it fires
on ANY content difference, including ones with zero rate impact (project
memory records a confirmed case: two of BOB's own "changed" snapshots
differed only by a transient "Webcast" banner string, no rate moved at
all). Trusting that flag directly would make the digest noisy and,
worse, occasionally wrong.

Instead this re-parses every historical snapshot with the SAME
deterministic parser fd_rates already trusts (`extract_rows_for_source`,
shared with trend.py) and compares the actual EXTRACTED VALUE at each
tenure milestone between chronologically consecutive real fetches. A
banner-only page edit changes the hash but not the parsed rate, so it
correctly produces zero reported change; a genuine rate move is caught
even on a fetch that (for whatever reason) didn't flip `changed=True`.

Covers all three product types this project tracks — `rate_changes()`
for fd_rates, `loan_rate_changes("home_loan")` and
`loan_rate_changes("education_loan")` for the other two — since a real
rate move could in principle happen in any of them, not just deposits.

Three distinct outputs, kept separate rather than conflated:
- `rate_changes()` — an FD tenure's matched rate genuinely differs
  between two real, chronologically-consecutive fetches of the same
  source.
- `loan_rate_changes(loan_type)` — same discipline, applied to
  loan_rates' different shape (tier_label-keyed rows compared by their
  own label text, not a shared tenure-milestone axis).
  Both require at least 2 real fetches to exist at all for that source
  (nothing to diff a single fetch against).
- `new_coverage()` — a source's FIRST-EVER successful fetch happened in
  the lookback window. This is deliberately NOT reported as a "change"
  (there's no real prior value, so "old rate -> new rate" would be
  fabricated) — most of loan_rates (home_loan, education_loan) is in
  this bucket right now, since those products were only built this week
  and have exactly one real fetch each.

Run standalone for a demo against real current data:

    python automation/digest.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from db import get_connection, resolve_scraped_path  # noqa: E402
from extract_structured import extract_rows_for_source  # noqa: E402
from fetch_and_track import clean_text, extract_pdf_text  # noqa: E402
from tenure_utils import is_special_band, tenure_covers  # noqa: E402
from tenure_utils import STANDARD_TENURE_MILESTONES as TENURE_MILESTONES  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "banks.json"

# PSB peer group for the Banker View's peer-comparison line — same
# bank_id set as app.py's PSB_BANKS, in automation's lowercase ids.
PSB_BANK_IDS = {
    "indian_bank", "sbi", "bob", "canara", "pnb", "boi", "union_bank",
    "iob", "central_bank", "punjab_sind", "bom", "uco",
}

# Private-bank peer group — the mirror set. Banker View compares PSB to
# PSB and Private to Private, never mixed (a PSB's real competitive set
# is other PSBs, not ICICI — matches project convention: app.py's own
# PRIVATE_BANKS list, in automation's lowercase ids).
PRIVATE_BANK_IDS = {"icici", "hdfc", "kotak", "axis", "indusind"}


def _peer_group(bank_id: str) -> set[str] | None:
    """A bank's own natural peer group — PSB or Private — or None if
    bank_id belongs to neither tracked group (there's no third peer
    group in this app to fall back to)."""
    if bank_id in PSB_BANK_IDS:
        return PSB_BANK_IDS
    if bank_id in PRIVATE_BANK_IDS:
        return PRIVATE_BANK_IDS
    return None


def _fd_rate_source_ids() -> list[str]:
    """Every source_id in fetch_log that's a bare fd_rates fetch (not a
    '_home_loan'/'_education_loan' composite) with at least one real
    (non-error) snapshot."""
    con = get_connection()
    ids = [
        r[0] for r in con.execute(
            "SELECT DISTINCT source_id FROM fetch_log "
            "WHERE error IS NULL AND source_id NOT LIKE '%\\_home_loan' ESCAPE '\\' "
            "AND source_id NOT LIKE '%\\_education_loan' ESCAPE '\\'"
        ).fetchall()
    ]
    con.close()
    return ids


def _matched_rate(raw_rows: list[dict], target_days: int, col: str) -> tuple[str, float] | None:
    """Best (highest) rate among rows whose tenure_label covers
    target_days — same matching rule compare_rates.py/trend.py already
    use, kept consistent rather than reinvented."""
    match = None
    for raw in raw_rows:
        label = raw.get("tenure_label", "")
        rate = raw.get(col)
        if rate is not None and tenure_covers(label, target_days):
            if match is None or rate > match[1]:
                match = (label, rate)
    return match


def _parsed_fd_snapshots(con, bank_id: str) -> list[tuple[datetime, list[dict]]]:
    """Every real (non-error) fetch_log snapshot for bank_id's FD source,
    re-parsed with the same deterministic parser fd_rates trusts,
    chronologically ordered — the shared snapshot-loading step both
    rate_changes() (milestone-keyed) and special_band_changes()
    (label-keyed) diff against, so there's one fetch+parse path, not
    two copies of it."""
    snapshots = con.execute(
        "SELECT fetched_at, file_path, text_hash FROM fetch_log "
        "WHERE source_id = ? AND error IS NULL ORDER BY fetched_at",
        [bank_id],
    ).fetchall()
    if len(snapshots) < 2:
        return []  # nothing to diff a single fetch against

    hash_cache: dict[str, list[dict] | None] = {}
    parsed: list[tuple[datetime, list[dict]]] = []
    for fetched_at, file_path, text_hash in snapshots:
        path = resolve_scraped_path(file_path)
        if path is None:
            continue
        if text_hash not in hash_cache:
            try:
                if path.suffix == ".pdf":
                    text = extract_pdf_text(path.read_bytes())
                else:
                    text = clean_text(path.read_text(encoding="utf-8"))
                hash_cache[text_hash] = extract_rows_for_source(bank_id, text, str(path), None)
            except Exception:
                hash_cache[text_hash] = None
        if hash_cache[text_hash] is not None:
            parsed.append((fetched_at, hash_cache[text_hash]))
    return parsed


def rate_changes(days: int = 7, customer_type: str = "general") -> list[dict]:
    """Real tenure-level rate changes detected within the lookback
    window, across every bank with fd_rates history. One entry per
    (bank, tenure) pair whose matched rate genuinely differs between two
    chronologically-consecutive real fetches, with the later fetch
    falling inside the window.

    Only checks STANDARD_TENURE_MILESTONES (Compare's own 8 buckets) —
    a real, substantial-duration band that falls between two milestones
    (e.g. HDFC's "3 Years 1 day to < 4 Years 7 Months") is structurally
    invisible here; see special_band_changes() for that case, confirmed
    2026-08-26 to have silently missed a real 7.00%->7.10% HDFC move
    for exactly this reason."""
    col = "interest_rate_general" if customer_type == "general" else "interest_rate_senior"
    cutoff = datetime.now() - timedelta(days=days)

    con = get_connection()
    bank_ids = _fd_rate_source_ids()
    changes = []

    for bank_id in bank_ids:
        parsed = _parsed_fd_snapshots(con, bank_id)
        for tenure_name, target_days in TENURE_MILESTONES.items():
            prev_rate: float | None = None
            for fetched_at, raw_rows in parsed:
                m = _matched_rate(raw_rows, target_days, col)
                if m is None:
                    continue
                label, rate = m
                if prev_rate is not None and rate != prev_rate and fetched_at >= cutoff:
                    changes.append({
                        "bank_id": bank_id,
                        "tenure": tenure_name,
                        "tenure_label": label,
                        "old_rate": prev_rate,
                        "new_rate": rate,
                        "changed_at": fetched_at,
                        "customer_type": customer_type,
                    })
                prev_rate = rate

    con.close()
    return sorted(changes, key=lambda c: c["changed_at"], reverse=True)


def special_band_changes(days: int = 7, customer_type: str = "general") -> list[dict]:
    """Real rate changes for SPECIAL bands — real published FD tenures
    that don't match any of STANDARD_TENURE_MILESTONES and so are
    invisible to rate_changes() above (see its docstring — this is the
    fix for the confirmed-missed HDFC 7.00%->7.10% "3 Years 1 day to
    < 4 Years 7 Months" senior-rate move). Matched by the band's own
    tenure_label TEXT between consecutive snapshots (same approach
    loan_rate_changes() already uses for tier_label — irregular bands
    have no shared milestone axis to key off, only their own stable
    label), not by day-count milestone."""
    col = "interest_rate_general" if customer_type == "general" else "interest_rate_senior"
    cutoff = datetime.now() - timedelta(days=days)

    con = get_connection()
    bank_ids = _fd_rate_source_ids()
    changes = []

    for bank_id in bank_ids:
        parsed = _parsed_fd_snapshots(con, bank_id)
        prev_by_label: dict[str, float] | None = None
        for fetched_at, raw_rows in parsed:
            current: dict[str, float] = {}
            for raw in raw_rows:
                label = raw.get("tenure_label", "")
                rate = raw.get(col)
                if rate is not None and is_special_band(label):
                    current[label] = rate
            if prev_by_label is not None and fetched_at >= cutoff:
                for label, new_rate in current.items():
                    old_rate = prev_by_label.get(label)
                    if old_rate is not None and old_rate != new_rate:
                        changes.append({
                            "bank_id": bank_id,
                            "tenure_label": label,
                            "old_rate": old_rate,
                            "new_rate": new_rate,
                            "changed_at": fetched_at,
                            "customer_type": customer_type,
                        })
            prev_by_label = current

    con.close()
    return sorted(changes, key=lambda c: c["changed_at"], reverse=True)


def new_coverage(days: int = 7) -> list[dict]:
    """Sources whose FIRST-EVER successful fetch happened within the
    lookback window — real new coverage, deliberately not framed as a
    rate 'change' since there's no genuine prior value to diff against."""
    cutoff = datetime.now() - timedelta(days=days)
    con = get_connection()
    rows = con.execute(
        "SELECT source_id, MIN(fetched_at) FROM fetch_log "
        "WHERE error IS NULL GROUP BY source_id HAVING MIN(fetched_at) >= ? "
        "ORDER BY MIN(fetched_at)",
        [cutoff],
    ).fetchall()
    con.close()

    results = []
    for source_id, first_fetched_at in rows:
        if source_id.endswith("_home_loan"):
            bank_id, product = source_id.removesuffix("_home_loan"), "home_loan"
        elif source_id.endswith("_education_loan"):
            bank_id, product = source_id.removesuffix("_education_loan"), "education_loan"
        else:
            bank_id, product = source_id, "fd_rates"
        results.append({
            "source_id": source_id, "bank_id": bank_id, "product": product,
            "first_fetched_at": first_fetched_at,
        })
    return results


def _loan_rate_source_ids(loan_type: str) -> list[str]:
    """Every bank_id with real (non-error) fetch_log history for the
    given loan_type ('home_loan' or 'education_loan')."""
    suffix = f"_{loan_type}"
    con = get_connection()
    ids = [
        r[0].removesuffix(suffix) for r in con.execute(
            "SELECT DISTINCT source_id FROM fetch_log WHERE error IS NULL AND source_id LIKE ?",
            [f"%{suffix}"],
        ).fetchall()
    ]
    con.close()
    return ids


def _loan_snapshot_rows(bank_id: str, loan_type: str, file_path: str) -> list[dict] | None:
    """Re-parse one historical loan_rates snapshot with the exact same
    parser extract_loan_rates.py already dispatches to for this
    bank_id + loan_type — mirrors extract_loan_rates.py's own run()/
    run_education() dispatch logic (PDF vs raw-HTML vs plain-text
    parsers) rather than a second copy of the parser registries."""
    import extract_loan_rates as elr

    if loan_type == "home_loan":
        pdf_parsers, raw_html_parsers, parsers = elr._PDF_PARSERS, {}, elr._PARSERS
    else:
        pdf_parsers = elr._EDUCATION_PDF_PARSERS
        raw_html_parsers = elr._EDUCATION_RAW_HTML_PARSERS
        parsers = elr._EDUCATION_PARSERS

    parser = pdf_parsers.get(bank_id) or raw_html_parsers.get(bank_id) or parsers.get(bank_id)
    if parser is None:
        return None
    path = resolve_scraped_path(file_path)
    if path is None:
        return None
    try:
        if bank_id in pdf_parsers:
            return parser(str(path))
        if bank_id in raw_html_parsers:
            return parser(path.read_text(encoding="utf-8"))
        return parser(clean_text(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def loan_rate_changes(loan_type: str, days: int = 7) -> list[dict]:
    """Real tier-level rate changes for home_loan or education_loan —
    same real-value-diffing discipline as rate_changes() above, applied
    to loan_rates' shape (tier_label-keyed rows, not tenure-keyed).
    Matches a snapshot's rows to the PREVIOUS snapshot's rows by their
    own tier_label text (stable per bank across real fetches, same
    assumption tenure_label stability rests on for fd_rates) rather
    than by position, so an added/removed tier doesn't get
    misattributed as an existing tier's rate changing."""
    cutoff = datetime.now() - timedelta(days=days)
    con = get_connection()
    bank_ids = _loan_rate_source_ids(loan_type)
    changes = []

    for bank_id in bank_ids:
        source_id = f"{bank_id}_{loan_type}"
        snapshots = con.execute(
            "SELECT fetched_at, file_path, text_hash FROM fetch_log "
            "WHERE source_id = ? AND error IS NULL ORDER BY fetched_at",
            [source_id],
        ).fetchall()
        if len(snapshots) < 2:
            continue

        hash_cache: dict[str, list[dict] | None] = {}
        parsed: list[tuple[datetime, list[dict]]] = []
        for fetched_at, file_path, text_hash in snapshots:
            if text_hash not in hash_cache:
                hash_cache[text_hash] = _loan_snapshot_rows(bank_id, loan_type, file_path)
            if hash_cache[text_hash] is not None:
                parsed.append((fetched_at, hash_cache[text_hash]))

        prev_tiers: dict[str, float] | None = None
        for fetched_at, raw_rows in parsed:
            tiers: dict[str, float] = {}
            for raw in raw_rows:
                label = raw.get("tier_label") or "General"
                rate = raw.get("rate_min")
                if rate is not None:
                    tiers[label] = rate
            if prev_tiers is not None and fetched_at >= cutoff:
                for label, new_rate in tiers.items():
                    old_rate = prev_tiers.get(label)
                    if old_rate is not None and old_rate != new_rate:
                        changes.append({
                            "bank_id": bank_id,
                            "loan_type": loan_type,
                            "tier_label": label,
                            "old_rate": old_rate,
                            "new_rate": new_rate,
                            "changed_at": fetched_at,
                        })
            prev_tiers = tiers

    con.close()
    return sorted(changes, key=lambda c: c["changed_at"], reverse=True)


def peer_rank(bank_id: str, tenure_name: str, customer_type: str = "general") -> dict | None:
    """This bank's rank among PSB peers for an FD tenure milestone right
    now — e.g. "2nd highest of 11". Powers the light inline positioning
    phrase folded directly into a rate-change card (replacing the
    removed separate Banker View toggle — see PROJECT_STATUS.md §10b).
    FD-only: higher rate = better rank for a deposit product, the
    opposite of a loan product, and loan tier_labels aren't a shared,
    comparable axis across banks the way FD tenure milestones are, so
    this isn't extended to loan_rate_changes() results.
    Returns None if bank_id isn't a tracked PSB or has no current data
    for this tenure."""
    if bank_id not in PSB_BANK_IDS:
        return None
    from compare_rates import compare_for_tenure

    target_days = TENURE_MILESTONES.get(tenure_name)
    if target_days is None:
        return None

    rows = compare_for_tenure(target_days, customer_type)
    psb_rows = sorted(
        (r for r in rows if r["bank_id"] in PSB_BANK_IDS),
        key=lambda r: r["rate"], reverse=True,
    )
    rank = next((i + 1 for i, r in enumerate(psb_rows) if r["bank_id"] == bank_id), None)
    if rank is None:
        return None
    return {"rank": rank, "total": len(psb_rows)}


def peer_comparison(bank_id: str, tenure_name: str, customer_type: str = "general") -> dict | None:
    """RE-WIRED 2026-08-24 as the data engine for Banker View's peer
    positioning (PROJECT_STATUS.md §18) — was unwired since 2026-08-21
    (§10b), when the separate Customer/Banker toggle it powered was
    removed from the customer-facing digest ("doesn't add value with
    sparse/no changes to show"). That removal was about the DIGEST
    specifically; a full peer snapshot doesn't need rate CHANGES to be
    useful — it's a snapshot of CURRENT standing, real and rich
    regardless of whether anything moved this week. Same "unwire, don't
    delete" precedent as automation/eligibility.py, now paying off.

    Auto-detects bank_id's own peer group — PSB banks compare against
    PSB peers, private banks against private peers, never mixed (a
    PSB's real competitive set is other PSBs, not a private bank).
    Returns None if bank_id is in neither tracked group, or if there's
    no current peer data for this tenure. Uses compare_for_tenure()
    (already real, already verified) rather than re-deriving matching
    logic — this function only adds the peer-group framing on top.

    Includes both the full sorted peer table (this bank's OWN rate
    included, so a caller can render "where do we stand" without a
    second query) and a real rank derived from that same list — one
    self-contained call for everything a peer-positioning display
    needs.
    """
    group = _peer_group(bank_id)
    if group is None:
        return None
    from compare_rates import compare_for_tenure

    target_days = TENURE_MILESTONES.get(tenure_name)
    if target_days is None:
        return None

    rows = compare_for_tenure(target_days, customer_type)
    group_rows = sorted(
        (r for r in rows if r["bank_id"] in group),
        key=lambda r: r["rate"], reverse=True,
    )
    ours = next((r for r in group_rows if r["bank_id"] == bank_id), None)
    peers = [r for r in group_rows if r["bank_id"] != bank_id]
    if not peers or ours is None:
        return None

    rank = next(i + 1 for i, r in enumerate(group_rows) if r["bank_id"] == bank_id)
    peer_avg = sum(r["rate"] for r in peers) / len(peers)
    return {
        "peer_group": "PSB" if group is PSB_BANK_IDS else "Private",
        "rank": rank,
        "total": len(group_rows),
        "table": group_rows,
        "peer_avg": round(peer_avg, 3),
        "our_rate": ours["rate"],
        "diff": round(ours["rate"] - peer_avg, 3),
        "peer_count": len(peers),
    }


if __name__ == "__main__":
    print("=" * 70)
    print("FD rate changes, last 7 days:")
    print("=" * 70)
    for c in rate_changes(7):
        print(f"  {c['bank_id']:<14} {c['tenure']:<10} {c['old_rate']}% -> {c['new_rate']}% "
              f"({c['changed_at']})")

    print()
    print("=" * 70)
    print("Special-band FD rate changes, last 30 days:")
    print("=" * 70)
    for c in special_band_changes(30):
        print(f"  {c['bank_id']:<14} {c['tenure_label']:<45} {c['old_rate']}% -> {c['new_rate']}% "
              f"({c['changed_at']})")

    for loan_type in ("home_loan", "education_loan"):
        print()
        print("=" * 70)
        print(f"{loan_type} rate changes, last 7 days:")
        print("=" * 70)
        for c in loan_rate_changes(loan_type, 7):
            print(f"  {c['bank_id']:<14} {c['tier_label']:<40} {c['old_rate']}% -> {c['new_rate']}% "
                  f"({c['changed_at']})")

    print()
    print("=" * 70)
    print("New coverage, last 7 days:")
    print("=" * 70)
    for n in new_coverage(7):
        print(f"  {n['bank_id']:<14} {n['product']:<16} first fetched {n['first_fetched_at']}")
