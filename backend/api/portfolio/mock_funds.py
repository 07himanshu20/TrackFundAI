"""
mock_funds.py
Generate Funds 2-5 with synthesized but realistic-looking financials.
Real Fund 1 (Healthcare & Life Sciences) is built from parsed Excel data;
these mock funds give the demo a full multi-fund hierarchy to drill through.

All mock financials are denominated in USD (the portfolio base currency)
so they roll up cleanly with the FX-converted real fund.
"""

import random
from api.portfolio.schema import empty_financials


# Reproducible randomness so every regeneration looks the same
random.seed(42)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _months_2025_jan_to(end_month: int) -> list:
    return [f"2025-{m:02d}" for m in range(1, end_month + 1)]


def _gen_monthly_pl(base_revenue: float, gp_pct: float, opex_ratio: float, n_months: int = 6) -> list:
    """Generate a sensible monthly P&L trend with mild noise + slight growth."""
    points = []
    for i, period in enumerate(_months_2025_jan_to(n_months)):
        growth = 1 + (i * 0.015)  # 1.5% MoM growth trend
        noise = random.uniform(0.88, 1.12)
        revenue = base_revenue * growth * noise
        gp = revenue * (gp_pct / 100) * random.uniform(0.95, 1.05)
        cogs = revenue - gp
        opex = revenue * opex_ratio * random.uniform(0.95, 1.05)
        ebitda = gp - opex
        points.append({
            "period": period,
            "revenue": round(revenue, 2),
            "cogs": round(cogs, 2),
            "gross_profit": round(gp, 2),
            "gp_pct": round(gp / revenue * 100, 2),
            "opex": round(opex, 2),
            "ebitda": round(ebitda, 2),
            "ebitda_pct": round(ebitda / revenue * 100, 2),
        })
    return points


def _gen_cash_flow(monthly_pl: list, opening_cash: float) -> list:
    cf = []
    cash = opening_cash
    for m in monthly_pl:
        op_cf = m["ebitda"] * random.uniform(0.7, 1.0)   # operating CF ~ 70-100% of EBITDA
        inv_cf = -m["revenue"] * random.uniform(0.02, 0.06)
        fin_cf = -m["revenue"] * random.uniform(0.005, 0.02)
        net = op_cf + inv_cf + fin_cf
        closing = cash + net
        cf.append({
            "period": m["period"],
            "opening_cash": round(cash, 2),
            "operating_cf": round(op_cf, 2),
            "investing_cf": round(inv_cf, 2),
            "financing_cf": round(fin_cf, 2),
            "net_cash_flow": round(net, 2),
            "closing_cash": round(closing, 2),
        })
        cash = closing
    return cf


def _gen_working_capital(periods: list, dso_base: float, dio_base: float, dpo_base: float, nwc_base: float) -> list:
    out = []
    for p in periods:
        dso = dso_base + random.uniform(-5, 5)
        dio = dio_base + random.uniform(-7, 7)
        dpo = dpo_base + random.uniform(-4, 4)
        out.append({
            "period": p,
            "dso": round(dso, 1),
            "dio": round(dio, 1),
            "dpo": round(dpo, 1),
            "nwc": round(nwc_base * random.uniform(0.85, 1.15), 0),
            "ccc": round(dso + dio - dpo, 1),
        })
    return out


def _build_financials(base_revenue: float, gp_pct: float, opex_ratio: float,
                       opening_cash: float, dso: float, dio: float, dpo: float, nwc: float) -> dict:
    fin = empty_financials()
    fin["monthly_pl"] = _gen_monthly_pl(base_revenue, gp_pct, opex_ratio)

    # Summary = sum of last 6 months
    rev = sum(m["revenue"] for m in fin["monthly_pl"])
    gp = sum(m["gross_profit"] for m in fin["monthly_pl"])
    cogs = sum(m["cogs"] for m in fin["monthly_pl"])
    opex = sum(m["opex"] for m in fin["monthly_pl"])
    ebitda = sum(m["ebitda"] for m in fin["monthly_pl"])

    bud_revenue = rev * random.uniform(0.95, 1.10)
    bud_ebitda = ebitda * random.uniform(0.9, 1.15)

    fin["summary"] = {
        "period": "Jun 2025 YTD",
        "revenue": round(rev, 2),
        "cogs": round(cogs, 2),
        "gross_profit": round(gp, 2),
        "gp_pct": round(gp / rev * 100, 2),
        "opex": round(opex, 2),
        "ebitda": round(ebitda, 2),
        "ebitda_pct": round(ebitda / rev * 100, 2),
        "ytd_revenue": round(rev, 2),
        "ytd_gross_profit": round(gp, 2),
        "ytd_ebitda": round(ebitda, 2),
        "ytd_budget_revenue": round(bud_revenue, 2),
        "ytd_budget_ebitda": round(bud_ebitda, 2),
    }

    fin["cost_structure"] = {
        "cogs_pct": round(cogs / rev * 100, 2),
        "gp_pct": round(gp / rev * 100, 2),
        "opex_pct": round(opex / rev * 100, 2),
        "ebitda_pct": round(ebitda / rev * 100, 2),
    }

    fin["budget_vs_actual"] = [
        {"period": "YTD", "line_item": "Revenue", "budget": round(bud_revenue, 2),
         "actual": round(rev, 2), "variance": round(rev - bud_revenue, 2),
         "variance_pct": round((rev - bud_revenue) / bud_revenue * 100, 2)},
        {"period": "YTD", "line_item": "EBITDA", "budget": round(bud_ebitda, 2),
         "actual": round(ebitda, 2), "variance": round(ebitda - bud_ebitda, 2),
         "variance_pct": round((ebitda - bud_ebitda) / bud_ebitda * 100, 2) if bud_ebitda else None},
    ]

    fin["cash_flow"] = _gen_cash_flow(fin["monthly_pl"], opening_cash)
    fin["working_capital"] = _gen_working_capital(
        [m["period"] for m in fin["monthly_pl"]], dso, dio, dpo, nwc
    )

    return fin


# ---------------------------------------------------------------------------
# Mock fund definitions
# ---------------------------------------------------------------------------

MOCK_FUNDS = [
    {
        "id_slug": "tech_growth",
        "name": "Tech Growth Fund",
        "description": "Early-stage SaaS, fintech, and AI companies (USD).",
        "currency": "USD",
        "sectors": [
            {
                "id_slug": "saas",
                "name": "SaaS",
                "segments": [
                    {
                        "id_slug": "horizontal_saas",
                        "name": "Horizontal SaaS",
                        "companies": [
                            {"name": "Cloudly Inc.", "rev": 850_000, "gp_pct": 78, "opex_ratio": 0.55,
                             "cash": 5_200_000, "dso": 45, "dio": 0, "dpo": 30, "nwc": 1_800_000},
                            {"name": "WorkflowOps", "rev": 1_200_000, "gp_pct": 82, "opex_ratio": 0.60,
                             "cash": 8_400_000, "dso": 38, "dio": 0, "dpo": 28, "nwc": 2_900_000},
                            {"name": "DataPulse", "rev": 620_000, "gp_pct": 75, "opex_ratio": 0.65,
                             "cash": 3_100_000, "dso": 52, "dio": 0, "dpo": 35, "nwc": 1_400_000},
                        ],
                    },
                    {
                        "id_slug": "vertical_saas",
                        "name": "Vertical SaaS",
                        "companies": [
                            {"name": "MediBill", "rev": 540_000, "gp_pct": 76, "opex_ratio": 0.58,
                             "cash": 2_800_000, "dso": 60, "dio": 0, "dpo": 40, "nwc": 1_200_000},
                            {"name": "RetailGrid", "rev": 720_000, "gp_pct": 72, "opex_ratio": 0.62,
                             "cash": 4_100_000, "dso": 50, "dio": 0, "dpo": 35, "nwc": 1_900_000},
                        ],
                    },
                ],
            },
            {
                "id_slug": "fintech",
                "name": "Fintech",
                "segments": [
                    {
                        "id_slug": "payments",
                        "name": "Payments",
                        "companies": [
                            {"name": "PayLink", "rev": 1_800_000, "gp_pct": 65, "opex_ratio": 0.50,
                             "cash": 12_000_000, "dso": 25, "dio": 0, "dpo": 20, "nwc": 3_500_000},
                            {"name": "QuickPay Africa", "rev": 950_000, "gp_pct": 58, "opex_ratio": 0.55,
                             "cash": 4_200_000, "dso": 30, "dio": 0, "dpo": 22, "nwc": 1_600_000},
                        ],
                    },
                    {
                        "id_slug": "lending",
                        "name": "Digital Lending",
                        "companies": [
                            {"name": "CrediFlex", "rev": 2_100_000, "gp_pct": 55, "opex_ratio": 0.45,
                             "cash": 18_000_000, "dso": 0, "dio": 0, "dpo": 0, "nwc": 8_000_000},
                        ],
                    },
                ],
            },
        ],
    },

    {
        "id_slug": "industrial",
        "name": "Industrial Innovation Fund",
        "description": "Manufacturing, robotics, and AgriTech companies (USD).",
        "currency": "USD",
        "sectors": [
            {
                "id_slug": "manufacturing",
                "name": "Manufacturing",
                "segments": [
                    {
                        "id_slug": "robotics",
                        "name": "Robotics",
                        "companies": [
                            {"name": "ArmTech Robotics", "rev": 3_200_000, "gp_pct": 42, "opex_ratio": 0.30,
                             "cash": 8_500_000, "dso": 65, "dio": 80, "dpo": 50, "nwc": 4_500_000},
                            {"name": "AutoLine Systems", "rev": 5_400_000, "gp_pct": 38, "opex_ratio": 0.28,
                             "cash": 12_000_000, "dso": 70, "dio": 90, "dpo": 55, "nwc": 7_800_000},
                        ],
                    },
                    {
                        "id_slug": "advanced_materials",
                        "name": "Advanced Materials",
                        "companies": [
                            {"name": "GrapheneCore", "rev": 1_900_000, "gp_pct": 35, "opex_ratio": 0.32,
                             "cash": 5_500_000, "dso": 60, "dio": 75, "dpo": 45, "nwc": 3_200_000},
                            {"name": "PolymerOne", "rev": 2_800_000, "gp_pct": 40, "opex_ratio": 0.30,
                             "cash": 7_200_000, "dso": 58, "dio": 85, "dpo": 50, "nwc": 4_100_000},
                        ],
                    },
                ],
            },
            {
                "id_slug": "agritech",
                "name": "AgriTech",
                "segments": [
                    {
                        "id_slug": "precision_farming",
                        "name": "Precision Farming",
                        "companies": [
                            {"name": "FarmIQ", "rev": 1_200_000, "gp_pct": 48, "opex_ratio": 0.40,
                             "cash": 3_800_000, "dso": 55, "dio": 30, "dpo": 35, "nwc": 1_900_000},
                            {"name": "GreenSensor", "rev": 850_000, "gp_pct": 52, "opex_ratio": 0.42,
                             "cash": 2_400_000, "dso": 50, "dio": 25, "dpo": 30, "nwc": 1_400_000},
                        ],
                    },
                ],
            },
        ],
    },

    {
        "id_slug": "consumer",
        "name": "Consumer Brands Fund",
        "description": "Consumer goods, D2C, and lifestyle brands (USD).",
        "currency": "USD",
        "sectors": [
            {
                "id_slug": "d2c",
                "name": "Direct-to-Consumer",
                "segments": [
                    {
                        "id_slug": "wellness",
                        "name": "Wellness",
                        "companies": [
                            {"name": "PureBrew", "rev": 2_800_000, "gp_pct": 60, "opex_ratio": 0.45,
                             "cash": 6_500_000, "dso": 5, "dio": 60, "dpo": 30, "nwc": 2_200_000},
                            {"name": "VitalDaily", "rev": 1_600_000, "gp_pct": 65, "opex_ratio": 0.50,
                             "cash": 4_200_000, "dso": 5, "dio": 55, "dpo": 28, "nwc": 1_500_000},
                        ],
                    },
                    {
                        "id_slug": "apparel",
                        "name": "Apparel",
                        "companies": [
                            {"name": "Loomly", "rev": 4_200_000, "gp_pct": 55, "opex_ratio": 0.42,
                             "cash": 9_800_000, "dso": 8, "dio": 90, "dpo": 35, "nwc": 4_500_000},
                            {"name": "Northshore", "rev": 3_100_000, "gp_pct": 58, "opex_ratio": 0.40,
                             "cash": 7_400_000, "dso": 6, "dio": 75, "dpo": 32, "nwc": 3_200_000},
                        ],
                    },
                ],
            },
            {
                "id_slug": "food_bev",
                "name": "Food & Beverage",
                "segments": [
                    {
                        "id_slug": "specialty_foods",
                        "name": "Specialty Foods",
                        "companies": [
                            {"name": "Heritage Bakehouse", "rev": 5_500_000, "gp_pct": 45, "opex_ratio": 0.32,
                             "cash": 11_000_000, "dso": 25, "dio": 35, "dpo": 40, "nwc": 4_800_000},
                            {"name": "Nordic Brews", "rev": 3_800_000, "gp_pct": 50, "opex_ratio": 0.35,
                             "cash": 8_200_000, "dso": 30, "dio": 45, "dpo": 38, "nwc": 3_900_000},
                        ],
                    },
                ],
            },
        ],
    },

    {
        "id_slug": "real_assets",
        "name": "Real Assets Fund",
        "description": "Real estate, infrastructure, and renewable energy (USD).",
        "currency": "USD",
        "sectors": [
            {
                "id_slug": "real_estate",
                "name": "Real Estate",
                "segments": [
                    {
                        "id_slug": "logistics",
                        "name": "Logistics & Warehousing",
                        "companies": [
                            {"name": "MetroLogistics REIT", "rev": 12_000_000, "gp_pct": 70, "opex_ratio": 0.20,
                             "cash": 25_000_000, "dso": 15, "dio": 0, "dpo": 30, "nwc": 8_500_000},
                        ],
                    },
                    {
                        "id_slug": "residential",
                        "name": "Residential",
                        "companies": [
                            {"name": "HavenHomes Fund", "rev": 8_400_000, "gp_pct": 65, "opex_ratio": 0.25,
                             "cash": 18_000_000, "dso": 12, "dio": 0, "dpo": 25, "nwc": 6_200_000},
                        ],
                    },
                ],
            },
            {
                "id_slug": "renewables",
                "name": "Renewables",
                "segments": [
                    {
                        "id_slug": "solar",
                        "name": "Solar",
                        "companies": [
                            {"name": "SunRise Power", "rev": 6_800_000, "gp_pct": 60, "opex_ratio": 0.22,
                             "cash": 15_000_000, "dso": 35, "dio": 60, "dpo": 45, "nwc": 5_500_000},
                            {"name": "Helios Grid", "rev": 4_500_000, "gp_pct": 55, "opex_ratio": 0.25,
                             "cash": 10_200_000, "dso": 40, "dio": 50, "dpo": 40, "nwc": 4_000_000},
                        ],
                    },
                    {
                        "id_slug": "wind",
                        "name": "Wind",
                        "companies": [
                            {"name": "Northwind Energy", "rev": 9_200_000, "gp_pct": 58, "opex_ratio": 0.20,
                             "cash": 22_000_000, "dso": 38, "dio": 70, "dpo": 50, "nwc": 7_800_000},
                        ],
                    },
                ],
            },
        ],
    },
]


def build_mock_funds() -> list:
    """
    Returns a list of fund nodes (just the company-leaf financials populated).
    The aggregator (in builder.py) will compute roll-ups for sector/segment/fund.
    """
    funds = []
    for fund_def in MOCK_FUNDS:
        fund = {
            "name": fund_def["name"],
            "id_slug": fund_def["id_slug"],
            "currency": fund_def["currency"],
            "description": fund_def["description"],
            "is_real": False,
            "sectors": [],
        }
        for sector_def in fund_def["sectors"]:
            sector = {
                "name": sector_def["name"],
                "id_slug": sector_def["id_slug"],
                "segments": [],
            }
            for segment_def in sector_def["segments"]:
                segment = {
                    "name": segment_def["name"],
                    "id_slug": segment_def["id_slug"],
                    "companies": [],
                }
                for c in segment_def["companies"]:
                    fin = _build_financials(
                        base_revenue=c["rev"],
                        gp_pct=c["gp_pct"],
                        opex_ratio=c["opex_ratio"],
                        opening_cash=c["cash"],
                        dso=c["dso"],
                        dio=c["dio"],
                        dpo=c["dpo"],
                        nwc=c["nwc"],
                    )
                    company = {
                        "name": c["name"],
                        "id_slug": c["name"].lower().replace(" ", "_").replace(".", "").replace("&", "and"),
                        "currency": fund_def["currency"],
                        "description": f"Mock portfolio company in {segment_def['name']}.",
                        "financials": fin,
                    }
                    segment["companies"].append(company)
                sector["segments"].append(segment)
            fund["sectors"].append(sector)
        funds.append(fund)
    return funds
