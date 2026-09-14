"""Surfaces candidate 'small fixed search/candidate window' patterns near
FAISS .search() calls, for human review — does NOT auto-judge whether any
site is a bug, just lists them so nobody has to remember to manually grep.

Why this exists: this exact shape of bug has already happened TWICE in
this project — `retrieve()`'s bank-scoped `search_k` (PROJECT_STATUS.md
§28) and `retrieve_general()`'s `_CANDIDATE_WINDOW` (2026-08-26, found
independently, weeks later, via the same repeat-regression symptom, not
by anyone re-checking the code after the first fix). Both were a small
fixed-size candidate window computed GLOBALLY (across every bank) before
filtering down to one bank/subset — so the filtered target could lose out
to a sibling bank's content before filtering ever got a turn. See
feedback_multibank_rag_gotchas.md's "second regression = look for a
shared fixed-size resource" rule.

This script doesn't re-derive that judgment automatically — a human still
has to look at each site and ask "is this window sized relative to what
it gets filtered down to afterward, or a small constant shared globally
across a growing dimension (banks, products, etc.)?" It runs two
independent passes so that question gets asked deliberately every time,
instead of only when a bug report forces it:

  Pass 1 finds every FAISS-style `.search(` call site in the codebase and
  prints the surrounding lines, flagging ones that already contain a
  k=/search_k/window-shaped signal.

  Pass 2 exists because Pass 1 alone has a real, confirmed blind spot:
  `retrieve_general()`'s `_CANDIDATE_WINDOW` doesn't sit near a literal
  `.search(` call at all — it's passed as an argument to `retrieve()`,
  which is where the actual FAISS `.search(` call lives, one level of
  indirection away. Run against Pass-1-only logic, this script would have
  MISSED that exact known case (confirmed 2026-09-01, see
  PROJECT_STATUS.md). Pass 2 independently greps the whole codebase for
  the constant NAMES themselves (`search_k`, `_CANDIDATE_WINDOW`,
  `candidate_window`, and case/spacing variants) regardless of proximity
  to any `.search(` call, to catch that indirect shape too.

Deliberately excludes `re.search(...)` (Python's regex search) in Pass 1
— this codebase uses that constantly for text parsing, and it has
nothing to do with FAISS candidate windows; including it would flood the
output with noise and bury the real candidates.

Run:
    python automation/scan_search_windows.py
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_CONTEXT_BEFORE = 12
_CONTEXT_AFTER = 3

# Signal patterns worth a human's attention in the context around a
# .search( call — not exhaustive, just what this project's own two real
# bugs looked like (a small hardcoded k, a search_k/_CANDIDATE_WINDOW-
# named constant, or a max(k, ...)/max(k*n, ...) formula).
_SIGNAL_RE = re.compile(
    r"search_k|_CANDIDATE_WINDOW|candidate_window|\bk\s*=\s*\d+|max\(\s*k\b",
    re.IGNORECASE,
)

_RE_SEARCH_RE = re.compile(r"\bre\.search\(")

# Pass 2: the constant NAMES themselves, wherever they appear — a
# definition, a function-argument use, a comment referencing one — not
# anchored to a .search( call at all. Covers the exact indirect shape
# that let retrieve_general()'s _CANDIDATE_WINDOW slip past Pass 1.
_NAME_PATTERN_RE = re.compile(
    r"search_k|_?candidate_window", re.IGNORECASE
)


def find_python_files() -> list[Path]:
    files = list(PROJECT_ROOT.glob("*.py"))
    files += list((PROJECT_ROOT / "automation").glob("*.py"))
    return sorted(set(files))


def scan_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines):
        if ".search(" not in line:
            continue
        if _RE_SEARCH_RE.search(line):
            continue  # Python regex search, not a FAISS index search
        start = max(0, i - _CONTEXT_BEFORE)
        end = min(len(lines), i + _CONTEXT_AFTER + 1)
        context = lines[start:end]
        signals = [
            (start + j, ctx_line)
            for j, ctx_line in enumerate(context)
            if _SIGNAL_RE.search(ctx_line)
        ]
        hits.append({
            "file": path.relative_to(PROJECT_ROOT),
            "line": i + 1,
            "call_line": line.strip(),
            "context": context,
            "context_start_line": start + 1,
            "signals": signals,
        })
    return hits


def scan_file_for_names(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = []
    for i, line in enumerate(text.splitlines()):
        if _NAME_PATTERN_RE.search(line):
            hits.append((i + 1, line.strip()))
    return hits


def print_report() -> None:
    files = find_python_files()

    # Pass 1 — proximity to a literal FAISS .search( call.
    pass1_hits: list[dict] = []
    for path in files:
        pass1_hits.extend(scan_file(path))

    print(f"Scanned {len(files)} Python file(s) under {PROJECT_ROOT}\n")

    print("=" * 78)
    print(f"PASS 1 — sites near a literal .search( call "
          f"({len(pass1_hits)} found, re.search excluded)")
    print("=" * 78)
    for hit in pass1_hits:
        print("-" * 78)
        print(f"{hit['file']}:{hit['line']}  ->  {hit['call_line']}")
        print(f"  candidate-window-looking line(s) nearby: {len(hit['signals'])}")
        for line_no, line_text in hit["signals"]:
            print(f"    L{line_no + 1}: {line_text.strip()}")

    # Pass 2 — the constant names themselves, anywhere, regardless of
    # proximity to a .search( call. Catches the indirect "constant feeds a
    # helper function that itself calls .search(" shape Pass 1 can't see.
    print("\n" + "=" * 78)
    name_hits_by_file: dict[Path, list[tuple[int, str]]] = {}
    total_name_hits = 0
    for path in files:
        hits = scan_file_for_names(path)
        if hits:
            name_hits_by_file[path] = hits
            total_name_hits += len(hits)

    print(f"PASS 2 — search_k / _CANDIDATE_WINDOW / candidate_window "
          f"anywhere in the codebase ({total_name_hits} line(s), "
          f"{len(name_hits_by_file)} file(s))")
    print("=" * 78)
    for path, hits in name_hits_by_file.items():
        print("-" * 78)
        print(f"{path}")
        for line_no, line_text in hits:
            print(f"    L{line_no}: {line_text}")

    print("\n" + "=" * 78)
    print(
        "For each site above (either pass): is its search/candidate window "
        "sized relative to what it gets filtered down to afterward (a "
        "bank, a product, etc.), or a small constant shared globally "
        "across a growing dimension? This script surfaces candidates "
        "only — it does not judge that."
    )


if __name__ == "__main__":
    print_report()
