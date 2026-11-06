"""Shared tenure-parsing helper for Phase C's comparison and eligibility
tools. Both need to answer "does this bank's tenure_label cover N days?",
and `tenure_days_min`/`tenure_days_max` are only populated for 24 of the
current 36 fd_rates rows (the LLM-based extraction path fills them
inconsistently; the deterministic Canara parser doesn't fill them at all).
Parsing tenure_label text at query time is more reliable than trusting
those columns alone.
"""

from __future__ import annotations

import re

_DAY_UNITS = {
    "day": 1, "days": 1,
    "month": 30, "months": 30,
    "year": 365, "years": 365, "yr": 365, "yrs": 365, "y": 365,
}
# "year"/"years" must stay listed before bare "y" in this alternation so
# real "5 years" text matches the full word first, not just its trailing
# "y" (added after ICICI's "5Y (Tax Saver FD)" row needed it — see below).
_TOKEN_RE = re.compile(r"(\d+)\s*(day|days|year|years|yr|yrs|y|month|months)\b")

# "<=" is deliberately excluded (inclusive upper bound) — only a STRICT
# upper-bound marker belongs here. See parse_tenure_range()'s use of this.
_EXCLUSIVE_UPPER_RE = re.compile(r"less\s+than|\bunder\b|\bbelow\b|<(?!=)")


def parse_tenure_range(label: str) -> tuple[int, int]:
    """Best-effort (min_days, max_days) span for a free-text tenure label.
    Not exact — bank pages phrase tenures inconsistently — but good enough
    to test "does day N fall in this band" for comparison/eligibility
    purposes. Falls back to (0, 99999) — i.e. "matches everything" — only
    if nothing parseable is found, which should be rare and is worth
    noticing if it happens a lot.

    Adjacent number+unit tokens (e.g. "2 Years 11 Months 1 day") are
    merged into ONE compound duration rather than treated as independent
    range boundaries, as long as they're separated by nothing but
    whitespace AND each unit is smaller than the one before it (year >
    month > day) — that second condition is what stops a genuine range
    like "6 months to 9 months" from merging into one nonsense value while
    still correctly merging "6 months 1 day" into 181.

    This generalizes what used to be several hand-written regexes for
    specific compound shapes (years+months, then years+days, then
    years+months+days, then months+days) — real bank tenure labels kept
    turning up new N-part compound shapes across different banks (SBI,
    Canara, ICICI, HDFC), so merging by adjacency + descending unit size
    handles any depth/combination without hardcoding each one as it's
    found. Confirmed against every previously-fixed case (Canara's
    compound month bug, PNB's '>' exclusive-bound bug, Union Bank's
    'Yr'/'Yrs' bug, ICICI's 'years+days' and bare-'y' bugs, and HDFC's
    'years+months+days' and 'months+days' compounds) before replacing the
    old implementation — every one still parses to the same day-range as
    when each fix was verified individually.
    """
    label_l = label.lower()
    matches = list(_TOKEN_RE.finditer(label_l))

    day_values = []
    i = 0
    while i < len(matches):
        amount, unit = int(matches[i].group(1)), _DAY_UNITS[matches[i].group(2)]
        total = amount * unit
        j = i + 1
        while j < len(matches):
            gap = label_l[matches[j - 1].end():matches[j].start()]
            next_amount, next_unit = int(matches[j].group(1)), _DAY_UNITS[matches[j].group(2)]
            if gap.strip() != "" or next_unit >= unit:
                break
            total += next_amount * next_unit
            unit = next_unit
            j += 1
        day_values.append(total)
        i = j

    if not day_values:
        return (0, 99999)

    day_values.sort()
    lo, hi = day_values[0], day_values[-1]

    if "above" in label_l and lo == hi:
        # "Above X" with only one distinct value parsed means "X to next
        # band" is unknown — treat as open-ended upward from X.
        hi = 99999
    if len(day_values) == 1:
        # A single bare number, e.g. "1 Year" or "444 Days" — treat as an
        # exact-point tenure, not a range covering everything below it.
        hi = lo

    # A leading '>' (e.g. PNB's ">1 Year to 389 Days", ">2 to 3 Years")
    # means the lower bound is exclusive — the band starts the day AFTER
    # that value, not on it. Without this, a 365-day query wrongly matched
    # both PNB's "1 Year" row (365, correct) AND its ">1 Year to 389 Days"
    # row (366-389, band actually starts at 366) as if both included day
    # 365 — confirmed via compare_for_tenure(365, ...) surfacing the wrong
    # (higher, but not actually applicable) rate for that exact day.
    if label.strip().startswith(">") and lo < hi:
        lo += 1

    # An upper bound spelled "below X", "less than X", "under X", or "< X"
    # (e.g. IndusInd's "1 Year to below 1 Year 6 Month", Kotak's "2 years-
    # less than 3 years", Punjab & Sind's "1000 Days < 3 years") means the
    # upper bound is EXCLUSIVE — the band ends the day before X, not on it.
    # Confirmed 2026-11-06: without this, Kotak's "2 years- less than 3
    # years" (6.8%/7.55%) wrongly matched a 3-year (1095-day) query instead
    # of the band that actually starts AT 3 years, "3 years and above but
    # less than 4 years" (6.4%/6.9%) — the same overlapping-boundary bug as
    # PNB's '>' prefix above, just spelled the opposite direction. "<="
    # ("2Y <= 776 Days") is deliberately NOT matched here — that's an
    # inclusive upper bound, the opposite of what this block is for.
    if _EXCLUSIVE_UPPER_RE.search(label_l) and lo < hi:
        hi -= 1

    return (lo, hi)


def is_bare_number_label(label: str) -> bool:
    """True if a tenure_label is NOTHING but digits (e.g. Punjab & Sind's
    '500') — no day/month/year unit at all, so parse_tenure_range() can't
    tell what duration it actually means and falls back to its "matches
    everything" (0, 99999) span. That fallback is the right default for a
    genuinely free-text label parse_tenure_range has never seen the shape
    of, but it's the WRONG answer for a bare number — "500" then silently
    satisfied every tenure query (1-year, 3-year, 5-year, ...) and showed
    up in the 3-year ranking ahead of banks with a real matching rate.
    tenure_covers() special-cases this (see below) rather than guessing
    whether "500" means days, and compare_for_tenure() excludes it from
    standard-tenure ranking entirely — it still needs to be seen somewhere
    real, so it's surfaced as-is (raw label, no inferred range) in the
    Special / Additional Tenure Bands section instead."""
    return bool(re.fullmatch(r"\d+", label.strip()))


def tenure_covers(label: str, target_days: int) -> bool:
    if is_bare_number_label(label):
        return False
    lo, hi = parse_tenure_range(label)
    return lo <= target_days <= hi


# The 8 representative day-counts Compare's FD tab (app.py's
# _TENURE_OPTIONS) and the digest (digest.py's TENURE_MILESTONES) both
# check — canonical here so both stay in sync with the SAME definition
# of "special band" (a real published tenure that matches none of
# these), rather than three independent copies of this list drifting.
STANDARD_TENURE_MILESTONES = {
    "1 month": 30, "3 months": 90, "6 months": 180,
    "1 year": 365, "2 years": 730, "3 years": 1095,
    "5 years": 1825, "10 years": 3650,
}

# Confirmed 2026-08-26 (PROJECT_STATUS.md — HDFC's 3yr1day-4yr7mo band
# investigation): scanning every bank's real fd_rates rows against
# STANDARD_TENURE_MILESTONES turns up 134 non-matching bands, but the
# overwhelming majority are routine short-tenure LADDER rungs every
# bank publishes (7-14 days, 15-29 days, 30-45 days, ...) — narrow,
# granular steps that simply don't happen to straddle one of 8 sparse
# milestone points, not a genuine gap in coverage. The minimum-duration
# cutoff below keeps `is_special_band()` scoped to what's actually
# "special" — a substantial, meaningfully-long band a customer would
# plausibly want to compare (HDFC's real case, PNB's 1204-day row,
# Punjab & Sind's >5Y<66 Month row, etc.) — not every short-term rung.
_SPECIAL_BAND_MIN_DAYS = 365


def is_special_band(label: str) -> bool:
    """True if this tenure label covers NONE of STANDARD_TENURE_MILESTONES
    (Compare/the digest structurally can't see it) AND spans at least
    _SPECIAL_BAND_MIN_DAYS — i.e. a genuine, substantial-duration gap
    worth surfacing separately, not routine short-tenure laddering."""
    lo, hi = parse_tenure_range(label)
    if lo < _SPECIAL_BAND_MIN_DAYS:
        return False
    return not any(lo <= d <= hi for d in STANDARD_TENURE_MILESTONES.values())
