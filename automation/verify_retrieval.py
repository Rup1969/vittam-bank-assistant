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
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))

from sentence_transformers import SentenceTransformer  # noqa: E402
import faiss  # noqa: E402

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
    return results


def print_report(results: list[dict]) -> None:
    capacity_fails = [r for r in results if r["check"] == "general_chat_capacity" and r["status"] == "FAIL"]
    bank_results = [r for r in results if r["check"] != "general_chat_capacity"]

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

    total = len(bank_results)
    failed = [r for r in bank_results if r["status"] == "FAIL"]
    print("-" * 100)
    print(f"TOTAL: {total} bank-level checks, {len(failed)} FAIL, {total - len(failed)} PASS")
    print(f"       {len(capacity_fails)} structural-ceiling warning(s) (see above, not counted in FAIL)")


if __name__ == "__main__":
    all_results = run_all()
    print_report(all_results)
