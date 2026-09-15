"""Insights & Tools, Step 3 — "Latest Banking News".

Pulls real headlines from live RSS feeds (RSS chosen over HTML scraping
per the project's usual "prefer a reliable structured source over a
brittle scraper" discipline — see PROJECT_STATUS.md for the pre-flight
check that ruled Moneycontrol out: its RSS feeds are all dead/frozen,
some going back to 2016, and its live page's headline list isn't in a
clean, reliably-scrapable static structure).

Only the headline, the outlet's OWN one-line RSS description (never the
full article body), and a link back to the original article are shown —
same copyright discipline as the rest of this project (RBI PDFs are
cited/linked, never reproduced; digest cards report real numbers, never
paraphrase third-party prose at length). A long RSS description is
truncated to roughly one sentence, not rewritten — we don't generate new
text pretending to be the outlet's summary.

Run standalone for a demo against real live feeds:

    python automation/news.py
"""

from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

NEWS_SOURCES = [
    {
        "id": "economic_times",
        "name": "Economic Times",
        "rss_url": "https://economictimes.indiatimes.com/industry/banking/finance/banking/rssfeeds/13358319.cms",
        # Feed is already scoped to the Banking/Finance industry section —
        # no keyword filtering needed, every item qualifies.
        "banking_scoped": True,
    },
    {
        "id": "business_standard",
        "name": "Business Standard",
        "rss_url": "https://www.business-standard.com/rss/finance-103.rss",
        # Feed covers all of "Finance" (SEBI, insurance, NBFC, mutual
        # funds too) — filter to banking/RBI-relevant items only.
        "banking_scoped": False,
    },
]

_BANKING_KEYWORDS_RE = re.compile(
    r"\b(bank|banking|banks|rbi|reserve bank|psb|psu bank|nbfc|deposit|"
    r"loan|emi|interest rate|repo rate|credit|lender|lending|casa|npa|"
    r"monetary policy|fd\b|current account|savings account)\b",
    re.IGNORECASE,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _clean_text(raw: str) -> str:
    text = _TAG_RE.sub("", raw or "")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    return _WHITESPACE_RE.sub(" ", text).strip()


def _one_line_summary(description: str, max_chars: int = 160) -> str:
    """Truncates the outlet's own RSS description to roughly one
    sentence/line — never expands or rewrites it into new text."""
    text = _clean_text(description)
    first_sentence = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    candidate = first_sentence if first_sentence else text
    if len(candidate) > max_chars:
        candidate = candidate[:max_chars].rsplit(" ", 1)[0] + "..."
    return candidate


def _fetch_source(source: dict) -> list[dict]:
    """Fetches and parses one RSS feed. Returns an empty list (never
    raises) on any network/parse failure, so one broken source can't
    take down the whole news panel — same defensive pattern as the rest
    of this project's real-data-or-honest-gap discipline."""
    try:
        req = urllib.request.Request(source["rss_url"], headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
    except Exception as e:
        # Same fix as iba_news.py's fetcher -- log the real cause instead
        # of a silent empty return, so a deployed-host failure is
        # diagnosable from the platform's own console/logs.
        print(f"[news] fetch failed for {source['id']} ({source['rss_url']}): "
              f"{type(e).__name__}: {e}", flush=True)
        return []

    items = []
    for item in root.iter("item"):
        title = _clean_text(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        description = item.findtext("description") or ""
        pub_date_raw = item.findtext("pubDate") or ""
        if not title or not link:
            continue

        if not source["banking_scoped"]:
            haystack = f"{title} {description}"
            if not _BANKING_KEYWORDS_RE.search(haystack):
                continue

        try:
            pub_date = parsedate_to_datetime(pub_date_raw)
        except Exception:
            pub_date = None

        items.append({
            "headline": title,
            "summary": _one_line_summary(description),
            "link": link,
            "outlet": source["name"],
            "pub_date": pub_date,
            "pub_date_label": pub_date.strftime("%d %b %Y, %I:%M %p") if pub_date else "",
        })
    return items


def fetch_headlines(limit: int = 5) -> list[dict]:
    """Real headlines from all configured live sources, merged and
    sorted newest-first, capped at `limit`. Each dict: headline,
    summary (one line, outlet's own RSS description), link, outlet,
    pub_date, pub_date_label. Returns fewer than `limit` items (even
    zero) rather than padding with anything fabricated if feeds are
    slow/unreachable at call time."""
    all_items: list[dict] = []
    for source in NEWS_SOURCES:
        all_items.extend(_fetch_source(source))

    all_items.sort(key=lambda x: x["pub_date"] or _EPOCH, reverse=True)
    return all_items[:limit]


def archive_headlines(headlines: list[dict]) -> int:
    """Persists fetched headlines into news_archive (automation.duckdb),
    one row per article, deduplicated on `link` (a headline re-fetched on
    a later refresh — still current, feed not re-scanning past items —
    is a no-op, not a duplicate row). Called once per real feed fetch
    (see app.py's `_cached_headlines`, itself behind `st.cache_data`),
    never per page view, so this doesn't hammer the DB on every render.
    Returns the number of genuinely new rows inserted."""
    import db

    if not headlines:
        return 0
    con = db.get_connection()
    before = con.execute("SELECT count(*) FROM news_archive").fetchone()[0]
    con.executemany(
        """
        INSERT INTO news_archive (headline, summary, link, outlet, pub_date)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (link) DO NOTHING
        """,
        [
            (h["headline"], h["summary"], h["link"], h["outlet"], h["pub_date"])
            for h in headlines
        ],
    )
    after = con.execute("SELECT count(*) FROM news_archive").fetchone()[0]
    con.close()
    return after - before


def get_archived_headlines(days: int = 30) -> list[dict]:
    """Archived headlines from the last `days` days, newest first (by
    pub_date when known, else by archived_at — an item with no parseable
    pub_date shouldn't fall out of a recency-ordered list entirely)."""
    import db

    con = db.get_connection()
    rows = con.execute(
        """
        SELECT headline, summary, link, outlet, pub_date, archived_at
        FROM news_archive
        WHERE archived_at >= current_timestamp - INTERVAL (?) DAY
        ORDER BY coalesce(pub_date, archived_at) DESC
        """,
        [days],
    ).fetchall()
    con.close()
    return [
        {
            "headline": r[0], "summary": r[1], "link": r[2], "outlet": r[3],
            "pub_date": r[4],
            "pub_date_label": r[4].strftime("%d %b %Y, %I:%M %p") if r[4] else "",
            "archived_at": r[5],
        }
        for r in rows
    ]


if __name__ == "__main__":
    headlines = fetch_headlines(limit=5)
    print("=" * 70)
    print(f"Latest Banking News — {len(headlines)} real headlines")
    print("=" * 70)
    for h in headlines:
        print(f"\n[{h['outlet']}] {h['pub_date_label']}")
        print(f"  {h['headline']}")
        print(f"  {h['summary']}")
        print(f"  {h['link']}")
