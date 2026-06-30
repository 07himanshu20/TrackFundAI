"""
Merger — combines the JSON outputs of every Flavor A layer + Flavor B chunk
into ONE unified JSON object with the same top-level shape phase2_persister
already consumes.

  • Within a layer: chunk outputs are appended (arrays concat with per-array
    natural-key DEDUP; objects shallow-merged). Chunks should be disjoint
    by construction (row-range filtering at source), but defensive dedup
    protects against any chunk-boundary glitches universally.

  • Across layers: top-level keys are merged. Each layer owns specific keys
    (per prompt contract). When two layers happen to emit the same key, the
    collision is recorded in __phase3_diagnostics__.collisions for visibility.
"""

import logging
import re
from collections import defaultdict

logger = logging.getLogger(__name__)


# Universal entity-name canonicalisation. Strips legal suffixes, trailing
# punctuation, multiple-whitespace. Used by EVERY natural key so that
# "ABC Capital Pvt Ltd" and "ABC Capital" and "ABC Capital Pvt. Ltd."
# all dedup to the same entity. Works for any AIF Excel format / locale.
_LEGAL_SUFFIX_RE = re.compile(
    r'\b('
    r'pvt\.?\s*ltd\.?|'
    r'private\s+limited|'
    r'limited|'
    r'ltd\.?|'
    r'llp|'
    r'l\.l\.p\.?|'
    r'inc\.?|'
    r'incorporated|'
    r'corp\.?|'
    r'corporation|'
    r'co\.?|'
    r'company|'
    r'plc|'
    r'gmbh|'
    r's\.?a\.?|'
    r'ag|'
    r'bv|'
    r'pte\.?\s*ltd\.?|'
    r'sdn\.?\s*bhd\.?|'
    r'aif|'
    r'trust'
    r')\b\.?',
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r'[,.;:&/\\\-_\(\)\[\]\{\}\'"!?]+')
_WS_RE = re.compile(r'\s+')


def _canonicalize_entity_name(v) -> str:
    """Universal name canonicaliser. None / empty → empty string."""
    if v is None:
        return ''
    s = str(v).strip()
    if not s:
        return ''
    s = s.lower()
    s = _LEGAL_SUFFIX_RE.sub(' ', s)
    s = _PUNCT_RE.sub(' ', s)
    s = _WS_RE.sub(' ', s).strip()
    return s


_LAYER_BLOCKS = {
    'L1': {
        'fund_master', 'investors', 'commitments', 'capital_calls',
        'distributions', 'nav_records', 'waterfall', 'fund_performance',
        'entities', 'compliance_records',
    },
    'L2': {
        'portfolio_investments', 'valuations', 'exits', 'quoted_unquoted',
    },
    'L3': {
        'portfolio_kpis_periodic', 'monthly_pl_rows', 'monthly_bs_rows',
        'monthly_cf_rows', 'budget_vs_actual', 'burn_runway',
    },
}

_ARRAY_BLOCKS = {
    'investors', 'commitments', 'capital_calls', 'distributions',
    'nav_records', 'entities', 'compliance_records',
    'portfolio_investments', 'valuations', 'exits', 'quoted_unquoted',
    'portfolio_kpis_periodic', 'monthly_pl_rows', 'monthly_bs_rows',
    'monthly_cf_rows', 'budget_vs_actual', 'burn_runway',
    'sheet_completeness',
}
_OBJECT_BLOCKS = {'fund_master', 'waterfall', 'fund_performance', 'provenance'}


# Natural key functions per array block. A row's dedup key is a tuple of
# normalised values; rows with identical keys are merged (later non-null
# fields fill in earlier nulls). Universal: keys are derived from fields
# every AIF Excel format exposes, not file-specific.
def _norm(v):
    """Generic scalar normaliser: lowercase, stripped. Not for entity names."""
    if v is None:
        return None
    s = str(v).strip().lower()
    return s or None


def _name(v):
    """Entity-name normaliser. Strips legal suffixes + punctuation so
    'ABC Capital Pvt Ltd' == 'ABC Capital' == 'ABC Capital Pvt. Ltd.'.
    Returns None for empty so dedup correctly treats keyless rows."""
    s = _canonicalize_entity_name(v)
    return s or None


def _k_investors(r):
    return (_name(r.get('investor_name') or r.get('legal_name')),
            _norm(r.get('pan')))


def _k_commitments(r):
    return (_name(r.get('investor_name') or r.get('legal_name')),
            _norm(r.get('scheme_name')),
            _norm(r.get('commitment_amount')))


def _k_capital_calls(r):
    return (_norm(r.get('scheme_name')),
            _norm(r.get('call_number')),
            _norm(r.get('call_date')),
            _name(r.get('investor_name')))


def _k_distributions(r):
    return (_norm(r.get('scheme_name')),
            _norm(r.get('distribution_number')),
            _norm(r.get('distribution_date')),
            _name(r.get('investor_name')))


def _k_nav_records(r):
    return (_norm(r.get('scheme_name')),
            _norm(r.get('nav_date') or r.get('period_end')))


def _k_entities(r):
    return (_norm(r.get('entity_type')),
            _name(r.get('entity_name')))


def _k_compliance(r):
    return (_name(r.get('fund_name')),
            _norm(r.get('scheme_name')),
            _norm(r.get('compliance_type') or r.get('report_type')),
            _norm(r.get('due_date') or r.get('reporting_period_end')))


def _k_portfolio(r):
    # Universal dedup key: include BOTH tranche_number AND investment_date AND
    # amount_invested so two genuinely-distinct tranches into the same company
    # on the same date (different amounts) don't get collapsed.
    # Fixed 2026-06-30: previously falling back tranche→date meant two
    # tranches on the same day with same instrument were treated as 1.
    return (_name(r.get('company_name')),
            _norm(r.get('scheme_name')),
            _norm(r.get('instrument_type')),
            _norm(r.get('tranche_number')),
            _norm(r.get('investment_date')),
            _norm(r.get('amount_invested') or r.get('cost_basis') or
                  r.get('investment_amount')))


def _k_valuations(r):
    return (_name(r.get('company_name')),
            _norm(r.get('valuation_date')),
            _norm(r.get('cost_basis') or r.get('investment_ref')))


def _k_exits(r):
    return (_name(r.get('company_name')),
            _norm(r.get('exit_date')),
            _norm(r.get('exit_type')))


def _k_quoted(r):
    return (_name(r.get('company_name')),
            _norm(r.get('isin')))


def _k_kpis_periodic(r):
    return (_name(r.get('company_name')),
            _norm(r.get('period')))


def _k_monthly(r):
    return (_name(r.get('company_name')),
            _norm(r.get('period')))


def _k_budget(r):
    return (_name(r.get('company_name')),
            _norm(r.get('period')),
            _norm(r.get('line_item')))


def _k_burn(r):
    return (_name(r.get('company_name')),
            _norm(r.get('period')))


def _k_sheet_completeness(r):
    return (_norm(r.get('sheet_name')),
            _norm(r.get('target_array')))


_NATURAL_KEYS = {
    'investors':              _k_investors,
    'commitments':            _k_commitments,
    'capital_calls':          _k_capital_calls,
    'distributions':          _k_distributions,
    'nav_records':            _k_nav_records,
    'entities':               _k_entities,
    'compliance_records':     _k_compliance,
    'portfolio_investments':  _k_portfolio,
    'valuations':             _k_valuations,
    'exits':                  _k_exits,
    'quoted_unquoted':        _k_quoted,
    'portfolio_kpis_periodic': _k_kpis_periodic,
    'monthly_pl_rows':        _k_monthly,
    'monthly_bs_rows':        _k_monthly,
    'monthly_cf_rows':        _k_monthly,
    'budget_vs_actual':       _k_budget,
    'burn_runway':            _k_burn,
    'sheet_completeness':     _k_sheet_completeness,
}


def _dedup_rows(block_key: str, rows: list) -> tuple[list, int]:
    """Dedup rows of an array block by natural key. Later rows fill nulls in
    earlier rows (additive merge). Returns (deduped_rows, n_collisions)."""
    key_fn = _NATURAL_KEYS.get(block_key)
    if key_fn is None:
        return list(rows), 0

    by_key: dict = {}
    no_key_rows: list = []
    collisions = 0
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            k = key_fn(r)
        except Exception:
            no_key_rows.append(r)
            continue
        # If every component of the key is None, treat as keyless (can't
        # dedup a row that has none of its identifying fields).
        if not any(c is not None for c in (k if isinstance(k, tuple) else (k,))):
            no_key_rows.append(r)
            continue
        if k in by_key:
            collisions += 1
            existing = by_key[k]
            for kk, vv in r.items():
                if vv is None or vv == '':
                    continue
                if existing.get(kk) in (None, ''):
                    existing[kk] = vv
        else:
            by_key[k] = dict(r)

    return list(by_key.values()) + no_key_rows, collisions


def merge_layer_chunks(layer: str, chunk_outputs: list[dict]) -> dict:
    """Merge all chunks of ONE layer into one dict, with per-array dedup."""
    merged: dict = {}
    array_blocks: dict[str, list] = defaultdict(list)
    object_blocks: dict[str, dict] = defaultdict(dict)

    for co in chunk_outputs:
        data = co.get('data') or {}
        if not isinstance(data, dict):
            logger.warning(
                f'[phase3.merger] {co.get("chunk_id")} returned non-dict data; skipped'
            )
            continue
        for key, val in data.items():
            if key in _ARRAY_BLOCKS and isinstance(val, list):
                array_blocks[key].extend(val)
            elif key in _OBJECT_BLOCKS and isinstance(val, dict):
                object_blocks[key].update(val)
            else:
                merged[key] = val

    dedup_report: dict = {}
    for k, arr in array_blocks.items():
        deduped, dup_count = _dedup_rows(k, arr)
        merged[k] = deduped
        if dup_count:
            dedup_report[k] = {'input_rows': len(arr), 'output_rows': len(deduped),
                               'duplicates_collapsed': dup_count}
            logger.info(
                f'[phase3.merger] {layer}.{k}: deduped {len(arr)} → '
                f'{len(deduped)} ({dup_count} duplicate keys collapsed)'
            )
    for k, obj in object_blocks.items():
        merged[k] = obj

    if dedup_report:
        merged.setdefault('__merge_dedup__', {})[layer] = dedup_report

    return merged


def merge_all_layers(layer_results: dict[str, dict]) -> dict:
    """Combine merged-per-layer outputs into the unified workbook JSON.

    Records cross-layer key collisions into a per-call diagnostics block
    surfaced via __phase3_diagnostics__.collisions for dashboard visibility.
    """
    unified: dict = {}
    collisions: list[dict] = []
    merge_dedup: dict = {}

    for layer, ldata in layer_results.items():
        if not isinstance(ldata, dict):
            continue
        owned = _LAYER_BLOCKS.get(layer, set())
        layer_dedup = ldata.pop('__merge_dedup__', None)
        if layer_dedup:
            merge_dedup.update(layer_dedup)
        for key, val in ldata.items():
            if key in unified:
                collisions.append({
                    'block': key,
                    'second_writer_layer': layer,
                    'note': 'last-writer-wins applied',
                })
            unified[key] = val
            if owned and key not in owned and key not in {'sheet_completeness', 'provenance'}:
                logger.info(
                    f'[phase3.merger] {layer} emitted out-of-contract block "{key}" — kept'
                )

    if collisions:
        logger.warning(
            f'[phase3.merger] {len(collisions)} cross-layer key collision(s): '
            f'{[c["block"] for c in collisions]}'
        )
        unified.setdefault('__merge_collisions__', []).extend(collisions)
    if merge_dedup:
        unified.setdefault('__merge_dedup__', {}).update(merge_dedup)

    return unified
