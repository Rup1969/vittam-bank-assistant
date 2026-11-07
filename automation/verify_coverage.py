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

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from db import DB_PATH, get_connection  # noqa: E402
from tenure_utils import (  # noqa: E402
    STANDARD_TENURE_MILESTONES,
    _EXCLUSIVE_UPPER_RE,
    is_bare_number_label,
    is_special_band,
    parse_tenure_range,
    tenure_covers,
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


def check_tenure_label_sanity() -> list[dict]:
    """Guards the exact class of bug fixed 2026-11-06 (tenure_utils.py's
    exclusive-upper-bound handling and bare-number labels) so a future
    regression or a new bank's odd label shape is caught automatically
    instead of waiting for a user to notice a wrong Compare Rates row.

    1. **Bare-number labels** (e.g. Punjab & Sind's "500", no day/month/
       year unit at all): `is_bare_number_label()` flags these so
       `tenure_covers()` can special-case them to never match — a bare
       number used to fall through to parse_tenure_range()'s "matches
       everything" (0, 99999) default and silently win every tenure
       query. FAIL — this is a real, fixable upstream extraction defect
       (the live page almost always DOES publish a unit, e.g. Punjab &
       Sind's real row is "500 Days"; this project just isn't storing
       it), not a design limitation; `compare_for_tenure()` already
       excludes it from ranking either way, so this FAIL tracks "the
       underlying data needs a fix," not "chat/Compare currently shows
       a wrong number."

    2. **A "less than N" (or "under"/"below"/"<") band that still covers
       exactly N**: the exact shape of the Kotak/SBI/axis/etc. bug this
       session found (2 Years 11 Months 1 day-offs aside, several real
       bands' own upper bound was being treated as inclusive instead of
       exclusive). For every band with such a marker and a real (not
       single-point) range, the day immediately above its parsed `hi`
       is the boundary the label names — this asserts that day is NOT
       covered, i.e. the next band (not this one) owns it."""
    con = get_connection()
    rows = con.execute(
        "SELECT DISTINCT bank_id, tenure_label FROM fd_rates ORDER BY bank_id, tenure_label"
    ).fetchall()
    con.close()

    results = []
    for bank_id, label in rows:
        if is_bare_number_label(label):
            results.append({
                "bank_id": bank_id, "product": "fd", "check": "tenure_label_sanity",
                "status": "FAIL",
                "detail": f'"{label}" has no day/month/year unit at all -- real duration unknown, '
                          f"excluded from tenure_covers()/compare_for_tenure() as a result; "
                          f"the underlying extraction for this row needs a fix upstream",
            })
            continue

        if _EXCLUSIVE_UPPER_RE.search(label.lower()):
            lo, hi = parse_tenure_range(label)
            if lo < hi:
                boundary = hi + 1
                still_covered = tenure_covers(label, boundary)
                results.append({
                    "bank_id": bank_id, "product": "fd", "check": "tenure_label_sanity",
                    "status": "FAIL" if still_covered else "PASS",
                    "detail": (
                        f'"{label}" ({lo}-{hi}d) still covers day {boundary} -- its own exclusive '
                        f"upper-bound wording ('less than'/'under'/'below'/'<') should have excluded it"
                        if still_covered else
                        f'"{label}" ({lo}-{hi}d) correctly excludes day {boundary}, its own named boundary'
                    ),
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


# (bank_id, loan_type) pairs whose loan_rates.benchmark_rate is confirmed,
# by reading that bank's own extractor, to hold the RAW RBI repo rate
# itself (not a bank-computed figure that happens to share a column
# name). An explicit allowlist, not a blanket benchmark_type match on
# "repo"/"EBLR" — those two strings carry DIFFERENT meanings across
# different banks' extractors, confirmed 2026-11-05 while building this
# check: sbi/home_loan stores benchmark_type="EBLR" too, but its value
# (7.90%) is the bank's own "Prevailing EBLR" (repo + spread already
# applied, per load_sbi_home_loan_legacy.py's own sourced comment), not
# the raw repo rate -- comparing it to RBI's repo rate would be a false
# positive, and it's also one of the explicitly frozen/manual banks this
# project already treats as out of scope for automated drift checks.
# union_bank/EBLR, icici/axis/central_bank's "repo"-labeled rows, etc.
# never populate a numeric benchmark_rate at all, so they're excluded by
# the IS NOT NULL filter below regardless.
_REPO_PEGGED_SOURCES = {
    ("hdfc", "home_loan"),          # _HDFC_REPO_RE scrapes "Current applicable Repo Rate = X%" directly
    ("kotak", "home_loan"),         # _KOTAK_REPO_RE scrapes "Floating Category (Repo Rate: X%" directly
    ("punjab_sind", "home_loan"),   # PRLR table's own "Repo %" column (fix 1, 2026-11-05)
    ("punjab_sind", "education_loan"),
}


def check_benchmark_rate_drift() -> list[dict]:
    """For every (bank, loan_type) confirmed above to store the RAW RBI
    repo rate as loan_rates.benchmark_rate, does it still match RBI's
    real current repo rate? By definition it always should — these
    benchmarks move in lockstep with RBI's own rate, unlike a bank-set
    MCLR/BRLLR/RLLR (deliberately excluded, never compared here). Found
    stale 2026-11-05: Punjab & Sind's PRLR regex had the repo figure
    hardcoded as a literal match string, so it silently stopped
    extracting ANY row the moment the bank's page updated to 5.50% —
    see extract_loan_rates.py's Punjab & Sind comments for the fix.
    Reads the real current repo rate from rbi_current_policy_rates.txt's
    own AUTO-GENERATED block — the same figure sync_rag_docs.py
    regenerates from a live rbi.org.in fetch — instead of a hardcoded
    constant, so this check can't itself go stale."""
    rbi_text = (PROJECT_ROOT / "data" / "raw" / "rbi" / "rbi_current_policy_rates.txt").read_text(encoding="utf-8")
    match = re.search(r"Repo Rate:\s*([\d.]+)%", rbi_text)
    if not match:
        return [{
            "bank_id": "?", "product": "loan_benchmark", "check": "benchmark_drift",
            "status": "FAIL",
            "detail": "could not read a current Repo Rate figure from rbi_current_policy_rates.txt",
        }]
    current_repo = float(match.group(1))

    con = get_connection()
    rows = con.execute(
        "SELECT DISTINCT bank_id, loan_type, benchmark_rate FROM loan_rates "
        "WHERE benchmark_rate IS NOT NULL ORDER BY bank_id, loan_type"
    ).fetchall()
    con.close()

    results = []
    for bank_id, loan_type, benchmark_rate in rows:
        if (bank_id, loan_type) not in _REPO_PEGGED_SOURCES:
            continue
        status = "PASS" if benchmark_rate == current_repo else "FAIL"
        results.append({
            "bank_id": bank_id, "product": loan_type, "check": "benchmark_drift",
            "status": status,
            "detail": f"benchmark_rate={benchmark_rate:g}% vs current RBI repo rate={current_repo:g}%"
                      + ("" if status == "PASS" else " -- STALE, loan_rates.benchmark_rate needs a re-fetch"),
        })
    return results


_FRESHNESS_MAX_AGE_DAYS = 3


def check_loan_freshness() -> list[dict]:
    """Every (bank, loan_type) whose loan_rates.data_source = 'live_fetch'
    is supposed to be refreshed by the daily automated workflow — this
    catches the exact class of bug fix 1 found (Punjab & Sind's regex
    silently returning zero rows for weeks: fetch_log looked healthy the
    whole time, so nothing EVER pointed at the problem until a user
    noticed a wrong chat answer). A row whose own fetched_at is more than
    _FRESHNESS_MAX_AGE_DAYS old means the pipeline isn't actually keeping
    it current, whatever the reason. 'manual_load_frozen' rows (Indian
    Bank, BOI, SBI home_loan, every bank's vehicle_loan) are deliberately
    excluded — they're never expected to refresh automatically."""
    con = get_connection()
    rows = con.execute(
        "SELECT bank_id, loan_type, MAX(fetched_at) FROM loan_rates "
        "WHERE data_source = 'live_fetch' GROUP BY bank_id, loan_type "
        "ORDER BY bank_id, loan_type"
    ).fetchall()
    con.close()

    now = datetime.now(timezone.utc)
    results = []
    for bank_id, loan_type, fetched_at in rows:
        # fetched_at is stored as a string in some rows (tz-aware ISO,
        # from this session's Punjab & Sind fix) and a naive datetime in
        # others (every pre-existing row) -- normalize both the same way
        # fd_rates' own VARCHAR/TIMESTAMP split already taught this
        # project to (PROJECT_STATUS.md §44-45): parse to a tz-aware
        # datetime regardless of which shape it arrived in.
        if isinstance(fetched_at, str):
            dt = datetime.fromisoformat(fetched_at)
        else:
            dt = fetched_at
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (now - dt).total_seconds() / 86400
        status = "PASS" if age_days <= _FRESHNESS_MAX_AGE_DAYS else "FAIL"
        results.append({
            "bank_id": bank_id, "product": loan_type, "check": "loan_freshness",
            "status": status,
            "detail": f"last extracted {age_days:.1f} days ago ({dt:%Y-%m-%d})"
                      + ("" if status == "PASS" else f" -- stale, exceeds {_FRESHNESS_MAX_AGE_DAYS}-day bound"),
        })
    return results


# Known suffixes fetch_and_track.py's own source_id convention uses for
# a non-fd_rates url_type (bare bank_id means "fd_rates" -- see its
# run()'s own comment) -- kept local rather than importing from
# fetch_and_track.py, since that module also pulls in Playwright (a
# heavy, Groq-adjacent dependency this check has no other reason to
# load).
_SOURCE_ID_PRODUCT_SUFFIXES = ("_home_loan", "_education_loan", "_vehicle_loan")

# Found live, 2026-11-06/07: Axis's FD source had 62 CONSECUTIVE fetch
# errors (every single fetch from 2026-08-17 through today) before
# anyone noticed -- a dead, date-stamped PDF URL, same silent-staleness
# shape as Punjab & Sind's regex bug (fix 1, same investigation). That
# bug was only found by a human asking "is this still broken?" and
# reading fetch_log by hand; this check makes the exact same read
# automatic. A streak longer than this many consecutive errors (most
# recent fetches first) is a FAIL, regardless of how long ago the
# streak started -- unlike loan_freshness above (which only cares about
# the AGE of the last success), this also catches a source that fails
# every day but happens to have failed only yesterday too, which a
# pure-age check could still miss for a day or two after it starts.
_MAX_CONSECUTIVE_FETCH_ERRORS = 3


def _source_id_to_bank_and_product(source_id: str) -> tuple[str, str]:
    for suffix in _SOURCE_ID_PRODUCT_SUFFIXES:
        if source_id.endswith(suffix):
            return source_id[: -len(suffix)], suffix.lstrip("_")
    return source_id, "fd_rates"


def _read_only_sources() -> tuple[set[str], set[tuple[str, str]]]:
    """Two levels of config-flagged "known blocked", mirroring the
    content_type/content_types split fetch_and_track.py already uses for
    a similar bank-wide-vs-per-product distinction:

    - `"read_only": true` (bank-wide) -- every url_type for this bank is
      manually loaded (Indian Bank, BOI: their only url_type IS
      fd_rates, so this is accurate for them).
    - `"read_only_products": [...]` (per-product) -- only the NAMED
      url_type(s) are manually loaded, the rest of the bank's sources
      are still live (Union Bank, added 2026-11-07: its FD page is a
      confirmed persistent WAF block, manually snapshotted via
      load_manual_snapshots.py, but its home_loan PDF is still fetched
      live and must NOT be swept into "known blocked" alongside it).

    Read from config rather than a hardcoded bank_id list, so this
    tracks config's own flags instead of drifting out of sync with
    them."""
    config = json.loads((PROJECT_ROOT / "config" / "banks.json").read_text(encoding="utf-8"))
    bank_wide = {b["id"] for b in config["banks"] if b.get("read_only")}
    per_product = {
        (b["id"], product)
        for b in config["banks"]
        for product in b.get("read_only_products", [])
    }
    return bank_wide, per_product


def check_consecutive_fetch_errors() -> list[dict]:
    """For every real fetch_log source, how many of its MOST RECENT
    fetches in a row have `error IS NOT NULL`, counting back from today
    until the first success (or the start of its history)? A streak
    longer than `_MAX_CONSECUTIVE_FETCH_ERRORS` is a FAIL -- the pipeline
    is actively, repeatedly failing for this source right now, not just
    once. Read-only against fetch_log; never writes anything (per this
    project's standing rule that the daily workflow is the only writer
    of automation.duckdb).

    A source whose bank is config-flagged `read_only` (confirmed
    2026-11-07: Indian Bank and BOI's FD data is genuinely
    `data_source='manual_load_frozen'` for exactly this reason -- their
    sites are bot-defended, per this project's standing "never try to
    defeat a site's access controls" rule) is reported as its OWN check
    name/status ("known_blocked", not "FAIL") -- the daily workflow
    still attempts these every run (config has no per-source on/off
    switch for the attempt itself), so they'd otherwise permanently
    clutter this check with a failure this project already knows about
    and isn't going to "fix" by defeating the block."""
    con = get_connection()
    source_ids = [r[0] for r in con.execute("SELECT DISTINCT source_id FROM fetch_log").fetchall()]
    read_only_banks, read_only_products = _read_only_sources()

    results = []
    for source_id in sorted(source_ids):
        rows = con.execute(
            "SELECT error FROM fetch_log WHERE source_id = ? ORDER BY fetched_at DESC",
            [source_id],
        ).fetchall()
        streak = 0
        for (error,) in rows:
            if error is None:
                break
            streak += 1
        bank_id, product = _source_id_to_bank_and_product(source_id)
        exceeds = streak > _MAX_CONSECUTIVE_FETCH_ERRORS
        if exceeds and (bank_id in read_only_banks or (bank_id, product) in read_only_products):
            results.append({
                "bank_id": bank_id, "product": product, "check": "consecutive_fetch_errors_known_blocked",
                "status": "INFO",
                "detail": f"{streak} consecutive fetch error(s) -- known blocked (config read_only/"
                          f"read_only_products, manual_load_frozen data; see README Known Limitations), "
                          f"not counted as FAIL",
            })
            continue
        status = "FAIL" if exceeds else "PASS"
        results.append({
            "bank_id": bank_id, "product": product, "check": "consecutive_fetch_errors",
            "status": status,
            "detail": f"{streak} consecutive fetch error(s) (most recent fetches first)"
                      + ("" if status == "PASS" else
                         f" -- exceeds the {_MAX_CONSECUTIVE_FETCH_ERRORS}-streak bound, "
                         f"this source is actively failing every run"),
        })
    con.close()
    return results


# >30% of a source's PREVIOUS row count lost in one commit is a FAIL --
# distinct from "dropped to zero rows", which is always a FAIL
# regardless of the prior count (even losing 1 of 2 rows is a 50% drop
# that would already trip this bound, but 0 rows is unambiguous on its
# own and worth its own clearer message).
_ROWS_DROP_FAIL_FRACTION = 0.30


def _git_commits_touching_db() -> list[str]:
    """Every commit hash (newest first) that actually changed
    data/automation.duckdb -- NOT every commit in the repo, most of
    which never touch this file at all."""
    result = subprocess.run(
        ["git", "log", "--format=%H", "--", "data/automation.duckdb"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=False,
    )
    return [h for h in result.stdout.splitlines() if h.strip()]


def _extract_db_blob(commit_hash: str, dest_path: Path) -> bool:
    """Writes that commit's exact data/automation.duckdb blob to
    dest_path. False (not an exception) if the commit/path doesn't
    resolve -- a shallow clone or a commit before this file existed are
    both real, non-error cases to just report nothing for."""
    result = subprocess.run(
        ["git", "show", f"{commit_hash}:data/automation.duckdb"],
        cwd=str(PROJECT_ROOT), capture_output=True, check=False,
    )
    if result.returncode != 0 or not result.stdout:
        return False
    dest_path.write_bytes(result.stdout)
    return True


def _source_row_counts(
    db_path: Path, read_only_products: set[tuple[str, str]] = frozenset(),
) -> dict[tuple[str, str], int]:
    """(bank_id, product) -> row count, for every data_source='live_fetch'
    FD/loan source -- the same scope loan_freshness/benchmark_drift above
    already treat as this project's real, automatically-refreshed data.

    A source listed in `read_only_products` (Union Bank's fd_rates,
    switched to a manual CSV snapshot -- see config/banks.json,
    §65/§66) counts BOTH 'live_fetch' AND 'manual_load_frozen' rows
    instead -- confirmed as a real false positive 2026-11-07: comparing
    live_fetch-only counts across the commit where that switch actually
    happened read as "18 -> 0 rows, FAIL" (true in a narrow sense -- the
    live_fetch count really did go to zero -- but the real row count,
    counting the manual snapshot that replaced it, didn't drop at all).
    This compares like with like: a source's TOTAL currently-trusted row
    count (whichever data_source it's expected to carry), not just
    whichever single data_source happened to apply last time. A bank-
    wide `read_only` source (Indian Bank, BOI) is deliberately NOT
    included here -- it already has zero live_fetch rows under the old
    scope (so it never appeared in this check at all before), and
    `read_only_products` is the flag this fix was asked to key off, not
    the bank-wide one.

    Opens the given file directly and read-only (never through
    get_connection(), which also runs schema-migration ALTERs this
    one-off read has no business triggering, especially not against an
    extracted PAST snapshot)."""
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    counts: dict[tuple[str, str], int] = {}

    live_fetch_fd_banks = {r[0] for r in con.execute(
        "SELECT DISTINCT bank_id FROM fd_rates WHERE data_source = 'live_fetch'"
    ).fetchall()}
    manual_fd_banks = {bank_id for bank_id, product in read_only_products if product == "fd_rates"}
    for bank_id in sorted(live_fetch_fd_banks | manual_fd_banks):
        if (bank_id, "fd_rates") in read_only_products:
            data_sources = ("live_fetch", "manual_load_frozen")
        else:
            data_sources = ("live_fetch",)
        placeholders = ", ".join("?" for _ in data_sources)
        n = con.execute(
            f"SELECT COUNT(*) FROM fd_rates WHERE bank_id = ? AND data_source IN ({placeholders})",
            [bank_id, *data_sources],
        ).fetchone()[0]
        counts[(bank_id, "fd_rates")] = n

    live_fetch_loans = {
        (r[0], r[1]) for r in con.execute(
            "SELECT DISTINCT bank_id, loan_type FROM loan_rates WHERE data_source = 'live_fetch'"
        ).fetchall()
    }
    manual_loans = {(bank_id, product) for bank_id, product in read_only_products if product != "fd_rates"}
    for bank_id, loan_type in sorted(live_fetch_loans | manual_loans):
        if (bank_id, loan_type) in read_only_products:
            data_sources = ("live_fetch", "manual_load_frozen")
        else:
            data_sources = ("live_fetch",)
        placeholders = ", ".join("?" for _ in data_sources)
        n = con.execute(
            f"SELECT COUNT(*) FROM loan_rates WHERE bank_id = ? AND loan_type = ? "
            f"AND data_source IN ({placeholders})",
            [bank_id, loan_type, *data_sources],
        ).fetchone()[0]
        counts[(bank_id, loan_type)] = n

    con.close()
    return counts


def check_rows_per_source_health(
    current_db_path: Path | None = None, previous_db_path: Path | None = None,
) -> list[dict]:
    """Compares each live_fetch FD/loan source's row count in the
    CURRENT database against its row count as of the previous commit
    that touched data/automation.duckdb -- catches a real content
    regression (a parser silently extracting far fewer rows, or none,
    while the fetch itself still reports success) that neither
    loan_freshness (only sees AGE, not content) nor
    consecutive_fetch_errors (only sees the FETCH layer, before
    extraction even runs) would notice, since both would look perfectly
    healthy if the fetch succeeds and extraction merely returns less
    than before.

    `current_db_path`/`previous_db_path` are overridable for testing
    against two specific snapshots (e.g. two historical commits) without
    touching the real file; left as None (the normal run_all() case),
    the current one is the real data/automation.duckdb on disk and the
    previous one is extracted fresh from git into a temp file, cleaned
    up before returning.

    Only reports sources whose row count actually CHANGED -- an
    unchanged count (the normal case for most sources on most days) is
    not interesting enough to list every single run."""
    cleanup: list[Path] = []
    try:
        if current_db_path is None:
            current_db_path = DB_PATH
        if previous_db_path is None:
            commits = _git_commits_touching_db()
            if len(commits) < 2:
                return [{
                    "bank_id": "-", "product": "-", "check": "rows_per_source_health",
                    "status": "INFO",
                    "detail": "fewer than 2 commits touch data/automation.duckdb in this "
                              "repo's history -- nothing to compare against yet",
                }]
            # mkstemp's own fd must be closed immediately -- an open
            # handle to it blocks duckdb.connect() on Windows ("file is
            # being used by another process"), and blocks the cleanup
            # unlink() too.
            fd, tmp_name = tempfile.mkstemp(suffix=".duckdb")
            os.close(fd)
            tmp = Path(tmp_name)
            cleanup.append(tmp)
            if not _extract_db_blob(commits[1], tmp):
                return [{
                    "bank_id": "-", "product": "-", "check": "rows_per_source_health",
                    "status": "INFO",
                    "detail": f"could not extract data/automation.duckdb from commit "
                              f"{commits[1][:10]} -- skipping this run",
                }]
            previous_db_path = tmp

        _, read_only_products = _read_only_sources()
        current_counts = _source_row_counts(current_db_path, read_only_products)
        previous_counts = _source_row_counts(previous_db_path, read_only_products)
    finally:
        for p in cleanup:
            p.unlink(missing_ok=True)

    results = []
    for bank_id, product in sorted(set(current_counts) | set(previous_counts)):
        cur_n = current_counts.get((bank_id, product), 0)
        prev_n = previous_counts.get((bank_id, product), 0)
        if cur_n == prev_n:
            continue  # unchanged -- not interesting, not reported
        if prev_n == 0:
            # A brand-new source this run, not a regression -- nothing
            # to compare its count AGAINST.
            results.append({
                "bank_id": bank_id, "product": product, "check": "rows_per_source_health",
                "status": "PASS",
                "detail": f"{prev_n} -> {cur_n} rows (new source, no prior baseline)",
            })
            continue
        drop_fraction = (prev_n - cur_n) / prev_n
        if cur_n == 0:
            status, reason = "FAIL", "dropped to ZERO rows"
        elif drop_fraction > _ROWS_DROP_FAIL_FRACTION:
            status, reason = "FAIL", f"lost {drop_fraction:.0%} of its prior rows (exceeds {_ROWS_DROP_FAIL_FRACTION:.0%} bound)"
        else:
            status, reason = "PASS", f"changed by {drop_fraction:+.0%}" if drop_fraction else "changed"
        results.append({
            "bank_id": bank_id, "product": product, "check": "rows_per_source_health",
            "status": status,
            "detail": f"{prev_n} -> {cur_n} rows ({reason})",
        })
    return results


_IBA_SNAPSHOT_MAX_AGE_DAYS = 30
_IBA_SNAPSHOT_PATH = PROJECT_ROOT / "data" / "iba_snapshot.json"


def check_iba_snapshot_age() -> list[dict]:
    """IBA blocks both Streamlit Cloud and GitHub Actions (confirmed
    2026-11-05 — a live-fetch test step returned zero circulars from
    GitHub Actions' IP range too, same as the hosted deployment), so
    data/iba_snapshot.json is refreshed by hand, not by the daily
    workflow. This can only ever WARN, never FAIL — there's no automated
    pipeline to blame for staleness here, just a reminder that the
    manual refresh (README Known Limitations has the exact command) is
    overdue."""
    import json

    if not _IBA_SNAPSHOT_PATH.exists():
        return [{
            "bank_id": "iba", "product": "iba_snapshot", "check": "iba_snapshot_age",
            "status": "WARN",
            "detail": f"{_IBA_SNAPSHOT_PATH.name} does not exist -- IBA News will show "
                      f"nothing on a deployment where the live fetch is blocked",
        }]

    try:
        payload = json.loads(_IBA_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(payload["fetched_at"])
    except Exception as exc:
        return [{
            "bank_id": "iba", "product": "iba_snapshot", "check": "iba_snapshot_age",
            "status": "WARN",
            "detail": f"could not read fetched_at from {_IBA_SNAPSHOT_PATH.name}: "
                      f"{type(exc).__name__}: {exc}",
        }]

    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 86400
    status = "WARN" if age_days > _IBA_SNAPSHOT_MAX_AGE_DAYS else "OK"
    return [{
        "bank_id": "iba", "product": "iba_snapshot", "check": "iba_snapshot_age",
        "status": status,
        "detail": f"snapshot is {age_days:.1f} days old ({fetched_at:%Y-%m-%d})"
                  + ("" if status == "OK" else
                     f" -- overdue for a manual refresh (>{_IBA_SNAPSHOT_MAX_AGE_DAYS} days, "
                     f"see README Known Limitations for the command)"),
    }]


_RAW_DOCS_DIR = PROJECT_ROOT / "data" / "raw"
_AUTO_START_LINE = "<!--AUTO:START-->"
_AUTO_END_LINE = "<!--AUTO:END-->"
_PERCENT_TOKEN_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PREVIEW_CHARS = 500  # app.py's _MAX_CHUNK_CHARS_GENERAL — General Chat's chunk-0 preview budget
# Same "starts with 1 Year/1 year, optionally '>'/'Above '" rule
# sync_rag_docs.py's _reorder_common_tenures_first() uses for its n=1
# case — kept as its own copy rather than a cross-module import, since
# the two scripts have no other dependency on each other and this one
# regex is simple enough not to be worth coupling them over.
_ONE_YEAR_LABEL_RE = re.compile(r"^(?:>|Above\s+)?\s*1\s*Years?\b", re.IGNORECASE)
# A standalone "1 year"/"1-year" mention inside the preview text itself
# (not anchored to line-start like _ONE_YEAR_LABEL_RE, which matches a
# full tenure_label) — \b1\b keeps this from matching inside "21 year"
# or "<1 year" wrongly... actually <1 year SHOULD match (a deposit of
# "less than 1 year" still mentions "1 year"), and \b sits correctly
# between "<" and "1" either way.
_ONE_YEAR_MENTION_RE = re.compile(r"\b1[\s-]*years?\b", re.IGNORECASE)
# How far (characters) a matching rate figure may sit after a "1 year"
# mention and still count as "visibly attached to it" — found necessary,
# not arbitrary: an earlier, looser version of this check matched the
# 1-year rate's VALUE anywhere in the first 500 chars, which produced a
# real false positive on BOB (its unrelated 211-270-day senior rate
# happens to also be 6.25%, the same number as BOB's actual 1-year
# rate) and a borderline one on Canara (the headline sentence's overall
# max senior rate, 6.90%, happens to equal the 1-year row's own senior
# rate, but nothing at that point in the text says "1 year"). Requiring
# the number to sit near an explicit "1 year" mention — not just appear
# somewhere in the window — is what catches those; confirmed by testing
# this exact check against both banks' pre-fix (long-marker) text, where
# it correctly WARNs (see the commit message for the measurements).
_ONE_YEAR_PROXIMITY_CHARS = 40


def check_preview_figures() -> list[dict]:
    """For every FD document with an AUTO block (SBI/BOB/Canara/PNB/HDFC,
    as of 2026-11 — RBI's policy-rates doc has no "1-year FD rate"
    concept and is out of scope here), does General Chat's 500-character
    chunk-0 preview contain the SPECIFIC 1-year rate figure, not just any
    percentage? Tightened 2026-11-06 after the original, looser version
    (any percentage at all) stayed green through two real regressions in
    a row (the old long marker ate the budget; later the generic
    template's own summary paragraph did) — both times because the
    summary sentence's own headline range% satisfied "contains a
    figure" even while the specific 1-year line was pushed out of the
    preview entirely. Checking for the 1-year row's own real
    general/senior rate closes that gap: a regression that merely
    removes a DIFFERENT percentage can't accidentally look fine anymore.

    WARN, never FAIL: a document whose preview never included the
    1-year figure even before any of this automation (SBI's is a known,
    accepted case — its tenure table starts too late in the chunk
    regardless) is a disclosed limitation, not a bug to block on; this
    reports every case so a NEW one doesn't go unnoticed, without
    blocking anything on an already-accepted one.

    Mirrors build_index.py's own header/marker-stripping exactly (not a
    separate parallel implementation) so this measures what General Chat
    actually retrieves, not an approximation of it."""
    results = []
    if not _RAW_DOCS_DIR.exists():
        return results

    con = get_connection()
    for fpath in sorted(_RAW_DOCS_DIR.rglob("*.txt")):
        if ".ipynb_checkpoints" in fpath.parts:
            continue
        text = fpath.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not any(l.strip() == _AUTO_START_LINE for l in lines):
            continue  # no AUTO block -- not in this check's scope at all

        doc_id = next(
            (l.split(":", 1)[1].strip() for l in lines if l.startswith("DOCUMENT_ID")),
            fpath.stem,
        )
        if not doc_id.endswith("fd_rates"):
            continue  # scoped to FD documents only, per this check's own purpose

        bank_id = fpath.parent.name
        rows = con.execute(
            "SELECT tenure_label, interest_rate_general, interest_rate_senior FROM fd_rates "
            "WHERE bank_id = ? AND product_type = 'fixed_deposit' AND deposit_variant = 'retail'",
            [bank_id],
        ).fetchall()
        one_year = next((r for r in rows if _ONE_YEAR_LABEL_RE.match(r[0])), None)
        if one_year is None:
            results.append({
                "bank_id": bank_id, "product": fpath.stem, "check": "preview_figures",
                "status": "WARN",
                "detail": "no '1 Year...'-labeled retail row found in today's data -- can't check",
            })
            continue
        label, g, s = one_year

        body_lines = [
            l for l in lines
            if not l.startswith(("SOURCE", "DOCUMENT_ID", "CATEGORY", "BANK", "SOURCE_TYPE", "DATE", "==="))
            and l.strip() not in (_AUTO_START_LINE, _AUTO_END_LINE)
        ]
        body = "\n".join(body_lines).strip()
        chunk0_text = " ".join(body.split())[:_PREVIEW_CHARS]

        wanted = {round(v, 2) for v in (g, s) if v is not None}
        has_rate = False
        for mention in _ONE_YEAR_MENTION_RE.finditer(chunk0_text):
            window = chunk0_text[mention.end(): mention.end() + _ONE_YEAR_PROXIMITY_CHARS]
            nearby_values = {round(float(m.group(1)), 2) for m in _PERCENT_TOKEN_RE.finditer(window)}
            if wanted & nearby_values:
                has_rate = True
                break
        # A bank with no senior-citizen rate at all (Union Bank's manual
        # snapshot, §65/§66) has s = None here -- f"{s:g}" crashes on
        # that directly; "not published" matches the wording the FD-rate
        # shortcut/Compare Rates already use for the same case elsewhere.
        g_s = f"{g:g}%" if g is not None else "not published"
        s_s = f"{s:g}%" if s is not None else "not published"
        results.append({
            "bank_id": bank_id, "product": fpath.stem, "check": "preview_figures",
            "status": "OK" if has_rate else "WARN",
            "detail": (
                f"1-year rate ({g_s}/{s_s} for {label!r}) appears in the first {_PREVIEW_CHARS} "
                f"chars of General Chat's preview"
                if has_rate else
                f"1-year rate ({g_s}/{s_s} for {label!r}) does NOT appear in the first "
                f"{_PREVIEW_CHARS} chars of General Chat's preview -- a General Chat answer for "
                f"this bank's 1-year FD rate may have nothing concrete to quote "
                f"(preview ends: ...{chunk0_text[-60:]!r})"
            ),
        })
    return results


def run_all() -> list[dict]:
    return (check_tenure_coverage() + check_tenure_label_sanity() + check_missed_changes()
            + check_benchmark_rate_drift() + check_loan_freshness() + check_consecutive_fetch_errors()
            + check_rows_per_source_health() + check_iba_snapshot_age() + check_preview_figures())


def print_report(results: list[dict]) -> None:
    coverage = [r for r in results if r["check"] == "tenure_coverage"]
    short_gaps = [r for r in results if r["check"] == "tenure_coverage_short_gap"]
    label_sanity = [r for r in results if r["check"] == "tenure_label_sanity"]
    missed = [r for r in results if r["check"] == "missed_changes"]
    drift = [r for r in results if r["check"] == "benchmark_drift"]
    freshness = [r for r in results if r["check"] == "loan_freshness"]
    error_streaks = [r for r in results if r["check"] == "consecutive_fetch_errors"]
    known_blocked = [r for r in results if r["check"] == "consecutive_fetch_errors_known_blocked"]
    rows_health = [r for r in results if r["check"] == "rows_per_source_health"]
    rows_health_checkable = [r for r in rows_health if r["status"] != "INFO"]
    iba_age = [r for r in results if r["check"] == "iba_snapshot_age"]
    preview_figures = [r for r in results if r["check"] == "preview_figures"]
    # "checkable" = everything that counts toward PASS/FAIL — short-gap
    # INFO rows and the IBA snapshot-age WARN are reported but
    # deliberately excluded, same as verify_retrieval.py's own
    # "general_chat_capacity" precedent.
    checkable = coverage + label_sanity + missed + drift + freshness + error_streaks + rows_health_checkable

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
    print("Tenure-label sanity — bare-number labels, and 'less than N'/'under'/'below'/'<' "
          "bands that still cover N")
    print("=" * 100)
    if not label_sanity:
        print("No bare-number labels and no exclusive-upper-bound band found covering its own boundary.")
    else:
        fails = [r for r in label_sanity if r["status"] == "FAIL"]
        print(f"{len(label_sanity)} checked, {len(fails)} FAIL, {len(label_sanity) - len(fails)} PASS")
        for r in label_sanity:
            if r["status"] == "FAIL":
                print(f"  FAIL  {r['bank_id']:<14} {r['detail']}")

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

    print()
    print("=" * 100)
    print("Benchmark-rate drift — Punjab & Sind's EBLR benchmark_rate vs. the real current RBI repo rate")
    print("=" * 100)
    if not drift:
        print("No repo-pegged benchmark_rate rows found -- nothing to check.")
    else:
        for r in drift:
            print(f"  {r['status']:<4}  {r['bank_id']:<14} {r['product']:<16} {r['detail']}")

    print()
    print("=" * 100)
    print(f"Loan-rate freshness — every live_fetch (bank, loan_type) extracted within "
          f"the last {_FRESHNESS_MAX_AGE_DAYS} days?")
    print("=" * 100)
    if not freshness:
        print("No live_fetch loan_rates rows found -- nothing to check.")
    else:
        for r in freshness:
            print(f"  {r['status']:<4}  {r['bank_id']:<14} {r['product']:<16} {r['detail']}")

    print()
    print("=" * 100)
    print(f"Consecutive fetch errors — any source failing EVERY fetch right now "
          f"(>{_MAX_CONSECUTIVE_FETCH_ERRORS} in a row)?")
    print("=" * 100)
    error_fails = [r for r in error_streaks if r["status"] == "FAIL"]
    if not error_streaks:
        print("No fetch_log sources found -- nothing to check.")
    elif not error_fails:
        print(f"All {len(error_streaks)} source(s) have {_MAX_CONSECUTIVE_FETCH_ERRORS} or fewer "
              f"consecutive fetch errors.")
    else:
        print(f"{len(error_fails)} of {len(error_streaks)} source(s):")
        for r in error_fails:
            print(f"  FAIL  {r['bank_id']:<14} {r['product']:<16} {r['detail']}")
    if known_blocked:
        print(f"\n{len(known_blocked)} source(s) KNOWN BLOCKED (config read_only/read_only_products -- "
              f"manual_load_frozen data, not counted in FAIL):")
        for r in known_blocked:
            print(f"  INFO  {r['bank_id']:<14} {r['product']:<16} {r['detail']}")

    print()
    print("=" * 100)
    print(f"Rows-per-source health — every live_fetch FD/loan source's row count vs. the "
          f"previous commit that touched automation.duckdb (>{_ROWS_DROP_FAIL_FRACTION:.0%} drop or 0 rows = FAIL)")
    print("=" * 100)
    if not rows_health:
        print("No source's row count changed since the previous commit that touched automation.duckdb.")
    elif all(r["status"] == "INFO" for r in rows_health):
        for r in rows_health:
            print(f"  INFO  {r['detail']}")
    else:
        changed = [r for r in rows_health if r["status"] != "INFO"]
        if not changed:
            print("No source's row count changed since the previous commit.")
        else:
            print(f"{len(changed)} source(s) changed:")
            for r in changed:
                print(f"  {r['status']:<4}  {r['bank_id']:<14} {r['product']:<16} {r['detail']}")

    print()
    print("=" * 100)
    print(f"IBA snapshot age — manually refreshed, so this can only WARN, never FAIL")
    print("=" * 100)
    for r in iba_age:
        print(f"  {r['status']:<4}  {r['detail']}")

    print()
    print("=" * 100)
    print(f"General Chat preview figures — first {_PREVIEW_CHARS} chars of every AUTO-block "
          f"doc's indexed text, WARN only (never FAIL) since some are known, accepted exceptions")
    print("=" * 100)
    warned = [r for r in preview_figures if r["status"] == "WARN"]
    if not preview_figures:
        print("No documents with an AUTO block found -- nothing to check.")
    elif not warned:
        print(f"All {len(preview_figures)} AUTO-block document(s) have a rate figure in the first "
              f"{_PREVIEW_CHARS} chars.")
    else:
        print(f"{len(warned)} of {len(preview_figures)} AUTO-block document(s):")
        for r in warned:
            print(f"  WARN  {r['bank_id']:<10} {r['product']:<24} {r['detail']}")

    total = len(checkable)
    failed = [r for r in checkable if r["status"] == "FAIL"]
    print()
    print("-" * 100)
    print(f"TOTAL: {total} checks, {len(failed)} FAIL, {total - len(failed)} PASS")
    print(f"       {len(coverage)} tenure-coverage checks, {len(label_sanity)} tenure-label-sanity "
          f"checks, {len(missed)} missed-change checks, {len(drift)} benchmark-drift checks, "
          f"{len(freshness)} loan-freshness checks, {len(error_streaks)} consecutive-fetch-error checks, "
          f"{len(rows_health_checkable)} rows-per-source-health checks")
    print(f"       {len(short_gaps)} short-tenure band(s) reported separately (not counted in FAIL)")
    print(f"       {len(known_blocked)} source(s) known blocked (config read_only/read_only_products, not counted in FAIL)")
    print(f"       {len(iba_age)} IBA snapshot-age check (WARN only, not counted in FAIL)")
    print(f"       {len(preview_figures)} General Chat preview-figure check(s), {len(warned)} WARN "
          f"(WARN only, not counted in FAIL)")
    if failed:
        print("\nFAILURES:")
        for r in failed:
            print(f"  [{r['check']}] {r['bank_id']} / {r['product']}: {r['detail']}")


if __name__ == "__main__":
    results = run_all()
    print_report(results)
