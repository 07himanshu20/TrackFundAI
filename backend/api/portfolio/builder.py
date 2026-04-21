"""
builder.py
==========
Top-level assembler: builds the unified PortfolioDocument.

New architecture (config-driven):
  1. Read COMPANY_REGISTRY from company_config.py
  2. For each company, parse its Excel file:
       - use_legacy_parser=False → generic_parser (Gemini-powered)
       - use_legacy_parser=True  → old hardcoded parser (kept as fallback)
  3. Assemble the fund/sector/segment/company hierarchy from the registry
  4. Compute roll-ups (USD-converted summaries) for every internal node
  5. Append mock funds
  6. Return a complete PortfolioDocument dict

Adding a new company: edit company_config.py only. No code changes here.
"""

import os
import json
import importlib
import logging
from datetime import datetime, timezone
from typing import Optional

from api.portfolio.schema import (
    LEVEL_FUND, LEVEL_SECTOR, LEVEL_SEGMENT, LEVEL_COMPANY,
    empty_financials, make_id,
)
from api.portfolio.fx_rates import FX_RATES_PER_USD, FX_AS_OF_DATE, to_usd
from api.portfolio.mock_funds import build_mock_funds
from api.portfolio.company_config import COMPANY_REGISTRY, get_fund_hierarchy
from api.portfolio import generic_parser

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Roll-up aggregation (unchanged from original)
# ---------------------------------------------------------------------------

def _convert_summary_to_usd(summary: dict, currency: str) -> dict:
    if not summary or currency.upper() == "USD":
        return dict(summary or {})
    converted = {}
    for k, v in (summary or {}).items():
        if isinstance(v, (int, float)) and not k.endswith("_pct") and k not in ("period",):
            converted[k] = to_usd(v, currency)
        else:
            converted[k] = v
    return converted


def _aggregate_summaries(child_nodes: list) -> dict:
    parent = {}
    fields_to_sum = [
        "revenue", "cogs", "gross_profit", "opex", "ebitda",
        "ytd_revenue", "ytd_gross_profit", "ytd_ebitda",
        "ytd_budget_revenue", "ytd_budget_ebitda",
        "budget_revenue", "budget_ebitda",
    ]
    for f in fields_to_sum:
        vals = [(c.get("financials", {}).get("summary") or {}).get(f) for c in child_nodes]
        vals = [v for v in vals if isinstance(v, (int, float))]
        if vals:
            parent[f] = round(sum(vals), 2)

    rev = parent.get("revenue")
    if rev:
        gp = parent.get("gross_profit")
        ebitda = parent.get("ebitda")
        if gp is not None:
            parent["gp_pct"] = round(gp / rev * 100, 2)
        if ebitda is not None:
            parent["ebitda_pct"] = round(ebitda / rev * 100, 2)

    parent["period"] = "Latest"
    return parent


def _aggregate_monthly_pl(child_nodes: list) -> list:
    by_period = {}
    for c in child_nodes:
        for pt in c.get("financials", {}).get("monthly_pl", []) or []:
            period = pt.get("period")
            if not period:
                continue
            agg = by_period.setdefault(period, {"period": period, "revenue": 0, "cogs": 0,
                                                  "gross_profit": 0, "opex": 0, "ebitda": 0})
            for f in ("revenue", "cogs", "gross_profit", "opex", "ebitda"):
                v = pt.get(f)
                if isinstance(v, (int, float)):
                    agg[f] += v
    out = []
    for period in sorted(by_period.keys()):
        agg = by_period[period]
        rev = agg["revenue"]
        if rev:
            agg["gp_pct"] = round(agg["gross_profit"] / rev * 100, 2)
            agg["ebitda_pct"] = round(agg["ebitda"] / rev * 100, 2)
        for f in ("revenue", "cogs", "gross_profit", "opex", "ebitda"):
            agg[f] = round(agg[f], 2)
        out.append(agg)
    return out


def _aggregate_cash_flow(child_nodes: list) -> list:
    by_period = {}
    for c in child_nodes:
        for pt in c.get("financials", {}).get("cash_flow", []) or []:
            period = pt.get("period")
            if not period:
                continue
            agg = by_period.setdefault(period, {"period": period, "opening_cash": 0,
                                                 "operating_cf": 0, "investing_cf": 0,
                                                 "financing_cf": 0, "net_cash_flow": 0,
                                                 "closing_cash": 0})
            for f in ("opening_cash", "operating_cf", "investing_cf", "financing_cf",
                      "net_cash_flow", "closing_cash"):
                v = pt.get(f)
                if isinstance(v, (int, float)):
                    agg[f] += v
    return [by_period[p] for p in sorted(by_period.keys())]


def _convert_node_to_usd(node: dict, currency: str) -> dict:
    fin = node.get("financials", {})
    if currency.upper() == "USD":
        return dict(fin)

    out = {
        "summary": _convert_summary_to_usd(fin.get("summary", {}), currency),
        "monthly_pl": [],
        "cash_flow": [],
        "working_capital": list(fin.get("working_capital", []) or []),
        "sales_by_segment": [],
        "sales_by_geo": [],
        "budget_vs_actual": [],
        "cost_structure": dict(fin.get("cost_structure", {})),
    }

    for pt in fin.get("monthly_pl", []) or []:
        new_pt = dict(pt)
        for f in ("revenue", "cogs", "gross_profit", "opex", "ebitda"):
            v = new_pt.get(f)
            if isinstance(v, (int, float)):
                new_pt[f] = to_usd(v, currency)
        out["monthly_pl"].append(new_pt)

    for pt in fin.get("cash_flow", []) or []:
        new_pt = dict(pt)
        for f in ("opening_cash", "operating_cf", "investing_cf", "financing_cf",
                  "net_cash_flow", "closing_cash"):
            v = new_pt.get(f)
            if isinstance(v, (int, float)):
                new_pt[f] = to_usd(v, currency)
        out["cash_flow"].append(new_pt)

    for src_key in ("sales_by_segment", "sales_by_geo"):
        for pt in fin.get(src_key, []) or []:
            new_pt = dict(pt)
            for f in ("revenue", "gross_margin"):
                v = new_pt.get(f)
                if isinstance(v, (int, float)):
                    new_pt[f] = to_usd(v, currency)
            out[src_key].append(new_pt)

    for pt in fin.get("budget_vs_actual", []) or []:
        new_pt = dict(pt)
        for f in ("budget", "actual", "variance"):
            v = new_pt.get(f)
            if isinstance(v, (int, float)):
                new_pt[f] = to_usd(v, currency)
        out["budget_vs_actual"].append(new_pt)

    return out


# ---------------------------------------------------------------------------
# Node construction
# ---------------------------------------------------------------------------

def _make_company_node(parent_id: str, name: str, slug: str, currency: str,
                        financials: dict, is_real: bool, description: Optional[str] = None) -> dict:
    usd_fin = _convert_node_to_usd({"financials": financials}, currency)
    return {
        "id": make_id(LEVEL_COMPANY, slug, parent_id),
        "name": name,
        "level": LEVEL_COMPANY,
        "parent_id": parent_id,
        "currency": "USD",
        "native_currency": currency,
        "is_real": is_real,
        "description": description,
        "financials": usd_fin,
        "children": [],
    }


def _make_internal_node(parent_id: Optional[str], level: str, name: str, slug: str,
                         children: list, is_real: bool, description: Optional[str] = None) -> dict:
    nid = make_id(level, slug, parent_id)
    fin = {
        "summary": _aggregate_summaries(children),
        "monthly_pl": _aggregate_monthly_pl(children),
        "cash_flow": _aggregate_cash_flow(children),
        "working_capital": [],
        "sales_by_segment": [],
        "sales_by_geo": [],
        "budget_vs_actual": [],
        "cost_structure": {},
    }

    summary = fin["summary"]
    rev = summary.get("revenue")
    if rev:
        cogs = summary.get("cogs")
        gp = summary.get("gross_profit")
        opex = summary.get("opex")
        ebitda = summary.get("ebitda")
        fin["cost_structure"] = {
            "cogs_pct": round((cogs or 0) / rev * 100, 2) if cogs is not None else None,
            "gp_pct": round((gp or 0) / rev * 100, 2) if gp is not None else None,
            "opex_pct": round((opex or 0) / rev * 100, 2) if opex is not None else None,
            "ebitda_pct": round((ebitda or 0) / rev * 100, 2) if ebitda is not None else None,
        }

    bva_by_li = {}
    for c in children:
        for r in c.get("financials", {}).get("budget_vs_actual", []) or []:
            li = r.get("line_item")
            if not li:
                continue
            agg = bva_by_li.setdefault(li, {"line_item": li, "period": "YTD",
                                              "budget": 0, "actual": 0})
            for f in ("budget", "actual"):
                v = r.get(f)
                if isinstance(v, (int, float)):
                    agg[f] += v
    for agg in bva_by_li.values():
        agg["variance"] = round(agg["actual"] - agg["budget"], 2)
        agg["variance_pct"] = round(agg["variance"] / agg["budget"] * 100, 2) if agg["budget"] else None
        for f in ("budget", "actual"):
            agg[f] = round(agg[f], 2)
        fin["budget_vs_actual"].append(agg)

    for c in children:
        c["parent_id"] = nid

    return {
        "id": nid,
        "name": name,
        "level": level,
        "parent_id": parent_id,
        "currency": "USD",
        "is_real": is_real,
        "description": description,
        "financials": fin,
        "children": children,
    }


# ---------------------------------------------------------------------------
# Parse a single company using either Gemini or legacy parser
# ---------------------------------------------------------------------------

def _parse_company(company_cfg: dict) -> dict:
    """
    Parse one company's Excel file(s) and return a parsed result dict.
    Returns {"company_meta", "financials", "segments"} as expected by builder.
    """
    name = company_cfg["name"]
    slug = company_cfg["slug"]
    currency = company_cfg["currency"]
    files = company_cfg.get("files", [])
    hints = company_cfg.get("hints", {})
    use_legacy = company_cfg.get("use_legacy_parser", False)

    primary_file = next(
        (f["path"] for f in files if f.get("role") == "primary"),
        files[0]["path"] if files else None,
    )

    if not primary_file:
        raise ValueError(f"No file configured for company '{name}'")

    if use_legacy:
        # Use the old hardcoded parser
        mod_name = company_cfg.get("legacy_parser_module")
        kwargs = company_cfg.get("legacy_parser_kwargs", {})
        logger.info("Using legacy parser '%s' for '%s'", mod_name, name)
        mod = importlib.import_module(mod_name)
        return mod.parse(primary_file, **kwargs)
    else:
        # Use Gemini generic parser
        logger.info("Using Gemini generic parser for '%s' (file: %s)", name, primary_file)
        return generic_parser.parse(
            filepath=primary_file,
            company_name=name,
            company_slug=slug,
            currency=currency,
            scale=hints.get("scale"),
            reporting_period=hints.get("reporting_period"),
            description=company_cfg.get("description"),
            extra_hints={k: v for k, v in hints.items() if k not in ("scale", "reporting_period")},
        )


# ---------------------------------------------------------------------------
# Build the real fund(s) from company_config registry
# ---------------------------------------------------------------------------

def _build_real_funds() -> list[dict]:
    """
    Build all real fund nodes from COMPANY_REGISTRY.
    Groups companies into their fund/sector/segment hierarchy.
    """
    hierarchy = get_fund_hierarchy()
    fund_nodes = []

    for fund_slug, fund_data in hierarchy.items():
        fund_id = make_id(LEVEL_FUND, fund_slug)
        sector_nodes = []

        for sector_slug, sector_data in fund_data["sectors"].items():
            sector_id = make_id(LEVEL_SECTOR, sector_slug, fund_id)
            segment_nodes = []

            for segment_slug, segment_data in sector_data["segments"].items():
                seg_id = make_id(LEVEL_SEGMENT, segment_slug, sector_id)
                company_nodes = []

                for company_cfg in segment_data["companies"]:
                    try:
                        parsed = _parse_company(company_cfg)
                        node = _make_company_node(
                            parent_id=seg_id,
                            name=parsed["company_meta"]["name"],
                            slug=parsed["company_meta"]["slug"],
                            currency=parsed["company_meta"]["currency"],
                            financials=parsed["financials"],
                            is_real=company_cfg.get("is_real", True),
                            description=parsed["company_meta"].get("description"),
                        )
                        company_nodes.append(node)
                        logger.info("Parsed company: %s", company_cfg["name"])
                    except Exception as e:
                        logger.error(
                            "Failed to parse company '%s': %s",
                            company_cfg["name"], e, exc_info=True
                        )
                        # Don't skip — raise so the user knows immediately
                        raise

                if company_nodes:
                    seg_node = _make_internal_node(
                        parent_id=sector_id,
                        level=LEVEL_SEGMENT,
                        name=segment_data["name"],
                        slug=segment_slug,
                        children=company_nodes,
                        is_real=True,
                        description=segment_data.get("description"),
                    )
                    segment_nodes.append(seg_node)

            if segment_nodes:
                sec_node = _make_internal_node(
                    parent_id=fund_id,
                    level=LEVEL_SECTOR,
                    name=sector_data["name"],
                    slug=sector_slug,
                    children=segment_nodes,
                    is_real=True,
                    description=sector_data.get("description"),
                )
                sector_nodes.append(sec_node)

        if sector_nodes:
            fund_node = _make_internal_node(
                parent_id=None,
                level=LEVEL_FUND,
                name=fund_data["name"],
                slug=fund_slug,
                children=sector_nodes,
                is_real=True,
                description=fund_data.get("description", ""),
            )
            fund_nodes.append(fund_node)

    return fund_nodes


# ---------------------------------------------------------------------------
# Mock fund builder (unchanged)
# ---------------------------------------------------------------------------

def _build_mock_fund_node(fund_def: dict) -> dict:
    fund_id = make_id(LEVEL_FUND, fund_def["id_slug"])
    sectors = []
    for sec in fund_def["sectors"]:
        sec_id = make_id(LEVEL_SECTOR, sec["id_slug"], fund_id)
        segments = []
        for seg in sec["segments"]:
            seg_id = make_id(LEVEL_SEGMENT, seg["id_slug"], sec_id)
            companies = []
            for c in seg["companies"]:
                companies.append(_make_company_node(
                    parent_id=seg_id, name=c["name"], slug=c["id_slug"],
                    currency=c["currency"], financials=c["financials"],
                    is_real=False, description=c.get("description"),
                ))
            segments.append(_make_internal_node(
                parent_id=sec_id, level=LEVEL_SEGMENT,
                name=seg["name"], slug=seg["id_slug"],
                children=companies, is_real=False,
            ))
        sectors.append(_make_internal_node(
            parent_id=fund_id, level=LEVEL_SECTOR,
            name=sec["name"], slug=sec["id_slug"],
            children=segments, is_real=False,
        ))
    return _make_internal_node(
        parent_id=None, level=LEVEL_FUND,
        name=fund_def["name"], slug=fund_def["id_slug"],
        children=sectors, is_real=False,
        description=fund_def.get("description"),
    )


# ---------------------------------------------------------------------------
# Determine earliest and latest periods across all real companies
# ---------------------------------------------------------------------------

def _get_period_range(funds: list) -> dict:
    all_periods = []

    def _collect(node):
        for pt in (node.get("financials", {}).get("monthly_pl") or []):
            p = pt.get("period")
            if p:
                all_periods.append(p)
        for child in node.get("children", []):
            _collect(child)

    for fund in funds:
        _collect(fund)

    if not all_periods:
        return {"start": "2025-01", "end": "2025-06"}

    all_periods.sort()
    return {"start": all_periods[0], "end": all_periods[-1]}


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------

def build_portfolio() -> dict:
    """Return a complete PortfolioDocument."""
    funds = []

    # Real funds from company_config registry
    real_funds = _build_real_funds()
    funds.extend(real_funds)

    # Mock funds
    mock_defs = build_mock_funds()
    for fd in mock_defs:
        funds.append(_build_mock_fund_node(fd))

    period_range = _get_period_range(real_funds)

    doc = {
        "schema_version": "2.0",
        "base_currency": "USD",
        "fx_as_of": FX_AS_OF_DATE,
        "fx_rates": FX_RATES_PER_USD,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_range": period_range,
        "funds": funds,
    }
    return doc


def save_portfolio(out_path: str) -> dict:
    doc = build_portfolio()
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=2, default=str)
    logger.info(
        "Portfolio saved to %s (%d funds, period %s → %s)",
        out_path,
        len(doc["funds"]),
        doc["period_range"]["start"],
        doc["period_range"]["end"],
    )
    return doc


def save_portfolio_to_db(doc: Optional[dict] = None) -> dict:
    """
    Build (or accept) a PortfolioDocument and write it to the database.
    Replaces the active snapshot with a new one.
    Also invalidates the in-memory cache in service.py so the next
    request reads fresh data from DB.
    """
    from portfolio.models import PortfolioSnapshot, PortfolioNode

    if doc is None:
        doc = build_portfolio()

    # Deactivate existing snapshots
    PortfolioSnapshot.objects.filter(is_active=True).update(is_active=False)

    snapshot = PortfolioSnapshot.objects.create(
        schema_version=doc.get('schema_version', '2.0'),
        base_currency=doc.get('base_currency', 'USD'),
        fx_as_of=doc.get('fx_as_of', ''),
        fx_rates=doc.get('fx_rates', {}),
        period_range=doc.get('period_range', {}),
        source='excel_parse',
        is_active=True,
    )

    node_count = 0
    db_nodes = {}  # node_id -> PortfolioNode instance

    def _save_node(node_data, parent_node_id, sort_order):
        nonlocal node_count
        node_id = node_data.get('id', '')
        if not node_id:
            return

        db_node = PortfolioNode.objects.create(
            snapshot=snapshot,
            node_id=node_id,
            name=node_data.get('name', ''),
            level=node_data.get('level', 'company'),
            parent_node_id=parent_node_id,
            currency=node_data.get('currency', 'USD'),
            native_currency=node_data.get('native_currency'),
            is_real=node_data.get('is_real', False),
            description=node_data.get('description'),
            financials=node_data.get('financials', {}),
            sort_order=sort_order,
        )
        db_nodes[node_id] = db_node
        node_count += 1

        for idx, child in enumerate(node_data.get('children', []) or []):
            _save_node(child, node_id, idx)

    for idx, fund in enumerate(doc.get('funds', [])):
        _save_node(fund, None, idx)

    # Second pass: set parent FK
    for nid, db_node in db_nodes.items():
        if db_node.parent_node_id and db_node.parent_node_id in db_nodes:
            db_node.parent = db_nodes[db_node.parent_node_id]
            db_node.save(update_fields=['parent'])

    # Invalidate service.py cache so next request loads from DB
    try:
        from api.portfolio import service as portfolio_service
        portfolio_service.reload()
    except Exception:
        pass

    logger.info(
        "Portfolio saved to DB (%d nodes, %d funds, period %s → %s)",
        node_count,
        len(doc.get("funds", [])),
        doc.get("period_range", {}).get("start", "?"),
        doc.get("period_range", {}).get("end", "?"),
    )
    return doc
