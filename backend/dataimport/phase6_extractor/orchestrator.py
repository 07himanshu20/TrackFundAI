"""
Phase 6 Orchestrator — main entry point: run_phase6_import(import_file, progress_cb).

Pipeline:
  1. Open workbook, encrypted-file guard, eager-load into workbook cache
  2. Stage 1 — ONE Gemini call classifies every sheet (domain, layout, column_map)
  3. Stage 2 — Python reads every row deterministically via the column_map.
              Four layouts: tabular, key_value, wide_period, entity_pivoted.
              No Gemini for rows. Zero truncation.
  4. Stage 3 — Assemble persister-shaped unified_json (route domains, translate
              label slugs → persister field names, merge LP line items into
              commitments).
  5. Stage 4 — persist_phase2() writes to DB, then Phase 4 derivations compute
              per-investment IRR/MOIC + fund tiles (Net IRR/MOIC/TVPI/DPI/RVPI)
              + European whole-fund waterfall.

Semantic decisions → Gemini (once). Bulk data movement → Python (zero calls).
Universal across any sheet name / column name / layout.
"""
import json
import logging
import os
import time
from typing import Callable, Optional

from django.utils import timezone

from ..phase3_layers.workbook_cache import load_workbook, evict as _evict_cache
from .stage1_classifier import run_stage1
from .extractors import extract_sheet
from .unified_builder import build_unified_json

logger = logging.getLogger(__name__)


_SENTINEL_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SENTINEL_PATH = os.path.join(_SENTINEL_DIR, '.import_active')


def _write_sentinel(import_file_id):
    try:
        with open(_SENTINEL_PATH, 'w') as f:
            f.write(f'{import_file_id}\n{timezone.now().isoformat()}\n')
    except Exception:
        pass


def _delete_sentinel():
    try:
        if os.path.exists(_SENTINEL_PATH):
            os.remove(_SENTINEL_PATH)
    except Exception:
        pass


def run_phase6_import(import_file, progress_cb: Optional[Callable] = None):
    """Phase 6 import pipeline. Same contract as run_phase3_import /
    run_phase2_import — returns a dict with status, extractor, wall_time_s,
    persist."""
    from ..phase2_persister import persist_phase2

    def _p(pct: int, msg: str):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass
        logger.info(f'[Phase6 {pct}%] {msg}')

    started_at = time.time()
    _write_sentinel(import_file.id)
    filepath = ''

    try:
        import_file.status = 'importing'
        import_file.save(update_fields=['status'])

        _p(3, 'Phase 6: opening workbook…')
        filepath = import_file.file.path

        try:
            workbook_data = load_workbook(filepath)
        except Exception as e:
            err = f'workbook open failed: {type(e).__name__}: {e}'
            if any(t in str(e).lower() for t in ('encrypted', 'password', 'invalid', 'badzipfile')):
                err = (f'workbook is encrypted, password-protected, or corrupted '
                       f'({type(e).__name__}: {e}). Please save as plain .xlsx and re-upload.')
            logger.error(f'[Phase6] {err}')
            import_file.status = 'failed'
            import_file.error_detail = err
            import_file.save(update_fields=['status', 'error_detail'])
            return {'status': 'failed', 'error': 'workbook_open_failed', 'detail': err}

        sheet_names = workbook_data['sheets']
        _p(8, f'Phase 6: loaded {len(sheet_names)} sheets from workbook')

        # ── Stage 1 — ONE Gemini call ────────────────────────────────────────
        _p(12, f'Phase 6 Stage 1: classifying {len(sheet_names)} sheets via Gemini (one call)…')
        stage1_t0 = time.time()
        stage1 = run_stage1(workbook_data)
        stage1_elapsed = time.time() - stage1_t0
        sheets_map = stage1.get('sheets') or {}
        classified = sum(1 for sn in sheet_names if sheets_map.get(sn, {}).get('domain'))
        _p(45, f'Phase 6 Stage 1 done in {stage1_elapsed:.1f}s '
               f'— {classified}/{len(sheet_names)} sheets classified')

        # ── Stage 2 — deterministic Python extraction ────────────────────────
        _p(50, 'Phase 6 Stage 2: extracting rows deterministically (no Gemini)…')
        stage2_t0 = time.time()
        per_sheet: dict = {}
        for sn in sheet_names:
            info = sheets_map.get(sn) or {}
            domain = info.get('domain')
            layout = info.get('layout') or 'tabular'
            column_map = info.get('column_map') or {}
            rows = workbook_data['data'][sn]['rows']
            if not domain:
                per_sheet[sn] = {'domain': None}
                continue
            out = extract_sheet(rows, layout, column_map, sn, domain=domain)
            out['domain'] = domain
            out['layout'] = layout
            per_sheet[sn] = out
        stage2_elapsed = time.time() - stage2_t0

        total_rows = sum(
            len(info.get('rows', []))
            + len(info.get('events', []))
            + len(info.get('line_items', []))
            + (1 if info.get('kv') else 0)
            for info in per_sheet.values()
        )
        _p(65, f'Phase 6 Stage 2 done in {stage2_elapsed:.2f}s '
               f'— {total_rows:,} records extracted')

        # ── Stage 3 — build unified_json ─────────────────────────────────────
        _p(72, 'Phase 6 Stage 3: assembling unified JSON for persister…')
        workbook_data['__source_filepath__'] = filepath
        unified = build_unified_json(per_sheet, workbook_data)

        counts = {k: (len(v) if isinstance(v, list) else 0)
                  for k, v in unified.items() if isinstance(v, list)}
        logger.info(f'[phase6] unified JSON: '
                    f'inv={counts.get("portfolio_investments", 0)} '
                    f'lps={counts.get("investors", 0)} '
                    f'cc={counts.get("capital_calls", 0)} '
                    f'exits={counts.get("exits", 0)} '
                    f'dist={counts.get("distributions", 0)} '
                    f'val={counts.get("valuations", 0)} '
                    f'nav={counts.get("nav_records", 0)}')

        # I5 — fund_master fallback: only stamp filename when Gemini truly
        # failed to identify the fund. If the fund_master domain never got
        # classified, use the filename stem; otherwise trust Gemini's output.
        fm = unified.setdefault('fund_master', {})
        if not fm.get('fund_name'):
            stem = (import_file.original_filename or 'Unnamed Fund').rsplit('.', 1)[0]
            fm['fund_name'] = stem
            unified.setdefault('__phase6_diagnostics__', {})['fund_master_fallback'] = (
                f'fund_name defaulted from filename: {stem!r}'
            )

        unified['__phase6_diagnostics__'] = {
            'stage1_gemini_s': round(stage1_elapsed, 2),
            'stage2_python_s': round(stage2_elapsed, 2),
            'sheets_total': len(sheet_names),
            'sheets_classified': classified,
            'total_records_extracted': total_rows,
        }

        # ── Save unified JSON for debugging ──────────────────────────────────
        try:
            from django.conf import settings as _dj_settings
            out_dir = os.path.join(_dj_settings.MEDIA_ROOT, 'dataimport', '_phase6_outputs')
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(
                out_dir,
                f'{import_file.id}_{import_file.original_filename}.json',
            )
            with open(out_path, 'w', encoding='utf-8') as f:
                json.dump(unified, f, indent=2, default=str, ensure_ascii=False)
            logger.info(f'[Phase 6] unified JSON saved → {out_path}')
        except Exception as e:
            logger.warning(f'[Phase 6] could not save unified JSON: {e}')

        # ── Stage 4 — persist + Phase 4 derivations ──────────────────────────
        _p(80, 'Phase 6 Stage 4: persisting to database (Phase 4 derivations fire)…')
        organization = import_file.job.organization
        user = import_file.job.uploaded_by
        persist_result = persist_phase2(
            unified, import_file, organization, user, progress_cb=_p,
        )

        elapsed = round(time.time() - started_at, 2)
        _p(100, f'Phase 6: complete in {elapsed}s '
                f'(stage1={stage1_elapsed:.1f}s, stage2={stage2_elapsed:.2f}s)')

        import_file.status = 'completed'
        import_file.completed_at = timezone.now()
        import_file.save(update_fields=['status', 'completed_at'])

        return {
            'status': 'completed',
            'extractor': 'phase6',
            'stage1_gemini_s': round(stage1_elapsed, 2),
            'stage2_python_s': round(stage2_elapsed, 2),
            'sheets_classified': classified,
            'total_records_extracted': total_rows,
            'wall_time_s': elapsed,
            'persist': persist_result,
        }
    finally:
        _delete_sentinel()
        try:
            _evict_cache(filepath or '')
        except Exception:
            pass
