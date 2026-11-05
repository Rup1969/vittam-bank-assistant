"""Regenerates the AUTO-GENERATED section of specific RAG documents from
real current data -- real database rows for a bank's rate document, a
live fetch of rbi.org.in for RBI's policy-rates document.

Closes the gap documented since the original HDFC investigation
(PROJECT_STATUS.md Sections 44-45, and re-confirmed 2026-11-05 after 51
days of the daily pipeline only ever refreshing automation.duckdb, never
the plain-English documents chat actually reads from): a real rate change
in the database was never reflected in chat, because nothing regenerated
the document or rebuilt the search index from it.

Scope of this version (PROJECT_STATUS.md Section 62, Step 1 of a 5-step
plan): the RBI policy-rates document and HDFC's FD document only. This is
a deliberate pilot, not the full rollout -- see that section for what
isn't built yet (other banks, the daily workflow wiring, a drift check).

Marker convention, by design, not incidental: a document's
"[AUTO-GENERATED:START ...]" / "[AUTO-GENERATED:END]" lines bound the
ONLY region this script ever rewrites. Everything outside those markers
-- explanatory prose, caveats, methodology notes -- is hand-written and
untouched. If a document has no markers, this script does nothing to it
-- it never introduces markers into a document nobody asked to automate,
and never guesses at a different document structure.

Run standalone:
    python automation/sync_rag_docs.py
"""

from __future__ import annotations

import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(AUTOMATION_DIR))

from db import get_connection  # noqa: E402

_START_PREFIX = "[AUTO-GENERATED:START"
_END_LINE = "[AUTO-GENERATED:END]"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

RBI_HOME_URL = "https://www.rbi.org.in/"
RBI_DOC_PATH = PROJECT_ROOT / "data" / "raw" / "rbi" / "rbi_current_policy_rates.txt"
HDFC_FD_DOC_PATH = PROJECT_ROOT / "data" / "raw" / "hdfc" / "hdfc_fd_rates.txt"


def _replace_marked_block(text: str, new_body: str) -> str | None:
    """Swaps the content strictly BETWEEN the START/END marker lines for
    new_body, keeping the marker lines themselves and everything outside
    them byte-for-byte untouched. Returns None if the document has no
    markers at all (nothing to do, not an error)."""
    lines = text.splitlines(keepends=True)
    start_i = next((i for i, l in enumerate(lines) if l.strip().startswith(_START_PREFIX)), None)
    end_i = next((i for i, l in enumerate(lines) if l.strip() == _END_LINE), None)
    if start_i is None or end_i is None or end_i <= start_i:
        return None
    return "".join(lines[: start_i + 1] + [new_body] + lines[end_i:])


def _update_date_header(text: str, new_date: str) -> str:
    return re.sub(r"(?m)^DATE:\s*\S+\s*$", f"DATE: {new_date}", text, count=1)


# ───────────────────────────────── RBI policy rates ───────────────────

# Confirmed directly against the real page 2026-11-05: rbi.org.in's
# homepage renders its "Policy Rates"/"Reserve Ratios" figures as plain
# <th>label</th>...<td>: value</td> pairs inside two accordion tables --
# no JS rendering needed, a plain fetch sees them. This regex doesn't
# care which accordion a row is in; it just needs the row label to be one
# of the ones listed in `_WANTED` below.
_ROW_RE = re.compile(r"<th[^>]*>\s*([^<]+?)\s*</th>\s*<td[^>]*>\s*:?\s*([\d.]+%?)\s*</td>", re.S)

# Real label text on the page (confirmed 2026-11-05) -> this document's
# own established names, and whether that line gets a " p.a." suffix.
_WANTED = {
    "Policy Repo Rate": ("Repo Rate", True),
    "Fixed Reverse Repo Rate": ("Reverse Repo Rate", True),
    "Marginal Standing Facility Rate": ("Marginal Standing Facility (MSF) Rate", True),
    "Bank Rate": ("Bank Rate", True),
    "CRR": ("Cash Reserve Ratio (CRR)", False),
    "SLR": ("Statutory Liquidity Ratio (SLR)", False),
}


def fetch_rbi_current_rates() -> list[tuple[str, str]] | None:
    """Real current values scraped directly from rbi.org.in's own
    homepage. Returns None -- never a guess, never the old value padded
    back in -- if the fetch fails or the page no longer has one of the
    expected labels, since that means the page structure changed and a
    confirmed-correct regex match can no longer be trusted."""
    try:
        req = urllib.request.Request(RBI_HOME_URL, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[sync_rag_docs] RBI homepage fetch failed: {type(e).__name__}: {e}", flush=True)
        return None

    found = {label.strip(): value.strip() for label, value in _ROW_RE.findall(html)}
    missing = [real for real in _WANTED if real not in found]
    if missing:
        print(f"[sync_rag_docs] RBI homepage is missing expected label(s) {missing} "
              f"-- page structure may have changed, not regenerating", flush=True)
        return None

    return [
        (doc_label, found[real_label] + (" p.a." if wants_pa else ""))
        for real_label, (doc_label, wants_pa) in _WANTED.items()
    ]


def regenerate_rbi_doc() -> bool:
    """Returns True if the document's content actually changed on disk."""
    rates = fetch_rbi_current_rates()
    if rates is None:
        return False

    text = RBI_DOC_PATH.read_text(encoding="utf-8")
    now = datetime.now(timezone.utc)
    today_label = f"{now.day} {now.strftime('%B %Y')}"

    lines = [f"As of {today_label} (live-fetched from rbi.org.in), RBI's key policy rates are:\n", "\n"]
    for label, value in rates:
        lines.append(f"{label}: {value}\n")
    new_body = "".join(lines)

    new_text = _replace_marked_block(text, new_body)
    if new_text is None:
        print("[sync_rag_docs] rbi_current_policy_rates.txt has no AUTO-GENERATED markers -- skipping")
        return False
    new_text = _update_date_header(new_text, now.strftime("%Y-%m-%d"))

    if new_text == text:
        return False
    RBI_DOC_PATH.write_text(new_text, encoding="utf-8")
    return True


# ───────────────────────────────── bank FD documents ──────────────────

def _fd_rows_for_bank(bank_id: str) -> list[tuple]:
    """Real current fd_rates rows for one bank, in the real fetch's own
    row order (DuckDB preserves insertion order; extract_structured.py
    inserts in the source page's own tenure order) -- not re-sorted, so
    the document's tenure listing stays in the same order a human reading
    the bank's own page would see, same as before this script existed."""
    con = get_connection()
    rows = con.execute(
        "SELECT tenure_label, interest_rate_general, interest_rate_senior, "
        "effective_date, fetched_at FROM fd_rates "
        "WHERE bank_id = ? AND product_type = 'fixed_deposit' AND deposit_variant = 'retail'",
        [bank_id],
    ).fetchall()
    con.close()
    return rows


def regenerate_bank_fd_doc(bank_id: str, doc_path: Path, bank_display_name: str) -> bool:
    """Returns True if the document's content actually changed on disk.
    Regenerates BOTH the summary paragraph and the tenure table from the
    same query result, in the same run -- so the two can never disagree
    with each other the way a hand-maintained summary sentence could
    silently drift from a hand-maintained table."""
    rows = _fd_rows_for_bank(bank_id)
    if not rows:
        print(f"[sync_rag_docs] no fd_rates rows found for {bank_id!r} -- skipping")
        return False

    text = doc_path.read_text(encoding="utf-8")

    generals = [g for _, g, _, _, _ in rows if g is not None]
    seniors = [s for _, _, s, _, _ in rows if s is not None]
    min_rate, max_rate = min(generals), max(generals)
    premiums = sorted({round(s - g, 2) for _, g, s, _, _ in rows if g is not None and s is not None})
    if len(premiums) == 1:
        premium_sentence = (
            f"with senior citizens earning a flat additional {premiums[0]:g}% "
            f"over the general rate across every tenure."
        )
    else:
        flat = min(premiums)  # the common case; any wider premium is named per-row in the table itself
        premium_sentence = (
            f"with senior citizens earning an additional {flat:g}% over the "
            f"general rate on most tenures (one or more bands carry a wider "
            f"premium -- the table below always shows each band's own real "
            f"senior rate, never assume a flat {flat:g}%)."
        )

    # fd_rates.fetched_at is stored as VARCHAR (confirmed directly, not
    # assumed -- unlike fetch_log.fetched_at, which is a real TIMESTAMP
    # column), so this is real string comparison/slicing, not datetime math.
    latest_fetch_str = max(fetched_at for *_, fetched_at in rows)
    effective_dates = {eff for *_, eff, _ in rows if eff}
    effective_label = (
        sorted(effective_dates)[-1] if len(effective_dates) == 1
        else latest_fetch_str[:10]
    )
    effective_human = _human_date(datetime.strptime(effective_label, "%Y-%m-%d")) \
        if re.match(r"^\d{4}-\d{2}-\d{2}$", effective_label) else effective_label

    lines = [
        f"{bank_display_name}'s current fixed deposit (FD) interest rate for the general\n",
        f"public ranges from {min_rate:g}% to {max_rate:g}% per annum across its full set\n",
        f"of published tenures, {premium_sentence}\n",
        "\n",
        f"Effective {effective_human}, domestic retail term deposit rates by tenure\n",
        "(general public / senior citizen):\n",
    ]
    for tenure_label, g, s, _, _ in rows:
        g_s = f"{g:g}%" if g is not None else "not published"
        s_s = f"{s:g}%" if s is not None else "not published"
        lines.append(f"- {tenure_label}: {g_s} / {s_s}\n")
    new_body = "".join(lines)

    new_text = _replace_marked_block(text, new_body)
    if new_text is None:
        print(f"[sync_rag_docs] {doc_path.name} has no AUTO-GENERATED markers -- skipping")
        return False
    new_text = _update_date_header(new_text, latest_fetch_str[:10])

    if new_text == text:
        return False
    doc_path.write_text(new_text, encoding="utf-8")
    return True


# Windows' strftime has no %-d; this works on both Windows and POSIX.
def _human_date(dt: datetime) -> str:
    return f"{dt.day} {dt.strftime('%B %Y')}"


def run() -> dict[str, bool]:
    """Returns {name: changed?} for every document this pilot covers."""
    results = {}
    results["rbi_current_policy_rates"] = regenerate_rbi_doc()
    results["hdfc_fd_rates"] = regenerate_bank_fd_doc("hdfc", HDFC_FD_DOC_PATH, "HDFC Bank")
    return results


if __name__ == "__main__":
    results = run()
    for name, changed in results.items():
        print(f"{name}: {'UPDATED' if changed else 'no change / skipped'}")
