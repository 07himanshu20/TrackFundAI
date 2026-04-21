"""
analisa.py
Analisa Resources (M) Sdn. Bhd. parser.
Reuses the existing single-company excel_parser.parse_excel() and reshapes
its output into the unified portfolio schema.

File: 01 Monthly Financial Presentation 2025 May Analisa.xlsx
Native currency: MYR
Segments: HID, LabFriend, Project/NGS, Sci.Lab, Sci.Lab-Qiagen, Service
"""

from api.excel_parser import parse_excel
from api.portfolio.schema import empty_financials


COMPANY_NAME = "Analisa Resources (M) Sdn. Bhd."
COMPANY_SLUG = "analisa"
CURRENCY = "MYR"


def parse(filepath: str) -> dict:
    """
    Parse the Analisa Excel file and return a `Financials` dict + segment children.

    Returns:
        {
            "company_meta": {name, slug, currency, report_month, ...},
            "financials": Financials,
            "segments": [   # one child node per business segment
                {"id", "name", "financials": Financials},
                ...
            ],
        }
    """
    raw = parse_excel(filepath)

    fin = empty_financials()

    # ---- Summary KPIs (current period: May 2025) ----
    s = raw.get("summary", {})

    def _val(key, sub="actual_month"):
        return (s.get(key) or {}).get(sub)

    fin["summary"] = {
        "period": raw.get("report_month", "May 2025"),
        "revenue": _val("revenue"),
        "cogs": _val("cogs"),
        "gross_profit": _val("gross_profit"),
        "gp_pct": _val("gp_pct"),
        "opex": _val("opex"),
        "ebitda": _val("ebitda"),
        "normalized_ebitda": _val("normalized_ebitda"),
        # YTD too
        "ytd_revenue": _val("revenue", "actual_ytd"),
        "ytd_gross_profit": _val("gross_profit", "actual_ytd"),
        "ytd_ebitda": _val("ebitda", "actual_ytd"),
        # Budget for current month/YTD
        "budget_revenue": _val("revenue", "budget_month"),
        "budget_ebitda": _val("ebitda", "budget_month"),
        "ytd_budget_revenue": _val("revenue", "budget_ytd"),
        "ytd_budget_ebitda": _val("ebitda", "budget_ytd"),
    }

    # ---- Monthly P&L trend ----
    for m in raw.get("monthly_pl", []):
        period = f"{m['year']}-{m['month_num']:02d}"
        revenue = m.get("revenue") or 0
        cogs = m.get("cogs") or 0
        gp = revenue - cogs
        gp_pct = round(gp / revenue * 100, 2) if revenue else None
        opex = m.get("opex") or 0
        ebitda = m.get("ebitda")
        ebitda_pct = round((ebitda / revenue * 100), 2) if ebitda is not None and revenue else None

        fin["monthly_pl"].append({
            "period": period,
            "revenue": revenue,
            "cogs": cogs,
            "gross_profit": gp,
            "gp_pct": gp_pct,
            "opex": opex,
            "ebitda": ebitda,
            "ebitda_pct": ebitda_pct,
            "normalized_ebitda": m.get("normalized_ebitda"),
        })

    # ---- Cash flow ----
    for cf in raw.get("cash_flow", []):
        # Existing parser stores in MYR '000 — convert to full MYR for uniformity
        def _k(v):
            return (v * 1000) if isinstance(v, (int, float)) else None
        period = cf.get("period")
        # Try to normalise period to YYYY-MM
        if isinstance(period, str) and len(period) >= 6 and "-" in period:
            iso_period = period
        else:
            iso_period = str(period) if period else ""
        fin["cash_flow"].append({
            "period": iso_period,
            "opening_cash": _k(cf.get("opening_cash")),
            "operating_cf": _k(cf.get("operating_cf")),
            "investing_cf": _k(cf.get("investing_cf")),
            "financing_cf": _k(cf.get("financing_cf")),
            "net_cash_flow": _k(cf.get("net_cash_flow")),
            "closing_cash": _k(cf.get("closing_cash")),
        })

    # ---- Working capital ----
    wc = raw.get("working_capital", {})
    if isinstance(wc, dict) and wc:
        # The existing parser returns nested structure — flatten to a list of points
        # keyed by period if available, else single snapshot
        periods_set = set()
        for metric_data in wc.values():
            if isinstance(metric_data, dict):
                periods_set.update(metric_data.keys())
        periods = sorted(p for p in periods_set if p)
        if periods:
            for p in periods:
                fin["working_capital"].append({
                    "period": p,
                    "dso": (wc.get("dso") or {}).get(p),
                    "dio": (wc.get("dio") or {}).get(p),
                    "dpo": (wc.get("dpo") or {}).get(p),
                    "nwc": (wc.get("nwc") or {}).get(p),
                    "ccc": (wc.get("ccc") or {}).get(p),
                })
        else:
            # Snapshot only
            fin["working_capital"].append({
                "period": raw.get("report_month", "May 2025"),
                "dso": wc.get("dso"),
                "dio": wc.get("dio"),
                "dpo": wc.get("dpo"),
                "nwc": wc.get("nwc"),
                "ccc": wc.get("ccc"),
            })

    # ---- Sales by segment (collect for company AND build segment children) ----
    # Existing parser returns: {ytd_comparison: {segment_name: {ytd_2024, ytd_2025, yoy_ratio}}, monthly: [...]}
    segments_raw = raw.get("sales_segments", {})
    segment_children = []

    ytd_comp = (segments_raw or {}).get("ytd_comparison", {})
    monthly_seg = (segments_raw or {}).get("monthly", [])

    # Build per-segment monthly time series (for segment-level financials)
    by_segment_monthly = {}
    for pt in monthly_seg:
        seg = pt.get("segment")
        if not seg:
            continue
        by_segment_monthly.setdefault(seg, []).append(pt)

    for seg_name, vals in ytd_comp.items():
        if seg_name == "Total":
            continue
        seg_ytd_2025 = vals.get("ytd_2025") or 0
        seg_ytd_2024 = vals.get("ytd_2024") or 0
        # yoy_ratio in raw can be either a ratio (~1.37) OR an absolute value depending on
        # whether the source cell was a % or a number. Compute YoY% from ytd values directly.
        yoy_pct = None
        if seg_ytd_2024:
            yoy_pct = round((seg_ytd_2025 - seg_ytd_2024) / seg_ytd_2024 * 100, 2)

        # Roll into company sales_by_segment (using YTD 2025 revenue)
        fin["sales_by_segment"].append({
            "label": seg_name,
            "revenue": seg_ytd_2025,
            "ytd_prior": seg_ytd_2024,
            "yoy_pct": yoy_pct,
        })

        # Build a segment child node — its financials carry sales summary +
        # the monthly per-period revenue series for that segment.
        seg_fin = empty_financials()
        seg_fin["summary"] = {
            "period": raw.get("report_month", "May 2025"),
            "ytd_revenue": seg_ytd_2025,
            "ytd_revenue_prior": seg_ytd_2024,
            "yoy_pct": yoy_pct,
        }
        # Carry the monthly sales points for this segment
        for pt in by_segment_monthly.get(seg_name, []):
            seg_fin["monthly_pl"].append({
                "period": pt.get("period"),
                "revenue": pt.get("revenue"),
            })

        slug = (seg_name.lower()
                .replace(" ", "_")
                .replace("/", "_")
                .replace(".", "")
                .replace("-", "_"))
        segment_children.append({
            "id_slug": slug,
            "name": seg_name,
            "financials": seg_fin,
        })

    # ---- Cost structure (current period) ----
    rev = fin["summary"].get("revenue") or 0
    cogs = fin["summary"].get("cogs") or 0
    opex = fin["summary"].get("opex") or 0
    ebitda = fin["summary"].get("ebitda") or 0
    if rev:
        fin["cost_structure"] = {
            "cogs_pct": round(cogs / rev * 100, 2),
            "gp_pct": round((rev - cogs) / rev * 100, 2),
            "opex_pct": round(opex / rev * 100, 2),
            "ebitda_pct": round(ebitda / rev * 100, 2),
        }

    # ---- Budget vs Actual (current period summary) ----
    for li_key, label in [
        ("revenue", "Revenue"),
        ("cogs", "COGS"),
        ("gross_profit", "Gross Profit"),
        ("opex", "OPEX"),
        ("ebitda", "EBITDA"),
    ]:
        actual = (s.get(li_key) or {}).get("actual_month")
        budget = (s.get(li_key) or {}).get("budget_month")
        if actual is None and budget is None:
            continue
        var = (actual - budget) if (actual is not None and budget is not None) else None
        var_pct = round(var / budget * 100, 2) if (var is not None and budget) else None
        fin["budget_vs_actual"].append({
            "period": raw.get("report_month", "May 2025"),
            "line_item": label,
            "budget": budget,
            "actual": actual,
            "variance": var,
            "variance_pct": var_pct,
        })

    return {
        "company_meta": {
            "name": COMPANY_NAME,
            "slug": COMPANY_SLUG,
            "currency": CURRENCY,
            "report_month": raw.get("report_month", "May 2025"),
            "description": "Malaysian life-science equipment distributor (HID, LabFriend, Sci.Lab, NGS, Service segments).",
        },
        "financials": fin,
        "segments": segment_children,
        "_raw_parse_report": raw.get("parse_report", {}),
    }
