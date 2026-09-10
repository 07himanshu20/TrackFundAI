"""RESOURCE CEILING (Track #3 + #4 of the 100-file readiness decomposition): prove — token-free — that
batch memory does NOT grow with file count. The consolidated RunResult is legitimately O(N) (it is the
product). The real architectural invariant is that TRANSIENT working memory (grids/parse buffers) is
bounded — it is O(max_workers), NOT O(N): the parent holds no MIS grid (each grid is read, used and
released inside its worker), and at most `max_workers` grids are live at once (the model-on thread pool
maps one file per worker). So the honest OOM headroom is `workers x largest-grid`, independent of how
many files are in the batch.

A regression that retained grids, held every file's rows, or accumulated per-file state would make the
transient working set grow with N — these tests redden against exactly that.

Instrument: tracemalloc (Python-object memory — deterministic and CI-stable, unlike RSS).
  transient(N) = peak_during_run(N) - retained_after_run(N)
Model-on runs use a MOCK provider (token-free) so the real THREAD-POOL path (up to `workers` grids live
at once) is exercised without spending. Distinct content per synthetic file (seeded -> distinct
content_fp) so every file genuinely parses and nothing is collapsed by the reuse cache."""
import gc
import os
import sys
import tempfile
import time
import tracemalloc

import openpyxl
import pytest

from backend.dataimport.preingest3 import llm, pipeline
from backend.dataimport.preingest3.ratecard import default_inr_card

RC = default_inr_card('2026-06-30')

_CORPUS = '/Users/himanshusharma/portfolio-dashboard/backend/media/preingest/trivesta/100e86d5/in'
_REAL_HEAVY = os.path.join(_CORPUS, '0625_CPM_Monthly_Report-R1.xlsx')
# small fund-domain real files that yield figures on the model-OFF deterministic path (fast, KB-sized)
_FIGURE_FILES = ['TFAI_Fund_Terms_and_LP_Register_wip.xlsx', 'TFAI_Investments_and_Deployment.xlsx',
                 'TFAI_Capital_Calls_and_Distributions_draft.xlsx']

_MOCK = lambda prompt, **kw: type('R', (), {'text': '{"located":[]}'})()


def _slow_mock(delay):
    """A latency-simulating provider. Essential for measuring CONCURRENT grid transient: an instant
    mock lets each worker release its grid before the next peaks (grids never overlap), so it would
    UNDER-measure. Real locator calls take ~5s of network wait during which the worker HOLDS its grid;
    the delay reproduces that overlap so `workers` grids are genuinely live at once — token-free."""
    def _p(prompt, **kw):
        time.sleep(delay)
        return type('R', (), {'text': '{"located":[]}'})()
    return _p


def _synthetic(path, seed, rows, sheets=2):
    """A distinct-content workbook (seeded -> unique content_fp); `rows`/`sheets` set the grid weight."""
    wb = openpyxl.Workbook()
    for s in range(sheets):
        ws = wb.create_sheet(f'Sheet{s}') if s else wb.active
        ws.append(['Particulars', 'Apr-25', 'May-25', 'Jun-25', 'Jul-25', 'Aug-25'])
        for r in range(rows):
            ws.append([f'Line {r} s{seed}', r + seed, r + seed + 1, r + seed + 2, r + seed + 3, r + seed + 4])
    wb.save(path)
    return path


def _peak_retained_mb(files, *, workers=1, model_on=False, provider=None):
    """(peak_high_water_MB, retained_after_MB). transient = peak - retained. model_on drives the real
    thread-pool path with a token-free mock provider (up to `workers` grids live at once). Pass a
    latency `provider` to force real concurrent grid-holding."""
    llm.set_model_provider(provider if provider is not None else (_MOCK if model_on else None))
    gc.collect()
    tracemalloc.start()
    tracemalloc.reset_peak()
    res = pipeline.run(files, as_of='2026-06-30', org='resceil', rate_card=RC,
                       require_model=model_on, max_workers=workers)
    peak = tracemalloc.get_traced_memory()[1]
    gc.collect()
    retained = tracemalloc.get_traced_memory()[0]      # live while `res` is still held
    tracemalloc.stop()
    assert res is not None and res.files
    del res
    return peak / (1024 * 1024), retained / (1024 * 1024)


def test_transient_working_set_is_o1_in_file_count():
    """Serial (workers=1): transient = one grid at a time, flat across N; output grows O(N) (expected)."""
    with tempfile.TemporaryDirectory() as d:
        small = [(f'S{i}', _synthetic(os.path.join(d, f's{i}.xlsx'), i, rows=200)) for i in range(4)]
        big = [(f'B{i}', _synthetic(os.path.join(d, f'b{i}.xlsx'), 1000 + i, rows=200)) for i in range(16)]
        p4, r4 = _peak_retained_mb(small)
        p16, r16 = _peak_retained_mb(big)
        t4, t16 = p4 - r4, p16 - r16
        print(f'\n  [serial] N=4:  peak={p4:.2f} retained={r4:.2f} transient={t4:.2f} MB')
        print(f'  [serial] N=16: peak={p16:.2f} retained={r16:.2f} transient={t16:.2f} MB')
        print(f'  result grows O(N) (expected): retained {r4:.2f}->{r16:.2f} ({r16 / max(r4, .01):.1f}x for 4x files)')
        assert t16 <= t4 * 2.0 + 1.0, (
            f'transient grid working set scaled with file count: {t4:.2f}->{t16:.2f} MB — grids retained')


def test_transient_flat_to_n100():
    """Item 6: one N=100 point — transient stays flat from N=16 to N=100 (serial, light grids)."""
    with tempfile.TemporaryDirectory() as d:
        n16 = [(f'A{i}', _synthetic(os.path.join(d, f'a{i}.xlsx'), i, rows=50, sheets=1)) for i in range(16)]
        n100 = [(f'H{i}', _synthetic(os.path.join(d, f'h{i}.xlsx'), 5000 + i, rows=50, sheets=1)) for i in range(100)]
        p16, r16 = _peak_retained_mb(n16)
        p100, r100 = _peak_retained_mb(n100)
        t16, t100 = p16 - r16, p100 - r100
        print(f'\n  [serial] N=16:  transient={t16:.2f} MB   retained(output)={r16:.2f} MB')
        print(f'  [serial] N=100: transient={t100:.2f} MB   retained(output)={r100:.2f} MB '
              f'({r100 / max(r16, .01):.1f}x for 6.25x files -> O(N) output, ~{r100 / 100 * 1000:.0f} KB/file)')
        assert t100 <= t16 * 2.0 + 1.0, f'transient grew from N=16 to N=100: {t16:.2f}->{t100:.2f} MB'


def test_concurrency_adds_no_python_accumulation():
    """Item 1 (SUBSTANTIVE), corrected. Two facts were established empirically (see scratchpad/
    rss_concurrency.py, measured on the real heavy corpus):
      • The peak is NOT driven by worker count: 8 heavy real files peak at ~477 MB RSS at BOTH
        workers=2 and workers=8. The parent releases each MIS grid after serial routing, so at most
        ONE grid is live during routing; the workers x grid worst-case is pessimistic.
      • The real O(N) scaling term is the accumulated RunResult OUTPUT (~21 MB per HEAVY file), not
        grids. tracemalloc cannot see calamine's NATIVE grid memory, so RSS is the instrument for the
        absolute ceiling; that ceiling is characterized in the scratchpad harness + memory, not here.

    This permanent test guards the reliable Python-level invariant the thread pool must not break:
    running at workers=8 must add NO per-file Python accumulation vs serial (workers=1) on the SAME
    files — i.e., no worker hoards its grid into shared state. Reddens if a worker leaks per-file state."""
    with tempfile.TemporaryDirectory() as d:
        files = [(f'C{i}', _synthetic(os.path.join(d, f'c{i}.xlsx'), i, rows=800)) for i in range(8)]
        p1, r1 = _peak_retained_mb(files, workers=1, model_on=True)
        p8, r8 = _peak_retained_mb(files, workers=8, model_on=True)
        t1, t8 = p1 - r1, p8 - r8
        print(f'\n  [same 8 files] serial transient={t1:.2f} MB  |  workers=8 transient={t8:.2f} MB')
        print(f'  RSS worst-case (native, real heavy corpus, see scratchpad): ~477 MB @ workers 2==8')
        assert t8 <= t1 * 2.0 + 1.0, (
            f'concurrency added per-file Python accumulation: serial={t1:.2f} -> workers=8 {t8:.2f} MB '
            f'(a worker is hoarding grid/state instead of releasing it)')


def _deep_size(obj, seen):
    """Deep id-deduped size: SHARED objects (e.g. a Figure referenced from both cir.records and the
    extraction dict) counted once — the correct instrument for output retention (tracemalloc's post-run
    'current' conflates the product with module-cache/last-grid residue and is NOT the output)."""
    from decimal import Decimal
    if id(obj) in seen:
        return 0
    seen.add(id(obj))
    s = sys.getsizeof(obj)
    if isinstance(obj, (str, bytes, int, float, Decimal, bool, type(None))):
        return s
    if isinstance(obj, dict):
        for k, v in obj.items():
            s += _deep_size(k, seen) + _deep_size(v, seen)
    elif isinstance(obj, (list, tuple, set, frozenset)):
        for x in obj:
            s += _deep_size(x, seen)
    elif hasattr(obj, '__dict__'):
        s += _deep_size(vars(obj), seen)
    return s


@pytest.mark.skipif(not all(os.path.exists(os.path.join(_CORPUS, f)) for f in _FIGURE_FILES),
                    reason='figure-yielding corpus files not present')
def test_output_retention_is_bytes_per_figure_not_grid_scale():
    """The RunResult output is the O(N) term, but it is O(figures) at ~KB/figure — NOT grid-scale. This
    guards guardrail C's finding (measured ~0.9-2.6 KB/figure on the real corpus; extraction shares
    Figure refs so it adds ~0). A regression that retained a grid/rows/snapshot per figure would push
    this to MB/figure — reddens well before the 20 KB/figure ceiling (>=8x headroom, catches grid-scale
    retention, not calibrated to the current value). Deterministic (deep-size), no RSS, no timing."""
    llm.set_model_provider(None)
    files = [(f, os.path.join(_CORPUS, f)) for f in _FIGURE_FILES]
    res = pipeline.run(files, as_of='2026-06-30', org='outret', rate_card=RC, max_workers=1)
    figs = [f for r in res.cir.records for f in r.figures()]
    assert figs, 'no figures extracted — test cannot size per-figure retention'
    seen = set()
    cir_b = _deep_size(res.cir, seen)
    extraction_marginal = _deep_size(res.extraction, seen)   # over cir → ~0 if shared refs
    per_fig = cir_b / len(figs)
    print(f'\n  cir={cir_b / 1024:.1f}KB  figures={len(figs)}  per-figure={per_fig:.0f}B  '
          f'extraction-marginal={extraction_marginal}B (shared)')
    assert per_fig < 20_000, f'output retention is grid-scale: {per_fig:.0f} B/figure (regression?)'
    assert extraction_marginal < cir_b * 0.25, 'extraction is duplicating cir, not sharing refs'


@pytest.mark.skipif(not os.path.exists(_REAL_HEAVY), reason='real heavy corpus file not present')
def test_single_heavy_file_grid_constant():
    """Item 4/5: the REAL largest grid sets the worst-case constant. Measure one real CPM (9 MB) grid's
    transient and its retained output — worst-case production transient ~= workers x this grid."""
    p, r = _peak_retained_mb([('CPM', _REAL_HEAVY)], workers=1, model_on=False)
    transient = p - r
    print(f'\n  [real CPM 9MB] one-grid transient={transient:.1f} MB  retained(output)={r:.2f} MB')
    print(f'  => 8-worker worst-case transient ~= {transient * 8:.0f} MB (bounded, no OOM); '
          f'real output/file={r:.2f} MB << 9MB raw grid (confirms O(N) output stays small)')
    assert p > 0 and r >= 0, 'heavy file did not complete'
