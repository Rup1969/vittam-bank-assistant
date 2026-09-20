"""Systematic cross-check across all 17 banks and all product types,
built after two real bugs were found manually (an EMI figure that
turned out to be a tenure-assumption mismatch, not a code bug — see
PROJECT_STATUS.md — and a genuine chat-retrieval content gap for Indian
Bank's education loan). Rather than keep checking bank-by-bank by hand,
this automates two independent checks:

1. **EMI/maturity cross-check** — for every bank+product with a real
   rate in fd_rates/loan_rates, recomputes the figure via `calculator.py`
   AND via a second, independently-written implementation of the same
   standard formula (not calling into calculator.py at all), at a fixed
   test amount/tenure. Flags anything beyond a tiny rounding tolerance.
   This is a regression check on the ARITHMETIC, not a judgment call —
   if it ever fails, that's a real bug in one of the two implementations.

2. **Chat-vs-structured cross-check** — for every bank+product with a
   real rate in the structured DB, checks whether the matching FAISS-
   indexed RAG chunk (the content chat actually retrieves from) contains
   ANY percentage-like number at all. This is exactly the failure mode
   found for Indian Bank's education loan: the doc is real, correctly
   retrieved, but was written before the automation-loaded rate existed
   and never mentions a number — so chat has nothing to answer with,
   even though retrieval "worked." A missing chunk entirely (never
   indexed) is a distinct, also-flagged failure mode.

   This check has TWO variants, because bank-scoped chat and General
   Chat genuinely see different amounts of a chunk's text:
   - `chat_bankscoped` — does a rate appear ANYWHERE in the full chunk?
     (what a bank-scoped query sees — `retrieve()` passes the whole
     chunk through, no truncation.)
   - `chat_general` — does a rate appear within the first
     `_MAX_CHUNK_CHARS_GENERAL` (500) characters? (what General Chat
     actually sees — `retrieve_general()` in app.py truncates every
     selected chunk to its first 500 chars before it ever reaches the
     LLM, to keep a 13-bank context within Groq's per-minute token
     budget.) A chunk can genuinely PASS `chat_bankscoped` while FAILING
     `chat_general` if its rate is stated late in the doc — confirmed
     as a real, separate bug for Indian Bank's education loan (rate
     originally at character ~950, past the 500-char cutoff): bank-
     scoped chat answered correctly, General Chat did not, even after
     the content gap itself was fixed — caught only because a user
     compared the two chat modes directly and reported they disagreed.

Deliberately does NOT call Groq/the LLM for this check — burning real
API calls across 17 banks x 3 products x N phrasings would be slow,
rate-limit-prone, and non-deterministic to re-run. Checking the
retrieved chunk's raw text directly is faster, free, deterministic, and
tests the actual root cause (content, not phrasing) that both bugs
found this session trace back to.

Run standalone:

    python automation/verify_all.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from db import get_connection  # noqa: E402
from gap_notes import to_faiss_code  # noqa: E402

TEST_AMOUNT = 500000
TEST_LOAN_YEARS = 7
TEST_FD_TENURE_DAYS = 365  # "1 year"
RATE_TOLERANCE = 0.01  # rupees, for EMI/maturity cross-check
INDIAN_BANK_ID = "indian_bank"

# doc_id naming in indexes/chunks.json (legacy, Indian Bank) and
# indexes/other_banks_chunks.json (everyone else) is NOT one consistent
# pattern — confirmed by inspecting both files directly, not guessed:
#   - Most banks: "{bank_id}_loan_home" and "{bank_id}_loan_education"
#     (word order reversed vs the DB's "home_loan"/"education_loan").
#   - SBI alone: "sbi_home_loan" (NOT reversed — an inconsistency from
#     whenever that doc was originally authored, predating the others).
#   - central_bank's docs are all prefixed "centralbank" (no underscore)
#     and punjab_sind's are all prefixed "psb" — neither matches the
#     DB bank_id or the FAISS "bank" tag used for filtering.
#   - Indian Bank (the legacy single-bank pool, no doc_id prefix at
#     all): FD rates live under "rates_deposit_structured", not
#     "fd_rates" — a completely different doc_id, not just a missing
#     prefix. Home/education loan docs there ARE "loan_home"/
#     "loan_education", same suffix as everyone else.
# An earlier version of this script assumed a single uniform pattern
# and produced a wave of false-positive "not indexed" failures for
# home_loan across nearly every bank — caught only by manually
# inspecting a few banks' real doc_id lists before trusting the report.
_DOC_ID_PREFIX_OVERRIDES = {"central_bank": "centralbank", "punjab_sind": "psb"}

_DOC_ID_SUFFIX = {
    "fd": "fd_rates",
    "home_loan": "loan_home",
    "education_loan": "loan_education",
    "vehicle_loan": "loan_vehicle",
}
_DOC_ID_SUFFIX_OVERRIDES = {
    ("sbi", "home_loan"): "home_loan",
}
_INDIAN_BANK_DOC_ID = {
    "fd": "rates_deposit_structured",
    "home_loan": "loan_home",
    "education_loan": "loan_education",
    "vehicle_loan": "loan_vehicle",
}

_RATE_NUMBER_RE = re.compile(r"\b\d{1,2}(?:\.\d{1,3})?\s*%")

# Must match app.py's retrieve_general()'s truncation exactly — this
# check has no value if it drifts from the real cutoff.
_MAX_CHUNK_CHARS_GENERAL = 500


def _independent_fd_maturity(amount: float, rate: float, days: int) -> float:
    """A second, separately-written compound-interest implementation —
    deliberately not importing calculator.py's formula, so this is a
    real cross-check, not the same code checking itself."""
    years = days / 365.0
    quarters = years * 4
    return amount * (1 + (rate / 100) / 4) ** quarters


def _independent_emi(amount: float, annual_rate: float, years: float) -> float:
    """A second, separately-written reducing-balance EMI implementation."""
    months = round(years * 12)
    if months <= 0:
        months = 1
    m_rate = annual_rate / 1200.0
    if m_rate == 0:
        return amount / months
    growth = (1.0 + m_rate) ** months
    return amount * m_rate * growth / (growth - 1.0)


def _load_faiss_chunks() -> dict[str, list[dict]]:
    """Both chunk pools, loaded once — {'legacy': [...], 'other': [...]}."""
    pools = {}
    legacy_path = PROJECT_ROOT / "indexes" / "chunks.json"
    other_path = PROJECT_ROOT / "indexes" / "other_banks_chunks.json"
    pools["legacy"] = json.loads(legacy_path.read_text(encoding="utf-8")) if legacy_path.exists() else []
    pools["other"] = json.loads(other_path.read_text(encoding="utf-8")) if other_path.exists() else []
    return pools


def _find_chunk_text(pools: dict, bank_id: str, product: str) -> str | None:
    """Returns the concatenated raw text of every chunk matching this
    bank+product's doc_id, or None if no matching chunk is indexed at
    all (a distinct failure mode from 'indexed but no rate stated')."""
    if bank_id == INDIAN_BANK_ID:
        doc_id = _INDIAN_BANK_DOC_ID[product]
        matches = [c for c in pools["legacy"] if c.get("doc_id") == doc_id]
    else:
        faiss_bank = to_faiss_code(bank_id)
        prefix = _DOC_ID_PREFIX_OVERRIDES.get(bank_id, bank_id)
        suffix = _DOC_ID_SUFFIX_OVERRIDES.get((bank_id, product), _DOC_ID_SUFFIX[product])
        doc_id = f"{prefix}_{suffix}"
        matches = [
            c for c in pools["other"]
            if c.get("doc_id") == doc_id and c.get("bank", "").upper() == faiss_bank
        ]
    if not matches:
        return None
    return " ".join(c.get("text", "") for c in matches)


def check_calculations() -> list[dict]:
    """Check 1: EMI/maturity arithmetic, calculator.py vs. an
    independently-written second implementation, at a fixed test
    amount/tenure per product."""
    from calculator import fd_maturity, loan_emi
    from compare_rates import compare_for_tenure

    results = []

    fd_rows = compare_for_tenure(TEST_FD_TENURE_DAYS, "general")
    for row in fd_rows:
        bank_id = row["bank_id"]
        via_code = fd_maturity(bank_id, "1 year", TEST_AMOUNT, "general")
        if via_code is None:
            results.append({
                "bank_id": bank_id, "product": "fd", "check": "calculation",
                "status": "FAIL", "detail": "calculator.fd_maturity() returned None despite a real rate row existing",
            })
            continue
        independent = _independent_fd_maturity(TEST_AMOUNT, row["rate"], TEST_FD_TENURE_DAYS)
        diff = abs(via_code["maturity_value"] - independent)
        status = "PASS" if diff <= RATE_TOLERANCE else "FAIL"
        results.append({
            "bank_id": bank_id, "product": "fd", "check": "calculation", "status": status,
            "detail": f"rate={row['rate']}% code=Rs.{via_code['maturity_value']:,.2f} "
                      f"independent=Rs.{independent:,.2f} diff=Rs.{diff:.4f}",
        })

    con = get_connection()
    loan_bank_types = con.execute(
        "SELECT DISTINCT bank_id, loan_type FROM loan_rates ORDER BY bank_id, loan_type"
    ).fetchall()
    con.close()

    for bank_id, loan_type in loan_bank_types:
        via_code = loan_emi(bank_id, loan_type, TEST_AMOUNT, TEST_LOAN_YEARS)
        if via_code is None:
            results.append({
                "bank_id": bank_id, "product": loan_type, "check": "calculation",
                "status": "FAIL", "detail": "calculator.loan_emi() returned None despite a real rate row existing",
            })
            continue
        independent = _independent_emi(TEST_AMOUNT, via_code["rate"], TEST_LOAN_YEARS)
        diff = abs(via_code["emi"] - independent)
        status = "PASS" if diff <= RATE_TOLERANCE else "FAIL"
        results.append({
            "bank_id": bank_id, "product": loan_type, "check": "calculation", "status": status,
            "detail": f"rate={via_code['rate']}% code=Rs.{via_code['emi']:,.2f}/mo "
                      f"independent=Rs.{independent:,.2f}/mo diff=Rs.{diff:.4f}",
        })

    return results


def check_chat_retrieval() -> list[dict]:
    """Check 2: for every bank+product with a real structured rate, does
    the matching indexed RAG chunk contain a rate number — checked both
    ways chat actually consumes it (see module docstring): anywhere in
    the full chunk (bank-scoped) and within the first 500 chars
    (General Chat's real truncation)."""
    pools = _load_faiss_chunks()
    results = []

    con = get_connection()
    fd_banks = [r[0] for r in con.execute("SELECT DISTINCT bank_id FROM fd_rates").fetchall()]
    loan_bank_types = con.execute(
        "SELECT DISTINCT bank_id, loan_type FROM loan_rates ORDER BY bank_id, loan_type"
    ).fetchall()
    con.close()

    combos = [(b, "fd") for b in fd_banks] + [(b, lt) for b, lt in loan_bank_types]

    for bank_id, product in combos:
        text = _find_chunk_text(pools, bank_id, product)
        if text is None:
            for check_name in ("chat_bankscoped", "chat_general"):
                results.append({
                    "bank_id": bank_id, "product": product, "check": check_name,
                    "status": "FAIL", "detail": "no matching chunk indexed at all for this bank+product",
                })
            continue

        match = _RATE_NUMBER_RE.search(text)
        has_rate_anywhere = bool(match)
        results.append({
            "bank_id": bank_id, "product": product, "check": "chat_bankscoped",
            "status": "PASS" if has_rate_anywhere else "FAIL",
            "detail": (
                "indexed content contains at least one rate figure" if has_rate_anywhere
                else "indexed content exists but states NO numeric rate — chat has nothing to answer with"
            ),
        })

        has_rate_within_general = bool(match) and match.start() < _MAX_CHUNK_CHARS_GENERAL
        results.append({
            "bank_id": bank_id, "product": product, "check": "chat_general",
            "status": "PASS" if has_rate_within_general else "FAIL",
            "detail": (
                f"rate figure appears at char {match.start()}, within General Chat's "
                f"{_MAX_CHUNK_CHARS_GENERAL}-char truncation" if has_rate_within_general
                else (
                    f"rate figure exists but at char {match.start()}, PAST General Chat's "
                    f"{_MAX_CHUNK_CHARS_GENERAL}-char truncation — bank-scoped chat can answer, "
                    f"General Chat cannot" if has_rate_anywhere
                    else "no rate figure anywhere in the chunk (same root cause as chat_bankscoped FAIL)"
                )
            ),
        })

    return results


def run_all() -> list[dict]:
    return check_calculations() + check_chat_retrieval()


def print_report(results: list[dict]) -> None:
    by_key: dict[tuple, dict] = {}
    for r in results:
        by_key[(r["bank_id"], r["product"], r["check"])] = r

    banks = sorted(set(r["bank_id"] for r in results))
    products = ["fd", "home_loan", "education_loan", "vehicle_loan"]
    checks = ["calculation", "chat_bankscoped", "chat_general"]

    print("=" * 110)
    print(f"Verification report — {len(banks)} banks x {len(products)} products x {len(checks)} checks")
    print("=" * 110)
    header = f"{'Bank':<16}{'Product':<16}{'Calc':<8}{'BankChat':<10}{'GenChat':<10}Notes"
    print(header)
    print("-" * 110)
    for bank_id in banks:
        for product in products:
            calc = by_key.get((bank_id, product, "calculation"))
            bankscoped = by_key.get((bank_id, product, "chat_bankscoped"))
            general = by_key.get((bank_id, product, "chat_general"))
            if calc is None and bankscoped is None and general is None:
                continue
            calc_s = calc["status"] if calc else "  —  "
            bank_s = bankscoped["status"] if bankscoped else "  —  "
            gen_s = general["status"] if general else "  —  "
            notes = []
            if calc and calc["status"] == "FAIL":
                notes.append(f"calc: {calc['detail']}")
            if bankscoped and bankscoped["status"] == "FAIL":
                notes.append(f"bank-chat: {bankscoped['detail']}")
            if general and general["status"] == "FAIL":
                notes.append(f"general-chat: {general['detail']}")
            print(f"{bank_id:<16}{product:<16}{calc_s:<8}{bank_s:<10}{gen_s:<10}{'; '.join(notes)}")

    total = len(results)
    failed = [r for r in results if r["status"] == "FAIL"]
    print("-" * 110)
    print(f"TOTAL: {total} checks, {len(failed)} FAIL, {total - len(failed)} PASS")
    if failed:
        print("\nFAILURES:")
        for r in failed:
            print(f"  [{r['check']}] {r['bank_id']} / {r['product']}: {r['detail']}")


if __name__ == "__main__":
    results = run_all()
    print_report(results)
    # Non-zero exit on any FAIL so a CI job running this goes red.
    sys.exit(1 if any(r["status"] == "FAIL" for r in results) else 0)
