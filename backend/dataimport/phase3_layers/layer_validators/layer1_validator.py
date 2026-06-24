"""
Layer 1 per-call validator — lightweight checks BEFORE merge.

Catches the most common Layer 1 issues:
  • No fund_master emitted at all (workbook probably routed wrong sheets)
  • nav_records without period_end dates (Rule 25 HARD GUARD #3)
  • waterfall block missing mandatory keys (Rule 23)
  • fund_performance present but missing key totals (data hole)
"""

import logging

logger = logging.getLogger(__name__)


_WATERFALL_REQUIRED = {
    'carry_amount_gross', 'carry_amount_net', 'clawback_provision', 'carry_base',
}


def validate_layer1(data: dict) -> list[dict]:
    """Return list of soft-warning dicts. Empty list = clean."""
    warnings: list[dict] = []
    if not isinstance(data, dict):
        return [{'rule': 'shape', 'msg': 'Layer 1 output is not a dict'}]

    if not data.get('fund_master'):
        warnings.append({'rule': 'fund_master_missing',
                         'msg': 'Layer 1 produced no fund_master block'})

    nav_records = data.get('nav_records') or []
    if isinstance(nav_records, list) and nav_records:
        no_date_rows = sum(
            1 for r in nav_records
            if isinstance(r, dict) and not (r.get('period_end') or r.get('nav_date'))
        )
        if no_date_rows:
            warnings.append({
                'rule': 'nav_records_no_period_end',
                'msg': f'{no_date_rows}/{len(nav_records)} nav_records rows missing period_end (Rule 25 HG#3)',
            })

    waterfall = data.get('waterfall') or {}
    if waterfall and isinstance(waterfall, dict):
        missing = sorted(_WATERFALL_REQUIRED - set(waterfall.keys()))
        if missing:
            warnings.append({
                'rule': 'waterfall_required_fields',
                'msg': f'waterfall missing mandatory keys (Rule 23): {missing}',
            })

    fp = data.get('fund_performance') or {}
    if fp and isinstance(fp, dict):
        if not fp.get('fund_nav_latest') and not nav_records:
            warnings.append({
                'rule': 'no_nav_anywhere',
                'msg': 'Neither fund_nav_latest nor nav_records present — NAV will be null',
            })

    if warnings:
        logger.info(f'[phase3.L1_validator] {len(warnings)} soft warnings')
    return warnings
