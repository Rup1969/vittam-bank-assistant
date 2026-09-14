"""Human-readable status report for the Phase A/B automation pipeline.

Run anytime to see what's actually in data/automation.duckdb, without
needing to know SQL:

    python automation/report.py
"""

from __future__ import annotations

from db import get_connection


def run() -> None:
    con = get_connection()

    print("=" * 70)
    print("FETCH HISTORY (Phase A — every time a source was checked)")
    print("=" * 70)
    rows = con.execute(
        "SELECT source_id, fetched_at, changed, error FROM fetch_log "
        "ORDER BY fetched_at DESC"
    ).fetchall()
    if not rows:
        print("(none yet — run: python automation/fetch_and_track.py)")
    for source_id, fetched_at, changed, error in rows:
        status = f"ERROR: {error}" if error else ("changed" if changed else "unchanged")
        print(f"  {fetched_at}  {source_id:<15} {status}")

    print()
    print("=" * 70)
    print("EXTRACTED FD RATES (Phase B — structured data pulled from real pages)")
    print("=" * 70)
    rows = con.execute(
        "SELECT bank_id, tenure_label, interest_rate_general, interest_rate_senior, "
        "source_url FROM fd_rates ORDER BY bank_id, tenure_days_min NULLS LAST"
    ).fetchall()
    if not rows:
        print("(none yet — run: python automation/extract_structured.py)")
    current_bank = None
    for bank_id, tenure, gen, sen, url in rows:
        if bank_id != current_bank:
            print(f"\n  {bank_id.upper()}  (source: {url})")
            current_bank = bank_id
        print(f"    {tenure:<45} General: {gen}%   Senior: {sen}%")

    con.close()
    print()
    print("=" * 70)
    print("Raw page snapshots you can open yourself in any browser:")
    print("  data/scraped/<source_id>/<timestamp>.html")
    print("=" * 70)


if __name__ == "__main__":
    run()
