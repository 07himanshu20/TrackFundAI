"""
Orchestrator — the DAG (map-reduce shape).

MAP  : one bounded worker per file, in parallel, with a concurrency cap AND a
       hard per-file wall-clock timeout. A file whose extraction hangs is
       ABANDONED after the deadline and recorded as failed — the run never
       freezes on a stuck model call (the failure mode we hit repeatedly).
REDUCE (barrier, deterministic code): merge fund records, normalize company
       rows, assemble → reconcile → verify → write the workbook.
"""
import logging
import multiprocessing as mp
import os
import time
from collections import defaultdict

from . import cache
from . import schema as sch
from . import worker as _worker
from .assembler import assemble
from .normalizer import normalize_company_row
from .reconciler import reconcile
from .verifier import verify
from .writer import build_workbook

logger = logging.getLogger(__name__)

CONCURRENCY = int(os.environ.get('PREINGEST2_CONCURRENCY', '3'))
# Progress-aware backstop. With per-CALL socket read timeouts now bounding every
# model call (see api.gemini_service), a file's worker should never stall
# silently. The kill fires only when a file makes NO progress for FILE_IDLE_S
# (reset on every heartbeat), or blows past the absolute FILE_HARD_S ceiling —
# never on a legitimately large-but-progressing workbook.
FILE_IDLE_S = int(os.environ.get('PREINGEST2_FILE_IDLE_S', '150'))
FILE_HARD_S = int(os.environ.get('PREINGEST2_FILE_HARD_S', '1200'))
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _extract_all(files, progress):
    """MAP phase — one KILLABLE subprocess per file, capped concurrency.
    A file whose extraction hangs is terminated at the deadline, freeing the
    slot (no pool starvation). Returns (results_by_label, audit)."""
    ctx = mp.get_context('spawn')
    os.environ['PYTHONPATH'] = _BACKEND_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')
    n = len(files)
    pending = list(files)
    running = []          # [proc, queue, label, start, path]
    results, audit = {}, []
    done = 0

    def _spawn(lbl, p):
        q = ctx.Queue()
        proc = ctx.Process(target=_worker.run, args=(lbl, p, _BACKEND_ROOT, q), daemon=True)
        proc.start()
        now = time.time()
        # item = [proc, q, lbl, started_at, path, last_progress_at]
        running.append([proc, q, lbl, now, p, now])

    def _drain_one(item):
        """Pull at most one message; return True if the file finished (ok/error)."""
        proc, q, lbl, start, p, _last = item
        try:
            payload = q.get_nowait()
        except Exception:  # queue empty
            return False
        kind, data = payload
        if kind == 'hb':
            item[5] = time.time()  # progress → reset the idle deadline
            return False
        if kind == 'ok':
            results[lbl] = data
            audit.append({'file': lbl, 'file_class': data.get('file_class'),
                          'entity': data.get('entity'), 'cache': data.get('_cache'),
                          'status': 'ok', 'notes': ''})
        else:
            audit.append({'file': lbl, 'file_class': '?', 'entity': None,
                          'cache': '-', 'status': 'error', 'notes': str(data)[:200]})
        proc.join(2)
        running.remove(item)
        return True

    while pending or running:
        while pending and len(running) < CONCURRENCY:
            _spawn(*pending.pop(0))
        time.sleep(0.5)
        for item in running[:]:
            proc, q, lbl, start, p, last = item
            # Drain all queued messages (heartbeats + terminal) so progress
            # pings can't back up behind a slow poll loop.
            finished = False
            while _drain_one(item):
                finished = True
                break
            if finished:
                done += 1
                if progress:
                    progress(int(5 + 70 * done / max(n, 1)), f'Extracted {done}/{n}')
                continue
            # refresh last-progress (may have been reset by a heartbeat above)
            last = item[5]
            idle = time.time() - last
            total = time.time() - start
            if not proc.is_alive():
                note = 'worker died'
            elif idle > FILE_IDLE_S:
                logger.warning(f'[preingest2] IDLE >{FILE_IDLE_S}s — killing {lbl}')
                proc.terminate()
                note = f'no progress for {int(idle)}s — killed (backstop)'
            elif total > FILE_HARD_S:
                logger.warning(f'[preingest2] HARD >{FILE_HARD_S}s — killing {lbl}')
                proc.terminate()
                note = f'>{FILE_HARD_S}s absolute ceiling — killed (backstop)'
            else:
                continue
            proc.join(2)
            audit.append({'file': lbl, 'file_class': '?', 'entity': None,
                          'cache': '-', 'status': 'timeout', 'notes': note})
            running.remove(item)
            done += 1
            if progress:
                progress(int(5 + 70 * done / max(n, 1)), f'Extracted {done}/{n}')
    return results, audit


def consolidate(files, progress=None):
    """files = [(label, path)]. Returns (workbook, summary)."""
    n = len(files)
    if progress:
        progress(5, f'Extracting {n} files (killable, cap {CONCURRENCY})')
    results, audit = _extract_all(files, progress)

    # ── REDUCE (deterministic) ────────────────────────────────────────────
    dom = defaultdict(list)
    company_rows = []
    for lbl, rec in results.items():
        if rec.get('file_class') == 'mis' and rec.get('company_row'):
            company_rows.append(normalize_company_row(dict(rec['company_row'])))
        for d, recs in (rec.get('records_by_domain') or {}).items():
            dom[d].extend(recs)

    if progress:
        progress(80, 'Assembling Fund Master Workbook')
    assembled, metrics = assemble(dict(dom), company_rows)

    if progress:
        progress(88, 'Reconciling (tie-outs)')
    recon, recon_blocked = reconcile(dict(dom), metrics)

    if progress:
        progress(93, 'Verifying (spot-checks)')
    vchecks, v_blocked = verify(assembled, metrics)

    wb = build_workbook(assembled, audit, recon + vchecks)

    summary = {
        'files': n,
        'company_rows': len(company_rows),
        'fund_domains': {d: len(r) for d, r in dom.items()},
        'sheet_row_counts': {s['name']: len(assembled[s['key']]['rows'])
                             for s in sch.SHEETS},
        'audit': audit,
        'reconciliation': recon,
        'verification': vchecks,
        'blocked': recon_blocked or v_blocked,
        'cache': cache.stats(),
    }
    return wb, summary


def consolidate_to_path(files, out_path, progress=None):
    wb, summary = consolidate(files, progress=progress)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    wb.save(out_path)
    summary['output_path'] = out_path
    return summary
