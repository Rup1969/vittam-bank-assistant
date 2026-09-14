"""
Builds two separate FAISS indexes from curated .txt files in data/raw/:
"Other Banks" (the commercial-bank peer group) and "RBI" (the regulator,
kept structurally separate — see chat). Never touches indian_bank.index;
Indian Bank stays on its own separate legacy pipeline entirely, and any
file tagged BANK: INDIAN_BANK found under data/raw/ is skipped, not
ingested here.

Usage:
    python build_index.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

from sentence_transformers import SentenceTransformer
import faiss

import settings

# Windows terminals default stdout to cp1252, which can't encode the
# warning glyphs this script prints — force utf-8 so it always works,
# regardless of the console's codepage.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def _extract_header(lines: list[str], key: str, default: str) -> str:
    return next(
        (l.split(":", 1)[1].strip() for l in lines if l.startswith(key)),
        default,
    )


def load_raw_files(raw_dir: Path) -> list[dict]:
    """Load all .txt files, recursively, from data/raw/ (e.g. data/raw/sbi/*.txt)."""
    docs = []
    files = sorted(
        f for f in Path(raw_dir).rglob("*.txt")
        if ".ipynb_checkpoints" not in f.parts
    )

    if not files:
        print(f"No .txt files found under {raw_dir}")
        return docs

    for fpath in files:
        text = fpath.read_text(encoding="utf-8")
        lines = text.splitlines()

        source      = _extract_header(lines, "SOURCE", str(fpath))
        doc_id      = _extract_header(lines, "DOCUMENT_ID", fpath.stem)
        category    = _extract_header(lines, "CATEGORY", "UNKNOWN").upper()
        bank        = _extract_header(lines, "BANK", "UNKNOWN").upper()
        source_type = _extract_header(lines, "SOURCE_TYPE", "UNKNOWN").upper()
        # Effective/last-amended date — used for recency-aware retrieval,
        # primarily relevant for RBI content (see chat). Expected YYYY-MM-DD
        # so string sorting and date sorting agree.
        date        = _extract_header(lines, "DATE", "")

        # Indian Bank stays on its own separate legacy index — never let
        # it end up in this pipeline's output, even if a file with this
        # header sits under data/raw/ by mistake.
        if bank == settings.INDIAN_BANK:
            print(f"  ⏭ {fpath.name}: BANK=INDIAN_BANK — skipped, belongs to the legacy pipeline")
            continue

        # Hard skip on an unrecognized/missing BANK header rather than a
        # soft warning — an UNKNOWN bank would otherwise fall through to
        # the "other banks" pool by default (bank != RBI), which is how
        # untagged legacy Indian Bank files ended up polluting that pool
        # once already. Better to refuse than to silently mis-pool.
        if bank not in settings.BANKS:
            print(f"  ⏭ {fpath.name}: BANK '{bank}' not in settings.BANKS — skipped")
            continue

        if category not in settings.CATEGORIES:
            print(f"  ⚠ {fpath.name}: CATEGORY '{category}' not in settings.CATEGORIES")
        if source_type not in settings.SOURCE_TYPES:
            print(f"  ⚠ {fpath.name}: SOURCE_TYPE '{source_type}' not in settings.SOURCE_TYPES")
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            print(f"  ⚠ {fpath.name}: DATE '{date}' not in YYYY-MM-DD format — recency sorting may misbehave")

        body_lines = [
            l for l in lines
            if not l.startswith(("SOURCE", "DOCUMENT_ID", "CATEGORY", "BANK", "SOURCE_TYPE", "DATE", "==="))
        ]
        body = "\n".join(body_lines).strip()

        docs.append({
            "doc_id": doc_id,
            "bank": bank,
            "category": category,
            "source_type": source_type,
            "date": date,
            "source": source,
            "text": body,
            "filename": fpath.name,
        })

    print(f"Loaded {len(docs)} documents from {raw_dir}\n")
    for d in docs:
        wc = len(d["text"].split())
        print(f"  [{d['bank']:<12}|{d['category']:<10}|{d['source_type']:<20}] {d['doc_id']:<40} {wc:>5} words")
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
            "bank": doc["bank"],
            "category": doc["category"],
            "source_type": doc["source_type"],
            "date": doc.get("date", ""),
            "source": doc["source"],
            "text": chunk_text,
            "word_count": end - start,
        })
        if end == len(words):
            break
        start += chunk_size - overlap
        idx += 1

    return chunks


def build_pool(chunks: list[dict], model: SentenceTransformer | None,
                index_path: Path, chunks_path: Path, label: str) -> None:
    if not chunks:
        print(f"\n{label}: no chunks — skipping (nothing tagged for this pool yet).")
        return

    # Written first, unconditionally — chunking work must survive even if
    # embedding fails below (network issue, model download problem, etc.).
    chunks_path.write_text(json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{label}: {len(chunks)} chunks -> {chunks_path}")

    if model is None:
        print(f"{label}: embedding model unavailable — chunks saved, index NOT built. "
              f"Re-run build_index.py once the model can load to finish this pool.")
        return

    embeddings = model.encode(
        [c["text"] for c in chunks],
        batch_size=32,
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    faiss.normalize_L2(embeddings)
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)
    faiss.write_index(index, str(index_path))
    print(f"{label}: FAISS index built ({index.ntotal} vectors) -> {index_path}")


def main() -> None:
    documents = load_raw_files(settings.RAW_DIR)
    if not documents:
        print("Nothing to index — add .txt files under data/raw/<bank>/ first.")
        return

    all_chunks = []
    for doc in documents:
        all_chunks.extend(chunk_document(doc, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP))

    print(f"\nTotal chunks: {len(all_chunks)}")
    print("\nChunks by bank:")
    for bank, count in sorted(Counter(c["bank"] for c in all_chunks).items()):
        print(f"  {bank:<12} {count:>4}")
    print("\nChunks by source_type:")
    for st_, count in sorted(Counter(c["source_type"] for c in all_chunks).items()):
        print(f"  {st_:<20} {count:>4}")

    # Split by pool — RBI is a regulator, kept structurally separate from
    # the commercial-bank peer group (see chat). Indian Bank never appears
    # here at all; load_raw_files() already filtered it out above.
    rbi_chunks   = [c for c in all_chunks if c["bank"] == settings.RBI]
    other_chunks = [c for c in all_chunks if c["bank"] != settings.RBI]

    # Model load attempted AFTER chunks exist conceptually, but the actual
    # write happens inside build_pool() regardless of whether this succeeds.
    print(f"\nLoading embedding model {settings.EMBED_MODEL} ...")
    try:
        model = SentenceTransformer(settings.EMBED_MODEL)
    except Exception as e:
        print(f"⚠ Could not load embedding model: {e}")
        print("  Chunks will still be saved below; indexes will be skipped this run.")
        model = None

    t0 = time.time()
    build_pool(other_chunks, model, settings.OTHER_BANKS_INDEX_PATH,
               settings.OTHER_BANKS_CHUNKS_PATH, "Other Banks")
    build_pool(rbi_chunks, model, settings.RBI_INDEX_PATH,
               settings.RBI_CHUNKS_PATH, "RBI")

    print(f"\nDone in {time.time() - t0:.1f}s")
    print(
        "\nNext: set MULTI_BANK_READY=true in .env, restart Streamlit, "
        "and the bank/category filters will go live."
    )


if __name__ == "__main__":
    main()
