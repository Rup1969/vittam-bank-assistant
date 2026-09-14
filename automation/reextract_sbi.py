"""One-off: re-run SBI's FD-rate extraction against its latest cached
snapshot (no live re-fetch) using the updated EXTRACTION_PROMPT that tags
deposit_variant instead of silently discarding the Non-Callable Term
Deposit table. See PROJECT_STATUS.md section 4b/8 item 0 for the audit
finding this fixes.

Run once after any EXTRACTION_PROMPT change that should apply to SBI's
already-fetched content without waiting for the next scheduled fetch:

    python automation/reextract_sbi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

from groq import Groq  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import settings  # noqa: E402
from fetch_and_track import clean_text  # noqa: E402
from db import get_connection  # noqa: E402
from extract_structured import extract_rows_for_source  # noqa: E402
from schema import FDRateRecord  # noqa: E402


def run() -> None:
    con = get_connection()
    row = con.execute(
        "SELECT fetched_at, file_path FROM fetch_log "
        "WHERE source_id = 'sbi' AND error IS NULL "
        "ORDER BY fetched_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        print("No SBI fetch_log entry found — run fetch_and_track.py first.")
        con.close()
        return
    fetched_at, file_path = row

    html = Path(file_path).read_text(encoding="utf-8")
    text = clean_text(html)

    config = json.loads((PROJECT_ROOT / "config" / "banks.json").read_text(encoding="utf-8"))
    source_url = next(b["urls"]["fd_rates"] for b in config["banks"] if b["id"] == "sbi")

    client = Groq(api_key=settings.GROQ_API_KEY)
    raw_rows = extract_rows_for_source("sbi", text, file_path, client)

    if not raw_rows:
        print("Extraction returned zero rows — leaving existing SBI data untouched.")
        con.close()
        return

    con.execute("DELETE FROM fd_rates WHERE bank_id = 'sbi'")
    accepted, rejected = 0, 0
    for raw in raw_rows:
        try:
            record = FDRateRecord(
                bank_id="sbi",
                source_url=source_url,
                fetched_at=str(fetched_at),
                **raw,
            )
        except ValidationError as exc:
            print(f"  rejected row {raw!r}: {exc}")
            rejected += 1
            continue
        con.execute(
            """
            INSERT INTO fd_rates
            (bank_id, product_type, tenure_label, tenure_days_min, tenure_days_max,
             interest_rate_general, interest_rate_senior, min_deposit,
             effective_date, source_url, fetched_at, data_source, deposit_ceiling,
             deposit_variant)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                record.bank_id, record.product_type, record.tenure_label,
                record.tenure_days_min, record.tenure_days_max,
                record.interest_rate_general, record.interest_rate_senior,
                record.min_deposit, record.effective_date, record.source_url,
                record.fetched_at, record.data_source, record.deposit_ceiling,
                record.deposit_variant,
            ],
        )
        accepted += 1
    con.close()

    print(f"SBI re-extraction: {accepted} rows accepted, {rejected} rejected.")
    for raw in raw_rows:
        print(f"  {raw.get('tenure_label'):<30} variant={raw.get('deposit_variant', 'retail'):<12} "
              f"gen={raw.get('interest_rate_general')}  sen={raw.get('interest_rate_senior')}")


if __name__ == "__main__":
    run()
