"""
Orchestrates the pre-ingestion pipeline → a Fund Master Workbook (TFAI shape).

    for each uploaded file:
        rows = workbook_cache.load_workbook(path)      # cached values only
        fps  = fingerprint_workbook(rows)              # deterministic
        kind = classify_file(fps)                      # ONE Gemini call: fund | company_mis
        maps = map_file(fps)                           # Gemini column maps (per batch)
        recs = move_sheet(...) for every sheet         # deterministic value copy
        if fund        -> merge recs into fund_records by domain
        if company_mis -> summarize_company(recs) -> ONE Portfolio row

    assemble(fund_records, company_rows) -> fixed template
    build_workbook(...)                  -> TFAI.xlsx

The output is ALWAYS the fixed 18-sheet Fund Master structure. Company MIS files
collapse to one row each, so a raw data-dump can never explode the output.
"""
import logging
import os
from collections import defaultdict

from ..phase3_layers.workbook_cache import evict, load_workbook
from . import output_template as tmpl
from .assembler import assemble
from .company_summary import summarize_company
from .file_classifier import classify_file
from .fingerprint import fingerprint_workbook
from .mapper import map_file
from .mover import move_sheet
from .template_writer import build_workbook

logger = logging.getLogger(__name__)

# Canonical domains that hold a single company's operating financials — the
# source of the Type-B summary rows.
_COMPANY_FINANCIAL_DOMAINS = ('financials_pl_bva', 'fund_pl_bs',
                              'burn_runway', 'valuations_kpis')


def consolidate(files, progress=None):
    """files: [(label, filepath)]. Returns (workbook, summary_dict)."""
    fund_records = defaultdict(list)
    company_rows = []
    audit = []
    n = len(files)

    def _tick(i, msg):
        if progress:
            progress(int(5 + 90 * i / max(n, 1)), msg)

    for i, (label, path) in enumerate(files):
        _tick(i, f'Reading {label}')
        try:
            wb = load_workbook(path)
        except Exception as e:  # noqa: BLE001
            logger.exception(f'[preingest] read failed {label}')
            audit.append({'file': label, 'kind': '?', 'sheets_total': 0,
                          'records_extracted': 0, 'status': 'read_error',
                          'notes': str(e)[:200]})
            continue

        fps = fingerprint_workbook(wb)
        _tick(i, f'Classifying {label} ({len(fps)} sheets)')
        try:
            kind_info = classify_file(fps)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'[preingest] file-class failed {label}: {e}')
            kind_info = {'kind': 'fund', 'company_name': None, 'confidence': 'low'}
        kind = kind_info['kind']

        try:
            maps = map_file(fps, label)
        except Exception as e:  # noqa: BLE001
            logger.exception(f'[preingest] map failed {label}')
            audit.append({'file': label, 'kind': kind, 'sheets_total': len(fps),
                          'records_extracted': 0, 'status': 'map_error',
                          'notes': str(e)[:200]})
            evict(path)
            continue

        # deterministic extraction of every sheet
        per_domain = defaultdict(list)
        recs_total = 0
        for fp in fps:
            sn = fp['sheet']
            m = maps.get(sn, {'domain': None, 'skip': True})
            recs, rep = move_sheet(label, sn, wb['data'][sn]['rows'], m)
            dom = rep.get('domain')
            if recs and dom:
                for r in recs:
                    r['__source_kind__'] = kind
                per_domain[dom].extend(recs)
                recs_total += len(recs)

        if kind == 'company_mis':
            fin = []
            for d in _COMPANY_FINANCIAL_DOMAINS:
                fin.extend(per_domain.get(d, []))
            _tick(i, f'Summarizing {label}')
            try:
                row = summarize_company(kind_info.get('company_name') or label, fin)
            except Exception as e:  # noqa: BLE001
                logger.exception(f'[preingest] summary failed {label}')
                row = {'company': kind_info.get('company_name') or label,
                       'note': f'summary error: {e}'}
            company_rows.append(row)
            audit.append({'file': label, 'kind': 'company_mis',
                          'sheets_total': len(fps), 'records_extracted': recs_total,
                          'status': 'ok', 'notes': f"company={row.get('company')}"})
        else:
            for d, rs in per_domain.items():
                fund_records[d].extend(rs)
            audit.append({'file': label, 'kind': 'fund',
                          'sheets_total': len(fps), 'records_extracted': recs_total,
                          'status': 'ok', 'notes': ''})
        evict(path)

    if progress:
        progress(96, 'Assembling Fund Master Workbook')
    assembled = assemble(fund_records, company_rows)
    wb_out = build_workbook(assembled, audit)

    summary = {
        'files': n,
        'company_rows': len(company_rows),
        'fund_domains': {d: len(r) for d, r in fund_records.items()},
        'sheet_row_counts': {s['name']: len(assembled[s['key']]['rows'])
                             for s in tmpl.SHEETS},
        'audit': audit,
    }
    return wb_out, summary


def consolidate_to_path(files, out_path, progress=None):
    wb, summary = consolidate(files, progress=progress)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    wb.save(out_path)
    summary['output_path'] = out_path
    return summary
