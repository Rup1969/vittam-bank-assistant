"""Generates the README's primary evaluation chart from REAL, current
verify_retrieval.py results — not a static/frozen image. Re-run this any
time retrieval content changes, so the README's chart never drifts from
what the suite actually reports.

Chart: per-category bank-scoped pass rate (does bank_filter=<bank>
retrieve the bank's own real target doc for that product category?) —
this is the retrieval-competition check this project's severe-gap fixing
effort (PROJECT_STATUS.md §22-28) was built around, so it's the most
direct real evidence of system reliability.

Run standalone:

    python automation/plot_eval_chart.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(AUTOMATION_DIR))

from sentence_transformers import SentenceTransformer  # noqa: E402,F401 (import order)
import faiss  # noqa: E402,F401
import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from verify_retrieval import run_all  # noqa: E402

OUT_PATH = PROJECT_ROOT / "docs" / "assets" / "verify_retrieval_pass_rates.png"


def main():
    results = run_all()
    bank_scoped = [r for r in results if r["check"] == "bank_scoped"]

    by_topic: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in bank_scoped:
        by_topic[r["topic"]][1] += 1
        if r["status"] == "PASS":
            by_topic[r["topic"]][0] += 1

    topics = sorted(by_topic.keys())
    rates = [100 * by_topic[t][0] / by_topic[t][1] for t in topics]
    _ACRONYMS = {"kcc": "KCC", "msme_loan": "MSME Loan"}
    labels = [_ACRONYMS.get(t, t.replace("_rag", "").replace("_", " ").title()) for t in topics]
    counts = [f"{by_topic[t][0]}/{by_topic[t][1]}" for t in topics]

    total_pass = sum(by_topic[t][0] for t in topics)
    total_n = sum(by_topic[t][1] for t in topics)

    fig, ax = plt.subplots(figsize=(9, 5))
    colors = ["#2e7d32" if r == 100 else "#f9a825" if r >= 90 else "#c62828" for r in rates]
    bars = ax.barh(labels, rates, color=colors, height=0.6)
    ax.set_xlim(0, 108)
    ax.set_xlabel("Bank-scoped retrieval pass rate (%)")
    ax.set_title(
        f"Retrieval reliability by product category\n"
        f"(does the right bank's own doc surface for its own real query? — "
        f"{total_pass}/{total_n} overall = {100*total_pass/total_n:.1f}%)",
        fontsize=11,
    )
    for bar, rate, count in zip(bars, rates, counts):
        ax.text(bar.get_width() + 1.5, bar.get_y() + bar.get_height() / 2,
                 f"{rate:.0f}%  ({count})", va="center", fontsize=9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.invert_yaxis()
    fig.tight_layout()

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PATH, dpi=150)
    print(f"Saved {OUT_PATH} ({total_pass}/{total_n} = {100*total_pass/total_n:.1f}% overall)")


if __name__ == "__main__":
    main()
