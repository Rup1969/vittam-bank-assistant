# Project Deep Dive — A Plain-Language Reference

This document compiles a 9-section, plain-language walkthrough of this project delivered across several conversations in 2026-08-31 and 2026-09-01. Unlike `PROJECT_STATUS.md` (a chronological build log — what happened, in what order, session by session), this document is organized by **topic**, so a new reader — including a future version of the person maintaining this project — can understand the whole system without reconstructing it from history.

A note on honesty, since it shaped how this was written: several sections below record a claim made *earlier in the same walkthrough* that turned out to be wrong, oversimplified, or incomplete — caught either by a sharp follow-up question or by a later piece of real engineering work. Those corrections are kept visible rather than smoothed away, including one genuine mistake (Section 4) where a confident-sounding claim was made from reading a script's comments instead of checking the live database. The point of leaving them in is the same reason the rest of this project documents its own gaps honestly: a clean-sounding final answer that hides how the understanding actually developed would be less trustworthy, not more.

Every verification claim below is real — a script that was actually run, a screenshot actually taken, a query actually issued against the live database — not a description of what *should* be true.

---

## 1. Architecture — the main pieces, and how they fit together

At a high level, this is a Streamlit web app with four working parts that stay deliberately separate rather than merged into one system:

1. **The Library** — plain-English documents (`data/raw/**/*.txt`, one set per bank, plus RBI content), turned into a searchable index by an embedding model and FAISS (a local nearest-neighbor search library). This is what chat actually reads from.
2. **The Spreadsheet** — a DuckDB database (`data/automation.duckdb`) holding exact structured numbers: `fd_rates` and `loan_rates` tables, fed by an independent fetch/extract pipeline. This is what Compare Rates, the Calculator, and the rate-change digest read from.
3. **The Front Counter** — `app.py`, the Streamlit UI itself: login, chat, Compare Rates, Insights & Tools.
4. **The AI Writer** — Groq, an LLM used narrowly (see Section 5) to turn retrieved facts into a readable answer, or to parse a free-text question into structured parameters. It is deliberately kept out of the business of stating numbers on its own.

These four pieces are independent by design: the Library and the Spreadsheet describe the *same real-world facts* (a bank's FD rate) through two completely separate pipelines that don't talk to each other. That separation is a real, load-bearing trade-off — see Section 9.

**A correction made during this walkthrough:** an early framing of this section said the AI "never invents facts." That's an oversimplification of what's actually an *instruction*, not a *guarantee*. The honest picture has three distinct failure types, all real, all previously observed in this project:
- **Wrong-source-but-faithfully-reported** — the AI accurately reports a real number from the wrong (stale) source, because the source itself was out of date. This is exactly what happened in the HDFC case (Section 8's twin story, detailed in Section 3).
- **Genuine hallucination** — observed once: a "3 years 1 day to 10 years" tenure range that didn't match any real published band, seemingly a distorted merge of two real ranges. Seen once, never reproduced, not root-caused.
- **The safe failure** — "I could not find relevant information," the deliberately engineered fallback when retrieval finds nothing usable (Section 6).

---

## 2. Why This Approach, Not an Alternative

Three real tool choices, each with real alternatives that were not chosen — the goal here is to be honest about *why*, including admitting where the "why" isn't fully known.

**FAISS, not a hosted vector database (Pinecone, Chroma, Weaviate).** FAISS is a local, free, no-server nearest-neighbor search *library* — not a hosted service you sign up for. At this project's real scale (roughly 250 chunks total, not millions), a hosted vector database would be meaningfully more infrastructure than the problem needs: an external account, a network round-trip, a recurring cost, for a search space small enough to hold entirely in memory on a laptop. FAISS fits a project of this size; the hosted alternatives are built for a much bigger one.

**DuckDB, not SQLite, Postgres, or plain CSV files.** DuckDB is an embedded analytical database — no server process to run, a single file on disk, but with a real, rich SQL engine built for exactly the kind of read-heavy, tabular, aggregate-and-filter queries this project's rate comparisons need (rank every bank's rate for a tenure, find every change in the last 7 days). SQLite would have handled the "embedded, one file" part fine but is weaker at analytical queries; Postgres would mean running a real server for a single-user local project; plain CSV files would mean reimplementing basic query logic by hand. DuckDB was the one option that kept "no server to manage" and "real SQL" at the same time.

**Groq, not OpenAI, Anthropic's own API, or a local model.** Groq was chosen for fast, low-cost inference suited to a learning project. Honest gap here: Gemini is confirmed to have been considered and set aside; whether OpenAI's API, Anthropic's API, or a local model were ever formally compared is **not confirmed** — rather than invent a tidy reason, that's stated as genuinely unknown.

**What FAISS actually is, plainly:** not a database, not a hosted service — a library you import into your own Python process that, given a list of vectors (numeric fingerprints of text meaning), can very quickly find "which of these vectors is most similar to this new one." This project uses a separate small embedding model to turn text into those vectors first; FAISS's only job is the fast similarity search afterward.

---

## 3. How Data Actually Flows, Start to Finish

Two genuinely separate pipelines, both starting from a bank's real published rate and ending on your screen — but they never cross.

**Pipeline A — the Library (chat's path):**
A person writes or edits a plain-English `.txt` document for a bank/product → `build_index.py` (or `rebuild_indian_bank_legacy_index.py` for the original Indian Bank pool) reads every document, embeds it, and writes a FAISS index file to disk → the running app loads that index into memory → a user's question gets embedded the same way → FAISS finds the closest-matching chunks → Groq turns those chunks into an answer, constrained to only use what was retrieved.

**Pipeline B — the Spreadsheet (Compare Rates/Calculator's path):**
`fetch_and_track.py` downloads a bank's real page (or, for a handful of permanently-blocked banks, a human manually sources the number once) → `extract_structured.py`/`extract_loan_rates.py` parses the real numbers out (a bank-specific deterministic parser for almost every bank; Groq only for SBI, see Section 5) → the numbers land in DuckDB's `fd_rates`/`loan_rates` tables → Compare Rates, the Calculator, and the digest all read directly from there.

**The follow-up question that mattered:** is Pipeline A's index-build step ("write to disk") actually automatic, or a manual step someone has to remember? It was manual — and pulling on that thread surfaced a **third**, previously undocumented sync gap, on top of the one already known (Pipeline A vs. Pipeline B disagreeing). All three gaps, and what happened to each:

| Gap | What it is | Status |
|---|---|---|
| **#1 — DB vs. RAG-text drift** | Pipeline A and Pipeline B describe the same real fact but nothing forces them to update together. Real instance: a live fetch updated HDFC's structured senior FD rate (7.00%→7.10%) on 2026-08-19; the matching text document was never touched, and chat kept answering the stale 7.00% for weeks. | **Documented, not fixed.** Too large a redesign for this project's scope, per explicit decision. `verify_coverage.py` protects the *numeric* side (a rate change is never silently missed by Compare/the digest) but cannot check whether a document's stated number still agrees with the database. Named honestly in README's Known Limitations. |
| **#2 — Document edited, index never rebuilt** | Someone fixes a document's content but forgets to re-run `build_index.py` — the old, wrong version keeps being served even after the "fix." | **Fixed.** New `verify_index_freshness.py` (4th verify tool) compares each source document's last-modified time against the index's build time and flags anything newer than the index. 3/3 PASS, plus a live check in the app's own sidebar. |
| **#3 — Index rebuilt on disk, but the running server keeps serving the old one from memory** | Streamlit's caching keeps the index loaded once per server process — rebuilding the index file on disk doesn't make a running server pick it up without a full restart. | **Fixed.** The cache key now includes the three index files' real modification times, so a genuine rebuild forces a reload automatically. Verified live: a temporary diagnostic print confirmed the loading function re-executed a second time after a real edit + rebuild + browser-reload-only (no restart), with the cache key changing *only* for the pools actually rebuilt — not a blanket reload. |

---

## 4. What Each Major File Does

Grouped the same way as Section 1's four pieces.

**Front counter:** `app.py` (the whole UI — chat, Compare Rates, Insights & Tools), `settings.py` (central config: file paths, the bank list, default numbers).

**Building/maintaining the Library:** `build_index.py` (rebuilds the 16-bank + RBI FAISS index), `rebuild_indian_bank_legacy_index.py` (rebuilds the original Indian Bank index), `fetch_and_track.py` (downloads a bank's real page), `extract_structured.py`/`extract_loan_rates.py` (turns a messy page into clean database rows — per-bank parser logic lives here), a cluster of `load_<bank>_<product>_legacy.py` one-off scripts for banks that had to be hand-loaded once rather than fetched live.

**The Spreadsheet's own logic:** `db.py` (database schema), `compare_rates.py`/`compare_loan_rates.py` (Compare Rates' ranking logic), `calculator.py` (real EMI/maturity math, no AI), `switch.py` ("Should I Switch?" comparator), `digest.py` ("This Week's Changes" — reads historical snapshots, not live-hash comparisons), `tenure_utils.py` (shared logic for understanding free-text tenure ranges like "3 years 1 day to under 4 years 7 months").

**Extra live feeds:** `news.py` (Economic Times / Business Standard RSS), `rbi_news.py` (RBI's own "What's New" homepage, scraped directly), `iba_news.py` (Indian Banks' Association circulars — wired into the staff-facing "Banker View," not a customer-facing tab; a standalone tab existed once and was removed 2026-08-24 as a redundant duplicate).

**Quality control:** the four `verify_*.py` tools (Section 7) — run by a developer, not part of the live app's request path.

**Two corrections that came out of this section specifically:**

- **`tenure_utils.py` was not always the one shared home for the "8 standard tenure milestones" list.** It was duplicated — once in `app.py`'s own `_TENURE_OPTIONS`, once in `digest.py`'s own copy — until both were consolidated into `tenure_utils.STANDARD_TENURE_MILESTONES` on 2026-08-30, specifically because the HDFC investigation (Section 8) needed one trustworthy definition to scan every bank against. Worth being precise that the consolidation is not fully complete even now: `digest.py` imports the canonical value but re-exports it under its old name, and `app.py`/`calculator.py`/`switch.py` still import it *from `digest.py`*, not directly from `tenure_utils.py`. One real value now, not two — but still a chain of re-exports, not a single clean import everywhere.

- **A genuine mistake, made and then corrected, about which banks are "permanently frozen."** Checking the live database directly (not each loader script's own comments) gives the real, current picture: **Indian Bank** is manually loaded for FD, home loan, and education loan. **BOI** is manually loaded for FD and education loan, and has *zero* home-loan rows at all (a different, plainer gap — not a frozen snapshot, just absent). **SBI** is manually loaded for home loan *only* — its FD rate is genuinely live-fetched. An earlier answer in this same walkthrough also named **Union Bank** as frozen, based on reading `load_union_bank_legacy.py`'s own docstring (which does, truthfully, describe a WAF block — as of when it was written). Checking the actual `fd_rates`/`loan_rates` tables directly showed this was wrong: Union Bank's data is `live_fetch`, not `manual_load_frozen`, across FD, home loan, and education loan — a real fetch succeeded on 2026-08-19, after that script was written, and the WAF block it describes no longer holds. The wrong claim was corrected in README, in `PROJECT_STATUS.md` (with the original wrong entry left visible and a correction entry added right after it, not edited away), and in the loader script's own docstring (which now says plainly that it's superseded). The generalizable lesson, saved to memory: a script's comment describes what was true when it was written, not necessarily what's true in the live system now — that has to be checked directly, every time, even when the script's own reasoning sounds confident and complete.

---

## 5. Where the LLM (Groq) Is Used, and Where It Isn't

The one rule that holds everywhere: **Groq is used for language, never for numbers that end up trusted as-is.**

**Used:**
- Answering chat questions (General Chat, per-bank, RBI Guidelines) — turning retrieved real document chunks into a readable answer, constrained to only use what was retrieved.
- The Calculator's chat shortcut — turning a free-text question ("EMI on 5 lakh for 3 years?") into structured parameters only; the actual EMI number always comes from `calculator.py`'s real formula afterward.
- SBI's structured-data extraction — the *one* bank whose page format defeated every attempt at a deterministic parser, so Groq is used to pull rate rows out as JSON. Every other bank uses a hand-written parser with zero LLM involvement.
- The RAGAS evaluation pipeline — generates the test answer being graded, and (routed through Groq's OpenAI-compatible endpoint) judges it.

**Not used:** all the actual math (Compare Rates, the Calculator's real output, Should I Switch?, the digest's change-detection), retrieval itself (a separate, smaller embedding model does the similarity search — not Groq), the chat shortcuts' intent detection (plain regex pattern matching), all four verify tools (deliberately — determinism and cost matter more than convenience there), and all the news/RBI/IBA scraping (regex/HTML parsing).

**The one real gap found and closed:** SBI's Groq-extracted FD rates had *no* plausibility check on the actual value — only a type check (is it a number or null), not a sanity check (is it a *plausible* rate). A hallucinated 45% for a real 6.8% rate would have passed validation and been stored, indistinguishable from a correct row — the one place an LLM output reached a real number with no extra scrutiny. **Fixed**: a Pydantic validator now rejects any non-null FD rate outside 0–15%, reusing the exact rejection/logging path that already existed for malformed rows. Verified with four real tests: a deliberately implausible 45%/45.5% pair correctly rejected; a real correct SBI rate (6.25%/6.75%) still passes; a genuinely-missing value (`null`) still passes, since the bound shouldn't punish missing data; the boundary value 15.0% itself is correctly accepted.

Confirmed separately: RAGAS's evaluation scope is chat-answers only — its four metrics (faithfulness, answer relevancy, context precision, context recall) never touch Compare Rates, the Calculator, or the digest, since those paths never call an LLM at all, so there's nothing for RAGAS to grade there.

---

## 6. How Errors and Edge Cases Are Handled

Six genuinely different situations, handled in genuinely different ways:

1. **A bank's data doesn't exist at all** (e.g., Kotak/IndusInd's vehicle loan) — never fabricated. Excluded from Compare Rates/Calculator/Should I Switch?, with a hand-written, honest, plain-English reason (`gap_notes.GAP_REASONS`) shown instead of a silent blank.
2. **A bank's site actively blocks automated fetching** — handled entirely upstream, before any user ever asks a question; a human sources the data once, and it's served like any other row from then on. The honesty lives in the documentation, not in a runtime message.
3. **Chat genuinely finds nothing relevant** — a fixed, honest fallback message, deliberately safer than letting the LLM improvise with no real grounding.
4. **The Groq API call itself fails** (rate limit, network error, a genuine bug) — a broad catch-all returns one of two safe messages depending on whether the error looks rate-limit-shaped.
   - **Gap found and fixed:** nothing was ever *recorded* when this failed — no logging anywhere in `app.py`. A real bug would have been indistinguishable from a rate limit, with zero trail to debug from. Fixed by logging the real exception type, message, and full traceback to the server console *before* returning the same unchanged safe message to the user — separating what the user sees from what a developer can diagnose from. Verified with a real test: the actual function, extracted from `app.py`, run against three different fake failures (a `KeyError`, a rate-limit-shaped `Exception`, a `ConnectionError`) — all three showed full real detail in the captured console output, while the return value stayed the identical safe string each time.
5. **A bad number from an LLM extraction** — caught upstream, before it ever reaches a user (Section 5's bounds check).
6. **Bugs caught before any user ever sees them** — the four verify tools (Section 7), run by a developer, not live.

**A fifth category, found only by asking the question directly, and still an open gap:** none of the above — and none of the four verify tools — cover a bug where the backend data and logic are completely correct but the *screen* renders it wrong. This already happened once, for real: Insights & Tools and Compare Rates once showed whichever bank's profile theme was last selected in the sidebar, even though neither panel is bank-specific at all. That bug's fix was, at the time, verified only by code review — not a live click-through, due to browser-automation friction in this environment. It was later **re-verified live**, in this same conversation, once that friction happened not to apply: selected a themed bank (ICICI), confirmed its real red/orange header, toggled Compare Rates and Insights & Tools on — both correctly fell through to the neutral header — then toggled back off and confirmed ICICI's theme correctly returned. Real screenshots at every step. This class of bug is now named plainly in README's Known Limitations: caught only through manual/live review, no automated test for it, and none built, since a tool that inspects actual rendered UI state is a meaningfully different kind of check than the other four.

---

## 7. How We Evaluate Whether the System Works — the Four Verification Tools

Four tools, each answering a genuinely different question — not four flavors of the same test.

| Tool | Question it answers | Current real result |
|---|---|---|
| `verify_all.py` | Does the right document even *contain* a usable number? | 186/186 PASS |
| `verify_retrieval.py` | If the content exists, does it actually *win* against competing documents for a realistic question? | 300 checks, 11 known milder cases |
| `verify_coverage.py` | Can Compare Rates and the digest actually *see* every real rate/band sitting in the database? | 200/200 PASS |
| `verify_index_freshness.py` | Is the searchable index actually *current* with the documents it was built from? | 3/3 PASS + a live sidebar check |

**A number that needed reconciling:** an earlier reference to "27 milder General Chat issues" doesn't match the "11" figure used elsewhere. Both are real, and 11 is the current, correct total — not a different subset with the rest quietly still open. The 27 was General-Chat-only, and dropped in two real steps: 27→17 by fixing a false-negative bug *in the verification script itself* (a dict comprehension silently overwriting a correct result), and 17→10 by a real structural fix (the same "shared fixed-size window" bug from Section 8, applied to a different function). Separately, 1 unrelated bank-scoped case (`indian_bank`/`personal_loan`) was never part of the 27 to begin with. 10 + 1 = 11, re-confirmed as the same 11 cases in the most recent regression check.

**A gap named honestly, not built:** none of the four tools catch a UI-wiring bug (Section 6). And, until this walkthrough, no tool proactively checked for the *specific architectural pattern* behind Section 8's case study recurring a third time somewhere else in the code — the first two occurrences were each found reactively, by recognizing a repeating symptom, not by re-reading the fixed code afterward. This led to building a fifth, standalone script (not one of the "four," since it surfaces candidates for human judgment rather than pass/failing anything): `scan_search_windows.py`.

- **Pass 1** finds every literal FAISS `.search(` call site and flags nearby lines shaped like a small fixed window or a `search_k`-style name.
- **A real blind spot was found on the very first run**: Pass 1 correctly flagged the known `retrieve()` bug and its test-script replica, and surfaced one real, previously-undiscussed third instance (a dead-code duplicate sitting in the unused `app_vittam_backup.py`) — but it completely missed `retrieve_general()`'s `_CANDIDATE_WINDOW`, the exact second known case, because that constant is passed into a helper function rather than sitting next to a literal `.search(` call.
- **Pass 2** was added specifically to close that gap: an independent, proximity-free search for the constant *names* themselves, anywhere in the codebase. Re-run against known ground truth confirmed: `retrieve_general()`'s case is now correctly flagged, the two previously-correct flags are unchanged, and no new false positives were introduced beyond the script's own self-referential comments.

---

## 8. A Real Case Study — the `search_k` Bug

**Symptom:** PNB's chat said *"I could not find relevant information"* for its own personal loan rate, even though the correct document existed and was well-written.

**First attempt, treated as a content problem:** rewrote PNB's document, confirmed fixed. Same for Punjab & Sind Bank's.

**The twist:** a full re-check of every bank afterward found IOB — untouched, previously fine — now failing the *same* way on its own home-loan document. A genuine content bug in one bank's document cannot make a different, untouched bank start failing; that's the signal something structural was wrong.

**Root cause:** a bank-specific question doesn't search "that bank's documents" — it searches the *entire* multi-bank pool, keeps only a small fixed number of the best-scoring matches (16, shared across all 17 banks), and *only then* discards anything not from the requested bank. Strengthening one bank's document could, structurally, always risk pushing a different bank's document out of that shared small window.

**Fix:** when a question is scoped to one specific bank, search the *entire* pool (cheap — only ~215 chunks total) and filter to that bank afterward, instead of taking a small window from the whole pool first.

**Verification:** the repeated fix-one-break-another cycle stopped; a full re-check across all banks/products showed zero of these structural failures remaining, down from 13 at the start.

**The honest complication:** this exact bug shape recurred, independently, weeks later, in a completely different function — General Chat's own cross-bank path had its own separate small fixed window, found only because the same whack-a-mole symptom reappeared and was recognized from memory of the first bug, not because anyone had gone back and checked. (See Section 7 for what came out of asking *why* that proactive check didn't happen, and what now exists instead.)

**The generalizable lesson:** if fixing one thing keeps breaking a different thing, in a repeating cycle, stop patching content and look for a small shared resource everything is quietly competing over.

---

## 9. The Trade-offs Made Along the Way

- **Three separate systems (Library, Spreadsheet, search index) instead of one unified one** — chosen for clarity of ownership, paid for in sync-drift risk. Two of the three resulting gaps are now closed with real automated checks (Section 3); one (DB vs. RAG-text drift) remains an open, honestly-documented risk.
- **A custom parser per bank, instead of one generic AI extractor for everyone** — slower, more repetitive engineering work, chosen specifically to keep an LLM out of the business of producing numbers at scale. SBI is the one deliberate exception, which is exactly why Section 5's bounds-check mattered.
- **Accepting "we can't automate this bank," rather than working harder to defeat a bot-defense** — a practical and ethical boundary, not a technical limit. The real cost: those banks' data has to be maintained by hand and doesn't refresh on its own (see Section 4's corrected list of which banks this actually applies to).
- **Eight standard tenure buckets plus a separate "Special/Additional" section**, instead of showing every real band or approximating everything to the nearest bucket — trades a little visual simplicity in the primary view for real accuracy everywhere, at the cost of a second, less-visible place to look. This is the direct structural product of the HDFC investigation.
- **Truncating what General Chat sees to ~500 characters per document**, to keep a many-bank comparison inside Groq's real per-minute token limit — trades some per-bank completeness for the ability to compare many banks in one answer at all. A rate stated late in a document can genuinely be missed in this one mode, even though the same document works fine when asked about that bank specifically.
- **Building verification tools reactively, around real incidents, instead of one exhaustive test plan written up front** — every tool that exists earns its place against something that actually broke, which is a real strength, but it also means coverage lags one step behind whatever hasn't failed yet. The UI-wiring gap (Section 6) and the search-window-scanner's own first-run blind spot (Section 7) are both direct examples of that lag showing up in practice, not hypotheticals.

---

*Compiled 2026-09-01 from a 9-section walkthrough delivered across this project's working sessions. For the chronological build history, see `PROJECT_STATUS.md`. For user-facing features, setup, and the current list of known limitations, see `README.md`.*
