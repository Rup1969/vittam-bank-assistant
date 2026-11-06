"""Systematic retrieval-competition check — built 2026-08-24 after finding
4 real bugs one at a time while auditing Kisan Credit Card (KCC) coverage
(PROJECT_STATUS.md, KCC section): Indian Bank, IndusInd, Bank of
Maharashtra, and Punjab & Sind Bank all had real, indexed content that
correctly mentioned the topic being asked about, but chat still failed to
surface it — not because the content was missing (verify_all.py's job),
but because OTHER banks' docs out-scored theirs for a natural-language
question, or because General Chat's round-robin selection ran out of
room. This is a genuinely different failure class from verify_all.py's
"does the chunk contain the right content" check, and from the earlier
General-Chat-truncation bug (§16f) — it's about RANKING, not content
presence or chunk position.

This script replicates app.py's `retrieve()` and `retrieve_general()`
logic exactly (search_k formula, _MIN_RELEVANCE floor, round-robin
selection) against the REAL FAISS indexes and the REAL embedding model —
not a re-derivation, not a guess — so its verdicts match what the live
app actually does. It does NOT call Groq; like verify_all.py, checking
retrieval directly is deterministic, fast, and tests the actual
mechanism the two real bugs above trace back to.

Two independent failure modes are checked per (bank, topic, query):

1. **bank_scoped** — does bank_filter=<bank> actually retrieve the
   bank's own target doc? Real gotcha confirmed while building this:
   `retrieve()`'s bank_filter is applied AFTER a GLOBAL top-`search_k`
   FAISS search across every bank's chunks, not a per-bank search filtered
   first — so a bank's own doc can lose a bank-scoped query entirely if
   enough OTHER banks' docs outrank it globally for that phrasing (this
   is exactly what happened to IndusInd: its real doc never entered the
   global top-16 for a KCC question, so bank-scoped search returned
   nothing at all for IndusInd on that topic).

2. **general_chat** — does the bank's target doc win its own round-robin
   slot in `retrieve_general()`? A bank can genuinely have great content
   and still lose here for a purely STRUCTURAL reason unrelated to its
   own doc quality: `retrieve_general()`'s `k` (currently 13, hardcoded
   at the function signature — see `_GENERAL_CHAT_K` below, which MUST be
   kept in sync with `app.py`) was set when there were 13 total banks
   (see app.py's own code comment: "k=13 matches the current total bank
   count"). The roster has since grown to `_REAL_BANK_COUNT` banks — so
   any query broadly relevant to more than 13 banks GUARANTEES at least
   one bank gets excluded, no matter how well-written every doc is. This
   script flags that distinct condition separately (`capacity_ceiling`)
   from a bank's own weak score (`below_cutoff`), since only the second
   is fixable by editing a doc — the first needs `_GENERAL_CHAT_K` raised
   to match the real roster (a code change, not a content one; not made
   by this script, flagged for a human decision since it changes context
   size/token cost for every General Chat query, not just this topic).

Add a new topic to `TOPICS` below (queries + a `bank_id -> doc_id`
map) any time a new "does every bank cover X" question is worth
tracking — this is meant to accumulate over time, the same way
verify_all.py's product coverage grew from fd-only to fd+home+education.

Run standalone:

    python automation/verify_retrieval.py
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

from sentence_transformers import SentenceTransformer  # noqa: E402
import faiss  # noqa: E402

from db import get_connection  # noqa: E402
from tenure_utils import tenure_covers  # noqa: E402

# Must match app.py's real constants exactly — this check has no value if
# it drifts from what the live app actually does (same precedent as
# verify_all.py's _MAX_CHUNK_CHARS_GENERAL comment).
TOP_K = 4
_MIN_RELEVANCE = 0.35
_GENERAL_CHAT_K = 17  # app.py's retrieve_general(..., k=17) default (raised 2026-08-24 from 13, see PROJECT_STATUS.md §23)

# settings.OTHER_BANKS (16) + Indian Bank = the real current roster.
# Kept as a separate constant (not imported) so this script visibly
# flags drift if _GENERAL_CHAT_K is ever fixed without updating this,
# or vice versa.
_REAL_BANK_COUNT = 17

BANKS = {
    "indian_bank": "INDIAN_BANK", "sbi": "SBI", "bob": "BOB", "canara": "CANARA",
    "pnb": "PNB", "boi": "BOI", "union_bank": "UNION_BANK", "iob": "IOB",
    "central_bank": "CENTRAL_BANK", "punjab_sind": "PUNJAB_SIND", "bom": "BOM", "uco": "UCO",
    "icici": "ICICI", "hdfc": "HDFC", "kotak": "KOTAK_MAHINDRA", "axis": "AXIS",
    "indusind": "INDUSIND",
}

# doc_id naming in indexes/chunks.json (legacy, Indian Bank) and
# indexes/other_banks_chunks.json (everyone else) is NOT one consistent
# pattern — same real inconsistency verify_all.py already had to solve
# (see that file's own comment on this): central_bank's docs are
# prefixed "centralbank" (no underscore), punjab_sind's are "psb", and
# Indian Bank's legacy pool has no bank prefix on doc_id at all.
_DOC_ID_PREFIX_OVERRIDES = {"central_bank": "centralbank", "punjab_sind": "psb"}


def _std_target_doc_map(suffix: str, overrides: dict[str, str | None] | None = None) -> dict[str, str]:
    """Builds a `bank_id -> doc_id` map for the standard `{prefix}_{suffix}`
    naming convention, applying the same prefix exceptions as above.
    `overrides` replaces specific entries (e.g. SBI's real doc_id is
    `sbi_home_loan`, not `sbi_loan_home`) — pass `None` for a bank that
    genuinely has no doc for this topic at all (a category-1 "never
    built" gap, not a retrieval question) so `run_topic()` skips it
    instead of reporting a misleading FAIL."""
    mapping = {}
    for bank_id in BANKS:
        if bank_id == "indian_bank":
            candidate = suffix
        else:
            prefix = _DOC_ID_PREFIX_OVERRIDES.get(bank_id, bank_id)
            candidate = f"{prefix}_{suffix}"
        mapping[bank_id] = candidate
    for bank_id, doc_id in (overrides or {}).items():
        if doc_id is None:
            mapping.pop(bank_id, None)
        else:
            mapping[bank_id] = doc_id
    return mapping


TOPICS = {
    "kcc": {
        "queries": [
            "Does this bank offer a Kisan Credit Card scheme?",
            "What is the Kisan Credit Card (KCC) interest rate and loan limit?",
        ],
        "target_doc_by_bank": _std_target_doc_map(
            "loan_agriculture", overrides={"sbi": "sbi_loan_kcc"}
        ),
    },
    # 2026-08-24: broader sweep across every other product category with
    # a near-universal doc, added after the KCC audit's 4 retrieval bugs
    # raised the obvious follow-up question — is this pattern hiding
    # elsewhere? One natural "does this bank offer X" query per category,
    # matching the exact phrasing style that caught the KCC bugs.
    "gold_loan": {
        "queries": ["Does this bank offer a gold loan? What is the gold loan interest rate?"],
        "target_doc_by_bank": _std_target_doc_map(
            "loan_gold", overrides={"indian_bank": "loan_jewel"}
        ),
    },
    "vehicle_loan": {
        "queries": ["What is the car/vehicle loan interest rate at this bank?"],
        # HDFC has no vehicle-loan doc at all — a real, already-documented
        # category-1 gap (CloudFront-blocked every attempt, see
        # project_bank_automation_pipeline memory), not tested here.
        "target_doc_by_bank": _std_target_doc_map("loan_vehicle", overrides={"hdfc": None}),
    },
    "msme_loan": {
        "queries": ["Does this bank offer MSME or business loans?"],
        "target_doc_by_bank": _std_target_doc_map("loan_msme"),
    },
    "personal_loan": {
        "queries": ["What is the personal loan interest rate at this bank?"],
        "target_doc_by_bank": _std_target_doc_map("loan_personal"),
    },
    "mortgage_loan": {
        "queries": ["Does this bank offer a loan against property or mortgage loan?"],
        # Same already-documented HDFC gap as vehicle_loan.
        "target_doc_by_bank": _std_target_doc_map("loan_mortgage", overrides={"hdfc": None}),
    },
    "home_loan_rag": {
        "queries": ["What is the home loan interest rate at this bank?"],
        # RAG-doc-level check, distinct from verify_all.py's structured
        # fd_rates/loan_rates check — this tests whether the DOCUMENT
        # itself is retrievable at all, not whether a rate number exists
        # inside it.
        "target_doc_by_bank": _std_target_doc_map(
            "loan_home", overrides={"sbi": "sbi_home_loan"}
        ),
    },
    "education_loan_rag": {
        "queries": ["What is the education loan interest rate at this bank?"],
        # IndusInd has no education-loan product at all — a real,
        # already-documented category (confirmed via its own nav, not a
        # fetch failure; see project_multibank_rbi_rag memory).
        "target_doc_by_bank": _std_target_doc_map("loan_education", overrides={"indusind": None}),
    },
}


# ───────────────── FD-rate lookup (added 2026-11-06) ────────────────────
#
# The TOPICS above all check ONE thing: is the bank's target DOCUMENT
# retrieved at all. The verification-sheet investigation found a
# genuinely different failure mode that check can't see: the right
# document can be beside the point if a DIFFERENT, unsynced document (a
# forgotten "*_rates_deposit_structured.txt" mirror) outranks it, or the
# right document's own internal ordering surfaces the wrong tenure band
# — the content that's actually retrieved states a rate figure that
# simply isn't the one Compare Rates would give for that tenure. Real
# cases found this way: SBI's and BOB's stale structured-doc competitor
# winning retrieval for 1yr/3yr FD questions (one missing the row
# entirely, one plain wrong-topic); HDFC's "common tenures first"
# reordering promoting the wrong neighboring band for 3yr/5yr. Scoped to
# every bank with an AUTO block (the ones this project actively keeps
# synced) — a bank with no AUTO block has no commitment to be current by
# construction, so a miss there isn't this check's business.
#
# Ground truth is tenure_covers() — automation/tenure_utils.py — the
# EXACT function compare_for_tenure() uses for Compare Rates itself, with
# the same tie-break (the higher general rate wins when more than one
# band's range includes the target day). This check has no independent
# opinion of its own about what's "correct"; it only asks whether
# retrieval agrees with what the rest of the app already shows.
FD_LOOKUP_TENURES = {"1-year": (1, 365), "3-year": (3, 1095), "5-year": (5, 1825)}
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_AUTO_START_LINE = "<!--AUTO:START-->"
# A bare "N year(s)"/"N-year" mention in the preview text -- used the same
# way check_preview_figures() (verify_coverage.py) uses its own 1-year-
# only version. BUG FOUND while building this check (2026-11-06, testing
# against the pre-fix state as instructed): a first version compared the
# expected rate's VALUE against every percentage anywhere in the 500-char
# preview, with no regard for position -- which produced a false PASS
# for HDFC's pre-fix 3-year case, because a DIFFERENT, adjacent band
# ("2 Years 1 day to < 2 Year 11 Months") coincidentally shares the exact
# same rate (6.55%/7.05%) as the real 3-year band, and that different
# band's own figure happened to sit inside the truncated preview even
# though the real 3-year band's figure did not. Checking for the figure
# NEAR an explicit "N year" mention instead -- not just present anywhere
# -- closes that gap, the same fix already applied to
# verify_coverage.py's check_preview_figures().
_TENURE_MENTION_RE = {
    n: re.compile(rf"\b{n}[\s-]*years?\b", re.IGNORECASE)
    for n, _days in FD_LOOKUP_TENURES.values()
}
_TENURE_PROXIMITY_CHARS = 60
# app.py's _MAX_CHUNK_CHARS_GENERAL -- General Chat's own chunk-0 preview
# budget. Checked against THIS path, not bank-scoped retrieve(), because
# bank-scoped chat never truncates a chunk at all (confirmed by reading
# app.py directly: the truncation lives only inside retrieve_general()) --
# so a bank-scoped full chunk always contains every band's real figure
# somewhere in its text regardless of ordering, and could never have
# caught HDFC's actual bug (a reordering issue, not a missing-content
# one). General Chat's truncated preview is where that bug was real and
# measurable, and where SBI's/BOB's wrong-document problem is visible too.
_GENERAL_CHAT_PREVIEW_CHARS = 500


def _banks_with_auto_block() -> dict[str, str]:
    """bank_id -> FAISS 'bank' tag, for every *_fd_rates.txt under
    data/raw/ that has an AUTO block. Scans the real files rather than
    hardcoding a list, so this check automatically covers the next bank
    added to the rollout without needing its own update."""
    found = {}
    for fpath in sorted((PROJECT_ROOT / "data" / "raw").rglob("*_fd_rates.txt")):
        text = fpath.read_text(encoding="utf-8")
        if not any(l.strip() == _AUTO_START_LINE for l in text.splitlines()):
            continue
        bank_id = fpath.parent.name
        if bank_id in BANKS:
            found[bank_id] = BANKS[bank_id]
    return found


def check_fd_rate_shortcut() -> list[dict]:
    """PRIMARY FD-rate check, as of 2026-11-06: does the deterministic
    fd_rate_shortcut.py chat shortcut (built the same day, see that
    module's own docstring) state the SAME rate Compare Rates shows, for
    every AUTO-block bank's 1/3/5-year tenure? This is what chat actually
    answers with now for this question shape -- RAG retrieval is bypassed
    entirely for it -- so this is the check that matters; the
    retrieval-based one below is kept only as an early-warning WARN for
    the (now much lower-stakes) case where the shortcut itself doesn't
    fire and chat falls through to RAG.

    Verifies the shortcut's OWN rendered text, not just that it imports
    the same function Compare Rates uses -- a transcription or
    field-access bug in render_fd_rate_answer() could still show the
    wrong number even while calling the right function, and this would
    catch that."""
    from fd_rate_shortcut import parse_fd_rate_intent, render_fd_rate_answer

    con = get_connection()
    results = []
    for bank_id in _banks_with_auto_block():
        rows = con.execute(
            "SELECT tenure_label, interest_rate_general, interest_rate_senior FROM fd_rates "
            "WHERE bank_id = ? AND product_type = 'fixed_deposit' AND deposit_variant = 'retail'",
            [bank_id],
        ).fetchall()

        for label_name, (n, days) in FD_LOOKUP_TENURES.items():
            matches = [r for r in rows if tenure_covers(r[0], days)]
            if not matches:
                results.append({
                    "bank_id": bank_id, "tenure": label_name, "check": "fd_rate_shortcut",
                    "status": "SKIP",
                    "detail": f"no retail band covers day {days} for this bank -- nothing to check",
                })
                continue
            best = max(matches, key=lambda r: r[1])
            expected_g, expected_s = best[1], best[2]

            intent = parse_fd_rate_intent(f"What is the {n} year FD rate?", context_bank_id=bank_id)
            if intent is None:
                results.append({
                    "bank_id": bank_id, "tenure": label_name, "check": "fd_rate_shortcut",
                    "status": "FAIL",
                    "detail": f"shortcut did not recognize its own canonical phrasing for {n} year -- parse_fd_rate_intent returned None",
                })
                continue
            answer = render_fd_rate_answer(intent)
            found = {round(float(m.group(1)), 2) for m in _PERCENT_RE.finditer(answer)}
            expected = {round(v, 2) for v in (expected_g, expected_s) if v is not None}
            passed = expected.issubset(found)
            results.append({
                "bank_id": bank_id, "tenure": label_name, "check": "fd_rate_shortcut",
                "status": "PASS" if passed else "FAIL",
                "detail": (
                    f"shortcut states {expected_g:g}%/{expected_s:g}% ({best[0]!r}), matching Compare Rates"
                    if passed else
                    f"shortcut answer does not state Compare Rates' figure for {best[0]!r} "
                    f"({expected_g:g}%/{expected_s:g}%) -- shortcut said: {answer!r}"
                ),
            })
    return results


def check_fd_rate_retrieval_preview() -> list[dict]:
    """SECONDARY, WARN-only as of 2026-11-06 (was the primary FAIL-counted
    check before fd_rate_shortcut.py existed): does RAG retrieval's top
    chunk, truncated to General Chat's real 500-char preview, still state
    the right figure near an explicit tenure mention? Now that
    check_fd_rate_shortcut() above covers what chat actually answers with
    for this question shape, a FAIL here just means "if the shortcut
    doesn't fire, the RAG fallback would also be wrong" -- worth knowing,
    not worth blocking on, since the shortcut is what real traffic hits.

    The TOPICS above all check ONE thing: is the bank's target DOCUMENT
    retrieved at all. The verification-sheet investigation found a
    genuinely different failure mode that check can't see: the right
    document can be beside the point if a DIFFERENT, unsynced document (a
    forgotten "*_rates_deposit_structured.txt" mirror, or an unrelated
    "*_loan_against_deposit.txt") outranks it, or the right document's own
    internal ordering surfaces the wrong tenure band.

    Ground truth is tenure_covers() — automation/tenure_utils.py — the
    EXACT function compare_for_tenure() uses for Compare Rates itself,
    with the same tie-break (the higher general rate wins when more than
    one band's range includes the target day)."""
    con = get_connection()
    model, legacy_index, legacy_chunks, other_index, other_chunks = _load_pools()

    results = []
    for bank_id, faiss_code in _banks_with_auto_block().items():
        rows = con.execute(
            "SELECT tenure_label, interest_rate_general, interest_rate_senior FROM fd_rates "
            "WHERE bank_id = ? AND product_type = 'fixed_deposit' AND deposit_variant = 'retail'",
            [bank_id],
        ).fetchall()

        for label_name, (n, days) in FD_LOOKUP_TENURES.items():
            matches = [r for r in rows if tenure_covers(r[0], days)]
            if not matches:
                results.append({
                    "bank_id": bank_id, "tenure": label_name, "check": "fd_rate_retrieval_preview",
                    "status": "SKIP",
                    "detail": f"no retail band covers day {days} for this bank -- nothing to check",
                })
                continue
            best = max(matches, key=lambda r: r[1])
            expected = {round(v, 2) for v in (best[1], best[2]) if v is not None}

            query = f"What is the {label_name} FD rate?"
            gen_selected, _ = _retrieve_general(model, query, legacy_index, legacy_chunks, other_index, other_chunks)
            bank_chunks = [c for c in gen_selected if c.get("bank", "INDIAN_BANK").upper() == faiss_code.upper()]
            if not bank_chunks:
                results.append({
                    "bank_id": bank_id, "tenure": label_name, "check": "fd_rate_retrieval_preview",
                    "status": "WARN",
                    "detail": f"bank excluded from General Chat's round-robin entirely for {query!r}",
                })
                continue

            chunk = bank_chunks[0]
            preview = chunk["text"][:_GENERAL_CHAT_PREVIEW_CHARS]
            passed = False
            for mention in _TENURE_MENTION_RE[n].finditer(preview):
                window = preview[mention.end(): mention.end() + _TENURE_PROXIMITY_CHARS]
                nearby = {round(float(m.group(1)), 2) for m in _PERCENT_RE.finditer(window)}
                if expected & nearby:
                    passed = True
                    break
            results.append({
                "bank_id": bank_id, "tenure": label_name, "check": "fd_rate_retrieval_preview",
                "status": "OK" if passed else "WARN",
                "detail": (
                    f"top chunk ({chunk['doc_id']!r}) preview states Compare Rates' own figure "
                    f"({best[0]!r}: {best[1]:g}%/{best[2]:g}%) near an explicit {n}-year mention"
                    if passed else
                    f"top chunk ({chunk['doc_id']!r}) preview does NOT state Compare Rates' figure for "
                    f"{best[0]!r} ({best[1]:g}%/{best[2]:g}%) near an explicit {n}-year mention -- "
                    f"RAG fallback would answer with something else (preview ends: ...{preview[-80:]!r})"
                ),
            })
    return results


def _load_pools():
    model = SentenceTransformer("all-MiniLM-L6-v2")
    legacy_index = faiss.read_index(str(PROJECT_ROOT / "indexes" / "indian_bank.index"))
    legacy_chunks = json.loads((PROJECT_ROOT / "indexes" / "chunks.json").read_text(encoding="utf-8"))
    other_index = faiss.read_index(str(PROJECT_ROOT / "indexes" / "other_banks.index"))
    other_chunks = json.loads((PROJECT_ROOT / "indexes" / "other_banks_chunks.json").read_text(encoding="utf-8"))
    return model, legacy_index, legacy_chunks, other_index, other_chunks


def _retrieve(model, query, index, chunks, k=TOP_K, bank_filter=None):
    q_vec = model.encode([query], convert_to_numpy=True)
    faiss.normalize_L2(q_vec)
    search_k = index.ntotal if bank_filter else max(k * 4, k + 8)
    scores, indices = index.search(q_vec, min(search_k, index.ntotal))
    out = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        chunk = chunks[idx]
        if bank_filter and chunk.get("bank", "INDIAN_BANK").upper() != bank_filter.upper():
            continue
        out.append({**chunk, "score": float(score)})
    return out[:k]


_CANDIDATE_WINDOW = 500  # must match app.py's retrieve_general() — see its comment


def _retrieve_general(model, query, legacy_index, legacy_chunks, other_index, other_chunks, k=_GENERAL_CHAT_K):
    candidates = []
    for index, chunks in [(legacy_index, legacy_chunks), (other_index, other_chunks)]:
        candidates.extend(_retrieve(model, query, index, chunks, k=_CANDIDATE_WINDOW))
    candidates = [c for c in candidates if c["score"] >= _MIN_RELEVANCE]
    candidates.sort(key=lambda c: c["score"], reverse=True)

    by_bank: dict[str, list[dict]] = {}
    for c in candidates:
        by_bank.setdefault(c.get("bank", "INDIAN_BANK"), []).append(c)

    selected, round_idx = [], 0
    while len(selected) < k and any(len(v) > round_idx for v in by_bank.values()):
        round_candidates = sorted(
            (v[round_idx] for v in by_bank.values() if len(v) > round_idx),
            key=lambda c: c["score"], reverse=True,
        )
        for c in round_candidates:
            if len(selected) >= k:
                break
            selected.append(c)
        round_idx += 1

    # How many distinct banks had a real (>=_MIN_RELEVANCE) round-0
    # candidate at all — this is the count that determines whether
    # exclusion was STRUCTURALLY GUARANTEED regardless of doc quality.
    round0_bank_count = len({c.get("bank", "INDIAN_BANK") for v in by_bank.values() for c in v[:1]})

    return selected, round0_bank_count


def run_topic(topic_name: str) -> list[dict]:
    topic = TOPICS[topic_name]
    model, legacy_index, legacy_chunks, other_index, other_chunks = _load_pools()
    results = []

    for query in topic["queries"]:
        # Capacity check: how many banks have ANY relevant content for
        # this query at all? If more than _GENERAL_CHAT_K, General Chat
        # WILL exclude someone no matter what — flagged once per query,
        # not per bank, since it's a property of the query/topic, not
        # any single bank's content.
        _, round0_bank_count = _retrieve_general(
            model, query, legacy_index, legacy_chunks, other_index, other_chunks
        )
        if round0_bank_count > _GENERAL_CHAT_K:
            results.append({
                "topic": topic_name, "bank_id": "*", "query": query,
                "check": "general_chat_capacity", "status": "FAIL",
                "detail": (
                    f"{round0_bank_count} banks have relevant content for this query, "
                    f"but General Chat's k={_GENERAL_CHAT_K} can only ever show "
                    f"{_GENERAL_CHAT_K} - at least {round0_bank_count - _GENERAL_CHAT_K} "
                    f"bank(s) are guaranteed excluded regardless of doc quality. "
                    f"Real bank roster is {_REAL_BANK_COUNT}; k has not been raised to match."
                ),
            })

        for bank_id, faiss_code in BANKS.items():
            target = topic["target_doc_by_bank"].get(bank_id)
            if target is None:
                # No doc exists for this bank+topic at all — a category-1
                # gap (verify_all.py / manual audit territory), not a
                # retrieval-competition question. Skip rather than fail,
                # so this script's FAILs stay meaningful (real content
                # that isn't surfacing), not noise from known non-builds.
                continue

            index, chunks = (legacy_index, legacy_chunks) if bank_id == "indian_bank" else (other_index, other_chunks)
            bs = _retrieve(model, query, index, chunks, k=TOP_K, bank_filter=faiss_code)
            bs_ids = [r["doc_id"] for r in bs]
            bs_pass = target in bs_ids
            results.append({
                "topic": topic_name, "bank_id": bank_id, "query": query,
                "check": "bank_scoped", "status": "PASS" if bs_pass else "FAIL",
                "detail": (
                    f"target doc ranked #{bs_ids.index(target) + 1} of {len(bs_ids)}" if bs_pass
                    else f"target doc '{target}' not retrieved at all - top4 were {bs_ids or '(none - bank has no candidate in the global top-search-window for this query)'}"
                ),
            })

            gen_selected, _ = _retrieve_general(
                model, query, legacy_index, legacy_chunks, other_index, other_chunks
            )
            # BUG FIX 2026-08-26: a plain `{bank: doc_id for c in gen_selected}`
            # dict comprehension silently keeps only the LAST occurrence of a
            # repeated key — but round-robin can legitimately give one bank
            # TWO slots when other banks run out of candidates (e.g. only 13
            # of 17 banks have any relevant content, so round_idx advances to
            # 1 for banks that still have a second candidate, filling the
            # remaining slots). When that happens, a bank's CORRECT round-0
            # pick was silently overwritten by its round-1 pick in the old
            # single-doc dict, producing a false "wrong doc selected" FAIL
            # even though the correct doc genuinely won a slot and — since
            # app.py's generate_answer() includes every selected chunk as its
            # own numbered source, never collapsing by bank — the real LLM
            # prompt DOES have the correct doc available. Collect ALL of a
            # bank's selected doc_ids and check membership instead of a
            # single last-wins doc_id. Found via real vehicle_loan
            # investigation (3 of 7 reported general_chat fails — bob, canara,
            # bom — turned out to be this bug, not a real content gap; see
            # PROJECT_STATUS.md for the batch this was found in).
            gen_doc_ids_by_bank: dict[str, list[str]] = {}
            for c in gen_selected:
                gen_doc_ids_by_bank.setdefault(c.get("bank", "INDIAN_BANK"), []).append(c["doc_id"])
            picked_ids = gen_doc_ids_by_bank.get(faiss_code, [])
            gen_pass = target in picked_ids
            results.append({
                "topic": topic_name, "bank_id": bank_id, "query": query,
                "check": "general_chat", "status": "PASS" if gen_pass else "FAIL",
                "detail": (
                    "correct doc selected" if gen_pass
                    else (f"wrong doc(s) selected: {picked_ids}" if picked_ids
                          else f"bank excluded from General Chat's top-{_GENERAL_CHAT_K} entirely for this query"),
                ),
            })

    return results


def run_all() -> list[dict]:
    results = []
    for topic_name in TOPICS:
        results.extend(run_topic(topic_name))
    results.extend(check_fd_rate_shortcut())
    results.extend(check_fd_rate_retrieval_preview())
    return results


def print_report(results: list[dict]) -> None:
    capacity_fails = [r for r in results if r["check"] == "general_chat_capacity" and r["status"] == "FAIL"]
    fd_shortcut = [r for r in results if r["check"] == "fd_rate_shortcut"]
    fd_preview = [r for r in results if r["check"] == "fd_rate_retrieval_preview"]
    bank_results = [
        r for r in results
        if r["check"] not in ("general_chat_capacity", "fd_rate_shortcut", "fd_rate_retrieval_preview")
    ]

    print("=" * 100)
    print(f"Retrieval-competition report - {len(TOPICS)} topic(s)")
    print("=" * 100)

    if capacity_fails:
        print("\nSTRUCTURAL CEILING (not fixable by editing a doc):")
        for r in capacity_fails:
            print(f"  [{r['topic']}] {r['query']!r}")
            print(f"    {r['detail']}")

    print(f"\n{'Topic':<20}{'Bank':<14}{'Query':<12}{'BankScoped':<12}{'GeneralChat':<12}Notes")
    print("-" * 100)
    by_key: dict[tuple, dict] = {}
    for r in bank_results:
        by_key[(r["topic"], r["bank_id"], r["query"], r["check"])] = r

    banks = sorted(BANKS.keys())
    for topic_name, topic in TOPICS.items():
        for qi, query in enumerate(topic["queries"]):
            for bank_id in banks:
                bs = by_key.get((topic_name, bank_id, query, "bank_scoped"))
                gen = by_key.get((topic_name, bank_id, query, "general_chat"))
                if bs is None and gen is None:
                    continue
                bs_s = bs["status"] if bs else "  —  "
                gen_s = gen["status"] if gen else "  —  "
                notes = []
                if bs and bs["status"] == "FAIL":
                    notes.append(f"bank-scoped: {bs['detail']}")
                if gen and gen["status"] == "FAIL":
                    notes.append(f"general: {gen['detail']}")
                print(f"{topic_name:<20}{bank_id:<14}{'q' + str(qi + 1):<12}{bs_s:<12}{gen_s:<12}{'; '.join(notes)}")

    print()
    print("=" * 100)
    print("FD-rate shortcut (PRIMARY) — does the deterministic chat shortcut state the same rate "
          "Compare Rates shows, for every AUTO-block bank's 1/3/5-year tenure?")
    print("=" * 100)
    fd_shortcut_checkable = [r for r in fd_shortcut if r["status"] != "SKIP"]
    fd_shortcut_failed = [r for r in fd_shortcut_checkable if r["status"] == "FAIL"]
    for r in fd_shortcut:
        print(f"  {r['status']:<4}  {r['bank_id']:<14} {r['tenure']:<8} {r['detail']}")
    print(f"  -> {len(fd_shortcut_checkable)} checked, {len(fd_shortcut_failed)} FAIL, "
          f"{len(fd_shortcut_checkable) - len(fd_shortcut_failed)} PASS "
          f"({len(fd_shortcut) - len(fd_shortcut_checkable)} skipped -- no band covers that exact tenure point)")

    print()
    print("=" * 100)
    print("FD-rate RAG-retrieval preview (SECONDARY, WARN only) — would the OLD retrieval-only path "
          "(no shortcut) have stated the right figure? Informational now that the shortcut is primary.")
    print("=" * 100)
    fd_preview_checkable = [r for r in fd_preview if r["status"] != "SKIP"]
    fd_preview_warned = [r for r in fd_preview_checkable if r["status"] == "WARN"]
    for r in fd_preview:
        print(f"  {r['status']:<4}  {r['bank_id']:<14} {r['tenure']:<8} {r['detail']}")
    print(f"  -> {len(fd_preview_checkable)} checked, {len(fd_preview_warned)} WARN "
          f"(WARN only, not counted in FAIL)")

    total = len(bank_results) + len(fd_shortcut_checkable)
    failed = [r for r in bank_results if r["status"] == "FAIL"] + fd_shortcut_failed
    print()
    print("-" * 100)
    print(f"TOTAL: {total} bank-level checks, {len(failed)} FAIL, {total - len(failed)} PASS")
    print(f"       {len(capacity_fails)} structural-ceiling warning(s) (see above, not counted in FAIL)")
    print(f"       {len(fd_preview_checkable)} RAG-retrieval-preview checks, {len(fd_preview_warned)} WARN "
          f"(WARN only, not counted in FAIL)")


if __name__ == "__main__":
    all_results = run_all()
    print_report(all_results)
