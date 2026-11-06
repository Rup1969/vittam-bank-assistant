# FD Rate Verification Sheet

Manually-checked FD rates, cross-referenced against each bank's own live
page, Compare Rates, and (where retrieval is the thing being checked)
what bank-scoped chat would actually retrieve. Built so rate claims in
this project don't have to be taken on trust — every row below was
checked against a live fetch at the time it was added.

## Part 1 — SBI, HDFC, Bank of Baroda: 1/3/5-year FD rates (2026-11-05)

**Status note (2026-11-06): the 4 MISMATCHes recorded below were all
fixed the same day they were found** — SBI's and BOB's stale
`*_rates_deposit_structured.txt` documents were retired (unique content
moved into the real FD doc first), and HDFC's "common tenures first"
reordering now uses `tenure_utils.tenure_covers()` instead of a text-
prefix match. This section is kept as-is, as the record of what was
found and why — not as a statement of current behavior. See Part 2 for
the next round of issues found and fixed, and `PROJECT_STATUS.md` for
the full before/after detail.

Live fetch: 2026-11-06. Compare Rates values: read live from
`automation.duckdb`. 9 cells checked at the time — 4 MISMATCH, 5 MATCH.

### SBI — [sbi.bank.in/.../retail-domestic-term-deposits](https://sbi.bank.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits)

| Tenure | Live page row (quoted) | Gen / Sr | Compare Rates | What chat would use | Flag |
|---|---|---|---|---|---|
| 1 year | "1 Year to less than 2 years" — Revised Rates w.e.f. 15/12/2025 | 6.25% / 6.75% | 6.25% / 6.75% | `sbi_rates_deposit_structured` — row missing entirely, jumps from "211 days to <1 year" to "2 years to 3 years" | MISMATCH |
| 3 years | "2 years to less than 3 years" — Revised Rates w.e.f. 15/12/2025 | 6.40% / 6.90% | 6.40% / 6.90% | `sbi_rates_deposit_structured` shows "2 years to 3 years: 6.95%" — wrong general figure; senior shown as placeholder text, not a number | MISMATCH |
| 5 years | "5 years and up to 10 years" — Revised Rates w.e.f. 15/12/2025 | 6.05% / 7.05%* | 6.30%† / 7.05% | `sbi_rates_deposit_structured` shows "5 years to 10 years: 7.05%" labeled as the general-public figure — that's actually SBI's own senior rate | MISMATCH |

\* Senior Citizen rate includes a 0.50% "SBI We-Care" premium per the bank's own footnote.
† At the time, Compare's general-rate pick for a 5-year query was actually SBI's "3 years to less than 5 years" row (6.30%) — it paid a higher general rate than the 5-10yr band's own inclusive boundary under the pre-fix exclusive-bound logic. **Part 2 below found this was itself a bug** (that band should not have covered day 1,825 at all) — Compare's 5-year general figure for SBI is now correctly 6.05%, matching the 5-10yr band directly.

### HDFC Bank — [hdfc.bank.in/fixed-deposit/fd-interest-rate](https://www.hdfc.bank.in/fixed-deposit/fd-interest-rate)

| Tenure | Live page row (quoted) | Gen / Sr | Compare Rates | What chat would use | Flag |
|---|---|---|---|---|---|
| 1 year | "1 Year to < 15 months" — applicable from 4 Nov 2026 | 6.25% / 6.75% | 6.25% / 6.75% | `hdfc_fd_rates` leads with "1 Year to < 15 months: 6.25% / 6.75%" — correct | MATCH |
| 3 years | "2 Years 11 Months 1 day <= 3 Year" — the band that actually contains day 1,095 | 6.55% / 7.05% | 6.55% / 7.05% | `hdfc_fd_rates` leads with "3 Years 1 day to < 4 Years 7 Months: 6.6% / 7.2%" — a different band that starts the day *after* day 1,095, not the one that covers it | MISMATCH |
| 5 years | "4 Year 7 Months 1 day <=5 Years" — the band that actually contains day 1,825 | 6.60% / 7.20% | 6.60% / 7.20% | `hdfc_fd_rates` leads with "5 Years 1 day to 10 Years: 6.6% / 7.1%" — general matches by coincidence, senior is wrong (7.1% vs the real 7.2%) | MISMATCH |

HDFC's own bands have no exact "3 years" or "5 years" point — each boundary sits inside a compound band like "2 Years 11 Months 1 day". Compare Rates finds the right one using real day-count math (`tenure_covers()`); the FD document's old "common tenures first" reordering used a simpler text match that picked HDFC's neighboring band instead, on both tenures. Fixed same day.

### Bank of Baroda — [bankofbaroda.bank.in/.../fixed-deposits-callable-and-non-callable-upto-ten-crores](https://bankofbaroda.bank.in/interest-rate-and-service-charges/deposits-interest-rates/fixed-deposits-callable-and-non-callable-upto-ten-crores)

| Tenure | Live page row (quoted) | Gen / Sr | Compare Rates | What chat would use | Flag |
|---|---|---|---|---|---|
| 1 year | "1 year" — w.e.f 12-06-2026 | 6.25% / 6.75%* | 6.25% / 6.75% | `bob_loan_against_deposit` — wrong topic entirely, this is BOB's loan-against-FD overdraft page, no deposit rate on it | MISMATCH |
| 3 years | "Above 2 Years and upto 3 Years" / "Above 3 Years and upto 5 Years" — boundary is ambiguous in the bank's own wording | 6.25–6.30% / 6.75–6.90%† | 6.30% / 6.90% | `bob_loan_against_deposit` — wrong topic again | MISMATCH |
| 5 years | "Above 3 Years and upto 5 Years" — ends at exactly 5 years, w.e.f 12-06-2026 | 6.30% / 6.90%* | 6.30% / 6.90% | `bob_rates_deposit_structured` shows "3-5 years: 6.30% / 6.90%" — correct | MATCH |

\* Asterisked footnotes on BOB's own page describe additional 0.10–0.60% premiums for super senior citizens on specific bands — not a different base rate.
† BOB's own wording doesn't cleanly say which band "3 years" belongs to; Compare Rates resolves the tie by taking whichever band pays more at that exact tenure.

### Three things found here

1. **SBI and BOB had a second, unsynced rate document that outranked the real one.** Both had a `*_rates_deposit_structured.txt`, written once as a "mirror" and never wired into sync automation — stale, and in SBI's case actively wrong (a senior rate mislabeled as general). **Fixed 2026-11-06**: both retired, unique content (savings account rate, RD note) moved into the real FD doc.
2. **HDFC's "common tenures first" reordering could pick the wrong band.** Text-prefix match instead of real day-count math. **Fixed 2026-11-06**: now uses `tenure_utils.tenure_covers()`, the same logic Compare Rates uses.
3. **This verification scope was never part of the regression suite.** `verify_retrieval.py` had no "what is the N-year FD rate" check for any bank. **Fixed 2026-11-06**: added as the primary, deterministic `check_fd_rate_shortcut()`, now run for every AUTO-block bank's 1/3/5-year tenure.

One limitation: the "what chat would use" column shows the exact document/passage bank-scoped retrieval hands the LLM, not the LLM's own generated sentence — the Groq key needed for that isn't readable in this session. Where the retrieved passage is wrong or off-topic, the generated answer is very likely wrong too, but that's an inference, not a transcript.

## Part 2 — IndusInd, Kotak, Punjab & Sind at 3 years (2026-11-06)

Found while diagnosing why 3 rows in the "highest 3-year FD rate" ranking
looked wrong. Root cause: `tenure_utils.parse_tenure_range()` treated an
upper bound spelled "less than X" / "under X" / "< X" as **inclusive** of
X — so a band like Kotak's "2 years- less than 3 years" wrongly matched
an exact 3-year (1,095-day) query, when the bank's own wording says it
shouldn't. Fixed the same day (`_EXCLUSIVE_UPPER_RE`); see
`PROJECT_STATUS.md` for the full list of the 51 real bands across 13
banks this turned out to affect, and the 40 Compare Rates cells (across
axis, canara, central_bank, indian_bank, iob, kotak, punjab_sind, sbi)
that changed as a result.

| Bank | Source URL | Live-quoted row (fetched 2026-11-06) | Database value |
|---|---|---|---|
| IndusInd Bank | [indusind.bank.in/.../fixed-deposit-interest-rate.html](https://www.indusind.bank.in/in/en/personal/fixed-deposit-interest-rate.html) | "2 Years to 3 Years" | 7.25% / 7.85% |
| Kotak Mahindra Bank | [kotak.bank.in/.../fixed-deposit-interest-rate.html](https://www.kotak.bank.in/en/personal-banking/deposits/fixed-deposit/fixed-deposit-interest-rate.html) | "3 years and above but less than 4 years" | 6.40% / 6.90% |
| Punjab & Sind Bank | [punjabandsind.bank.in/content/interestdom](https://punjabandsind.bank.in/content/interestdom) | "3 Years < 44 Months" | 5.85% / 6.35% |

All three live-quoted rows matched the database value exactly — no stale
data found here. What was wrong was which row got picked for an exact
3-year query, not the row's own numbers:

- **IndusInd's "2 Years to 3 Years"** turned out to be correct as-is —
  diagnosed, not fixed. Its upper bound is genuinely inclusive of day
  1,095 (the next band, "Above 3 Years up to below 61 Months", starts
  the day after), so no exclusivity fix applies here.
- **Kotak**: before the fix, the 3-year ranking used "2 years- less than
  3 years" (6.80%/7.55%) — wrong, since "less than 3 years" should stop
  at day 1,094. After the fix, it correctly uses "3 years and above but
  less than 4 years" (6.40%/6.90%), confirmed against the live page
  above.
- **Punjab & Sind**: a different bug entirely. Its real DB row for this
  rank was literally `"500"` — no unit at all. `parse_tenure_range()`
  couldn't parse any day/month/year token from it, fell back to
  "matches everything" (0–99,999 days), and so it silently won *every*
  tenure ranking regardless of the actual query. The bank's own live
  page (fetched today) shows the real row as **"500 Days"** — the unit
  was dropped somewhere in this project's own extraction, not published
  ambiguously by the bank. Fixed the symptom (`is_bare_number_label()`
  makes `tenure_covers()` return `False` for it, so it can no longer
  win a ranking it doesn't belong in) but **not** the underlying
  extraction defect — that still needs a fix to whatever wrote this row
  into `automation.duckdb`, which this project doesn't hand-edit. See
  `verify_coverage.py`'s new `check_tenure_label_sanity()`, which FAILs
  on this row specifically until that's done.
