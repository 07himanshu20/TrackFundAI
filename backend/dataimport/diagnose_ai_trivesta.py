"""Diagnostic script — runs Phase 6 Stages 1-3 on AI_Trivesta.xlsx and dumps
the intermediate state to /tmp so we can inspect exactly:

  1. What domain + layout Gemini assigned to each sheet
  2. What column_map Gemini produced per sheet
  3. What rows extract_sheet produced (per sheet)
  4. What unified_builder passed to the persister
  5. Where valuations/hurdle/ARR routing broke

Usage:
    cd backend && python manage.py shell <<'PY'
    from dataimport.diagnose_ai_trivesta import diagnose
    diagnose('/Users/himanshusharma/Downloads/AI_Trivesta.xlsx')
    PY

Writes diagnostic JSON to /tmp/ai_trivesta_diagnosis.json
"""
import json
import os
from decimal import Decimal
from datetime import date, datetime


def _serialize(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, tuple):
        return list(obj)
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def diagnose(filepath: str, out_path: str = '/tmp/ai_trivesta_diagnosis.json'):
    from dataimport.phase3_layers.workbook_cache import load_workbook, evict as _evict_cache
    from dataimport.phase6_extractor.stage1_classifier import run_stage1
    from dataimport.phase6_extractor.extractors import extract_sheet
    from dataimport.phase6_extractor.unified_builder import build_unified_json

    print(f"Loading workbook: {filepath}")
    workbook_data = load_workbook(filepath)
    sheet_names = workbook_data['sheets']
    print(f"  {len(sheet_names)} sheets: {sheet_names}")

    print("\n=== STAGE 1: Gemini classification (one call) ===")
    stage1 = run_stage1(workbook_data)
    sheets_map = stage1.get('sheets') or {}

    stage1_dump = {}
    for sn in sheet_names:
        info = sheets_map.get(sn, {})
        stage1_dump[sn] = {
            'domain': info.get('domain'),
            'layout': info.get('layout'),
            'column_map': info.get('column_map', {}),
            'notes': info.get('notes'),
        }
        print(f"  {sn:30}  domain={info.get('domain')!r:22}  layout={info.get('layout')!r}")

    print("\n=== STAGE 2: Extract rows per sheet ===")
    per_sheet = {}
    for sn in sheet_names:
        info = sheets_map.get(sn) or {}
        domain = info.get('domain')
        layout = info.get('layout') or 'tabular'
        column_map = info.get('column_map') or {}
        rows = workbook_data['data'][sn]['rows']
        if not domain:
            per_sheet[sn] = {'domain': None, 'skipped': True}
            print(f"  {sn:30}  SKIPPED (no domain)")
            continue
        out = extract_sheet(rows, layout, column_map, sn, domain=domain)
        out['domain'] = domain
        out['layout'] = layout
        per_sheet[sn] = out
        n_rows = len(out.get('rows', []))
        n_events = len(out.get('events', []))
        n_line = len(out.get('line_items', []))
        n_kv = len(out.get('kv', {})) if out.get('kv') else 0
        print(f"  {sn:30}  rows={n_rows:4}  events={n_events:3}  line_items={n_line:3}  kv={n_kv:3}")

    per_sheet_dump = {}
    for sn, info in per_sheet.items():
        per_sheet_dump[sn] = {
            'domain': info.get('domain'),
            'layout': info.get('layout'),
            'rows_count': len(info.get('rows', []) or []),
            'events_count': len(info.get('events', []) or []),
            'line_items_count': len(info.get('line_items', []) or []),
            'kv_count': len(info.get('kv', {}) or {}),
            'rows_sample': (info.get('rows') or [])[:5],
            'kv': info.get('kv', {}),
        }

    print("\n=== STAGE 3: unified_builder ===")
    workbook_data['__source_filepath__'] = filepath
    unified = build_unified_json(per_sheet, workbook_data)

    unified_summary = {}
    for key, val in unified.items():
        if isinstance(val, list):
            unified_summary[key] = {'count': len(val), 'sample': val[:2] if val else []}
        elif isinstance(val, dict):
            unified_summary[key] = {'keys': list(val.keys())[:20], 'sample': {k: val[k] for k in list(val.keys())[:5]}}
        else:
            unified_summary[key] = val

    for key in ['fund', 'scheme', 'portfolio_companies', 'investments', 'valuations',
                'portfolio_kpis_periodic', 'commitments', 'capital_calls', 'distributions',
                'nav_records', 'budget_vs_actual', 'exits', 'waterfall_carry']:
        val = unified.get(key)
        if val is None:
            print(f"  {key:30}  <missing>")
        elif isinstance(val, list):
            print(f"  {key:30}  count={len(val)}")
        elif isinstance(val, dict):
            print(f"  {key:30}  keys={list(val.keys())[:8]}")
        else:
            print(f"  {key:30}  {val!r}")

    # Extra focus: look at valuations rows and their linkage keys
    print("\n=== DEEP DIVE: valuations[] ===")
    for i, v in enumerate((unified.get('valuations') or [])[:6]):
        print(f"  #{i}: {v}")
    print(f"  Total valuations in unified_json: {len(unified.get('valuations') or [])}")

    print("\n=== DEEP DIVE: scheme fields (hurdle_rate_pct etc) ===")
    scheme_dict = unified.get('scheme') or {}
    for k in ['hurdle_rate_pct', 'carry_pct', 'management_fee_pct',
              'catch_up_pct', 'sponsor_commitment_pct']:
        print(f"  {k}: {scheme_dict.get(k)!r}")

    print("\n=== DEEP DIVE: portfolio_kpis_periodic (looking for ARR) ===")
    kpis = unified.get('portfolio_kpis_periodic') or []
    print(f"  Total KPI rows: {len(kpis)}")
    for kpi in kpis[:6]:
        print(f"  {kpi}")
    arr_rows = [k for k in kpis if k.get('arr') is not None]
    print(f"  Rows with ARR set: {len(arr_rows)}")

    full = {
        'file': filepath,
        'sheet_names': sheet_names,
        'stage1_classification': stage1_dump,
        'stage2_per_sheet': per_sheet_dump,
        'stage3_unified_summary': unified_summary,
        'unified_valuations_sample': (unified.get('valuations') or [])[:20],
        'unified_scheme': unified.get('scheme') or {},
        'unified_portfolio_kpis_periodic_sample': (unified.get('portfolio_kpis_periodic') or [])[:20],
    }
    with open(out_path, 'w') as f:
        json.dump(full, f, indent=2, default=_serialize)
    print(f"\n✅ Full diagnosis written to {out_path}")
    print(f"   File size: {os.path.getsize(out_path):,} bytes")

    try:
        _evict_cache(filepath)
    except Exception:
        pass

    return full
