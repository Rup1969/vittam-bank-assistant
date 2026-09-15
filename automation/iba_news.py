"""Insights & Tools — "IBA News" tab (separate sub-tab from "Latest
Banking News", never merged into it — Indian Banks' Association is a
distinct kind of source: the industry body, not a news outlet, covering
things RBI doesn't issue directly (wage settlements, Dearness
Allowance/Relief circulars, sector-wide coordination).

Pre-flight checked 2026-08-22 (see PROJECT_STATUS.md for the full
table): iba.org.in has NO bot-defense or CAPTCHA anywhere — every layer
is plain-HTTP fetchable, including the underlying circular PDFs, which
is actually better than RBI's own Master Direction PDFs (CAPTCHA-gated).

Pulls real circular titles + dates + links from IBA's own live
circulars index. Never invents summary prose the source doesn't
provide — IBA's listing has no separate one-line description field
(unlike the ET/Business Standard RSS feeds), so `_classify()` derives a
short, honest, deterministic category label from real keyword matches
in the circular's own title (same discipline as the digest's FD/loan
direction-color logic: real-data-derived, not fabricated).

Run standalone:

    python automation/iba_news.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

IBA_SOURCES = [
    {
        "id": "iba_all_circulars",
        "label": "Circulars index",
        "url": (
            "https://www.iba.org.in/iba/home/HomeAction.do?doNewslist=yes"
            "&sectionIdIndex=5&subSectionIdIndex=0&subSectionIdIndex1=0"
        ),
    },
    {
        "id": "iba_dearness",
        "label": "Dearness Allowance/Relief page",
        "url": "https://www.iba.org.in/iba/home/HomeAction.do?doDearnessCircular=yes",
    },
]

# Matches each real table row: <td>#</td><td>DD-MM-YYYY</td>
# <td><a href='...'>Title</a></td> — confirmed against IBA's live HTML
# during the pre-flight check, not a guessed structure.
_ROW_RE = re.compile(
    r"<td>\d+</td>\s*<td>(\d{2}-\d{2}-\d{4})</td>\s*<td[^>]*>\s*<a href='([^']+)'>([^<]+)</a>",
    re.IGNORECASE,
)
_REF_ID_RE = re.compile(r"_(\d+)\.html\s*$")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _classify(title: str) -> str:
    """Real, deterministic keyword classification of the circular's own
    title — not a fabricated summary, just a label."""
    t = title.lower()
    if "dearness relief" in t:
        return "Dearness Relief"
    if "dearness allowance" in t:
        return "Dearness Allowance"
    if "bipartite" in t or "joint note" in t:
        return "Bipartite Settlement / Joint Note"
    return "General Circular"


def _fetch_source(source: dict) -> list[dict]:
    """Fetches and parses one IBA listing page. Returns an empty list
    (never raises) on any network/parse failure, so one broken source
    can't take down the whole tab."""
    try:
        req = urllib.request.Request(source["url"], headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("iso-8859-1", errors="replace")
    except Exception as e:
        # Previously silent (bare `except Exception: return []`) -- on a
        # deployed host with no console access, "the tab shows nothing"
        # gave zero clue whether this was a timeout, DNS failure, a 403
        # from the site itself, or something else. Same fix as
        # generate_answer()'s error handling (PROJECT_STATUS.md ~2026-09):
        # log the real cause, keep the safe empty-list return unchanged.
        print(f"[iba_news] fetch failed for {source['id']} ({source['url']}): "
              f"{type(e).__name__}: {e}", flush=True)
        return []

    items = []
    for date_str, link, title in _ROW_RE.findall(html):
        title = title.strip()
        if not title or not link:
            continue
        try:
            pub_date = datetime.strptime(date_str, "%d-%m-%Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pub_date = None
        ref_match = _REF_ID_RE.search(link)
        items.append({
            "headline": title,
            "category": _classify(title),
            "ref_id": ref_match.group(1) if ref_match else None,
            "link": link.strip(),
            "outlet": "Indian Banks' Association (IBA)",
            "source_label": source["label"],
            "pub_date": pub_date,
            "pub_date_label": pub_date.strftime("%d %b %Y") if pub_date else date_str,
        })
    return items


def fetch_circulars(limit: int = 6) -> list[dict]:
    """Real IBA circulars, merged from both configured endpoints,
    deduped by the numeric circular reference id embedded in each URL
    (the Dearness Allowance page is a filtered subset of the full
    Circulars index, so the same circular often appears in both — shown
    once, not twice), sorted newest-first, capped at `limit`. Returns
    fewer (even zero) rather than padding with anything fabricated if
    IBA's site is slow/unreachable at call time."""
    all_items: list[dict] = []
    for source in IBA_SOURCES:
        all_items.extend(_fetch_source(source))

    seen = set()
    deduped = []
    for item in all_items:
        key = item["ref_id"] or item["link"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    deduped.sort(key=lambda x: x["pub_date"] or _EPOCH, reverse=True)
    return deduped[:limit]


# Fallback snapshot for hosted deployments IBA's site blocks (confirmed
# HTTP 403 against Streamlit Cloud's network range, PROJECT_STATUS.md
# §56-58) — NOT a live feed. render_iba_news() in app.py only ever reads
# this when the live fetch above genuinely fails; it's never preferred
# over real live data, and every place it's shown discloses the fetch
# date so it can't be mistaken for current. Same honest-disclosure
# pattern as data/automation.duckdb's committed snapshot. Nothing
# refreshes this automatically — re-run `python automation/iba_news.py
# --snapshot` by hand periodically, same manual-update discipline this
# project already uses for Indian Bank/BOI/SBI's other blocked sources.
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "data" / "iba_snapshot.json"


def save_snapshot(path: Path = SNAPSHOT_PATH, limit: int = 10) -> int:
    """Fetches real, current circulars right now and writes them (plus a
    real fetched_at timestamp) to `path`. Returns the number saved."""
    circulars = fetch_circulars(limit=limit)
    serializable = [
        {**c, "pub_date": c["pub_date"].isoformat() if c["pub_date"] else None}
        for c in circulars
    ]
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "circulars": serializable,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(serializable)


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict | None:
    """The saved snapshot (`{"fetched_at": ..., "circulars": [...]}`), or
    None if it doesn't exist or is unreadable — callers must handle that,
    never assume it's present."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


if __name__ == "__main__":
    if "--snapshot" in sys.argv:
        n = save_snapshot()
        print(f"Saved {n} real circulars to {SNAPSHOT_PATH}")
        sys.exit(0)

    circulars = fetch_circulars(limit=6)
    print("=" * 70)
    print(f"IBA News — {len(circulars)} real circulars")
    print("=" * 70)
    for c in circulars:
        print(f"\n[{c['outlet']}] {c['pub_date_label']} — {c['category']} (via {c['source_label']})")
        print(f"  {c['headline']}")
        print(f"  {c['link']}")
