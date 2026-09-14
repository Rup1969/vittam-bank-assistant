"""Phase B — LLM-assisted structured extraction from newly-changed sources.

Run manually, after fetch_and_track.py has recorded at least one changed
fetch:

    python automation/extract_structured.py

For each fetch_log row marked changed=True that hasn't been extracted yet:
  1. Re-read the raw HTML snapshot, clean it to text.
  2. Ask Groq (same provider/key as the main app) to extract FD-rate rows
     as JSON matching FDRateRecord, with an explicit "null if not stated,
     never infer" instruction (spec B2).
  3. Validate every returned row against FDRateRecord (pydantic) before
     accepting it — malformed rows are logged and skipped, never silently
     stored (spec B2's "reject and log if malformed").
  4. Insert valid rows into the fd_rates table.

Cleaned page text is windowed down to the actual rate-table region before
being sent to the LLM (see find_rate_table_window below) rather than just
truncated to the first N characters. This was a real, confirmed bug, not a
hypothetical one: both SBI's and Bank of Baroda's real tenure/rate tables
sit ~15,000 characters into the cleaned page text (well past a long
navigation-menu preamble), so a naive "first 6000 chars" truncation handed
the LLM nothing but menu text — with no real numbers in its input, it
fabricated a plausible-looking table instead of correctly reporting
nothing was found. This was caught by cross-checking the extracted numbers
against real, independently-verified rates, not assumed safe because the
code ran without error.

find_rate_table_window locates the densest cluster of percentage-like
numbers in the page — the actual table — rather than assuming a fixed
character offset, so it isn't tuned to any one bank's specific page layout.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber
from groq import Groq
from pydantic import ValidationError

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))   # for settings.py
sys.path.insert(0, str(AUTOMATION_DIR))  # for db.py, schema.py, fetch_and_track.py

import settings  # noqa: E402
from db import already_extracted, get_connection  # noqa: E402
from fetch_and_track import clean_text  # noqa: E402
from schema import FDRateRecord  # noqa: E402

# Keeps a single extraction call well inside Groq's 6000 TPM budget instead
# of exhausting it on page boilerplate alone (see module docstring).
# Widened from an initial 6000 — anchoring on the START of a table (see
# find_rate_table_window) needs enough runway before the density-threshold
# crossing point to include the table's own header/opening rows, which a
# smaller window was cutting off (confirmed on BOB's retail table).
# Lowered from 8000 to 4000 on 2026-08-18 (see EXTRACTION_MAX_TOKENS note
# below): both of SBI's real tables (retail + non-callable) together span
# well under 2000 chars, so 8000 was mostly wasted prompt-token budget
# competing with the model's own hidden reasoning tokens under Groq's
# fixed 8000 TPM per-request cap. Only SBI uses this generic path today —
# revisit if a future bank needs the generic path with a wider table.
MAX_INPUT_CHARS = 4000

_RATE_NUMBER_RE = re.compile(r"\d{1,2}\.\d{1,2}\s*%?")


def find_rate_table_window(text: str, window_chars: int = MAX_INPUT_CHARS) -> str:
    """Return the window_chars-wide slice of text starting at the FIRST
    substantially-dense cluster of rate-like numbers — i.e. the actual
    rate table, not whatever happens to be first on the page (nav/legal
    boilerplate) or necessarily the single densest region either.

    Prefers the earliest cluster (not the globally densest) because a real
    page can have multiple tables — confirmed on Canara's deposit-rates
    page, which stacks a retail table (below Rs. 3 crore, the one this
    pipeline wants) before several bulk/crore-banded tables that repeat
    near-identical rates across many amount columns, making them denser
    by this metric despite being the WRONG table. Retail-before-bulk is
    the layout convention observed across every bank page fetched this
    session, so "earliest dense-enough cluster" is a better generic
    signal than "densest cluster" once more than one table exists.

    Falls back to the first window_chars of text if fewer than 3
    rate-like numbers exist at all (nothing to cluster around)."""
    positions = [m.start() for m in _RATE_NUMBER_RE.finditer(text)]
    if len(positions) < 3:
        return text[:window_chars]

    # Density = how many other matches fall within 500 chars of this one —
    # a real rate table has many numbers packed close together; isolated
    # mentions elsewhere on the page (e.g. "RBI Repo Rate: 5.25%") don't
    # cluster with anything nearby.
    densities = [
        (p, sum(1 for q in positions if abs(q - p) <= 500))
        for p in positions
    ]
    max_density = max(d for _, d in densities)
    # Earliest position reaching at least half the peak density — high
    # enough to skip isolated single mentions, low enough that the first
    # real table (even if a later one is denser) still qualifies.
    threshold = max(3, max_density // 2)
    best_pos = next(p for p, d in densities if d >= threshold)

    # Back off well before the threshold-crossing point — a table's own
    # header row and opening tenure bands sit BEFORE the point where rate
    # density first crosses the threshold (confirmed on BOB: the crossing
    # point landed mid-table, cutting off "7 days to 14 days" onward).
    start = max(0, best_pos - 1500)
    return text[start:start + window_chars]

# Deliberately NOT settings.MAX_TOKENS (450) — that value is tuned for
# short conversational chat answers in the live app. Extracting a full
# array of tenure-rate rows as JSON needs more output room than that, or
# the response gets cut off mid-array (confirmed: 450 truncated the JSON
# for both SBI and Canara on the first real run). This script is a batch
# job that runs rarely, not a live per-user query, so a larger one-off
# budget here doesn't compete with interactive usage in practice.
#
# Raised from 2000 to 6500 on 2026-08-18 after adding the deposit_variant
# retail/non_callable instructions (see FDRateRecord.deposit_variant):
# the longer, more conditional prompt made openai/gpt-oss-20b's hidden
# reasoning balloon to ~3900 tokens for SBI's real page — confirmed via
# direct reproduction at 2000/3000/4000 (all finish_reason="length", 0
# visible chars) vs 6000 (finish_reason="stop", real JSON, usage showed
# reasoning_tokens=3917 of 4819 completion tokens). Same failure class as
# the live-chat MAX_TOKENS bug — see project memory's reasoning-model
# lesson: any prompt change needs this retested, not just a model change.
EXTRACTION_MAX_TOKENS = 5500

EXTRACTION_PROMPT = """\
You are extracting fixed deposit interest rate data from a bank's rate page.

Return ONLY a JSON array of objects, one per tenure row, each with exactly
these fields:
  tenure_label (string, e.g. "7-45 days" or "1 year" — copy the page's own wording)
  tenure_days_min (integer or null)
  tenure_days_max (integer or null)
  interest_rate_general (number or null)
  interest_rate_senior (number or null)
  min_deposit (number or null)
  effective_date (string "YYYY-MM-DD" or null)
  deposit_variant (string, exactly "retail" or "non_callable" — see rule below)

Rules:
- Use null for anything not explicitly stated on the page. Never infer or
  estimate a value that isn't there.
- Only extract rows that are actually fixed/term deposit tenure-rate rows —
  ignore navigation, loan rates, unrelated content.
- Some tables list a "Rate of Interest" figure AND a separate "Annualised
  Interest Yield" figure for each customer category (they look similar but
  are NOT the same number — yield is always slightly higher, reflecting
  compounding). ALWAYS use the "Rate of Interest" value for
  interest_rate_general/interest_rate_senior. NEVER use the "Annualised
  Interest Yield" value, even though it appears right next to the rate and
  is easy to mistake for it.
- If a tenure row has one number per category (just Rate, no separate
  Yield), use that number directly — this warning only applies when both
  a Rate and a Yield are explicitly present as separate figures.
- Some tables show BOTH an "Existing" (prior, no-longer-current) rate AND
  a "Revised" (latest, currently-effective) rate for each customer
  category, e.g. four columns per row: Existing-General, Revised-General,
  Existing-Senior, Revised-Senior. ALWAYS use the REVISED/latest column
  for interest_rate_general and interest_rate_senior — never the
  Existing/prior one, and never mix a General column with a Senior
  column's number. Match each value to BOTH its correct customer category
  (General vs Senior) AND its correct revision status (Existing vs
  Revised) — a table can have up to 4 numbers per row and only ONE of
  them is the current General rate, only ONE is the current Senior rate.
- Extract the RETAIL / general-public domestic term deposit table
  (typically labelled "below Rs. 3 crore" or similar, with columns for
  General Public and Senior Citizen) — tag every row from this table
  deposit_variant="retail".
- If the page ALSO has a separate "Non-Callable Term Deposit" (or
  "Non-Callable" / "Special") table for the retail/below-Rs.3-crore
  segment — i.e. a real, distinctly-named second product, not just a
  bulk/wholesale amount tier — extract those rows too, tagged
  deposit_variant="non_callable". Do NOT silently drop this table: a
  customer comparing rates across banks needs to see it, just clearly
  labeled as a different (premature-withdrawal-not-allowed) product from
  the plain retail one, not merged into the same figure.
- Do NOT extract bulk/high-value deposit tables (segmented by crore-amount
  bands) or NRI/NRE/FCNR-specific tables, even if they appear in the text —
  a page often has several deposit tables and only the retail and (if
  present) non-callable-retail ones are wanted here.
- WARNING: a bulk/crore-banded table sometimes splits a tenure into finer
  bands than the retail table does (e.g. a bulk table might list "91 Days
  to 120 Days" and "121 Days to 179 Days" as two separate rows, where the
  retail table has ONE row "91 Days to 179 Days" covering the same span).
  If you see a tenure phrase near a wall of many repeated identical or
  near-identical numbers (a sign of a crore-band table repeating the same
  rate across many amount columns), that is the bulk table — skip it, and
  use the coarser tenure label from the retail table instead, even if the
  retail table's row appears less prominently in the text.
- WARNING: some tables have a "Particulars" or similar column, right
  after the tenure, describing a rate as a SPREAD rather than stating a
  plain rate (e.g. "0.30% above Card Rate"). That percentage is NOT a
  fixed deposit rate — it's a formula, not a value. Skip this entire row
  (a "non-callable"-style table structured this way) rather than pulling
  that spread number, or a nearby number, into
  interest_rate_general/interest_rate_senior.
- If the text below does not contain an actual fixed-deposit rate table
  with real tenure and percentage values, return an empty array []. Do
  NOT invent a plausible-looking table — an empty array is the CORRECT
  answer when no real table is present, not a failure.
- Return valid JSON only, no markdown fences, no commentary.

Page text:
{page_text}
"""


def extract_rows(client: Groq, page_text: str) -> list[dict]:
    truncated = find_rate_table_window(page_text)
    resp = client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "user", "content": EXTRACTION_PROMPT.format(page_text=truncated)},
        ],
        max_tokens=EXTRACTION_MAX_TOKENS,
        temperature=0.0,
    )
    content = resp.choices[0].message.content.strip()
    # Strip markdown fences if the model added them despite instructions.
    if content.startswith("```"):
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
    return json.loads(content)


# Canara's retail deposit table lists 8 numbers per tenure row (Callable
# General Rate/Yield, Callable Senior Rate/Yield, Non-Callable General
# Rate/Yield, Non-Callable Senior Rate/Yield — or "NA" where a cell is
# blank). Two separate LLM prompt attempts (a plain instruction, then an
# explicit "use Rate not Yield" warning) both still returned the Yield
# figure for interest_rate_senior instead of the Rate — confirmed by
# cross-checking against the real page twice. Rather than keep guessing at
# prompt wording for an 8-column table, this pulls the known-fixed column
# positions directly: position 0 = Callable General Rate, position 2 =
# Callable Senior Rate (0-indexed) — deterministic, not a language-model
# guess. This is a deliberate one-source carve-out from the generic LLM
# pipeline, not a template for every future source; most bank pages so far
# have a simpler 2-column (General/Senior) table the LLM handles correctly.
_CANARA_TENURE_LABELS = [
    "7 Days to 45 Days",
    "46 Days to 90 Days",
    "91 Days to 179 Days",
    "180 Days to 269 Days",
    "270 Days to less than 1 Year",
    "1 Year & above to 1 year 3 months Only (Except 444 days)",
    "444 Days",
    "555 Days",
    "Above 1 Year 3 months to less than 2 Years (Except 555 days)",
    "2 Years & above to less than 3 Years",
    "3 Years & above to less than 5 Years",
    "5 Years & above to 10 Years",
]
_CANARA_NUMBER_RE = r"([\d.]+|NA)"


def extract_canara_rows(text: str) -> list[dict]:
    """Deterministic parser for Canara's retail term-deposit table — see
    module-level comment above for why this bypasses the LLM entirely."""
    date_match = re.search(r"w\.e\.f\.?\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    rows = []
    for label in _CANARA_TENURE_LABELS:
        pattern = re.escape(label) + r"\*?\s+" + r"\s+".join([_CANARA_NUMBER_RE] * 4)
        m = re.search(pattern, text)
        if not m:
            continue
        gen_rate, _gen_yield, sr_rate, _sr_yield = m.groups()
        rows.append({
            "tenure_label": label,
            "interest_rate_general": None if gen_rate == "NA" else float(gen_rate),
            "interest_rate_senior": None if sr_rate == "NA" else float(sr_rate),
            "effective_date": effective_date,
        })
    return rows


# PNB's rendered page has NO inline '%' next to the actual FD table cells —
# only the unrelated savings-account historical-rate section (a long list
# of prior years' revisions) is %-dense, so find_rate_table_window's
# %-density heuristic anchors on that instead of the real table entirely.
# A different failure mode from Canara's column-misalignment, but the same
# resolution: bypass the LLM with a parser keyed to this page's own known,
# stable structure. PNB's table is a clean numbered row format
# ("<Sl.No> <Period> <General> <Senior> <SuperSenior>", repeated 23 times)
# rather than Canara's fixed label set, so this uses a row regex instead of
# a lookup list — confirmed to extract all 23 rows correctly against the
# live page (2026-08-15), matching the bank's own official rate card
# verbatim. This was also independently necessary regardless of the
# %-density issue: PNB's cleaned page text alone (~8.7k chars between the
# first and last '%' sign) plus the extraction prompt overhead pushed the
# generic LLM path over Groq's 6000 TPM per-request cap.
_PNB_ROW_RE = re.compile(
    r"(?P<sl>\d{1,2})\s+(?P<period>.+?)\s+"
    r"(?P<general>\d{1,2}\.\d{2})\s+(?P<senior>\d{1,2}\.\d{2})\s+(?P<super>\d{1,2}\.\d{2})"
    r"(?=\s+\d{1,2}\s+|\s*$)"
)


def extract_pnb_rows(text: str) -> list[dict]:
    """Deterministic parser for PNB's retail term-deposit table — see
    module-level comment above for why this bypasses the LLM entirely."""
    table_start = text.find("Domestic Term Deposit")
    table_end = text.find("PNB Uttam", table_start)
    if table_start == -1 or table_end == -1:
        return []
    region = text[table_start:table_end]

    date_match = re.search(r"revised w\.e\.f\.?\s*(\d{2})\.(\d{2})\.(\d{4})", region)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    # Anchor past the column-header row so the header's own embedded dates
    # ("w.e.f. 01.06.2026") can't be mistaken for a row's leading Sl.No —
    # confirmed as a real false-match without this anchor.
    header_end = re.search(
        r"Super Senior Citizens w\.e\.f\.?\s*\d{2}\.\d{2}\.\d{4}", region
    )
    table_text = region[header_end.end():] if header_end else region

    rows = []
    for m in _PNB_ROW_RE.finditer(table_text):
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": float(m.group("general")),
            "interest_rate_senior": float(m.group("senior")),
            "effective_date": effective_date,
        })
    return rows


# Union Bank's page mixes a Callable-deposit rate and a Non-Callable-deposit
# rate/"NO Slab" in the same flattened table text, and — unlike PNB/Canara —
# most rows only print ONE value at all (the Non-Callable column appears to
# use HTML rowspan in the real table, so inner_text only repeats it on rows
# where the value actually changes, flattening to a variable-length token
# stream rather than a fixed column count per row). A fixed-column regex
# (like PNB's) can't handle a variable number of trailing values per row, so
# this tokenizes on rate-shaped tokens ("\d{1,2}\.\d{2}" or the literal "NO
# Slab") first, then groups each run of value-tokens under the preceding
# non-value (period) token — the first value in each group is always the
# retail Callable rate (what this table's own header calls "< Rs. 3 Cr"),
# which is what this pipeline's schema tracks; any second value (Non-
# Callable, "> Rs 1 Cr to < Rs 3 Cr") is intentionally discarded, same as
# every other bank's non-callable/bulk variant is out of scope here.
# Senior/super-senior rates are NOT printed per-row at all — the page
# states them as a flat +0.50%/+0.75% addendum in prose instead, so they're
# computed here rather than parsed per-row.
_UNION_VALUE_RE = re.compile(r"\d{1,2}\.\d{2}|NO Slab")
_UNION_SENIOR_PREMIUM = 0.50


def extract_union_bank_rows(text: str) -> list[dict]:
    """Deterministic parser for Union Bank's retail term-deposit table —
    see module-level comment above for why this bypasses the LLM entirely."""
    table_start = text.find("7-14 Days")
    table_end = text.find("Union Bank of India offers an additional rate component")
    if table_start == -1 or table_end == -1:
        return []
    table_text = text[table_start:table_end]

    date_match = re.search(r"effective from (\d{1,2})\s*\w*\s+(\w+)\s+(\d{4})", text)
    effective_date = None
    if date_match:
        try:
            effective_date = datetime.strptime(
                f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}",
                "%d %B %Y",
            ).strftime("%Y-%m-%d")
        except ValueError:
            effective_date = None

    tokens = [t.strip() for t in re.split(f"({_UNION_VALUE_RE.pattern})", table_text) if t.strip()]

    rows = []
    current_period, current_values = None, []
    for token in tokens:
        if _UNION_VALUE_RE.fullmatch(token):
            current_values.append(token)
        else:
            if current_period is not None and current_values:
                rows.append((current_period, current_values))
            current_period, current_values = token, []
    if current_period is not None and current_values:
        rows.append((current_period, current_values))

    out = []
    for period, values in rows:
        callable_rate = values[0]
        if callable_rate == "NO Slab":
            continue  # first value should always be a real rate; skip if not
        general = float(callable_rate)
        out.append({
            "tenure_label": period,
            "interest_rate_general": general,
            "interest_rate_senior": round(general + _UNION_SENIOR_PREMIUM, 2),
            "effective_date": effective_date,
        })
    return out


# IOB's table prints THREE numeric columns per row — "Existing" (the
# pre-revision rate, now stale), "Revised" (the current rate), and
# "Non-Callable" (a different product) — and confirmed via cross-check
# that the generic LLM path got this wrong in a genuinely new way: it put
# the STALE "Existing" value into interest_rate_general and the CURRENT
# "Revised" value into interest_rate_senior (mislabeling it as a senior
# rate, when it's actually just the correct general-public current rate),
# missing the real senior-citizen premium entirely. Confirmed 2026-08-15
# by comparing extracted rows against the live page's own three columns.
# Like Union Bank, IOB doesn't print a separate senior-citizen rate
# per row either — it's a flat +0.50%/+0.75% addendum stated in prose.
_IOB_VALUE_RE = re.compile(r"\d{1,2}\.\d{2}|(?<!\S)-(?!\S)")
_IOB_SENIOR_PREMIUM = 0.50


# BOB's page bundles the wanted retail table (below Rs. 3 crore) with
# several bulk/crore-tier and NRE/non-callable tables right after it on the
# same page. An earlier fix scoped the LLM PROMPT to "only the retail
# table" and that fixed correctness for a while — but it never reduced the
# actual INPUT size, and confirmed 2026-08-15: every extraction attempt for
# BOB fails with Groq's 6000 TPM cap on a SINGLE request (6113 tokens
# requested), because find_rate_table_window's 8000-char window still
# captures the retail table plus multiple following bulk-deposit tables
# regardless of what the prompt says to ignore. Rather than keep tuning
# window size against a moving page, this parses only the retail table
# directly — anchored between its own header and the next table's header,
# so bulk/NRE/non-callable tables never enter the input at all.
_BOB_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<general>\d{1,2}\.\d{2})\s+"
    r"(?P<senior>\d{1,2}\.\d{2})\s*[*#$]{0,2}\s+"
    r"(?P<super>\d{1,2}\.\d{2})\s*[*#$]{0,2}",
    re.S,
)


def extract_bob_rows(text: str) -> list[dict]:
    """Deterministic parser for BOB's retail term-deposit table — see
    module-level comment above for why this bypasses the LLM entirely."""
    table_start = text.find("Fixed Deposits (Callable): Domestic Term Deposits")
    table_end = text.find("Domestic Term Deposits & NRO Deposits", table_start)
    if table_start == -1 or table_end == -1:
        return []
    region = text[table_start:table_end]

    date_match = re.search(r"w\.e\.f\.?\s*(\d{2})[-.](\d{2})[-.](\d{4})", region)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    header_end = re.search(r"Resident Super Senior Citizen", region)
    table_text = region[header_end.end():] if header_end else region

    rows = []
    for m in _BOB_ROW_RE.finditer(table_text):
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": float(m.group("general")),
            "interest_rate_senior": float(m.group("senior")),
            "effective_date": effective_date,
        })
    return rows


# Central Bank of India's retail (<3Cr) table prints FOUR numbers per row —
# General-Rate, General-Yield, Senior-Rate, Senior-Yield — same Rate-vs-Yield
# shape as Canara, confirmed against the real 2026-08-19 fetch (e.g. "180 –
# 270 days 5.50% 5.61% 6.00% 6.14%" — Rate and Yield genuinely differ for
# longer tenures, though they coincide for short ones). Uses a generic
# non-greedy row regex (BOB's pattern) rather than Canara's fixed label list,
# since finditer naturally advances past each match regardless of the
# tenure label's exact wording/dash character — confirmed working against
# the real fetched text before trusting it, same discipline as every other
# parser here. A second table for the Rs. 3 Cr-10 Cr tier immediately
# follows with the same 12 tenure labels — the region is bounded to end
# before it so those never enter the parse.
_CENTRAL_BANK_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<g>\d{1,2}\.\d{2})%\s+(?P<gy>\d{1,2}\.\d{2})%\s+"
    r"(?P<s>\d{1,2}\.\d{2})%\s+(?P<sy>\d{1,2}\.\d{2})%"
)


def extract_central_bank_rows(text: str) -> list[dict]:
    """Deterministic parser for Central Bank of India's retail term-deposit
    table — see module-level comment above for why this bypasses the LLM."""
    start = text.find("7 -14 days")
    end = text.find("Maturity Period Rates for Deposits Rs. 3 Cr to Rs. 10 Cr")
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    date_match = re.search(r"W\.E\.F\.?\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    rows = []
    for m in _CENTRAL_BANK_ROW_RE.finditer(region):
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": float(m.group("g")),
            "interest_rate_senior": float(m.group("s")),
            "effective_date": effective_date,
        })
    return rows


# Bank of Maharashtra's "Regular Schemes" table prints one row per tenure
# with FOUR value slots — Callable(<3Cr), Non-Callable(<3Cr), Callable
# (3-10Cr), Non-Callable(3-10Cr) — but the retail (<3Cr) Non-Callable slot
# is explicitly published as the literal string "xx" (not a number) for
# EVERY row, confirmed against the real 2026-08-19 fetch — so there is no
# retail-tier deposit_variant ambiguity to resolve here, unlike SBI/Punjab
# & Sind. Only the Callable(<3Cr) value is captured; the 3-10Cr bulk column
# is skipped (same retail-only scope as every other bank's parser). Senior
# citizen rate isn't printed per-row at all — a flat "+0.50% Extra for
# Senior Citizens" footnote applies uniformly, so it's computed, not parsed
# (same pattern as Union Bank/IOB).
_BOM_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<retail>\d{1,2}\.\d{2})\s+xx\s+(?P<bulk>\d{1,2}\.\d{2})\s+xx"
)
_BOM_SENIOR_PREMIUM = 0.50


def extract_bom_rows(text: str) -> list[dict]:
    """Deterministic parser for Bank of Maharashtra's retail term-deposit
    table — see module-level comment above for why this bypasses the LLM."""
    start = text.find("7- 30 days")
    end = text.find("Amount Above Rs. 10 Cr to Rs. 100 Cr")
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    date_match = re.search(r"w\.e\.f\.?\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    rows = []
    for m in _BOM_ROW_RE.finditer(region):
        general = float(m.group("retail"))
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": general,
            "interest_rate_senior": round(general + _BOM_SENIOR_PREMIUM, 2),
            "effective_date": effective_date,
        })
    return rows


# UCO Bank's retail table prints TWO numbers per row — Rate and Yield (not
# General/Senior) — confirmed against the real 2026-08-19 fetch (e.g. "121-
# 150 days 4.25% 4.26%": Rate and Yield differ from the second decimal).
# Senior/staff premiums aren't printed per-row either — a separate flat
# table states "Senior Citizen 0.25%" (tenors up to 1 year) / "0.50%"
# (above 1 year), so they're computed, not parsed, same pattern as Union
# Bank/IOB/Bank of Maharashtra. Several named tenures pay a distinctly
# HIGHER rate than the immediately-surrounding standard slab — "333 days"
# (6.30% vs neighboring 5.00%/5.00%), "444 days" (6.45% vs 6.10%/6.10%),
# "535 days" (6.60% vs 6.10%/6.10%), and the three "(Green Deposit)" rows
# at 1000/2000/3000 days — a genuinely different shape of multi-rate-per-
# tenure than SBI/Punjab & Sind's callable/non-callable pairs (a named
# bonus product, not a withdrawal-flexibility variant of the SAME product),
# so these are tagged deposit_variant="special_tenor", not "non_callable".
_UCO_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<rate>\d{1,2}\.\d{2})%\s+(?P<yield>\d{1,2}\.\d{2})%"
)
_UCO_SPECIAL_LABELS = {"333 days", "444 days", "535 days"}


def extract_uco_rows(text: str) -> list[dict]:
    """Deterministic parser for UCO Bank's retail term-deposit table — see
    module-level comment above for why this bypasses the LLM entirely."""
    start = text.find("7-14 days")
    end = text.find("Note) 444 Day")
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    date_match = re.search(r"Revised w\.e\.f\.?\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    up_to_1yr_match = re.search(r"Senior Citizen\s+(\d{1,2}\.\d{2})%\s+(\d{1,2}\.\d{2})%", text)
    premium_up_to_1yr = float(up_to_1yr_match.group(1)) if up_to_1yr_match else 0.25
    premium_above_1yr = float(up_to_1yr_match.group(2)) if up_to_1yr_match else 0.50

    rows = []
    for m in _UCO_ROW_RE.finditer(region):
        label = m.group("period").strip()
        general = float(m.group("rate"))
        is_special = label in _UCO_SPECIAL_LABELS or "Green Deposit" in label
        # "1 year" / bare-day labels without "to"/"-"/"above" wording sit at
        # or past the 1-year boundary; a simpler, honest heuristic than
        # re-parsing tenure_utils here — err toward the (larger) above-1yr
        # premium for any label containing "year" or a day-count >= 365 is
        # more precision than this footnote table itself distinguishes by
        # label text alone, so tenures before "1 year" in the table's own
        # listed order use the up-to-1yr premium, at/after use above-1yr.
        premium = premium_up_to_1yr if label in (
            "7-14 days", "15-29 days", "30-45 days", "46-60 days", "61-90 days",
            "91-120 days", "121-150 days", "151-180 days", "181-332 days",
            "333 days", "334 days-364 days",
        ) else premium_above_1yr
        row = {
            "tenure_label": label,
            "interest_rate_general": general,
            "interest_rate_senior": round(general + premium, 2),
            "effective_date": effective_date,
        }
        if is_special:
            row["deposit_variant"] = "special_tenor"
        rows.append(row)
    return rows


# Punjab & Sind Bank's table has no separate rate column at all — plain
# "<label> <rate>" pairs (some labels 5+ words, no "%" sign on the number),
# confirmed against the real 2026-08-19 fetch (retried with wait_until=
# "commit" after the default networkidle wait timed out — same class of
# issue as IndusInd's pages). The Callable/Non-Callable distinction found
# in earlier RAG research is real and confirmed here too, but structured
# differently than expected: it's embedded IN the tenure label text itself
# ("375 Days (Callable)" / "375 Days (Non‑Callable*)" as two separate
# rows), not a second value column — note the label uses U+2011 NON-BREAKING
# HYPHEN in "Non-Callable", not a plain hyphen, so the variant check below
# matches on "Non" + "Callable" substrings rather than the literal dash.
# "PSB Green Earth" tenures (22/44/66 months) are a third real pattern —
# named bonus tenors paying above their neighboring standard slab, same
# deposit_variant="special_tenor" treatment as UCO's Green Deposits.
# Senior citizen rate isn't printed per-row — a flat +0.50% footnote
# applies only to tenures of 180 days and up (shorter tenures get none),
# so tenure_utils.parse_tenure_range is used to test the boundary
# generically rather than hardcoding every label, since this bank's label
# set is the most varied of any parser in this file (compound bounds like
# ">1 Year – 374 Days", "2Y-776 Days", "3Y - <44 Month").
_PSB_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<rate>\d{1,2}\.\d{2})(?:\(\$\)|\$|#)?"
)
_PSB_SENIOR_PREMIUM = 0.50
_PSB_SENIOR_MIN_DAYS = 180


def extract_punjab_sind_rows(text: str) -> list[dict]:
    """Deterministic parser for Punjab & Sind Bank's retail term-deposit
    table — see module-level comment above for why this bypasses the LLM."""
    from tenure_utils import parse_tenure_range

    start = text.find("7 – 14 Days")
    if start == -1:
        start = text.find("7 - 14 Days")
    end = text.find("(#) Minimum amount")
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    date_match = re.search(r"Revised w\.e\.f\.?\s*(\d{2})/(\d{2})/(\d{4})", text)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    rows = []
    for m in _PSB_ROW_RE.finditer(region):
        # A trailing "($)"/"#" marker from the PREVIOUS row can bleed into
        # the start of the next label when it directly precedes the label
        # with no separating row boundary in the flattened text — strip any
        # leading marker punctuation before trusting the label.
        label = re.sub(r"^[\s($)#]+", "", m.group("period")).strip()
        if not label:
            continue
        general = float(m.group("rate"))
        lo_days, _hi_days = parse_tenure_range(label)
        premium = _PSB_SENIOR_PREMIUM if lo_days >= _PSB_SENIOR_MIN_DAYS else 0.0
        row = {
            "tenure_label": label,
            "interest_rate_general": general,
            "interest_rate_senior": round(general + premium, 2),
            "effective_date": effective_date,
        }
        if "Non" in label and "Callable" in label:
            row["deposit_variant"] = "non_callable"
        elif "Green Earth" in label:
            row["deposit_variant"] = "special_tenor"
        rows.append(row)
    return rows


# SBI was the last bank still on the generic LLM extraction path — moved
# to a deterministic parser 2026-08-18 after the LLM proved genuinely
# UNRELIABLE (not just wrong once): the same page, same prompt, same
# temperature=0.0 produced three different sets of numbers across three
# consecutive runs (confirmed via direct reproduction — one run correctly
# picked the "Revised" column, another collapsed General into the Senior
# column for every row, a third silently dropped the entire Non-Callable
# table). Root cause: SBI's retail table prints FOUR numbers per row
# (Existing-General, Revised-General, Existing-Senior, Revised-Senior —
# added by the bank sometime between the 2026-08-15 and 2026-08-17
# fetches, changing the table from the simple 2-column shape the generic
# prompt was written for), and the reasoning model's hidden token budget
# under Groq's fixed 8000 TPM cap didn't leave enough room to reliably
# get both the column AND the customer-category right every time. Same
# resolution as every other bank that hit a page-specific quirk the
# generic LLM path couldn't handle reliably (Canara's Rate/Yield mixup,
# PNB's %-density heuristic, Union Bank's rowspan flattening, IOB's
# Existing/Revised/Non-Callable triple column) — bypass the LLM with a
# parser keyed to this page's own known, fixed structure.
#
# SBI ALSO genuinely publishes a second real product spanning the same
# tenures as parts of the retail table — a "Non-Callable Term Deposit
# (Retail)" table (1 Year and 2 Years only, as of 2026-08-18) at a higher
# rate in exchange for giving up premature withdrawal. This is captured
# too (tagged deposit_variant="non_callable"), not discarded, so
# compare_for_tenure() can show it as a labeled secondary option rather
# than either silently picking the higher number or silently dropping it
# — see FDRateRecord.deposit_variant and compare_rates.py.
_SBI_RETAIL_TENURE_LABELS = [
    "7 days to 45 days",
    "46 days to 179 days",
    "180 days to 210 days",
    "211 days to less than 1 year",
    "1 Year to less than 2 years",
    "2 years to less than 3 years",
    "3 years to less than 5 years",
    "5 years and up to 10 years",
]
_SBI_NON_CALLABLE_TENURE_LABELS = ["1 Year", "2 Years"]


def extract_sbi_rows(text: str) -> list[dict]:
    """Deterministic parser for SBI's retail term-deposit table (4 numbers
    per row: Existing-General, REVISED-General, Existing-Senior,
    REVISED-Senior — only the Revised pair is current) plus its separate
    Non-Callable Term Deposit (Retail) table — see module-level comment
    above for why this bypasses the LLM entirely."""
    date_match = re.search(
        r"Revised Rates for Public w\.e\.f\.?\s*(\d{2})/(\d{2})/(\d{4})", text
    )
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    rows = []
    for label in _SBI_RETAIL_TENURE_LABELS:
        pattern = re.escape(label) + r"\s+([\d.]+)\*?\s+([\d.]+)\*?\s+([\d.]+)\*?\s+([\d.]+)\*?"
        m = re.search(pattern, text)
        if not m:
            continue
        _existing_gen, revised_gen, _existing_sen, revised_sen = m.groups()
        rows.append({
            "tenure_label": label,
            "interest_rate_general": float(revised_gen),
            "interest_rate_senior": float(revised_sen),
            "effective_date": effective_date,
            "deposit_variant": "retail",
        })

    nc_start = text.find("NON-CALLABLE TERM DEPOSIT")
    nc_text = text[nc_start:nc_start + 500] if nc_start != -1 else ""
    for label in _SBI_NON_CALLABLE_TENURE_LABELS:
        pattern = re.escape(label) + r"\s+[\d.]+%\s+above\s+Card\s+Rate\s+([\d.]+)%\s+([\d.]+)%"
        m = re.search(pattern, nc_text)
        if not m:
            continue
        general, senior = m.groups()
        rows.append({
            "tenure_label": label,
            "interest_rate_general": float(general),
            "interest_rate_senior": float(senior),
            "effective_date": effective_date,
            "deposit_variant": "non_callable",
        })

    return rows


# ICICI's rendered HTML rate page only shows 2 of its ~10 real tenure rows
# statically — the rest are populated client-side from this bank's own JSON
# data API, which config/banks.json points at DIRECTLY for this source
# (bypassing the HTML page's table entirely, see its config note). This is
# the cleanest extraction of any bank so far — a real JSON parse, no
# regex/windowing needed — but the column mapping needed independent
# verification before trusting it: interestData is an array of several
# sub-arrays (one per crore-tier tab on the live page), and only sub-array
# index 0 turned out to be the retail (<3Cr) General/Senior table wanted
# here. Confirmed 2026-08-15 by cross-checking against the live page's
# visibly-rendered rows for two specific tenures — general=6.5%/senior=7.1%
# for "3Y1D-5Y" and general=6.5%/senior=7.0% for "5Y1D-10Y" both matched
# sub-array 0's c1/c2 fields exactly (including two tenures where senior
# is +0.60% instead of the standard +0.50%, also matching). Fields c3/c4
# in this JSON are unrelated internal values, not additional rate columns.
def extract_icici_rows(text: str) -> list[dict]:
    """Deterministic parser for ICICI's own JSON rate-data API — see
    module-level comment above for the column-mapping verification."""
    # fetch_source() navigates directly to the JSON URL; Chromium wraps a
    # raw-JSON response in <pre>...</pre>, and clean_text() strips that down
    # to just the JSON text (its collapsed whitespace doesn't affect
    # json.loads, which doesn't care about formatting).
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    retail_tier = data.get("interestData", [[]])[0]
    return [
        {
            "tenure_label": row["tenure"],
            "interest_rate_general": float(row["c1"]),
            "interest_rate_senior": float(row["c2"]),
            "effective_date": None,  # not present in this JSON; see RAG doc for the bank-stated date instead
        }
        for row in retail_tier
        if "tenure" in row and "c1" in row and "c2" in row
    ]


# HDFC's page is a clean two-column table (period, general%, senior%) with
# no inline footnote markers next to the numbers — the most straightforward
# structure of any bank onboarded so far. Two tables appear on the page in
# sequence (retail "< 3 Crore" first, then ">=3 Crore to <5 Crore"); this
# anchors on the first "RESIDENT CUSTOMERS:" marker (which appears once
# after each table) to stop before the bulk-tier table starts.
_HDFC_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<general>\d{1,2}\.\d{2})%\s+(?P<senior>\d{1,2}\.\d{2})%"
)


def extract_hdfc_rows(text: str) -> list[dict]:
    """Deterministic parser for HDFC's retail term-deposit table — see
    module-level comment above for the table structure."""
    table_start = text.find("Tenure Bucket < 3 Crore")
    table_end = text.find("RESIDENT CUSTOMERS", table_start)
    if table_start == -1 or table_end == -1:
        return []
    region = text[table_start:table_end]

    date_match = re.search(
        r"Applicable from (\d{1,2})\w*\s*(\w+),?\s*(\d{4})", text
    )
    effective_date = None
    if date_match:
        try:
            effective_date = datetime.strptime(
                f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}",
                "%d %B %Y",
            ).strftime("%Y-%m-%d")
        except ValueError:
            effective_date = None

    header_end = re.search(r"Senior Citizen Rates \(per annum\)", region)
    table_text = region[header_end.end():] if header_end else region

    rows = []
    for m in _HDFC_ROW_RE.finditer(table_text):
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": float(m.group("general")),
            "interest_rate_senior": float(m.group("senior")),
            "effective_date": effective_date,
        })
    return rows


# IndusInd's retail (<3Cr) table is the simplest structure onboarded so far:
# a plain 2-column table (period, general, senior), no '%' signs inline next
# to the numbers (unlike HDFC's), no crore-tier split within this one table.
# Confirmed via the "wait_until=commit" + settle-wait fetch fix (see
# config/banks.json's indusind note) — this table only appears once real
# content loads. Two more tables follow for the 3-5Cr and 1-5Cr non-callable
# tiers; the end anchor stops before those.
_INDUSIND_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<general>\d{1,2}\.\d{2})\s+(?P<senior>\d{1,2}\.\d{2})(?=\s|$)"
)


def extract_indusind_rows(text: str) -> list[dict]:
    """Deterministic parser for IndusInd's retail term-deposit table — see
    module-level comment above for the table structure."""
    table_start = text.find("Tenure Rate Rate")
    if table_start == -1:
        return []
    table_end = text.find("Note: Interest is compounded quarterly", table_start)
    if table_end == -1:
        return []
    table_text = text[table_start + len("Tenure Rate Rate"):table_end]

    date_match = re.search(r"w\.e\.f\.?\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
    effective_date = None
    if date_match:
        try:
            effective_date = datetime.strptime(
                f"{date_match.group(1)}/{date_match.group(2)}/{date_match.group(3)}",
                "%d/%m/%Y",
            ).strftime("%Y-%m-%d")
        except ValueError:
            effective_date = None

    rows = []
    for m in _INDUSIND_ROW_RE.finditer(table_text):
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": float(m.group("general")),
            "interest_rate_senior": float(m.group("senior")),
            "effective_date": effective_date,
        })
    return rows


# Kotak's retail table has 4 numeric columns per row (Regular <3Cr, Regular
# 3-5Cr, Senior <3Cr, Senior 3-5Cr) — same structural class as ICICI's
# crore-tier split, confirmed genuinely distinct per column (not just
# duplicated values) since some rows show different Senior<3Cr vs
# Senior3-5Cr figures. Wanted columns are position 0 (general, <3Cr) and
# position 2 (senior, <3Cr), matching the "retail below 3 crore" convention
# used for every other bank in this pipeline. A second, separate table for
# bulk tiers (Rs. 5 crore and above) follows immediately after on the same
# page — the end anchor stops before it starts.
_KOTAK_ROW_RE = re.compile(
    r"(?P<period>.+?)\s+(?P<c0>\d{1,2}\.\d{2})%\s+(?P<c1>\d{1,2}\.\d{2})%\s+"
    r"(?P<c2>\d{1,2}\.\d{2})%\s+(?P<c3>\d{1,2}\.\d{2})%"
)


def extract_kotak_rows(text: str) -> list[dict]:
    """Deterministic parser for Kotak's retail term-deposit table — see
    module-level comment above for the column mapping."""
    table_start = text.find("INTEREST RATES FOR DOMESTIC")
    if table_start == -1:
        return []
    # The "effective from <date>" phrase appears TWICE before the bulk-tier
    # table starts: once as this table's own intro line, once as the bulk
    # table's intro — searching from table_start+1 wrongly matches the
    # first (confirmed: produced 0 rows, matching an ~80-char region
    # between the two adjacent occurrences of this table's own header).
    # The second occurrence is the real end boundary.
    end_matches = list(re.finditer(
        r"Fixed Deposits Interest Rates for Domestic/ NRO / NRE effective", text[table_start:]
    ))
    if len(end_matches) < 2:
        return []
    table_end = table_start + end_matches[1].start()
    region = text[table_start:table_end]

    date_match = re.search(r"effective from (\d{1,2})\w*\s*(\w+)\s*(\d{4})", region)
    effective_date = None
    if date_match:
        try:
            effective_date = datetime.strptime(
                f"{date_match.group(1)} {date_match.group(2)} {date_match.group(3)}",
                "%d %B %Y",
            ).strftime("%Y-%m-%d")
        except ValueError:
            effective_date = None

    header_matches = list(re.finditer(r"Rs\. 3 Cr\. & above but less than Rs\. 5 Cr\.", region))
    table_text = region[header_matches[-1].end():] if header_matches else region

    rows = []
    for m in _KOTAK_ROW_RE.finditer(table_text):
        rows.append({
            "tenure_label": m.group("period").strip(),
            "interest_rate_general": float(m.group("c0")),
            "interest_rate_senior": float(m.group("c2")),
            "effective_date": effective_date,
        })
    return rows


# Axis's retail rate table only exists in a downloadable PDF, not on any
# HTML page — see config/banks.json's axis note. This parser takes a
# FILE PATH (unlike every other extract_*_rows function, which take the
# already-fetched text) because it needs pdfplumber's TABLE-aware
# extraction directly on the PDF, not a flattened-text regex — flattening
# a PDF table to plain text loses column alignment the same way HTML
# table-to-text flattening does elsewhere in this pipeline, and a
# genuine table-structure API is available here (unlike for HTML tables),
# so there's no reason not to use it.
#
# Confirmed structure (2026-08-15, reading the real PDF directly before
# writing this parser): page 1 has two tables — the first is just the
# page's title banner, the second is the real retail (<5cr overall,
# split into <3Cr/3-5Cr within it) rate table. Each data row has the
# tenure label at column index 1 and the four rate values at columns 4
# (General <3Cr), 7 (General 3-5Cr), 10 (Senior <3Cr), 13 (Senior 3-5Cr)
# — column 4 and 10 are the ones this pipeline's schema wants, matching
# the "retail below 3 crore" convention used for every other bank.
def extract_axis_rows(file_path: str) -> list[dict]:
    """Deterministic, table-aware parser for Axis's retail PDF rate table
    — see module-level comment above for the confirmed column layout."""
    with pdfplumber.open(file_path) as pdf:
        tables = pdf.pages[0].extract_tables()
    if len(tables) < 2:
        return []
    data_table = tables[1]

    effective_date = None
    for row in data_table:
        for cell in row:
            if cell and "w.e.f" in cell:
                m = re.search(r"(\d{1,2})\w*\s*(\w+)\s*(\d{4})", cell)
                if m:
                    try:
                        effective_date = datetime.strptime(
                            f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %B %Y"
                        ).strftime("%Y-%m-%d")
                    except ValueError:
                        pass
                break
        if effective_date:
            break

    rows = []
    for row in data_table:
        period = row[1] if len(row) > 1 else None
        if not period or not re.search(r"\d", period):
            continue  # header/blank rows — real tenure labels always contain a number
        try:
            general = float(row[4])
            senior = float(row[10])
        except (ValueError, TypeError, IndexError):
            continue
        rows.append({
            "tenure_label": period.strip(),
            "interest_rate_general": general,
            "interest_rate_senior": senior,
            "effective_date": effective_date,
        })
    return rows


def extract_iob_rows(text: str) -> list[dict]:
    """Deterministic parser for IOB's retail term-deposit table — see
    module-level comment above for why this bypasses the LLM entirely."""
    table_start = text.find("7–14 Days")
    table_end = text.find("Minimum amount", table_start)
    if table_start == -1 or table_end == -1:
        return []
    table_text = text[table_start:table_end]

    date_match = re.search(r"Revised Rates for Deposits below Rs\. 3 Crore\s*W\.E\.F\.?\s*(\d{2})\.(\d{2})\.(\d{4})", text)
    effective_date = (
        f"{date_match.group(3)}-{date_match.group(2)}-{date_match.group(1)}"
        if date_match else None
    )

    tokens = [t.strip() for t in re.split(f"({_IOB_VALUE_RE.pattern})", table_text) if t.strip()]

    rows = []
    current_period, current_values = None, []
    for token in tokens:
        if _IOB_VALUE_RE.fullmatch(token):
            current_values.append(token)
        else:
            if current_period is not None and len(current_values) == 3:
                rows.append((current_period, current_values))
            current_period, current_values = token, []
    if current_period is not None and len(current_values) == 3:
        rows.append((current_period, current_values))

    out = []
    for period, (_existing, revised, _non_callable) in rows:
        general = float(revised)
        out.append({
            "tenure_label": period,
            "interest_rate_general": general,
            "interest_rate_senior": round(general + _IOB_SENIOR_PREMIUM, 2),
            "effective_date": effective_date,
        })
    return out


# Banks whose real page splits FD rates by deposit amount (a "below Rs. 3
# crore" retail table this project's parsers capture, plus a separate
# bulk/wholesale table at or above that which is NOT captured) — confirmed
# per-bank via each parser's own module comment (grep this file for "3
# Cr"/"3 crore"). Deliberately does NOT include Canara/PNB/SBI: their
# parsers/prompts never mentioned a crore-tier split, so there's no
# confirmed evidence their table has one — guessing a ceiling for them
# would risk inventing a constraint that doesn't actually exist, the same
# discipline this project has followed for every other extracted value.
# Indian Bank is also excluded deliberately: its legacy source text says
# "Min Amount: Rs.1,000 (no maximum)" explicitly.
_DEPOSIT_CEILING_BY_BANK: dict[str, float] = {
    bank_id: 30_000_000.0  # Rs. 3 crore
    for bank_id in (
        "bob", "union_bank", "iob", "hdfc", "icici", "kotak", "axis", "indusind",
    )
}


def extract_rows_for_source(
    source_id: str, text: str, file_path: str, client: Groq | None = None
) -> list[dict]:
    """Shared per-bank parser dispatch — factored out of run() so trend.py
    (Stage 2, rate-history-over-time) can re-run the exact same extraction
    logic against OLDER versioned snapshots, not just the latest fetch.
    Reusing this instead of a second copy of the dispatch chain means a
    future parser fix (e.g. a new bank's column-misalignment bug) only
    ever needs fixing in one place.

    `client` is only required for the generic LLM fallback path — every
    bank currently in config/banks.json has its own deterministic parser
    (SBI moved off the LLM path 2026-08-18, see extract_sbi_rows), so
    `client` is unused today, kept only for a future bank landing here
    before a parser is written for it."""
    if source_id == "sbi":
        return extract_sbi_rows(text)
    elif source_id == "canara":
        return extract_canara_rows(text)
    elif source_id == "pnb":
        return extract_pnb_rows(text)
    elif source_id == "union_bank":
        return extract_union_bank_rows(text)
    elif source_id == "iob":
        return extract_iob_rows(text)
    elif source_id == "bob":
        return extract_bob_rows(text)
    elif source_id == "icici":
        return extract_icici_rows(text)
    elif source_id == "hdfc":
        return extract_hdfc_rows(text)
    elif source_id == "kotak":
        return extract_kotak_rows(text)
    elif source_id == "axis":
        # Takes file_path, not text — see extract_axis_rows's module
        # comment for why (pdfplumber table-aware extraction needs the
        # actual PDF file, not flattened text).
        return extract_axis_rows(file_path)
    elif source_id == "indusind":
        return extract_indusind_rows(text)
    elif source_id == "central_bank":
        return extract_central_bank_rows(text)
    elif source_id == "bom":
        return extract_bom_rows(text)
    elif source_id == "uco":
        return extract_uco_rows(text)
    elif source_id == "punjab_sind":
        return extract_punjab_sind_rows(text)
    else:
        if client is None:
            raise ValueError(
                f"{source_id!r} has no deterministic parser and needs the "
                f"generic LLM extraction path, but no Groq client was supplied"
            )
        return extract_rows(client, text)


def run() -> None:
    con = get_connection()
    client = Groq(api_key=settings.GROQ_API_KEY)

    changed_rows = con.execute(
        "SELECT source_id, fetched_at, file_path FROM fetch_log "
        "WHERE changed = TRUE AND error IS NULL ORDER BY fetched_at"
    ).fetchall()

    if not changed_rows:
        print("No changed, unextracted sources found. Run fetch_and_track.py first.")
        con.close()
        return

    already_done = 0
    for source_id, fetched_at, file_path in changed_rows:
        if already_extracted(con, source_id, fetched_at):
            already_done += 1
            continue

        if Path(file_path).suffix == ".pdf":
            # PDF sources (currently just Axis) don't go through
            # clean_text() — HTML-table-to-text flattening loses column
            # alignment, and pdfplumber's own text extraction has the
            # same issue for a genuine table, so `text` here is only used
            # for the record; the actual table extraction re-opens this
            # same file with pdfplumber's TABLE-aware mode instead (see
            # extract_axis_rows).
            from fetch_and_track import extract_pdf_text
            text = extract_pdf_text(Path(file_path).read_bytes())
        else:
            html = Path(file_path).read_text(encoding="utf-8")
            text = clean_text(html)

        # Need the source's URL for provenance on each record — read it
        # back out of config rather than re-threading it through fetch_log.
        # fetch_log also carries composite source_ids for non-fd_rates URL
        # types (e.g. "bob_home_loan", added when fetch_and_track.py started
        # looping over every url_type per bank — see Stage 3) — those belong
        # to extract_loan_rates.py, not this FD-rate-only script. A plain
        # `next()` used to raise StopIteration the first time one of them
        # was marked changed=True on the same run as this script (confirmed
        # 2026-08-19: real crash on a genuinely-changed *_home_loan fetch,
        # not a hypothetical edge case) — skip gracefully instead.
        config = json.loads((PROJECT_ROOT / "config" / "banks.json").read_text(encoding="utf-8"))
        source_url = next(
            (b["urls"]["fd_rates"] for b in config["banks"]
             if b["id"] == source_id and "fd_rates" in b.get("urls", {})),
            None,
        )
        if source_url is None:
            print(f"{source_id}: no fd_rates URL configured for this source_id — skipping (not an FD-rate source)")
            continue

        try:
            raw_rows = extract_rows_for_source(source_id, text, file_path, client)
        except Exception as exc:
            print(f"{source_id}: extraction call failed — {type(exc).__name__}: {exc}")
            con.execute(
                "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                [source_id, fetched_at, f"LLM call failed: {exc}"],
            )
            continue

        # Replace, don't accumulate: a re-extraction of the same source
        # (e.g. after fetch_and_track.py sees new changed=True content)
        # used to INSERT on top of whatever rows already existed for that
        # bank_id with no cleanup — confirmed as a real bug via
        # compare_rates/eligibility output showing duplicate tenure rows
        # for SBI and Canara (each extracted more than once across
        # sessions). Only delete once we actually have new rows to
        # replace them with — an extraction that yields nothing (e.g. a
        # parser regression) should never wipe out previously-good data.
        if raw_rows:
            con.execute("DELETE FROM fd_rates WHERE bank_id = ?", [source_id])

        accepted, rejected = 0, 0
        for raw in raw_rows:
            try:
                record = FDRateRecord(
                    bank_id=source_id,
                    source_url=source_url,
                    fetched_at=str(fetched_at),
                    deposit_ceiling=_DEPOSIT_CEILING_BY_BANK.get(source_id),
                    **raw,
                )
            except ValidationError as exc:
                rejected += 1
                con.execute(
                    "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                    [source_id, fetched_at, f"Validation failed: {exc}"],
                )
                continue

            con.execute(
                """
                INSERT INTO fd_rates VALUES
                (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    record.bank_id, record.product_type, record.tenure_label,
                    record.tenure_days_min, record.tenure_days_max,
                    record.interest_rate_general, record.interest_rate_senior,
                    record.min_deposit, record.effective_date,
                    record.source_url, record.fetched_at, record.data_source,
                    record.deposit_ceiling, record.deposit_variant,
                ],
            )
            accepted += 1

        con.execute(
            "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
            [source_id, fetched_at, f"{accepted} accepted, {rejected} rejected"],
        )
        print(f"{source_id}: {accepted} rows accepted, {rejected} rejected")

    if already_done == len(changed_rows):
        # Every candidate row had already been extracted — a normal,
        # expected outcome on a day where fetch_and_track.py found no new
        # content anywhere, but previously produced ZERO stdout output for
        # this case (the loop just skipped through silently). That made an
        # empty scheduled-run log indistinguishable from a run that never
        # actually executed — this line exists so an unattended daily log
        # always has something to show for a genuinely successful no-op day.
        print(f"Nothing new to extract — all {already_done} already-processed source(s) up to date.")

    con.close()


if __name__ == "__main__":
    run()
