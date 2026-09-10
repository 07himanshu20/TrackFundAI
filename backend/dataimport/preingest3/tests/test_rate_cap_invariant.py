"""RATE-CAP INVARIANT (Track #1 of the 100-file readiness decomposition): prove — token-free — that
under 100-file request VOLUME the governor holds the aggregate issue rate at/below its cap, so a 429
can never be CAUSED by file count. 429 is a function of issue-rate vs the Vertex ceiling, not of how
many files are in the batch; with the issue-rate provably capped, file count can only add wall-time
(queue depth), never rejections. This is provable NOW, before the real ceiling is known, and with NO
real files and NO tokens.

Three independent facts, each the cheapest valid instrument:

  1. SINGLE GOVERNED DOOR (structural, universal): the model provider is invoked in exactly two places
     — health_check (one door-check per run, before the batch) and call_json (every batch issue) — and
     call_json acquires from the governor before it issues. So gating call_json gates ALL per-file
     model traffic. This test REDDENS if anyone adds a new, ungoverned provider call site, or removes
     the acquire from call_json — the universal guard against a bypass.

  2. AGGREGATE RATE CAPPED UNDER CONCURRENT VOLUME (through the real call_json chokepoint): many worker
     threads issuing at once are shaped to <= cap + burst in any window, and the batch takes the
     throttled wall-time (proving it did not flood). The safety direction is an UPPER bound on the rate,
     so timing jitter can only make the batch slower — it can never false-fail this assertion.

  3. GOVERNOR-ON IS BYTE-IDENTICAL AT VOLUME (through the pipeline): turning the governor on changes
     timing only, never the RunResult — re-confirmed at N-file scale on the real production path.

The absolute call VOLUME for a real 100-file run is a projection; the rate INVARIANT holds for ANY
volume by construction (token bucket). See [[project_model_migration]], [[project_parallelism_plan]]."""
import ast
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import openpyxl
import pytest

from backend.dataimport.preingest3 import dualrun, llm, pipeline
from backend.dataimport.preingest3 import rate_governor as rg
from backend.dataimport.preingest3.ratecard import default_inr_card

RC = default_inr_card('2026-06-30')


@pytest.fixture(autouse=True)
def _restore_global_state():
    """Every test here mutates process-global state (the governor singleton, the injected provider,
    the cache dir). Snapshot and restore so tests stay order-independent."""
    prov = llm._MODEL_PROVIDER
    gov = rg._GOVERNOR
    cache = llm._CACHE_DIR
    try:
        yield
    finally:
        llm._MODEL_PROVIDER = prov
        rg._GOVERNOR = gov
        llm._CACHE_DIR = cache


# ── 1. SINGLE GOVERNED DOOR — structural universal guard ─────────────────────────────────────
def test_provider_has_one_governed_door_and_calljson_acquires():
    """AST of llm.py: every `provider(...)` call lives inside call_json or health_check, and call_json
    contains a rate_governor.acquire(...) call. A new ungoverned door → this reddens."""
    src = open(llm.__file__, encoding='utf-8').read()
    tree = ast.parse(src)

    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert 'call_json' in funcs and 'health_check' in funcs

    def _calls_named(node, name):
        return [c for c in ast.walk(node)
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == name]

    def _enclosing_func(target):
        for fn in funcs.values():
            if any(c is target for c in ast.walk(fn)):
                return fn.name
        return None

    provider_calls = [c for c in ast.walk(tree)
                      if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == 'provider']
    assert provider_calls, 'no provider(...) invocation found — test is stale, re-derive the doors'
    doors = {_enclosing_func(c) for c in provider_calls}
    assert doors <= {'call_json', 'health_check'}, (
        f'UNGOVERNED DOOR: provider(...) invoked outside call_json/health_check in {doors}')

    # call_json must acquire from the governor (attribute call rate_governor.acquire(...))
    acquires = [c for c in ast.walk(funcs['call_json'])
                if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == 'acquire']
    assert acquires, 'call_json no longer calls rate_governor.acquire — the batch door is ungoverned'


def test_health_check_is_the_only_ungoverned_door_and_is_bounded_to_one_per_run():
    """Item 2: the 'single door' is really two — call_json (governed, per-file) + health_check (the
    door-check, deliberately UNGOVERNED because it is a config probe, not batch traffic). That is safe
    ONLY if it is bounded to <=1 issue per run, so it can never become uncounted rate under load. Assert
    the bound: a whole pipeline.run issues the health prompt exactly once, no matter how many files."""
    calls = {'health': 0, 'other': 0}

    def _provider(prompt, **kw):
        if prompt == llm._HEALTH_PROMPT:
            calls['health'] += 1
        else:
            calls['other'] += 1
        return type('R', (), {'text': '{"located":[]}'})()

    llm.set_model_provider(_provider)
    rg.configure()                                             # governor off → provider is reachable
    with tempfile.TemporaryDirectory() as d:
        files = [(f'Co{i}', _xlsx(os.path.join(d, f'f{i}.xlsx'), i)) for i in range(5)]
        pipeline.run(files, as_of='2026-06-30', org='healthdoor', rate_card=RC,
                     model_provider=None, require_model=True, max_workers=3)
    assert calls['health'] == 1, f'health_check (ungoverned door) issued {calls["health"]} times, not 1/run'


# ── 2. AGGREGATE RATE CAPPED UNDER CONCURRENT VOLUME (through call_json) ──────────────────────
def test_aggregate_rate_capped_under_concurrent_volume():
    # SCOPE (item 7): use_cache=False here = the CONSERVATIVE worst case — every call hits the governor.
    # In production the cache is checked BEFORE acquire (llm.py), so cache hits skip the governor entirely
    # and real load is strictly LOWER than measured here. This proves the ceiling under the heaviest case.
    rate_per_s, burst, N, workers = 10.0, 3, 48, 8
    rg.configure(rpm=int(rate_per_s * 60), burst=burst, max_wait_s=30.0)

    issues = []
    lock = threading.Lock()

    def _provider(prompt, **kw):
        with lock:
            issues.append(time.monotonic())
        return type('R', (), {'text': '{}'})()

    llm.set_model_provider(_provider)

    def _one(i):
        return llm.call_json('locate_rows', f'sig{i}', 'p' * 200, use_cache=False)

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(_one, range(N)))
    wall = time.monotonic() - t0

    assert all(r.kind == llm.OK for r in results), 'a call held under a generous budget'
    assert len(issues) == N, f'expected {N} issues, got {len(issues)}'

    # (a) threading did NOT flood: the batch took roughly the throttled wall-time (N-burst)/rate.
    min_wall = (N - burst) / rate_per_s * 0.6
    assert wall >= min_wall, f'no throttling: {wall:.2f}s < {min_wall:.2f}s (would-flood baseline is ~0s)'

    # (b) windowed cap: issues in any 1s window <= burst + rate + slack (an UPPER bound → jitter-safe).
    ts = sorted(t - t0 for t in issues)
    cap = burst + rate_per_s + 3
    peak = max(sum(1 for u in ts if t <= u < t + 1.0) for t in ts)
    assert peak <= cap, f'windowed rate {peak} exceeded cap {cap} — burst not shaped'


# ── 3. GOVERNOR-ON IS BYTE-IDENTICAL AT VOLUME (through the pipeline) ─────────────────────────
def _xlsx(path, seed):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(['Particulars', 'Apr-25', 'May-25'])
    ws.append(['Revenue', 10 + seed, 11 + seed])
    ws.append(['EBITDA', 2 + seed, 3 + seed])
    wb.save(path); return path


def _sig(files, *, governed, cache_dir):
    llm._CACHE_DIR = cache_dir
    if governed:
        rg.configure(rpm=100000, tpm=100000000, max_wait_s=30.0)   # generous → no holds, pure timing
    else:
        rg.configure()                                             # disabled = passthrough
    llm.set_model_provider(lambda prompt, **kw: type('R', (), {'text': '{"located":[]}'})())
    res = pipeline.run(files, as_of='2026-06-30', org='ratecap', rate_card=RC,
                       model_provider=None, require_model=True, max_workers=4)
    return dualrun.signature(res)


def test_governor_on_is_byte_identical_at_volume():
    with tempfile.TemporaryDirectory() as d:
        files = [(f'Co{i}', _xlsx(os.path.join(d, f'f{i}.xlsx'), i)) for i in range(6)]
        off = _sig(files, governed=False, cache_dir=os.path.join(d, 'c_off'))
        on = _sig(files, governed=True, cache_dir=os.path.join(d, 'c_on'))
        assert on == off, 'enabling the governor changed the RunResult — it must be timing-only'
