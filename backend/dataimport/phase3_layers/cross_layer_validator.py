"""
Cross-Layer Validator — runs the IDENTITY checks from priority_matrix.py
AFTER reconciliation. Violations are attached to the unified JSON as
quality flags; they NEVER block persistence (per design: dashboard shows
the value with ⚠️ but the import succeeds).

These identities are mathematical / accounting truths that must hold
universally across all AIF workbooks:
  TVPI ≡ (Dist + NAV) / Called
  RVPI + DPI ≡ TVPI
  MOIC ≡ Σ FV holding / Σ cost
  active_fair_value ≡ Σ Valuation.fair_value_of_holding
  total_committed_capital ≡ Σ Commitment.commitment_amount
  total_called_capital ≡ Σ CapitalCall.total_call_amount
  total_distributions ≡ Σ Distribution.total_net_amount
  carry_amount_net ≡ carry_amount_gross − gp_clawback_provision
"""

import logging
from decimal import Decimal

from .priority_matrix import IDENTITY_CHECKS, within_tolerance

logger = logging.getLogger(__name__)


def validate_identities(merged: dict) -> dict:
    """Run every identity check from priority_matrix.IDENTITY_CHECKS.

    Returns the merged dict with a new top-level '__identity_violations__'
    list (empty when all checks pass).
    """
    violations: list[dict] = []
    passes: list[dict] = []

    for check in IDENTITY_CHECKS:
        try:
            lhs = check['lhs'](merged)
            rhs = check['rhs'](merged)
        except Exception as e:
            logger.warning(
                f'[phase3.validator] check {check["name"]} raised {type(e).__name__}: {e}'
            )
            continue

        if lhs is None or rhs is None:
            # One side missing → check is not applicable (data hole, not a violation)
            continue

        if within_tolerance(lhs, rhs, check['tolerance']):
            passes.append({
                'name': check['name'],
                'description': check['description'],
                'lhs': str(lhs),
                'rhs': str(rhs),
                'principle': check['principle'],
            })
        else:
            try:
                diff = abs(Decimal(str(lhs)) - Decimal(str(rhs)))
            except Exception:
                diff = None
            v = {
                'name': check['name'],
                'description': check['description'],
                'lhs': str(lhs),
                'rhs': str(rhs),
                'difference': str(diff) if diff is not None else None,
                'tolerance': {
                    'pct': str(check['tolerance'].get('pct')) if check['tolerance'].get('pct') else None,
                    'abs': str(check['tolerance'].get('abs')) if check['tolerance'].get('abs') else None,
                },
                'principle': check['principle'],
                'severity': 'WARN',
            }
            violations.append(v)
            logger.warning(
                f'[phase3.validator] IDENTITY VIOLATION {check["name"]}: '
                f'lhs={lhs} rhs={rhs} diff={diff}  ({check["description"]})'
            )

    merged['__identity_violations__'] = violations
    merged['__identity_passes__'] = passes

    logger.info(
        f'[phase3.validator] {len(passes)} passed, {len(violations)} violated '
        f'(out of {len(IDENTITY_CHECKS)} identities)'
    )
    return merged
