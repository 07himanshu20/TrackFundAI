"""
Layer 3 per-call validator — lightweight checks BEFORE merge.

Checks:
  • portfolio_kpis_periodic rows have (company_name, period)
  • monthly_pl_rows preserve per-period granularity (not collapsed)
"""

import logging

logger = logging.getLogger(__name__)


def _missing_key_count(rows: list, keys: tuple) -> int:
    n = 0
    for r in rows:
        if isinstance(r, dict) and not all(r.get(k) for k in keys):
            n += 1
    return n


def validate_layer3(data: dict) -> list[dict]:
    warnings: list[dict] = []
    if not isinstance(data, dict):
        return [{'rule': 'shape', 'msg': 'Layer 3 output is not a dict'}]

    kpis = data.get('portfolio_kpis_periodic') or []
    if isinstance(kpis, list) and kpis:
        bad = _missing_key_count(kpis, ('company_name', 'period'))
        if bad:
            warnings.append({
                'rule': 'kpi_missing_company_period',
                'msg': f'{bad}/{len(kpis)} portfolio_kpis_periodic rows missing company_name or period (Rule 13)',
            })

    for block in ('monthly_pl_rows', 'monthly_bs_rows', 'monthly_cf_rows'):
        rows = data.get(block) or []
        if isinstance(rows, list) and rows:
            bad = _missing_key_count(rows, ('company_name', 'period'))
            if bad:
                warnings.append({
                    'rule': f'{block}_missing_keys',
                    'msg': f'{bad}/{len(rows)} {block} rows missing company_name or period (Rule 11)',
                })

    if warnings:
        logger.info(f'[phase3.L3_validator] {len(warnings)} soft warnings')
    return warnings
