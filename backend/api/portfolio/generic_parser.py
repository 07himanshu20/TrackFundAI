"""
generic_parser.py
=================
Single entry point for parsing ANY company MIS Excel file.

Pipeline:
  1. mis_extractor.extract_summary_for_gemini(filepath) → compact raw_data
  2. gemini_mis_parser.parse_mis_with_gemini(raw_data, hints) → Financials dict
  3. Return {company_meta, financials} in the same shape as the old hardcoded parsers

This replaces analisa.py, cpm.py, integris.py, stentco.py for new companies.
The old parsers are kept as reference implementations and will be used as
VALIDATION sources during testing (we compare their output to generic_parser output).

Usage:
    from api.portfolio.generic_parser import parse

    result = parse(
        filepath="/path/to/company_mis.xlsx",
        company_name="Acme Corp",
        company_slug="acme",
        currency="MYR",          # native currency — passed as hint to Gemini
        scale="thousands",        # optional: "full"|"thousands"|"millions"|"lakhs"
        reporting_period="May 2025",  # optional hint
        description="...",        # optional company description
    )
    # result = {"company_meta": {...}, "financials": Financials, "segments": [...]}
"""

from __future__ import annotations

import logging
import json
import os
from typing import Optional

from api.portfolio.mis_extractor import extract_summary_for_gemini
from api.portfolio.gemini_mis_parser import parse_mis_with_gemini
from api.portfolio.schema import empty_financials

logger = logging.getLogger(__name__)


def parse(
    filepath: str,
    company_name: str,
    company_slug: str,
    currency: str = "USD",
    scale: Optional[str] = None,
    reporting_period: Optional[str] = None,
    description: Optional[str] = None,
    extra_hints: Optional[dict] = None,
) -> dict:
    """
    Parse any MIS Excel file using Gemini semantic analysis.

    Returns:
        {
            "company_meta": {
                "name": str,
                "slug": str,
                "currency": str,
                "report_month": str,
                "description": str,
                "gemini_structure": dict,   # Pass 1 structure analysis
            },
            "financials": Financials,       # canonical schema
            "segments": [],                 # Gemini populates sales_by_segment
                                            # caller may convert to segment children
        }
    """
    logger.info("generic_parser: parsing '%s' for company '%s'", filepath, company_name)

    # Optional short-circuit: reuse a validated Gemini cache if GEMINI_VALIDATE_CACHE is set.
    # Lets builder.py assemble portfolio.json from already-validated output instead of
    # burning API quota on every build.
    cache_dir = os.environ.get("GEMINI_VALIDATE_CACHE")
    if cache_dir:
        cpath = os.path.join(cache_dir, f"{company_slug}.json")
        if os.path.exists(cpath):
            logger.info("Reusing cached Gemini output: %s", cpath)
            with open(cpath) as f:
                financials = json.load(f)
            period = (
                reporting_period
                or (financials.get("summary") or {}).get("period")
                or "Unknown"
            )
            segments = _build_segment_children(financials.get("sales_by_segment", []))
            return {
                "company_meta": {
                    "name": company_name,
                    "slug": company_slug,
                    "currency": currency,
                    "report_month": period,
                    "description": description or "",
                },
                "financials": financials,
                "segments": segments,
            }

    if not os.path.exists(filepath):
        raise FileNotFoundError(f"MIS file not found: {filepath}")

    # Step 1: Extract raw workbook content
    logger.info("Step 1: extracting workbook cells...")
    raw_data = extract_summary_for_gemini(filepath)
    logger.info(
        "Extracted %d sheets, total chars: %d",
        len(raw_data.get("sheets", [])),
        len(json.dumps(raw_data, separators=(",", ":")))
    )

    # Step 2: Build company hints dict
    hints = {
        "company_name": company_name,
        "currency": currency,
    }
    if scale:
        hints["scale"] = scale
    if reporting_period:
        hints["reporting_period"] = reporting_period
    if extra_hints:
        hints.update(extra_hints)

    # Step 3: Gemini two-pass extraction
    logger.info("Step 2: running Gemini two-pass extraction...")
    financials = parse_mis_with_gemini(raw_data, company_hints=hints)

    # Step 4: Determine the actual reporting period
    period = (
        reporting_period
        or (financials.get("summary") or {}).get("period")
        or "Unknown"
    )

    # Step 5: Build segment children from sales_by_segment
    # (same shape as the old hardcoded parsers — callers use this for the hierarchy)
    segments = _build_segment_children(financials.get("sales_by_segment", []))

    return {
        "company_meta": {
            "name": company_name,
            "slug": company_slug,
            "currency": currency,
            "report_month": period,
            "description": description or "",
        },
        "financials": financials,
        "segments": segments,
    }


def _build_segment_children(sales_by_segment: list) -> list:
    """
    Convert sales_by_segment entries into the segment-child format
    expected by builder.py (_make_internal_node).
    """
    from api.portfolio.schema import empty_financials

    children = []
    for seg in sales_by_segment:
        label = seg.get("label", "")
        if not label:
            continue
        slug = (
            label.lower()
            .replace(" ", "_")
            .replace("/", "_")
            .replace(".", "")
            .replace("-", "_")
            .replace("&", "and")
            .replace("(", "")
            .replace(")", "")
        )
        seg_fin = empty_financials()
        seg_fin["summary"] = {
            "revenue": seg.get("revenue"),
            "gross_profit": seg.get("gross_margin"),
            "gp_pct": seg.get("gm_pct"),
        }
        children.append({
            "id_slug": slug,
            "name": label,
            "financials": seg_fin,
        })
    return children


# ---------------------------------------------------------------------------
# Standalone test runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Run from backend directory:
        python -m api.portfolio.generic_parser <filepath> <company_name> <slug> <currency>

    Example:
        python -m api.portfolio.generic_parser \\
            "/Users/himanshusharma/Trivesta_VC_Work/01 Monthly Financial Presentation 2025 May Analisa.xlsx" \\
            "Analisa Resources (M) Sdn. Bhd." analisa MYR
    """
    import sys
    import django
    import os

    # Minimal Django setup for standalone run
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    django.setup()

    if len(sys.argv) < 5:
        print("Usage: python -m api.portfolio.generic_parser <filepath> <company_name> <slug> <currency>")
        sys.exit(1)

    filepath = sys.argv[1]
    company_name = sys.argv[2]
    slug = sys.argv[3]
    currency = sys.argv[4]
    scale = sys.argv[5] if len(sys.argv) > 5 else None

    result = parse(
        filepath=filepath,
        company_name=company_name,
        company_slug=slug,
        currency=currency,
        scale=scale,
    )

    print("\n=== company_meta ===")
    print(json.dumps(result["company_meta"], indent=2))
    print("\n=== financials.summary ===")
    print(json.dumps(result["financials"]["summary"], indent=2))
    print(f"\n=== monthly_pl ({len(result['financials']['monthly_pl'])} rows) ===")
    for row in result["financials"]["monthly_pl"][-6:]:
        print(f"  {row['period']}: rev={row['revenue']}, ebitda={row['ebitda']}")
    print(f"\n=== budget_vs_actual ({len(result['financials']['budget_vs_actual'])} rows) ===")
    for row in result["financials"]["budget_vs_actual"]:
        print(f"  {row['line_item']}: actual={row['actual']}, budget={row['budget']}, var%={row['variance_pct']}")
    print(f"\n=== segments ({len(result['segments'])}) ===")
    for s in result["segments"]:
        print(f"  {s['name']}: rev={s['financials']['summary'].get('revenue')}")
