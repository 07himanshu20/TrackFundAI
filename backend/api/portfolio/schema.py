"""
schema.py
Defines the unified portfolio data shape: Funds -> Sectors -> Segments -> Companies.

Every node carries an `id`, `name`, `level`, `currency` (native), and a `financials`
block. Internal nodes (fund/sector/segment) hold rolled-up financials computed
from their children. Leaf nodes (company) hold the parsed/synthesized actuals.

The financials shape is uniform at every level so the comparison UI can
render the same chart/table component for fund-vs-fund, sector-vs-sector,
segment-vs-segment, or company-vs-company.

Periods are represented as ISO month strings: "YYYY-MM".
All monetary values stored in NATIVE currency at the leaf, and rolled up
in USD (using fx_rates) for internal nodes that span currencies.
"""

from typing import Literal, TypedDict, Optional


# ---------------------------------------------------------------------------
# Hierarchy levels
# ---------------------------------------------------------------------------

LEVEL_FUND = "fund"
LEVEL_SECTOR = "sector"
LEVEL_SEGMENT = "segment"
LEVEL_COMPANY = "company"

LEVELS = [LEVEL_FUND, LEVEL_SECTOR, LEVEL_SEGMENT, LEVEL_COMPANY]

CHILD_OF = {
    LEVEL_FUND: LEVEL_SECTOR,
    LEVEL_SECTOR: LEVEL_SEGMENT,
    LEVEL_SEGMENT: LEVEL_COMPANY,
    LEVEL_COMPANY: None,
}


# ---------------------------------------------------------------------------
# Financials block (uniform at every level)
# ---------------------------------------------------------------------------

class MonthlyPLPoint(TypedDict, total=False):
    period: str           # "YYYY-MM"
    revenue: float
    cogs: float
    gross_profit: float
    gp_pct: float
    opex: float
    ebitda: float
    ebitda_pct: float


class CashFlowPoint(TypedDict, total=False):
    period: str
    opening_cash: float
    operating_cf: float
    investing_cf: float
    financing_cf: float
    net_cash_flow: float
    closing_cash: float


class WorkingCapitalPoint(TypedDict, total=False):
    period: str
    dso: float            # days sales outstanding
    dio: float            # days inventory outstanding
    dpo: float            # days payable outstanding
    nwc: float            # net working capital
    ccc: float            # cash conversion cycle = DSO + DIO - DPO


class SalesBreakdownPoint(TypedDict, total=False):
    label: str            # segment / brand / country
    revenue: float
    gross_margin: float
    gm_pct: float


class BudgetVsActualPoint(TypedDict, total=False):
    period: str           # "YYYY-MM" or "YTD"
    line_item: str        # "Revenue", "COGS", "GP", "OPEX", "EBITDA"
    budget: float
    actual: float
    variance: float
    variance_pct: float


class Financials(TypedDict, total=False):
    # Top-level KPI snapshot (current period)
    summary: dict          # {revenue, cogs, gp, gp_pct, opex, ebitda, ebitda_pct, ...}

    # Time series
    monthly_pl: list       # list[MonthlyPLPoint]
    cash_flow: list        # list[CashFlowPoint]
    working_capital: list  # list[WorkingCapitalPoint]

    # Breakdowns
    sales_by_segment: list # list[SalesBreakdownPoint]
    sales_by_geo: list     # list[SalesBreakdownPoint]

    # Budget comparison
    budget_vs_actual: list # list[BudgetVsActualPoint]

    # Cost structure (current period, single point)
    cost_structure: dict   # {cogs_pct, gp_pct, opex_pct, ebitda_pct}


# ---------------------------------------------------------------------------
# Node shape
# ---------------------------------------------------------------------------

class PortfolioNode(TypedDict, total=False):
    id: str                # stable slug, e.g. "fund_healthcare", "company_analisa"
    name: str              # display name
    level: str             # one of LEVELS
    parent_id: Optional[str]
    currency: str          # ISO 4217 native currency for this node's financials
    is_real: bool          # True if backed by parsed Excel data, False if mocked
    description: Optional[str]
    financials: Financials
    children: list         # list[PortfolioNode]   (empty for company leaf)


# ---------------------------------------------------------------------------
# Top-level portfolio document
# ---------------------------------------------------------------------------

class PortfolioDocument(TypedDict, total=False):
    schema_version: str
    base_currency: str             # "USD"
    fx_as_of: str                  # "YYYY-MM-DD"
    fx_rates: dict                 # {ccy: rate_per_usd}
    generated_at: str              # ISO timestamp
    period_range: dict             # {start: "YYYY-MM", end: "YYYY-MM"}
    funds: list                    # list[PortfolioNode]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_id(level: str, slug: str, parent_id: Optional[str] = None) -> str:
    """Build a stable hierarchical id like 'fund_hc::sector_dist::segment_lab::company_analisa'."""
    if parent_id:
        return f"{parent_id}::{level}_{slug}"
    return f"{level}_{slug}"


def empty_financials() -> Financials:
    return {
        "summary": {},
        "monthly_pl": [],
        "cash_flow": [],
        "working_capital": [],
        "sales_by_segment": [],
        "sales_by_geo": [],
        "budget_vs_actual": [],
        "cost_structure": {},
    }
