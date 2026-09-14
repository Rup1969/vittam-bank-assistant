"""Phase C — rate history over time (Stage 2, spec's deferred "trend"
feature).

`fd_rates` itself holds no history — every re-extraction does a
delete-then-insert per bank (see extract_structured.py's run(), fixed
earlier this project to stop duplicate rows piling up), so it only ever
reflects the LATEST known state. The real accumulated history lives in the
versioned raw snapshots under data/scraped/<bank_id>/<timestamp>.{html,pdf}
— saved specifically "never overwritten... full history for free" (see
project memory) — and in fetch_log's row-per-fetch-attempt record.

This module is what makes that history usable: it re-parses every
successfully-fetched historical snapshot for a bank using the SAME parser
`extract_structured.py` already trusts (imported directly, not
reimplemented — extract_rows_for_source is the shared dispatch), and
reports the rate for a requested tenure at each point in time it was
actually fetched.

Honesty note: this project is ~3 weeks old, and most banks only have 1-2
distinct fetch dates worth of REAL content differences (most re-fetches
on different days returned byte-identical pages) — see fetch_log's
`changed` column. Don't expect a rich multi-month trend for every bank;
expect a rich one only where `changed=True` fired more than once (BOB and
Canara are the strongest examples right now). This function reports
whatever real history exists, including a single point, rather than
padding or interpolating anything.

Run standalone for a demo against real current data:

    python automation/trend.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from groq import Groq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
import settings  # noqa: E402

from db import get_connection  # noqa: E402
from extract_structured import extract_rows_for_source  # noqa: E402
from fetch_and_track import clean_text, extract_pdf_text  # noqa: E402
from tenure_utils import tenure_covers  # noqa: E402

VALID_CUSTOMER_TYPES = ("general", "senior")

# Banks with a deterministic parser never need the Groq client — only
# constructed lazily, and only for a bank that actually needs the generic
# LLM fallback path (currently just SBI), so re-running history for e.g.
# BOB never requires a GROQ_API_KEY call at all.
_CUSTOM_PARSER_BANKS = {
    "canara", "pnb", "union_bank", "iob", "bob",
    "icici", "hdfc", "kotak", "axis", "indusind",
}


def rate_history(bank_id: str, target_days: int, customer_type: str = "general") -> list[dict]:
    """Real (fetched_at, rate, tenure_label) history for one bank/tenure,
    built by re-running extraction against every successfully-fetched
    historical snapshot on disk — not a synthetic or interpolated series.

    Snapshots sharing an identical text_hash (i.e. the page genuinely
    didn't change between two fetches) are only parsed ONCE and the result
    reused — this avoids re-running the LLM extraction path repeatedly for
    identical content when a bank happens to be on the generic path, and
    is just faster in general. The returned list still has one entry per
    real fetch event, so a flat run of identical values across several
    dates is visible as such (proof of staleness/stability), not collapsed
    away.
    """
    if customer_type not in VALID_CUSTOMER_TYPES:
        raise ValueError(f"customer_type must be one of {VALID_CUSTOMER_TYPES}, got {customer_type!r}")
    col = "interest_rate_general" if customer_type == "general" else "interest_rate_senior"

    con = get_connection()
    snapshots = con.execute(
        "SELECT fetched_at, file_path, text_hash FROM fetch_log "
        "WHERE source_id = ? AND error IS NULL ORDER BY fetched_at",
        [bank_id],
    ).fetchall()
    con.close()

    if not snapshots:
        return []

    config = json.loads((PROJECT_ROOT / "config" / "banks.json").read_text(encoding="utf-8"))
    if not any(b["id"] == bank_id for b in config["banks"]):
        raise ValueError(f"Unknown bank_id: {bank_id!r}")

    client = None if bank_id in _CUSTOM_PARSER_BANKS else Groq(api_key=settings.GROQ_API_KEY)

    hash_cache: dict[str, list[dict]] = {}
    history = []
    for fetched_at, file_path, text_hash in snapshots:
        path = Path(file_path)
        if not path.exists():
            history.append({
                "fetched_at": str(fetched_at), "rate": None, "tenure_label": None,
                "note": f"snapshot file missing on disk: {file_path}",
            })
            continue

        if text_hash not in hash_cache:
            try:
                if path.suffix == ".pdf":
                    text = extract_pdf_text(path.read_bytes())
                else:
                    text = clean_text(path.read_text(encoding="utf-8"))
                hash_cache[text_hash] = extract_rows_for_source(bank_id, text, str(path), client)
            except Exception as exc:
                hash_cache[text_hash] = []
                history.append({
                    "fetched_at": str(fetched_at), "rate": None, "tenure_label": None,
                    "note": f"extraction failed: {type(exc).__name__}: {exc}",
                })
                continue

        raw_rows = hash_cache[text_hash]
        match = None
        for raw in raw_rows:
            label = raw.get("tenure_label", "")
            rate = raw.get(col)
            if rate is not None and tenure_covers(label, target_days):
                if match is None or rate > match[col]:
                    match = raw
        if match:
            history.append({
                "fetched_at": str(fetched_at),
                "tenure_label": match["tenure_label"],
                "rate": match[col],
            })
        else:
            history.append({
                "fetched_at": str(fetched_at), "rate": None, "tenure_label": None,
                "note": "no row covering this tenure found in this snapshot",
            })

    return history


if __name__ == "__main__":
    demo_targets = [
        ("bob", 365, "general", "BOB, 1-year, general public"),
        ("canara", 365, "general", "Canara, 1-year, general public"),
    ]
    for bank_id, days, ctype, label in demo_targets:
        print("=" * 70)
        print(f"Rate history — {label}")
        print("=" * 70)
        for point in rate_history(bank_id, days, ctype):
            rate_str = f"{point['rate']}%" if point["rate"] is not None else "—"
            extra = f"  ({point['tenure_label']})" if point.get("tenure_label") else ""
            note = f"  [{point['note']}]" if point.get("note") else ""
            print(f"  {point['fetched_at']:<28} {rate_str:<8}{extra}{note}")
        print()
