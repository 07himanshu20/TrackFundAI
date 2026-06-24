"""
Layer 2 per-call validator — lightweight checks BEFORE merge.

Checks:
  • portfolio_investments present when valuations[] is present (Rule 17c)
  • Every valuations[] row has cost_basis (Rule 26 disambiguation)
  • portfolio_investments[] rows include irr_pct (Rule 21)
"""

import logging

logger = logging.getLogger(__name__)


def validate_layer2(data: dict) -> list[dict]:
    warnings: list[dict] = []
    if not isinstance(data, dict):
        return [{'rule': 'shape', 'msg': 'Layer 2 output is not a dict'}]

    pi = data.get('portfolio_investments') or []
    vals = data.get('valuations') or []

    if vals and not pi:
        warnings.append({
            'rule': 'valuations_without_investments',
            'msg': f'{len(vals)} valuations[] rows but portfolio_investments[] is empty (Rule 17c)',
        })

    if isinstance(vals, list) and vals:
        no_cost_basis = sum(
            1 for r in vals if isinstance(r, dict) and not r.get('cost_basis')
        )
        if no_cost_basis:
            warnings.append({
                'rule': 'valuations_missing_cost_basis',
                'msg': f'{no_cost_basis}/{len(vals)} valuations rows missing cost_basis (Rule 26)',
            })

    if isinstance(pi, list) and pi:
        no_irr = sum(
            1 for r in pi if isinstance(r, dict) and r.get('irr_pct') in (None, '')
        )
        if no_irr:
            warnings.append({
                'rule': 'portfolio_missing_irr',
                'msg': f'{no_irr}/{len(pi)} portfolio_investments rows missing irr_pct (Rule 21)',
            })

    if warnings:
        logger.info(f'[phase3.L2_validator] {len(warnings)} soft warnings')
    return warnings
