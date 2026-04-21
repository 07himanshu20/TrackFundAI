"""
company_config.py
=================
Central registry of every company in the portfolio.

For each company this stores:
  - Where to find its Excel MIS file
  - Its position in the fund/sector/segment hierarchy
  - Known facts about currency and scale (hints to Gemini)
  - An optional "override" dict for edge cases Gemini might misidentify

Adding a NEW company to the system:
  1. Drop its Excel file somewhere accessible
  2. Add an entry to COMPANY_REGISTRY below
  3. Run: python -m api.portfolio.builder   (rebuilds portfolio.json)

That is it. No Python parser code needed.

Scale values:
  "full"        - values are in full currency units (1234 means 1234)
  "thousands"   - values are in thousands (1234 means 1,234,000)
  "millions"    - values are in millions
  "lakhs"       - Indian format: 1 lakh = 100,000
  "crores"      - Indian format: 1 crore = 10,000,000

use_legacy_parser: if True, use the old hardcoded parser instead of Gemini.
  Keep this True for existing real companies until Gemini output is validated.
  Set to False to switch to Gemini for new companies.
"""

from __future__ import annotations
import os

# Base directory where Excel files are stored
EXCEL_DIR = "/Users/himanshusharma/Trivesta_VC_Work"


def _path(*parts: str) -> str:
    return os.path.join(EXCEL_DIR, *parts)


# ---------------------------------------------------------------------------
# Company Registry
# Each entry is a dict consumed by builder.py
# ---------------------------------------------------------------------------

COMPANY_REGISTRY: list[dict] = [

    # ────────────────────────────────────────────────────────────────────
    # Fund 1: Healthcare & Life Sciences
    # ────────────────────────────────────────────────────────────────────

    {
        # Hierarchy placement
        "fund_slug":    "healthcare",
        "fund_name":    "Healthcare & Life Sciences Fund",
        "sector_slug":  "distribution",
        "sector_name":  "Healthcare Distribution",
        "sector_desc":  "Healthcare and life-science product distribution across Asia + global devices.",
        "segment_slug": "lab_distribution",
        "segment_name": "Lab Equipment Distribution",
        "segment_desc": "Distributors of laboratory and life-science equipment in SE Asia.",

        # Company identity
        "name":        "Analisa Resources (M) Sdn. Bhd.",
        "slug":        "analisa",
        "currency":    "MYR",
        "description": "Malaysian life-science equipment distributor (HID, LabFriend, Sci.Lab, NGS, Service segments).",
        "is_real":     True,

        # File(s) — list supports multi-file companies (e.g. Board + Team views)
        "files": [
            {
                "path": _path("01 Monthly Financial Presentation 2025 May Analisa.xlsx"),
                "role": "primary",
            }
        ],

        # Gemini hints — what we already know about this file's format
        "hints": {
            "scale": "thousands",        # '05 Summary P&L (2)' header: "In MYR'000"
            "reporting_period": "May 2025",
            "notes": (
                "This is a multi-sheet MIS. The summary P&L is in '05 Summary P&L (2)' "
                "with header 'In MYR'000' — values are in MYR thousands. "
                "Monthly trend is in 'Montly PL (2)' (2025) and 'Montly PL' (2024). "
                "Cash flow is in '09 Cash Flow (Jan-May)'. "
                "DSO/DIO/DPO is in '12. DSO DSI DIO'. "
                "Sales segments are in '03 Sales update'."
            ),
        },

        # Use legacy parser (validated, keeps existing portfolio.json numbers)
        # Set to False to switch to Gemini
        "use_legacy_parser": False,
        "legacy_parser_module": "api.portfolio.parsers.analisa",
    },

    {
        "fund_slug":    "healthcare",
        "fund_name":    "Healthcare & Life Sciences Fund",
        "sector_slug":  "distribution",
        "sector_name":  "Healthcare Distribution",
        "sector_desc":  "Healthcare and life-science product distribution across Asia + global devices.",
        "segment_slug": "lab_distribution",
        "segment_name": "Lab Equipment Distribution",
        "segment_desc": "Distributors of laboratory and life-science equipment in SE Asia.",

        "name":        "Chemopharm Group (CPM)",
        "slug":        "cpm",
        "currency":    "MYR",
        "description": "Multi-country (MY/SG/VN/TH/ID/PHP) life-science & chemical distribution group.",
        "is_real":     True,

        "files": [
            {
                "path": _path("0625 CPM Group ECPM June 25.xlsx"),
                "role": "primary",
            }
        ],

        "hints": {
            "scale": "full",
            "reporting_period": "June 2025",
            "notes": (
                "This is a group consolidated MIS. Use 'Country (YTD)' sheet for headline P&L. "
                "The TOTAL column (including Hausen) is the consolidated figure. "
                "Budget columns are labelled 'CY 25 Budget'. "
                "Segment breakdown is in 'Segment sales & gp'. "
                "All values in full MYR."
            ),
        },

        "use_legacy_parser": False,
        "legacy_parser_module": "api.portfolio.parsers.cpm",
    },

    {
        "fund_slug":    "healthcare",
        "fund_name":    "Healthcare & Life Sciences Fund",
        "sector_slug":  "distribution",
        "sector_name":  "Healthcare Distribution",
        "sector_desc":  "Healthcare and life-science product distribution across Asia + global devices.",
        "segment_slug": "diagnostics",
        "segment_name": "Diagnostics & Reagents",
        "segment_desc": "Clinical diagnostics and reagents distribution.",

        "name":        "Integris EL Group",
        "slug":        "integris",
        "currency":    "USD",
        "description": "Multi-country (MY/SG/IN/PH/TH/ID-VN) clinical diagnostics & life-science distribution group.",
        "is_real":     True,

        "files": [
            {
                "path": _path("Integris EL MIS - EL May'25.xlsx"),
                "role": "primary",
            }
        ],

        "hints": {
            "scale": "millions",        # Finance sheet values are in $M
            "reporting_period": "May 2025",
            "notes": (
                "The main data is in the 'Finance' sheet. "
                "All monetary values are in USD millions — multiply by 1,000,000. "
                "Column layout: col 1=Actual MTD, col 2=AOP MTD, col 3=Prior MTD, "
                "col 7=Actual YTD FY26, col 8=AOP YTD, col 9=Prior YTD. "
                "GM% rows may be stored as fractions (0.42 = 42%). "
                "SG&A is OPEX. 'Total Revenue' is the revenue row. 'Total GM' is gross profit."
            ),
        },

        "use_legacy_parser": False,
        "legacy_parser_module": "api.portfolio.parsers.integris",
    },

    {
        "fund_slug":    "healthcare",
        "fund_name":    "Healthcare & Life Sciences Fund",
        "sector_slug":  "distribution",
        "sector_name":  "Healthcare Distribution",
        "sector_desc":  "Healthcare and life-science product distribution across Asia + global devices.",
        "segment_slug": "devices",
        "segment_name": "Medical Devices (Cardiology)",
        "segment_desc": "Cardiology stents, balloons, and catheter products (DES/DCB/PTCA).",

        "name":        "Stent-Co (Board View)",
        "slug":        "stentco_board",
        "currency":    "EUR",
        "description": "Cardiology medical-device distributor (DES/DCB/PTCA stents & balloons), global sales.",
        "is_real":     True,

        "files": [
            {
                "path": _path("Sale Report_Board_May-25.xlsx"),
                "role": "primary",
            }
        ],

        "hints": {
            "scale": "thousands",       # Revenue values are in EUR '000
            "reporting_period": "YTD July 2025",
            "notes": (
                "Sales report workbook. Main data is in 'Summary_Brand-YTD' sheet. "
                "Values are in EUR thousands — multiply by 1,000. "
                "Column layout (0-indexed): col 7=Brand name, col 4=Actual YTD Revenue, "
                "col 5=Actual YTD GM, col 11=AOP Revenue, col 12=AOP GM. "
                "Categories are DES, DCB, PTCA. 'Grand Total' row has portfolio total. "
                "AOP = Annual Operating Plan = Budget."
            ),
        },

        "use_legacy_parser": False,
        "legacy_parser_module": "api.portfolio.parsers.stentco",
        "legacy_parser_kwargs": {"view": "board"},
    },

    {
        "fund_slug":    "healthcare",
        "fund_name":    "Healthcare & Life Sciences Fund",
        "sector_slug":  "distribution",
        "sector_name":  "Healthcare Distribution",
        "sector_desc":  "Healthcare and life-science product distribution across Asia + global devices.",
        "segment_slug": "devices",
        "segment_name": "Medical Devices (Cardiology)",
        "segment_desc": "Cardiology stents, balloons, and catheter products (DES/DCB/PTCA).",

        "name":        "Stent-Co (Team View)",
        "slug":        "stentco_team",
        "currency":    "EUR",
        "description": "Cardiology medical-device distributor (DES/DCB/PTCA stents & balloons), team-level breakdown.",
        "is_real":     True,

        "files": [
            {
                "path": _path("Sale Report_Team_May-25.xlsx"),
                "role": "primary",
            }
        ],

        "hints": {
            "scale": "thousands",
            "reporting_period": "YTD July 2025",
            "notes": (
                "Same structure as Board view but with team-level detail. "
                "Values in EUR thousands. Use 'Summary_Brand-YTD' sheet. "
                "AOP = Budget."
            ),
        },

        "use_legacy_parser": False,
        "legacy_parser_module": "api.portfolio.parsers.stentco",
        "legacy_parser_kwargs": {"view": "team"},
    },

    # ────────────────────────────────────────────────────────────────────
    # To add a new company: copy the block above and fill in the details.
    # Only 'path', 'name', 'slug', 'currency', and hierarchy fields are
    # required. 'hints' are optional but improve accuracy.
    # ────────────────────────────────────────────────────────────────────

]


# ---------------------------------------------------------------------------
# Helper: group companies by fund → sector → segment
# ---------------------------------------------------------------------------

def get_fund_hierarchy() -> dict:
    """
    Returns a nested dict: fund_slug → sector_slug → segment_slug → [companies]
    Used by builder.py to assemble the portfolio tree.
    """
    hierarchy = {}
    for company in COMPANY_REGISTRY:
        fs = company["fund_slug"]
        ss = company["sector_slug"]
        sg = company["segment_slug"]
        hierarchy.setdefault(fs, {
            "name": company["fund_name"],
            "slug": fs,
            "sectors": {},
        })
        hierarchy[fs]["sectors"].setdefault(ss, {
            "name": company["sector_name"],
            "slug": ss,
            "description": company.get("sector_desc", ""),
            "segments": {},
        })
        hierarchy[fs]["sectors"][ss]["segments"].setdefault(sg, {
            "name": company["segment_name"],
            "slug": sg,
            "description": company.get("segment_desc", ""),
            "companies": [],
        })
        hierarchy[fs]["sectors"][ss]["segments"][sg]["companies"].append(company)
    return hierarchy
