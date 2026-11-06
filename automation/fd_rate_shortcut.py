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
_RANKING_RE = re.compile(r"\b(highest|best|lowest|top)\b", re.IGNORECASE)
_LOWEST_RE = re.compile(r"\blowest\b", re.IGNORECASE)
_SENIOR_RE = re.compile(r"\bsenior\b", re.IGNORECASE)

_TENURE_PHRASE_RE = re.compile(r"\b(\d+)\s*(day|days|month|months|year|years|yr|yrs)\b", re.IGNORECASE)
_TENURE_UNIT_DAYS = {
    "day": 1, "days": 1, "month": 30, "months": 30,
    "year": 365, "years": 365, "yr": 365, "yrs": 365,
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
    amount, unit = int(matches[0].group(1)), matches[0].group(2).lower()
    return amount * _TENURE_UNIT_DAYS[unit], matches[0].group(0)


def parse_fd_rate_intent(query: str, context_bank_id: str | None = None) -> dict | None:
    """Returns a parsed intent dict, or None if this isn't confidently a
    "what/who has the FD rate for tenure X" question.

    `context_bank_id` is bank-scoped chat's current bank (an automation
    id), or None for General Chat/RBI Guidelines. When set, it's used
    directly -- a bank named in the query text is NOT allowed to
    override it, since the user is already looking at that bank's own
    page; silently answering for a different bank would be surprising.
    """
    if not _RATE_INTENT_RE.search(query):
        return None
    tenure = _extract_tenure_days(query)
    if tenure is None:
        return None
    days, tenure_phrase = tenure

    ranking = bool(_RANKING_RE.search(query))
    direction = "lowest" if _LOWEST_RE.search(query) else "highest"
    customer_type = "senior" if _SENIOR_RE.search(query) else "general"
    bank_id = context_bank_id if context_bank_id else _detect_bank_id(query)

    if not ranking and bank_id is None:
        # No bank in context, none named, and not a ranking question --
        # genuinely ambiguous which bank is meant. Never guess.
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
