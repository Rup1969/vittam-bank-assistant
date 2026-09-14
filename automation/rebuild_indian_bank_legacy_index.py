"""Rebuilds indexes/indian_bank.index + indexes/chunks.json from
data/legacy_indian_bank_source/*.txt.

This index was originally built once, early in the project, via a
Jupyter notebook (Untitled.ipynb/Untitled1.ipynb) — there was no .py
script to regenerate it since. That was fine as long as its source
.txt files never changed. It stopped being fine once
loan_education.txt needed a real content fix (adding the numeric
education-loan rate that was manually loaded into loan_rates on
2026-08-20, but never back-ported into this RAG doc — see
PROJECT_STATUS.md for the bug this fixes).

Deliberately mirrors build_index.py's chunking (same CHUNK_SIZE/
CHUNK_OVERLAP, same word-based chunking function) so the two pools stay
comparable, but uses the LEGACY header schema (DOCUMENT_ID/CATEGORY/
PRODUCT/SOURCE — no BANK/SOURCE_TYPE/DATE, unlike data/raw/*.txt) and
writes the legacy chunk shape (chunk_id/doc_id/category/source/text/
word_count — no "bank" key, since app.py's retrieve() already defaults
a missing "bank" field to "INDIAN_BANK" for exactly this pool).

This does NOT touch data/raw/, other_banks.index, or rbi.index — only
the Indian Bank legacy pool, and only because its own source .txt files
under data/legacy_indian_bank_source/ were deliberately edited.

Run standalone:

    python automation/rebuild_indian_bank_legacy_index.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sentence_transformers import SentenceTransformer
import faiss

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
import settings  # noqa: E402

LEGACY_SOURCE_DIR = PROJECT_ROOT / "data" / "legacy_indian_bank_source"


def _extract_header(lines: list[str], key: str, default: str) -> str:
    return next(
        (l.split(":", 1)[1].strip() for l in lines if l.startswith(key)),
        default,
    )


def load_legacy_docs() -> list[dict]:
    docs = []
    files = sorted(LEGACY_SOURCE_DIR.glob("*.txt"))
    for fpath in files:
        text = fpath.read_text(encoding="utf-8")
        lines = text.splitlines()

        doc_id = _extract_header(lines, "DOCUMENT_ID", fpath.stem)
        category = _extract_header(lines, "CATEGORY", "UNKNOWN").upper()
        source = _extract_header(lines, "SOURCE", str(fpath))

        body_lines = [
            l for l in lines
            if not l.startswith(("DOCUMENT_ID", "CATEGORY", "PRODUCT", "SOURCE", "===="))
        ]
        body = "\n".join(body_lines).strip()

        docs.append({"doc_id": doc_id, "category": category, "source": source, "text": body})
    return docs


def chunk_document(doc: dict, chunk_size: int, overlap: int) -> list[dict]:
    words = doc["text"].split()
    chunks = []
    start, idx = 0, 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk_text = " ".join(words[start:end])
        chunks.append({
            "chunk_id": f"{doc['doc_id']}_chunk{idx:03d}",
            "doc_id": doc["doc_id"],
            "category": doc["category"],
            "source": doc["source"],
            "text": chunk_text,
            "word_count": end - start,
        })
        if end == len(words):
            break
        start += chunk_size - overlap
        idx += 1
    return chunks


def main() -> None:
    docs = load_legacy_docs()
    if not docs:
        print(f"No .txt files found under {LEGACY_SOURCE_DIR} — nothing to rebuild.")
        return

    all_chunks = []
    for doc in docs:
        all_chunks.extend(chunk_document(doc, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP))

    print(f"Loaded {len(docs)} docs -> {len(all_chunks)} chunks from {LEGACY_SOURCE_DIR}")

    print(f"Loading embedding model {settings.EMBED_MODEL} ...")
    model = SentenceTransformer(settings.EMBED_MODEL)

    embeddings = model.encode(
        [c["text"] for c in all_chunks], batch_size=32,
        show_progress_bar=True, convert_to_numpy=True,
    )
    faiss.normalize_L2(embeddings)
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    settings.LEGACY_CHUNKS_PATH.write_text(
        json.dumps(all_chunks, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    faiss.write_index(index, str(settings.LEGACY_INDEX_PATH))
    print(f"Wrote {settings.LEGACY_CHUNKS_PATH} ({len(all_chunks)} chunks)")
    print(f"Wrote {settings.LEGACY_INDEX_PATH} ({index.ntotal} vectors)")


if __name__ == "__main__":
    main()
