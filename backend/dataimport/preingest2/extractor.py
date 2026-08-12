"""
Stage 3 — Extraction & the per-file worker (the only stage that reads real
content into the model). One bounded worker per file, cache-guarded.

Sequence:
  1. Golden-record cache lookup (file hash + pipeline version). HIT → re-serve
     the frozen record, NO model call (determinism gate).
  2. MISS → profile (S1) → classify (S2) → extract:
       • FUND file      : map columns (LLM) + move cached values (code) per sheet,
                          into canonical domain records with provenance. Any sheet
                          the segmenter flags as a raw numeric ledger is
                          code-aggregated, never row-exploded.
       • COMPANY MIS     : collapse to ONE Portfolio row (TTM) — never row-dumped.
  3. Freeze the validated record to the cache.

Explosion is structurally impossible: company MIS files never emit rows, and
fund ledgers are aggregated in code.
"""
import logging

from ..preingest.company_summary import summarize_company
from ..preingest.mapper import map_file
from ..preingest.mover import move_sheet
from . import cache
from .classifier import classify
from .profiler import profile_file
from .segmenter import segment_sheet

logger = logging.getLogger(__name__)

_COMPANY_FIN_DOMAINS = ('financials_pl_bva', 'fund_pl_bs', 'burn_runway', 'valuations_kpis')


def _is_raw_ledger(rows) -> bool:
    units = segment_sheet(rows)
    return bool(units) and all(u['kind'] == 'ledger_summary' for u in units)


def extract_file(label: str, path: str, heartbeat=None) -> dict:
    """Return the frozen extraction record for one file (from cache or fresh).

    `heartbeat(stage)` — optional callback pinged after each unit of work
    (profile, classify, each sheet). The orchestrator resets the per-file
    kill deadline on every ping, so the process-kill only fires when a file
    makes NO progress for the idle window — a true backstop, not a blind
    wall-clock that false-kills a legitimately large workbook.
    """
    def _hb(stage):
        if heartbeat:
            try:
                heartbeat(stage)
            except Exception:  # noqa: BLE001 — never let telemetry break extraction
                pass

    cached = cache.get(path)
    if cached is not None:
        cached['_cache'] = 'hit'
        return cached

    prof = profile_file(path)
    _hb('profiled')
    fps = prof['sheets']
    wb = prof['raw']
    cls = classify(fps)
    _hb('classified')
    record = {'file_class': cls['file_class'], 'entity': cls['entity_name'],
              'confidence': cls['confidence'], 'needs_review': cls['needs_review'],
              'records_by_domain': {}, 'company_row': None, '_cache': 'miss'}

    maps = map_file(fps, label)
    _hb('mapped')
    by_domain = record['records_by_domain']
    for fp in fps:
        sn = fp['sheet']
        rows = wb['data'][sn]['rows']
        if _is_raw_ledger(rows):
            # raw numeric ledger — never row-explode; its totals (if needed) are
            # handled by the fund-P&L/line-item path, not by unpivoting.
            _hb(f'sheet:{sn}')
            continue
        m = maps.get(sn, {'domain': None, 'skip': True})
        recs, rep = move_sheet(label, sn, rows, m)
        dom = rep.get('domain')
        if recs and dom:
            by_domain.setdefault(dom, []).extend(recs)
        _hb(f'sheet:{sn}')

    if cls['file_class'] == 'mis':
        fin = []
        for d in _COMPANY_FIN_DOMAINS:
            fin.extend(by_domain.get(d, []))
        try:
            record['company_row'] = summarize_company(cls['entity_name'] or label, fin)
        except Exception as e:  # noqa: BLE001
            logger.exception(f'[preingest2] summary failed {label}')
            record['company_row'] = {'company': cls['entity_name'] or label,
                                     'note': f'summary error: {e}'}
        # a company MIS contributes only its one Portfolio row, not its raw records
        record['records_by_domain'] = {}

    cache.put(path, record)
    return record
