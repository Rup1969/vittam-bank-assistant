"""Phase B, Stage 3 — structured extraction of loan rates (home loan first).

Run manually, after fetch_and_track.py has recorded at least one changed
"<bank>_home_loan" fetch:

    python automation/extract_loan_rates.py

Deliberately a SEPARATE module from extract_structured.py (which already
covers fd_rates end-to-end) rather than folding loan rates into it —
LoanRateRecord is a genuinely different shape from FDRateRecord (rate
bands tied to a floating benchmark, tiered by credit score/loan amount/
employment type rather than tenure), and extract_structured.py was
already 900+ lines before this was added. All 6 parsers here are
deterministic (regex/table-anchor based), same as most of fd_rates'
parsers by now — no bank's home loan page was tried against the generic
LLM path, since the pre-flight check already confirmed all 6 have a
real, parseable structure.

Every parser was written against REAL fetched content (not guessed from
memory of the live page), and every row this module has ever produced
has been cross-checked against that same real page text before being
reported as done — same discipline as every fd_rates parser in this
project.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

import pdfplumber

AUTOMATION_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUTOMATION_DIR.parents[0]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(AUTOMATION_DIR))

import json  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from db import already_extracted, get_connection, resolve_scraped_path  # noqa: E402
from fetch_and_track import clean_text  # noqa: E402
from schema import LoanRateRecord  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config" / "banks.json"


# ---------------------------------------------------------------------
# BOB — bankofbaroda.bank.in/interest-rate-and-service-charges/retail-
# loans-interest-rates bundles 10+ home-loan variant tables (bob Green
# Home Loan, EWS/LIG/MIG, Home Improvement, Max Savings, ...) all sharing
# the same "Product / Conditions / Repo Rate + Spread / Effective Rate of
# Interest" shape. Anchored on "Baroda Home Loan to Non-Staff members"
# specifically as the representative retail product, stopping before the
# next variant ("Baroda Home Improvement Loan to Non-Staff members").
# BRLLR's current numeric value is published elsewhere on the same page
# ("applicable BRLLR is 7.90%") — captured separately since the
# formula text itself only ever says "BRLLR", never the number.
# ---------------------------------------------------------------------
_BOB_BRLLR_RE = re.compile(r"applicable BRLLR is\s*([\d.]+)%")
_BOB_ROW_RE = re.compile(
    r"For (Salaried|Non-Salaried)\*\s*Repo Rate \+ Spread\s*"
    r"BRLLR\s*[–−-]\s*([\d.]+)%\s*to\s*BRLLR\s*\+\s*([\d.]+)%\s*"
    r"Effective Rate of Interest\s*From\s*([\d.]+)%\s*to\s*([\d.]+)%"
)


def extract_bob_home_loan_rows(text: str) -> list[dict]:
    brllr_match = _BOB_BRLLR_RE.search(text)
    benchmark_rate = float(brllr_match.group(1)) if brllr_match else None

    start = text.find("Baroda Home Loan to Non-Staff members")
    end = text.find("Baroda Home Improvement Loan to Non-Staff members", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _BOB_ROW_RE.finditer(region):
        employment, spread_lo, spread_hi, eff_lo, eff_hi = m.groups()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "BRLLR",
            "benchmark_rate": benchmark_rate,
            "rate_min": float(eff_lo),
            "rate_max": float(eff_hi),
            "tier_type": "employment_type",
            "tier_label": employment,
        })
    return rows


# ---------------------------------------------------------------------
# PNB — pnb.bank.in/Retail-Advances-interst-rate-on-advances-linked-to-
# mclr.html bundles several home-loan-variant tables (plain "HOUSING
# LOAN/HOME LOAN", "HOME LOAN (CRE CATEGORY)", "HOME LOAN (TL PNB MAX
# SAVER)", ...). Anchored on the first, plain "HOUSING LOAN/HOME LOAN"
# section as the representative product. Each row already has the
# COMPUTED floating rate inline ("RLLR+BSP-0.85% (presently 7.25% p.a.)")
# — no separate benchmark lookup needed, unlike BOB. Each row also
# carries two FIXED rates (tenure <=10yrs, tenure >10yrs) alongside the
# floating one, so one input row produces three LoanRateRecord rows.
# ---------------------------------------------------------------------
_PNB_ROW_RE = re.compile(
    r"(800 & above|750 and above|700-749|600-699|PNB Pride\*)\s+"
    r"RLLR\+BSP\s*([+-]\s?[\d.]+)%\s*\(presently\s+([\d.]+)%[^)]*\)\s+"
    r"([\d.]+)%\s+([\d.]+)%"
)


def extract_pnb_home_loan_rows(text: str) -> list[dict]:
    start = text.find("HOUSING LOAN/HOME LOAN ROI STARTING FROM")
    end = text.find("HOME LOAN (CRE CATEGORY)", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _PNB_ROW_RE.finditer(region):
        cibil_band, _spread, floating_rate, fixed_upto10, fixed_above10 = m.groups()
        cibil_band = cibil_band.strip()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "RLLR",
            "rate_min": float(floating_rate),
            "rate_max": float(floating_rate),
            "tier_type": "credit_score",
            "tier_label": cibil_band,
        })
        rows.append({
            "rate_type": "fixed",
            "rate_min": float(fixed_upto10),
            "rate_max": float(fixed_upto10),
            "tier_type": "credit_score",
            "tier_label": f"{cibil_band} (tenure <=10yrs)",
        })
        rows.append({
            "rate_type": "fixed",
            "rate_min": float(fixed_above10),
            "rate_max": float(fixed_above10),
            "tier_type": "credit_score",
            "tier_label": f"{cibil_band} (tenure >10yrs)",
        })
    return rows


# ---------------------------------------------------------------------
# HDFC — homeloans.hdfc.bank.in/ps/home-loans-in-india/interest-rates.
# NOTE: this is a SEPARATE SUBDOMAIN from hdfc.bank.in (which every other
# HDFC content category hit a CloudFront 403 on that same day) — confirmed
# not behind the same block. Simplest structure of any bank in this
# batch: one single formula, no tiering at all.
# ---------------------------------------------------------------------
_HDFC_REPO_RE = re.compile(r"Current applicable Repo Rate\s*=\s*([\d.]+)%")
_HDFC_RATE_RE = re.compile(
    r"Policy Repo Rate \+ ([\d.]+)% to ([\d.]+)%\s*=\s*([\d.]+)% to ([\d.]+)%"
)


def extract_hdfc_home_loan_rows(text: str) -> list[dict]:
    repo_match = _HDFC_REPO_RE.search(text)
    rate_match = _HDFC_RATE_RE.search(text)
    if not rate_match:
        return []
    _spread_lo, _spread_hi, rate_lo, rate_hi = rate_match.groups()
    return [{
        "rate_type": "floating",
        "benchmark_type": "repo",
        "benchmark_rate": float(repo_match.group(1)) if repo_match else None,
        "rate_min": float(rate_lo),
        "rate_max": float(rate_hi),
        "tier_type": None,
        "tier_label": None,
    }]


# ---------------------------------------------------------------------
# ICICI — icici.bank.in/personal-banking/loans/home-loan/home-loan-
# interest-rates has THREE distinct real tables on one page: a
# credit-score-tiered "Special" rate (pre-approved digital journey only),
# a loan-slab-tiered "Standard" rate (the general-purpose one), and a
# fixed-tenure table. All three are extracted, distinguished by
# tier_type. Processing fee (0.5% of loan amount + taxes) is the same
# across all rows on this page, stated once in prose.
# ---------------------------------------------------------------------
_ICICI_SPECIAL_RE = re.compile(
    r"(800|750\s*[–-]\s*800)\s+([\d.]+)%\s+([\d.]+)%"
)
_ICICI_STANDARD_RE = re.compile(
    r"(Up to ₹[\d.]+\s*lakh|₹[\d.]+\s*lakh to ₹[\d.]+\s*lakh|Above ₹[\d.]+\s*lakh)\s+"
    r"([\d.]+)%\s*[–-]\s*([\d.]+)%\s+([\d.]+)%\s*[–-]\s*([\d.]+)%"
)
_ICICI_FIXED_RE = re.compile(
    r"(24 Months|37 Months|60 Months|120 Months|Full Term)\s+"
    r"([\d.]+)%\s*[–-]\s*([\d.]+)%"
)
_ICICI_FEE = "0.5% of the loan amount + applicable taxes"


def extract_icici_home_loan_rows(text: str) -> list[dict]:
    rows = []

    special_start = text.find("Special Home Loan Interest rates")
    special_end = text.find("Standard Home Loan interest rate")
    if special_start != -1 and special_end != -1:
        region = text[special_start:special_end]
        for m in _ICICI_SPECIAL_RE.finditer(region):
            band, salaried, self_emp = m.groups()
            band = band.replace("–", "-").strip()
            for emp_type, rate in (("Salaried", salaried), ("Self-Employed", self_emp)):
                rows.append({
                    "rate_type": "floating",
                    "benchmark_type": "repo",
                    "rate_min": float(rate),
                    "rate_max": float(rate),
                    "tier_type": "credit_score",
                    "tier_label": f"{band} ({emp_type}, pre-approved digital only)",
                    "processing_fee": _ICICI_FEE,
                })

    standard_start = text.find("Standard Home Loan interest rate")
    standard_end = text.find("Fixed home loan interest rates")
    if standard_start != -1 and standard_end != -1:
        region = text[standard_start:standard_end]
        for m in _ICICI_STANDARD_RE.finditer(region):
            slab, sal_lo, sal_hi, se_lo, se_hi = m.groups()
            slab = slab.replace("₹", "Rs.").strip()
            for emp_type, lo, hi in (("Salaried", sal_lo, sal_hi), ("Self-Employed", se_lo, se_hi)):
                rows.append({
                    "rate_type": "floating",
                    "benchmark_type": "repo",
                    "rate_min": float(lo),
                    "rate_max": float(hi),
                    "tier_type": "loan_amount",
                    "tier_label": f"{slab} ({emp_type})",
                    "processing_fee": _ICICI_FEE,
                })

    fixed_start = text.find("Fixed home loan interest rates")
    fixed_end = text.find("Home Loan Interest Rates for Non", fixed_start)
    if fixed_start != -1 and fixed_end != -1:
        region = text[fixed_start:fixed_end]
        for m in _ICICI_FIXED_RE.finditer(region):
            tenure, lo, hi = m.groups()
            rows.append({
                "rate_type": "fixed",
                "rate_min": float(lo),
                "rate_max": float(hi),
                "tier_type": "fixed_tenure",
                "tier_label": tenure,
                "processing_fee": _ICICI_FEE,
            })

    return rows


# ---------------------------------------------------------------------
# Kotak — kotak.bank.in/en/personal-banking/loans/home-loan/interest-
# rates.html. Unlike Kotak's car loan (via subsidiary KMPL, no published
# rate) and education loan (via partner Credila, no Kotak-branded rate),
# home loan is originated directly by Kotak Mahindra Bank and DOES
# publish a real number. Simple 3-line table; the third row ("switching
# from Fixed to Adjustable") has no numeric rate, only a policy
# description, and is correctly skipped by the regex not matching it.
# ---------------------------------------------------------------------
_KOTAK_REPO_RE = re.compile(r"Floating Category \(Repo Rate:\s*([\d.]+)%")
_KOTAK_FLOATING_RE = re.compile(
    r"For Salaried & For Self-employed\s*([\d.]+)%\*?\s*p\.a\. onwards"
)
_KOTAK_FIXED_RE = re.compile(
    r"switching from an Adjustable Interest Rate to a Fixed Rate\*\s*"
    r"([\d.]+)%\s*p\.a\. onwards \(Fixed Rate\)"
)


def extract_kotak_home_loan_rows(text: str) -> list[dict]:
    start = text.find("Customer Type")
    end = text.find("Hybrid Interest Rate", start)
    if start == -1:
        return []
    region = text[start:end if end != -1 else start + 1000]

    repo_match = _KOTAK_REPO_RE.search(region)
    benchmark_rate = float(repo_match.group(1)) if repo_match else None

    rows = []
    floating_match = _KOTAK_FLOATING_RE.search(region)
    if floating_match:
        rate = float(floating_match.group(1))
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "repo",
            "benchmark_rate": benchmark_rate,
            "rate_min": rate,
            "rate_max": rate,
            "tier_type": "employment_type",
            "tier_label": "Salaried & Self-Employed",
        })
    fixed_match = _KOTAK_FIXED_RE.search(region)
    if fixed_match:
        rate = float(fixed_match.group(1))
        rows.append({
            "rate_type": "fixed",
            "rate_min": rate,
            "rate_max": rate,
            "tier_type": None,
            "tier_label": "Existing customers switching Adjustable to Fixed",
        })
    return rows


# ---------------------------------------------------------------------
# Axis — axis.bank.in/loans/home-loan/interest-rates-charges. Same clean
# CIBIL-tiered format as Axis's already-built LAP/education-loan docs
# (2 bands: 751+, and below-750/no-history). Also has a separate,
# single flat "Fixed Home Loans" rate (14.00% p.a.) that applies across
# all products other than "Vanilla" — extracted as a third row.
# ---------------------------------------------------------------------
_AXIS_ROW_RE = re.compile(
    r"(Cibil Score - 751 and above|Between 750 and below / No credit history)\s+"
    r"REPO \+ ([\d.]+)% to REPO \+ ([\d.]+)%\s+"
    r"([\d.]+)% to ([\d.]+)% p\.a\."
)
_AXIS_FIXED_RE = re.compile(
    r"Fixed Home Loans come with a competitive fixed interest rate of\s*([\d.]+)%"
)


def extract_axis_home_loan_rows(text: str) -> list[dict]:
    start = text.find("Interest Rates on Asha Home Loans")
    end = text.find("For Home Loan Rate of Interest Disbursed", start)
    if start == -1:
        return []
    region = text[start:end if end != -1 else start + 1500]

    rows = []
    for m in _AXIS_ROW_RE.finditer(region):
        cibil_band, _spread_lo, _spread_hi, eff_lo, eff_hi = m.groups()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "repo",
            "rate_min": float(eff_lo),
            "rate_max": float(eff_hi),
            "tier_type": "credit_score",
            "tier_label": cibil_band.replace("Cibil Score - ", ""),
        })

    fixed_match = _AXIS_FIXED_RE.search(text[start:end + 500 if end != -1 else len(text)])
    if fixed_match:
        rate = float(fixed_match.group(1))
        rows.append({
            "rate_type": "fixed",
            "rate_min": rate,
            "rate_max": rate,
            "tier_type": None,
            "tier_label": "Fixed Home Loan (all variants other than Vanilla)",
        })
    return rows


# ---------------------------------------------------------------------
# IndusInd — found via the bounded one-attempt-per-bank search (not part
# of the original 6 confirmed-clean banks): the home loan product page
# only links out to a shared "know more about APR data" page rather than
# showing the table inline (same off-page pattern as its own LAP page
# needed, see extract_axis_rows-style precedent in extract_structured.py
# for a different bank's PDF case). Single-row Min/Max/Average format,
# same shape IndusInd already uses for other loan products' APR
# disclosure. This page ALSO needed its own settle-wait retuning
# (8000ms, not the 5000ms tuned for IndusInd's FD-rates page) — see
# fetch_source()'s comment in fetch_and_track.py.
# ---------------------------------------------------------------------
_INDUSIND_ROW_RE = re.compile(
    r"Home Loan Interest Rate\s*APR Data for Q1 FY27\s*"
    r"Bank ROI\s*APR\s*Min\s*Max\s*Average\s*Min\s*Max\s*"
    r"([\d.]+)%\s*([\d.]+)%\s*([\d.]+)%\s*([\d.]+)%\s*([\d.]+)%"
)


def extract_indusind_home_loan_rows(text: str) -> list[dict]:
    m = _INDUSIND_ROW_RE.search(text)
    if not m:
        return []
    roi_min, roi_max, _roi_avg, _apr_min, _apr_max = m.groups()
    return [{
        "rate_type": "floating",
        "rate_min": float(roi_min),
        "rate_max": float(roi_max),
        "tier_type": None,
        "tier_label": None,
    }]


# ---------------------------------------------------------------------
# IOB — iob.bank.in/en/lending-interest-rates1. RE-SEARCHED 2026-08-19,
# reversing the 2026-08-17 bounded-search miss on this SAME URL (27
# tables, previously reported as having no housing/home/RLLR match —
# either a since-added table or an earlier miss on a large page; this
# session's real Playwright fetch confirms the table genuinely exists
# now). Clean 6-tier CIC risk-grade table, Salaried/Non-Salaried columns,
# RLLR-linked, ends before the next scheme ("IOB HARIT SUBHAGRUHA").
# ---------------------------------------------------------------------
_IOB_ROW_RE = re.compile(
    r"(CIC-\d \([^)]*\)(?:\s*&\s*NTC)?)\s+RLLR\s*[+-]\s*[\d.]+\s+([\d.]+)\s+"
    r"RLLR\s*[+-]\s*[\d.]+\s+([\d.]+)"
)


def extract_iob_home_loan_rows(text: str) -> list[dict]:
    start = text.find("Risk Grade Interest RATE ROI")
    end = text.find("IOB HARIT SUBHAGRUHA", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _IOB_ROW_RE.finditer(region):
        risk_grade, salaried, non_salaried = m.groups()
        for emp_type, rate in (("Salaried", salaried), ("Non-Salaried", non_salaried)):
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "RLLR",
                "rate_min": float(rate),
                "rate_max": float(rate),
                "tier_type": "credit_score",
                "tier_label": f"{risk_grade} ({emp_type})",
            })
    return rows


# ---------------------------------------------------------------------
# Central Bank of India — centralbank.bank.in/retail-scheme-interest.
# LOAN RATES EXTENSION (2026-08-19). Simple numbered scheme list, NOT
# CIBIL-tiered on this page (asterisk notes CIC-based pricing exists but
# isn't broken out numerically here) — every scheme already has a
# precomputed "Present Range" (Lo% to Hi%), no benchmark math needed.
# Scoped to the 4 schemes whose name contains "Home" (items 1-4 of a
# ~20-item list that also covers vehicle/personal/education/gold loans
# in the identical row shape) — Item 5 "Cent Top Up" is a generic
# top-up product, not home-loan-specific, so excluded from this scope.
# ---------------------------------------------------------------------
_CENTRAL_BANK_HOME_LOAN_ROW_RE = re.compile(
    r"\d+\.\s*(.+?)\*?\s*(Repo|RBLR)\b.*?([\d.]+)%\s*to\s*([\d.]+)%"
)


def extract_central_bank_home_loan_rows(text: str) -> list[dict]:
    start = text.find("S N Scheme Name")
    end = text.find("5. Cent Top Up", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _CENTRAL_BANK_HOME_LOAN_ROW_RE.finditer(region):
        name, benchmark, lo, hi = m.groups()
        name = re.sub(r"[@*\s]+$", "", name).strip()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "repo" if benchmark == "Repo" else benchmark,
            "rate_min": float(lo),
            "rate_max": float(hi),
            "tier_type": "scheme",
            "tier_label": name,
        })
    return rows


# ---------------------------------------------------------------------
# Punjab & Sind Bank — punjabandsind.bank.in/content/prlr. LOAN RATES
# EXTENSION (2026-08-19). The housing-loan product page itself has no
# rate table (only a "click here for rate of interest" link) — this
# PRLR page has SIX separate CIBIL-tiered home-loan-family tables
# (Apna Ghar Premium, PSB Apna Ghar & Kisan Home Loan, Apna Ghar Sahaj,
# Apna Ghar Gaurav Part-A/B, E Apna Ghar Digital), all sharing the same
# 7-tier shape. Anchored on "PSB Apna Ghar & Kisan Home Loan" — the
# flagship general-purpose product matching the RAG doc's own product
# name ("PSB Apna Ghar") — same one-representative-table convention as
# BOB/PNB's parsers.
# ---------------------------------------------------------------------
_PUNJAB_SIND_HOME_LOAN_TIER_RE = re.compile(
    r"(825 & above|791-824|750-790|725-749|700-724|650-699\*|649 & Below#)\s+"
    r"5\.25\s+2\.10\s+[\d.]+\s+[\d.]+\s+([\d.]+)"
)


def extract_punjab_sind_home_loan_rows(text: str) -> list[dict]:
    start = text.find("PSB Apna Ghar & Kisan Home Loan")
    end = text.find("PSB Apna Ghar Sahaj", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _PUNJAB_SIND_HOME_LOAN_TIER_RE.finditer(region):
        tier, rate = m.groups()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "EBLR",
            "benchmark_rate": 5.25,
            "rate_min": float(rate),
            "rate_max": float(rate),
            "tier_type": "credit_score",
            "tier_label": tier.replace("*", "").replace("#", "").strip(),
        })
    return rows


# ---------------------------------------------------------------------
# Bank of Maharashtra — bankofmaharashtra.bank.in/retail-interest-rates.
# LOAN RATES EXTENSION (2026-08-19). The home-loan product page itself
# only shows a single headline "7.00% p.a." rate — traced via its own
# "More Interest Rates & Charges" link to this real tiered page. Real
# 8-row CIBIL-tiered table (800+ down to below-600, plus a separate
# "-1 to 05/NTC" no-credit-history row), Salaried/Non-Salaried columns
# sharing the same tiers, RLLR benchmark for both.
# ---------------------------------------------------------------------
_BOM_HOME_LOAN_TIER_RE = re.compile(
    r"(800 & above|750 to 799|725 to 749|700 to 724|650 to 699|"
    r"600 to 649|below 600|-1 to 05 */NTC)\s+RLLR\s*[+-]?[\d.]*%\s+([\d.]+)%\s+"
    r"RLLR\s*[+-]?[\d.]*%\s+([\d.]+)%"
)


def extract_bom_home_loan_rows(text: str) -> list[dict]:
    start = text.find("Housing loan to General Public")
    end = text.find("Housing Loan to Salaried Employees", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _BOM_HOME_LOAN_TIER_RE.finditer(region):
        tier, salaried, non_salaried = m.groups()
        for emp_type, rate in (("Salaried", salaried), ("Non-Salaried", non_salaried)):
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "RLLR",
                "rate_min": float(rate),
                "rate_max": float(rate),
                "tier_type": "credit_score",
                "tier_label": f"{tier} ({emp_type})",
            })
    return rows


# ---------------------------------------------------------------------
# UCO Bank — uco.bank.in/repo (same URL also serves as the FD-rate
# benchmark page). LOAN RATES EXTENSION (2026-08-19). Real 8-tier table
# using UCO's own loyalty-tier naming (Super Platina/Platina/Premia/
# Optima) tied to CIBIL-or-equivalent score bands, under section
# "13 HOME LOAN > a Individuals" — deliberately scoped to just this
# individual/CIBIL-tiered table, excluding the separate "b UCO Corporate
# Home Loan Scheme" (Internal Credit Rating tiered, not CIBIL) and
# "c UCO TOP UP HOME LOAN" sections that follow in the same numbered list.
# ---------------------------------------------------------------------
_UCO_HOME_LOAN_TIER_RE = re.compile(
    r"For Home Loan \(Borrowers with Cibil Score(?: or Equivalent)?\s*([^)]+)\)"
    r"(?:\s*\([^)]*\))?\s+[\d.]+%\s+-?[\d.]+%\s+([\d.]+)%"
)


def extract_uco_home_loan_rows(text: str) -> list[dict]:
    start = text.find("13 HOME LOAN")
    end = text.find("b Uco Corporate Home Loan Scheme", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _UCO_HOME_LOAN_TIER_RE.finditer(region):
        tier, rate = m.groups()
        rows.append({
            "rate_type": "floating",
            "tier_type": "credit_score",
            "tier_label": f"CIBIL {tier.strip()}",
            "rate_min": float(rate),
            "rate_max": float(rate),
        })
    return rows


# ---------------------------------------------------------------------
# Union Bank of India — unionbankofindia.bank.in/pdf/retail_roi.pdf.
# LOAN RATES EXTENSION (2026-08-19): the earlier Stage 3 bounded search
# (2026-08-17) found this exact URL but it returned HTTP 500 with an
# invalid PDF body ("No /Root object!") — logged as "found but currently
# broken." Retried this session: now fetches cleanly (443KB, real
# content, 12 pages). NOT wired into config/banks.json's normal urls
# loop — fetch_and_track.py's `is_pdf` flag is currently bank-level and
# scoped only to the "fd_rates" url_type (so it wouldn't apply to a
# "home_loan" url_type without a larger per-url_type refactor, out of
# scope here) — fetched and tracked as a one-off instead, with its
# fetch_log row inserted directly to slot into the same extraction flow
# every other bank uses.
#
# Table-aware pdfplumber extraction needed (not flattened text) — same
# reason as Axis's PDF, and confirmed necessary here too: this bank's
# real "UNION HOME / AWAS / REPAIR & RENOVATION" table splits by CIC
# score x Salaried/Non-Salaried x Male/Female, and several of the
# middle tiers (750-799, 700-749, 680-699) print FOUR rate values on
# what flattens to one ambiguous line with no reliable way to attribute
# which number belongs to which of the four borrower-type columns after
# flattening — rather than guess, only tiers where exactly ONE rate
# value is present in the row (i.e. every borrower type gets the same
# rate at that tier: 825+, 800-824, 650-679, 600-649) are extracted from
# the table directly. The full published range (7.15%-9.35%) is
# additionally taken from the document's own summary table ("INTEREST
# RATE RANGE ON LOANS... Home Loan 7.15% to 9.35%, Mean 8.25%") — its
# minimum (7.15%) independently cross-checks against the 825+ tier's
# rate extracted above, both agreeing exactly.
# ---------------------------------------------------------------------
_UNION_BANK_TIER_RE = re.compile(r"^\d{3}\s*(?:&\s*above|to\s*\d{3})$")
_UNION_BANK_RATE_RE = re.compile(r"=\s*([\d.]+)%")
_UNION_BANK_RANGE_RE = re.compile(r"Home Loan\s+([\d.]+)%\s*to\s*([\d.]+)%")


def extract_union_bank_home_loan_rows(file_path: str) -> list[dict]:
    """Takes file_path, not text — needs pdfplumber's table-aware
    extraction directly on the PDF, not flattened text (see module
    comment above)."""
    rows = []
    with pdfplumber.open(file_path) as pdf:
        tables = pdf.pages[0].extract_tables()
        data_table = tables[1] if len(tables) >= 2 else []

        for row in data_table:
            cells = [c for c in row if c]
            joined = " ".join(c.replace("\n", " ") for c in cells)
            rate_matches = _UNION_BANK_RATE_RE.findall(joined)
            if len(rate_matches) != 1:
                continue  # ambiguous (multiple borrower-type columns share this row) — skip rather than misattribute
            tier_label = next(
                (c.strip() for c in cells if _UNION_BANK_TIER_RE.match(c.strip())), None
            )
            if tier_label is None:
                continue
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "EBLR",
                "rate_min": float(rate_matches[0]),
                "rate_max": float(rate_matches[0]),
                "tier_type": "credit_score",
                "tier_label": f"{tier_label} (all borrower types)",
            })

        for page in pdf.pages:
            m = _UNION_BANK_RANGE_RE.search(page.extract_text() or "")
            if m:
                rows.append({
                    "rate_type": "floating",
                    "benchmark_type": "EBLR",
                    "rate_min": float(m.group(1)),
                    "rate_max": float(m.group(2)),
                    "tier_type": None,
                    "tier_label": (
                        "Full published range across every CIC-score/gender/"
                        "employment tier and scheme variant (Union Home/Awas/"
                        "PMAY-U/Green Home/Smart Save)"
                    ),
                })
                break

    return rows


# ---------------------------------------------------------------------
# Canara Bank — canarabank.bank.in/pages/rates-of-interest-for-retail-
# lending-schemes-linked-to-rllr. LOAN RATES EXTENSION (2026-08-19),
# previously "not yet assessed" (see PROJECT_STATUS.md §5b — Canara was
# never checked in the original Stage 3 pre-flight, a real gap found
# while building this, not previously known). Found via the housing-loan
# product page's own "INTEREST RATES" link, then one more hop from a
# navigation-hub page to this RLLR-specific variant (a sibling
# Fixed-Rate page exists too, out of scope).
#
# Richest structure of any bank in this project: CRG-PRIME/CRG:1-4 tiers
# (a Credit Risk Group system, not a raw CIBIL score) x Women/Other-
# borrower columns x 4 loan-amount slabs (up to 50L, 50-100L, 100-250L,
# above 250L). Scoped to the first slab (up to Rs. 50 lakh) as the
# representative tier — matches the "most retail borrowers" convention
# used elsewhere (BOB anchors on one product variant out of 10+; ICICI/
# PNB tier by CIBIL, not loan-amount x CIBIL combined) — capturing all 4
# slabs x 5 tiers x 2 genders (40 rows) for one bank would be
# disproportionate versus every other bank's scope in this table.
#
# Also found evidence during this extension that Canara's `interactions`
# config (a popup-dismiss + accordion click, needed for its fd_rates
# page) does NOT apply to this home_loan page at all — the accordion
# selector simply isn't there — leading to a real fix in
# fetch_and_track.py: `interactions` is now scoped to fd_rates only,
# like `content_type`, rather than assumed site-wide like `wait_until`.
# ---------------------------------------------------------------------
_CANARA_HOME_LOAN_ROW_RE = re.compile(
    r"(CRG-PRIME|CRG\s*:\s*\d)\s+[\d.]+\s+[\d.]+\s+-?[\d.]+\s+([\d.]+)\s+"
    r"[\d.]+\s+-?[\d.]+\s+([\d.]+)"
)


def extract_canara_home_loan_rows(text: str) -> list[dict]:
    start = text.find("APPLICABLE ROI FOR SANCTIONED LIMIT UPTO Rs. 50.00 LAKH")
    end = text.find("APPLICABLE ROI FOR SANCTIONED LIMIT ABOVE RS. 50.00 LAKH", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _CANARA_HOME_LOAN_ROW_RE.finditer(region):
        tier, women_rate, other_rate = m.groups()
        tier = re.sub(r"\s*:\s*", " ", tier).strip()
        for gender, rate in (("Women Borrowers", women_rate), ("Other Borrowers", other_rate)):
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "RLLR",
                "benchmark_rate": 8.00,
                "rate_min": float(rate),
                "rate_max": float(rate),
                "tier_type": "credit_score",
                "tier_label": f"{tier} ({gender}, loan up to Rs. 50 lakh)",
            })
    return rows


_PARSERS = {
    "bob": extract_bob_home_loan_rows,
    "pnb": extract_pnb_home_loan_rows,
    "hdfc": extract_hdfc_home_loan_rows,
    "icici": extract_icici_home_loan_rows,
    "kotak": extract_kotak_home_loan_rows,
    "axis": extract_axis_home_loan_rows,
    "indusind": extract_indusind_home_loan_rows,
    "iob": extract_iob_home_loan_rows,
    "central_bank": extract_central_bank_home_loan_rows,
    "punjab_sind": extract_punjab_sind_home_loan_rows,
    "bom": extract_bom_home_loan_rows,
    "uco": extract_uco_home_loan_rows,
    "canara": extract_canara_home_loan_rows,
}

# Banks whose parser takes file_path directly (pdfplumber table-aware
# extraction) instead of the standard cleaned-text string — see
# extract_axis_rows in extract_structured.py for the original precedent.
_PDF_PARSERS = {"union_bank": extract_union_bank_home_loan_rows}

# Union Bank's home_loan URL isn't in config/banks.json's normal per-bank
# "urls" dict — see extract_union_bank_home_loan_rows's module comment
# for why (fetch_and_track.py's PDF handling is currently bank-level,
# scoped to fd_rates only). Source URL kept here instead so
# LoanRateRecord.source_url still gets a real, correct value.
_PDF_SOURCE_URLS = {
    "union_bank": "https://www.unionbankofindia.bank.in/pdf/retail_roi.pdf",
}


def run() -> None:
    con = get_connection()

    changed_rows = con.execute(
        "SELECT source_id, fetched_at, file_path FROM fetch_log "
        "WHERE source_id LIKE '%\\_home_loan' ESCAPE '\\' "
        "AND changed = TRUE AND error IS NULL ORDER BY fetched_at"
    ).fetchall()

    if not changed_rows:
        print("No changed, unextracted home_loan sources found. Run fetch_and_track.py first.")
        con.close()
        return

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    already_done = 0
    for source_id, fetched_at, file_path in changed_rows:
        if already_extracted(con, source_id, fetched_at):
            already_done += 1
            continue

        resolved_path = resolve_scraped_path(file_path)
        if resolved_path is None:
            print(f"{source_id}: snapshot file not found here ({file_path}) "
                  f"— skipping, not fetched on this machine")
            continue

        bank_id = source_id.removesuffix("_home_loan")
        is_pdf_parser = bank_id in _PDF_PARSERS
        parser = _PDF_PARSERS.get(bank_id) or _PARSERS.get(bank_id)
        if parser is None:
            print(f"{source_id}: no parser registered, skipping")
            continue

        if is_pdf_parser:
            parser_input = str(resolved_path)
        else:
            html = resolved_path.read_text(encoding="utf-8")
            parser_input = clean_text(html)

        source_url = next(
            (b["urls"]["home_loan"] for b in config["banks"]
             if b["id"] == bank_id and "home_loan" in b.get("urls", {})),
            _PDF_SOURCE_URLS.get(bank_id),
        )
        if source_url is None:
            print(f"{source_id}: no home_loan URL configured, skipping")
            continue

        try:
            raw_rows = parser(parser_input)
        except Exception as exc:
            print(f"{source_id}: extraction failed — {type(exc).__name__}: {exc}")
            con.execute(
                "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                [source_id, fetched_at, f"Parser failed: {exc}"],
            )
            continue

        if raw_rows:
            con.execute("DELETE FROM loan_rates WHERE bank_id = ? AND loan_type = 'home_loan'", [bank_id])

        accepted, rejected = 0, 0
        for raw in raw_rows:
            try:
                record = LoanRateRecord(
                    bank_id=bank_id,
                    source_url=source_url,
                    fetched_at=str(fetched_at),
                    **raw,
                )
            except ValidationError as exc:
                rejected += 1
                con.execute(
                    "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                    [source_id, fetched_at, f"Validation failed: {exc}"],
                )
                continue

            con.execute(
                "INSERT INTO loan_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    record.bank_id, record.loan_type, record.rate_type,
                    record.benchmark_type, record.benchmark_rate,
                    record.rate_min, record.rate_max,
                    record.tier_type, record.tier_label,
                    record.processing_fee, record.max_ltv,
                    record.effective_date, record.source_url,
                    record.fetched_at, record.data_source,
                ],
            )
            accepted += 1

        con.execute(
            "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
            [source_id, fetched_at, f"{accepted} accepted, {rejected} rejected"],
        )
        print(f"{source_id}: {accepted} rows accepted, {rejected} rejected")

    if already_done == len(changed_rows):
        print(f"Nothing new to extract — all {already_done} already-processed source(s) up to date.")

    con.close()


# =======================================================================
# Education Loan (Batch 1) — 2026-08-19. Same discipline as home loan:
# every parser below built and tested against REAL fetched content, not
# guessed from RAG-doc prose. Efficiency note: all 6 of this batch's
# sources were ALREADY fetched for the home-loan work (multi-product
# rate-card pages that happen to also list education loan rates) — zero
# new fetches needed, just new parsing of already-cached content. See
# `run_education()` below for how this reuses the existing home_loan
# fetch_log rows rather than tracking a separate url_type.
#
# Scoping principle, consistent with every home_loan parser: each bank's
# ONE flagship/most-general education loan product (or a small handful
# of clearly-distinct real products under one umbrella heading), not
# every named variant on the page — several banks publish 3-9+ named
# sub-schemes (institute-tier scholarships, skill loans, medico-specific,
# government-scheme-specific) that would balloon scope disproportionately
# versus every other bank's loan_rates coverage in this project.
# =======================================================================

# ---------------------------------------------------------------------
# Central Bank of India — same `retail-scheme-interest` page already
# fetched for home_loan. Item 20, "Education Loans under various
# Categories" — ALL 6 real rows kept (unlike most banks' partial scope)
# since the page itself already presents them as one cohesive table, not
# several separately-named products. Labels are the bank's own verbose
# prose (kept mostly verbatim rather than force-shortened, to avoid
# losing real distinctions like "with collateral Security").
# ---------------------------------------------------------------------
_CENTRAL_BANK_EDU_RATE_RE = re.compile(r"(Repo|RBLR)\s*([+-])\s*([\d.]+)%\s+([\d.]+)%")


def extract_central_bank_education_rows(text: str) -> list[dict]:
    start = text.find("20. Education Loans")
    end = text.find("Cent Vidyarthi", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    header_end = region.find("Effective ROI")
    if header_end == -1:
        return []
    header_end += len("Effective ROI")

    rows = []
    prev_end = header_end
    for m in _CENTRAL_BANK_EDU_RATE_RE.finditer(region, header_end):
        label = region[prev_end:m.start()].strip()
        benchmark, sign, spread, rate = m.groups()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "repo" if benchmark == "Repo" else benchmark,
            "rate_min": float(rate),
            "rate_max": float(rate),
            "tier_type": "scheme",
            "tier_label": label,
        })
        prev_end = m.end()
    return rows


# ---------------------------------------------------------------------
# Punjab & Sind Bank — same PRLR page already fetched for home_loan.
# "PSB Model Education Loan Scheme" — the flagship, general-purpose
# product (3 loan-amount tiers). The CGFSEL/CGFSSD government-guarantee
# variant and "PSB Skill Loan" that follow on the same page are narrower
# variants, skipped for scope, same convention as skipping Central
# Bank's "Cent Top Up" for its home_loan parser.
# ---------------------------------------------------------------------
_PUNJAB_SIND_EDU_ROW_RE = re.compile(
    r"(Loan up to Rs\.4\.00 Lakh|Loan Above Rs\.4 lakh & up to Rs\.7\.50 Lakh|Above Rs\.7\.50 Lakh)\s+"
    r"5\.25\s+2\.10\s+[\d.]+\s+[\d.]+\s+([\d.]+)"
)


def extract_punjab_sind_education_rows(text: str) -> list[dict]:
    start = text.find("Education Loan Product Information")
    end = text.find("EDUCATION LOAN COVERED UNDER", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _PUNJAB_SIND_EDU_ROW_RE.finditer(region):
        tier, rate = m.groups()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "EBLR",
            "benchmark_rate": 5.25,
            "rate_min": float(rate),
            "rate_max": float(rate),
            "tier_type": "loan_amount",
            "tier_label": f"PSB Model Education Loan Scheme ({tier})",
        })
    return rows


# ---------------------------------------------------------------------
# Bank of Baroda — same `retail-loans-interest-rates` page already
# fetched for home_loan. Two distinct real products kept, mirroring
# ICICI's home_loan precedent of keeping multiple real tables from one
# page: "Baroda Gyan" (domestic study, amount-tiered, incl. its defence-
# personnel special rate) and "PM VIDYALAXMI SCHEME" (5 institute-grade
# tiers, AAA down to C) — the two most general-purpose real tables.
# "Baroda Scholar" (abroad-study) and every other named scheme on this
# page are skipped for scope.
# ---------------------------------------------------------------------
_BOB_EDU_GYAN_RE = re.compile(
    r"(Upto Rs\.[\d.]+ Lakh|Above Rs [\d.]+ lakh|\(Special rate of Interest for Children[^)]*\))"
    r"\s+Repo Rate \+ Spread\s+BRLLR\s*[+-]\s*[\d.]+%\s+Effective Rate of Interest\s+([\d.]+)%"
)
_BOB_EDU_VIDYALAXMI_RE = re.compile(
    r"Conditions\s+(Top 10 IIMs & Top 10 IITs|AA|A|B|C)\s+Repo Rate \+ Spread\s+"
    r"BRLLR\s*[+\-–]?\s*[\d.]*%\s+Effective Rate of Interest\s+([\d.]+)%"
)


def extract_bob_education_rows(text: str) -> list[dict]:
    rows = []

    gyan_start = text.find("Baroda Gyan Conditions")
    gyan_end = text.find("Baroda Executive Development", gyan_start)
    if gyan_start != -1 and gyan_end != -1:
        for m in _BOB_EDU_GYAN_RE.finditer(text[gyan_start:gyan_end]):
            tier, rate = m.groups()
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "BRLLR",
                "rate_min": float(rate),
                "rate_max": float(rate),
                "tier_type": "loan_amount",
                "tier_label": f"Baroda Gyan ({tier.strip()})",
            })

    vidya_start = text.find("PM VIDYALAXMI SCHEME")
    vidya_end = text.find("Baroda Education Loan to Students of Premier Institutions", vidya_start)
    if vidya_start != -1 and vidya_end != -1:
        for m in _BOB_EDU_VIDYALAXMI_RE.finditer(text[vidya_start:vidya_end]):
            tier, rate = m.groups()
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "BRLLR",
                "rate_min": float(rate),
                "rate_max": float(rate),
                "tier_type": "scheme",
                "tier_label": f"PM Vidyalaxmi — Institute Category {tier}",
            })

    return rows


# ---------------------------------------------------------------------
# PNB — same `Retail-Advances...mclr.html` page already fetched for
# home_loan. "PNB SARASWATI" (study-in-India, flagship) — 2 loan-amount
# tiers x Male/Female x Floating/Fixed(<=10yr/>10yr). PNB PRATIBHA
# (premier-institute-specific) and PM VIDYALAXMI (161+ named institutes)
# are skipped for scope, same convention as BOB's Baroda Scholar.
# ---------------------------------------------------------------------
_PNB_EDU_ROW_RE = re.compile(
    r"(Loan irrespective of amount|Loan upto Rs\.7\.50 lakhs \(covered under CGFSEL Scheme\))"
    r".*?\(Presently\s*([\d.]+)\s*%\)\s*([\d.]+)%\s*([\d.]+)%\s*"
    r"RLLR\+BSP[+-]?\s*[\d.]*%\s*\(Presently\s*([\d.]+)\s*%\)\s*([\d.]+)%\s*([\d.]+)%"
)


def extract_pnb_education_rows(text: str) -> list[dict]:
    start = text.find("EDUCATION LOAN FOR STUDY IN INDIA")
    end = text.find("EDUCATION LOAN FOR STUDIES IN PREMIER INSTITUTE", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _PNB_EDU_ROW_RE.finditer(region):
        tier, m_float, m_fixed10, m_fixed10p, f_float, f_fixed10, f_fixed10p = m.groups()
        for gender, floating, fixed10, fixed10p in (
            ("Other than Female Student", m_float, m_fixed10, m_fixed10p),
            ("Female Student", f_float, f_fixed10, f_fixed10p),
        ):
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "RLLR",
                "rate_min": float(floating),
                "rate_max": float(floating),
                "tier_type": "loan_amount",
                "tier_label": f"PNB Saraswati ({tier}, {gender})",
            })
            rows.append({
                "rate_type": "fixed",
                "rate_min": float(fixed10),
                "rate_max": float(fixed10),
                "tier_type": "loan_amount",
                "tier_label": f"PNB Saraswati ({tier}, {gender}, tenure <=10yrs)",
            })
            rows.append({
                "rate_type": "fixed",
                "rate_min": float(fixed10p),
                "rate_max": float(fixed10p),
                "tier_type": "loan_amount",
                "tier_label": f"PNB Saraswati ({tier}, {gender}, tenure >10yrs)",
            })
    return rows


# ---------------------------------------------------------------------
# UCO Bank — same `uco.bank.in/repo` page already fetched for home_loan.
# Two sub-products under "6 Education Loan": (a) "UCO Udaan a IBA"
# (amount-tiered, the IBA-model general scheme) and (c) "Education Loan:
# UCO Udaan" (List A/B/C institute x collateral tiers, the bank's own
# flagship-branded product). "UCO Skill", "Uco Utkarsh", and "Uco Aspire
# 2.0" (a webometrics-ranking-tiered variant) are skipped for scope.
# ---------------------------------------------------------------------
_UCO_EDU_IBA_RE = re.compile(r"(UP TO 7\.5 Lacs|ABOVE 7\.5 Lacs)\s+[\d.]+%\s+([\d.]+)%")
_UCO_EDU_UDAAN_RE = re.compile(
    r"For List '(A|B|C)' Institute with (Nil Collateral|at least 100% collateral|"
    r"less than 100% collateral)\s+[+-]?[\d.]+%\s+([\d.]+)%"
)


def extract_uco_education_rows(text: str) -> list[dict]:
    rows = []

    iba_start = text.find("6 Education Loan: UCO Udaan a IBA")
    iba_end = text.find("b UCO Skill", iba_start)
    if iba_start != -1 and iba_end != -1:
        for m in _UCO_EDU_IBA_RE.finditer(text[iba_start:iba_end]):
            tier, rate = m.groups()
            rows.append({
                "rate_type": "floating",
                "tier_type": "loan_amount",
                "tier_label": f"UCO Udaan IBA ({tier.title()})",
                "rate_min": float(rate),
                "rate_max": float(rate),
            })

    udaan_start = text.find("c Education Loan: UCO Udaan Spread Effective ROI")
    udaan_end = text.find("d Education Loans: Uco Utkarsh", udaan_start)
    if udaan_start != -1 and udaan_end != -1:
        for m in _UCO_EDU_UDAAN_RE.finditer(text[udaan_start:udaan_end]):
            list_grade, collateral, rate = m.groups()
            rows.append({
                "rate_type": "floating",
                "tier_type": "scheme",
                "tier_label": f"UCO Udaan (List '{list_grade}' Institute, {collateral})",
                "rate_min": float(rate),
                "rate_max": float(rate),
            })

    return rows


# ---------------------------------------------------------------------
# Union Bank of India — same manually-retried PDF already used for
# home_loan (`unionbankofindia.bank.in/pdf/retail_roi.pdf`). Table-aware
# pdfplumber extraction (page index 4, first table): "3.1 UNION EDUCATION
# - INLAND STUDY / ABROAD STUDY / NRI STUDENT" — the flagship general
# product, 2 loan-amount tiers x Male/Female Student. Premier Abroad
# (Cat A/B), Skill Development, PM Vidyalaxmi (5 institute grades), ISB,
# Union Medicos, and Tier II (pages 4-5's other 7 tables) are all real
# but skipped for scope, same convention as every other bank this batch.
# ---------------------------------------------------------------------
_UNION_BANK_EDU_RATE_RE = re.compile(r"=\s*([\d.]+)%")


def extract_union_bank_education_rows(file_path: str) -> list[dict]:
    with pdfplumber.open(file_path) as pdf:
        tables = pdf.pages[4].extract_tables()
    if not tables:
        return []
    data_table = tables[0]

    rows = []
    for row in data_table[2:]:  # skip the 2 header rows
        cells = [c for c in row if c]
        if len(cells) < 3:
            continue
        quantum = cells[0].replace("\n", " ").strip()
        male_m = _UNION_BANK_EDU_RATE_RE.search(cells[1])
        female_m = _UNION_BANK_EDU_RATE_RE.search(cells[2])
        if not male_m or not female_m:
            continue
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "EBLR",
            "rate_min": float(male_m.group(1)),
            "rate_max": float(male_m.group(1)),
            "tier_type": "loan_amount",
            "tier_label": f"{quantum} (Male Student)",
        })
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "EBLR",
            "rate_min": float(female_m.group(1)),
            "rate_max": float(female_m.group(1)),
            "tier_type": "loan_amount",
            "tier_label": f"{quantum} (Female Student)",
        })
    return rows


# ---------------------------------------------------------------------
# SBI — education-loan-specific fetch (Batch 2), sbi.bank.in/web/
# personal-banking/loans/education-loans. UNLIKE every other bank in
# this project, the rate isn't in the page's flattened TEXT at all in a
# labeled way — it's rendered by a tabbed rate-carousel widget where each
# product's numbers live in a `<div id="menu-N">` with no visible label
# text nearby (flattening loses the tab-trigger -> panel association
# entirely). Verified via TWO independent real signals before trusting
# it, not a guess: (1) `id="menu-6"` carries `class="... rate_active"` —
# the widget's own default-selected tab on this URL; (2) the page's own
# product-dropdown lists exactly 12 items in a fixed order (Home/
# Personal/Pension/SB Account/Gold/NRE SB/Education/Auto/Fixed Deposit/
# PM Surya Ghar/Loan Against MF/Agriculture), and menu-6 is the 7th
# (0-indexed 6th) — Education Loan's exact position. Both signals agree:
# 6.90% p.a. Operates on RAW HTML (not clean_text()) since the div id
# itself is the only reliable anchor — flattened text loses it. If SBI
# ever reorders this product list, this positional anchor would need
# re-deriving, same caveat class as Axis's date-stamped PDF URL.
# ---------------------------------------------------------------------
_SBI_EDU_RE = re.compile(r"([\d.]+)%\s*<span>\s*p\.a\.\*?\s*</span>")


def extract_sbi_education_rows(html: str) -> list[dict]:
    idx = html.find('id="menu-6"')
    if idx == -1:
        return []
    region = html[idx:idx + 600]
    m = _SBI_EDU_RE.search(region)
    if not m:
        return []
    rate = float(m.group(1))
    return [{
        "rate_type": "floating",
        "rate_min": rate,
        "rate_max": rate,
        "tier_type": None,
        "tier_label": None,
    }]


# ---------------------------------------------------------------------
# Canara Bank — education-loan-specific fetch (Batch 2), the Vidya
# Turant product page. Single flat rate, clearly labeled in plain text
# ("Rate of Interest: 6.85% p.a.") — no ambiguity, no tiering found.
# ---------------------------------------------------------------------
_CANARA_EDU_RE = re.compile(r"Rate of Interest:\s*([\d.]+)%\s*p\.a\.")


def extract_canara_education_rows(text: str) -> list[dict]:
    m = _CANARA_EDU_RE.search(text)
    if not m:
        return []
    rate = float(m.group(1))
    return [{
        "rate_type": "floating",
        "rate_min": rate,
        "rate_max": rate,
        "tier_type": None,
        "tier_label": None,
    }]


# ---------------------------------------------------------------------
# HDFC — education-loan-specific fetch (Batch 2), the dedicated
# `interest-rates-and-charges` page (confirmed fetchable, same
# intermittent-not-permanent CloudFront pattern as every other hdfc.
# bank.in page). Single flat "Starting from 10.50% p.a." for domestic
# education — no tiered table found on this specific page.
# ---------------------------------------------------------------------
_HDFC_EDU_RE = re.compile(r"Interest Rates\s*Starting from\s*([\d.]+)%\s*p\.a\.")


def extract_hdfc_education_rows(text: str) -> list[dict]:
    m = _HDFC_EDU_RE.search(text)
    if not m:
        return []
    rate = float(m.group(1))
    return [{
        "rate_type": "floating",
        "rate_min": rate,
        "rate_max": rate,
        "tier_type": None,
        "tier_label": None,
    }]


# ---------------------------------------------------------------------
# Bank of Maharashtra — education-loan-specific fetch (Batch 2), the
# `educational-loans` product page. Single flat "Interest Rate 6.85 %
# P.A" figure, clearly labeled — no tiering found; the page names 3 real
# sub-schemes (Model Education Loan, Maha Bank Skill Loan, PM-Vidya
# Laxmi) but publishes only this one headline rate directly.
# ---------------------------------------------------------------------
_BOM_EDU_RE = re.compile(r"Interest Rate\s*([\d.]+)\s*%\s*P\.A")


def extract_bom_education_rows(text: str) -> list[dict]:
    m = _BOM_EDU_RE.search(text)
    if not m:
        return []
    rate = float(m.group(1))
    return [{
        "rate_type": "floating",
        "rate_min": rate,
        "rate_max": rate,
        "tier_type": None,
        "tier_label": None,
    }]


# ---------------------------------------------------------------------
# Axis Bank — RE-RESEARCHED 2026-08-20, reversing the §4i pre-flight's
# "no table found" verdict. The dedicated `interest-rates-charges`
# sub-page genuinely has no rate table (checked THREE times now across
# two sessions — a real Playwright fetch, twice — plus a BeautifulSoup
# table scan this pass: 1 table, fees/charges only, no rates). The real
# table lives on the EDUCATION LOAN LANDING page itself
# (`/loans/education-loan`) instead — 3 loan-amount tiers, precomputed
# effective ROI, no benchmark math needed. KNOWN ODDITY, flagged not
# silently resolved: this table's own printed "Repo Rate" column reads
# 6.50% per row, but Axis's Repo Rate is 5.25% everywhere else in this
# project (FD/home_loan parsers) — the effective-ROI figures ARE
# internally self-consistent with THIS table's own 6.50% (6.50+8.70=
# 15.20 checks out arithmetically), so used as printed rather than
# reconciled against the different figure used elsewhere; may indicate
# this specific page/table is stale on Axis's own site.
# ---------------------------------------------------------------------
_AXIS_EDU_ROW_RE = re.compile(
    r"(Up to ₹4 lakh|Loans greater than ₹4 lakh and up to ₹7\.5 lakh|"
    r"Loans greater than ₹7\.5 lakh)\s+[\d.]+%\s+[\d.]+%\s+([\d.]+)%"
)


def extract_axis_education_rows(text: str) -> list[dict]:
    start = text.find("Loan Type Loan Amount")
    end = text.find("Current Repo Rate", start)
    if start == -1 or end == -1:
        return []
    region = text[start:end]

    rows = []
    for m in _AXIS_EDU_ROW_RE.finditer(region):
        tier, rate = m.groups()
        rows.append({
            "rate_type": "floating",
            "benchmark_type": "repo",
            "rate_min": float(rate),
            "rate_max": float(rate),
            "tier_type": "loan_amount",
            "tier_label": tier,
        })
    return rows


# ---------------------------------------------------------------------
# Indian Overseas Bank — RE-RESEARCHED 2026-08-20, reversing the §4i
# pre-flight's "no education section" verdict on the sibling
# `lending-interest-rates1` URL. The real page is a DIFFERENT
# capitalization/slug, `Lending-Interest-Rates` — found via the Vidya
# Jyothi product page's own "Rate of Interest -> Click here" link (the
# same off-page-link pattern seen elsewhere in this project, e.g.
# IndusInd's home_loan). Five real named schemes kept (13 rows total,
# same "genuinely all education-specific, not narrower variants of one
# product" reasoning as UCO's parser): Vidya Jyothi (Public/Staff),
# Vidya Suraksha, Vidya Shresht (A/B rated institutes), IOB Scholar
# (3 tiers), and "Other Education Loan Schemes" (Skill/Vocational/
# Career Dream x2/Bihar Student Credit Card). RLLR = 8.10%
# (w.e.f 15.12.2025).
#
# Real parsing bug found and fixed while building this: TWO of the 13
# real rate figures (Vidya Jyothi's Public and Staff rows) print WITHOUT
# a trailing '%' in the source HTML, while every other row has one — a
# regex requiring the literal '%' silently skipped past both, merging
# them into the NEXT real match's label instead of missing them
# outright (caught by comparing row count against the real page, not by
# an exception). Fixed by making the trailing '%' optional.
# ---------------------------------------------------------------------
_IOB_EDU_SCHEME_NAMES = [
    "VIDYA JYOTHI", "VIDYA SURAKSHA", "VIDYA SHRESHT", "IOB SCHOLAR",
    "Other Education Loan Schemes",
]
_IOB_EDU_ROW_RE = re.compile(r"RLLR\s*([+-])\s*([\d.]+)%\s+([\d.]+)%?")


def extract_iob_education_rows(text: str) -> list[dict]:
    region_start = text.find("VIDYA JYOTHI")
    region_end = text.find("FIXED RATE OF INTEREST", region_start)
    if region_start == -1 or region_end == -1:
        return []

    positions = []
    for name in _IOB_EDU_SCHEME_NAMES:
        idx = text.find(name, region_start, region_end)
        if idx != -1:
            positions.append((idx, name))
    positions.sort()

    rows = []
    for i, (start, name) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else region_end
        seg = text[start:end].replace("Type Rate of Interest Effective Rate", "")
        seg = re.sub(r"CONCESSIONS DETAILS CONCESSION.*?(?=RLLR|$)", "", seg, flags=re.DOTALL)
        prev_end = len(name)
        for m in _IOB_EDU_ROW_RE.finditer(seg):
            label = seg[prev_end:m.start()].strip(" .")
            sign, spread, rate = m.groups()
            rows.append({
                "rate_type": "floating",
                "benchmark_type": "RLLR",
                "benchmark_rate": 8.10,
                "rate_min": float(rate),
                "rate_max": float(rate),
                "tier_type": "scheme",
                "tier_label": f"{name.title()} ({label})" if label else name.title(),
            })
            prev_end = m.end()
    return rows


# ---------------------------------------------------------------------
# ICICI Bank — icici.bank.in/personal-banking/loans/education-loan/
# interest-rates. CONFIRMED in the original education-loan pre-flight
# (§4i, 2026-08-19) — the RAG doc's "no rate found" was a research miss
# (secondary-source search only, no official page located), not a real
# gap. Marked ready-to-build at pre-flight time but fell through the
# cracks of both build batches — built once the gap was noticed
# (2026-08-20). Rich real content, same "keep every real table on the
# page" scope as ICICI's own home_loan parser (which kept all 3 of its
# tables): Abroad (Secured/Unsecured), Domestic Engineering/Management
# by institute tier (Select vs Others, each Secured/Unsecured), Medical
# (Secured/Unsecured, all institutes), I-Group Employees (Secured/
# Unsecured), EXBT cases i.e. applicant already working (Secured/
# Unsecured), plus two flat government-subsidy-scheme rates (CGFSEL,
# PM Vidyalaxmi) that don't have a Secured/Unsecured split.
# ---------------------------------------------------------------------
_ICICI_EDU_ABROAD_RE = re.compile(
    r"ROI for abroad studies\s+Secured\s+Unsecured\s+Starting from\s+([\d.]+)%\s+([\d.]+)%"
)
_ICICI_EDU_ENG_MGMT_RE = re.compile(
    r"(Select Domestic Institutes\*|Others)\s+([\d.]+)%\s+([\d.]+)%"
)
_ICICI_EDU_MEDICAL_RE = re.compile(r"All\s+([\d.]+)%\s+([\d.]+)%")
_ICICI_EDU_IGROUP_RE = re.compile(r"I-Group Employees\s+([\d.]+)%\s+([\d.]+)%")
_ICICI_EDU_EXBT_RE = re.compile(r"ALL\s+([\d.]+)%\s+([\d.]+)%")
_ICICI_EDU_CGFSEL_RE = re.compile(
    r"Credit Guarantee Fund scheme for Education Loan \(CGFSEL\)\s+([\d.]+)%"
)
_ICICI_EDU_PMVIDYALAXMI_RE = re.compile(
    r"Pradhan Mantri Vidyalakshmi \(PMVidyalaxmi\) Scheme\s+([\d.]+)%"
)


def extract_icici_education_rows(text: str) -> list[dict]:
    rows = []

    m = _ICICI_EDU_ABROAD_RE.search(text)
    if m:
        secured, unsecured = m.groups()
        for label, rate in (("Secured", secured), ("Unsecured", unsecured)):
            rows.append({
                "rate_type": "floating", "rate_min": float(rate), "rate_max": float(rate),
                "tier_type": "scheme", "tier_label": f"Abroad Studies ({label})",
            })

    eng_start = text.find("Engineering and Management Courses")
    eng_end = text.find("Medical Courses", eng_start)
    if eng_start != -1 and eng_end != -1:
        for tier, secured, unsecured in _ICICI_EDU_ENG_MGMT_RE.findall(text[eng_start:eng_end]):
            tier_name = "Select Domestic Institutes" if "Select" in tier else "Other Domestic Institutes"
            for label, rate in (("Secured", secured), ("Unsecured", unsecured)):
                rows.append({
                    "rate_type": "floating", "rate_min": float(rate), "rate_max": float(rate),
                    "tier_type": "scheme",
                    "tier_label": f"Engineering/Management — {tier_name} ({label})",
                })

    med_start = text.find("Medical Courses")
    med_end = text.find("I-Group employees", med_start)
    if med_start != -1 and med_end != -1:
        m = _ICICI_EDU_MEDICAL_RE.search(text[med_start:med_end])
        if m:
            secured, unsecured = m.groups()
            for label, rate in (("Secured", secured), ("Unsecured", unsecured)):
                rows.append({
                    "rate_type": "floating", "rate_min": float(rate), "rate_max": float(rate),
                    "tier_type": "scheme", "tier_label": f"Medical Courses ({label})",
                })

    ig_start = text.find("I-Group employees")
    ig_end = text.find("EXBT cases", ig_start)
    if ig_start != -1 and ig_end != -1:
        m = _ICICI_EDU_IGROUP_RE.search(text[ig_start:ig_end])
        if m:
            secured, unsecured = m.groups()
            for label, rate in (("Secured", secured), ("Unsecured", unsecured)):
                rows.append({
                    "rate_type": "floating", "rate_min": float(rate), "rate_max": float(rate),
                    "tier_type": "scheme", "tier_label": f"I-Group Employees ({label})",
                })

    exbt_start = text.find("EXBT cases")
    exbt_end = text.find("Note*", exbt_start)
    if exbt_start != -1 and exbt_end != -1:
        m = _ICICI_EDU_EXBT_RE.search(text[exbt_start:exbt_end])
        if m:
            secured, unsecured = m.groups()
            for label, rate in (("Secured", secured), ("Unsecured", unsecured)):
                rows.append({
                    "rate_type": "floating", "rate_min": float(rate), "rate_max": float(rate),
                    "tier_type": "scheme", "tier_label": f"EXBT — Applicant Working ({label})",
                })

    m = _ICICI_EDU_CGFSEL_RE.search(text)
    if m:
        rate = float(m.group(1))
        rows.append({
            "rate_type": "floating", "rate_min": rate, "rate_max": rate,
            "tier_type": "scheme", "tier_label": "CGFSEL (Government Subsidy Scheme)",
        })

    m = _ICICI_EDU_PMVIDYALAXMI_RE.search(text)
    if m:
        rate = float(m.group(1))
        rows.append({
            "rate_type": "floating", "rate_min": rate, "rate_max": rate,
            "tier_type": "scheme", "tier_label": "PM Vidyalaxmi (Government Subsidy Scheme)",
        })

    return rows


_EDUCATION_PARSERS = {
    "central_bank": extract_central_bank_education_rows,
    "punjab_sind": extract_punjab_sind_education_rows,
    "bob": extract_bob_education_rows,
    "pnb": extract_pnb_education_rows,
    "uco": extract_uco_education_rows,
    "canara": extract_canara_education_rows,
    "hdfc": extract_hdfc_education_rows,
    "bom": extract_bom_education_rows,
    "axis": extract_axis_education_rows,
    "iob": extract_iob_education_rows,
    "icici": extract_icici_education_rows,
}
_EDUCATION_PDF_PARSERS = {
    "union_bank": extract_union_bank_education_rows,
}
# Parsers that need the RAW (unflattened) HTML rather than clean_text()
# output — currently just SBI, whose rate lives only in a div id with no
# nearby label text once tags are stripped (see extract_sbi_education_
# rows's module comment).
_EDUCATION_RAW_HTML_PARSERS = {
    "sbi": extract_sbi_education_rows,
}


def run_education(bank_ids: list[str] | None = None) -> None:
    """Batched education-loan extraction. Two source patterns, both
    supported: (1) Batch 1's banks had education rates sitting on the
    SAME page/PDF already fetched for home_loan — reuses that fetch_log
    row, no new fetch. (2) Batch 2's banks needed a dedicated
    `education_loan` url_type fetch (tracked via fetch_and_track.py's
    normal per-url_type mechanism, source_id `f"{bank_id}_education_
    loan"`) — tried FIRST, falling back to the reused home_loan row only
    if no dedicated fetch exists. Neither path is gated on
    `changed = TRUE` like `run()` — Batch 1 isn't a new fetch at all,
    and Batch 2's dedicated fetches are looked up by "most recent
    successful", not "changed since last time", since this is each
    bank's first-ever education-loan extraction. Logged to
    extraction_log under `f"{bank_id}_education_loan"` regardless of
    which source pattern was used, so it's traceable uniformly.

    `bank_ids` restricts to a subset (e.g. one batch) — default is every
    bank with an education-loan parser registered.
    """
    con = get_connection()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    targets = bank_ids or (
        list(_EDUCATION_PARSERS) + list(_EDUCATION_PDF_PARSERS) + list(_EDUCATION_RAW_HTML_PARSERS)
    )
    for bank_id in targets:
        is_pdf_parser = bank_id in _EDUCATION_PDF_PARSERS
        is_raw_html_parser = bank_id in _EDUCATION_RAW_HTML_PARSERS
        parser = (
            _EDUCATION_PDF_PARSERS.get(bank_id)
            or _EDUCATION_RAW_HTML_PARSERS.get(bank_id)
            or _EDUCATION_PARSERS.get(bank_id)
        )
        if parser is None:
            print(f"{bank_id}: no education-loan parser registered, skipping")
            continue

        row = con.execute(
            "SELECT fetched_at, file_path FROM fetch_log "
            "WHERE source_id = ? AND error IS NULL ORDER BY fetched_at DESC LIMIT 1",
            [f"{bank_id}_education_loan"],
        ).fetchone()
        reused_home_loan = False
        if row is None:
            row = con.execute(
                "SELECT fetched_at, file_path FROM fetch_log "
                "WHERE source_id = ? AND error IS NULL ORDER BY fetched_at DESC LIMIT 1",
                [f"{bank_id}_home_loan"],
            ).fetchone()
            reused_home_loan = True
        if row is None:
            print(f"{bank_id}: no education_loan or home_loan fetch_log row found, skipping")
            continue
        fetched_at, file_path = row

        resolved_path = resolve_scraped_path(file_path)
        if resolved_path is None:
            print(f"{bank_id}: snapshot file not found here ({file_path}) "
                  f"— skipping, not fetched on this machine")
            continue

        if is_pdf_parser:
            parser_input = str(resolved_path)
        elif is_raw_html_parser:
            parser_input = resolved_path.read_text(encoding="utf-8")
        else:
            html = resolved_path.read_text(encoding="utf-8")
            parser_input = clean_text(html)

        url_type = "home_loan" if reused_home_loan else "education_loan"
        source_url = next(
            (b["urls"][url_type] for b in config["banks"]
             if b["id"] == bank_id and url_type in b.get("urls", {})),
            _PDF_SOURCE_URLS.get(bank_id),
        )
        if source_url is None:
            print(f"{bank_id}: no {url_type} URL configured, skipping")
            continue

        log_source_id = f"{bank_id}_education_loan"
        try:
            raw_rows = parser(parser_input)
        except Exception as exc:
            print(f"{log_source_id}: extraction failed — {type(exc).__name__}: {exc}")
            con.execute(
                "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                [log_source_id, fetched_at, f"Parser failed: {exc}"],
            )
            continue

        if raw_rows:
            con.execute(
                "DELETE FROM loan_rates WHERE bank_id = ? AND loan_type = 'education_loan'",
                [bank_id],
            )

        accepted, rejected = 0, 0
        for raw in raw_rows:
            try:
                record = LoanRateRecord(
                    bank_id=bank_id,
                    loan_type="education_loan",
                    source_url=source_url,
                    fetched_at=str(fetched_at),
                    **raw,
                )
            except ValidationError as exc:
                rejected += 1
                con.execute(
                    "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                    [log_source_id, fetched_at, f"Validation failed: {exc}"],
                )
                continue

            con.execute(
                "INSERT INTO loan_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    record.bank_id, record.loan_type, record.rate_type,
                    record.benchmark_type, record.benchmark_rate,
                    record.rate_min, record.rate_max,
                    record.tier_type, record.tier_label,
                    record.processing_fee, record.max_ltv,
                    record.effective_date, record.source_url,
                    record.fetched_at, record.data_source,
                ],
            )
            accepted += 1

        con.execute(
            "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
            [log_source_id, fetched_at, f"{accepted} accepted, {rejected} rejected"],
        )
        print(f"{log_source_id}: {accepted} rows accepted, {rejected} rejected")

    con.close()


# ---------------------------------------------------------------------
# Vehicle loan — run_vehicle(), 2026-08-26. Same OUTCOME as run_education()
# (real, schema-valid, deduped, provenance-flagged LoanRateRecord rows
# inserted into loan_rates) but a genuinely different MECHANISM: run()/
# run_education() re-parse an already-fetched cached page (fetch_log/
# data/scraped) via a per-bank regex parser. No such cache exists for
# vehicle loans at all (confirmed: zero fetch_log rows and zero data/
# scraped/ files matching vehicle/car/auto before this was written) — so
# there is nothing to re-parse. Instead, the real numbers below are
# transcribed directly from each bank's already-built `data/raw/<bank>/
# <bank>_loan_vehicle.txt` RAG doc — each of which already cites its own
# real source (official page or cross-verified secondary aggregators)
# with a SOURCE header, from this project's original per-bank content
# build. This matches `load_boi_fd_legacy.py`'s established precedent
# for "real data, no live-fetch mechanism available" (data_source=
# 'manual_load_frozen', same as every other manually-loaded row in this
# project) — the primary/secondary distinction is carried in each row's
# own `source_url` text, not a separate DB column, matching how BOI's
# fd_rates and every other secondary-sourced row in this project already
# communicates it.
#
# Scope: 13 of the 17 banks with a real number in hand. Deliberately
# EXCLUDED: Kotak and IndusInd (both confirmed, via their own official
# pages, to publish no fixed vehicle-loan rate at all — a real gap, not
# fixable without fabricating a number); Indian Bank (its legacy doc has
# no numeric rate — "Linked to RLLR — floating", no spread — needs fresh
# research or a user-supplied source, same situation home/education loan
# were in before a docx was supplied); HDFC (no vehicle_loan RAG doc
# exists at all — genuinely unresearched, not just unbuilt).
#
# Flagship-only scoping, same convention as every home_loan/education_
# loan parser: one representative row per bank's main car-loan product,
# not every named variant (used-car/two-wheeler/EV/commercial sub-
# products are real but out of scope here, same as education_loan
# skipping institute-tier sub-schemes).
#
# rate_type: where a bank's own page doesn't explicitly say "floating"
# or "fixed", "floating" is used as the default — grounded in a real,
# already-documented regulatory fact this project cites elsewhere (RBI's
# Master Direction on Interest Rate on Advances mandates external-
# benchmark-linked floating pricing for retail advances), not a guess.
# SBI (explicitly "fixed at disbursement") and PNB (explicitly "both
# floating and fixed-rate options offered", → "hybrid") are the two
# exceptions, both because their own doc states it outright.
_VEHICLE_ROWS: dict[str, dict] = {
    "axis": {
        "source_url": "https://www.axis.bank.in/loans/car-loan (official page)",
        "effective_date": "2026-08-16",
        "rows": [
            {"rate_type": "floating", "benchmark_type": "MCLR", "benchmark_rate": 8.85,
             "rate_min": 8.85, "rate_max": 11.70, "processing_fee": "Rs. 3,500-Rs. 12,000",
             "max_ltv": 100.0},
        ],
    },
    "bob": {
        "source_url": "https://bankofbaroda.bank.in/loans/vehicle-loan and "
                       "https://bankofbaroda.bank.in/interest-rate-and-service-charges/"
                       "retail-loans-interest-rates (official pages) — Baroda Car Loan (New) only, "
                       "Digital Car Loan/Two-Wheeler variants out of scope",
        "effective_date": "2025-12-06",
        "rows": [
            {"rate_type": "floating", "benchmark_type": "BRLLR", "benchmark_rate": 7.90,
             "rate_min": 7.60, "rate_max": 11.30},
        ],
    },
    "bom": {
        "source_url": "https://bankofmaharashtra.bank.in/personal-banking/loans/car-loan (official page)",
        "effective_date": "2026-08-19",
        "rows": [
            {"rate_type": "floating", "rate_min": 7.45, "rate_max": 12.00,
             "tier_type": "credit_score", "tier_label": "Depends on credit score",
             "max_ltv": 90.0},
        ],
    },
    "canara": {
        "source_url": "https://www.canarabank.bank.in/canara-vehicle and "
                       "https://www.canarabank.bank.in/pages/interest-rate-range-on-loans "
                       "(official pages) — Canara Vehicle only, Canara Green Wheels (EV) out of scope",
        "effective_date": "2025-12-12",
        "rows": [
            {"rate_type": "floating", "rate_min": 7.45, "rate_max": 15.00},
        ],
    },
    "central_bank": {
        "source_url": "https://centralbank.bank.in/en/node/418 and "
                       "https://centralbank.bank.in/retail-scheme-interest (official pages)",
        "effective_date": "2026-08-19",
        "rows": [
            {"rate_type": "floating", "benchmark_type": "RBLR",
             "rate_min": 7.65, "rate_max": 9.30,
             "tier_type": "vehicle_type", "tier_label": "4-Wheeler",
             "processing_fee": "0.50% (+GST for 4-wheelers), waived until 31 Dec 2026"},
            {"rate_type": "floating", "benchmark_type": "RBLR",
             "rate_min": 10.25, "rate_max": 11.35,
             "tier_type": "vehicle_type", "tier_label": "2-Wheeler",
             "processing_fee": "0.50%, waived until 31 Dec 2026"},
        ],
    },
    "icici": {
        "source_url": "https://www.icici.bank.in/personal-banking/loans/car-loan/interest-rate "
                       "(official page)",
        "effective_date": "2026-08-15",
        "rows": [
            {"rate_type": "floating", "rate_min": 8.35, "rate_max": 8.40},
        ],
    },
    "sbi": {
        "source_url": "https://sbi.bank.in/web/personal-banking/loans/auto-loans and "
                       "https://sbi.bank.in/web/interest-rates/interest-rates/loan-schemes-"
                       "interest-rates/auto-loans (official pages) — Standard/NRI/Assured Car "
                       "Loans (flagship), Green Car / Two-Wheeler variants out of scope",
        "effective_date": "2026-07-01",
        "rows": [
            {"rate_type": "fixed", "rate_min": 8.70, "rate_max": 9.85},
        ],
    },
    "uco": {
        "source_url": "https://uco.bank.in/en/web/guest/vehicle-loan1 and "
                       "https://uco.bank.in/repo (official pages)",
        "effective_date": "2026-08-19",
        "rows": [
            {"rate_type": "floating", "benchmark_type": "UCO Float", "benchmark_rate": 8.30,
             "rate_min": 7.70, "rate_max": 10.25},
        ],
    },
    "boi": {
        "source_url": "Secondary-sourced (BOI's own site is Cloudflare-blocked, confirmed "
                       "repeatedly — see feedback_multibank_rag_gotchas memory): cross-referenced "
                       "https://www.bankbazaar.com/bank-of-india-car-loan.html and "
                       "https://www.codeforbanks.com/banking/car-loan/bank-of-india/"
                       "car-loan-interest-rates/, both agree MCLR + 0.85% spread",
        "effective_date": "2026-06-08",
        "rows": [
            {"rate_type": "floating", "benchmark_type": "MCLR", "benchmark_rate": 8.70,
             "rate_min": 7.60, "rate_max": 8.85},
        ],
    },
    "iob": {
        "source_url": "Secondary-sourced (no first-party IOB page found for this product): "
                       "cross-referenced https://www.creditmantri.com/indian-overseas-bank-"
                       "car-loan-interest-rates/ and https://www.codeforbanks.com/banking/"
                       "car-loan/Indian-overseas-bank/car-loan-interest-rates/",
        "effective_date": "2026-07-21",
        "rows": [
            {"rate_type": "floating", "rate_min": 7.80, "rate_max": 10.05},
        ],
    },
    "pnb": {
        "source_url": "Secondary-sourced (no first-party PNB page found for this product): "
                       "cross-referenced https://www.creditmantri.com/punjab-national-bank-"
                       "car-loan-interest-rates/, https://www.bankbazaar.com/punjab-national-"
                       "bank-car-loan.html, and https://www.zeebiz.com/personal-finance/"
                       "news-car-loan-rates-april-2026",
        "effective_date": "2026-06-28",
        "rows": [
            {"rate_type": "hybrid", "rate_min": 7.30, "rate_max": 10.70,
             "processing_fee": "0.25% (subject to stated min/max)"},
        ],
    },
    "punjab_sind": {
        "source_url": "https://punjabandsind.bank.in/content/conveyance (official page — "
                       "Eligibility/Loan Amount/Tenure/Margin/Fees only, no rate stated there); "
                       "rate itself is secondary-sourced from "
                       "https://www.bankbazaar.com/punjab-and-sind-bank-car-loan-interest-rates.html "
                       "(read 2026-08-25), whose tenure figures matched the bank's own official "
                       "page exactly — a real cross-check",
        "effective_date": "2026-08-25",
        "rows": [
            {"rate_type": "floating", "benchmark_type": "EBLR", "rate_min": 9.05, "rate_max": 9.05},
        ],
    },
    "union_bank": {
        "source_url": "Secondary-sourced (creditmantri.com cross-referenced against the bank's "
                       "own https://www.unionbankofindia.bank.in/en/details/union-vehicle page, "
                       "which lists the scheme but not a numeric rate): "
                       "https://www.creditmantri.com/union-bank-of-india-car-loan-interest-rates/ "
                       "— Union Miles (new 4-wheeler) flagship only, Green Miles (EV)/used-vehicle "
                       "variants out of scope",
        "effective_date": "2026-08-07",
        "rows": [
            {"rate_type": "floating", "rate_min": 8.85, "rate_max": 10.50, "max_ltv": 85.0},
        ],
    },
}


def run_vehicle(bank_ids: list[str] | None = None) -> None:
    """Loads real, already-sourced vehicle-loan rates for the 13 banks
    with a real number in hand (see `_VEHICLE_ROWS` and the module
    comment above for scope/sourcing/exclusions). `bank_ids` restricts
    to a subset — default is every bank in `_VEHICLE_ROWS`."""
    con = get_connection()
    targets = bank_ids or list(_VEHICLE_ROWS)
    fetched_at = str(datetime.now())

    for bank_id in targets:
        entry = _VEHICLE_ROWS.get(bank_id)
        if entry is None:
            print(f"{bank_id}: no vehicle-loan data registered, skipping")
            continue

        log_source_id = f"{bank_id}_vehicle_loan"
        con.execute("DELETE FROM loan_rates WHERE bank_id = ? AND loan_type = 'vehicle_loan'", [bank_id])

        accepted, rejected = 0, 0
        for raw in entry["rows"]:
            try:
                record = LoanRateRecord(
                    bank_id=bank_id,
                    loan_type="vehicle_loan",
                    source_url=entry["source_url"],
                    fetched_at=fetched_at,
                    effective_date=entry["effective_date"],
                    data_source="manual_load_frozen",
                    **raw,
                )
            except ValidationError as exc:
                rejected += 1
                con.execute(
                    "INSERT INTO extraction_log VALUES (?, ?, 'rejected', ?, current_timestamp)",
                    [log_source_id, fetched_at, f"Validation failed: {exc}"],
                )
                continue

            con.execute(
                "INSERT INTO loan_rates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    record.bank_id, record.loan_type, record.rate_type,
                    record.benchmark_type, record.benchmark_rate,
                    record.rate_min, record.rate_max,
                    record.tier_type, record.tier_label,
                    record.processing_fee, record.max_ltv,
                    record.effective_date, record.source_url,
                    record.fetched_at, record.data_source,
                ],
            )
            accepted += 1

        con.execute(
            "INSERT INTO extraction_log VALUES (?, ?, 'ok', ?, current_timestamp)",
            [log_source_id, fetched_at, f"{accepted} accepted, {rejected} rejected"],
        )
        print(f"{log_source_id}: {accepted} rows accepted, {rejected} rejected")

    con.close()


if __name__ == "__main__":
    run()
