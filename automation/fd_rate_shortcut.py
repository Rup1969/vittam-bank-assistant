"""Deterministic FD-rate chat shortcut -- no Groq call, anywhere in this
file, not even for parsing the question. That's one step stricter than
the existing calculator shortcut (app.py's _CALC_INTENT_RE handler),
which still uses Groq for language understanding before computing the
real number deterministically. Here even the parsing is plain regex,
because the whole point is a rate figure chat can state with ZERO LLM
involvement between the real data and the user.

Built 2026-11-06, directly after the FD-rate verification sheet found
RAG retrieval giving wrong or stale answers to "what's the N-year FD
rate" for several banks -- a stale, unsynced competitor document
outranking the real one (SBI, Bank of Baroda), a tenure-reordering bug
promoting the wrong neighboring band (HDFC), and a chunk-split issue
(PNB). Retrieval ranking can't be made to structurally guarantee a
correct answer to this one question shape; this shortcut sidesteps
retrieval for it entirely, using the exact same compare_for_tenure()
(compare_rates.py) Compare Rates itself calls, so the two can never
disagree with each other.

Confidence is deliberately narrow -- an explicit rate/FD cue AND exactly
one unambiguous tenure phrase, and (outside bank-scoped chat, where the
bank is already fixed by the sidebar) either a named bank or a ranking
word. Anything this isn't sure about returns None from
parse_fd_rate_intent() and the caller falls through to normal RAG+Groq
chat -- never a guessed bank, tenure, or ranking.

Run standalone for a quick manual check:

    python automation/fd_rate_shortcut.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))

from compare_rates import compare_for_tenure  # noqa: E402
from gap_notes import freshness_label  # noqa: E402
from db import get_connection  # noqa: E402

_RATE_INTENT_RE = re.compile(r"\b(rate|fd|fixed\s+deposit)\b", re.IGNORECASE)
# Narrower than _RATE_INTENT_RE on purpose -- "rate" alone also matches a
# home/education/vehicle/personal loan question, which this shortcut does
# NOT cover and must never hijack into a tenure prompt (see
# looks_like_unparsed_fd_question() below).
_FD_SPECIFIC_RE = re.compile(r"\b(fd|fixed\s+deposit)\b", re.IGNORECASE)
_OTHER_PRODUCT_RE = re.compile(
    r"\b(home\s+loan|education\s+loan|vehicle\s+loan|personal\s+loan|"
    r"gold\s+loan|mortgage|mclr|repo|benchmark)\b", re.IGNORECASE,
)
# "which bank(s)" and "maximum" added 2026-11-07 -- found live on
# Streamlit Cloud that "Which bank has the highest 3-year FD rate?" fell
# through to the old text answer; "highest" alone already matched, so the
# real bug there was the tenure phrase (see _TENURE_PHRASE_RE below), but
# "which bank" is just as clearly a ranking question even WITHOUT another
# ranking word ("which bank offers the best 3-year FD" has "best" too, but
# a bare "which bank for a 3 year FD" should still rank, not demand a
# named bank it was never given).
_RANKING_RE = re.compile(r"\b(highest|best|lowest|top|maximum|which\s+banks?)\b", re.IGNORECASE)
_LOWEST_RE = re.compile(r"\blowest\b", re.IGNORECASE)
_SENIOR_RE = re.compile(r"\bsenior\b", re.IGNORECASE)

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
# [\s-]* (not \s*) between the amount and the unit so "3-year" matches --
# the exact real-world phrasing that fell through on Streamlit Cloud
# ("Which bank has the highest 3-year FD rate?" has a hyphen, "highest FD
# rate for 3 years" doesn't -- only the hyphenated one failed). Bare "y"
# ("3y") is listed LAST in the unit alternation, same ordering rule
# tenure_utils.py's own token regex uses, so "years"/"yr"/"yrs" are tried
# first and a real "3 years" never gets short-matched as "3 y" + "ears".
_TENURE_PHRASE_RE = re.compile(
    r"\b(?:(\d+)|(" + "|".join(_WORD_NUMBERS) + r"))[\s-]*"
    r"(day|days|month|months|year|years|yr|yrs|y)\b",
    re.IGNORECASE,
)
_TENURE_UNIT_DAYS = {
    "day": 1, "days": 1, "month": 30, "months": 30,
    "year": 365, "years": 365, "yr": 365, "yrs": 365, "y": 365,
}

# A separate map from app.py's own _BANK_MENTION_PATTERNS -- that one
# returns a DISPLAY name ("Bank of Baroda"), not the lowercase automation
# id ("bob") automation.duckdb's tables actually use, and the two naming
# schemes don't correspond 1:1 (see gap_notes.py's own module docstring).
# Kept local and minimal rather than reverse-mapping display names.
_BANK_ID_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bindian\s+bank\b", re.I), "indian_bank", "Indian Bank"),
    (re.compile(r"\bsbi\b|\bstate\s+bank\s+of\s+india\b", re.I), "sbi", "SBI"),
    (re.compile(r"\bbob\b|\bbank\s+of\s+baroda\b", re.I), "bob", "Bank of Baroda"),
    (re.compile(r"\bcanara(\s+bank)?\b", re.I), "canara", "Canara Bank"),
    (re.compile(r"\bpnb\b|\bpunjab\s+national\s+bank\b", re.I), "pnb", "PNB"),
    (re.compile(r"\bboi\b|\bbank\s+of\s+india\b", re.I), "boi", "BOI"),
    (re.compile(r"\bunion\s+bank(\s+of\s+india)?\b", re.I), "union_bank", "Union Bank of India"),
    (re.compile(r"\biob\b|\bindian\s+overseas\s+bank\b", re.I), "iob", "IOB"),
    (re.compile(r"\bcentral\s+bank(\s+of\s+india)?\b", re.I), "central_bank", "Central Bank of India"),
    (re.compile(r"\bpunjab\s*(&|and)\s*sind\b", re.I), "punjab_sind", "Punjab & Sind Bank"),
    (re.compile(r"\bbank\s+of\s+maharashtra\b|\bbom\b", re.I), "bom", "Bank of Maharashtra"),
    (re.compile(r"\buco(\s+bank)?\b", re.I), "uco", "UCO Bank"),
    (re.compile(r"\bicici(\s+bank)?\b", re.I), "icici", "ICICI Bank"),
    (re.compile(r"\bhdfc(\s+bank)?\b", re.I), "hdfc", "HDFC Bank"),
    (re.compile(r"\bkotak(\s+mahindra)?(\s+bank)?\b", re.I), "kotak", "Kotak Mahindra Bank"),
    (re.compile(r"\baxis(\s+bank)?\b", re.I), "axis", "Axis Bank"),
    (re.compile(r"\bindus\s*ind(\s+bank)?\b", re.I), "indusind", "IndusInd Bank"),
]
_DISPLAY_NAME = {bank_id: name for _pat, bank_id, name in _BANK_ID_PATTERNS}


def _detect_bank_id(query: str) -> str | None:
    for pattern, bank_id, _name in _BANK_ID_PATTERNS:
        if pattern.search(query):
            return bank_id
    return None


def _extract_tenure_days(query: str) -> tuple[int, str] | None:
    """Exactly one tenure phrase required -- zero or more than one is
    ambiguous, and this never guesses which one the user means."""
    matches = list(_TENURE_PHRASE_RE.finditer(query))
    if len(matches) != 1:
        return None
    m = matches[0]
    digit_str, word_str, unit = m.group(1), m.group(2), m.group(3)
    amount = int(digit_str) if digit_str else _WORD_NUMBERS[word_str.lower()]
    unit_l = unit.lower()
    if unit_l in ("month", "months") and amount % 12 == 0:
        # "36 months" means exactly 3 years (1095 days), not 36*30=1080 --
        # a real difference: 1080 falls inside several banks' "2 years to
        # < 3 years" band, not their "exactly 3 years" one, so the naive
        # 30-day-month conversion would have silently answered the WRONG
        # band for any whole-year phrasing stated in months. Only applies
        # when evenly divisible by 12 -- "18 months" (not a whole number
        # of years) is intentionally left as 18*30=540, its own genuine
        # non-year tenure point.
        days = (amount // 12) * 365
    else:
        days = amount * _TENURE_UNIT_DAYS[unit_l]
    return days, m.group(0)


def looks_like_unparsed_fd_question(query: str) -> bool:
    """True when the query is confidently an FD-rate or FD-ranking
    question that parse_fd_rate_intent() still returned None for -- the
    caller should prompt for a tenure rather than silently falling
    through to normal RAG+Groq chat (found live on Streamlit Cloud:
    "Which bank has the highest 3-year FD rate?" fell through to the old
    text answer because its hyphenated "3-year" didn't match the tenure
    regex at the time -- a confusing silent failure for a question this
    shortcut clearly should have answered).

    Deliberately narrower than a bare _RATE_INTENT_RE match: a home/
    education/vehicle/personal loan rate question (or a repo/MCLR/
    benchmark one) also contains "rate" but isn't an FD question this
    shortcut covers at all, and must still fall through normally rather
    than being wrongly told to add a tenure it was never missing."""
    if _OTHER_PRODUCT_RE.search(query):
        return False
    if _FD_SPECIFIC_RE.search(query):
        return True
    return bool(_RATE_INTENT_RE.search(query) and _RANKING_RE.search(query))


TENURE_PROMPT = "Please include a tenure, e.g. 3 years."


def parse_fd_rate_intent(query: str, context_bank_id: str | None = None) -> dict | None:
    """Returns a parsed intent dict, or None if this isn't confidently a
    "what/who has the FD rate for tenure X" question.

    `context_bank_id` is bank-scoped chat's current bank (an automation
    id), or None for General Chat/RBI Guidelines. When set, it's used
    directly -- a bank named in the query text is NOT allowed to
    override it, since the user is already looking at that bank's own
    page; silently answering for a different bank would be surprising.

    Every early return here is logged (`print`, so it shows up in
    Streamlit Cloud's own logs, not just a local console) -- this
    function returning None used to be a completely silent fall-through
    to normal chat, which made the "3-year" hyphen bug above invisible
    until a user happened to notice and report it.
    """
    if not _RATE_INTENT_RE.search(query):
        print(f"[fd_shortcut] fell through: no rate/FD intent cue -- {query!r}")
        return None
    tenure = _extract_tenure_days(query)
    if tenure is None:
        print(f"[fd_shortcut] fell through: no single parseable tenure phrase -- {query!r}")
        return None
    days, tenure_phrase = tenure

    ranking = bool(_RANKING_RE.search(query))
    direction = "lowest" if _LOWEST_RE.search(query) else "highest"
    customer_type = "senior" if _SENIOR_RE.search(query) else "general"
    bank_id = context_bank_id if context_bank_id else _detect_bank_id(query)

    if not ranking and bank_id is None:
        # No bank in context, none named, and not a ranking question --
        # genuinely ambiguous which bank is meant. Never guess.
        print(f"[fd_shortcut] fell through: no bank named/in context and not a ranking question -- {query!r}")
        return None

    return {
        "days": days, "tenure_phrase": tenure_phrase, "ranking": ranking,
        "direction": direction, "customer_type": customer_type, "bank_id": bank_id,
    }


def _row_detail(bank_id: str, tenure_label: str) -> tuple[float | None, float | None, str | None]:
    """Both rates for one bank's EXACT tenure_label, read directly --
    never re-derived from two separate compare_for_tenure() calls, which
    can legitimately pick different tenure_labels for general vs. senior
    at the same target day (confirmed for real: SBI's 5-year query picks
    a different band for general than for senior, since a neighboring
    band pays more to general customers at that exact point). Reading
    both columns off the one row compare_for_tenure() already chose keeps
    "the tenure band" singular and internally consistent."""
    con = get_connection()
    row = con.execute(
        "SELECT interest_rate_general, interest_rate_senior, data_source FROM fd_rates "
        "WHERE bank_id = ? AND tenure_label = ? AND product_type = 'fixed_deposit' "
        "AND deposit_variant = 'retail' LIMIT 1",
        [bank_id, tenure_label],
    ).fetchone()
    con.close()
    if row is None:
        return None, None, None
    return row[0], row[1], row[2]


def _format_row(bank_id: str, tenure_label: str) -> str:
    general, senior, data_source = _row_detail(bank_id, tenure_label)
    label = _DISPLAY_NAME.get(bank_id, bank_id.replace("_", " ").title())
    fresh = freshness_label(bank_id, "rate", data_source=data_source, source_id=bank_id)
    g_s = f"{general:g}%" if general is not None else "not published"
    s_s = f"{senior:g}%" if senior is not None else "not published"
    return f"**{label}** — {tenure_label}: General {g_s} / Senior {s_s} ({fresh['label']})"


def render_fd_rate_answer(intent: dict) -> str:
    """Plain-text (light Markdown) chat answer -- the ONLY numbers in it
    come straight from compare_for_tenure(), never typed or guessed
    here."""
    primary_type = intent["customer_type"]
    results = compare_for_tenure(intent["days"], primary_type)

    if intent["ranking"]:
        # compare_for_tenure() already sorts highest-first; "lowest" needs
        # the opposite end, shown ascending (lowest first) -- otherwise a
        # "lowest FD rate" question would silently get answered with the
        # top-5 HIGHEST rates, which is wrong, not just unhelpful.
        ordered = results if intent["direction"] == "highest" else list(reversed(results))
        top5 = ordered[:5]
        if not top5:
            return f"No bank has a published FD rate covering {intent['tenure_phrase']}."
        who = "senior citizen" if primary_type == "senior" else "general public"
        which = "Lowest" if intent["direction"] == "lowest" else "Top"
        lines = [f"{which} {len(top5)} FD rates for {intent['tenure_phrase']} ({who}):", ""]
        for i, r in enumerate(top5, 1):
            lines.append(f"{i}. {_format_row(r['bank_id'], r['tenure_label'])}")
        return "\n".join(lines)

    bank_id = intent["bank_id"]
    match = next((r for r in results if r["bank_id"] == bank_id), None)
    if match is None:
        label = _DISPLAY_NAME.get(bank_id, bank_id.replace("_", " ").title())
        return f"{label} has no published FD rate covering {intent['tenure_phrase']}."
    return _format_row(bank_id, match["tenure_label"])


if __name__ == "__main__":
    for q in [
        "What is the highest 3 year FD rate?",
        "What is SBI's 1 year FD rate?",
        "HDFC 5 year senior citizen FD rate",
    ]:
        intent = parse_fd_rate_intent(q)
        print(f"Q: {q}")
        print(f"  intent: {intent}")
        if intent:
            print(f"  {render_fd_rate_answer(intent)}")
        print()
