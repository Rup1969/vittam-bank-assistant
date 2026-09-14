"""RAGAS evaluation baseline for the bank-scoped chat pipeline.

Runs `eval_testset.py`'s real test cases through the ACTUAL production
code path: `verify_retrieval.py`'s `_retrieve()` (a byte-for-byte replica
of `app.py`'s `retrieve()`, already trusted as this project's retrieval
ground truth) against the REAL FAISS indexes, then a REAL Groq call using
the exact same system prompt / user-prompt template / model / temperature
/ max_tokens as `app.py`'s `generate_answer()` — not a re-derivation, not
a mocked answer. Scores each case with 4 RAGAS metrics:

  - faithfulness:      does the real answer stick to the retrieved context,
                        or does it drift/invent beyond it?
  - answer_relevancy:   does the real answer actually address the question?
  - context_precision:  are the retrieved chunks relevant to the reference?
  - context_recall:     does retrieval surface what the reference needs?

Compatibility notes (found 2026-08-25, see feedback_multibank_rag_gotchas
memory for the full story):
  - ragas 0.4.3's generic "groq" provider adapter is broken upstream — it
    hardcodes Anthropic's `client.messages.create` shape regardless of
    provider. Routed through provider="openai" instead, pointed at Groq's
    own OpenAI-compatible endpoint (https://api.groq.com/openai/v1) with
    an AsyncOpenAI client — this hits ragas's correctly-tested OpenAI
    adapter path. Does NOT touch this project's own `groq` package/client,
    which app.py still uses directly and unmodified.
  - Embeddings use the project's own local all-MiniLM-L6-v2 via
    `ragas.embeddings.huggingface_provider.HuggingFaceEmbeddings` — no
    OpenAI embeddings call, no extra API dependency.
  - Installing `ragas` pulled in `datasets` as a new transitive dependency
    of `sentence_transformers`, which changed sentence_transformers'
    internal import path and caused a REAL segfault when `faiss` imports
    before `sentence_transformers` in the same process (the order app.py
    used). Fixed by reordering imports project-wide — see app.py's own
    comment at its import block. This script also imports
    sentence_transformers before faiss for the same reason.

Run standalone:

    python automation/evaluate_rag.py [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(AUTOMATION_DIR))
sys.path.insert(0, str(PROJECT_ROOT))

from sentence_transformers import SentenceTransformer  # noqa: E402
import faiss  # noqa: E402

import settings  # noqa: E402
from verify_retrieval import BANKS, _load_pools, _retrieve  # noqa: E402
from eval_testset import build_testset  # noqa: E402

from openai import AsyncOpenAI  # noqa: E402
from ragas.llms import llm_factory  # noqa: E402
from ragas.embeddings.huggingface_provider import HuggingFaceEmbeddings  # noqa: E402
from ragas.metrics.collections import (  # noqa: E402
    Faithfulness,
    AnswerRelevancy,
    ContextPrecision,
    ContextRecall,
)

# Exact match to app.py's GENERAL_SYSTEM_PROMPT (used for every bank-scoped
# chat, not just General Chat mode — see app.py's own scope-resolution
# block around SYSTEM_PROMPT assignment). Duplicated here rather than
# imported from app.py because app.py has Streamlit page-config/UI code
# that executes at import time and isn't safe to import as a plain module
# (same reason verify_retrieval.py replicates retrieve() instead of
# importing it).
_SYSTEM_PROMPT = (
    "You are a helpful and professional assistant covering multiple Indian banks "
    "and general RBI guidelines for commercial banking. "
    "Answer customer questions using ONLY the context provided. Rules: "
    "1. Never invent facts, rates, or product names outside the context. "
    "2. Every chunk is tagged with a BANK. Never attribute one bank's rate, "
    "product name, or policy to a different bank — if the question asks about "
    "a bank with no matching context, say so instead of guessing from another bank's data. "
    "3. When comparing banks, state each bank's figure separately and clearly "
    "label which bank it belongs to. "
    "4. Every chunk also carries a SOURCE_TYPE — use it to set your tone: "
    "   - PRODUCT_PAGE: a bank's own marketing/FAQ content. Note rates are subject to change "
    "and the final rate may depend on credit profile. "
    "   - RBI_MASTER_CIRCULAR or RBI_MASTER_DIRECTION: binding regulatory rules. "
    "State these as rules, not as marketing claims. "
    "5. If the answer is not in context, say: I do not have that detail, "
    "please check the relevant bank's official website or a branch. "
    "6. When the question asks about 'all banks' or implies a full-market "
    "comparison (e.g. 'which bank has the highest...', 'compare X across "
    "banks'), the context you receive may only cover SOME banks, not every "
    "bank that exists — never imply the banks in your context are the "
    "complete set. Answer using exactly the banks present in the context, "
    "phrase any ranking as scoped to those ('the highest among the banks "
    "I have data for is...', not an absolute market-wide claim), and end "
    "by naming which banks you couldn't cover and pointing the customer to "
    "that bank's own profile page (under Public Sector Bank or Private "
    "Bank) for its current rate. "
    "7. Be warm, clear, and complete. Never cut off mid sentence. "
    "8. Write in plain prose only — no markdown (no **, no tables, no bullet "
    "lists with * or -). This answer renders in a plain-text chat bubble, so "
    "markdown syntax would show up as literal stray characters. "
    "9. The [Source i | BANK | CATEGORY | SOURCE_TYPE | doc_id] tags and "
    "SOURCE_TYPE labels in the context are for your eyes only, to decide "
    "wording and tone — never quote, print, or reference them (or the word "
    "'SOURCE_TYPE' itself) in your answer. Write a normal, direct answer."
)


def _generate_answer(groq_client, query: str, retrieved_chunks: list[dict]) -> str:
    """Exact replica of app.py's generate_answer() — same context format,
    same prompt template, same model/temperature/max_tokens."""
    if not retrieved_chunks:
        return (
            "I could not find relevant information. "
            "Please check the relevant bank's official website or a branch."
        )
    context = "\n\n---\n\n".join(
        f"[Source {i} | {c.get('bank', 'INDIAN_BANK')} | {c['category']} | "
        f"{c.get('source_type', 'PRODUCT_PAGE')} | {c['doc_id']}]\n{c['text']}"
        for i, c in enumerate(retrieved_chunks, 1)
    )
    prompt = (
        f"Context:\n{context}\n\n"
        f"Customer Question: {query}\n\n"
        f"Answer using only the context above."
    )
    response = groq_client.chat.completions.create(
        model=settings.GROQ_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        max_tokens=settings.MAX_TOKENS,
        temperature=0.1,
    )
    return response.choices[0].message.content


def run_pipeline_for_case(model, groq_client, legacy_index, legacy_chunks,
                           other_index, other_chunks, case: dict) -> dict:
    """Runs one test case through the REAL retrieval + REAL Groq call,
    exactly the code path a live bank-scoped chat query takes."""
    bank_code = BANKS[case["bank"]]
    if case["bank"] == "indian_bank":
        index, chunks = legacy_index, legacy_chunks
    else:
        index, chunks = other_index, other_chunks

    retrieved = _retrieve(model, case["question"], index, chunks, bank_filter=bank_code)
    answer = _generate_answer(groq_client, case["question"], retrieved)

    return {
        **case,
        "answer": answer,
        "retrieved_contexts": [c["text"] for c in retrieved],
        "retrieved_doc_ids": [c["doc_id"] for c in retrieved],
        "target_doc_retrieved": case["target_doc"] in [c["doc_id"] for c in retrieved],
    }


async def _score_with_backoff(coro_fn, *args, **kwargs):
    """Groq's 8000 TPM ceiling is shared across every judge call this
    script makes; running 4 metrics concurrently per case (the natural
    first approach) blows past it immediately. Calls are made
    SEQUENTIALLY (see score_case below), and this retries on a real 429
    using the server's own suggested wait time from the error message.

    Confirmed the hard way: `instructor`'s own internal retry wrapper
    intercepts the raw `openai.RateLimitError` FIRST, exhausts its own
    (small, fixed) retry budget, and re-raises as
    `instructor.v2.core.errors.InstructorRetryException` — a caller
    catching only `RateLimitError` never sees it and the exception
    propagates uncaught. Catching the base `Exception` and checking for
    the 429/rate_limit signature in its string is more robust than
    chasing every wrapper type instructor's retry internals might raise.
    """
    import re

    last_exc = None
    for attempt in range(5):
        try:
            return await coro_fn(*args, **kwargs)
        except Exception as e:
            msg = str(e)
            if "429" not in msg and "rate_limit" not in msg.lower():
                raise
            last_exc = e
            m = re.search(r"try again in ([\d.]+)s", msg)
            wait_s = float(m.group(1)) + 2.0 if m else 15.0
            print(f"      [rate limited, waiting {wait_s:.1f}s]", end="", flush=True)
            await asyncio.sleep(wait_s)
    raise last_exc


async def score_case(faithfulness, answer_relevancy, context_precision, context_recall, run: dict) -> dict:
    question = run["question"]
    answer = run["answer"]
    contexts = run["retrieved_contexts"]
    reference = run["reference"]

    if not contexts:
        # Groq was never given real context (zero-candidate retrieval) —
        # scoring against an empty context list isn't meaningful for
        # faithfulness/context metrics; record as a hard zero instead of
        # a misleading LLM-judged number.
        return {**run, "faithfulness": 0.0, "answer_relevancy": 0.0,
                "context_precision": 0.0, "context_recall": 0.0}

    # Sequential, not gather() — see _score_with_backoff's docstring.
    f = await _score_with_backoff(faithfulness.ascore, user_input=question,
                                   response=answer, retrieved_contexts=contexts)
    ar = await _score_with_backoff(answer_relevancy.ascore, user_input=question, response=answer)
    cp = await _score_with_backoff(context_precision.ascore, user_input=question,
                                    reference=reference, retrieved_contexts=contexts)
    cr = await _score_with_backoff(context_recall.ascore, user_input=question,
                                    retrieved_contexts=contexts, reference=reference)
    return {
        **run,
        "faithfulness": f.value,
        "answer_relevancy": ar.value,
        "context_precision": cp.value,
        "context_recall": cr.value,
    }


async def main(limit: int | None = None):
    print("Loading real FAISS indexes + embedding model...")
    model, legacy_index, legacy_chunks, other_index, other_chunks = _load_pools()

    from groq import Groq
    groq_client = Groq(api_key=settings.GROQ_API_KEY)

    judge_client = AsyncOpenAI(api_key=settings.GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    # ragas's InstructorModelArgs defaults max_tokens=1024, tuned for
    # non-reasoning models. openai/gpt-oss-20b is a reasoning model that
    # spends hidden "thinking" tokens before any visible JSON output —
    # the exact same failure class already documented in this project for
    # app.py's own MAX_TOKENS (450->1200, see feedback_multibank_rag_
    # gotchas memory). Confirmed via reproduction: at the 1024 default,
    # Faithfulness's statement-generation call (which also has to digest
    # the full retrieved context, a bigger prompt than a plain chat
    # answer) returned Groq HTTP 400 "Failed to validate JSON" with an
    # EMPTY failed_generation — the model burned its whole budget on
    # reasoning. ragas's own reasoning-model detection (_map_openai_params)
    # doesn't recognize the "openai/gpt-oss-20b" name pattern, so this
    # isn't handled automatically — pass a generous explicit budget.
    judge_llm = llm_factory(settings.GROQ_MODEL, provider="openai", client=judge_client, max_tokens=4000)
    embeddings = HuggingFaceEmbeddings(model="all-MiniLM-L6-v2", use_api=False, normalize_embeddings=True)

    faithfulness = Faithfulness(llm=judge_llm)
    answer_relevancy = AnswerRelevancy(llm=judge_llm, embeddings=embeddings)
    context_precision = ContextPrecision(llm=judge_llm)
    context_recall = ContextRecall(llm=judge_llm)

    cases = build_testset()
    if limit:
        cases = cases[:limit]
    print(f"Running {len(cases)} test cases through the real chat pipeline...\n")

    runs = []
    errors = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['topic']:20s} {case['bank']:14s} -> ", end="", flush=True)
        try:
            run = run_pipeline_for_case(model, groq_client, legacy_index, legacy_chunks,
                                         other_index, other_chunks, case)
            scored = await score_case(faithfulness, answer_relevancy, context_precision, context_recall, run)
            runs.append(scored)
            print(f"faith={scored['faithfulness']:.2f} rel={scored['answer_relevancy']:.2f} "
                  f"ctx_p={scored['context_precision']:.2f} ctx_r={scored['context_recall']:.2f}")
        except Exception as e:
            # One case's LLM-judge call exhausting its retry budget
            # shouldn't abort a 24-case run — record it and keep going,
            # same "don't let one bad item hide the rest of the report"
            # principle as verify_retrieval.py's full-suite-not-just-
            # targeted-checks rule.
            print(f"FAILED: {e}")
            errors.append({**case, "error": str(e)})

    if errors:
        print(f"\n{len(errors)} case(s) failed to score (not included in averages below):")
        for e in errors:
            print(f"  {e['topic']:20s} {e['bank']:14s} {e['error'][:120]}")

    _print_report(runs)
    out_path = AUTOMATION_DIR / "eval_results.json"
    out_path.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    print(f"\nFull results (incl. real answers/contexts) written to {out_path}")


def _print_report(runs: list[dict]):
    print("\n" + "=" * 100)
    print("RAGAS EVALUATION BASELINE")
    print("=" * 100)

    metrics = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]

    print(f"\n{'topic':20s} {'bank':14s} {'faith':>7s} {'rel':>7s} {'ctx_p':>7s} {'ctx_r':>7s}  target_doc_retrieved")
    print("-" * 100)
    for r in runs:
        print(f"{r['topic']:20s} {r['bank']:14s} "
              f"{r['faithfulness']:7.2f} {r['answer_relevancy']:7.2f} "
              f"{r['context_precision']:7.2f} {r['context_recall']:7.2f}  "
              f"{'yes' if r['target_doc_retrieved'] else 'NO'}")

    print("\n" + "-" * 100)
    print("OVERALL AVERAGES:")
    for m in metrics:
        avg = sum(r[m] for r in runs) / len(runs)
        print(f"  {m:20s}: {avg:.3f}")

    print("\nBY TOPIC:")
    topics = sorted(set(r["topic"] for r in runs))
    for t in topics:
        subset = [r for r in runs if r["topic"] == t]
        avgs = {m: sum(r[m] for r in subset) / len(subset) for m in metrics}
        print(f"  {t:20s} n={len(subset)}  " + "  ".join(f"{m}={avgs[m]:.2f}" for m in metrics))

    low = [r for r in runs if r["faithfulness"] < 0.7 or r["answer_relevancy"] < 0.7
           or r["context_precision"] < 0.5 or r["context_recall"] < 0.5]
    if low:
        print(f"\nFLAGGED (below-threshold on at least one metric) — {len(low)} of {len(runs)}:")
        for r in low:
            print(f"  {r['topic']:20s} {r['bank']:14s} faith={r['faithfulness']:.2f} "
                  f"rel={r['answer_relevancy']:.2f} ctx_p={r['context_precision']:.2f} "
                  f"ctx_r={r['context_recall']:.2f}")
    else:
        print("\nNo cases fell below threshold on any metric.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N test cases")
    args = parser.parse_args()
    asyncio.run(main(limit=args.limit))
