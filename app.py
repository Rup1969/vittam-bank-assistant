import os
import re
import sys
import json
import time
import traceback
import streamlit as st
from groq import Groq
from pathlib import Path
import numpy as np

import settings

EMBED_MODEL      = settings.EMBED_MODEL
TOP_K            = settings.TOP_K
GROQ_MODEL       = settings.GROQ_MODEL
GROQ_API_KEY     = settings.GROQ_API_KEY

# Real usernames/passwords live only in .streamlit/secrets.toml (gitignored),
# never in source — see settings.ALLOWED_USERS and secrets.toml.example.
ALLOWED_USERS = settings.ALLOWED_USERS

# Single generic prompt for any bank-scoped or cross-bank chat — retired
# the old Indian-Bank-only LEGACY_SYSTEM_PROMPT once Indian Bank stopped
# being a special top-level case and became just another PSB entry (see
# BANK_UI/PSB_BANKS below). Every chunk (Indian Bank's included, via
# retrieve()'s "INDIAN_BANK" default) is tagged the same way, so one
# tag-aware prompt covers a single selected bank AND the cross-bank
# general chat equally well.
GENERAL_SYSTEM_PROMPT = (
    "You are a helpful and professional assistant covering multiple Indian banks "
    "and general RBI guidelines for commercial banking. "
    "Answer customer questions using ONLY the context provided. Rules: "
    "1. Never invent facts, rates, or product names outside the context. "
    "2. Every chunk is tagged with a BANK. Never attribute one bank's rate, "
    "product name, or policy to a different bank — if the question asks about "
    "a bank with no matching context, say so instead of guessing from another bank's data. "
    "3. When comparing banks, state each bank's figure separately and clearly "
    "label which bank it belongs to. "
    "4. Every chunk also carries a SOURCE_TYPE — use it to set your tone: "
    "   - PRODUCT_PAGE: a bank's own marketing/FAQ content. Note rates are subject to change "
    "and the final rate may depend on credit profile. "
    "   - RBI_MASTER_CIRCULAR or RBI_MASTER_DIRECTION: binding regulatory rules. "
    "State these as rules, not as marketing claims. "
    "5. If the answer is not in context, say: I do not have that detail, "
    "please check the relevant bank's official website or a branch. "
    "6. When the question asks about 'all banks' or implies a full-market "
    "comparison (e.g. 'which bank has the highest...', 'compare X across "
    "banks'), the context you receive may only cover SOME banks, not every "
    "bank that exists — never imply the banks in your context are the "
    "complete set. Answer using exactly the banks present in the context, "
    "phrase any ranking as scoped to those ('the highest among the banks "
    "I have data for is...', not an absolute market-wide claim), and end "
    "by naming which banks you couldn't cover and pointing the customer to "
    "that bank's own profile page (under Public Sector Bank or Private "
    "Bank) for its current rate. "
    "7. Be warm, clear, and complete. Never cut off mid sentence. "
    "8. Write in plain prose only — no markdown (no **, no tables, no bullet "
    "lists with * or -). This answer renders in a plain-text chat bubble, so "
    "markdown syntax would show up as literal stray characters. "
    "9. The [Source i | BANK | CATEGORY | SOURCE_TYPE | doc_id] tags and "
    "SOURCE_TYPE labels in the context are for your eyes only, to decide "
    "wording and tone — never quote, print, or reference them (or the word "
    "'SOURCE_TYPE' itself) in your answer. Write a normal, direct answer."
)

# RBI's pool is single-source (no bank-blending risk) but still needs
# source_type-aware tone, since it mixes binding rules with general info.
# RBI pool is a fast-lookup tool for bank staff checking current RBI
# rules mid-work, not a plain-language explainer for retail customers
# (see chat). Precision and recency matter more than approachability here.
RBI_SYSTEM_PROMPT = (
    "You are a regulatory lookup assistant for bank staff who need to quickly "
    "confirm what RBI currently requires on a topic. Answer using ONLY the "
    "context provided. Rules: "
    "1. Never invent facts, figures, rules, or dates outside the context. "
    "2. Every chunk may carry a DATE (its effective or last-amended date). "
    "State the date for any rule you give, e.g. 'as of [date]'. "
    "3. If more than one chunk addresses the same topic with different dates, "
    "treat the most recent DATE as current and explicitly say the older one "
    "may have been superseded — do not silently pick one. "
    "4. Use SOURCE_TYPE to set precision: "
    "RBI_MASTER_CIRCULAR / RBI_MASTER_DIRECTION -> state as a binding regulatory rule. "
    "RBI_CITIZENS_CORNER -> general guidance, note it is not the primary legal text. "
    "RBI_CURRENT_RATES -> note this changes with each monetary policy review. "
    "RBI_WHATS_NEW -> note this is a recent announcement that may since be superseded. "
    "5. These are general RBI rules applicable across banks, not specific to "
    "any one bank. "
    "6. If the answer is not in context, say so plainly and suggest checking "
    "rbi.org.in directly — do not guess. "
    "7. Be precise and complete. Never cut off mid sentence. "
    "8. Write in plain prose only — no markdown (no **, no tables, no bullet "
    "lists with * or -). This answer renders in a plain-text chat bubble, so "
    "markdown syntax would show up as literal stray characters. "
    "9. The [Source i | BANK | CATEGORY | SOURCE_TYPE | doc_id] tags and "
    "SOURCE_TYPE labels in the context are for your eyes only, to decide "
    "wording and precision — never quote, print, or reference them (or the "
    "word 'SOURCE_TYPE' itself) in your answer. Write a normal, direct answer."
)

SYSTEM_PROMPT = GENERAL_SYSTEM_PROMPT  # overwritten per-selection further below

# General (no bank picked) example questions — deliberately cross-bank,
# unlike a specific bank's own example list.
# Deliberately bank-only — General Chat doesn't search the RBI pool (RBI
# has its own separate section, with its own precision-focused tone), so
# an RBI-flavored example here would always come back empty and read as a
# bug rather than a scope boundary.
# Deliberately NOT "compare rates across all banks" — chat retrieval only
# ever surfaces its top TOP_K matching chunks (a handful, not one per
# bank), so a full-market comparison example here would silently answer
# from whichever 2-3 banks' docs happened to rank highest and look like a
# bug. Exhaustive comparison is exactly what the Compare toggle is for.
GENERAL_EXAMPLES = [
    "Which banks offer a zero balance savings account?",
    "Which banks offer a Kisan Credit Card scheme?",
    "What is the SBI home loan interest rate?",
    "What is the Union Bank of India FD interest rate?",
    "How does UPI payment work through a mobile banking app?",
    "What is the minimum balance requirement at Axis Bank?",
]

# Per-bank identity — title, subtitle, example questions, footer, and theme
# all live together here since they all change together whenever the
# customer switches which bank they're asking about. Keyed by the same
# BANK code used in the raw-file headers (settings.BANKS), so a new bank
# just needs an entry here plus content under data/raw/ — no other code
# changes required for it to show up correctly. Indian Bank is included
# here now (previously handled as its own separate top-level scope) so it
# can appear as an ordinary PSB entry, not a special case.
BANK_UI = {
    "INDIAN_BANK": {
        "label": "Indian Bank",
        "subtitle": "Ask about deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the interest rate for a 1 year FD?",
            "Who can open IND SHAKTI account?",
            "What is the home loan interest rate?",
            "How does IndSMART mobile banking work?",
            "What is IND Secure deposit scheme?",
            "What is the education loan interest rate?",
            "How to get a jewel loan?",
            "What is FASTag and how to get it?",
        ],
        "data": "indianbank.bank.in",
        "updated": "Feb 2026",
        "contact": "📞 Helpline: 1800 1700",
        # Real brand colors, supplied 2026-08-19: Primary #FEB135 (orange)
        # + Secondary #183883 (navy blue). Navy used as accent — the
        # orange primary is too light for white header text (same reason
        # this was left on a placeholder navy until the real secondary
        # was supplied), so accent/dark/tint/tint2 are all derived from
        # the navy, with the orange used as-is for mid.
        "theme": {
            "accent": "#183883", "mid": "#FEB135", "dark": "#0d1f48",
            "tint": "#e8ebf3", "tint2": "#c5cde0",
        },
    },
    "SBI": {
        "label": "State Bank of India (SBI)",
        "subtitle": "Ask about SBI deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the SBI FD interest rate?",
            "What is the SBI home loan interest rate?",
            "What is the SBI personal loan interest rate?",
            "What is the SBI gold loan interest rate?",
            "How does the YONO app work for UPI payments?",
            "What is the SBI education loan interest rate?",
            "What is the interest rate for a loan against fixed deposit?",
            "What SBI savings accounts are available for minors?",
        ],
        "data": "sbi.bank.in",
        "updated": "Jul 2026",
        "contact": "📞 SBI Helpline: 1800 1234",
        # Real brand colors, supplied 2026-08-15: Vivid Cerulean Blue #00B5EF
        # + Navy Blue #292075. Navy used as accent (better contrast for the
        # white header text than the brighter cerulean), cerulean as mid.
        "theme": {
            "accent": "#292075", "mid": "#00B5EF", "dark": "#170f42",
            "tint": "#ece9f7", "tint2": "#c9c0e8",
        },
    },
    "BOB": {
        "label": "Bank of Baroda (BOB)",
        "subtitle": "Ask about Bank of Baroda deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Bank of Baroda FD interest rate?",
            "What is the Bank of Baroda home loan interest rate?",
            "What is BRLLR and how does it affect BOB loan rates?",
            "How does the bob World app work?",
            "What is the Bank of Baroda personal loan interest rate?",
            "What is the Bank of Baroda gold loan interest rate?",
            "What savings account variants does Bank of Baroda offer?",
            "What is the Baroda Kisan Credit Card?",
        ],
        "data": "bankofbaroda.bank.in",
        "updated": "Jul 2026",
        "contact": "📞 BOB Helpline: 1800 5700",
        # Real brand color, supplied 2026-08-15: Baroda Orange #F15A29
        # (the logo's other color is white, not usable as a gradient
        # partner — mid/dark/tint derived from the real orange instead).
        "theme": {
            "accent": "#F15A29", "mid": "#f47f4f", "dark": "#8a2d10",
            "tint": "#fef0e8", "tint2": "#fbc9ab",
        },
    },
    "CANARA": {
        "label": "Canara Bank",
        "subtitle": "Ask about Canara Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Canara Bank FD interest rate?",
            "What is the Canara Bank housing loan interest rate?",
            "What is Canara Ready Cash and who is eligible?",
            "How does the Canara ai1 app work?",
            "What is the Canara Bank gold loan interest rate?",
            "What is Vidya Turant education loan scheme?",
            "What is the Canara Bank Kisan Credit Card interest rate?",
            "What is the loan-to-value ratio on a Canara Bank gold loan?",
        ],
        "data": "canarabank.bank.in",
        "updated": "Jul 2026",
        "contact": "📞 Canara Bank Helpline: 1800 1030",
        # Real brand colors, supplied 2026-08-15: Chinese Yellow #FFB600 +
        # Blue #019EEC. Blue used as accent (better white-text contrast
        # than the bright yellow), yellow as mid.
        "theme": {
            "accent": "#019EEC", "mid": "#FFB600", "dark": "#014f76",
            "tint": "#e3f4fc", "tint2": "#aedcf7",
        },
    },
    "PNB": {
        "label": "Punjab National Bank (PNB)",
        "subtitle": "Ask about PNB deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the PNB FD interest rate?",
            "What is the PNB home loan interest rate?",
            "What is the PNB personal loan interest rate?",
            "How does the PNB ONE app work for UPI payments?",
            "What is the PNB gold loan interest rate?",
            "What is the PNB Uttam non-callable deposit scheme?",
            "What is the PNB education loan interest rate?",
            "What is the PNB Kisan Credit Card interest rate?",
        ],
        "data": "pnb.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 PNB Helpline: 1800 180 2222",
        # Real brand colors, supplied 2026-08-15: Royal Gold/Yellow #FBBC09
        # + Vivid Burgundy Red #A20E37. Burgundy used as accent (much
        # better white-text contrast than the bright gold), gold as mid.
        "theme": {
            "accent": "#A20E37", "mid": "#FBBC09", "dark": "#5c0820",
            "tint": "#fbe8ec", "tint2": "#f0b9c4",
        },
    },
    "BOI": {
        "label": "Bank of India (BOI)",
        "subtitle": "Ask about Bank of India deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Bank of India FD interest rate?",
            "What is the Bank of India Star Home Loan interest rate?",
            "What is the Bank of India Star Personal Loan interest rate?",
            "How does the BOI Mobile app work for UPI payments?",
            "What is the Bank of India gold loan interest rate?",
            "What is the Star Sunidhi tax-saving deposit scheme?",
            "What is the Bank of India Star Education Loan interest rate?",
            "What is the Bank of India Kisan Credit Card interest rate?",
        ],
        "data": "bankofindia.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 BOI Helpline: 1800 103 1906",
        # Real brand colors, supplied 2026-08-19: Primary #017DC7 (blue)
        # + Secondary #F33F26 (red). Blue used as accent (better
        # white-text contrast than the red), red as mid.
        "theme": {
            "accent": "#017DC7", "mid": "#F33F26", "dark": "#01456D",
            "tint": "#e6f2f9", "tint2": "#c0dff1",
        },
    },
    "UNION_BANK": {
        "label": "Union Bank of India",
        "subtitle": "Ask about Union Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Union Bank of India FD interest rate?",
            "What is the Union Bank home loan interest rate?",
            "What is the Union Bank personal loan interest rate?",
            "How does the Vyom app work for UPI payments?",
            "What is the Union Bank gold loan interest rate?",
            "What is Union Miles car loan interest rate?",
            "What is the Union Education loan interest rate?",
            "What is the Union Green Card Kisan Credit Card?",
        ],
        "data": "unionbankofindia.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 Union Bank Helpline: 1800 2333",
        # Real brand colors, supplied 2026-08-15: Lebanese Red + French Blue.
        "theme": {
            "accent": "#DA251C", "mid": "#00579C", "dark": "#7a120c",
            "tint": "#fbe6e4", "tint2": "#f0b9b3",
        },
    },
    "IOB": {
        "label": "Indian Overseas Bank (IOB)",
        "subtitle": "Ask about Indian Overseas Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Indian Overseas Bank FD interest rate?",
            "What is the IOB home loan interest rate?",
            "What is the IOB personal loan interest rate?",
            "How does the IOB Connect app work for UPI payments?",
            "What is the IOB gold loan interest rate?",
            "What is the Vehicle Loans-Pushpaka interest rate?",
            "What is the IOB loan against property interest rate?",
            "What is the IOB Kisan Credit Card collateral requirement?",
        ],
        "data": "iob.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 IOB Helpline: 1800 425 4445",
        # Real brand color, supplied 2026-08-19: Primary #013CC8 (blue).
        # The supplied #5B7FE0 is explicitly a tint of the primary, not a
        # second brand color — used directly as mid (a real designed
        # lighter shade, rather than deriving our own), dark/tint/tint2
        # all derived from the primary the same way single-color banks
        # (e.g. BOB) already are.
        "theme": {
            "accent": "#013CC8", "mid": "#5B7FE0", "dark": "#01216E",
            "tint": "#e6ecfa", "tint2": "#c0cef1",
        },
    },
    "ICICI": {
        "label": "ICICI Bank",
        "subtitle": "Ask about ICICI Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the ICICI Bank FD interest rate?",
            "What is the ICICI Bank home loan interest rate?",
            "What is the ICICI Bank personal loan interest rate?",
            "How does the iMobile Pay app work for UPI payments?",
            "What is the ICICI Bank gold loan interest rate?",
            "What is the iWish flexible recurring deposit?",
            "What is the ICICI Bank car loan interest rate?",
            "What is the ICICI Bank Kisan Credit Card interest rate?",
        ],
        "data": "icici.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 ICICI Bank Helpline: 1800 1080",
        # Real brand colors, supplied 2026-08-19: Primary #B02A30 (maroon
        # red) + Secondary #F99D27 (orange). Maroon used as accent
        # (WCAG contrast ~6.5:1 against white vs. orange's ~2.1:1, below
        # the 3:1 minimum), orange as mid.
        "theme": {
            "accent": "#B02A30", "mid": "#F99D27", "dark": "#61171A",
            "tint": "#F7EAEA", "tint2": "#EBCACB",
        },
    },
    "HDFC": {
        "label": "HDFC Bank",
        "subtitle": "Ask about HDFC Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the HDFC Bank FD interest rate?",
            "What is the HDFC Bank home loan interest rate?",
            "What is the HDFC Bank personal loan interest rate?",
            "How does the PayZapp app work for UPI payments?",
            "What is the HDFC Bank gold loan interest rate?",
            "What is the HDFC Bank Kisan Credit Card credit limit?",
            "What is the HDFC TruFixed home loan scheme?",
            "What is the CGTMSE collateral-free MSME loan scheme?",
        ],
        "data": "hdfc.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 HDFC Bank Helpline: 1800 2600",
        # Real brand colors, supplied 2026-08-19: Primary #004C8F (blue)
        # + Secondary #ED232A (red). Blue used as accent (WCAG contrast
        # ~8.6:1 against white vs. red's ~4.3:1), red as mid.
        "theme": {
            "accent": "#004C8F", "mid": "#ED232A", "dark": "#002A4F",
            "tint": "#E6EDF4", "tint2": "#BFD2E3",
        },
    },
    "KOTAK_MAHINDRA": {
        "label": "Kotak Mahindra Bank",
        "subtitle": "Ask about Kotak Mahindra Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Kotak Mahindra Bank FD interest rate?",
            "What is the Kotak Mahindra Bank home loan interest rate?",
            "What is the Kotak Mahindra Bank personal loan interest rate?",
            "How does the Kotak811 app work for UPI payments?",
            "What is the Kotak Mahindra Bank gold loan interest rate?",
            "What is the Kotak Gold Loan Smart Choice scheme?",
            "What is ActivMoney sweep deposit?",
            "What is the CGTMSE collateral-free MSME loan limit at Kotak?",
        ],
        "data": "kotak.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 Kotak Mahindra Bank Helpline: 1800 4100",
        # Real brand colors, supplied 2026-08-19: Primary #003874 (navy
        # blue) + Secondary #ED1C24 (red). Navy used as accent (WCAG
        # contrast ~11.6:1 against white vs. red's ~4.4:1), red as mid.
        "theme": {
            "accent": "#003874", "mid": "#ED1C24", "dark": "#001F40",
            "tint": "#E6EBF1", "tint2": "#BFCDDC",
        },
    },
    "AXIS": {
        "label": "Axis Bank",
        "subtitle": "Ask about Axis Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Axis Bank FD interest rate?",
            "What is the Axis Bank home loan interest rate?",
            "What is the Axis Bank personal loan interest rate?",
            "How does the open by Axis Bank app work for UPI payments?",
            "What is the Axis Bank gold loan interest rate?",
            "What is the Axis Bank Zero-Collateral Loan for MSMEs?",
            "What is the Axis Bank Kisan Credit Card loan amount?",
            "What is Kisan Power at Axis Bank?",
        ],
        "data": "axis.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 Axis Bank Helpline: 1800 209 5577",
        # Real brand color, supplied 2026-08-19: single-color maroon
        # #800000 (WCAG contrast ~11:1 against white) — mid/dark/tint
        # derived from this one real color, same pattern as BOB/IOB.
        "theme": {
            "accent": "#800000", "mid": "#9C3838", "dark": "#460000",
            "tint": "#F2E6E6", "tint2": "#DFBFBF",
        },
    },
    "INDUSIND": {
        "label": "IndusInd Bank",
        "subtitle": "Ask about IndusInd Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the IndusInd Bank FD interest rate?",
            "What is the IndusInd Bank personal loan interest rate?",
            "What is the IndusInd Bank home loan interest rate?",
            "What is the IndusInd Bank gold loan interest rate?",
            "What is the Indus Kisan agriculture loan interest rate?",
            "What is IndusInd Bank's MSME business loan interest rate?",
            "What is the INDIE app and what can I do with it?",
            "What is the IndusInd Bank FD interest rate for senior citizens?",
        ],
        "data": "indusind.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 IndusInd Bank Helpline: 1860 267 7777",
        # Real brand color, supplied 2026-08-19: single-color dark red
        # #98272A (WCAG contrast ~7.9:1 against white) — mid/dark/tint
        # derived from this one real color, same pattern as BOB/IOB/Axis.
        "theme": {
            "accent": "#98272A", "mid": "#AF5759", "dark": "#541517",
            "tint": "#F5E9EA", "tint2": "#E5C9CA",
        },
    },
    "CENTRAL_BANK": {
        "label": "Central Bank of India",
        "subtitle": "Ask about Central Bank of India deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Central Bank of India FD interest rate?",
            "What is the Central Bank of India home loan interest rate?",
            "What is the Central Bank of India personal loan interest rate?",
            "How does Cent PAY work for UPI payments?",
            "What is the Central Bank of India gold loan interest rate?",
            "What is the Central Bank of India education loan interest rate?",
            "What is the Central KCC Kisan Credit Card interest rate?",
            "What is the Central Bank of India vehicle loan interest rate?",
        ],
        "data": "centralbank.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 Central Bank of India Helpline: 1800 30 30",
        # Real brand color, supplied 2026-08-19: Poseidon Blue #176FC1
        # (the logo's other color is white, not usable as a gradient
        # partner — mid/dark/tint derived from the real blue instead,
        # same pattern as BOB's single-color derivation).
        "theme": {
            "accent": "#176FC1", "mid": "#4A8FCF", "dark": "#0D3D6A",
            "tint": "#E8F1F9", "tint2": "#C5DBF0",
        },
    },
    "PUNJAB_SIND": {
        "label": "Punjab & Sind Bank",
        "subtitle": "Ask about Punjab & Sind Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Punjab & Sind Bank FD interest rate?",
            "What is the PSB Apna Ghar home loan interest rate?",
            "What is the Punjab & Sind Bank personal loan interest rate?",
            "How does PSB UNIC BHIM work for UPI payments?",
            "What is the Punjab & Sind Bank gold loan interest rate?",
            "What is the PSB Krishak Rath vehicle loan interest rate?",
            "What is the PSB Udyan Scheme interest rate?",
            "What is the Punjab & Sind Bank mortgage loan amount limit?",
        ],
        "data": "punjabandsind.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 Punjab & Sind Bank Helpline: 1800 419 8300",
        # Real brand color, supplied 2026-08-19: Celtic Green #167947
        # (the logo's other color is white, not usable as a gradient
        # partner — mid/dark/tint derived from the real green instead,
        # same pattern as BOB's single-color derivation).
        "theme": {
            "accent": "#167947", "mid": "#49966F", "dark": "#0C4327",
            "tint": "#E8F2ED", "tint2": "#C5DED1",
        },
    },
    "BOM": {
        "label": "Bank of Maharashtra",
        "subtitle": "Ask about Bank of Maharashtra deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the Bank of Maharashtra FD interest rate?",
            "What is the Bank of Maharashtra home loan interest rate?",
            "What is the Bank of Maharashtra personal loan interest rate?",
            "How does the Zen Lyfe app work for UPI payments?",
            "What is the Bank of Maharashtra gold loan interest rate?",
            "What is the Bank of Maharashtra car loan interest rate?",
            "What is the Bank of Maharashtra Kisan Credit Card interest rate?",
            "What is the Bank of Maharashtra education loan interest rate?",
        ],
        "data": "bankofmaharashtra.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 Bank of Maharashtra Helpline: 1800 233 4526",
        # Real brand color, supplied 2026-08-19: Sky Blue #0E88D3. The
        # companion "Yellow" has no exact hex on record, so mid/dark/tint
        # are all derived from the real blue instead of guessing a
        # yellow shade — same single-color derivation pattern as BOB.
        "theme": {
            "accent": "#0E88D3", "mid": "#43A2DD", "dark": "#084B74",
            "tint": "#E7F3FB", "tint2": "#C3E1F4",
        },
    },
    "UCO": {
        "label": "UCO Bank",
        "subtitle": "Ask about UCO Bank deposits, loans, digital products, and interest rates",
        "examples": [
            "What is the UCO Bank FD interest rate?",
            "What is the UCO Home loan interest rate?",
            "What is the UCO Cash personal loan interest rate?",
            "How does BHIM UCO UPI work for payments?",
            "What is the UCO Bank gold loan interest rate?",
            "What is the UCO Car loan interest rate?",
            "What is the UCO Bank Kisan Credit Card interest rate?",
            "What is the UCO Udaan education loan interest rate?",
        ],
        "data": "uco.bank.in",
        "updated": "Aug 2026",
        "contact": "📞 UCO Bank Helpline: 1800 8910",
        # Real brand colors, supplied 2026-08-19: Primary Cobalt Blue
        # #0044AA + Secondary Orange #F5821F. Cobalt Blue used as accent
        # (contrast ratio ~8.7:1 against white vs. orange's ~2.6:1, well
        # short of the 3:1 minimum), orange as mid — same real-colors
        # pattern as SBI/PNB/BOB/Canara/Union Bank.
        "theme": {
            "accent": "#0044AA", "mid": "#F5821F", "dark": "#00255E",
            "tint": "#E6ECF7", "tint2": "#BFD0EA",
        },
    },
}

# Grouped for the sidebar's Public/Private Sector navigation — Indian Bank
# sits inside PSB_BANKS now, not as a separate top-level scope.
PSB_BANKS     = ["INDIAN_BANK", "SBI", "BOB", "CANARA", "PNB", "BOI", "UNION_BANK", "IOB",
                  "CENTRAL_BANK", "PUNJAB_SIND", "BOM", "UCO"]
PRIVATE_BANKS = ["ICICI", "HDFC", "KOTAK_MAHINDRA", "AXIS", "INDUSIND"]

# Neutral app-level theme shown for General Chat and RBI Guidelines, where
# no single bank's color applies.
GENERAL_THEME = {
    "accent": "#003366", "mid": "#005599", "dark": "#001f3d",
    "tint": "#e8f0fe", "tint2": "#c5d8f7",
}
RBI_THEME = {
    "accent": "#3d2b6b", "mid": "#5c4394", "dark": "#221740",
    "tint": "#eeebf7", "tint2": "#cfc3e8",
}

_TENURE_OPTIONS = {
    "1 month": 30, "3 months": 90, "6 months": 180,
    "1 year": 365, "2 years": 730, "3 years": 1095,
    "5 years": 1825, "10 years": 3650,
}

# Hoisted to module scope — both Compare and Insights & Tools (and the
# chat-digest shortcut) need automation/ importable and need to resolve
# an automation bank_id (lowercase) back to its real BANK_UI label.
_AUTOMATION_DIR = str(Path(__file__).resolve().parent / "automation")
if _AUTOMATION_DIR not in sys.path:
    sys.path.insert(0, _AUTOMATION_DIR)


def resolve_bank_label(bank_id: str) -> str:
    from gap_notes import to_faiss_code
    return BANK_UI.get(to_faiss_code(bank_id), {}).get("label", bank_id.upper())


_LOAN_PRODUCT_LABELS = {"home_loan": "Home Loan", "education_loan": "Education Loan"}


def _ordinal(n: int) -> str:
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _fd_positioning_phrase(bank_id: str, tenure: str) -> str:
    """Light "now Nth highest among tracked PSBs" context folded
    directly into a rate-change line/card — replaces the removed
    separate Banker View toggle (it "doesn't add value with sparse/no
    changes to show," per explicit user instruction; this stays useful
    to both audiences without a toggle). Empty string if bank_id isn't
    a tracked PSB or has no current peer data — the caller just omits
    the clause rather than showing a broken phrase."""
    from digest import peer_rank
    info = peer_rank(bank_id, tenure)
    if not info:
        return ""
    return f" — now {_ordinal(info['rank'])} highest among tracked PSBs ({info['total']})"


def render_digest(days: int = 7) -> str:
    """Plain-English rate-change digest — the SAME string is used by
    both the Insights & Tools panel and the 'what changed this week'
    chat shortcut below, one rendering path over the same
    automation/digest.py data. Covers all three tracked product types
    (FD, home loan, education loan) — a real rate move could happen in
    any of them, not just deposits. No Customer/Banker split (removed
    2026-08-21 — see PROJECT_STATUS.md §10b); an FD change gets light
    peer-positioning context folded directly into its own line instead.
    No 'new coverage added' section (removed 2026-08-22) — that's
    project/coverage tracking, not something a day-to-day user of this
    digest cares about; automation/digest.py's new_coverage() still
    exists for that purpose, just not surfaced here (see PROJECT_STATUS.
    md §10c — same 'keep the function, unwire the UI' precedent as
    eligibility.py/peer_comparison()). Formatting is deliberately plain
    (dashes, no markdown bold/headers) since the chat bubble renders
    this as raw text with '\\n' -> '<br>' only, not real markdown — the
    Insights panel uses the identical string via st.markdown so it
    still reads cleanly there."""
    from digest import loan_rate_changes, rate_changes, special_band_changes

    lines = [f"📈 Rate-Change Digest — last {days} days\n"]

    fd_changes = rate_changes(days)
    # Special-band changes are checked for BOTH customer types, unlike
    # rate_changes() above (general only, an existing/unrelated scope
    # limit not touched here) — the confirmed real HDFC case this fix
    # was built for (7.00%->7.10%) is a SENIOR-only move, so checking
    # general alone would silently keep missing it even after this fix.
    special_changes = special_band_changes(days, "general") + special_band_changes(days, "senior")
    loan_changes = {lt: loan_rate_changes(lt, days) for lt in _LOAN_PRODUCT_LABELS}
    any_changes = bool(fd_changes) or bool(special_changes) or any(loan_changes.values())

    if not any_changes:
        lines.append(
            f"No genuine rate changes detected across any tracked bank or "
            f"product (FD, home loan, education loan) in the last {days} days. "
            f"Rates have been flat since this pipeline began fetching — a real, "
            f"verified finding (checked by re-parsing every historical "
            f"snapshot, not just trusting the page-changed flag), not a "
            f"tracking gap."
        )
    else:
        for c in fd_changes:
            arrow = "up" if c["new_rate"] > c["old_rate"] else "down"
            label = resolve_bank_label(c["bank_id"])
            date_str = c["changed_at"].strftime("%d %b")
            phrase = _fd_positioning_phrase(c["bank_id"], c["tenure"])
            lines.append(
                f"- {label} — {c['tenure']} FD rate went {arrow}: "
                f"{c['old_rate']}% -> {c['new_rate']}%{phrase} ({date_str})"
            )
        for c in special_changes:
            arrow = "up" if c["new_rate"] > c["old_rate"] else "down"
            label = resolve_bank_label(c["bank_id"])
            date_str = c["changed_at"].strftime("%d %b")
            ct_label = " (Senior)" if c["customer_type"] == "senior" else ""
            # No peer-positioning phrase here — that's derived from
            # compare_for_tenure() at one of the 8 milestones, which by
            # definition doesn't apply to a special (non-milestone) band.
            lines.append(
                f"- {label} — {c['tenure_label']} FD rate went {arrow}: "
                f"{c['old_rate']}% -> {c['new_rate']}%{ct_label} ({date_str})"
            )
        for lt, plabel in _LOAN_PRODUCT_LABELS.items():
            for c in loan_changes[lt]:
                arrow = "up" if c["new_rate"] > c["old_rate"] else "down"
                label = resolve_bank_label(c["bank_id"])
                date_str = c["changed_at"].strftime("%d %b")
                lines.append(
                    f"- {label} — {plabel} ({c['tier_label']}) rate went {arrow}: "
                    f"{c['old_rate']}% -> {c['new_rate']}% ({date_str})"
                )

    return "\n".join(lines)


_DIGEST_INTENT_RE = re.compile(
    r"(digest|rate change|rates? chang|what'?s new|whats new|"
    r"new(ly)? (added|covered)|this week|past week|last week)",
    re.IGNORECASE,
)

_CALC_INTENT_RE = re.compile(
    r"(maturity value|worth at maturity|\bemi\b|monthly (installment|payment|instalment)|"
    r"how much (will|would).{0,40}(worth|maturity)|compound interest|"
    r"calculate.{0,20}(fd|deposit|loan)|loan calculator|fd calculator|"
    r"what would.{0,60}(be worth|maturity))",
    re.IGNORECASE,
)


def _parse_calculator_query(query: str) -> dict | None:
    """Structured-parameter extraction for the calculator chat shortcut
    (Insights & Tools Step 2) — Groq is used ONLY to turn free text into
    parameters (amount, bank, tenure, product); it never states a final
    number itself. The actual arithmetic always runs through
    automation/calculator.py's real fd_maturity()/loan_emi() afterward —
    same "LLM for language, deterministic code for any numeric claim"
    discipline the digest shortcut and the rest of this project already
    follow. Returns None only on an outright API/parse failure; a dict
    with possibly-null fields otherwise — the caller decides what's
    missing and asks for clarification rather than guessing a value."""
    from calculator import all_known_banks

    bank_ids = all_known_banks()
    bank_options = ", ".join(f'{bid} ("{resolve_bank_label(bid)}")' for bid in bank_ids)

    system_prompt = (
        "You extract structured parameters from a bank-calculator question. "
        "Respond with ONLY a JSON object, no other text, with exactly these keys:\n"
        '{"product": "fd" | "home_loan" | "education_loan" | null, '
        '"amount": <number in rupees, or null>, '
        '"bank_id": <one of the exact ids below, or null>, '
        '"tenure": <for product "fd", one of "1 month", "3 months", "6 months", '
        '"1 year", "2 years", "3 years", "5 years", "10 years" (nearest match); '
        'for a loan product, a plain number of years; or null>, '
        '"customer_type": "general" or "senior" (default "general" if not stated)}\n\n'
        f"Valid bank_id values: {bank_options}\n\n"
        "Convert amounts like '5 lakh' to 500000, '1 crore' to 10000000, "
        "'50k' to 50000. If you cannot confidently determine a field, use null "
        "for it rather than guessing."
    )

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": query},
            ],
            max_tokens=200,
            temperature=0.0,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception:
        return None


def render_calculator_answer(parsed: dict) -> str:
    """Turns a parsed {product, amount, bank_id, tenure, customer_type}
    dict into a plain-text chat answer, calling the SAME deterministic
    calculator.py functions the panel uses. Missing/unresolvable fields
    produce an honest clarification request, never a guessed default
    for amount or bank (tenure alone gets a sensible stated default,
    since a calculator needs SOME tenure to compute anything at all)."""
    from calculator import fd_maturity, loan_emi

    product = parsed.get("product")
    amount = parsed.get("amount")
    bank_id = parsed.get("bank_id")
    customer_type = parsed.get("customer_type") or "general"

    if product not in ("fd", "home_loan", "education_loan"):
        return (
            "I couldn't tell which product you meant (FD, home loan, or "
            "education loan). Try something like \"what would ₹5 lakh in a "
            "BOB 1-year FD be worth at maturity?\" or \"EMI for a ₹30 lakh "
            "HDFC home loan over 20 years?\""
        )
    if not amount:
        return "I couldn't tell the amount you meant — please include it, e.g. \"₹5 lakh\" or \"₹30,00,000\"."
    if not bank_id:
        return "I couldn't tell which bank you meant — please name one, e.g. \"BOB\", \"HDFC\", \"SBI\"."

    label = resolve_bank_label(bank_id)

    if product == "fd":
        tenure = parsed.get("tenure") if parsed.get("tenure") in _TENURE_OPTIONS else "1 year"
        result = fd_maturity(bank_id, tenure, amount, customer_type)
        if result is None:
            return f"{label} has no published FD rate for {tenure}."
        return (
            f"{label} — {result['tenure_label']} FD at {result['rate']}% p.a. "
            f"({result['variant_label']}):\n"
            f"Rs. {amount:,.0f} would mature to Rs. {result['maturity_value']:,.2f} "
            f"(Rs. {result['interest_earned']:,.2f} interest).\n"
            f"Assumes quarterly compounding — the exact per-bank compounding "
            f"frequency isn't published in our source data; quarterly is the "
            f"industry-standard convention for Indian bank FDs."
        )

    loan_type = product
    plabel = _LOAN_PRODUCT_LABELS[loan_type]
    tenure_years = parsed.get("tenure")
    try:
        tenure_years = float(tenure_years) if tenure_years else (20.0 if loan_type == "home_loan" else 7.0)
    except (TypeError, ValueError):
        tenure_years = 20.0 if loan_type == "home_loan" else 7.0

    result = loan_emi(bank_id, loan_type, amount, tenure_years)
    if result is None:
        return f"{label} has no published {plabel} rate."
    tier = result["tier_label"] or "flat published rate, no tiering"
    return (
        f"{label} — {plabel} at {result['rate']}% p.a. (best published tier: {tier}):\n"
        f"Rs. {amount:,.0f} over {tenure_years:.0f} years -> EMI of Rs. {result['emi']:,.2f}/month "
        f"(Rs. {result['total_payment']:,.2f} total payment, Rs. {result['total_interest']:,.2f} "
        f"total interest).\n"
        f"Uses this bank's BEST published rate — your actual approved rate depends "
        f"on eligibility (credit score, loan amount, institute category, etc.) per "
        f"the bank's own published tiers. Standard reducing-balance EMI formula."
    )


# ── "This Week's Changes" card UI (Insights & Tools panel only) ──────
# Per user-supplied visual reference (insights_tools_reference.md): a
# news-feed of small cards, NOT a table — deliberately a separate
# rendering path from render_digest() above (which stays plain text,
# since that one is shared with the raw-HTML chat bubble that only does
# '\n' -> '<br>', no real markdown/CSS support). Both read the exact
# same automation/digest.py functions — this is presentation-only, not
# a second data path.
#
# Pure inline `style="..."` attributes on plain <div>s, no separate
# <style> block and no external stylesheet — the earlier bug where
# injected CSS rendered as literal visible text (see project memory)
# came specifically from mixing <link>/<style> tags into one
# st.markdown call; a self-contained inline-styled div doesn't hit that
# failure mode. Verified live in the browser before trusting this,
# same discipline as every other feature in this project.
_DIGEST_COLORS = {
    "ink": "#1B2A4A", "gold": "#C89B3C", "paper": "#EAF1F7",
    "paper_raised": "#FFFFFF", "line": "#D3DFEA", "text": "#2E3440",
    "text_mute": "#6C7A8C", "good": "#3F7D58", "warn": "#B5652E",
}


def _digest_card_html(title: str, headline: str, date_str: str, border_color: str) -> str:
    c = _DIGEST_COLORS
    date_html = (
        f'<div style="font-family:\'Courier New\',monospace;color:{c["text_mute"]};'
        f'font-size:10.5px;white-space:nowrap;margin-left:12px;">{date_str}</div>'
        if date_str else ""
    )
    return (
        f'<div style="background:{c["paper_raised"]};border:1px solid {c["line"]};'
        f'border-left:3px solid {border_color};border-radius:8px;padding:12px 16px;'
        f'margin-bottom:10px;display:flex;justify-content:space-between;'
        f'align-items:center;">'
        f'<div><div style="font-weight:700;color:{c["ink"]};font-size:13.5px;">{title}</div>'
        f'<div style="color:{c["text"]};font-size:13px;margin-top:2px;">{headline}</div></div>'
        f'{date_html}</div>'
    )


def render_digest_cards(days: int = 7) -> None:
    """"This Week's Changes" — one small card per real rate change
    (green left border = rate rose, orange = rate fell), across all
    three tracked product types. No Customer/Banker toggle (removed
    2026-08-21, see PROJECT_STATUS.md §10b) — an FD change card gets
    light peer-positioning context folded directly into its own
    headline instead, useful to both audiences without a separate view.
    No "new coverage added" cards (removed 2026-08-22, see §10c) —
    that's project/coverage tracking, not something a day-to-day user
    of this digest cares about; when there's nothing to report, this
    shows a single clean "no changes" card, nothing else."""
    from digest import loan_rate_changes, rate_changes, special_band_changes
    c = _DIGEST_COLORS

    fd_changes = rate_changes(days)
    # Both customer types, unlike rate_changes() above — see the matching
    # comment in render_digest(); the confirmed real HDFC case this fix
    # targets is a SENIOR-only rate move.
    special_changes = special_band_changes(days, "general") + special_band_changes(days, "senior")
    loan_changes = {lt: loan_rate_changes(lt, days) for lt in _LOAN_PRODUCT_LABELS}
    any_changes = bool(fd_changes) or bool(special_changes) or any(loan_changes.values())

    if not any_changes:
        st.markdown(
            _digest_card_html(
                "No rate changes this week",
                f"Every tracked bank's rate (FD, home loan, and education loan) "
                f"has been flat for the last {days} days — verified by "
                f"re-parsing historical snapshots, not just trusting a "
                f"page-changed flag.",
                "", c["gold"],
            ),
            unsafe_allow_html=True,
        )
        return

    for ch in fd_changes:
        border = c["good"] if ch["new_rate"] > ch["old_rate"] else c["warn"]
        label = resolve_bank_label(ch["bank_id"])
        phrase = _fd_positioning_phrase(ch["bank_id"], ch["tenure"])
        headline = f"{ch['tenure']} FD: {ch['old_rate']}% → {ch['new_rate']}%{phrase}"
        date_str = ch["changed_at"].strftime("%d %b %Y")
        st.markdown(_digest_card_html(label, headline, date_str, border), unsafe_allow_html=True)
    for ch in special_changes:
        border = c["good"] if ch["new_rate"] > ch["old_rate"] else c["warn"]
        label = resolve_bank_label(ch["bank_id"])
        ct_label = " (Senior)" if ch["customer_type"] == "senior" else ""
        # No peer-positioning phrase — see render_digest()'s matching comment.
        headline = f"{ch['tenure_label']} FD: {ch['old_rate']}% → {ch['new_rate']}%{ct_label}"
        date_str = ch["changed_at"].strftime("%d %b %Y")
        st.markdown(_digest_card_html(label, headline, date_str, border), unsafe_allow_html=True)
    for lt, plabel in _LOAN_PRODUCT_LABELS.items():
        for ch in loan_changes[lt]:
            border = c["good"] if ch["new_rate"] < ch["old_rate"] else c["warn"]  # lower loan rate = favourable
            label = resolve_bank_label(ch["bank_id"])
            headline = f"{plabel} ({ch['tier_label']}): {ch['old_rate']}% → {ch['new_rate']}%"
            date_str = ch["changed_at"].strftime("%d %b %Y")
            st.markdown(_digest_card_html(label, headline, date_str, border), unsafe_allow_html=True)


# ── Calculator (Insights & Tools, Step 2) ─────────────────────────────
# Same card-styling helpers as "This Week's Changes" — one result card,
# same inline-HTML-on-plain-div technique. All arithmetic lives in
# automation/calculator.py (fd_maturity/loan_emi); this function is
# presentation + Streamlit widget wiring only, same separation as the
# digest's UI functions vs. digest.py's data functions.
def _result_card_html(title: str, big_value: str, subline: str, note: str) -> str:
    c = _DIGEST_COLORS
    return (
        f'<div style="background:{c["paper_raised"]};border:1px solid {c["line"]};'
        f'border-radius:8px;padding:16px 20px;margin-top:10px;">'
        f'<div style="color:{c["text_mute"]};font-size:12px;">{title}</div>'
        f'<div style="color:{c["ink"]};font-size:1.6rem;font-weight:700;margin-top:2px;">{big_value}</div>'
        f'<div style="color:{c["text"]};font-size:13px;margin-top:4px;">{subline}</div>'
        f'<div style="color:{c["text_mute"]};font-size:11px;margin-top:10px;">{note}</div>'
        f'</div>'
    )


def render_calculator() -> None:
    """All inputs live inside an st.form per product — Streamlit only
    reruns/recomputes on the "Calculate" submit click, not on every
    keystroke/field change. Fixes a real reported UX problem: without a
    form, EVERY field edit triggered an immediate DB query + recompute,
    which felt slow and confusing mid-edit (e.g. while still typing an
    amount). The last computed result is cached in st.session_state and
    always re-rendered (even on reruns triggered by something else,
    like switching tabs) so it doesn't vanish until Calculate is
    pressed again."""
    from calculator import fd_banks, fd_maturity, loan_banks, loan_emi_at_rate

    product = st.radio(
        "Product", ["FD / Deposit", "Home Loan EMI", "Education Loan EMI", "Vehicle Loan EMI"],
        horizontal=True, key="calc_product",
    )

    if product == "FD / Deposit":
        with st.form("calc_fd_form"):
            col1, col2 = st.columns(2)
            with col1:
                amount = st.number_input(
                    "Amount (Rs.)", min_value=1000, value=100000, step=1000, key="calc_fd_amount",
                )
                tenure_name = st.selectbox(
                    "Tenure", list(_TENURE_OPTIONS.keys()), index=3, key="calc_fd_tenure",
                )
            with col2:
                customer_type = st.radio(
                    "Customer type", ["general", "senior"], horizontal=True, key="calc_fd_customer",
                )
                banks = fd_banks()
                bank_id = st.selectbox(
                    "Bank", banks, format_func=resolve_bank_label, key="calc_fd_bank",
                )
            calculate = st.form_submit_button("Calculate")

        if calculate:
            st.session_state["calc_fd_result"] = fd_maturity(bank_id, tenure_name, amount, customer_type)
            st.session_state["calc_fd_ctx"] = {"bank_id": bank_id, "tenure_name": tenure_name, "amount": amount}

        if "calc_fd_result" not in st.session_state:
            st.caption("Fill in the fields above and click Calculate.")
            return

        result = st.session_state["calc_fd_result"]
        ctx = st.session_state["calc_fd_ctx"]
        if result is None:
            st.info(f"{resolve_bank_label(ctx['bank_id'])} has no published FD rate for {ctx['tenure_name']}.")
        else:
            st.markdown(
                _result_card_html(
                    f"Maturity value — {resolve_bank_label(ctx['bank_id'])}, "
                    f"{result['tenure_label']}, {result['rate']}% p.a. ({result['variant_label']})",
                    f"Rs. {result['maturity_value']:,.2f}",
                    f"+ Rs. {result['interest_earned']:,.2f} interest on Rs. {ctx['amount']:,.0f} principal",
                    f"Assumes {result['compounding']} compounding — the exact per-bank compounding "
                    f"frequency isn't published in our source data; quarterly is the industry-standard "
                    f"convention for Indian bank FDs, not a bank-specific fact.",
                ),
                unsafe_allow_html=True,
            )

    else:
        loan_type = {
            "Home Loan EMI": "home_loan",
            "Education Loan EMI": "education_loan",
            "Vehicle Loan EMI": "vehicle_loan",
        }[product]
        default_amount = {"home_loan": 3000000, "education_loan": 500000, "vehicle_loan": 800000}[loan_type]
        default_years = {"home_loan": 20, "education_loan": 7, "vehicle_loan": 7}[loan_type]
        # Same default-rate-per-product convention as render_switch()'s
        # loan branch — a starting point, not a guess at any specific
        # bank's rate.
        default_rate = {"home_loan": 8.5, "education_loan": 12.0, "vehicle_loan": 9.0}[loan_type]
        # Vehicle loans start EMI immediately on disbursement — no real
        # moratorium period the way education loans have (course + grace
        # period before repayment starts). The moratorium input below is
        # education-loan-specific UI, not shown for this product; loan_emi()
        # itself already defaults moratorium_years=0 (a no-op) regardless.
        show_moratorium = loan_type != "vehicle_loan"

        with st.form(f"calc_{loan_type}_form"):
            col1, col2 = st.columns(2)
            with col1:
                amount = st.number_input(
                    "Loan amount (Rs.)", min_value=10000, value=default_amount, step=10000,
                    key=f"calc_{loan_type}_amount",
                )
                tenure_years = st.number_input(
                    "Repayment tenure (years)", min_value=1, max_value=30, value=default_years,
                    key=f"calc_{loan_type}_tenure",
                    help="EMI-paying period only — after any moratorium, not including it.",
                )
                # Manual, editable rate — restored after being reported
                # missing (a bank's best-published tier is only a starting
                # estimate; the applicant's actually-approved rate depends
                # on eligibility and commonly differs from it, same reason
                # render_switch()'s loan branch has always taken the
                # customer's own rate directly rather than looking one up).
                rate = st.number_input(
                    "Interest rate (% p.a.)", min_value=0.0, max_value=25.0,
                    value=default_rate, step=0.05, format="%.2f", key=f"calc_{loan_type}_rate",
                    help="Your loan's actual/approved rate — edit this to match your real "
                         "sanction letter or quote. The bank picker below is for "
                         "reference/labeling only and doesn't feed this number.",
                )
            with col2:
                banks = loan_banks(loan_type)
                bank_id = st.selectbox(
                    "Bank (for reference/labeling only)", banks, format_func=resolve_bank_label,
                    key=f"calc_{loan_type}_bank",
                )
                if show_moratorium:
                    moratorium_years = st.number_input(
                        "Moratorium period (years, optional)", min_value=0.0, max_value=10.0,
                        value=0.0, step=0.5, key=f"calc_{loan_type}_moratorium",
                        help="Course duration + grace period, common for education loans, "
                             "during which interest accrues and is added to the principal "
                             "before EMI repayment starts. Leave at 0 if not applicable.",
                    )
                else:
                    moratorium_years = 0.0
            calculate = st.form_submit_button("Calculate")

        result_key = f"calc_{loan_type}_result"
        ctx_key = f"calc_{loan_type}_ctx"
        if calculate:
            st.session_state[result_key] = loan_emi_at_rate(amount, rate, tenure_years, moratorium_years)
            st.session_state[ctx_key] = {
                "bank_id": bank_id, "moratorium_years": moratorium_years, "product": product,
            }

        if result_key not in st.session_state:
            st.caption("Fill in the fields above and click Calculate.")
            return

        result = st.session_state[result_key]
        ctx = st.session_state[ctx_key]
        moratorium_note = (
            f" A {ctx['moratorium_years']:.1f}-year moratorium adds Rs. {result['moratorium_interest']:,.2f} "
            f"in accrued interest to the principal (Rs. {result['disbursed_principal']:,.2f} -> "
            f"Rs. {result['capitalized_principal']:,.2f}) before EMI is calculated."
            if ctx["moratorium_years"] > 0 else ""
        )
        st.markdown(
            _result_card_html(
                f"Monthly EMI — {resolve_bank_label(ctx['bank_id'])}, {result['rate']}% p.a. (your rate)",
                f"Rs. {result['emi']:,.2f} / month",
                f"Total payment Rs. {result['total_payment']:,.2f} over "
                f"{result['tenure_months']} months (Rs. {result['total_interest']:,.2f} total interest)",
                f"Computed at the rate you entered — not looked up from the bank's "
                f"published tiers, since your actually-approved rate (eligibility, "
                f"credit score, loan amount, institute category, etc.) commonly "
                f"differs from a bank's best-case published rate. Standard "
                f"reducing-balance EMI formula.{moratorium_note}",
            ),
            unsafe_allow_html=True,
        )


# ── Latest Banking News (Insights & Tools, Step 3) ────────────────────
# Real headlines from live RSS feeds only (automation/news.py) — never
# the full article body, per this project's standing copyright
# discipline (RBI PDFs are cited/linked, never reproduced; here, each
# outlet's own one-line RSS description is shown, truncated, never
# rewritten into new "paraphrase" text). Moneycontrol was pre-flight
# checked and excluded — every one of its RSS feeds is dead (some
# frozen since 2016) and its live page isn't cleanly scrapable without
# JS rendering; see PROJECT_STATUS.md for the full pre-flight table.
@st.cache_data(ttl=900, show_spinner=False)
def _cached_headlines(limit: int) -> list[dict]:
    from news import archive_headlines, fetch_headlines
    headlines = fetch_headlines(limit=limit)
    # Runs once per real feed fetch (this function itself is behind
    # st.cache_data(ttl=900)), not once per page view — archive_headlines()
    # dedupes on link regardless, but gating it here also avoids a DB
    # round-trip on every cache-hit render.
    archive_headlines(headlines)
    return headlines


def _news_card_html(headline: str, summary: str, outlet: str, date_label: str, link: str) -> str:
    c = _DIGEST_COLORS
    date_html = f' · {date_label}' if date_label else ""
    return (
        f'<div style="background:{c["paper_raised"]};border:1px solid {c["line"]};'
        f'border-left:3px solid {c["gold"]};border-radius:8px;padding:12px 16px;'
        f'margin-bottom:10px;">'
        f'<a href="{link}" target="_blank" style="text-decoration:none;">'
        f'<div style="font-weight:700;color:{c["ink"]};font-size:14px;">{headline}</div></a>'
        f'<div style="color:{c["text"]};font-size:12.5px;margin-top:4px;">{summary}</div>'
        f'<div style="color:{c["text_mute"]};font-size:11px;margin-top:6px;">'
        f'{outlet}{date_html} · <a href="{link}" target="_blank" '
        f'style="color:{c["text_mute"]};">Read original →</a></div>'
        f'</div>'
    )


def render_news(limit: int = 5) -> None:
    headlines = _cached_headlines(limit)
    if not headlines:
        st.info(
            "Couldn't reach the news feeds right now — this reflects a real "
            "fetch issue, not a lack of news. Try again shortly."
        )
        return
    for h in headlines:
        st.markdown(
            _news_card_html(h["headline"], h["summary"], h["outlet"], h["pub_date_label"], h["link"]),
            unsafe_allow_html=True,
        )


@st.cache_data(ttl=300, show_spinner=False)
def _cached_archived_headlines(days: int) -> list[dict]:
    from news import get_archived_headlines
    return get_archived_headlines(days=days)


def render_news_archive(days: int = 30) -> None:
    """Everything news_archive has accumulated in the last `days` days —
    a persistent DuckDB record of headlines this app has actually seen,
    separate from the live "latest 5" view above (which only ever shows
    what the feeds currently return, and can't show something that later
    scrolled off the RSS feed's own short window)."""
    headlines = _cached_archived_headlines(days)
    if not headlines:
        st.info(
            "No archived headlines yet — the archive fills in as the Latest "
            "tab is viewed and the feed refreshes (every 15 minutes)."
        )
        return
    st.caption(f"{len(headlines)} headline(s) archived in the last {days} days.")
    for h in headlines:
        st.markdown(
            _news_card_html(h["headline"], h["summary"], h["outlet"], h["pub_date_label"], h["link"]),
            unsafe_allow_html=True,
        )


# ── IBA News (used inside Banker's View, §18/§19) ──────────────────────
# Indian Banks' Association — the industry body, not a news outlet; not
# merged into "Latest Banking News" per the original explicit
# instruction (§13). Originally its own standalone Insights & Tools
# tab; that separate tab was removed 2026-08-24 (§19) since it fully
# duplicated what Banker's View already showed — render_iba_news() is
# now called only from render_banker_view(). Same card visual language
# as the ET/Business Standard news cards (reuses _news_card_html
# directly), cached for a full day rather than 15 minutes — IBA's
# circulars update far less often than market news (most recent one
# found during the pre-flight check was already 17 days old), so a
# 15-minute cache would just be wasted fetches.
@st.cache_data(ttl=86400, show_spinner=False)
def _cached_iba_circulars(limit: int) -> list[dict]:
    from iba_news import fetch_circulars
    return fetch_circulars(limit=limit)


def render_iba_news(limit: int = 6) -> None:
    circulars = _cached_iba_circulars(limit)
    if not circulars:
        st.info(
            "Couldn't reach IBA's circulars index right now — this reflects a "
            "real fetch issue, not a lack of circulars. Try again shortly."
        )
        return
    for c in circulars:
        st.markdown(
            _news_card_html(c["headline"], c["category"], c["outlet"], c["pub_date_label"], c["link"]),
            unsafe_allow_html=True,
        )


# ── Should I Switch? (Insights & Tools, Step 4) ────────────────────────
# Customer enters their OWN current product (bank, actual rate, amount,
# tenure — the rate they actually hold may differ from that bank's
# currently published rate, since it could've been opened earlier);
# automation/switch.py ranks the top 3 real alternatives across every
# tracked bank and reports each one's benefit. Reuses the same
# _result_card_html() visual language as the Calculator (no new card
# design) — one card per ranked alternative.
#
# FD and loans are genuinely inverted comparisons (higher rate wins for
# a deposit, lower rate wins for a loan) — switch.py's _switch_fd() and
# _switch_loan() are deliberately separate code paths for this reason,
# same care already applied to the digest's card coloring. The rank
# badge here (🥇/🥈/🥉) and the "more at maturity" vs "saved in total
# interest" wording are the UI-side reflection of that same distinction
# — never shared/generic phrasing that could paper over the direction
# flip.
_RANK_BADGES = ["🥇 #1", "🥈 #2", "🥉 #3"]


def render_switch() -> None:
    """Same form-per-product pattern as render_calculator() — inputs
    only take effect on the "Compare" submit click, not on every
    keystroke. This function ranks against every OTHER tracked bank
    (a real DB query + top-3 loop), so the pre-form reactive version
    re-ran that on every single field edit, which is exactly the
    "starts fetching, confusing" complaint this was built to fix."""
    from calculator import fd_banks, loan_banks
    from switch import EXCLUDED_COSTS_NOTE, should_i_switch

    product = st.radio(
        "Product", ["FD / Deposit", "Home Loan", "Education Loan", "Vehicle Loan"],
        horizontal=True, key="switch_product",
    )

    if product == "FD / Deposit":
        st.caption(
            "Compares your CURRENT FD's actual rate against the top 3 "
            "real rates published by other tracked banks today."
        )
        with st.form("switch_fd_form"):
            col1, col2 = st.columns(2)
            with col1:
                banks = fd_banks()
                current_bank_id = st.selectbox(
                    "Your current bank", banks, format_func=resolve_bank_label, key="switch_fd_bank",
                )
                current_rate = st.number_input(
                    "Your current FD rate (% p.a.)", min_value=0.0, max_value=15.0,
                    value=6.0, step=0.05, format="%.2f", key="switch_fd_rate",
                )
                amount = st.number_input(
                    "Amount (Rs.)", min_value=1000, value=500000, step=1000, key="switch_fd_amount",
                )
            with col2:
                tenure_name = st.selectbox(
                    "Tenure", list(_TENURE_OPTIONS.keys()), index=3, key="switch_fd_tenure",
                )
                customer_type = st.radio(
                    "Customer type", ["general", "senior"], horizontal=True, key="switch_fd_customer",
                )
            compare = st.form_submit_button("Compare")

        if compare:
            st.session_state["switch_fd_result"] = should_i_switch(
                "fd", current_bank_id, current_rate, amount, tenure_name, customer_type,
            )
            st.session_state["switch_fd_ctx"] = {
                "bank_id": current_bank_id, "rate": current_rate, "amount": amount,
                "tenure_name": tenure_name, "customer_type": customer_type,
            }

        if "switch_fd_result" not in st.session_state:
            st.caption("Fill in the fields above and click Compare.")
            return

        result = st.session_state["switch_fd_result"]
        ctx = st.session_state["switch_fd_ctx"]
        if result is None:
            st.info(f"No published FD rate found for {ctx['tenure_name']} across any tracked bank.")
            return
        if not result["ranked"]:
            st.info("No other tracked bank has a published rate for this tenure to compare against.")
            return

        current_label = resolve_bank_label(ctx["bank_id"])
        st.caption(
            f"Your FD — {current_label}, {ctx['rate']}% p.a. -> Rs. {result['current_value']:,.2f} "
            f"at maturity ({result['banks_compared']} banks compared, {ctx['tenure_name']}, {ctx['customer_type']})."
        )
        for i, alt in enumerate(result["ranked"]):
            label = resolve_bank_label(alt["bank_id"])
            benefit = alt["benefit"]
            big_value = (
                f"Rs. {benefit:,.2f} more at maturity" if benefit >= 0
                else f"Rs. {abs(benefit):,.2f} less at maturity"
            )
            st.markdown(
                _result_card_html(
                    f"{_RANK_BADGES[i]} {label} — {alt['rate']}% p.a. ({alt['label']}) "
                    f"[{alt['rate_diff']:+.2f}pp vs your rate]",
                    big_value,
                    f"Rs. {ctx['amount']:,.0f} would reach Rs. {alt['alt_value']:,.2f} at {label}, "
                    f"vs Rs. {result['current_value']:,.2f} at your current bank.",
                    EXCLUDED_COSTS_NOTE,
                ),
                unsafe_allow_html=True,
            )

    else:
        loan_type = {
            "Home Loan": "home_loan",
            "Education Loan": "education_loan",
            "Vehicle Loan": "vehicle_loan",
        }[product]
        default_amount = {"home_loan": 3000000, "education_loan": 500000, "vehicle_loan": 800000}[loan_type]
        default_years = {"home_loan": 20, "education_loan": 7, "vehicle_loan": 7}[loan_type]
        default_rate = {"home_loan": 8.5, "education_loan": 12.0, "vehicle_loan": 9.0}[loan_type]
        # Same reasoning as Calculator: vehicle loans start EMI immediately
        # on disbursement, no real moratorium period — hide the education-
        # loan-specific moratorium input for this product.
        show_moratorium = loan_type != "vehicle_loan"

        st.caption(
            f"Compares your CURRENT {product} rate against the top 3 real "
            f"rates published by other tracked banks today — a LOWER rate "
            f"is the win for a loan."
        )
        with st.form(f"switch_{loan_type}_form"):
            col1, col2 = st.columns(2)
            with col1:
                banks = loan_banks(loan_type)
                current_bank_id = st.selectbox(
                    "Your current bank", banks, format_func=resolve_bank_label,
                    key=f"switch_{loan_type}_bank",
                )
                current_rate = st.number_input(
                    f"Your current {product} rate (% p.a.)", min_value=0.0, max_value=25.0,
                    value=default_rate, step=0.05, format="%.2f", key=f"switch_{loan_type}_rate",
                )
            with col2:
                amount = st.number_input(
                    "Loan amount (Rs.)", min_value=10000, value=default_amount, step=10000,
                    key=f"switch_{loan_type}_amount",
                )
                tenure_years = st.number_input(
                    "Repayment tenure (years)", min_value=1, max_value=30, value=default_years,
                    key=f"switch_{loan_type}_tenure",
                    help="EMI-paying period only — after any moratorium, not including it.",
                )
                if show_moratorium:
                    moratorium_years = st.number_input(
                        "Moratorium period (years, optional)", min_value=0.0, max_value=10.0,
                        value=0.0, step=0.5, key=f"switch_{loan_type}_moratorium",
                        help="Course duration + grace period, common for education loans, "
                             "during which interest accrues and is added to the principal "
                             "before EMI repayment starts. Applied to your current loan AND "
                             "every alternative shown below — leave at 0 if not applicable.",
                    )
                else:
                    moratorium_years = 0.0
            compare = st.form_submit_button("Compare")

        result_key = f"switch_{loan_type}_result"
        ctx_key = f"switch_{loan_type}_ctx"
        if compare:
            st.session_state[result_key] = should_i_switch(
                loan_type, current_bank_id, current_rate, amount, tenure_years,
                moratorium_years=moratorium_years,
            )
            st.session_state[ctx_key] = {
                "bank_id": current_bank_id, "rate": current_rate, "tenure_years": tenure_years,
                "moratorium_years": moratorium_years, "product": product,
            }

        if result_key not in st.session_state:
            st.caption("Fill in the fields above and click Compare.")
            return

        result = st.session_state[result_key]
        ctx = st.session_state[ctx_key]
        if result is None:
            st.info(f"No published {ctx['product']} rate found across any tracked bank.")
            return
        if not result["ranked"]:
            st.info(f"No other tracked bank has a published {ctx['product']} rate to compare against.")
            return

        current_label = resolve_bank_label(ctx["bank_id"])
        moratorium_note = (
            f" (includes a {ctx['moratorium_years']:.1f}-year moratorium, applied identically to your "
            f"current loan and every alternative below)" if ctx["moratorium_years"] > 0 else ""
        )
        st.caption(
            f"Your {ctx['product']} — {current_label}, {ctx['rate']}% p.a. -> EMI "
            f"Rs. {result['current_emi']:,.2f}/month, Rs. {result['current_total_interest']:,.2f} "
            f"total interest over {ctx['tenure_years']:.0f} years{moratorium_note} "
            f"({result['banks_compared']} banks compared)."
        )
        for i, alt in enumerate(result["ranked"]):
            label = resolve_bank_label(alt["bank_id"])
            benefit = alt["benefit"]
            big_value = (
                f"Rs. {benefit:,.2f} saved in total interest" if benefit >= 0
                else f"Rs. {abs(benefit):,.2f} more in total interest"
            )
            st.markdown(
                _result_card_html(
                    f"{_RANK_BADGES[i]} {label} — {alt['rate']}% p.a. (tier: {alt['label']}) "
                    f"[{alt['rate_diff']:+.2f}pp vs your rate]",
                    big_value,
                    f"EMI would be Rs. {alt['alt_emi']:,.2f}/month at {label} "
                    f"(Rs. {alt['alt_total_interest']:,.2f} total interest), vs your current "
                    f"Rs. {result['current_emi']:,.2f}/month (Rs. {result['current_total_interest']:,.2f}).",
                    EXCLUDED_COSTS_NOTE,
                ),
                unsafe_allow_html=True,
            )


# ── Banker's View (Insights & Tools, Step 5) ────────────────────────────
# A genuinely separate section for bank staff, not a toggle bolted onto
# the customer-facing digest — that pattern was tried and explicitly
# rejected 2026-08-21 (see PROJECT_STATUS.md §10b: "doesn't add value
# with sparse/no changes to show"). Built incrementally, component by
# component, each verified with real output before the next — same
# discipline as every other Insights & Tools feature.
#
# Component 1 (this pass): Peer Positioning. Re-wires
# digest.peer_comparison() (built 2026-08-21, unwired since — see its
# own docstring) as a standalone snapshot of CURRENT standing, not a
# change log — it never needed rate movement to be useful, only the
# DIGEST framing did. PSB banks compare against PSB peers, private
# banks against private peers, auto-detected from whichever bank is
# selected — never mixed.
def render_banker_peer_positioning() -> None:
    from calculator import fd_banks
    from digest import peer_comparison

    st.caption(
        "Snapshot of where a bank currently stands among its real peer "
        "group (PSB vs. PSB, private vs. private) for a given FD "
        "tenure — works regardless of whether rates changed recently."
    )

    with st.form("banker_peer_form"):
        col1, col2 = st.columns(2)
        with col1:
            banks = fd_banks()
            bank_id = st.selectbox(
                "Bank", banks, format_func=resolve_bank_label, key="banker_peer_bank",
            )
            tenure_name = st.selectbox(
                "Tenure", list(_TENURE_OPTIONS.keys()), index=3, key="banker_peer_tenure",
            )
        with col2:
            customer_type = st.radio(
                "Customer type", ["general", "senior"], horizontal=True, key="banker_peer_customer",
            )
        check = st.form_submit_button("Check Positioning")

    if check:
        st.session_state["banker_peer_result"] = peer_comparison(bank_id, tenure_name, customer_type)
        st.session_state["banker_peer_ctx"] = {
            "bank_id": bank_id, "tenure_name": tenure_name, "customer_type": customer_type,
        }

    if "banker_peer_result" not in st.session_state:
        st.caption("Fill in the fields above and click Check Positioning.")
        return

    result = st.session_state["banker_peer_result"]
    ctx = st.session_state["banker_peer_ctx"]
    label = resolve_bank_label(ctx["bank_id"])

    if result is None:
        st.info(
            f"{label} isn't in a tracked peer group (PSB or private), or has no "
            f"published rate for {ctx['tenure_name']} — nothing to compare."
        )
        return

    st.markdown(
        _result_card_html(
            f"{label} — {result['peer_group']} peer positioning, {ctx['tenure_name']}, {ctx['customer_type']}",
            f"{_ordinal(result['rank'])} highest of {result['total']} {result['peer_group']}s",
            f"Our rate: {result['our_rate']}% — peer average: {result['peer_avg']}% "
            f"({result['diff']:+.3f}pp {'above' if result['diff'] >= 0 else 'below'} peer average, "
            f"{result['peer_count']} peers compared).",
            "Snapshot of current standing, not a change log — reflects today's "
            "published rates regardless of recent movement. FD only for now.",
        ),
        unsafe_allow_html=True,
    )

    with st.expander(f"Full {result['peer_group']} peer table ({result['total']} banks)"):
        for row in result["table"]:
            marker = " **← this bank**" if row["bank_id"] == ctx["bank_id"] else ""
            st.markdown(f"- {resolve_bank_label(row['bank_id'])}: {row['rate']}%{marker}")


# Component 2: Rate movement, banker-framed. Reuses the EXACT SAME data
# functions "This Week's Changes" already calls (rate_changes(),
# loan_rate_changes()) — one data path, two presentations, per the
# roadmap's own original principle ("Banker View reframes the same
# underlying data... rather than maintaining a second data path").
# Deliberately no color-coded good/bad border here (unlike the customer
# digest's FD/loan-inverted coloring) — whether OUR OWN bank's rate
# move is "good" depends on margin/strategy considerations this app has
# no data on, so this stays a neutral, direction-stated prompt to
# review, not a value judgment this tool isn't positioned to make.
def render_banker_rate_movement() -> None:
    from digest import loan_rate_changes, peer_comparison, rate_changes

    st.caption(
        "Same underlying rate-change data as \"This Week's Changes\" — "
        "reframed for competitive review, not a second data path."
    )

    with st.spinner("Checking rate history across all banks and products..."):
        fd_changes = rate_changes(days=7)
        loan_changes = {lt: loan_rate_changes(lt, days=7) for lt in _LOAN_PRODUCT_LABELS}

    any_changes = bool(fd_changes) or any(loan_changes.values())
    if not any_changes:
        st.info(
            "No genuine rate changes detected across any tracked bank or product "
            "in the last 7 days — nothing to review for competitive positioning "
            "this week. Same real finding as the customer digest; verified by "
            "re-parsing historical snapshots, not a raw \"page changed\" flag."
        )
        return

    c = _DIGEST_COLORS
    for ch in fd_changes:
        label = resolve_bank_label(ch["bank_id"])
        arrow = "up" if ch["new_rate"] > ch["old_rate"] else "down"
        peer = peer_comparison(ch["bank_id"], ch["tenure"], "general")
        positioning = (
            f"now {_ordinal(peer['rank'])} highest of {peer['total']} {peer['peer_group']}s"
            if peer else "not in a tracked peer group"
        )
        headline = (
            f"{ch['tenure']} FD moved {arrow}: {ch['old_rate']}% → {ch['new_rate']}% "
            f"— {positioning}. Review our positioning."
        )
        date_str = ch["changed_at"].strftime("%d %b %Y")
        st.markdown(_digest_card_html(label, headline, date_str, c["gold"]), unsafe_allow_html=True)

    for lt, plabel in _LOAN_PRODUCT_LABELS.items():
        for ch in loan_changes[lt]:
            label = resolve_bank_label(ch["bank_id"])
            direction = "cheaper" if ch["new_rate"] < ch["old_rate"] else "pricier"
            headline = (
                f"{plabel} ({ch['tier_label']}) moved {direction}: "
                f"{ch['old_rate']}% → {ch['new_rate']}%. Review our positioning."
            )
            date_str = ch["changed_at"].strftime("%d %b %Y")
            st.markdown(_digest_card_html(label, headline, date_str, c["gold"]), unsafe_allow_html=True)


# Component 4: Objection-handling lookup. Banker enters what a
# customer claims a NAMED competitor offers; checks it against our own
# real tracked data. Deliberately reuses compare_for_tenure()/
# compare_loan() as-is — the exact same lookups Compare/Calculator
# already trust — no new comparison logic written for this tool, per
# explicit instruction. A structured form (bank/product/rate dropdowns
# and number input), not free-text parsing — this project's standing
# discipline is to avoid LLM extraction for anything a deterministic
# form can capture just as well, and every claim here needs an exact
# number, not a fuzzy sentence.
def render_banker_objection_check() -> None:
    from calculator import fd_banks, loan_banks
    from compare_loan_rates import compare_loan
    from compare_rates import compare_for_tenure
    from digest import TENURE_MILESTONES

    st.caption(
        "Enter what a customer claims another bank offers — checks it "
        "against our real tracked data rather than confirming or "
        "denying a competitor's rate from memory."
    )

    product = st.radio(
        "Product the claim is about", ["FD / Deposit", "Home Loan", "Education Loan", "Vehicle Loan"],
        horizontal=True, key="banker_obj_product",
    )

    if product == "FD / Deposit":
        with st.form("banker_obj_fd_form"):
            col1, col2 = st.columns(2)
            with col1:
                banks = fd_banks()
                claimed_bank_id = st.selectbox(
                    "Bank the customer named", banks, format_func=resolve_bank_label,
                    key="banker_obj_fd_bank",
                )
                claimed_rate = st.number_input(
                    "Rate the customer claims (% p.a.)", min_value=0.0, max_value=15.0,
                    value=7.0, step=0.05, format="%.2f", key="banker_obj_fd_rate",
                )
            with col2:
                tenure_name = st.selectbox(
                    "Tenure", list(_TENURE_OPTIONS.keys()), index=3, key="banker_obj_fd_tenure",
                )
                customer_type = st.radio(
                    "Customer type", ["general", "senior"], horizontal=True, key="banker_obj_fd_customer",
                )
            check = st.form_submit_button("Check Claim")

        if check:
            target_days = TENURE_MILESTONES.get(tenure_name)
            rows = compare_for_tenure(target_days, customer_type) if target_days else []
            real = next((r for r in rows if r["bank_id"] == claimed_bank_id), None)
            st.session_state["banker_obj_result"] = {"product": "fd", "real": real}
            st.session_state["banker_obj_ctx"] = {
                "bank_id": claimed_bank_id, "claimed_rate": claimed_rate,
                "tenure_name": tenure_name, "customer_type": customer_type,
            }

        if "banker_obj_result" not in st.session_state or st.session_state["banker_obj_result"]["product"] != "fd":
            st.caption("Fill in the fields above and click Check Claim.")
            return

        real = st.session_state["banker_obj_result"]["real"]
        ctx = st.session_state["banker_obj_ctx"]
        label = resolve_bank_label(ctx["bank_id"])

        if real is None:
            st.info(
                f"No published rate found for {label} at {ctx['tenure_name']} in our "
                f"tracked data — can't verify this claim either way."
            )
            return

        diff = round(ctx["claimed_rate"] - real["rate"], 3)
        if abs(diff) <= 0.05:
            verdict = f"Accurate — matches our tracked rate of {real['rate']}% almost exactly."
        elif diff > 0:
            verdict = (
                f"Inflated — customer's claim ({ctx['claimed_rate']}%) is "
                f"{diff:+.2f}pp higher than our tracked rate of {real['rate']}%."
            )
        else:
            verdict = (
                f"Understated — our tracked rate ({real['rate']}%) is actually "
                f"{abs(diff):.2f}pp higher than the customer's claim of "
                f"{ctx['claimed_rate']}% — worth correcting in our favor."
            )
        st.markdown(
            _result_card_html(
                f"{label} — {ctx['tenure_name']} FD claim check",
                verdict,
                f"Our tracked rate: {real['rate']}% p.a. ({real['tenure_label']}, "
                f"{real['variant_label']}, {ctx['customer_type']}).",
                "Caveat: general vs. senior-citizen rates differ (re-check the "
                "customer's actual eligibility); rates change, verify against the "
                "bank's live page before a final decision.",
            ),
            unsafe_allow_html=True,
        )

    else:
        loan_type = {"Home Loan": "home_loan", "Education Loan": "education_loan"}.get(
            product, "vehicle_loan"
        )
        with st.form(f"banker_obj_{loan_type}_form"):
            col1, col2 = st.columns(2)
            with col1:
                banks = loan_banks(loan_type)
                claimed_bank_id = st.selectbox(
                    "Bank the customer named", banks, format_func=resolve_bank_label,
                    key=f"banker_obj_{loan_type}_bank",
                )
            with col2:
                claimed_rate = st.number_input(
                    "Rate the customer claims (% p.a.)", min_value=0.0, max_value=25.0,
                    value=9.0, step=0.05, format="%.2f", key=f"banker_obj_{loan_type}_rate",
                )
            check = st.form_submit_button("Check Claim")

        if check:
            rows = compare_loan(loan_type)
            real = next((r for r in rows if r["bank_id"] == claimed_bank_id), None)
            st.session_state["banker_obj_result"] = {"product": loan_type, "real": real}
            st.session_state["banker_obj_ctx"] = {
                "bank_id": claimed_bank_id, "claimed_rate": claimed_rate, "product_label": product,
            }

        if "banker_obj_result" not in st.session_state or st.session_state["banker_obj_result"]["product"] != loan_type:
            st.caption("Fill in the fields above and click Check Claim.")
            return

        real = st.session_state["banker_obj_result"]["real"]
        ctx = st.session_state["banker_obj_ctx"]
        label = resolve_bank_label(ctx["bank_id"])

        if real is None:
            st.info(
                f"No published {product} rate found for {label} in our tracked data "
                f"— can't verify this claim either way."
            )
            return

        claimed = ctx["claimed_rate"]
        if claimed < real["rate_min"]:
            verdict = (
                f"Likely inaccurate — {claimed}% is below even our best published "
                f"tier ({real['rate_min']}%) for {label}. Ask for their source."
            )
        elif claimed <= real["rate_max"]:
            verdict = (
                f"Plausible — {claimed}% falls within our own best-tier's "
                f"published range ({real['rate_min']}%-{real['rate_max']}%)."
            )
        else:
            verdict = (
                f"Above our best tier's range ({real['rate_min']}%-{real['rate_max']}%) "
                f"— could be a different, weaker-eligibility tier, or inflated."
            )
        tier = real["tier_label"] or "flat published rate, no tiering"
        st.markdown(
            _result_card_html(
                f"{label} — {product} claim check",
                verdict,
                f"Our best published tier: {real['rate_min']}%-{real['rate_max']}% "
                f"p.a. ({tier}).",
                "Caveat: our best tier depends on credit score/CIBIL, loan amount, "
                "and (for education loans) institute category — a customer's real "
                "eligibility may not match this tier. Education loan quotes also "
                "don't reflect any moratorium-period interest capitalization "
                "(see Calculator). Verify against the bank's live page.",
            ),
            unsafe_allow_html=True,
        )


# Component 0 (added 2026-08-24, PROJECT_STATUS.md §20): a compact
# quick-reference strip of the real RBI benchmark rates — Repo,
# Reverse Repo, MSF, Bank Rate, CRR, SLR — for a banker who just wants
# the current numbers at a glance, not a paragraph to read. Parses the
# SAME rbi_current_policy_rates chunk Component 5 already surfaces as
# a text excerpt further down (no second data source); this is purely
# a second, denser PRESENTATION of that one real doc via st.metric.
# PLR (Prime Lending Rate) deliberately excluded, not forgotten: RBI
# hasn't published a PLR since the Base Rate/MCLR/RLLR external-
# benchmark regime replaced it (2016 for Base Rate, 2019 for RLLR on
# retail/MSME loans) — there is no current RBI-set PLR to show, and
# inventing one would violate this project's no-fabrication rule.
_RBI_REFERENCE_RATE_LABELS = [
    ("Repo Rate", "Repo Rate"),
    ("Reverse Repo Rate", "Reverse Repo Rate"),
    (r"Marginal Standing Facility \(MSF\) Rate", "MSF Rate"),
    ("Bank Rate", "Bank Rate"),
    (r"Cash Reserve Ratio \(CRR\)", "CRR"),
    (r"Statutory Liquidity Ratio \(SLR\)", "SLR"),
]


def render_banker_rbi_reference_rates() -> None:
    import re

    chunks = _load_rbi_chunks()
    by_doc = {c["doc_id"]: c for c in chunks}
    chunk = by_doc.get("rbi_current_policy_rates")
    if chunk is None:
        st.info("RBI content isn't indexed yet — run build_index.py.")
        return

    text = chunk["text"]
    date_str = chunk.get("date", "")
    st.caption(
        f"RBI's key policy benchmark rates, for ready reference"
        f"{f' — as of {date_str}' if date_str else ''}. Full detail "
        f"in the RBI section further below."
    )

    values = {}
    for pattern, label in _RBI_REFERENCE_RATE_LABELS:
        m = re.search(pattern + r":\s*([\d.]+%)", text)
        if m:
            values[label] = m.group(1)

    if not values:
        st.info("Rate figures not found in the current RBI doc.")
        return

    # st.metric isn't used anywhere else in the app, so this shrink is
    # effectively scoped to just this section — a smaller font reads
    # better for a quick-glance reference strip than the default
    # dashboard-sized st.metric value.
    st.markdown(
        '<style>'
        '[data-testid="stMetricValue"] { font-size: 1.1rem; }'
        '[data-testid="stMetricLabel"] { font-size: 0.75rem; }'
        '</style>',
        unsafe_allow_html=True,
    )
    row1 = st.columns(3)
    row2 = st.columns(3)
    for col, (_, label) in zip(row1 + row2, _RBI_REFERENCE_RATE_LABELS):
        with col:
            st.metric(label, values.get(label, "—"))

    st.caption(
        "PLR (Prime Lending Rate) isn't shown — RBI stopped publishing "
        "one once banks moved to the Base Rate → MCLR → RLLR "
        "benchmark framework; there's no current RBI-set PLR to quote. "
        "See \"Interest Rate on Advances — Master Direction\" below "
        "for how banks price loans today."
    )


def render_banker_view() -> None:
    # Order (user-requested 2026-08-25): RBI Reference Rates, then RBI
    # Operationally Relevant, then IBA Circulars, then everything else
    # (Peer Positioning / Rate Movement / Objection-Handling) — groups
    # the two RBI-sourced sections and IBA together at the top, ahead of
    # the bank-competitive-analysis sections.
    st.markdown("#### 📌 RBI Reference Rates")
    render_banker_rbi_reference_rates()

    st.divider()
    st.markdown("#### 📜 RBI — Operationally Relevant")
    render_banker_rbi_excerpts()

    # IBA circulars. Reuses render_iba_news() directly — same function,
    # same _cached_iba_circulars() daily cache, same card rendering — no
    # new code. There is no separate standalone "IBA News" tab any more
    # (removed 2026-08-24, PROJECT_STATUS.md §19) — this is now the only
    # place IBA circulars are shown, since they're staff-specific content
    # (wage settlements, Dearness Allowance/Relief) a retail customer
    # wouldn't need.
    st.divider()
    st.markdown("#### 🏛️ IBA Circulars")
    st.caption(
        "Real circulars from the Indian Banks' Association — wage "
        "settlements, Dearness Allowance/Relief, sector coordination. "
        "Staff-relevant content a customer wouldn't need."
    )
    with st.spinner("Fetching latest IBA circulars..."):
        render_iba_news(limit=6)

    st.divider()
    st.markdown("#### 📍 Peer Positioning")
    render_banker_peer_positioning()

    st.divider()
    st.markdown("#### 📊 Rate Movement — Competitive Review")
    render_banker_rate_movement()

    st.divider()
    st.markdown("#### 🔍 Objection-Handling Lookup")
    render_banker_objection_check()


# Component 5: curated RBI excerpts. NOT the full RBI Guidelines pool
# duplicated (22 real docs, most of it general-public Citizens Corner
# FAQ content — bank holidays, glossary, leadership, complaints
# process) — a hand-picked subset with real day-to-day operational
# relevance for bank staff specifically: current benchmark policy
# rates, and the Master Directions a frontline/credit banker actually
# operates under (deposit/advance rate rules, KYC, asset
# classification, credit risk, reserve requirements) plus the current
# grievance/ombudsman rules. Excludes governance/branch-authorisation
# (board/management-level, not frontline) and every Citizens Corner FAQ
# doc — those stay in the full RBI Guidelines mode, not duplicated here.
_BANKER_RBI_DOC_IDS = [
    ("rbi_current_policy_rates", "Current Policy Rates (Repo/CRR/SLR)"),
    ("rbi_md_crr_slr", "CRR & SLR — Master Direction"),
    ("rbi_md_interest_rate_deposits", "Interest Rate on Deposits — Master Direction"),
    ("rbi_md_interest_rate_advances", "Interest Rate on Advances — Master Direction"),
    ("rbi_md_kyc", "KYC — Master Direction"),
    ("rbi_md_asset_classification", "Asset Classification — Master Direction"),
    ("rbi_md_credit_risk_management", "Credit Risk Management — Master Direction"),
    ("rbi_grievance_ombudsman_2026", "Grievance Redress / Ombudsman Scheme (2026)"),
]


@st.cache_data(ttl=3600, show_spinner=False)
def _load_rbi_chunks() -> list[dict]:
    import json
    path = Path(__file__).resolve().parent / "indexes" / "rbi_chunks.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def render_banker_rbi_excerpts() -> None:
    st.caption(
        "A curated subset of RBI content with clear day-to-day relevance "
        "for bank staff — benchmark rates and the Master Directions a "
        "frontline/credit banker operates under. Not the full RBI "
        "Guidelines pool (22 docs, mostly general-public FAQ content) "
        "duplicated here — see RBI Guidelines mode for the rest."
    )
    chunks = _load_rbi_chunks()
    if not chunks:
        st.info("RBI content isn't indexed yet — run build_index.py.")
        return

    by_doc = {c["doc_id"]: c for c in chunks}
    for doc_id, title in _BANKER_RBI_DOC_IDS:
        chunk = by_doc.get(doc_id)
        if chunk is None:
            continue
        text = chunk["text"]
        excerpt = text[:280].rsplit(" ", 1)[0] + "…" if len(text) > 280 else text
        date_str = chunk.get("date", "")
        st.markdown(
            _digest_card_html(title, excerpt, date_str, _DIGEST_COLORS["gold"]),
            unsafe_allow_html=True,
        )
        with st.expander(f"Source note — {title}"):
            st.caption(chunk.get("source", "No source note on file."))


def check_login():
    if st.session_state.get("authenticated"):
        return True
    # Brand palette — the SAME navy/gold pair used throughout Compare
    # Rates, Insights & Tools, and the RBI flash banner (_DIGEST_COLORS),
    # not a separate identity for the login screen. Recolored from an
    # earlier blue/purple mockup per explicit instruction: keep that
    # mockup's layout/structure/copy, swap only the color system.
    c = _DIGEST_COLORS
    st.markdown(f"""
    <style>
        html, body, [class*="css"] {{
            font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto,
                         Helvetica, Arial, sans-serif;
        }}
        .stApp {{ background: linear-gradient(160deg, {c["paper"]}, #dfe8f0); }}
        #MainMenu {{visibility:hidden;}}
        footer     {{visibility:hidden;}}
        header     {{visibility:hidden;}}
        .block-container {{ padding-top: 2.5rem; max-width: 1000px; }}
        /* Streamlit hides the sidebar collapse/expand arrows until the user
           hovers the exact pixel region they sit in (visibility:hidden, not
           display:none) — a real click there only lands because :hover flips
           visibility back on first. That makes the controls easy to lose,
           especially once collapsed. Force both permanently visible instead
           of relying on hover discovery. */
        [data-testid="stSidebarCollapseButton"],
        [data-testid="stSidebarCollapseButton"] *,
        [data-testid="stExpandSidebarButton"],
        [data-testid="stExpandSidebarButton"] * {{
            visibility: visible !important;
            opacity: 1 !important;
        }}

        /* ── Sign-in card ──────────────────────────────────────────────
           st.form() renders as its own block, styled here as a bordered/
           shadowed card next to the hero column (a native st.columns()
           layout, not CSS-recreated tabs/chrome — the sign-in interaction
           itself stays 100% Streamlit-native: text_input/form_submit_
           button, only recolored). */
        [data-testid="stForm"] {{
            background: white;
            padding: 36px 40px 32px 40px;
            border-radius: 16px;
            border: 1px solid #eef1f5;
            box-shadow: 0 8px 32px rgba(27,42,74,0.14);
        }}
        [data-testid="stForm"] label p {{
            font-size: 12px; font-weight: 600; color: {c["ink"]};
            text-transform: none; letter-spacing: 0.01em;
        }}
        .login-form-title {{
            font-size: 22px; font-weight: 700; color: {c["ink"]};
            margin-bottom: 4px; text-align: center;
        }}
        .login-form-desc {{
            font-size: 13px; color: {c["text_mute"]}; margin-bottom: 22px;
            text-align: center;
        }}
        .login-enter-hint {{
            font-size: 11px; color: #8a94a3; margin: -10px 0 16px 2px;
        }}
        .login-form-foot {{
            margin: 16px 0 0 0;
            text-align: center; font-size: 12px; color: {c["text_mute"]};
        }}
        [data-testid="stForm"] input {{
            border-radius: 8px !important;
            border-width: 1.5px !important;
            border-style: solid !important;
            border-color: #dde3ea !important;
            padding: 10px 14px !important;
            font-size: 15px !important;
        }}
        [data-testid="stForm"] input:focus {{
            border-color: {c["ink"]} !important;
            box-shadow: 0 0 0 3px rgba(27,42,74,0.10) !important;
        }}
        [data-testid="stFormSubmitButton"] button {{
            background: linear-gradient(135deg, {c["ink"]}, #2c4270) !important;
            color: white !important;
            border: none !important;
            border-radius: 8px !important;
            padding: 11px 0 !important;
            font-size: 15px !important;
            font-weight: 600 !important;
            margin-top: 6px;
            box-shadow: 0 4px 14px rgba(27,42,74,0.28);
            transition: transform 0.05s ease, box-shadow 0.15s ease;
        }}
        [data-testid="stFormSubmitButton"] button:hover {{
            box-shadow: 0 6px 18px rgba(27,42,74,0.35);
        }}
        [data-testid="stFormSubmitButton"] button:active {{
            transform: translateY(1px);
        }}

        /* ── Feature / stat / trust cards — plain inline-styled markdown
           divs, no custom interaction chrome (no tabs, no hover-JS) — the
           pattern that read as clean/professional last time a mockup was
           adapted for this app (Insights & Tools digest cards), unlike
           the earlier full mockup port that got rejected for recreating
           custom CSS tab/toggle chrome over native widgets. */
        .login-feature-card {{
            background: white; border: 1px solid #eef1f5; border-radius: 14px;
            padding: 22px 18px; text-align: center; min-height: 240px;
            display: flex; flex-direction: column; justify-content: center;
        }}
        /* Cards were rendering at different heights (measured 221px vs
           239px) because each card's own text wraps to a different number
           of lines — height:100% alone doesn't reliably propagate through
           Streamlit's own wrapper divs, so a fixed min-height keeps all 4
           the same regardless of description length. Also center the two
           unequal-height columns above (hero text/illustration vs the
           sign-in card) against each other rather than top-aligning them —
           the sign-in card is naturally taller (padding + 2 inputs +
           button), which top-aligned left a visible gap under the shorter
           hero content and read as "not the same size." */
        [data-testid="stHorizontalBlock"] {{
            align-items: center;
        }}
        .login-feature-icon {{
            width: 52px; height: 52px; border-radius: 50%; margin: 0 auto 14px auto;
            display: flex; align-items: center; justify-content: center; font-size: 24px;
        }}
        .login-feature-title {{
            font-weight: 700; color: {c["ink"]}; font-size: 16px; margin-bottom: 8px;
        }}
        .login-feature-desc {{
            color: {c["text_mute"]}; font-size: 12.5px; line-height: 1.5;
        }}
        .login-stat-card {{
            background: white; border: 1px solid #eef1f5; border-radius: 14px;
            padding: 26px 18px; text-align: center;
        }}
        .login-stat-number {{
            font-weight: 800; font-size: 30px; margin: 10px 0 2px 0;
        }}
        .login-trust-item {{
            display: flex; align-items: flex-start; gap: 10px; padding: 4px 0;
        }}
    </style>
    """, unsafe_allow_html=True)

    # ── Top bar: logomark + "Secure. Private. Reliable." badge ─────────
    st.markdown(f"""
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:34px;">
        <div style="display:flex; align-items:center; gap:12px;">
            <div style="font-size:38px; line-height:1;">🏦</div>
            <div style="line-height:1.15;">
                <div style="font-weight:800; font-size:30px; color:{c["ink"]}; letter-spacing:0.02em;">VITTAM</div>
                <div style="font-weight:700; font-size:13px; color:{c["gold"]}; letter-spacing:0.14em;">BANK ASSISTANT</div>
            </div>
        </div>
        <div style="display:flex; align-items:center; gap:6px; color:{c["text_mute"]}; font-size:12.5px;">
            🛡️ Secure. Private. Reliable.
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Hero (left) + Sign-in card (right) — side by side, per direct
    # feedback that a stacked layout was tried and preferred worse than
    # this; "spread out" was about overall page WIDTH, addressed above by
    # narrowing .block-container to 860px, not by abandoning the
    # two-column arrangement itself. ─────────────────────────────────
    hero_col, form_col = st.columns([1, 1], gap="medium")
    with hero_col:
        st.markdown(f"""
        <div style="padding-top:8px;">
            <h1 style="font-size:1.7rem; font-weight:800; color:{c["ink"]}; line-height:1.3; margin-bottom:10px;">
                Your AI companion for<br><span style="color:{c["gold"]};">smarter banking intelligence</span>
            </h1>
            <p style="color:{c["text_mute"]}; font-size:13.5px; line-height:1.55;">
                Search across Public Sector Banks, leading Private Banks and
                RBI. Compare interest rates, discover insights and use smart
                banking tools — all in one place.
            </p>
        </div>
        """, unsafe_allow_html=True)
        # Simple flat SVG illustration in the brand palette — a lighter-
        # weight stand-in for the mockup's glossy bank-building render,
        # kept as static inline SVG (no interactivity) rather than
        # reproducing a stock-art illustration closely.
        st.markdown(f"""
        <svg viewBox="0 0 420 200" style="width:100%; max-width:380px; margin-top:14px;">
            <rect x="0" y="150" width="420" height="50" fill="{c["paper"]}"/>
            <rect x="20" y="60" width="26" height="90" fill="{c["ink"]}" opacity="0.15"/>
            <rect x="55" y="90" width="22" height="60" fill="{c["ink"]}" opacity="0.12"/>
            <rect x="350" y="75" width="24" height="75" fill="{c["ink"]}" opacity="0.12"/>
            <rect x="380" y="100" width="20" height="50" fill="{c["ink"]}" opacity="0.15"/>
            <g transform="translate(110,40)">
                <polygon points="100,0 200,45 0,45" fill="{c["ink"]}"/>
                <rect x="10" y="45" width="180" height="14" fill="{c["gold"]}"/>
                <rect x="20" y="59" width="18" height="70" fill="{c["ink"]}"/>
                <rect x="50" y="59" width="18" height="70" fill="{c["ink"]}"/>
                <rect x="80" y="59" width="18" height="70" fill="{c["ink"]}"/>
                <rect x="102" y="59" width="18" height="70" fill="{c["ink"]}"/>
                <rect x="132" y="59" width="18" height="70" fill="{c["ink"]}"/>
                <rect x="162" y="59" width="18" height="70" fill="{c["ink"]}"/>
                <rect x="0" y="129" width="200" height="10" fill="{c["gold"]}"/>
            </g>
        </svg>
        """, unsafe_allow_html=True)
    with form_col:
        # Real inline SVG, not an emoji — a glyph like 🔒 rendered as an
        # unreadable solid dot in the user's real browser (font/color-
        # emoji fallback gap), the same class of issue as the RBI flash
        # banner's flag-emoji bug; an SVG path always renders identically.
        st.markdown(f"""
        <div style="width:64px; height:64px; border-radius:50%; margin:4px auto 16px auto;
                    background:linear-gradient(135deg,{c["ink"]},#2c4270);
                    display:flex; align-items:center; justify-content:center;">
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
                <rect x="5" y="11" width="14" height="10" rx="2" fill="white"/>
                <path d="M8 11V7a4 4 0 0 1 8 0v4" stroke="white" stroke-width="2.2" fill="none"/>
            </svg>
        </div>
        """, unsafe_allow_html=True)
        if not ALLOWED_USERS:
            # Fresh clone, secrets.toml not set up yet — this would
            # otherwise look identical to "wrong password" forever, with
            # no clue why. See .streamlit/secrets.toml.example.
            st.warning(
                "No login accounts are configured yet. Copy "
                "`.streamlit/secrets.toml.example` to "
                "`.streamlit/secrets.toml` and fill in a real "
                "`[allowed_users]` table, then restart the app."
            )
            return False
        # A real st.form is required for Enter-to-submit to work at all —
        # plain text_input + a separate button only submits on click, since
        # Enter inside a bare text_input just triggers a rerun with no
        # signal that this was "the" submit action. Wrapping both fields in
        # one form makes Enter (or the submit button) trigger exactly one
        # well-defined submission, same pattern the main chat input uses.
        with st.form("login_form"):
            st.markdown(
                '<div class="login-form-title">Sign in</div>'
                '<div class="login-form-desc">Welcome back! Please sign in to continue.</div>',
                unsafe_allow_html=True,
            )
            # autocomplete="username"/"current-password" tells the browser
            # this is an existing-account login field, not a new-account
            # signup field — without it, Chrome's heuristics sometimes
            # treat an unrecognized password field as a signup field and
            # overlay its own "suggest a strong password" prompt on top.
            # Labeled "Username" (not "Email / Username") — this app
            # authenticates by username only (ALLOWED_USERS), there's no
            # email-based login path, so offering "Email" would be
            # misleading about what actually works here.
            username  = st.text_input("Username", placeholder="Enter username",
                                       autocomplete="username")
            password  = st.text_input("Password", type="password",
                                       placeholder="Enter password",
                                       autocomplete="current-password")
            st.markdown('<div class="login-enter-hint">Press Enter ↵ to submit</div>',
                        unsafe_allow_html=True)
            login_btn = st.form_submit_button("→ Sign in", use_container_width=True)
        st.markdown(
            '<div class="login-form-foot">Restricted access &middot; Authorized users only</div>',
            unsafe_allow_html=True,
        )
        if login_btn:
            if (username in ALLOWED_USERS and
                    ALLOWED_USERS[username] == password):
                st.session_state.authenticated = True
                st.session_state.username      = username
                st.rerun()
            else:
                st.error("Invalid username or password.")

    # ── Feature cards ────────────────────────────────────────────────
    # Compare's description was corrected from the source mockup's "...fees,
    # policies and offers..." — Compare Rates only ever compares published
    # INTEREST RATES (FD/Home/Education/Vehicle Loan tabs), never fees,
    # policies, or other offers; the broader claim would overstate what the
    # feature actually does.
    st.markdown("<div style='margin-top:44px;'></div>", unsafe_allow_html=True)
    features = [
        ("🔍", c["ink"], "Search",
         "Search across PSBs, private banks and RBI documents and get accurate answers."),
        ("⚖️", c["gold"], "Compare",
         "Compare real interest rates for FD, Home, Education and Vehicle Loans across banks."),
        ("💡", c["ink"], "Insights",
         "Track real rate changes, banking news and RBI updates as they happen."),
        ("🛠️", c["gold"], "Tools",
         "Use smart calculators and banking utility tools for your everyday banking needs."),
    ]
    feat_cols = st.columns(4, gap="medium")
    for col, (icon, tint, title, desc) in zip(feat_cols, features):
        with col:
            st.markdown(f"""
            <div class="login-feature-card">
                <div class="login-feature-icon" style="background:{tint}1A;">{icon}</div>
                <div class="login-feature-title">{title}</div>
                <div class="login-feature-desc">{desc}</div>
            </div>
            """, unsafe_allow_html=True)

    # ── Coverage stats — real, exact counts (PSB_BANKS/PRIVATE_BANKS),
    # not rounded/approximate ("12+" corrected to the real, exact 12). ──
    st.markdown(f"""
    <div style="text-align:center; font-weight:700; color:{c["ink"]}; font-size:18px;
                margin:46px 0 18px 0;">Comprehensive Banking Coverage</div>
    """, unsafe_allow_html=True)
    stats = [
        ("🏛️", str(len(PSB_BANKS)), "Public Sector Banks", "All PSBs"),
        ("🏢", str(len(PRIVATE_BANKS)), "Leading Private Banks", "Top private banks"),
        ("📋", "RBI", "Reserve Bank of India", "Policies & Circulars"),
    ]
    stat_cols = st.columns(3, gap="medium")
    for col, (icon, number, label, sub) in zip(stat_cols, stats):
        with col:
            st.markdown(f"""
            <div class="login-stat-card">
                <div style="font-size:26px;">{icon}</div>
                <div class="login-stat-number" style="color:{c["ink"]};">{number}</div>
                <div style="font-weight:600; color:{c["text"]}; font-size:13.5px;">{label}</div>
                <div style="color:{c["text_mute"]}; font-size:11.5px; margin-top:2px;">{sub}</div>
            </div>
            """, unsafe_allow_html=True)

    # ── Trust badges ─────────────────────────────────────────────────
    st.markdown("<div style='margin-top:30px;'></div>", unsafe_allow_html=True)
    badges = [
        ("🛡️", "Secure & Private", "Your data is encrypted and never shared."),
        ("🕐", "Trusted Information", "Information from official sources and notifications."),
        ("🧠", "AI Powered", "Intelligent search and insights you can trust."),
        ("⚡", "All in One Platform", "Search, compare, insights and tools together"),
    ]
    badge_cols = st.columns(4, gap="medium")
    for col, (icon, title, desc) in zip(badge_cols, badges):
        with col:
            st.markdown(f"""
            <div class="login-trust-item">
                <div style="font-size:18px;">{icon}</div>
                <div>
                    <div style="font-weight:700; color:{c["ink"]}; font-size:12.5px;">{title}</div>
                    <div style="color:{c["text_mute"]}; font-size:11px; line-height:1.4;">{desc}</div>
                </div>
            </div>
            """, unsafe_allow_html=True)

    # ── Footer bar ───────────────────────────────────────────────────
    st.markdown(f"""
    <div style="background:{c["ink"]}; border-radius:12px; margin:36px 0 20px 0;
                padding:16px 24px; text-align:center;">
        <div style="color:white; font-size:12.5px;">
            🛡️ Vittam Bank Assistant is an internal intelligence platform for authorized users only.
        </div>
        <div style="color:#9fb0c9; font-size:11px; margin-top:4px;">
            © 2026 Vittam Bank Assistant. All rights reserved.
        </div>
    </div>
    """, unsafe_allow_html=True)
    return False


@st.cache_resource
def load_embed_and_llm():
    """Loaded once regardless of which pool/bank is active."""
    model  = SentenceTransformer(EMBED_MODEL)
    client = Groq(api_key=GROQ_API_KEY)
    return model, client


def _pools_cache_key() -> tuple[float, float, float]:
    """Real on-disk mtimes of the 3 FAISS index files, recomputed fresh
    on every script rerun (cheap — 3 stat() calls, no file reads) and
    passed as load_all_pools()'s own argument rather than read inside
    the cached function itself. st.cache_resource keys its cache on the
    function's ARGUMENTS, not on file contents, so a rebuilt index (a
    new mtime) is what actually invalidates the old cached pools and
    triggers a real reload on the very next rerun — no server restart
    needed. Fixes a real, repeatedly-hit source of confusion in this
    project's own history: the server used to keep serving whatever it
    first loaded, silently, until someone remembered to fully stop and
    restart it after running build_index.py (see [[feedback_multibank_
    rag_gotchas]]) — confirmed working via a live rebuild-while-running
    test, not just reasoned about."""
    paths = (settings.LEGACY_INDEX_PATH, settings.OTHER_BANKS_INDEX_PATH, settings.RBI_INDEX_PATH)
    return tuple(p.stat().st_mtime if p.exists() else 0.0 for p in paths)


@st.cache_resource
def load_all_pools(index_mtimes: tuple[float, float, float]):
    """Always loads all three pools (Indian Bank legacy, other banks, RBI) —
    General Chat needs the first two merged, a single bank needs one of
    them, RBI needs the third. Loading all three up front avoids juggling
    conditional per-mode cache keys, and each index is small enough that
    this costs nothing noticeable. A pool that fails to load (e.g. its
    index hasn't been built yet) is simply absent from the returned dict
    rather than crashing the whole app.

    `index_mtimes` is never read in here — it exists purely so Streamlit's
    cache sees a different argument (and reloads for real) once any index
    file has actually changed on disk; see _pools_cache_key() above."""
    pools, errors = {}, {}
    for name, index_path, chunks_path in [
        ("indian_bank", settings.LEGACY_INDEX_PATH, settings.LEGACY_CHUNKS_PATH),
        ("other_banks", settings.OTHER_BANKS_INDEX_PATH, settings.OTHER_BANKS_CHUNKS_PATH),
        ("rbi", settings.RBI_INDEX_PATH, settings.RBI_CHUNKS_PATH),
    ]:
        try:
            index  = faiss.read_index(str(index_path))
            chunks = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
            pools[name] = (index, chunks)
        except Exception as e:
            errors[name] = str(e)
    return pools, errors


def retrieve(query, index, chunks, model, k=TOP_K, category_filter=None, bank_filter=None,
             recency_sort=False):
    q_vec = model.encode([query], convert_to_numpy=True)
    faiss.normalize_L2(q_vec)

    # Always pull extra candidates beyond k: filters discard some, and
    # recency re-ranking (see chat — RBI banker lookup) needs a wider pool
    # to actually have something to re-rank; taking only the top-k semantic
    # hits first would defeat the point of preferring the newer source.
    # When a bank_filter is set, a small fixed window over the FULL
    # multi-bank index is the wrong denominator: with 17 banks sharing one
    # index, a bank's genuinely relevant doc can rank just outside a small
    # top-N cutoff even though it would clearly be that bank's best match
    # (the bank-scoped analogue of the General Chat k=13->17 fix). Search
    # the whole index in that case — filtering to one bank afterward still
    # caps the result at k, and scanning the full (~200-chunk) index is
    # cheap, so there's no real cost to guaranteeing the bank's own top
    # docs are never dropped by an unrelated window-size accident.
    search_k = index.ntotal if bank_filter else max(k * 4, k + 8)
    scores, indices = index.search(q_vec, min(search_k, index.ntotal))

    candidates = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        chunk = chunks[idx]
        if category_filter and chunk.get("category", "").upper() != category_filter.upper():
            continue
        # .get(..., "INDIAN_BANK") default keeps this working on legacy
        # chunks.json entries that predate the "bank" field.
        if bank_filter and chunk.get("bank", "INDIAN_BANK").upper() != bank_filter.upper():
            continue
        candidates.append({**chunk, "score": float(score)})

    # Candidates already come back from FAISS in relevance order — keep
    # that as the base ranking always, rather than re-sorting by date.
    # An earlier version sorted the WHOLE candidate list by date whenever
    # recency_sort was on, which seemed right for the intended case (an
    # RBI topic reissued with a newer DATE should outrank its superseded
    # version) but actually broke retrieval the moment the pool held
    # *several* dated docs on *different* topics. The doc_id stem (with
    # any trailing _YYYY version year stripped) is a much more precise
    # same-document signature than (category, source_type) — see prior
    # project notes for the full story.
    if recency_sort:
        def topic_stem(doc_id: str) -> str:
            return re.sub(r"_\d{4}$", "", doc_id)

        best_by_topic = {}
        for c in candidates:
            date = c.get("date", "")
            if not date:
                continue
            topic_key = topic_stem(c.get("doc_id", ""))
            current_best = best_by_topic.get(topic_key)
            if current_best is None or date > current_best.get("date", ""):
                best_by_topic[topic_key] = c

        superseded_ids = {
            id(c) for c in candidates
            if c.get("date")
            and best_by_topic.get(topic_stem(c.get("doc_id", ""))) is not c
        }
        candidates = [c for c in candidates if id(c) not in superseded_ids]

    return candidates[:k]


# Below this, a chunk is treated as noise rather than a real match — chosen
# empirically: genuinely on-topic chunks cluster 0.5+, this just screens out
# the clearly-off-topic tail, not a precise cutoff (embedding similarity
# alone can't perfectly separate "relevant" from "coincidentally similar
# wording" — see retrieve_general()'s docstring).
_MIN_RELEVANCE = 0.35

# Spanning up to 13 banks' full chunks in one context blew almost the
# entire per-minute Groq token budget in testing (~7800 of 8000 TPM for a
# single query) and cut the answer off mid-sentence — confirmed live, not
# theoretical. Each chunk gets trimmed to its lead excerpt here (where a
# rate figure almost always appears — these docs open with the number,
# not bury it), keeping full bank breadth while the total request stays a
# fraction of the budget.
_MAX_CHUNK_CHARS_GENERAL = 500


def retrieve_general(query, pools, model, k=17):
    """Cross-bank retrieval for General Chat. RBI's pool is deliberately
    not included here; it's reached only via the separate RBI Guidelines
    mode.

    A plain top-k-by-score merge let one bank's especially rate-heavy
    prose dominate every slot — confirmed for real: "which bank has the
    highest savings bank rate" scored Indian Bank's own savings-rate
    paragraph so consistently high (it happens to state exact tiered
    percentages very explicitly) that it could fill most or all of a
    small top-k, crowding out every other bank's own savings content even
    when those banks had perfectly relevant chunks of their own, just
    scored a little lower. This does a diversity-first pass instead: take
    each bank's SINGLE best-matching chunk first (one round across every
    bank with a candidate), only dipping into a bank's second-best chunk
    if slots remain after every bank with relevant content has had a
    turn — so a "compare across banks" question actually sees a spread of
    banks, not depth from just one or two. `k` should always match the
    real total bank count (16 other banks + Indian Bank = 17 as of
    2026-08-24 — see settings.OTHER_BANKS) so a query relevant to every
    bank can surface all of them in one round; fewer appear only when
    fewer banks actually published anything relevant. **This was found
    stale on 2026-08-24** — k had been left at 13 (correct when there
    were 13 banks) through 4 more bank onboardings, so any sufficiently
    broad query was silently excluding at least one bank's real, correct
    content no matter how well that bank's own doc was written; caught
    via `automation/verify_retrieval.py`'s `general_chat_capacity` check
    while investigating 4 real Kisan Credit Card retrieval bugs (see
    PROJECT_STATUS.md §22-23). Raising it to 17 was verified safe against
    Groq's real per-minute token budget first, not assumed: a live
    17-bank-relevant query (k=17) cost 3,501 total tokens vs. 2,981 at
    k=13 (+520, ~17%), `finish_reason="stop"` both times (no truncation),
    comfortably under the 8,000 TPM ceiling with >2x headroom even at
    this worst case. Re-run this check before raising `k` again after
    any future bank onboarding — don't just bump the number.

    A low-relevance chunk (below `_MIN_RELEVANCE`) is dropped outright
    rather than force-included just to fill a bank's slot — a bank with
    truly no matching content should not appear at all, not appear with
    noise the LLM might mistake for a real answer.
    """
    # Candidate window fed INTO round-robin, deliberately much larger than
    # k (the final round-robin OUTPUT cap, still 17 below) — same fix as
    # §28's bank-scoped search_k, applied here: a small candidate window
    # (previously just max(k,16)=17 per pool) meant a bank's own best doc
    # could lose ENTIRELY before round-robin ever got a turn, if enough
    # other banks' docs outranked it globally for a given query — not a
    # round-robin-fairness problem, a recall problem one step upstream of
    # it. Confirmed via a live regression chain while fixing vehicle_loan
    # general_chat gaps (2026-08-26): every content fix that raised one
    # bank's score pushed a DIFFERENT bank below this same top-17-per-pool
    # cutoff, repeatedly, including one bank (boi) being pushed out twice.
    # 500 comfortably exceeds either pool's real size (~220 + ~27 chunks),
    # so this is effectively "scan the whole pool" — cheap at this corpus
    # size, and downstream relevance filtering + round-robin's own k=17
    # cap still bound what's actually shown; this only widens what's
    # AVAILABLE to select from.
    _CANDIDATE_WINDOW = 500
    candidates = []
    for name in ("indian_bank", "other_banks"):
        if name not in pools:
            continue
        index, chunks = pools[name]
        candidates.extend(retrieve(query, index, chunks, model, k=_CANDIDATE_WINDOW))
    candidates = [c for c in candidates if c["score"] >= _MIN_RELEVANCE]
    candidates.sort(key=lambda c: c["score"], reverse=True)

    by_bank = {}
    for c in candidates:
        by_bank.setdefault(c.get("bank", "INDIAN_BANK"), []).append(c)

    selected = []
    round_idx = 0
    while len(selected) < k and any(len(v) > round_idx for v in by_bank.values()):
        round_candidates = sorted(
            (v[round_idx] for v in by_bank.values() if len(v) > round_idx),
            key=lambda c: c["score"], reverse=True,
        )
        for c in round_candidates:
            if len(selected) >= k:
                break
            selected.append(c)
        round_idx += 1

    for c in selected:
        text = c.get("text", "")
        if len(text) > _MAX_CHUNK_CHARS_GENERAL:
            c["text"] = text[:_MAX_CHUNK_CHARS_GENERAL].rsplit(" ", 1)[0] + "…"

    return selected


def generate_answer(query, retrieved_chunks, client, fallback_hint=""):
    if not retrieved_chunks:
        return (
            "I could not find relevant information. "
            "Please check the relevant bank's official website or a branch."
            + fallback_hint
        )
    # .get(...) with a default keeps this working on the existing legacy
    # chunks.json, which has no "bank" or "source_type" keys yet.
    context = "\n\n---\n\n".join(
        f"[Source {i} | {c.get('bank', 'INDIAN_BANK')} | {c['category']} | "
        f"{c.get('source_type', 'PRODUCT_PAGE')} | {c['doc_id']}]\n{c['text']}"
        for i, c in enumerate(retrieved_chunks, 1)
    )
    prompt = (
        f"Context:\n{context}\n\n"
        f"Customer Question: {query}\n\n"
        f"Answer using only the context above."
    )
    try:
        response = client.chat.completions.with_raw_response.create(
            model    = GROQ_MODEL,
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt}
            ],
            max_tokens  = settings.MAX_TOKENS,
            temperature = 0.1,
        )
        # Read rate limit info from headers
        headers = response.headers
        st.session_state.rpm_limit     = headers.get("x-ratelimit-limit-requests",     "30")
        st.session_state.rpm_remaining = headers.get("x-ratelimit-remaining-requests", "?")
        st.session_state.tpm_limit     = headers.get("x-ratelimit-limit-tokens",       "6000")
        st.session_state.tpm_remaining = headers.get("x-ratelimit-remaining-tokens",   "?")
        # Calculate used tokens dynamically: Limit - Remaining
        try:
            lim = int(st.session_state.tpm_limit)
            rem = int(st.session_state.tpm_remaining)
            st.session_state.tpm_used = str(lim - rem)
        except Exception:
            st.session_state.tpm_used = "?"

        return response.parse().choices[0].message.content

    except Exception as e:
        # User-facing message stays deliberately simple/safe regardless of
        # cause; the real exception type + full traceback still goes to
        # the server console so whoever's debugging isn't stuck with only
        # "something went wrong" for a genuine bug (rate-limit text match
        # below is still just for picking WHICH safe message to show, not
        # for diagnosis anymore).
        print(f"[generate_answer] {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        err = str(e)
        if "429" in err or "rate" in err.lower():
            return (
                "Service is briefly busy. "
                "Please wait a moment and try again."
            )
        return "Something went wrong. Please try again."


def render_disclaimer():
    st.markdown(
        '<div style="margin-top:28px; padding-top:14px; border-top:1px solid #e5e9ef; '
        'font-size:13px; color:#999; text-align:center; line-height:1.6;">'
        'Information shown is compiled from publicly available bank and RBI sources for '
        'reference only — it is not financial advice. Rates, fees, and eligibility criteria '
        'change; always confirm directly with the bank before making a decision.</div>',
        unsafe_allow_html=True,
    )


st.set_page_config(
    page_title="Vittam Bank Assistant",
    page_icon="🏦",
    layout="wide"
)

if not check_login():
    st.stop()

# Deliberately deferred until AFTER the login gate above, not imported at
# module top-level: importing sentence_transformers/faiss pulls in torch
# and takes ~15-20s on a cold process (measured directly: torch alone
# ~2.5s, sentence_transformers ~7s more, loading the actual embedding
# model ~8s more). At module top-level, Python must finish these imports
# before ANY code in this file runs — including st.set_page_config() and
# the login screen itself — so an unauthenticated visitor was paying this
# full cost just to see the login form. Moved here, that cost is paid
# once, only after a successful login, right where load_embed_and_llm()/
# load_all_pools() below need it anyway (both @st.cache_resource, so this
# still only actually runs once per server process either way).
#
# Import order still matters and must be preserved: sentence_transformers
# before faiss. On this Windows/Python 3.14 environment, faiss's bundled
# OpenMP runtime loading first, then torch/transformers/datasets (pulled
# in transitively via sentence_transformers) loading after, segfaults the
# process; the reverse order doesn't. Confirmed 2026-08-25 after installing
# ragas added `datasets` as a new transitive dependency, which
# sentence_transformers now also imports internally — see
# feedback_multibank_rag_gotchas memory.
from sentence_transformers import SentenceTransformer
import faiss

st.markdown("""
<style>
    :root {
        --accent:        #003366;
        --accent-mid:    #005599;
        --accent-dark:   #001f3d;
        --accent-tint:   #e8f0fe;
        --accent-tint-2: #c5d8f7;
    }

    html, body, [class*="css"] {
        font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto,
                     Helvetica, Arial, sans-serif;
    }

    .stApp { background-color: #f4f6f9; }

    .bank-header {
        background: linear-gradient(135deg, var(--accent), var(--accent-mid));
        color: white; padding: 22px 32px; border-radius: 14px;
        margin-bottom: 22px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.12);
    }
    .bank-header h1 {
        margin:0; font-size:25px; font-weight:700; letter-spacing:-0.2px;
    }
    .bank-header p {
        margin:5px 0 0 0; font-size:15px; opacity:0.88; font-weight:400;
    }

    .user-msg {
        background: linear-gradient(135deg, var(--accent), var(--accent-mid));
        color:white; padding:12px 18px;
        border-radius:16px 16px 4px 16px; margin:10px 0;
        max-width:80%; margin-left:auto; font-size:14px;
        box-shadow: 0 2px 6px rgba(0,0,0,0.1);
    }
    .user-msg b { opacity:0.75; font-weight:600; font-size:12px; }

    .bot-msg {
        background:white; color:#232733; padding:16px 20px;
        border-radius:16px 16px 16px 4px; margin:10px 0;
        max-width:85%; border-left:3px solid var(--accent);
        font-size:14px; box-shadow:0 2px 10px rgba(0,0,0,0.07);
        line-height:1.65;
    }
    .bot-msg b { color: var(--accent); font-weight:600; font-size:13px; }

    .source-badge {
        display:inline-block; background:var(--accent-tint); color:var(--accent);
        font-size:11px; padding:3px 10px; border-radius:12px;
        margin:2px; border:1px solid var(--accent-tint-2); font-weight:500;
    }

    .sidebar-card {
        background:white; border-radius:12px; padding:16px;
        margin-bottom:14px; box-shadow:0 2px 8px rgba(0,0,0,0.05);
        border:1px solid #eef1f5;
    }
    .sidebar-card h4 {
        color:var(--accent); margin:0 0 10px 0; font-size:12px;
        font-weight:700; text-transform:uppercase; letter-spacing:0.6px;
    }

    .welcome-box {
        background:white; border-radius:12px; padding:10px 20px;
        text-align:center; box-shadow:0 2px 8px rgba(0,0,0,0.05);
        margin:10px 0; border:1px solid #eef1f5;
    }
    .welcome-box p {
        color:#555; font-size:13px; margin:0; line-height:1.5;
    }
    .welcome-box b { color:var(--accent); }

    .stTextInput input {
        border-radius:25px !important;
        border:2px solid #dfe4ea !important;
        padding:10px 20px !important;
        font-size:14px !important;
        transition: border-color 0.15s ease;
    }
    .stTextInput input:focus {
        border-color: var(--accent) !important;
        box-shadow: 0 0 0 3px var(--accent-tint) !important;
    }

    .stButton button, .stFormSubmitButton button {
        background: var(--accent) !important; color:white !important;
        border-radius:25px !important; border:none !important;
        padding:8px 24px !important; font-weight:600 !important;
        transition: background 0.15s ease, transform 0.05s ease;
    }
    .stButton button:hover, .stFormSubmitButton button:hover {
        background: var(--accent-dark) !important;
    }
    .stButton button:active, .stFormSubmitButton button:active {
        transform: scale(0.98);
    }

    #MainMenu {visibility:hidden;}
    footer     {visibility:hidden;}
    header     {visibility:hidden;}
    /* Force the sidebar collapse/expand arrows to always render instead of
       only on hover — see matching comment in check_login()'s CSS block. */
    [data-testid="stSidebarCollapseButton"],
    [data-testid="stSidebarCollapseButton"] *,
    [data-testid="stExpandSidebarButton"],
    [data-testid="stExpandSidebarButton"] * {
        visibility: visible !important;
        opacity: 1 !important;
    }
</style>
""", unsafe_allow_html=True)


# ── Sidebar navigation ────────────────────────────────────────────────
# "What to explore" replaces the old 3-way Indian Bank / Other Banks / RBI
# scope selector — General Chat (no bank picked, cross-bank retrieval) is
# now the default, Indian Bank is just another PSB entry, and Compare is
# a separate toggle rather than a 4th scope.
explore_mode = st.sidebar.selectbox(
    "🏦 What would you like to explore?",
    ["General Chat — ask about any bank", "Public Sector Bank", "Private Bank", "RBI Guidelines"],
    key="explore_mode",
)

selected_bank = None
if explore_mode == "Public Sector Bank":
    selected_bank = st.sidebar.selectbox(
        "Select PSB", PSB_BANKS, format_func=lambda b: BANK_UI[b]["label"], key="psb_select",
    )
elif explore_mode == "Private Bank":
    selected_bank = st.sidebar.selectbox(
        "Select Private Bank", PRIVATE_BANKS, format_func=lambda b: BANK_UI[b]["label"], key="private_select",
    )

st.sidebar.markdown("---")


# Compare Rates and Insights & Tools are both full-panel views (each
# replaces the whole main area via st.stop() below) — mutually exclusive
# by design, so turning one on turns the other off rather than trying to
# render both at once.
def _turn_off_insights():
    if st.session_state.get("compare_toggle"):
        st.session_state["insights_toggle"] = False


def _turn_off_compare():
    if st.session_state.get("insights_toggle"):
        st.session_state["compare_toggle"] = False


compare_on = st.sidebar.toggle(
    "📊 Compare Rates", value=False, key="compare_toggle", on_change=_turn_off_insights,
)
insights_on = st.sidebar.toggle(
    "📈 Insights & Tools", value=False, key="insights_toggle", on_change=_turn_off_compare,
)

pools, pool_errors = load_all_pools(_pools_cache_key())
embed_model, groq_client = load_embed_and_llm()
resources_ok = True

if explore_mode == "RBI Guidelines":
    if "rbi" not in pools:
        resources_ok = False
        st.info("The RBI pool hasn't been built yet. Run `python build_index.py`.")
    else:
        index, chunks = pools["rbi"]
        SYSTEM_PROMPT = RBI_SYSTEM_PROMPT
elif selected_bank:
    pool_name = "indian_bank" if selected_bank == "INDIAN_BANK" else "other_banks"
    if pool_name not in pools:
        resources_ok = False
        st.info(f"That pool hasn't been built yet. Run `python build_index.py`.")
    else:
        index, chunks = pools[pool_name]
        SYSTEM_PROMPT = GENERAL_SYSTEM_PROMPT
else:
    # General Chat — needs both bank pools available to merge; RBI-pool
    # absence doesn't matter here since it's never queried in this mode.
    if "indian_bank" not in pools and "other_banks" not in pools:
        resources_ok = False
        st.info("No bank content pools are built yet. Run `python build_index.py`.")
    SYSTEM_PROMPT = GENERAL_SYSTEM_PROMPT

n_chunks, n_docs = 0, 0
if resources_ok:
    if explore_mode == "RBI Guidelines":
        n_chunks = len(chunks)
        n_docs = len(set(c["doc_id"].rsplit("_chunk", 1)[0] for c in chunks))
    elif selected_bank:
        bank_chunks = (
            chunks if selected_bank == "INDIAN_BANK"
            else [c for c in chunks if c.get("bank") == selected_bank]
        )
        n_chunks = len(bank_chunks)
        n_docs = len(set(c["doc_id"].rsplit("_chunk", 1)[0] for c in bank_chunks))
    else:
        for name in ("indian_bank", "other_banks"):
            if name in pools:
                _, pool_chunks = pools[name]
                n_chunks += len(pool_chunks)
        n_docs = n_chunks  # cross-pool doc de-dup isn't worth the complexity here

# A mode/bank switch is a different retrieval scope entirely, so the
# thread clears — same rule as before, just keyed on the new state.
_active_combo = (explore_mode, selected_bank)
if st.session_state.get("_active_combo") != _active_combo:
    st.session_state.messages = []
    st.session_state.total_queries = 0
    st.session_state["_active_combo"] = _active_combo

if "messages"      not in st.session_state:
    st.session_state.messages      = []
if "total_queries" not in st.session_state:
    st.session_state.total_queries = 0

if "rpm_limit"     not in st.session_state:
    st.session_state.rpm_limit     = "30"
if "rpm_remaining" not in st.session_state:
    st.session_state.rpm_remaining = "?"
if "tpm_limit"     not in st.session_state:
    st.session_state.tpm_limit     = "6000"
if "tpm_remaining" not in st.session_state:
    st.session_state.tpm_remaining = "?"
if "tpm_used"      not in st.session_state:
    st.session_state.tpm_used      = "?"

with st.sidebar:
    st.markdown(f"""
    <div style="text-align:center; margin-bottom:20px;">
        <div style="font-size:48px;">🏦</div>
        <div style="font-weight:700; color:var(--accent); font-size:16px;">
            Vittam Bank Assistant
        </div>
        <div style="font-size:11px; color:#888; margin-top:4px;">
            RAG + Groq
        </div>
        <div style="font-size:12px; color:#4CAF50; margin-top:6px;">
            👤 {st.session_state.get("username", "").title()}
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown(f"""
    <div class="sidebar-card">
        <h4>📊 Knowledge Base</h4>
        <div style="font-size:13px; color:#444; line-height:2;">
            📄 Documents : <b>{n_docs}</b><br>
            🧩 Chunks    : <b>{n_chunks}</b><br>
            🤖 Embedder  : <b>MiniLM-L6-v2</b><br>
            🔍 Top-K     : <b>{TOP_K}</b>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Cheap (filesystem timestamps only, no file reads/embeddings) —
    # runs on every rerun, not cached, so this can never itself go
    # stale. Reuses the exact same check verify_index_freshness.py runs
    # standalone, not a re-implementation — catches a real gap this
    # project had no automated check for: a source .txt edited but
    # build_index.py never re-run, so the index quietly keeps serving
    # the old content even though the "real" file already changed.
    try:
        from verify_index_freshness import check_index_freshness
        _stale_pools = [r for r in check_index_freshness() if r["status"] == "FAIL"]
    except Exception:
        _stale_pools = []
    if _stale_pools:
        _stale_names = ", ".join(r["pool"] for r in _stale_pools)
        st.warning(
            f"⚠️ Index may be stale: **{_stale_names}**. A source document "
            f"was edited more recently than the matching index was built — "
            f"run `python build_index.py` (or the legacy-pool rebuild "
            f"script) to pick up the change.",
            icon="⚠️",
        )

    show_sources = st.toggle("Show retrieved sources", value=True)

    st.markdown(f"""
    <div class="sidebar-card">
        <h4>💬 Session Stats</h4>
        <div style="font-size:13px; color:#444;">
            Queries: <b>{st.session_state.total_queries}</b>
        </div>
    </div>
    """, unsafe_allow_html=True)

    rpm_rem = st.session_state.rpm_remaining
    tpm_rem = st.session_state.tpm_remaining
    tpm_lim = st.session_state.tpm_limit
    tpm_use = st.session_state.tpm_used
    rpm_lim = st.session_state.rpm_limit

    try:
        tpm_pct = int(100 * (1 - int(tpm_rem) / int(tpm_lim)))
        color   = ("#4CAF50" if tpm_pct < 60 else
                   "#FF9800" if tpm_pct < 85 else
                   "#f44336")
    except Exception:
        tpm_pct = 0
        color   = "#4CAF50"

    st.markdown(f"""
    <div class="sidebar-card">
        <h4>📡 Groq API Limits</h4>
        <div style="font-size:13px; color:#444; line-height:2;">
            🔄 RPM Left : <b>{rpm_rem} / {rpm_lim}</b><br>
            ⚡ TPM Left : <b style="color:{color};">{tpm_rem} / {tpm_lim}</b><br>
            📊 TPM Used : <b>{tpm_use} tokens</b>
        </div>
        <div style="margin-top:8px; background:#eee; border-radius:6px; height:6px;">
            <div style="width:{min(tpm_pct, 100)}%; background:{color}; height:6px; border-radius:6px;">
            </div>
        </div>
        <div style="font-size:10px; color:#888; margin-top:4px;">
            {"🟢 Good" if tpm_pct < 60 else
             "🟡 Getting busy" if tpm_pct < 85 else
             "🔴 Near limit — wait a moment"}
        </div>
        <div style="font-size:10px; color:#999; margin-top:6px; line-height:1.4;">
            💡 Tip: TPM is a rolling per-minute window — spacing questions
            roughly 20–25 seconds apart lets it recover between queries
            instead of running the app into the limit.
        </div>
    </div>
    """, unsafe_allow_html=True)

    if st.button("🗑️ Clear Chat", use_container_width=True):
        st.session_state.messages      = []
        st.session_state.total_queries = 0
        st.rerun()

    if st.button("🚪 Logout", use_container_width=True):
        st.session_state.authenticated = False
        st.session_state.messages      = []
        st.session_state.total_queries = 0
        st.rerun()

    if selected_bank:
        bank_info = BANK_UI[selected_bank]
        footer = {
            "data": bank_info["data"], "updated": bank_info["updated"],
            "note": "Verify rates with the bank.", "contact": bank_info["contact"],
        }
    elif explore_mode == "RBI Guidelines":
        footer = {
            "data": "rbi.org.in", "updated": "Jul 2026",
            "note": "Reference for bank staff — verify against rbi.org.in for legal use.",
            "contact": "📋 Complaints: cms.rbi.org.in",
        }
    else:
        footer = {
            "data": "multiple banks", "updated": "—",
            "note": "Answers cite each bank by name — verify rates directly with the bank.",
            "contact": "",
        }
    st.markdown(f"""
    <div style="margin-top:20px; font-size:11px; color:#aaa; text-align:center;">
        Data: {footer["data"]}<br>
        Updated: {footer["updated"]}<br><br>
        {footer["note"]}<br>
        {footer["contact"]}
    </div>
    """, unsafe_allow_html=True)


# ── Header + theme for the active mode ──────────────────────────────────
# Compare Rates and Insights & Tools are cross-bank tools, not scoped to
# whichever bank/RBI mode happens to still be selected in the sidebar —
# showing that bank's own title/subtitle/accent color above one of these
# panels wrongly implied the panel's content was about that specific
# bank. Both panels render their own subheader already, so once either
# toggle is on this always falls through to the same neutral theme/title
# General Chat uses, regardless of `selected_bank`/`explore_mode`.
in_tool_panel = compare_on or insights_on
if selected_bank and not in_tool_panel:
    bank_info = BANK_UI[selected_bank]
    display_title    = bank_info["label"]
    display_subtitle = bank_info["subtitle"]
    display_examples = bank_info["examples"]
    theme = bank_info["theme"]
elif explore_mode == "RBI Guidelines" and not in_tool_panel:
    display_title    = "RBI Guidelines Lookup"
    display_subtitle = "Fast lookup for current RBI rules, rates, grievance redressal and other relevant info"
    display_examples = [
        "What is the current RBI repo rate?",
        "What is the deposit insurance limit under DICGC?",
        "What is the current RBI Ombudsman scheme for grievance redressal?",
        "What changed between RB-IOS 2021 and RB-IOS 2026?",
    ]
    theme = RBI_THEME
else:
    display_title    = "Vittam Bank Assistant"
    display_subtitle = ("Ask about deposits, loans, digital products, and interest rates — across any bank. "
                         "For a full side-by-side comparison across all banks, use the 📊 Compare Rates toggle instead. "
                         "To search bank-wise for one specific bank's details, switch to Public Sector Bank or "
                         "Private Bank above. For RBI rules and guidelines, switch to RBI Guidelines above.")
    display_examples = GENERAL_EXAMPLES
    theme = GENERAL_THEME

st.markdown(f"""
<style>
    :root {{
        --accent:        {theme["accent"]};
        --accent-mid:    {theme["mid"]};
        --accent-dark:   {theme["dark"]};
        --accent-tint:   {theme["tint"]};
        --accent-tint-2: {theme["tint2"]};
    }}
</style>
""", unsafe_allow_html=True)

st.markdown(f"""
<div class="bank-header">
    <div style="font-size:40px; display:inline; margin-right:14px;">🏦</div>
    <div style="display:inline-block; vertical-align:middle;">
        <h1>{display_title}</h1>
        <p>{display_subtitle}</p>
    </div>
</div>
""", unsafe_allow_html=True)


# ── RBI "What's New" — real items straight from rbi.org.in's own home
# page ticker, not this project's own static reference docs. Originally
# this banner surfaced the most-recently-DATED chunk from the static RBI
# pool (Master Directions, FAQs, etc.) — real content, but those are
# evergreen reference material, not a feed of RBI's actual recent
# activity, so "latest" there didn't mean "news." Corrected per direct
# feedback: pull RBI's own live "What's New" ticker instead (see
# automation/rbi_news.py) — genuinely current, RBI-authored items, same
# real-external-source discipline as news.py's ET/Business Standard
# feed. RBI's ticker carries no clean per-item date in its own HTML
# (page order is the recency signal — confirmed newest-first via
# descending notification/press-release IDs); never fabricated one.
@st.cache_data(ttl=900, show_spinner=False)
def _cached_rbi_whats_new(limit: int) -> list[dict]:
    from rbi_news import fetch_rbi_whats_new
    return fetch_rbi_whats_new(limit=limit)


def _rbi_flash_html(item: dict) -> str:
    c = _DIGEST_COLORS
    date_html = f'{item["date_hint"]} · ' if item["date_hint"] else ""
    return (
        f'<div style="background:linear-gradient(135deg,{c["paper_raised"]} 0%,#FFF8E8 100%);'
        f'border:1px solid {c["gold"]};border-radius:10px;padding:14px 18px;'
        f'margin-bottom:10px;box-shadow:0 1px 6px rgba(200,155,60,0.25);">'
        f'<div style="display:inline-block;background:{c["gold"]};color:#FFF;'
        f'font-size:10.5px;font-weight:700;letter-spacing:0.04em;border-radius:4px;'
        f'padding:2px 8px;margin-bottom:8px;">🆕 RBI — WHAT\'S NEW</div>'
        f'<a href="{item["link"]}" target="_blank" style="text-decoration:none;">'
        f'<div style="font-weight:700;color:{c["ink"]};font-size:15px;margin-top:2px;">'
        f'{item["headline"]}</div></a>'
        f'<div style="color:{c["text_mute"]};font-size:11px;margin-top:8px;">'
        f'{date_html}straight from rbi.org.in\'s own "What\'s New" listing</div>'
        f'</div>'
    )


def _rbi_more_html(item: dict) -> str:
    c = _DIGEST_COLORS
    date_html = f' · {item["date_hint"]}' if item["date_hint"] else ""
    return (
        f'<div style="background:{c["paper_raised"]};border:1px solid {c["line"]};'
        f'border-left:3px solid {c["gold"]};border-radius:8px;padding:10px 14px;'
        f'margin-bottom:8px;">'
        f'<a href="{item["link"]}" target="_blank" style="text-decoration:none;">'
        f'<div style="font-weight:600;color:{c["ink"]};font-size:13px;">{item["headline"]}</div></a>'
        f'<div style="color:{c["text_mute"]};font-size:10.5px;margin-top:4px;">RBI{date_html}</div>'
        f'</div>'
    )


if explore_mode == "RBI Guidelines" and not in_tool_panel:
    _rbi_items = _cached_rbi_whats_new(6)
    if _rbi_items:
        st.markdown(_rbi_flash_html(_rbi_items[0]), unsafe_allow_html=True)
        if len(_rbi_items) > 1:
            with st.expander(f"More from RBI's What's New ({len(_rbi_items) - 1})"):
                for _item in _rbi_items[1:]:
                    st.markdown(_rbi_more_html(_item), unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════════
# Compare view — only rendered when the sidebar toggle is on. Replaces
# the chat entirely rather than sitting open in an always-visible
# expander, per explicit instruction ("only be seen when I use that
# toggle, not by default"). Reads directly from the structured rate
# database (automation/), no RAG retrieval, no LLM call.
# ══════════════════════════════════════════════════════════════════════
if compare_on:
    from compare_rates import compare_for_tenure, special_tenure_bands
    from compare_loan_rates import compare_home_loan, compare_loan
    from gap_notes import freshness_label
    # Eligibility tab unwired from the UI 2026-08-19 (still fully intact
    # in automation/eligibility.py — check_eligibility() itself untouched,
    # only this import + the cmp_tab3 rendering block below are commented
    # out). Restore by uncommenting this import and the block near the
    # bottom of this section.
    # from eligibility import check_eligibility

    def _manual_source_asof(bank_id: str, table: str, type_col: str, type_value: str) -> str | None:
        """Indian Bank / BOI rows here come from manually-sourced documents,
        not a live fetch (see feedback_multibank_rag_gotchas memory) — this
        looks up the date to show as 'data as of' for just those two banks,
        preferring the bank's own effective_date when known, else the date
        we loaded the row."""
        if bank_id not in ("indian_bank", "boi"):
            return None
        from db import get_connection
        con = get_connection()
        row = con.execute(
            f"SELECT effective_date, fetched_at, data_source FROM {table} "
            f"WHERE bank_id = ? AND {type_col} = ? LIMIT 1",
            [bank_id, type_value],
        ).fetchone()
        con.close()
        if row is None or row[2] != "manual_load_frozen":
            return None
        effective_date, fetched_at, _ = row
        return effective_date or str(fetched_at)[:10]

    def _bank_data_sources(table: str, type_col: str, type_value: str) -> dict:
        """bank_id -> data_source for every row of this product, one query —
        feeds freshness_label()'s per-row 'Data' column below, so a legacy-
        loaded bank (Indian Bank/BOI/Union Bank/SBI's manual_load_frozen
        rows) shows 'Frozen — manual snapshot' next to its rate rather than
        silently looking identical to a live-fetched bank's row."""
        from db import get_connection
        con = get_connection()
        rows = con.execute(
            f"SELECT bank_id, data_source FROM {table} WHERE {type_col} = ?",
            [type_value],
        ).fetchall()
        con.close()
        return {bank_id: data_source for bank_id, data_source in rows}

    def _freshness_column(results: list[dict], data_sources: dict, fetch_suffix: str = "") -> list[str]:
        return [
            freshness_label(
                r["bank_id"], "rate",
                data_source=data_sources.get(r["bank_id"]),
                source_id=f'{r["bank_id"]}{fetch_suffix}',
            )["label"]
            for r in results
        ]

    def _missing_bank_hint(product_label: str) -> None:
        st.caption(
            f"ℹ️ A bank missing from this {product_label} table either doesn't "
            "offer the product, or we haven't found/tracked a published rate for "
            "it yet — check that bank's profile page or contact them directly."
        )

    def _asof_footnotes(results: list[dict], table: str, type_col: str, type_value: str) -> None:
        for bank_id in ("indian_bank", "boi"):
            if not any(r["bank_id"] == bank_id for r in results):
                continue
            asof = _manual_source_asof(bank_id, table, type_col, type_value)
            if asof:
                st.caption(
                    f"ℹ️ {resolve_bank_label(bank_id)} figures are manually "
                    f"sourced (not live-fetched) — updated up to {asof}."
                )

    st.subheader("📊 Compare")

    cmp_tab1, cmp_tab2, cmp_tab3, cmp_tab4 = st.tabs(
        ["FD Rates", "Home Loan", "Education Loan", "Vehicle Loan"]
    )

    with cmp_tab1:
        st.caption(
            "Ranks every bank's current FD rate for a chosen tenure — "
            "rule-based, reads directly from the structured rate database."
        )
        col_a, col_b = st.columns(2)
        with col_a:
            cmp_tenure_name = st.selectbox(
                "Tenure", list(_TENURE_OPTIONS.keys()), index=3, key="cmp_tenure"
            )
        with col_b:
            cmp_customer_type = st.radio(
                "Customer type", ["general", "senior"], horizontal=True, key="cmp_customer"
            )
        cmp_results = compare_for_tenure(_TENURE_OPTIONS[cmp_tenure_name], cmp_customer_type)
        if not cmp_results:
            st.info("No bank has a published rate covering this tenure.")
        else:
            # Every rate shown is the plain retail product by default (a
            # "Retail" tag makes that explicit rather than implicit) — a
            # bank whose special/non-callable product actually beats its
            # own retail rate for this tenure gets a visible secondary
            # line instead of that special rate silently winning the rank
            # or being silently dropped. See compare_for_tenure()'s
            # docstring / PROJECT_STATUS.md §4b for the SBI case that
            # prompted this (2026-08-18).
            fd_data_sources = _bank_data_sources("fd_rates", "product_type", "fixed_deposit")
            fd_freshness = _freshness_column(cmp_results, fd_data_sources)
            st.table([
                {"Rank": i, "Bank": resolve_bank_label(r["bank_id"]),
                 "Rate": f'{r["rate"]}% ({r["variant_label"]})',
                 "Matched tenure label": r["tenure_label"],
                 "Data": fresh}
                for i, (r, fresh) in enumerate(zip(cmp_results, fd_freshness), 1)
            ])
            for r in cmp_results:
                if r["secondary"]:
                    st.caption(
                        f'ℹ️ {resolve_bank_label(r["bank_id"])} — also available: '
                        f'{r["secondary"]["label"]} @ {r["secondary"]["rate"]}% '
                        f'({r["secondary"]["tenure_label"]})'
                    )
            _asof_footnotes(cmp_results, "fd_rates", "product_type", "fixed_deposit")
            _missing_bank_hint("FD Rates")

        # Real, published tenure bands that don't match ANY of the 8
        # buckets above — structurally invisible to the ranking table no
        # matter which tenure is picked (confirmed 2026-08-26: a real
        # HDFC senior-rate move, 7.00%->7.10%, on its "3 Years 1 day to
        # < 4 Years 7 Months" band, was completely absent from this view
        # for exactly this reason). Kept as a separate, collapsed section
        # rather than folded into the primary table/tenure picker — these
        # bands don't share a common tenure axis to rank against each
        # other the way the 8 milestones do, so a per-tenure ranked table
        # doesn't apply; each bank's own real range is shown as-is instead
        # of forced into a nearest-milestone approximation.
        special_bands = special_tenure_bands(cmp_customer_type)
        with st.expander(
            f"🔍 Special / Additional Tenure Bands ({len(special_bands)}) — "
            f"not covered by the 8 tenures above"
        ):
            st.caption(
                "Real published FD bands (named schemes like 444/555/666-day "
                "deposits, plus other substantial-duration gaps) that don't "
                "match any of the 8 standard tenures — shown with each "
                "bank's own actual range, not approximated to the nearest one."
            )
            if not special_bands:
                st.caption("None found.")
            else:
                st.table([
                    {"Bank": resolve_bank_label(b["bank_id"]),
                     "Tenure band": b["tenure_label"],
                     "Rate": f'{b["rate"]}%'}
                    for b in special_bands
                ])

    with cmp_tab2:
        st.caption(
            "Most home loan rates are floating, tied to a benchmark (Repo / RLLR / "
            "MCLR) plus a spread — shown as the best available tier, not a single fixed figure."
        )
        loan_results = compare_home_loan()
        if not loan_results:
            st.info("No home loan data available.")
        else:
            hl_data_sources = _bank_data_sources("loan_rates", "loan_type", "home_loan")
            hl_freshness = _freshness_column(loan_results, hl_data_sources, "_home_loan")
            st.table([
                {"Rank": i, "Bank": resolve_bank_label(r["bank_id"]),
                 "Rate": (f'{r["rate_min"]}% – {r["rate_max"]}%' if r["rate_max"] != r["rate_min"]
                          else f'{r["rate_min"]}%'),
                 "Basis": r["benchmark_type"] or "—",
                 "Data": fresh}
                for i, (r, fresh) in enumerate(zip(loan_results, hl_freshness), 1)
            ])
            _asof_footnotes(loan_results, "loan_rates", "loan_type", "home_loan")
            _missing_bank_hint("Home Loan")

    with cmp_tab3:
        st.caption(
            "Most education loan rates are floating, tied to a benchmark "
            "(Repo / RLLR / EBLR / BRLLR) plus a spread — shown as the "
            "best available tier, not a single fixed figure."
        )
        edu_results = compare_loan("education_loan")
        if not edu_results:
            st.info("No education loan data available.")
        else:
            edu_data_sources = _bank_data_sources("loan_rates", "loan_type", "education_loan")
            edu_freshness = _freshness_column(edu_results, edu_data_sources, "_education_loan")
            st.table([
                {"Rank": i, "Bank": resolve_bank_label(r["bank_id"]),
                 "Rate": (f'{r["rate_min"]}% – {r["rate_max"]}%' if r["rate_max"] != r["rate_min"]
                          else f'{r["rate_min"]}%'),
                 "Basis": r["benchmark_type"] or "—",
                 "Data": fresh}
                for i, (r, fresh) in enumerate(zip(edu_results, edu_freshness), 1)
            ])
            _asof_footnotes(edu_results, "loan_rates", "loan_type", "education_loan")
            _missing_bank_hint("Education Loan")

    with cmp_tab4:
        st.caption(
            "Most vehicle loan rates are floating, tied to a benchmark "
            "(MCLR / RBLR / BRLLR / UCO Float / EBLR) plus a spread — shown "
            "as the best available tier, not a single fixed figure. Some "
            "banks' rates below are secondary-sourced (no first-party rate "
            "page found) — flagged in that bank's own RAG doc and DB row; "
            "verify with the bank before quoting."
        )
        vehicle_results = compare_loan("vehicle_loan")
        if not vehicle_results:
            st.info("No vehicle loan data available.")
        else:
            vl_data_sources = _bank_data_sources("loan_rates", "loan_type", "vehicle_loan")
            vl_freshness = _freshness_column(vehicle_results, vl_data_sources, "_vehicle_loan")
            st.table([
                {"Rank": i, "Bank": resolve_bank_label(r["bank_id"]),
                 "Rate": (f'{r["rate_min"]}% – {r["rate_max"]}%' if r["rate_max"] != r["rate_min"]
                          else f'{r["rate_min"]}%'),
                 "Basis": r["benchmark_type"] or "—",
                 "Data": fresh}
                for i, (r, fresh) in enumerate(zip(vehicle_results, vl_freshness), 1)
            ])
            _asof_footnotes(vehicle_results, "loan_rates", "loan_type", "vehicle_loan")
            _missing_bank_hint("Vehicle Loan")

    # Eligibility tab unwired from the UI 2026-08-19, per explicit user
    # instruction — check_eligibility() itself is untouched in
    # automation/eligibility.py; only this rendering block (and its
    # import above) are commented out. To restore: uncomment the import,
    # add a 5th tab to the st.tabs(...) call above, add a cmp_tab5 =
    # st.tabs(...) binding, and uncomment this block under `with cmp_tab5:`.
    #
    # with cmp_tab5:
    #     st.caption(
    #         "Explicit rule-based matching (deposit amount, customer type, "
    #         "tenure) — not an LLM guess. Every result shows exactly why it "
    #         "qualified; every near-miss is shown too, with the reason it "
    #         "didn't, rather than silently disappearing."
    #     )
    #     col_c, col_d, col_e = st.columns(3)
    #     with col_c:
    #         elig_amount = st.number_input(
    #             "Deposit amount (Rs.)", min_value=0, value=50000, step=1000, key="elig_amount"
    #         )
    #     with col_d:
    #         elig_customer_type = st.radio(
    #             "Customer type", ["general", "senior"], horizontal=True, key="elig_customer"
    #         )
    #     with col_e:
    #         elig_tenure_name = st.selectbox(
    #             "Tenure", list(_TENURE_OPTIONS.keys()), index=3, key="elig_tenure"
    #         )
    #     elig_result = check_eligibility(
    #         elig_amount, elig_customer_type, _TENURE_OPTIONS[elig_tenure_name]
    #     )
    #     if not elig_result["qualifying"]:
    #         st.info("No qualifying products found for these inputs.")
    #     else:
    #         st.markdown("**Qualifying products** (highest rate first):")
    #         for r in elig_result["qualifying"]:
    #             bank_label = resolve_bank_label(r["bank_id"])
    #             st.markdown(f"- **{bank_label}** — {r['rate']}% ({r['tenure_label']})")
    #             for reason in r["reasons"]:
    #                 st.caption(f"　　✓ {reason}")
    #     if elig_result["excluded"]:
    #         with st.expander(f"Not qualifying ({len(elig_result['excluded'])}) — shown, not hidden"):
    #             for e in elig_result["excluded"]:
    #                 bank_label = resolve_bank_label(e["bank_id"])
    #                 st.markdown(f"- **{bank_label}** ({e['tenure_label']}): {e['reason']}")

    render_disclaimer()
    st.stop()


# ══════════════════════════════════════════════════════════════════════
# Insights & Tools — same full-panel pattern as Compare above (own
# sidebar toggle, mutually exclusive, st.stop() after rendering).
# Roadmap: PROJECT_STATUS.md §9. Steps 1-5 built: rate-change digest
# (§10-10c), calculator (§11), latest banking news (§12), IBA news
# (§13), should-I-switch (§15), banker's view (§18-§19). Rate Trends was
# tried and consciously dropped (§14-14b, kept trend.py's
# rate_history() for possible future reuse). Personal tracking (step 6)
# remains a separate future session. Tab layout revised §19: Latest
# Banking News moved first, IBA News folded into Banker's View only
# (its standalone tab was a full duplicate), Banker's View renamed from
# "Banker View".
# ══════════════════════════════════════════════════════════════════════
if insights_on:
    st.subheader("📈 Insights & Tools")

    tab_news, tab_digest, tab_calc, tab_switch, tab_banker = st.tabs(
        ["📰 Latest Banking News", "This Week's Changes", "🧮 Calculator",
         "🔄 Should I Switch?", "🏦 Banker's View"]
    )

    with tab_news:
        st.markdown(
            f'<div style="font-size:1.15rem;font-weight:700;color:{_DIGEST_COLORS["ink"]};'
            f'margin-bottom:2px;">Latest Banking News</div>'
            f'<div style="color:{_DIGEST_COLORS["text_mute"]};font-size:12.5px;margin-bottom:14px;">'
            f'Real headlines from Economic Times (Banking) and Business Standard '
            f'(Finance) — headline, a one-line summary from the outlet\'s own feed, '
            f'and a link to the original article. Refreshes every 15 minutes.</div>',
            unsafe_allow_html=True,
        )
        news_view = st.radio(
            "View", ["Latest", "Archive"], horizontal=True, key="news_view", label_visibility="collapsed",
        )
        if news_view == "Latest":
            with st.spinner("Fetching latest headlines..."):
                render_news(limit=5)
        else:
            st.caption(
                "Every headline the Latest tab has fetched is stored here — "
                "a persistent record, not just whatever the live feed still shows."
            )
            render_news_archive(days=30)

    with tab_digest:
        st.markdown(
            f'<div style="font-size:1.15rem;font-weight:700;color:{_DIGEST_COLORS["ink"]};'
            f'margin-bottom:2px;">This Week\'s Changes</div>'
            f'<div style="color:{_DIGEST_COLORS["text_mute"]};font-size:12.5px;margin-bottom:14px;">'
            f'Real before/after rate differences across FD, home loan, and education '
            f'loan rates — confirmed by re-parsing historical snapshots, not a raw '
            f'"page changed" flag.</div>',
            unsafe_allow_html=True,
        )

        with st.spinner("Checking rate history across all banks and products..."):
            render_digest_cards(days=7)

        st.caption(
            "💬 Tip: you can also ask about this in chat — e.g. \"what's new "
            "this week?\" — from General Chat or any bank-scoped view."
        )

    with tab_calc:
        render_calculator()
        st.caption(
            "💬 Tip: you can also ask this in chat — e.g. \"what would ₹5 "
            "lakh in a BOB 1-year FD be worth at maturity?\" or \"EMI for a "
            "₹30 lakh HDFC home loan over 20 years?\""
        )

    with tab_switch:
        st.markdown(
            f'<div style="font-size:1.15rem;font-weight:700;color:{_DIGEST_COLORS["ink"]};'
            f'margin-bottom:2px;">Should I Switch?</div>'
            f'<div style="color:{_DIGEST_COLORS["text_mute"]};font-size:12.5px;margin-bottom:14px;">'
            f'Enter your current FD to see the best real alternative rate '
            f'available today and the estimated maturity-value difference.</div>',
            unsafe_allow_html=True,
        )
        render_switch()

    with tab_banker:
        st.markdown(
            f'<div style="font-size:1.15rem;font-weight:700;color:{_DIGEST_COLORS["ink"]};'
            f'margin-bottom:2px;">Banker\'s View</div>'
            f'<div style="color:{_DIGEST_COLORS["text_mute"]};font-size:12.5px;margin-bottom:14px;">'
            f'A genuinely separate section for bank staff — peer positioning, '
            f'competitively-framed rate movement, and operational context. Not a '
            f'toggle on the customer digest.</div>',
            unsafe_allow_html=True,
        )
        render_banker_view()

    render_disclaimer()
    st.stop()


# ══════════════════════════════════════════════════════════════════════
# Chat — unchanged flow, only the retrieval source depends on the mode
# selected above.
# ══════════════════════════════════════════════════════════════════════

if not st.session_state.messages:
    welcome_subject = display_title if (selected_bank or explore_mode == "RBI Guidelines") else "any bank"
    st.markdown(f"""
    <div class="welcome-box">
        <p>👋 Welcome, <b>{st.session_state.get("username","").title()}</b> —
        ask about {welcome_subject} products, interest rates, and services.</p>
    </div>
    """, unsafe_allow_html=True)

    cols = st.columns(2)
    for i, ex in enumerate(display_examples):
        with cols[i % 2]:
            if st.button(f"💬 {ex}", key=f"ex_{explore_mode}_{selected_bank}_{i}",
                        use_container_width=True):
                st.session_state.messages.append({
                    "role": "user", "content": ex,
                    "sources": [], "time": 0
                })
                st.rerun()


for msg in st.session_state.messages:
    if msg["role"] == "user":
        st.markdown(
            f'<div class="user-msg"><b>You</b><br>{msg["content"]}</div>',
            unsafe_allow_html=True
        )
    else:
        answer_html  = msg["content"].replace("\n", "<br>")
        sources_html = ""
        if show_sources and msg.get("sources"):
            badges = "".join(
                f'<span class="source-badge">📄 '
                f'{(s.get("bank", "") + " · ") if s.get("bank") else ""}'
                f'{s["doc_id"].rsplit("_chunk", 1)[0]} '
                f'{("(" + s["date"] + ") ") if s.get("date") else ""}'
                f'({s["score"]:.2f})</span>'
                for s in msg["sources"]
            )
            sources_html = (
                f'<div style="margin-top:8px;">'
                f'<span style="font-size:11px;color:#888;">Sources: </span>'
                f'{badges}</div>'
            )
        time_html = (
            f'<div style="font-size:11px;color:#aaa;margin-top:6px;">'
            f'⏱️ {msg.get("time", 0):.1f}s</div>'
        ) if msg.get("time") else ""

        st.markdown(
            f'<div class="bot-msg">'
            f'<b style="color:var(--accent);">🏦 Assistant</b><br><br>'
            f'{answer_html}{sources_html}{time_html}</div>',
            unsafe_allow_html=True
        )


msgs = st.session_state.messages
if msgs and msgs[-1]["role"] == "user" and resources_ok:
    query = msgs[-1]["content"]

    # Digest shortcut — a "what changed this week" style question is
    # answered directly from automation/digest.py's real data, bypassing
    # RAG retrieval + the LLM entirely, same discipline as Compare/
    # Insights already reading structured data directly rather than
    # letting the model paraphrase (and risk fabricating) real numbers.
    if _DIGEST_INTENT_RE.search(query):
        t0 = time.time()
        answer = render_digest(days=7)
        retrieved = []
        elapsed = time.time() - t0
        st.session_state.messages.append({
            "role"    : "assistant",
            "content" : answer,
            "sources" : retrieved,
            "time"    : elapsed,
        })
        st.session_state.total_queries += 1
        st.rerun()

    # Calculator shortcut — an FD-maturity or loan-EMI style question is
    # answered by extracting parameters via Groq (language understanding
    # only) and then computing the real number deterministically through
    # automation/calculator.py, exactly like the Insights & Tools panel
    # does. The LLM never states the final figure itself.
    if _CALC_INTENT_RE.search(query):
        t0 = time.time()
        parsed = _parse_calculator_query(query)
        if parsed is None:
            answer = (
                "I couldn't process that calculator question right now — "
                "please try again, or use the Calculator tab under Insights & Tools."
            )
        else:
            answer = render_calculator_answer(parsed)
        retrieved = []
        elapsed = time.time() - t0
        st.session_state.messages.append({
            "role"    : "assistant",
            "content" : answer,
            "sources" : retrieved,
            "time"    : elapsed,
        })
        st.session_state.total_queries += 1
        st.rerun()

    with st.spinner("🔍 Searching knowledge base..."):
        t0 = time.time()
        if explore_mode == "RBI Guidelines":
            retrieved = retrieve(query, index, chunks, embed_model, recency_sort=True)
            answer = generate_answer(query, retrieved, groq_client)
        elif selected_bank:
            bank_filter = None if selected_bank == "INDIAN_BANK" else selected_bank
            retrieved = retrieve(query, index, chunks, embed_model, bank_filter=bank_filter)
            answer = generate_answer(query, retrieved, groq_client)
        else:
            retrieved = retrieve_general(query, pools, embed_model)
            answer = generate_answer(
                query, retrieved, groq_client,
                fallback_hint=" For RBI rules and guidelines, switch to RBI Guidelines above.",
            )
        elapsed = time.time() - t0

    st.session_state.messages.append({
        "role"    : "assistant",
        "content" : answer,
        "sources" : retrieved,
        "time"    : elapsed
    })
    st.session_state.total_queries += 1
    st.rerun()


st.markdown("<br>", unsafe_allow_html=True)
# A form is required here, not a bare text_input + button — see login
# form's comment above for why (clear_on_submit also resets the field).
with st.form("chat_input_form", clear_on_submit=True):
    col1, col2 = st.columns([6, 1])
    with col1:
        user_input = st.text_input(
            "Ask a question...",
            placeholder="e.g. What is the FD interest rate for senior citizens?",
            label_visibility="collapsed",
        )
    with col2:
        send = st.form_submit_button("Send →", use_container_width=True)

if send and user_input.strip() and resources_ok:
    st.session_state.messages.append({
        "role"   : "user",
        "content": user_input.strip(),
        "sources": [],
        "time"   : 0
    })
    st.rerun()

render_disclaimer()
