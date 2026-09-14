"""Pydantic schemas for structured extraction (Phase B).

Only the fixed-deposit schema is implemented for now — loan and digital
product schemas are deferred (see Bank_Assistant_Upgrade_Spec.md, Phase B),
kept as commented stubs below so the shape of "what's next" is visible
without pretending they're built.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class FDRateRecord(BaseModel):
    """One row of a bank's fixed-deposit rate table.

    `tenure_label` (free text, e.g. "271 days to <1 year") is the primary
    tenure field rather than a single `tenure_months` integer — real bank FD
    tables use irregular day/month buckets that don't cleanly map to one
    integer (confirmed against SBI/BOB/Canara's actual published tables).
    `tenure_days_min`/`tenure_days_max` are populated only when the label
    parses cleanly into a numeric day range.
    """

    bank_id: str
    product_type: str = "fixed_deposit"
    tenure_label: str
    tenure_days_min: int | None = None
    tenure_days_max: int | None = None
    interest_rate_general: float | None = None
    interest_rate_senior: float | None = None
    min_deposit: float | None = None
    effective_date: str | None = None
    source_url: str
    fetched_at: str
    # Distinguishes a row actually pulled from the bank's live site this
    # run ("live_fetch") from one loaded once by hand from an already-
    # verified source and never re-checked ("manual_load_frozen", e.g.
    # Indian Bank/Union Bank's legacy loaders) — added so this is visible
    # directly on the row itself, not only in extraction_log's free-text
    # detail, which a query against fd_rates alone would never see.
    data_source: str = "live_fetch"
    # For banks whose real page splits rates by deposit amount (a "below
    # Rs. 3 crore" retail table plus a separate bulk/wholesale table at or
    # above that), the parser only ever captures the retail table — this
    # records that table's own upper ceiling (in rupees) so eligibility
    # logic can tell "this rate doesn't apply above Rs. X" from "we don't
    # know the minimum," which used to be conflated (see eligibility.py).
    # None means either no such ceiling is known/confirmed for this bank,
    # or the bank's table genuinely has no amount-based tier at all.
    deposit_ceiling: float | None = None
    # Some banks publish more than one real FD product spanning the same
    # tenure — e.g. SBI's standard retail table AND a separate "Non-Callable
    # Term Deposit (Retail)" table, both covering "1 year" with different
    # rates (confirmed 2026-08-18 audit: 6.25% retail vs 6.55% non-callable,
    # both real, neither fabricated). "retail" is the plain, standard,
    # premature-withdrawal-allowed product every bank has and is what a
    # bare "FD rate" question means by default; "non_callable" marks a
    # variant that forgoes premature withdrawal for a higher rate. Every
    # deterministic per-bank parser only ever captures one table (the
    # retail one, by explicit design — see each parser's module comment),
    # so this only varies for the generic-LLM extraction path (currently
    # just SBI), which is now asked to tag both tables instead of trying
    # (unreliably, per the audit finding) to silently discard the second.
    deposit_variant: str = "retail"

    # Plausibility bound, not just a type check — added because
    # extract_rows()'s generic LLM path (currently only SBI) is the one
    # place a rate value is produced by Groq rather than a deterministic
    # regex parser, and a JSON-shaped hallucination (e.g. "45.0" for a
    # real 6.8% rate) would otherwise pass straight through: it's a valid
    # float, so the type check alone accepts it. No real bank FD in this
    # project has ever published a rate outside roughly 0-15%, so that's
    # the bound; applies to every row built here (deterministic parsers'
    # output included), not just SBI's, since they all funnel through
    # this same validation step and a genuine parser bug producing
    # garbage should be caught the same way.
    @field_validator("interest_rate_general", "interest_rate_senior")
    @classmethod
    def _rate_is_plausible(cls, v: float | None) -> float | None:
        if v is not None and not (0 <= v <= 15):
            raise ValueError(f"implausible FD rate: {v}% (expected 0-15%)")
        return v


class LoanRateRecord(BaseModel):
    """One row of a bank's loan rate table (Stage 3 — home loan first).

    Loan rates differ structurally from FD rates in two ways that shaped
    this schema, both confirmed during the pre-flight check across 13
    banks before any parser was written:

    1. A loan rate is almost never a single fixed number — every bank
       checked quotes either a min-max band or a "benchmark + spread"
       formula (Repo/RLLR/BRLLR/MCLR-linked). `rate_min`/`rate_max` hold
       the resulting band (equal to each other for a genuine single-point
       rate); `benchmark_type`/`benchmark_rate` hold the formula's pieces
       separately when the bank publishes the formula (BOB, PNB) rather
       than (or in addition to) a computed number, so both are available
       rather than only the derived figure.
    2. Loan rates are tiered — by credit score, loan amount slab, or
       employment type — the same way FD rates are tiered by tenure.
       Expect multiple LoanRateRecord rows per bank per loan_type, one
       per tier, exactly like fd_rates already has one row per tenure
       band. `tier_type`/`tier_label` name which axis a given row's band
       applies to; both None means the bank published only one flat rate
       (e.g. HDFC's single Repo+2.50%-to-7.95% formula, no tiering).
    """

    bank_id: str
    loan_type: str = "home_loan"
    rate_type: str  # "floating" | "fixed" | "hybrid"
    benchmark_type: str | None = None  # "repo" | "RLLR" | "BRLLR" | "MCLR" | None
    benchmark_rate: float | None = None  # e.g. 5.25 — needed to compute the
    # effective rate when a bank only publishes a formula (BOB, PNB), not
    # a precomputed number
    rate_min: float
    rate_max: float
    tier_type: str | None = None  # "credit_score" | "loan_amount" | "employment_type" | None
    tier_label: str | None = None  # e.g. "751 and above", "Up to Rs.35 lakh"
    processing_fee: str | None = None  # kept as free text, not float — seen
    # published as a flat % (ICICI: 0.5%), a range, and a fixed rupee amount
    # across different banks; forcing one numeric type would lose real
    # published fee formats rather than normalize them
    max_ltv: float | None = None
    effective_date: str | None = None
    source_url: str
    fetched_at: str
    data_source: str = "live_fetch"


# Deferred — see Bank_Assistant_Upgrade_Spec.md Phase B:
#
# class DigitalProductRecord(BaseModel):
#     bank_id: str
#     product_name: str
#     key_features: list[str]
#     eligibility: str | None
#     source_url: str
#     fetched_at: str
