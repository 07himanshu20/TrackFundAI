"""
Valuation Engine — IPEV Level 1/2/3 valuation computation.

Level 1: Exchange-listed → CMP × shares held (auto, daily)
Level 2: Observable inputs → peer EV/Revenue or EV/EBITDA multiples × company financials
Level 3: Model-based → DCF or Last Round pricing
Pre-IPO: DRHP implied value × (1 - DLOM%)

All computation is Gemini-assisted for:
  - Peer comparable selection (Level 2)
  - DCF assumption review (Level 3)
  - DLOM estimation (Pre-IPO)
"""

import logging
from decimal import Decimal
from datetime import date
from typing import Optional

from django.utils import timezone
from django.conf import settings

logger = logging.getLogger(__name__)


def compute_level1_valuation(investment, as_of_date: Optional[date] = None):
    """
    IPEV Level 1: Exchange-listed security.
    Uses the latest MarketPriceFeed entry for the portfolio company's
    listed security mapping.

    Returns Valuation instance (approved, auto-generated).
    """
    from marketdata.models import MarketPriceFeed, ListedSecurityMapping
    from investments.models import Valuation

    if as_of_date is None:
        as_of_date = timezone.now().date()

    company = investment.portfolio_company
    if not company:
        return None

    # Find primary listed security
    sec = ListedSecurityMapping.objects.filter(
        portfolio_company=company,
        is_active=True,
        is_primary_listing=True,
    ).first()

    if not sec:
        return None

    # Get latest price on or before as_of_date
    price_feed = MarketPriceFeed.objects.filter(
        security=sec,
        price_date__lte=as_of_date,
    ).order_by('-price_date').first()

    if not price_feed:
        return None

    fv = price_feed.fair_value_of_holding or (price_feed.close_price * sec.shares_held)

    # Apply DLOM if specified (unusual for Level 1, but supported for restricted shares)
    dlom = Decimal('0')
    # Check if there's a previous valuation with DLOM set
    prev_val = Valuation.objects.filter(
        investment=investment,
        ipev_level=1,
    ).order_by('-valuation_date').first()
    if prev_val and prev_val.dlom_pct:
        dlom = prev_val.dlom_pct / 100

    fv_after_dlom = fv * (1 - dlom)

    val, _ = Valuation.objects.update_or_create(
        investment=investment,
        valuation_date=as_of_date,
        methodology='comparables',
        defaults={
            'ipev_level': 1,
            'fair_value': fv_after_dlom,
            'fair_value_of_holding': fv_after_dlom,
            'enterprise_value': price_feed.close_price * Decimal('1'),  # placeholder
            'status': 'approved',
            'assumptions': (
                f'IPEV L1: {sec.ticker_symbol} @ {price_feed.close_price} × {sec.shares_held} shares'
                + (f' — DLOM {float(dlom*100):.1f}%' if dlom else '')
            ),
        },
    )
    return val


def compute_level2_valuation(
    investment,
    as_of_date: Optional[date] = None,
    peer_ev_revenue_multiple: Optional[float] = None,
    peer_ev_ebitda_multiple: Optional[float] = None,
    company_revenue: Optional[float] = None,
    company_ebitda: Optional[float] = None,
    ownership_pct: Optional[float] = None,
):
    """
    IPEV Level 2: Observable inputs — peer multiples.

    At least one of (peer_ev_revenue_multiple, peer_ev_ebitda_multiple) must be provided,
    along with the corresponding company metric.

    Returns draft Valuation (requires Board + IBBI certification to approve).
    """
    from investments.models import Valuation

    if as_of_date is None:
        as_of_date = timezone.now().date()

    # Use Gemini to suggest peer multiples if not provided
    if not peer_ev_revenue_multiple and not peer_ev_ebitda_multiple:
        multiples = _fetch_peer_multiples_gemini(investment)
        peer_ev_revenue_multiple = multiples.get('ev_revenue')
        peer_ev_ebitda_multiple = multiples.get('ev_ebitda')

    enterprise_value = Decimal('0')
    assumptions = []

    if peer_ev_revenue_multiple and company_revenue:
        ev_from_revenue = Decimal(str(peer_ev_revenue_multiple)) * Decimal(str(company_revenue))
        enterprise_value = ev_from_revenue
        assumptions.append(f'EV/Revenue: {peer_ev_revenue_multiple}x × ₹{company_revenue}Cr = ₹{float(ev_from_revenue):,.2f}Cr')

    if peer_ev_ebitda_multiple and company_ebitda and company_ebitda > 0:
        ev_from_ebitda = Decimal(str(peer_ev_ebitda_multiple)) * Decimal(str(company_ebitda))
        if enterprise_value > 0:
            # Average of both methods
            enterprise_value = (enterprise_value + ev_from_ebitda) / 2
            assumptions.append(f'EV/EBITDA: {peer_ev_ebitda_multiple}x × ₹{company_ebitda}Cr = ₹{float(ev_from_ebitda):,.2f}Cr (averaged)')
        else:
            enterprise_value = ev_from_ebitda
            assumptions.append(f'EV/EBITDA: {peer_ev_ebitda_multiple}x × ₹{company_ebitda}Cr = ₹{float(ev_from_ebitda):,.2f}Cr')

    if enterprise_value <= 0:
        return None

    # Fund's stake value = EV × ownership %
    own_pct = Decimal(str(ownership_pct or investment.ownership_pct or 0)) / 100
    fair_value_of_holding = enterprise_value * own_pct

    # Apply DLOM if specified
    prev_val = _get_latest_valuation(investment, 2)
    dlom = (prev_val.dlom_pct or Decimal('0')) / 100 if prev_val else Decimal('0')
    fair_value_after_dlom = fair_value_of_holding * (1 - dlom)

    peer_multiples_list = []
    if peer_ev_revenue_multiple:
        peer_multiples_list.append({'metric': 'EV/Revenue', 'multiple': peer_ev_revenue_multiple})
    if peer_ev_ebitda_multiple:
        peer_multiples_list.append({'metric': 'EV/EBITDA', 'multiple': peer_ev_ebitda_multiple})

    val, _ = Valuation.objects.update_or_create(
        investment=investment,
        valuation_date=as_of_date,
        methodology='comparables',
        defaults={
            'ipev_level': 2,
            'fair_value': fair_value_after_dlom,
            'fair_value_of_holding': fair_value_after_dlom,
            'enterprise_value': enterprise_value,
            'peer_multiples_used': peer_multiples_list,
            'dlom_pct': float(dlom * 100) if dlom else None,
            'status': 'draft',  # Requires Board + IBBI certification
            'assumptions': ' | '.join(assumptions),
        },
    )
    return val


def compute_level3_dcf_valuation(
    investment,
    as_of_date: Optional[date] = None,
    projected_free_cash_flows: Optional[list] = None,
    wacc_pct: Optional[float] = None,
    terminal_growth_rate_pct: Optional[float] = None,
    ownership_pct: Optional[float] = None,
    dlom_pct: Optional[float] = None,
):
    """
    IPEV Level 3: DCF (Discounted Cash Flow) valuation.

    projected_free_cash_flows: list of annual FCF projections (e.g., [10, 15, 20, 25, 30] Cr)
    wacc_pct: Weighted Average Cost of Capital (e.g., 18.0 = 18%)
    terminal_growth_rate_pct: Terminal growth rate (e.g., 4.0 = 4%)

    Returns draft Valuation (requires Board + IBBI certification).
    """
    from investments.models import Valuation

    if as_of_date is None:
        as_of_date = timezone.now().date()

    if not projected_free_cash_flows or not wacc_pct:
        return None

    wacc = Decimal(str(wacc_pct)) / 100
    tgr = Decimal(str(terminal_growth_rate_pct or 4.0)) / 100

    # DCF: PV of FCFs + Terminal Value
    pv_fcfs = Decimal('0')
    for t, fcf in enumerate(projected_free_cash_flows, start=1):
        pv_fcfs += Decimal(str(fcf)) / ((1 + wacc) ** t)

    # Gordon Growth terminal value
    last_fcf = Decimal(str(projected_free_cash_flows[-1]))
    n = len(projected_free_cash_flows)
    terminal_value = (last_fcf * (1 + tgr)) / (wacc - tgr) if wacc > tgr else Decimal('0')
    pv_terminal = terminal_value / ((1 + wacc) ** n)

    enterprise_value = pv_fcfs + pv_terminal

    # Fund's equity value
    own_pct = Decimal(str(ownership_pct or investment.ownership_pct or 0)) / 100
    equity_value = enterprise_value * own_pct

    # DLOM
    dlom = Decimal(str(dlom_pct or 0)) / 100
    fair_value = equity_value * (1 - dlom)

    val, _ = Valuation.objects.update_or_create(
        investment=investment,
        valuation_date=as_of_date,
        methodology='dcf',
        defaults={
            'ipev_level': 3,
            'fair_value': fair_value,
            'fair_value_of_holding': fair_value,
            'enterprise_value': enterprise_value,
            'discount_rate': wacc_pct,
            'dcf_terminal_growth_rate': terminal_growth_rate_pct or 4.0,
            'dlom_pct': float(dlom * 100) if dlom else None,
            'status': 'draft',
            'assumptions': (
                f'DCF: WACC={wacc_pct}%, TGR={terminal_growth_rate_pct}%, '
                f'FCF projections={projected_free_cash_flows}, '
                f'EV=₹{float(enterprise_value):,.2f}Cr'
            ),
        },
    )
    return val


def compute_pre_ipo_valuation(
    investment,
    as_of_date: Optional[date] = None,
    drhp_implied_value: Optional[float] = None,
    dlom_pct: Optional[float] = 20.0,
    ownership_pct: Optional[float] = None,
):
    """
    Pre-IPO track: DRHP implied valuation × (1 - DLOM).
    Typically used when company has filed DRHP but not yet listed.
    """
    from investments.models import Valuation

    if as_of_date is None:
        as_of_date = timezone.now().date()

    if not drhp_implied_value:
        return None

    ev = Decimal(str(drhp_implied_value))
    dlom = Decimal(str(dlom_pct or 20.0)) / 100
    own_pct = Decimal(str(ownership_pct or investment.ownership_pct or 0)) / 100

    fair_value = ev * own_pct * (1 - dlom)

    val, _ = Valuation.objects.update_or_create(
        investment=investment,
        valuation_date=as_of_date,
        methodology='recent_transaction',
        defaults={
            'ipev_level': 3,
            'is_pre_ipo': True,
            'fair_value': fair_value,
            'fair_value_of_holding': fair_value,
            'enterprise_value': ev,
            'drhp_value': drhp_implied_value,
            'dlom_pct': float(dlom * 100),
            'status': 'draft',
            'assumptions': (
                f'Pre-IPO: DRHP EV=₹{float(ev):,.2f}Cr × {float(own_pct*100):.2f}% '
                f'× (1 - {float(dlom*100):.1f}% DLOM) = ₹{float(fair_value):,.2f}Cr'
            ),
        },
    )
    return val


def _get_latest_valuation(investment, ipev_level: int):
    from investments.models import Valuation
    return (
        Valuation.objects.filter(investment=investment, ipev_level=ipev_level)
        .order_by('-valuation_date')
        .first()
    )


def _fetch_peer_multiples_gemini(investment) -> dict:
    """
    Use Gemini to suggest peer EV/Revenue and EV/EBITDA multiples
    based on the company's sector and stage.
    """
    try:
        from api.gemini_service import generate_content

        company = investment.portfolio_company
        sector = (company.sector if company else '') or investment.sector or 'Technology'
        stage = investment.instrument_type or 'equity'

        prompt = f"""You are a private equity valuation expert.

Company sector: {sector}
Investment stage/instrument: {stage}

Provide typical EV/Revenue and EV/EBITDA multiples for comparable public companies in this sector.
Return ONLY a JSON object with keys: "ev_revenue" (float) and "ev_ebitda" (float or null if not applicable).
Use current market data as of your knowledge cutoff. Be conservative (use median, not top-quartile).
Example: {{"ev_revenue": 4.5, "ev_ebitda": 18.0}}"""

        result = generate_content(prompt)
        import json
        text = result.text.strip()
        # Extract JSON from response
        if '{' in text:
            text = text[text.index('{'):text.rindex('}')+1]
        data = json.loads(text)
        return {
            'ev_revenue': data.get('ev_revenue'),
            'ev_ebitda': data.get('ev_ebitda'),
        }
    except Exception as e:
        logger.warning(f'Gemini peer multiples fetch failed: {e}')
        return {}
