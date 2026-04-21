"""
compare.py
Build comparison payloads for 2+ nodes at any level.

Given a list of node ids and a `mode` (actual | sales_margin | variance)
and a `metric` (revenue | gross_profit | ebitda | gp_pct | ebitda_pct |
opex | closing_cash | nwc), produce a uniform payload with:

  - a chart block ({labels, datasets, yFormat})
  - a table block ({columns, rows})
  - per-entity summary chips

The shape is the same regardless of level, so the frontend can render
fund-vs-fund, sector-vs-sector, segment-vs-segment, or company-vs-company
with the same component.
"""

from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Mode + metric definitions
# ---------------------------------------------------------------------------

MODE_ACTUAL = "actual"          # Budget vs Actual MIS (absolute values)
MODE_SALES_MARGIN = "sales_margin"  # Sales + Gross Margin breakdown
MODE_VARIANCE = "variance"      # Budget Variance (Actual - Budget, % var)
MODE_KPI_TABLE = "kpi_table"    # Multi-KPI side-by-side table (rows=entities, cols=KPIs)

MODES = {MODE_ACTUAL, MODE_SALES_MARGIN, MODE_VARIANCE, MODE_KPI_TABLE}

# Metric -> (summary_key, label, y_format)
METRICS = {
    "revenue":       ("revenue",       "Revenue",          "USD"),
    "gross_profit":  ("gross_profit",  "Gross Profit",     "USD"),
    "ebitda":        ("ebitda",        "EBITDA",           "USD"),
    "opex":          ("opex",          "OPEX",             "USD"),
    "gp_pct":        ("gp_pct",        "Gross Margin %",   "percent"),
    "ebitda_pct":    ("ebitda_pct",    "EBITDA %",         "percent"),
    "ytd_revenue":   ("ytd_revenue",   "YTD Revenue",      "USD"),
    "ytd_ebitda":    ("ytd_ebitda",    "YTD EBITDA",       "USD"),
    "closing_cash":  ("__cf_closing",  "Closing Cash",     "USD"),
    "nwc":           ("__wc_nwc",      "Net Working Cap.", "USD"),
}


def build_comparison(
    nodes: list[dict],
    mode: str = MODE_ACTUAL,
    metric: str = "revenue",
    as_of: Optional[str] = None,
    range_from: Optional[str] = None,
    range_to: Optional[str] = None,
) -> dict:
    """
    Build a comparison payload for a list of nodes (2+).
    Returns:
      {
        "mode": ...,
        "metric": ...,
        "entities": [{id, name, level, currency, is_real}, ...],
        "chart": {type, title, labels, datasets, yFormat},
        "table": {columns: [...], rows: [[...], ...]},
        "summary_chips": [...],
      }
    """
    if mode not in MODES:
        mode = MODE_ACTUAL
    if metric not in METRICS:
        metric = "revenue"

    if mode == MODE_ACTUAL:
        return _build_actual(nodes, metric)
    elif mode == MODE_SALES_MARGIN:
        return _build_sales_margin(nodes)
    elif mode == MODE_VARIANCE:
        return _build_variance(nodes, metric)
    elif mode == MODE_KPI_TABLE:
        return _build_kpi_table(nodes, as_of=as_of, range_from=range_from, range_to=range_to)
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entity(node: dict) -> dict:
    return {
        "id": node.get("id"),
        "name": node.get("name"),
        "level": node.get("level"),
        "currency": node.get("currency"),
        "is_real": node.get("is_real", False),
    }


def _resolve_metric(node: dict, key: str):
    fin = node.get("financials", {}) or {}
    if key == "__cf_closing":
        cf = fin.get("cash_flow", []) or []
        return cf[-1].get("closing_cash") if cf else None
    if key == "__wc_nwc":
        wc = fin.get("working_capital", []) or []
        return wc[-1].get("nwc") if wc else None
    return (fin.get("summary", {}) or {}).get(key)


def _bva_for(node: dict, line_item: str) -> tuple[Optional[float], Optional[float]]:
    """Return (actual, budget) for the matching line_item in budget_vs_actual list."""
    bva = (node.get("financials", {}) or {}).get("budget_vs_actual", []) or []
    for row in bva:
        if row.get("line_item", "").lower() == line_item.lower():
            return row.get("actual"), row.get("budget")
    # Fallback: pull from summary keys
    summary = (node.get("financials", {}) or {}).get("summary", {}) or {}
    if line_item.lower() == "revenue":
        return summary.get("ytd_revenue") or summary.get("revenue"), \
               summary.get("ytd_budget_revenue") or summary.get("budget_revenue")
    if line_item.lower() == "ebitda":
        return summary.get("ytd_ebitda") or summary.get("ebitda"), \
               summary.get("ytd_budget_ebitda") or summary.get("budget_ebitda")
    return None, None


# ---------------------------------------------------------------------------
# Mode 1: Budget vs Actual (MIS) — actuals for 1 metric across entities
# ---------------------------------------------------------------------------

def _build_actual(nodes: list[dict], metric: str) -> dict:
    summary_key, metric_label, y_fmt = METRICS[metric]

    labels = [n.get("name") for n in nodes]
    actuals, budgets = [], []

    has_budget = metric in ("revenue", "ebitda", "ytd_revenue", "ytd_ebitda")
    line_item = "Revenue" if "revenue" in metric else "EBITDA"

    for n in nodes:
        val = _resolve_metric(n, summary_key)
        actuals.append(_round(val))

        if has_budget:
            act_bva, bud_bva = _bva_for(n, line_item)
            budgets.append(_round(bud_bva))
        else:
            budgets.append(None)

    datasets = [{"label": "Actual", "data": actuals}]
    if has_budget and any(b is not None for b in budgets):
        datasets.append({"label": "Budget", "data": budgets})

    # Table
    cols = ["Entity", f"Actual {metric_label}"]
    if has_budget:
        cols += [f"Budget {metric_label}", "Variance", "Variance %"]
    rows = []
    for i, n in enumerate(nodes):
        a = actuals[i]
        if has_budget:
            b = budgets[i]
            var = (a - b) if (a is not None and b is not None) else None
            var_pct = round(var / b * 100, 2) if (var is not None and b) else None
            rows.append([n.get("name"), a, b, _round(var), var_pct])
        else:
            rows.append([n.get("name"), a])

    return {
        "mode": MODE_ACTUAL,
        "mode_label": "Budget vs Actual (MIS)",
        "metric": metric,
        "metric_label": metric_label,
        "entities": [_entity(n) for n in nodes],
        "chart": {
            "type": "bar",
            "title": f"{metric_label} — Budget vs Actual",
            "labels": labels,
            "datasets": datasets,
            "yFormat": y_fmt,
        },
        "table": {"columns": cols, "rows": rows},
        "summary_chips": _summary_chips(nodes),
    }


# ---------------------------------------------------------------------------
# Mode 2: Sales + Margin — revenue, GP, GP% side by side
# ---------------------------------------------------------------------------

def _build_sales_margin(nodes: list[dict]) -> dict:
    labels = [n.get("name") for n in nodes]
    revenues, gps, gp_pcts, ebitdas = [], [], [], []

    for n in nodes:
        rev = _resolve_metric(n, "revenue") or _resolve_metric(n, "ytd_revenue")
        gp = _resolve_metric(n, "gross_profit") or _resolve_metric(n, "ytd_gross_profit")
        gp_pct = _resolve_metric(n, "gp_pct")
        ebitda = _resolve_metric(n, "ebitda") or _resolve_metric(n, "ytd_ebitda")

        if gp_pct is None and rev and gp:
            gp_pct = round(gp / rev * 100, 2)

        revenues.append(_round(rev))
        gps.append(_round(gp))
        gp_pcts.append(_round(gp_pct))
        ebitdas.append(_round(ebitda))

    chart = {
        "type": "bar",
        "title": "Revenue vs Gross Profit vs EBITDA",
        "labels": labels,
        "datasets": [
            {"label": "Revenue (USD)", "data": revenues},
            {"label": "Gross Profit (USD)", "data": gps},
            {"label": "EBITDA (USD)", "data": ebitdas},
        ],
        "yFormat": "USD",
    }

    cols = ["Entity", "Revenue", "Gross Profit", "GP %", "EBITDA"]
    rows = [
        [nodes[i].get("name"), revenues[i], gps[i], gp_pcts[i], ebitdas[i]]
        for i in range(len(nodes))
    ]

    return {
        "mode": MODE_SALES_MARGIN,
        "mode_label": "Sales & Margin Analysis",
        "metric": "composite",
        "metric_label": "Sales / GP / EBITDA",
        "entities": [_entity(n) for n in nodes],
        "chart": chart,
        "table": {"columns": cols, "rows": rows},
        "summary_chips": _summary_chips(nodes),
    }


# ---------------------------------------------------------------------------
# Mode 3: Budget Variance — focus on variance & variance %
# ---------------------------------------------------------------------------

def _build_variance(nodes: list[dict], metric: str) -> dict:
    # Force into one of revenue/ebitda
    if metric not in ("revenue", "ebitda"):
        metric = "revenue"
    line_item = "Revenue" if metric == "revenue" else "EBITDA"
    metric_label = METRICS[metric][1]

    labels = [n.get("name") for n in nodes]
    variances, variance_pcts = [], []
    actuals, budgets = [], []

    for n in nodes:
        act, bud = _bva_for(n, line_item)
        actuals.append(_round(act))
        budgets.append(_round(bud))
        var = (act - bud) if (act is not None and bud is not None) else None
        var_pct = round(var / bud * 100, 2) if (var is not None and bud) else None
        variances.append(_round(var))
        variance_pcts.append(var_pct)

    chart = {
        "type": "bar",
        "title": f"{metric_label} — Budget Variance",
        "labels": labels,
        "datasets": [
            {"label": "Variance (USD)", "data": variances},
        ],
        "yFormat": "USD",
    }

    cols = ["Entity", "Actual", "Budget", "Variance", "Variance %"]
    rows = [
        [nodes[i].get("name"), actuals[i], budgets[i], variances[i], variance_pcts[i]]
        for i in range(len(nodes))
    ]

    return {
        "mode": MODE_VARIANCE,
        "mode_label": "Budget Variance",
        "metric": metric,
        "metric_label": metric_label,
        "entities": [_entity(n) for n in nodes],
        "chart": chart,
        "table": {"columns": cols, "rows": rows},
        "summary_chips": _summary_chips(nodes),
    }


# ---------------------------------------------------------------------------
# Summary chips — key headline metrics per entity
# ---------------------------------------------------------------------------

def _summary_chips(nodes: list[dict]) -> list[dict]:
    chips = []
    for n in nodes:
        s = (n.get("financials", {}) or {}).get("summary", {}) or {}
        chips.append({
            "id": n.get("id"),
            "name": n.get("name"),
            "is_real": n.get("is_real", False),
            "revenue": _round(s.get("ytd_revenue") or s.get("revenue")),
            "ebitda": _round(s.get("ytd_ebitda") or s.get("ebitda")),
            "gp_pct": _round(s.get("gp_pct")),
            "ebitda_pct": _round(s.get("ebitda_pct")),
        })
    return chips


def _round(v):
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Mode 4: KPI Table — rows=entities, columns=multiple KPIs.
# Supports two sub-modes:
#   - as_of=YYYY-MM : snapshot of that reporting month (pulls from monthly_pl
#     if the month matches; falls back to summary for the latest month)
#   - range_from=YYYY-MM & range_to=YYYY-MM : aggregates monthly_pl rows
#     between the two months inclusive, recomputing KPIs from sums
# If neither is given, defaults to the entity's summary snapshot.
# ---------------------------------------------------------------------------

def _month_in_range(period: str, lo: str, hi: str) -> bool:
    """Lexicographic compare works for YYYY-MM strings."""
    if not period:
        return False
    return lo <= period <= hi


def _kpi_from_monthly_row(row: dict) -> dict:
    """Extract one month's KPI snapshot from a monthly_pl row."""
    return {
        "revenue_mtd": row.get("revenue"),
        "gross_profit_mtd": row.get("gross_profit"),
        "ebitda_mtd": row.get("ebitda"),
        "gp_pct": row.get("gp_pct"),
        "ebitda_pct": row.get("ebitda_pct"),
    }


def _kpi_from_range(monthly_pl: list, lo: str, hi: str) -> Optional[dict]:
    """Aggregate monthly_pl between [lo, hi] inclusive. Returns None if no rows."""
    rows = [r for r in (monthly_pl or []) if _month_in_range(r.get("period", ""), lo, hi)]
    if not rows:
        return None
    rev = sum((r.get("revenue") or 0) for r in rows)
    gp = sum((r.get("gross_profit") or 0) for r in rows)
    eb = sum((r.get("ebitda") or 0) for r in rows)
    cogs = sum((r.get("cogs") or 0) for r in rows)
    opex = sum((r.get("opex") or 0) for r in rows)
    gp_pct = (gp / rev * 100) if rev else None
    eb_pct = (eb / rev * 100) if rev else None
    return {
        "revenue_range": rev,
        "gross_profit_range": gp,
        "ebitda_range": eb,
        "cogs_range": cogs,
        "opex_range": opex,
        "gp_pct": gp_pct,
        "ebitda_pct": eb_pct,
        "months_covered": len(rows),
        "range_label": f"{lo} – {hi}",
    }


def _bva_variance_pct(node: dict, line_item: str = "Revenue") -> Optional[float]:
    act, bud = _bva_for(node, line_item)
    if act is None or bud is None or not bud:
        return None
    return round((act - bud) / bud * 100, 2)


def _yoy_revenue_pct(node: dict) -> Optional[float]:
    """Compare ytd_revenue (current) to prior-year YTD by summing the same
    months of the prior calendar year from monthly_pl. Returns None if insufficient data."""
    fin = node.get("financials", {}) or {}
    summary = fin.get("summary", {}) or {}
    cur = summary.get("ytd_revenue")
    mp = fin.get("monthly_pl", []) or []
    if not cur or not mp:
        return None
    # Infer current-year months covered: pull the latest year in monthly_pl
    periods = sorted({r.get("period", "") for r in mp if r.get("period")})
    if not periods:
        return None
    latest = periods[-1]
    try:
        latest_year = int(latest.split("-")[0])
        latest_month = int(latest.split("-")[1])
    except (ValueError, IndexError):
        return None
    prev_year = latest_year - 1
    # Sum prior-year revenue for months 1..latest_month
    prev_total = 0.0
    count = 0
    for r in mp:
        p = r.get("period", "")
        try:
            y, m = p.split("-")
            if int(y) == prev_year and int(m) <= latest_month:
                prev_total += (r.get("revenue") or 0)
                count += 1
        except ValueError:
            continue
    if count == 0 or not prev_total:
        return None
    return round((cur - prev_total) / prev_total * 100, 2)


def _kpi_row_for_entity(
    node: dict,
    as_of: Optional[str],
    range_from: Optional[str],
    range_to: Optional[str],
) -> dict:
    """Produce one row of KPI values for an entity, respecting the selected mode."""
    fin = node.get("financials", {}) or {}
    summary = fin.get("summary", {}) or {}
    mp = fin.get("monthly_pl", []) or []

    if range_from and range_to:
        agg = _kpi_from_range(mp, range_from, range_to)
        if agg is None:
            return {
                "revenue": None,
                "gross_profit": None,
                "gp_pct": None,
                "ebitda": None,
                "ebitda_pct": None,
                "bva_variance_pct": None,
                "yoy_pct": None,
                "_note": "no data in range",
            }
        return {
            "revenue": agg["revenue_range"],
            "gross_profit": agg["gross_profit_range"],
            "gp_pct": agg["gp_pct"],
            "ebitda": agg["ebitda_range"],
            "ebitda_pct": agg["ebitda_pct"],
            "bva_variance_pct": _bva_variance_pct(node, "Revenue"),
            "yoy_pct": _yoy_revenue_pct(node),
            "_note": f"{agg['months_covered']} months",
        }

    if as_of:
        row = next((r for r in mp if r.get("period") == as_of), None)
        if row:
            return {
                "revenue_mtd": row.get("revenue"),
                "revenue": row.get("revenue"),
                "gross_profit": row.get("gross_profit"),
                "gp_pct": row.get("gp_pct"),
                "ebitda": row.get("ebitda"),
                "ebitda_pct": row.get("ebitda_pct"),
                "bva_variance_pct": _bva_variance_pct(node, "Revenue"),
                "yoy_pct": _yoy_revenue_pct(node),
                "_note": f"as of {as_of}",
            }
        # fall through to summary

    return {
        "revenue_mtd": summary.get("revenue"),
        "revenue_ytd": summary.get("ytd_revenue"),
        "gross_profit_ytd": summary.get("ytd_gross_profit") or summary.get("gross_profit"),
        "gp_pct": summary.get("gp_pct"),
        "ebitda_ytd": summary.get("ytd_ebitda") or summary.get("ebitda"),
        "ebitda_pct": summary.get("ebitda_pct"),
        "bva_variance_pct": _bva_variance_pct(node, "Revenue"),
        "yoy_pct": _yoy_revenue_pct(node),
        "_note": "summary snapshot",
    }


def _build_kpi_table(
    nodes: list[dict],
    as_of: Optional[str] = None,
    range_from: Optional[str] = None,
    range_to: Optional[str] = None,
) -> dict:
    use_range = bool(range_from and range_to)
    use_as_of = bool(as_of) and not use_range

    if use_range:
        cols = [
            "Entity",
            "Revenue (range)",
            "Gross Profit (range)",
            "GP %",
            "EBITDA (range)",
            "EBITDA %",
            "BvA Var % (Rev)",
            "YoY Rev %",
            "Coverage",
        ]
        sub_label = f"Range: {range_from} → {range_to}"
    elif use_as_of:
        cols = [
            "Entity",
            "Revenue (MTD)",
            "Gross Profit",
            "GP %",
            "EBITDA",
            "EBITDA %",
            "BvA Var % (Rev)",
            "YoY Rev %",
            "As of",
        ]
        sub_label = f"As of {as_of}"
    else:
        cols = [
            "Entity",
            "Revenue (MTD)",
            "Revenue (YTD)",
            "Gross Profit (YTD)",
            "GP %",
            "EBITDA (YTD)",
            "EBITDA %",
            "BvA Var % (Rev)",
            "YoY Rev %",
        ]
        sub_label = "Latest summary snapshot"

    rows = []
    labels = []
    revenue_series = []
    ebitda_series = []
    for n in nodes:
        k = _kpi_row_for_entity(n, as_of, range_from, range_to)
        name = n.get("name")
        labels.append(name)

        if use_range:
            rev = _round(k.get("revenue"))
            rows.append([
                name,
                rev,
                _round(k.get("gross_profit")),
                _round(k.get("gp_pct")),
                _round(k.get("ebitda")),
                _round(k.get("ebitda_pct")),
                k.get("bva_variance_pct"),
                k.get("yoy_pct"),
                k.get("_note"),
            ])
            revenue_series.append(rev)
            ebitda_series.append(_round(k.get("ebitda")))
        elif use_as_of:
            rev = _round(k.get("revenue"))
            rows.append([
                name,
                rev,
                _round(k.get("gross_profit")),
                _round(k.get("gp_pct")),
                _round(k.get("ebitda")),
                _round(k.get("ebitda_pct")),
                k.get("bva_variance_pct"),
                k.get("yoy_pct"),
                as_of if k.get("_note", "").startswith("as of") else "—",
            ])
            revenue_series.append(rev)
            ebitda_series.append(_round(k.get("ebitda")))
        else:
            rev_ytd = _round(k.get("revenue_ytd"))
            rows.append([
                name,
                _round(k.get("revenue_mtd")),
                rev_ytd,
                _round(k.get("gross_profit_ytd")),
                _round(k.get("gp_pct")),
                _round(k.get("ebitda_ytd")),
                _round(k.get("ebitda_pct")),
                k.get("bva_variance_pct"),
                k.get("yoy_pct"),
            ])
            revenue_series.append(rev_ytd)
            ebitda_series.append(_round(k.get("ebitda_ytd")))

    chart = {
        "type": "bar",
        "title": "Revenue vs EBITDA — " + sub_label,
        "labels": labels,
        "datasets": [
            {"label": "Revenue", "data": revenue_series},
            {"label": "EBITDA", "data": ebitda_series},
        ],
        "yFormat": "USD",
    }

    return {
        "mode": MODE_KPI_TABLE,
        "mode_label": "KPI Comparison Table",
        "sub_label": sub_label,
        "as_of": as_of,
        "range_from": range_from,
        "range_to": range_to,
        "entities": [_entity(n) for n in nodes],
        "chart": chart,
        "table": {"columns": cols, "rows": rows},
        "summary_chips": _summary_chips(nodes),
    }
