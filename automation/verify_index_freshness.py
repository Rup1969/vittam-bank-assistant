"""Fourth permanent verification tool — checks whether the FAISS
indexes on disk actually reflect the CURRENT content of their real
source `.txt` files, or whether someone edited a document and forgot
to (or hasn't yet) re-run `build_index.py` /
`automation/rebuild_indian_bank_legacy_index.py`.

Built after a direct question during a project walkthrough: this
project already knew about DB↔RAG-doc drift (a structured rate updated,
the matching text document not re-synced — see `PROJECT_STATUS.md`
§45) and about the index-on-disk↔server-in-memory gap (`st.
cache_resource` needing a full restart to notice a rebuilt index — a
real, repeatedly-hit source of confusion, see [[feedback_multibank_rag_
gotchas]]). This is the THIRD link in that same chain, previously
undocumented and unchecked: a source `.txt` file can be newer than the
index that's supposed to reflect it, with nothing — not this project's
other three verify_*.py tools, not the app itself — ever pointing that
out. `verify_all.py`/`verify_retrieval.py`/`verify_coverage.py` all
test against whatever the CURRENT index files already contain; none of
them compare those files against the real source `.txt` files' own
timestamps.

The check itself is simple and cheap (no embeddings, no LLM calls, no
re-parsing content — just filesystem timestamps): for each of the 3
pools, find the newest `st_mtime` among its real source `.txt` files,
and compare it to the index file's own `st_mtime`. If any source file
is newer than the index, the index is stale — flagged with exactly
which file(s) are newer, not just "something changed."

Also exposed as `check_index_freshness()` for reuse — `app.py` calls
this directly (same function, not a re-implementation) to show a small
live warning in the sidebar if the running app's indexes are stale,
so this doesn't only get caught when someone remembers to run this
script by hand.

Run standalone:

    python automation/verify_index_freshness.py
"""

from __future__ import annotations

import sys
from pathlib import Path

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


def _other_and_rbi_source_files() -> tuple[list[Path], list[Path]]:
    """Real `data/raw/**/*.txt` files, split into (other_banks, rbi)
    by each file's own real `BANK:` header — the exact same rule
    `build_index.py`'s `load_raw_files()` uses (RBI-tagged -> rbi pool,
    INDIAN_BANK-tagged -> skipped entirely, everything else -> other
    pool), so this check's grouping can never drift from what actually
    gets indexed where."""
    other_files, rbi_files = [], []
    for fpath in sorted(settings.RAW_DIR.rglob("*.txt")):
        if ".ipynb_checkpoints" in fpath.parts:
            continue
        lines = fpath.read_text(encoding="utf-8").splitlines()
        bank = _extract_header(lines, "BANK", "UNKNOWN").upper()
        if bank == settings.INDIAN_BANK:
            continue
        if bank == settings.RBI:
            rbi_files.append(fpath)
        else:
            other_files.append(fpath)
    return other_files, rbi_files


def check_index_freshness() -> list[dict]:
    """One result per pool: PASS if the index file is at least as new
    as every real source file that feeds it, FAIL (with the specific
    newer file(s) named) otherwise."""
    other_files, rbi_files = _other_and_rbi_source_files()
    legacy_files = sorted(LEGACY_SOURCE_DIR.glob("*.txt"))

    pools = [
        ("other_banks", settings.OTHER_BANKS_INDEX_PATH, other_files),
        ("rbi", settings.RBI_INDEX_PATH, rbi_files),
        ("indian_bank_legacy", settings.LEGACY_INDEX_PATH, legacy_files),
    ]

    results = []
    for name, index_path, source_files in pools:
        if not source_files:
            results.append({
                "pool": name, "status": "PASS",
                "detail": "no real source files found for this pool — nothing to compare",
            })
            continue

        if not index_path.exists():
            results.append({
                "pool": name, "status": "FAIL",
                "detail": f"index file {index_path.name} doesn't exist at all — "
                          f"never built, or built to a different path",
            })
            continue

        index_mtime = index_path.stat().st_mtime
        newer = sorted(
            (f for f in source_files if f.stat().st_mtime > index_mtime),
            key=lambda f: f.stat().st_mtime, reverse=True,
        )
        if newer:
            names = ", ".join(f.name for f in newer[:5])
            more = f" (+{len(newer) - 5} more)" if len(newer) > 5 else ""
            results.append({
                "pool": name, "status": "FAIL",
                "detail": f"{len(newer)} source file(s) newer than {index_path.name} — "
                          f"rebuild needed: {names}{more}",
            })
        else:
            results.append({
                "pool": name, "status": "PASS",
                "detail": f"{index_path.name} is current with all {len(source_files)} real source file(s)",
            })

    return results


def print_report(results: list[dict]) -> None:
    print("=" * 90)
    print("Index freshness — do the FAISS indexes reflect the CURRENT source .txt files?")
    print("=" * 90)
    for r in results:
        print(f"  [{r['status']}] {r['pool']:<20} {r['detail']}")

    failed = [r for r in results if r["status"] == "FAIL"]
    print("-" * 90)
    print(f"TOTAL: {len(results)} pools checked, {len(failed)} FAIL, {len(results) - len(failed)} PASS")
    if failed:
        print("\nTo fix a FAIL: re-run the matching build script, then restart the Streamlit")
        print("server (or rely on its automatic reload-on-rebuilt-index — see app.py).")
        print("  other_banks / rbi  -> python build_index.py")
        print("  indian_bank_legacy -> python automation/rebuild_indian_bank_legacy_index.py")


if __name__ == "__main__":
    print_report(check_index_freshness())
