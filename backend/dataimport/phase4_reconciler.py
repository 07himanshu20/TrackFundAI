"""
Phase 4 — Layer 3: Reconciler

The single decision point that decides, per metric, whether to use:
  (a) the semantically-extracted value from the workbook  (preferred per user rule)
  (b) the Gemini-with-Code-Execution computed value        (universal fallback)
  (c) None                                                  (only when both fail)

USER'S CORE RULE (verbatim, 2026-06-30):
  "If something is already present in the excel data then our dashboard
   should trust it. Always give priority to semantically extracted value."

So the reconciler does NOT pit extracted vs computed and "pick the lower."
Extracted always wins — PROVIDED the extraction is semantically credible.

The whole class of bugs we saw on Bharatcrest came from Gemini extracting
₹1153.56 Cr from a cell labelled "GP Carry Allocated" and routing it to
`carry_amount_gross`. The cell value matched, but the label semantics
didn't — "GP Carry Allocated" includes catchup + GP residual share, not
just gross carry.

The fix is a SEMANTIC LABEL WHITELIST: per metric, list the substrings
that must appear in the cell label, and the substrings that must NOT.
An extraction only qualifies as "trusted" if its label_text passes that
check. Otherwise the Gemini-computed value takes over.

This module is intentionally pure — no DB writes, no Gemini calls. It
takes a unified Phase-3 JSON + a Gemini-compute result and returns a
single dict that drops into phase2_persister._persist_fund_metrics().

DETERMINISM
  This file does no I/O, no LLM calls, no time-dependent operations.
  Same inputs → same output, every time.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .phase4_gemini_compute import METRIC_CONTRACT

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# SEMANTIC LABEL WHITELIST
# Per metric, a list of (required_any, forbidden_any) tuples applied to
# the lower-cased label_text. The label must contain AT LEAST ONE word
# from required_any AND NONE from forbidden_any to be accepted.
#
# Universal: these are CA-grade vocabulary rules, not fund-specific.
# Tune by reading the audit log and adjusting one entry at a time.
# ═════════════════════════════════════════════════════════════════════════

# Helper sentinel — empty tuple means "no constraint"
_NONE: Tuple[str, ...] = ()


LABEL_WHITELIST: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    # ── Carry / waterfall — the highest-stakes class ────────────────────
    'carry_amount_gross': (
        ('gross carry', 'carry (gross)', 'gross carried interest', 'carried interest gross'),
        ('allocated', 'allocation', 'p&l', 'profit share', 'profit allocation',
         'net carry', 'net of', 'after clawback', 'after holdback', 'distributed'),
    ),
    'carry_amount_net': (
        ('net carry', 'carry (net)', 'net carried interest', 'carried interest net',
         'carry net of clawback', 'carry after clawback'),
        ('gross', 'allocated', 'allocation', 'before clawback', 'before holdback'),
    ),
    'gp_clawback_provision': (
        ('clawback', 'claw-back', 'escrow', 'holdback', 'reserve for clawback'),
        ('paid', 'released', 'reversed'),
    ),
    'gp_catchup_amount': (
        ('catch-up', 'catchup', 'catch up', 'gp catch'),
        ('preferred', 'hurdle', 'carry'),
    ),
    'preferred_return_amount': (
        ('preferred return', 'hurdle return', 'pref return', 'priority return',
         'hurdle amount'),
        ('rate', 'percentage', '%', 'pct'),
    ),
    'return_of_capital_amount': (
        ('return of capital', 'roc', 'capital returned', 'principal returned'),
        ('rate', 'percentage', 'remaining'),
    ),
    'carry_base': (
        ('carry base', 'profit pool', 'distributable profit', 'available for carry',
         'profit available', 'residual after preferred'),
        ('rate', 'percentage'),
    ),
    'lp_total_return': (
        ('lp total', 'lp share', 'lp residual', 'lp distribution total',
         'limited partner share'),
        ('per lp', 'individual lp', 'per investor'),
    ),
    'gp_total_distribution': (
        ('gp total', 'gp share', 'general partner share', 'gp distribution total'),
        ('per gp',),
    ),
    'sponsor_commitment_amount': (
        ('sponsor commitment', 'gp commitment amount', 'gp investment in fund'),
        ('%', 'pct', 'percentage', 'rate'),
    ),

    # ── Capital totals (extraction trust is high; labels are usually clear) ─
    'total_committed_capital': (
        ('committed capital', 'total commitment', 'total committed', 'aggregate commitment',
         'commitments raised', 'fund corpus committed'),
        ('called', 'drawn', 'uncalled', 'undrawn', 'remaining', 'per lp', 'per investor'),
    ),
    'total_capital_called': (
        ('capital called', 'called capital', 'drawn down', 'drawdown', 'total drawn',
         'capital drawdown', 'paid-in capital', 'paid in'),
        ('committed', 'uncalled', 'undrawn', 'remaining', 'per lp', 'per investor'),
    ),
    'total_uncalled_capital': (
        ('uncalled', 'undrawn', 'unfunded', 'remaining commitment',
         'available for drawdown'),
        ('called', 'drawn', 'paid-in'),
    ),
    'total_invested_capital': (
        ('invested capital', 'capital invested', 'cost of investments',
         'total investment cost', 'aggregate cost', 'deployed capital'),
        ('called', 'committed', 'fair value', 'fv', 'market value'),
    ),
    'total_realised_proceeds': (
        ('realised proceeds', 'realized proceeds', 'realised value', 'realized value',
         'exit proceeds', 'cash realised'),
        ('unrealised', 'unrealized', 'fair value', 'fv'),
    ),
    'total_distributions': (
        ('distributions', 'distributions to lps', 'distributions paid',
         'capital returned', 'cash returned to lps', 'lp distributions'),
        ('per lp', 'per investor', 'unrealised', 'fv', 'fair value', 'interim',
         'gp carry'),
    ),
    'total_unrealised_fv_holding': (
        ('unrealised fair value', 'unrealized fair value', 'unrealised fv',
         'fmv', 'fair market value', 'portfolio fair value', 'total fv',
         'residual value', 'fair value of holdings'),
        ('per lp', 'per investor', 'per company', 'realised', 'realized', 'cost'),
    ),
    'fund_nav_latest': (
        ('nav', 'net asset value', 'fund nav'),
        ('per unit', 'per share', 'per lp', 'per investor'),
    ),
    'fund_nav_per_unit': (
        ('nav per unit', 'nav per share', 'unit value', 'unit nav'),
        ('total', 'aggregate'),
    ),

    # ── Performance ratios — extracted values are common in fund-admin sheets ─
    'moic': (
        ('moic', 'multiple on invested capital', 'multiple on capital',
         'gross multiple', 'net multiple'),
        ('tvpi', 'dpi', 'rvpi'),
    ),
    'tvpi': (
        ('tvpi', 'total value to paid-in', 'total value/paid-in'),
        ('moic', 'dpi', 'rvpi'),
    ),
    'dpi': (
        ('dpi', 'distributions to paid-in', 'distributed to paid-in'),
        ('moic', 'tvpi', 'rvpi'),
    ),
    'rvpi': (
        ('rvpi', 'residual value to paid-in'),
        ('moic', 'tvpi', 'dpi'),
    ),
    'net_irr': (
        ('net irr', 'irr (net)', 'net internal rate'),
        ('gross irr', 'target irr', 'projected', 'hurdle'),
    ),
    'gross_irr': (
        ('gross irr', 'irr (gross)', 'gross internal rate'),
        ('net irr', 'target', 'projected', 'hurdle'),
    ),

    # ── LPA terms ───────────────────────────────────────────────────────
    'hurdle_rate_pct': (
        ('hurdle', 'preferred return rate', 'pref return rate', 'priority rate'),
        ('amount', 'cumulative', 'accrued'),
    ),
    'carry_percentage': (
        ('carry %', 'carry pct', 'carried interest %', 'carried interest percentage',
         'carry rate', 'gp share %'),
        ('amount', 'gross', 'net', 'accrued'),
    ),
    'mgmt_fee_pct': (
        ('management fee %', 'mgmt fee %', 'management fee rate',
         'annual management fee'),
        ('amount', 'paid', 'accrued', 'ytd'),
    ),
    'gp_holdback_pct': (
        ('holdback %', 'escrow %', 'clawback escrow %', 'holdback rate'),
        ('amount', 'released'),
    ),
    'sponsor_commitment_pct': (
        ('sponsor commitment %', 'gp commitment %', 'gp share of corpus'),
        ('amount', 'cr'),
    ),
}


# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════

def _d(v) -> Optional[Decimal]:
    if v is None or v == '':
        return None
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None


def label_is_credible(metric: str, label_text: str) -> Tuple[bool, str]:
    """Decide whether a Gemini-extracted label semantically matches the metric.

    Returns (is_credible, reason_string). reason_string is human-readable so
    the audit log shows exactly why a label was accepted or rejected.

    Default to credible=True for metrics without a whitelist entry — the
    whitelist is gradual; we don't block metrics we haven't curated yet.
    """
    if not label_text or not isinstance(label_text, str):
        return False, 'no label_text provided'

    if metric not in LABEL_WHITELIST:
        return True, 'no whitelist entry — extraction trusted by default'

    required_any, forbidden_any = LABEL_WHITELIST[metric]
    lab = label_text.strip().lower()

    if required_any:
        hits = [w for w in required_any if w in lab]
        if not hits:
            return False, f'label missing any required keyword from {list(required_any)[:4]}...'

    if forbidden_any:
        bads = [w for w in forbidden_any if w in lab]
        if bads:
            return False, f'label contains forbidden keyword(s) {bads}'

    return True, 'label semantically matches metric'


# ═════════════════════════════════════════════════════════════════════════
# Metric-name normalisation
# Gemini occasionally emits a slightly different key than our contract
# (e.g. `preferred_return` vs `preferred_return_amount`). We map them
# back to the canonical contract key so the rest of the system sees
# one consistent vocabulary. Universal across fund formats.
# ═════════════════════════════════════════════════════════════════════════

_METRIC_ALIASES: Dict[str, str] = {
    'preferred_return':            'preferred_return_amount',
    'gp_carry_gross':              'carry_amount_gross',
    'gp_carry_net':                'carry_amount_net',
    'gp_clawback':                 'gp_clawback_provision',
    'gp_catchup':                  'gp_catchup_amount',
    'return_of_capital':           'return_of_capital_amount',
    'gross_carry':                 'carry_amount_gross',
    'net_carry':                   'carry_amount_net',
    'clawback':                    'gp_clawback_provision',
    'fund_nav':                    'fund_nav_latest',
    'nav':                         'fund_nav_latest',
    'committed_capital':           'total_committed_capital',
    'called_capital':              'total_capital_called',
    'distributions':               'total_distributions',
    'lp_distributions':            'total_distributions',
    'unrealised_fv':               'total_unrealised_fv_holding',
    'irr':                         'net_irr',
    'net_irr_pct':                 'net_irr',
    'gross_irr_pct':               'gross_irr',
}


def canonical_metric(name: str) -> str:
    """Normalise a metric name to its canonical contract key."""
    if not name:
        return name
    s = str(name).strip().lower()
    return _METRIC_ALIASES.get(s, s)


# ═════════════════════════════════════════════════════════════════════════
# Trusted-extraction collector
# ═════════════════════════════════════════════════════════════════════════

def collect_trusted_extractions(unified_json: dict) -> Dict[str, Dict[str, Any]]:
    """Walk unified_json['workbook_aggregates'] and keep only entries whose
    label_text passes the semantic whitelist for their declared metric.

    Returns: {metric_name: {value, sheet, cell, label_text, why_trusted}}.

    If multiple workbook_aggregates entries claim the same metric, the
    first one to PASS the whitelist wins. Iteration order is sorted by
    metric name then cell ref for determinism across re-imports.
    """
    aggs = (unified_json or {}).get('workbook_aggregates') or []
    if not isinstance(aggs, list):
        return {}

    sorted_aggs = sorted(
        (a for a in aggs if isinstance(a, dict) and a.get('metric')),
        key=lambda a: (str(a.get('metric')), str(a.get('sheet')), str(a.get('cell'))),
    )

    trusted: Dict[str, Dict[str, Any]] = {}
    for a in sorted_aggs:
        metric = canonical_metric(a.get('metric'))
        if metric in trusted:
            continue
        label = a.get('label_text') or ''
        ok, reason = label_is_credible(metric, label)
        if not ok:
            logger.info(
                f'[reconciler] REJECT extracted {metric}={a.get("value")} '
                f'(label="{label}" from {a.get("sheet")}!{a.get("cell")}) — {reason}'
            )
            continue
        val = _d(a.get('value'))
        if val is None:
            logger.info(f'[reconciler] REJECT extracted {metric} — value not numeric')
            continue
        trusted[metric] = {
            'value': val,
            'sheet': a.get('sheet'),
            'cell':  a.get('cell'),
            'label_text': label,
            'why_trusted': reason,
        }
        logger.info(
            f'[reconciler] ACCEPT extracted {metric}={val} '
            f'(from {a.get("sheet")}!{a.get("cell")}, label="{label}")'
        )

    return trusted


# ═════════════════════════════════════════════════════════════════════════
# Main entry point
# ═════════════════════════════════════════════════════════════════════════

def reconcile(
    unified_json: dict,
    gemini_compute_result: Optional[dict],
) -> Dict[str, Any]:
    """Build the final aggregates dict per the user's rule:
      EXTRACTED wins if its label passes the whitelist.
      Otherwise the Gemini-computed value is used.
      Otherwise None (the persister keeps the prior value or shows '—').

    Returns:
      {
        'flat':        {metric: Decimal},                ← drop-in for old aggregates
        'provenance':  {metric: {source, label, cell, formula, why}},
        'extracted_count': int,
        'computed_count':  int,
        'unavailable_count': int,
      }

    The `flat` shape matches what phase2_persister._persist_fund_metrics() +
    _persist_carried_interest() consume today, so this is a drop-in for the
    existing `compute_all_fund_aggregates()` return value.
    """
    trusted = collect_trusted_extractions(unified_json)
    raw_computed = (gemini_compute_result or {}).get('flat') or {}
    raw_meta     = (gemini_compute_result or {}).get('metrics') or {}
    # Normalise Gemini's emitted keys to the canonical contract vocabulary
    computed = {canonical_metric(k): v for k, v in raw_computed.items()}
    computed_meta = {canonical_metric(k): v for k, v in raw_meta.items()} if isinstance(raw_meta, dict) else {}

    flat: Dict[str, Optional[Decimal]] = {}
    prov: Dict[str, Dict[str, Any]] = {}

    all_keys = sorted(set(METRIC_CONTRACT.keys()) | set(computed.keys()) | set(trusted.keys()))

    for key in all_keys:
        if key in trusted:
            flat[key] = trusted[key]['value']
            prov[key] = {
                'source':      'extracted',
                'label_text':  trusted[key]['label_text'],
                'cell':        f'{trusted[key]["sheet"]}!{trusted[key]["cell"]}',
                'formula':     None,
                'why':         trusted[key]['why_trusted'],
            }
            continue

        if computed.get(key) is not None:
            flat[key] = _d(computed.get(key))
            meta = computed_meta.get(key, {}) if isinstance(computed_meta, dict) else {}
            prov[key] = {
                'source':      'gemini_code_execution',
                'label_text':  None,
                'cell':        ', '.join(meta.get('cell_refs') or []) or None,
                'formula':     meta.get('formula_used'),
                'why':         'no trusted extraction; Gemini computed via Code Execution',
            }
            continue

        flat[key] = None
        prov[key] = {
            'source':      'unavailable',
            'label_text':  None,
            'cell':        None,
            'formula':     None,
            'why':         'neither a credibly-labelled extraction nor a Gemini compute result was available',
        }

    counts = {
        'extracted_count':   sum(1 for v in prov.values() if v['source'] == 'extracted'),
        'computed_count':    sum(1 for v in prov.values() if v['source'] == 'gemini_code_execution'),
        'unavailable_count': sum(1 for v in prov.values() if v['source'] == 'unavailable'),
    }

    # Add a clean ledger line per run so the audit log shows what won.
    logger.info(
        f'[reconciler] reconciliation complete — '
        f'extracted={counts["extracted_count"]}, '
        f'computed={counts["computed_count"]}, '
        f'unavailable={counts["unavailable_count"]} '
        f'(of {len(all_keys)} metrics)'
    )

    return {
        'flat':        flat,
        'provenance':  prov,
        **counts,
    }
