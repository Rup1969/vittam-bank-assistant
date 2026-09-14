"""RAGAS evaluation test set — built from verify_retrieval.py's real TOPICS
dict (PROJECT_STATUS.md Section 22+), not a separately-invented question
bank. Each case pairs a real bank-scoped query with the real indexed
target doc's own text as the `reference` (ground truth) — never a
fabricated "ideal answer".

A representative sample of banks is used per topic (not all 16-17) to
keep a full eval run's real Groq call count tractable. Banks were chosen
for diversity: a mix of PSB/private, and deliberately including
`indian_bank` for personal_loan (its known non-severe "wrong doc"
bank_scoped fail, per PROJECT_STATUS.md Batch 1/2 checkpoints) so the
baseline has a real signal to prioritize against, not just clean passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(AUTOMATION_DIR))

from verify_retrieval import TOPICS  # noqa: E402

# Deliberately diverse bank sample per topic — not the same 3 every time,
# and touches 15 of 17 banks across the whole set at least once.
_SAMPLE_BANKS = {
    "kcc": ["sbi", "pnb", "kotak"],
    "gold_loan": ["bob", "icici", "boi"],
    "vehicle_loan": ["canara", "axis", "iob"],
    "msme_loan": ["hdfc", "central_bank", "punjab_sind"],
    "personal_loan": ["indian_bank", "bom", "uco"],
    "mortgage_loan": ["icici", "boi", "punjab_sind"],
    "home_loan_rag": ["sbi", "canara", "uco"],
    "education_loan_rag": ["axis", "pnb", "hdfc"],
}


def _load_indexed_chunks():
    other_chunks = json.loads(
        (PROJECT_ROOT / "indexes" / "other_banks_chunks.json").read_text(encoding="utf-8")
    )
    legacy_chunks = json.loads(
        (PROJECT_ROOT / "indexes" / "chunks.json").read_text(encoding="utf-8")
    )
    by_doc_id = {}
    for c in other_chunks:
        by_doc_id.setdefault(c["doc_id"], c["text"])
    for c in legacy_chunks:
        # legacy chunks.json entries don't carry a "bank" field of their own
        by_doc_id.setdefault(c["doc_id"], c["text"])
    return by_doc_id


def _reference_from_doc_text(text: str, max_chars: int = 220) -> str:
    """First real sentence(s) of the doc's own indexed text, up to
    max_chars, cut at a sentence boundary where possible — never a
    paraphrase or invented summary, just the doc's own words."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    period = cut.rfind(". ")
    if period > 40:
        return cut[: period + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def build_testset() -> list[dict]:
    by_doc_id = _load_indexed_chunks()
    cases = []
    for topic, spec in TOPICS.items():
        query = spec["queries"][0]
        banks = _SAMPLE_BANKS.get(topic, [])
        for bank in banks:
            target_doc = spec["target_doc_by_bank"].get(bank)
            if not target_doc:
                continue
            doc_text = by_doc_id.get(target_doc)
            if not doc_text:
                # Split-doc headline chunks (this session's Batch 1/2 work)
                # keep the same base id, so this shouldn't happen for any
                # current target — treat as a data-integrity signal, not
                # a silent skip.
                raise ValueError(
                    f"target_doc '{target_doc}' for {bank}/{topic} not found "
                    "in either indexed chunks.json — test set is stale."
                )
            cases.append(
                {
                    "topic": topic,
                    "bank": bank,
                    "question": query,
                    "target_doc": target_doc,
                    "reference": _reference_from_doc_text(doc_text),
                }
            )
    return cases


if __name__ == "__main__":
    cases = build_testset()
    print(f"Built {len(cases)} test cases across {len(TOPICS)} topics.\n")
    for c in cases:
        print(f"[{c['topic']:20s}] {c['bank']:14s} -> {c['target_doc']}")
        print(f"    Q: {c['question']}")
        print(f"    ref: {c['reference'][:100]}...")
        print()
