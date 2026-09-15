"""Insights & Tools / RBI Guidelines — RBI's own "What's New" ticker.

The RBI flash banner originally surfaced the most-recently-DATED chunk
from this project's own static RBI reference docs (Master Directions,
FAQs, etc.) — real content, but not actually "news": those docs are
evergreen reference material, occasionally updated, not a feed of
recent RBI activity. Corrected per explicit user feedback: pull from
RBI's own homepage "What's New" ticker instead — genuinely live,
RBI-authored recent announcements (notifications, press releases,
speeches), same "real external source" discipline as
`news.py`'s ET/Business Standard feeds.

RBI's ticker has no clean per-item date field in its HTML (unlike an
RSS feed's `pubDate`) — order on the page IS the recency signal (newest
first; confirmed via descending notification/press-release IDs). A few
item titles happen to embed a date in their own text (e.g. "...on
August 14, 2026") — extracted opportunistically as `date_hint` when
present, never fabricated when absent.

Run standalone for a demo against the real live page:

    python automation/rbi_news.py
"""

from __future__ import annotations

import re
import urllib.request

RBI_HOME_URL = "https://www.rbi.org.in/"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_LINK_RE = re.compile(r'<a\s+href=\s*"?([^"\s>]+)"?\s*>(.*?)</a>', re.S)
_DATE_HINT_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s+\d{4}"
)


def _clean_text(raw: str) -> str:
    text = _TAG_RE.sub("", raw or "")
    text = text.replace("&#39;", "'").replace("&amp;", "&").replace("&rsquo;", "’")
    return _WHITESPACE_RE.sub(" ", text).strip()


def fetch_rbi_whats_new(limit: int = 10) -> list[dict]:
    """Real items from RBI's own homepage "What's New" ticker, in the
    page's own order (newest first). Each dict: headline, link,
    date_hint (a date found embedded in the item's own title text, or
    "" if none — never fabricated). Returns fewer than `limit` items
    (even zero) rather than padding, matching this project's real-
    data-or-honest-gap discipline (see news.py's fetch_headlines)."""
    try:
        req = urllib.request.Request(RBI_HOME_URL, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        # Same fix as iba_news.py's/news.py's fetchers -- log the real
        # cause instead of a silent empty return.
        print(f"[rbi_news] fetch failed for {RBI_HOME_URL}: "
              f"{type(e).__name__}: {e}", flush=True)
        return []

    match = re.search(r'<div id="whats_new".*?</span>', html, re.S)
    if not match:
        return []
    block = match.group(0)

    items = []
    for href, raw_title in _LINK_RE.findall(block):
        if "NewLinkDetails.aspx" in href:
            continue
        title = _clean_text(raw_title)
        if not title or title.lower() == "more":
            continue
        date_match = _DATE_HINT_RE.search(title)
        items.append({
            "headline": title,
            "link": href.strip(),
            "date_hint": date_match.group(0) if date_match else "",
        })
        if len(items) >= limit:
            break
    return items


if __name__ == "__main__":
    items = fetch_rbi_whats_new(limit=10)
    print("=" * 70)
    print(f"RBI What's New — {len(items)} real items, newest first")
    print("=" * 70)
    for it in items:
        date_str = f" ({it['date_hint']})" if it["date_hint"] else ""
        print(f"\n{it['headline']}{date_str}")
        print(f"  {it['link']}")
