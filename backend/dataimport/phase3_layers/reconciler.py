"""
Reconciler — applies priority_matrix.py rules to the merged JSON.

For every canonical field in FIELD_PRIORITIES:
  1. Evaluate every candidate source in priority order.
  2. Pick the first source that returns a non-null value.
  3. Compare against all OTHER candidates that also have a value:
       within tolerance → recorded as `alternatives[]` (informational)
       outside tolerance → recorded as `disagreements[]` + quality_flag
  4. Tag the picked value with `priority_rule_applied` (e.g. "P1+P6")
     and write it back into the merged JSON's `fund_performance` /
     `waterfall` / etc. block AND into a `__reconciliation__` block that
     phase2_persister copies into FundMetric.provenance.

The unified JSON returned by reconcile() retains the same top-level shape
that phase2_persister consumes — we just normalise / overwrite scalars
where the matrix says the picked value differs from what the layer emitted.
"""

import logging
from decimal import Decimal

from .priority_matrix import (
    FIELD_PRIORITIES, PRINCIPLES,
    resolve_path, within_tolerance,
)

logger = logging.getLogger(__name__)


# Which top-level block each canonical field should be written back into.
# Keeps the unified JSON in the shape phase2_persister expects.
_FIELD_TO_BLOCK = {
    'fund_name':                  ('fund_master', 'fund_name'),
    'sebi_registration_number':   ('fund_master', 'sebi_registration_number'),
    'vintage_year':               ('fund_master', 'vintage_year'),

    'total_committed_capital':    ('fund_performance', 'total_committed_capital'),
    'total_called_capital':       ('fund_performance', 'total_called_capital'),
    'total_uncalled_capital':     ('fund_performance', 'total_uncalled_capital'),
    'drawdown_pct':               ('fund_performance', 'drawdown_pct'),

    'total_distributions':        ('fund_performance', 'total_distributions'),
    'lp_distributions':           ('fund_performance', 'lp_distributions'),
    'return_of_capital_amount':   ('waterfall', 'step_1_return_of_capital'),

    'net_irr':                    ('fund_performance', 'net_irr_computed'),
    'gross_irr':                  ('fund_performance', 'gross_irr'),
    'tvpi':                       ('fund_performance', 'tvpi'),
    'dpi':                        ('fund_performance', 'dpi'),
    'rvpi':                       ('fund_performance', 'rvpi'),
    'moic':                       ('fund_performance', 'moic_portfolio'),
    'active_fair_value':          ('fund_performance', 'total_unrealised_fv_holding'),
    'invested_cost':              ('fund_performance', 'total_invested_capital'),

    'fund_nav':                   ('fund_performance', 'fund_nav_latest'),

    'carry_base':                 ('waterfall', 'carry_base'),
    'preferred_return_amount':    ('waterfall', 'step_2_preferred_return'),
    'gp_catchup_amount':          ('waterfall', 'step_3_catchup_amount'),
    'carry_amount_gross':         ('waterfall', 'carry_amount_gross'),
    'carry_amount_net':           ('waterfall', 'net_carry'),
    'gp_clawback_provision':      ('waterfall', 'clawback_provision'),
    'accrued_management_fees':    ('fund_performance', 'accrued_management_fees'),
}


def _principles_string(principles: list[str]) -> str:
    return '+'.join(principles) if principles else 'none'


def reconcile(merged: dict) -> dict:
    """Apply priority matrix to the merged JSON. Mutates `merged` and returns it.

    Side effects:
      • Overwrites <block>.<field> with the picked value where the matrix
        identified a higher-priority source than what was originally emitted.
      • Adds a top-level '__reconciliation__' dict — phase2_persister reads
        it to attach priority_rule_applied + alternatives + disagreements
        into each FundMetric.provenance row.
    """
    reconciliation: dict[str, dict] = {}

    for field_id, rule in FIELD_PRIORITIES.items():
        sources = rule['sources']
        tolerance = rule['tolerance']
        principles = rule['principles']
        reason = rule.get('reason', '')

        evaluated: list[dict] = []
        for path, description in sources:
            value = resolve_path(merged, path)
            evaluated.append({
                'description': description,
                'path': '.'.join(str(p) for p in path),
                'value': value,
            })

        # Pick first non-null source
        picked = None
        picked_index = None
        for i, e in enumerate(evaluated):
            if e['value'] is not None:
                picked = e
                picked_index = i
                break

        if picked is None:
            # No source produced a value — skip this field entirely.
            continue

        # Skipped higher-priority sources (they had no value)
        skipped_higher = [
            f"{e['description']} (no value at {e['path']})"
            for e in evaluated[:picked_index]
        ]

        # Lower-priority sources with values → compare for agreement
        alternatives: list[dict] = []
        disagreements: list[dict] = []
        for e in evaluated[picked_index + 1:]:
            if e['value'] is None:
                continue
            if within_tolerance(picked['value'], e['value'], tolerance):
                alternatives.append({
                    'description': e['description'],
                    'value': str(e['value']),
                    'status': 'within_tolerance',
                })
            else:
                disagreements.append({
                    'description': e['description'],
                    'value': str(e['value']),
                    'status': 'outside_tolerance',
                })

        # Overwrite the canonical block.field with the picked value, so
        # phase2_persister picks it up via its existing _first_present logic.
        block_key, field_key = _FIELD_TO_BLOCK.get(field_id, (None, None))
        if block_key:
            block = merged.setdefault(block_key, {})
            if isinstance(block, dict):
                # Only overwrite if the existing value disagrees (or is missing).
                existing = block.get(field_key)
                try:
                    eq = (existing is not None) and within_tolerance(
                        existing, picked['value'], tolerance,
                    )
                except Exception:
                    eq = False
                if not eq:
                    block[field_key] = (
                        str(picked['value']) if isinstance(picked['value'], Decimal)
                        else picked['value']
                    )

        rule_str = _principles_string(principles)
        reconciliation[field_id] = {
            'picked_value': str(picked['value']),
            'picked_source': picked['description'],
            'priority_rule_applied': rule_str,
            'principles_meaning': {p: PRINCIPLES.get(p, '') for p in principles},
            'reason': reason,
            'skipped_higher_priority_sources': skipped_higher,
            'alternatives_within_tolerance': alternatives,
            'disagreements_outside_tolerance': disagreements,
            'quality_flag': 'reconciled' if disagreements else 'clean',
        }

    merged['__reconciliation__'] = reconciliation

    n_clean = sum(1 for r in reconciliation.values() if r['quality_flag'] == 'clean')
    n_recon = sum(1 for r in reconciliation.values() if r['quality_flag'] == 'reconciled')
    logger.info(
        f'[phase3.reconciler] {len(reconciliation)} fields reconciled — '
        f'{n_clean} clean, {n_recon} reconciled (had disagreements)'
    )
    return merged
