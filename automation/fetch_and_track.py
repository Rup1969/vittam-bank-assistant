"""Phase A — fetch, version, hash, and change-detect each configured source.

Run manually for now (see Bank_Assistant_Upgrade_Spec.md — real scheduled
automation is deferred until this has been verified working by hand a few
times):

    python automation/fetch_and_track.py

For each entry in config/banks.json:
  1. Fetch the page with a real (headless) browser — see note below on why
     plain HTTP isn't enough — no CAPTCHA/bot-detection bypass attempted;
     if a site blocks this, that's logged as an error for that source, not
     worked around. Sources marked `"content_type": "pdf"` (currently just
     Axis — its real rate table only exists as a downloadable PDF, not on
     any HTML page) are fetched as a raw binary download instead of a
     rendered page — see fetch_pdf_bytes below.
  2. Strip HTML to cleaned text (or extract flattened text from the PDF's
     pages) and hash it (SHA-256) — hashing the extracted TEXT rather than
     raw bytes means a PDF/page that's re-published with the same visible
     content but different internal metadata/generation-timestamp doesn't
     falsely register as "changed", same principle already applied to HTML.
  3. Save the raw fetched file to data/scraped/<source_id>/<timestamp>.html
     or .pdf — never overwritten, so every version stays available.
  4. Compare the hash to the last successful fetch for that source; log a
     fetch_log row noting whether the content changed.

Why a headless browser, not plain HTTP: the first real run of Phase B
(2026-07-25) surfaced that SBI's and Canara's actual FD rate tables are
loaded via JavaScript/AJAX after the page loads — a plain `urllib.request`
GET never sees them (confirmed: zero of the real tenure/rate numbers
appeared anywhere in the raw HTML for either bank). Rendering the page with
Playwright's headless Chromium and reading the DOM after load solves this —
it's still just viewing the page as any browser would, not defeating any
bot-detection.

This module never touches indexes/*.index, indexes/*.json, or
build_index.py — it is a fully separate pipeline, including for the
Indian Bank source (tracked read-only, per config's "read_only" flag).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import datetime
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

from db import get_connection, last_hash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "banks.json"
SCRAPED_DIR = PROJECT_ROOT / "data" / "scraped"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class UnexpectedContentError(Exception):
    """Raised when a fetch returns HTTP 200 but the content doesn't look
    like a real deposit-rates page — deliberately a POSITIVE check (does
    the expected content appear to be present?) rather than a blocklist of
    known bot-challenge signatures. Indian Bank's site returns two
    different bot-defense challenges observed so far — a plain "Request
    Rejected" page, and a separate, more elaborate obfuscated JS challenge
    (an F5/TS-style bot-defense script, identifiable by "TSPD" markers) —
    and there is no reason to expect those are the only two variants.
    Trying to pattern-match every specific challenge page is an arms race
    against the site's bot defense, which isn't something to build (or
    keep extending) — checking for expected real content instead is a
    stable signal that doesn't depend on knowing what any particular
    block page looks like."""


# Terms that should appear somewhere in genuine deposit-rate page text.
# Checked against the CLEANED text, not raw HTML — a bot-challenge page's
# actual rendered text (after stripping scripts) tends to be short and
# generic, which the length floor below also catches.
_EXPECTED_CONTENT_MARKERS = ("deposit", "interest rate", "tenure", "fixed deposit")
_MIN_CONTENT_CHARS = 500

# Retries a source's fetch within the same run before giving up on it.
# Added after confirming (twice — Union Bank, then HDFC) that some sites'
# WAF/CDN blocks are intermittent, not permanent: a failed attempt seconds
# apart from a successful one is a real, reproducible pattern here, not a
# hypothetical. A short pause between attempts gives whatever session/rate-
# based rule triggered the block a chance to clear before the next try.
# Applies to every source uniformly (cheap for sources that never fail —
# they simply succeed on attempt 1) rather than special-casing specific
# banks, since any source could turn out to have the same intermittent
# behavior in the future.
_MAX_FETCH_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 5


def fetch_source(
    url: str, page: Page, interactions: list[dict] | None = None, wait_until: str = "networkidle"
) -> str:
    """Load url in the given (already-open) Playwright page and return the
    rendered HTML — i.e. the DOM after JavaScript/AJAX content has loaded,
    not just the server's initial response.

    `interactions` is an optional, config-driven sequence of clicks to run
    after the page loads, before capturing content — some sites (Canara's
    deposit-rates page, confirmed 2026-07-25) only render their real rate
    table after a popup is dismissed and/or a tab/accordion is clicked;
    plain page-load alone never reveals it. Each entry is
    {"action": "click", "selector": "...", "optional": bool}. "optional"
    steps (e.g. a popup that may not appear on every visit) are skipped
    silently on timeout; non-optional steps raise, since a required
    interaction failing means the page can't be trusted to have loaded
    correctly.

    `wait_until` defaults to "networkidle" (waits for the page to stop
    firing network requests — the safest default, since most sites' rate
    tables genuinely load via a trailing AJAX call after the initial
    page). Overridable per-source via config's "wait_until" field for
    sites where this never fires: confirmed on IndusInd Bank's FD page
    (2026-08-15) — some persistent background request (analytics/chat
    widget, never identified further, not worth chasing) means
    "networkidle" times out every time even though the actual rate table
    itself finishes rendering almost immediately. "commit" (page has
    started receiving a response, DOM not necessarily finished) was
    confirmed to load this specific page's real content correctly.

    Raises on failure — callers decide how to log it, this function does
    not swallow errors."""
    page.goto(url, wait_until=wait_until, timeout=30000)
    if wait_until != "networkidle":
        # A weaker wait condition means the DOM isn't guaranteed finished
        # the instant goto() returns — give it a moment to actually
        # render before reading content. Skipped for the default
        # "networkidle" (which already implies rendering settled).
        # 3000ms was tried first and confirmed too short for IndusInd's
        # FD-rates page specifically (page.content() still only 56 chars
        # — just the <title> tag, body not yet populated); 6000ms
        # reliably captured that page. Bumped again to 8000ms (Stage 3,
        # 2026-08-17) after IndusInd's home_loan page needed a longer
        # settle than its FD page did — 5000ms produced only 24 chars on
        # that page specifically, 8000ms reliably captured its real
        # ~750KB rendered content. Different pages on the same site
        # apparently settle at different speeds, not just a fixed
        # per-site constant — if a THIRD IndusInd page needs even longer,
        # don't assume 8000ms is a permanent ceiling.
        page.wait_for_timeout(8000)

    for step in interactions or []:
        selector = step["selector"]
        try:
            page.locator(selector).first.click(timeout=8000)
            page.wait_for_timeout(1500)
        except PlaywrightTimeoutError:
            if not step.get("optional"):
                raise

    return page.content()


def fetch_pdf_bytes(url: str, page: Page) -> bytes:
    """Download a PDF's raw bytes via Playwright's request API — a PDF is
    a binary file, not something to render/navigate as a page, so this
    bypasses page.goto()/page.content() (the HTML path) entirely and just
    performs the HTTP GET, returning the response body directly."""
    response = page.request.get(url, timeout=30000)
    if not response.ok:
        raise RuntimeError(f"PDF fetch failed: HTTP {response.status}")
    return response.body()


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Flatten every page's text into one string — the PDF equivalent of
    clean_text() below, used for the same two purposes: hashing for
    change-detection, and the check_looks_like_real_content() sanity
    check. Deliberately NOT used for the actual rate-table extraction —
    flattening a PDF table to plain text loses column alignment the same
    way HTML table-to-text flattening does for other banks in this
    pipeline, so table-STRUCTURE extraction (extract_axis_rows, in
    extract_structured.py) re-opens the saved PDF file directly with
    pdfplumber's table-aware extraction instead of working from this
    flattened text."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return " ".join(
            " ".join((p.extract_text() or "").split())
            for p in pdf.pages
        )


def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ").split())


def check_looks_like_real_content(text: str) -> None:
    """Raises UnexpectedContentError if cleaned text doesn't look like a
    genuine rate page — see the class docstring for why this is a
    positive check, not a bot-challenge blocklist."""
    lowered = text.lower()
    if len(text) < _MIN_CONTENT_CHARS or not any(m in lowered for m in _EXPECTED_CONTENT_MARKERS):
        raise UnexpectedContentError(
            "Fetched content doesn't look like a real deposit-rates page "
            f"(length={len(text)} chars, no expected finance terms found) — "
            "likely a bot-defense challenge page rather than the actual site "
            "content. Not something this script tries to work around."
        )


def describe_fetch_error(exc: Exception) -> str:
    """Human-readable, non-alarming classification of a fetch failure —
    used both in the printed summary and the fetch_log.error column."""
    if isinstance(exc, UnexpectedContentError):
        return str(exc)
    message = str(exc)
    if "ERR_CERT" in message or "SSL" in message.upper():
        return (
            "SSL/certificate error — may be a local network/proxy issue in "
            "this environment rather than the site itself; verify from a "
            "different network before assuming the site is unreachable. "
            "Never disable certificate verification to work around this. "
            f"(raw: {message})"
        )
    if isinstance(exc, PlaywrightTimeoutError):
        return f"Page load timed out: {message}"
    return f"{type(exc).__name__}: {message}"


def run() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    con = get_connection()
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        for bank in config["banks"]:
            bank_id = bank["id"]
            for url_type, url in bank["urls"].items():
                # source_id stays bare bank_id for fd_rates (unchanged,
                # preserves every existing fetch_log row's identity) and
                # gets a "_<url_type>" suffix for anything else (Stage 3:
                # "_home_loan") — this is how one bank can have multiple
                # independently-tracked, independently-changed-detected
                # fetch histories without changing fetch_log's schema.
                source_id = bank_id if url_type == "fd_rates" else f"{bank_id}_{url_type}"

                # content_type is genuinely PER-URL, not per-bank — Axis's
                # fd_rates is a PDF but its home_loan page is normal HTML,
                # so this stays scoped to fd_rates only (applying it to
                # every url_type would break Axis's own new home_loan
                # fetch). wait_until, by contrast, is mostly a SITE-WIDE
                # rendering quirk, confirmed the hard way for IndusInd
                # Stage 3: its home_loan URL hit the exact same "never
                # reaches networkidle" issue its fd_rates page did (same
                # site, same persistent-background-request cause), so it
                # applies to every url_type for a bank, not just fd_rates.
                #
                # interactions turned out to be the THIRD pattern, found
                # 2026-08-19 via Canara's loan_rates extension: its
                # fd_rates page needs a popup-dismiss + "TERM DEPOSITS"
                # accordion click, but that accordion selector doesn't
                # exist on its home_loan page at all — a non-optional step
                # targeting a selector that isn't there would raise and
                # fail the whole fetch. Scoped to fd_rates only, like
                # content_type, rather than assumed site-wide like
                # wait_until — evidence-based, not a guess: if a future
                # bank's non-fd_rates page genuinely needs its own
                # interaction sequence, that'll surface as an honest fetch
                # failure/wrong-structure result, diagnosable the same way
                # this one was, and can grow into a real per-url_type
                # config shape then.
                is_pdf = url_type == "fd_rates" and bank.get("content_type") == "pdf"
                interactions = bank.get("interactions") if url_type == "fd_rates" else None
                wait_until = bank.get("wait_until", "networkidle")

                fetched_at = datetime.now()

                raw_bytes = text = None
                error = None
                for attempt in range(1, _MAX_FETCH_ATTEMPTS + 1):
                    try:
                        if is_pdf:
                            raw_bytes = fetch_pdf_bytes(url, page)
                            text = extract_pdf_text(raw_bytes)
                        else:
                            html = fetch_source(
                                url, page, interactions=interactions,
                                wait_until=wait_until,
                            )
                            raw_bytes = html.encode("utf-8")
                            text = clean_text(html)
                        check_looks_like_real_content(text)
                        error = None
                        break
                    except Exception as exc:
                        error = describe_fetch_error(exc)
                        if attempt < _MAX_FETCH_ATTEMPTS:
                            time.sleep(_RETRY_DELAY_SECONDS)

                if error is not None:
                    con.execute(
                        "INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?)",
                        [source_id, fetched_at, "", "", False,
                         f"{error} (failed after {_MAX_FETCH_ATTEMPTS} attempts)"],
                    )
                    results.append((source_id, "ERROR", error))
                    continue

                text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

                out_dir = SCRAPED_DIR / source_id
                out_dir.mkdir(parents=True, exist_ok=True)
                # Colon-free timestamp — Windows filenames can't contain ':'.
                stamp = fetched_at.strftime("%Y-%m-%d_%H%M%S")
                file_path = out_dir / f"{stamp}.{'pdf' if is_pdf else 'html'}"
                file_path.write_bytes(raw_bytes)

                previous_hash = last_hash(con, source_id)
                changed = previous_hash is None or previous_hash != text_hash

                con.execute(
                    "INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?)",
                    [source_id, fetched_at, str(file_path), text_hash, changed, None],
                )
                status = "CHANGED" if changed else "unchanged"
                results.append((source_id, status, str(file_path)))

        browser.close()

    con.close()

    print(f"\n{'Source':<15} {'Status':<12} Detail")
    print("-" * 70)
    for source_id, status, detail in results:
        print(f"{source_id:<15} {status:<12} {detail}")


if __name__ == "__main__":
    run()
