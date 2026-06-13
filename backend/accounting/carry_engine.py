"""
Carry Engine — writes the CarriedInterest record for a scheme.

Architecture (post Pass 3.5 / Pass 4 refactor):
  - No hardcoded waterfall math here. ALL formula choice and computation is
    delegated to the Gemini-driven derivation pipeline (Pass 3.5 universal
    extraction + Pass 4 fund-metric derivation).
  - This module is a pure WRITER: it reads canonical waterfall values from
    DerivedMetric rows (which were either extracted verbatim from the
    Excel waterfall sheet by Pass 3.5 or derived from LPA terms + capital
    flows by Pass 4) and persists them as CarriedInterest.
  - If a metric is not in DerivedMetric (neither extracted nor derived),
    the corresponding CarriedInterest field stays at 0 and the source is
    noted in CarriedInterest.notes so the dashboard / provenance panel
    can show "Source: not available".

Why no formulas live here:
  - Funds vary: European whole-fund vs American deal-by-deal; compound vs
    simple preferred return; 100% catch-up vs no catch-up; LPA may specify
    paid-in or committed as the carry base. A static engine cannot serve
    all of these. Gemini reads the LPA terms (passed as context in Pass 4)
    and picks the correct formulation per fund.
"""

from decimal import Decimal
from datetime import date
from typing import Optional

from django.db import transaction
from django.utils import timezone


def _dm_value(scheme, metric_key):
    """Pull a DerivedMetric value for `scheme` by key, or None when absent.

    DerivedMetric rows are written by Pass 3.5 (formula='(direct value
    imported)' + Excel cell provenance) or Pass 4 (Gemini formula +
    inputs_used provenance). The frontend distinguishes them via
    formula_expression.

    When the metric supports multiple semantic variants (e.g. gross/net
    for total_unrealised_fair_value), pick the row tagged with the
    metric's variant_default declared in canonical_schema. Falls back
    to the untagged row, then to the first row.
    """
    try:
        from dataimport.models import DerivedMetric
        from dataimport.canonical_schema import CANONICAL_VALUE_CATEGORIES
    except Exception:
        return None, None
    rows = list(
        DerivedMetric.objects.filter(
            scheme=scheme, metric_key=metric_key,
        ).exclude(value=None)
    )
    if not rows:
        return None, None
    meta = (
        CANONICAL_VALUE_CATEGORIES.get('fund_performance_metrics', {})
        .get(metric_key)
    )
    variant_default = (
        meta.get('variant_default') if isinstance(meta, dict) else None
    )
    chosen = None
    if variant_default:
        chosen = next((r for r in rows if r.variant == variant_default), None)
    if chosen is None:
        chosen = next((r for r in rows if not r.variant), None)
    if chosen is None:
        chosen = rows[0]
    if chosen.value is None:
        return None, None
    return chosen.value, chosen


def _dec(v):
    if v is None:
        return Decimal('0')
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal('0')


def compute_carry(scheme, as_of_date: Optional[date] = None):
    """Persist a CarriedInterest record by harvesting Pass 3.5 / Pass 4
    DerivedMetric outputs. Idempotent (update_or_create on
    (scheme, calculation_date)).

    Returns: CarriedInterest instance.
    """
    from accounting.models import CarriedInterest

    if as_of_date is None:
        as_of_date = timezone.now().date()

    # ── Source values (all from DerivedMetric — single source of truth) ──
    keys = [
        'total_called_capital', 'total_committed_capital',
        'total_distributions', 'total_realised_proceeds',
        'total_unrealised_fair_value', 'nav',
        'return_of_capital_amount', 'preferred_return_amount',
        'gp_catchup_amount', 'carry_base',
        'carry_amount_gross', 'carry_amount_net',
        'gp_clawback_provision', 'lp_total_return',
        'gp_total_distribution',
    ]
    values = {}
    sources = {}
    for k in keys:
        v, dm = _dm_value(scheme, k)
        values[k] = v
        if dm is not None:
            sources[k] = {
                'formula': dm.formula_expression or '',
                'reasoning': (dm.gemini_reasoning or '')[:300],
                'confidence': float(dm.confidence or 0.0),
            }

    # ── Map to CarriedInterest fields. Missing values default to 0; the
    # provenance line in notes makes the absence explicit. ────────────
    total_called = _dec(values.get('total_called_capital'))
    total_distributions = _dec(
        values.get('total_distributions')
        if values.get('total_distributions') is not None
        else (
            _dec(values.get('total_realised_proceeds'))
            if values.get('total_realised_proceeds') is not None else None
        )
    )
    preferred_return = _dec(values.get('preferred_return_amount'))
    carry_base = _dec(values.get('carry_base'))
    carry_gross = _dec(values.get('carry_amount_gross'))
    carry_net = _dec(values.get('carry_amount_net'))
    clawback = _dec(values.get('gp_clawback_provision'))

    # If Pass 4 produced carry_amount_gross but not carry_amount_net (or
    # vice versa), back-fill the missing one from the relationship
    # net = gross - clawback. This is identity, not a formula choice, so
    # safe to do mechanically.
    if values.get('carry_amount_gross') is not None and values.get('carry_amount_net') is None:
        carry_net = max(carry_gross - clawback, Decimal('0'))
    elif values.get('carry_amount_net') is not None and values.get('carry_amount_gross') is None:
        carry_gross = carry_net + clawback

    # ── Build the provenance/notes block ────────────────────────────────
    note_lines = [
        f'Calculation date: {as_of_date}',
        f'Scheme LPA: hurdle={scheme.hurdle_rate_pct}% '
        f'carry={scheme.carry_pct}% type={scheme.carry_type or "?"} '
        f'fee={scheme.management_fee_pct}%/{scheme.management_fee_basis or "?"}',
        '',
        'Source of each value (Pass 3.5 = direct from Excel, Pass 4 = Gemini-derived):',
    ]
    field_map = [
        ('total_called_capital', 'Total Called Capital'),
        ('total_distributions', 'Total Distributions'),
        ('preferred_return_amount', 'Preferred Return'),
        ('carry_base', 'Carry Base'),
        ('carry_amount_gross', 'GP Carry (Gross)'),
        ('carry_amount_net', 'GP Carry (Net)'),
        ('gp_clawback_provision', 'Clawback Provision'),
    ]
    for k, label in field_map:
        v = values.get(k)
        src = sources.get(k)
        if v is None or src is None:
            note_lines.append(f'  - {label}: NO SOURCE')
            continue
        formula = src.get('formula') or ''
        tag = 'Pass3.5 (Excel)' if formula == '(direct value imported)' else 'Pass4 (Gemini)'
        snippet = formula[:120] if formula and formula != '(direct value imported)' else ''
        note_lines.append(
            f'  - {label} = {v}  ['
            f'{tag}, conf={src.get("confidence", 0):.2f}'
            f'{("; formula=" + snippet) if snippet else ""}]'
        )
    notes = '\n'.join(note_lines)[:2000]

    # ── Persist ────────────────────────────────────────────────────────
    with transaction.atomic():
        carry_record, _ = CarriedInterest.objects.update_or_create(
            scheme=scheme,
            calculation_date=as_of_date,
            defaults={
                'total_distributions': total_distributions,
                'total_called_capital': total_called,
                'preferred_return_amount': preferred_return,
                'carry_base': carry_base,
                'carry_amount_gross': carry_gross,
                'carry_amount_net': max(carry_net, Decimal('0')),
                'gp_clawback_provision': clawback,
                'calculation_status': 'indicative',
                'notes': notes,
            },
        )

    return carry_record
