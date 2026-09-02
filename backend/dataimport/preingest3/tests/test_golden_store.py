"""Reddening controls for the durable golden-record store (Phase 2.4 — the value/answer cache).

Proves the ONE property that makes caching safe: a remembered NUMBER is keyed by the full content
fingerprint, so any changed value cell is a guaranteed MISS (fresh read), never a stale hit — and the
NEGATIVE CONTROL shows that keying by layout instead (the forbidden bug) WOULD serve stale."""
import os
import shutil

import openpyxl
import pytest

from backend.dataimport.preingest3 import golden_store
from backend.dataimport.preingest3.identity import compute_identity, extraction_cache_key
from backend.dataimport.preingest3.cir import Record, Figure, Provenance

IN = 'backend/media/preingest/trivesta/100e86d5/in'
_SAMPLE = 'TFAI_Fund_Terms_and_LP_Register_wip.xlsx'
_real = pytest.mark.skipif(not os.path.isdir(IN), reason='real fixture files not present')


def _rec(company='LDC', cost='110'):
    from decimal import Decimal
    prov = Provenance(source_file='f', content_fingerprint='fp', sheet='S', cell='A1')
    return Record('portfolio_investments', entity_id=company,
                  fields={'company': company, 'cost': Figure('cost', Decimal(cost), None, prov)})


def _key(content_fp, **over):
    base = dict(as_of='2026-06-30', rate_card_id='rc', anchor_cr=None,
                domicile=None, use_model=False)
    base.update(over)
    return extraction_cache_key(content_fp, **base)


def _edit_first_number(path):
    """Change one numeric value cell (leaves the layout/structure identical)."""
    wb = openpyxl.load_workbook(path)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    cell.value = cell.value + 1
                    wb.save(path)
                    return True
    return False


# ══════════════════════════════════════════════════════════════════════════════════
# store round-trip + fail-safe
# ══════════════════════════════════════════════════════════════════════════════════
def test_store_roundtrip_and_miss(tmp_path):
    d = str(tmp_path)
    golden_store.put(d, 'k1', _rec())
    got = golden_store.get(d, 'k1')
    assert got is not None and got.entity_id == 'LDC'
    assert golden_store.get(d, 'k2') is None            # absent key → miss


def test_corrupt_entry_is_a_silent_miss(tmp_path):
    d = str(tmp_path)
    with open(os.path.join(d, 'bad.rec'), 'wb') as fh:
        fh.write(b'not a pickle')
    assert golden_store.get(d, 'bad') is None            # never raises


def test_load_and_save_round_trip_the_whole_dict(tmp_path):
    d = str(tmp_path)
    n = golden_store.save(d, {'a': _rec('A'), 'b': _rec('B')})
    assert n == 2
    loaded = golden_store.load(d)
    assert set(loaded) == {'a', 'b'} and loaded['a'].entity_id == 'A'


def test_disabled_store_is_a_noop(tmp_path):
    assert golden_store.get(None, 'k') is None
    golden_store.put(None, 'k', _rec())                  # no error, writes nothing
    assert golden_store.load(None) == {}


# ══════════════════════════════════════════════════════════════════════════════════
# ACCEPTANCE TEST 1 — a changed value cell is a guaranteed MISS (no stale number)
# ══════════════════════════════════════════════════════════════════════════════════
@_real
def test_changed_value_cell_changes_key_so_store_misses(tmp_path):
    src = os.path.join(IN, _SAMPLE)
    work = str(tmp_path / 'f.xlsx')
    shutil.copy(src, work)

    fp1 = compute_identity(_SAMPLE, work).content_fp
    key1 = _key(fp1)
    store = str(tmp_path / 'store')
    golden_store.put(store, key1, _rec())               # a finished answer for the original file
    assert golden_store.get(store, key1) is not None    # (warm hit on the unchanged file)

    assert _edit_first_number(work)                      # change ONE value cell
    fp2 = compute_identity(_SAMPLE, work).content_fp
    key2 = _key(fp2)

    assert fp1 != fp2                                    # value changed → content fingerprint changed
    assert key1 != key2                                  # → cache key changed
    assert golden_store.get(store, key2) is None         # → MISS → the pipeline recomputes fresh


# ══════════════════════════════════════════════════════════════════════════════════
# NEGATIVE CONTROL — keying by LAYOUT (the forbidden bug) would serve a STALE number
# ══════════════════════════════════════════════════════════════════════════════════
@_real
def test_layout_key_would_serve_stale_but_content_key_does_not(tmp_path):
    src = os.path.join(IN, _SAMPLE)
    work = str(tmp_path / 'f.xlsx')
    shutil.copy(src, work)
    before = compute_identity(_SAMPLE, work)
    assert _edit_first_number(work)
    after = compute_identity(_SAMPLE, work)
    # THE BUG the design forbids: layout fingerprint is UNCHANGED by a value edit → a layout-keyed
    # number cache would HIT and serve the OLD number.
    assert before.layout_fp == after.layout_fp
    # THE FIX: content fingerprint DOES change → the content-keyed store misses → fresh read.
    assert before.content_fp != after.content_fp


# ══════════════════════════════════════════════════════════════════════════════════
# SELF-ERASING — a logic-version change rebuilds the store
# ══════════════════════════════════════════════════════════════════════════════════
def test_logic_version_change_rebuilds_the_store(tmp_path):
    store = str(tmp_path / 'store')
    k_v1 = _key('CONTENT_FP', logic_version='lv_aaa')
    k_v2 = _key('CONTENT_FP', logic_version='lv_bbb')
    golden_store.put(store, k_v1, _rec())
    assert k_v1 != k_v2                                  # improved logic → new key
    assert golden_store.get(store, k_v2) is None         # old entry unreachable → recompute under new logic


def test_config_change_misses_even_on_identical_content(tmp_path):
    # a new rate card / anchor / domicile changes the key even though the file bytes are identical
    assert _key('SAME_FP', rate_card_id='rc1') != _key('SAME_FP', rate_card_id='rc2')
    assert _key('SAME_FP', anchor_cr=None) != _key('SAME_FP', anchor_cr='100')
    assert _key('SAME_FP', domicile=None) != _key('SAME_FP', domicile='USA')


# ══════════════════════════════════════════════════════════════════════════════════
# CROSS-TENANT ISOLATION (load-bearing) — the entry key is org-BLIND (extraction_cache_key excludes
# org), so two tenants uploading a byte-identical file produce the SAME key. The store must NEVER serve
# one tenant's finished answer to another: isolation is carried by a per-org namespace in the PATH.
# ══════════════════════════════════════════════════════════════════════════════════
def test_org_dir_is_deterministic_distinct_and_traversal_safe(tmp_path):
    base = str(tmp_path)
    assert golden_store.org_dir(None, 'o') is None               # no base → inactive (guards like store_dir)
    assert golden_store.org_dir(base, 'o') == golden_store.org_dir(base, 'o')     # stable across calls
    assert golden_store.org_dir(base, 'A') != golden_store.org_dir(base, 'B')     # distinct orgs → distinct dirs
    # a hostile org string is HASHED, never interpolated as a path → it can never escape the base dir
    evil = golden_store.org_dir(base, '../../etc/../x')
    assert os.path.commonpath([os.path.normpath(base), evil]) == os.path.normpath(base)


def test_identical_file_is_isolated_across_tenants(tmp_path):
    base = str(tmp_path / 'store')
    key = _key('SAME_FP')                                        # SAME content+config → SAME ck for both tenants
    da = golden_store.org_dir(base, 'orgA')
    db = golden_store.org_dir(base, 'orgB')
    assert da != db
    golden_store.put(da, key, _rec('CompanyA'))                 # org A caches its finished answer
    assert golden_store.get(da, key).entity_id == 'CompanyA'     # org A hits its own entry
    assert golden_store.get(db, key) is None                     # org B, IDENTICAL key, is served NOTHING (no leak)


def test_shared_base_would_leak_proving_namespacing_is_load_bearing(tmp_path):
    # NEGATIVE CONTROL: without the org namespace (put+get on the SAME base dir), the identical key HITS
    # across tenants — the exact cross-fund leak the org_dir namespace prevents. Reddens against the bug.
    base = str(tmp_path / 'store')
    key = _key('SAME_FP')
    golden_store.put(base, key, _rec('CompanyA'))               # (the forbidden shared-dir case)
    assert golden_store.get(base, key) is not None              # a second tenant with the same key WOULD be served this
    # …whereas org-scoped, the same key is isolated — proven in test_identical_file_is_isolated_across_tenants.


# ══════════════════════════════════════════════════════════════════════════════════
# PIPELINE INTEGRATION — warm run serves from the durable store; edit → fresh recompute
# ══════════════════════════════════════════════════════════════════════════════════
@_real
def test_identity_cache_skips_reparse_but_edit_forces_recompute(tmp_path, monkeypatch):
    from backend.dataimport.preingest3 import identity
    identity.clear_identity_cache()
    work = str(tmp_path / 'f.xlsx')
    shutil.copy(os.path.join(IN, _SAMPLE), work)

    calls = {'n': 0}
    real = identity._parse_workbook                        # reader-agnostic parse point (calamine or fallback)
    monkeypatch.setattr(identity, '_parse_workbook',
                        lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1) or real(*a, **k)))

    id1 = identity.compute_identity('f', work)
    id2 = identity.compute_identity('f', work)
    id3 = identity.compute_identity('f', work)
    assert id1.content_fp == id2.content_fp == id3.content_fp
    assert calls['n'] == 1                                # parsed ONCE despite 3 calls → cache hit

    assert _edit_first_number(work)                       # change bytes on disk (itself opens the wb)
    calls['n'] = 0                                        # isolate the recompute below
    id4 = identity.compute_identity('f', work)
    assert calls['n'] == 1                                # new bytes → exactly one re-parse (never stale)
    assert id4.content_fp != id1.content_fp               # fresh content fingerprint
    assert id4.layout_fp == id1.layout_fp                 # value edit doesn't move the layout


@_real
@pytest.mark.slow
def test_pipeline_warm_run_serves_from_store_and_edit_forces_recompute(tmp_path, monkeypatch):
    from backend.dataimport.preingest3 import pipeline, extract as _extract
    from backend.dataimport.preingest3.ratecard import default_inr_card

    files = [(f, os.path.join(IN, f)) for f in sorted(os.listdir(IN)) if f.endswith('.xlsx')
             and not f.startswith('~$')]
    store = str(tmp_path / 'store')
    rc = default_inr_card('2026-06-30')

    calls = {'n': 0}
    real = _extract.extract_company
    monkeypatch.setattr(_extract, 'extract_company', lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1) or real(*a, **k)))
    monkeypatch.setattr(pipeline, 'extract_company', _extract.extract_company)

    pipeline.run(files, as_of='2026-06-30', org='t', rate_card=rc, store_dir=store)
    cold = calls['n']
    _od = golden_store.org_dir(store, 't')                    # the pipeline namespaces the base by org
    assert cold > 0 and len(golden_store.load(_od)) > 0       # cold run computed + persisted UNDER the org namespace
    assert golden_store.load(store) == {}                    # …and NOTHING at the shared base (proves org-scoping)

    calls['n'] = 0
    pipeline.run(files, as_of='2026-06-30', org='t', rate_card=rc, store_dir=store)
    warm = calls['n']
    assert warm < cold                                       # warm run served attributions from the store


@_real
@pytest.mark.slow
def test_warm_run_output_is_byte_identical_to_cold(tmp_path):
    """WARM==COLD — the complement to acceptance test 1. Acceptance test 1 proves INVALIDATION (edit →
    miss → fresh read); this proves a HIT returns the SAME answer, not merely that a miss recomputes. A
    cold run (empty store) populates it; a warm run (same files, same config, same logic) serves the cached
    Records; the emitted master workbook must be BYTE-IDENTICAL — the cache changes speed, never numbers.
    The workbook is a pure function of the CIR, so this closes the loop on the store's core promise. (The
    currency_report is a separate RunResult field, intentionally observation-order dependent on a warm
    serve, and not part of the workbook — see pipeline.run's re-observation note — so it is out of scope
    here by design.)"""
    from backend.dataimport.preingest3 import pipeline, master_workbook as mw
    from backend.dataimport.preingest3.ratecard import default_inr_card

    files = [(f, os.path.join(IN, f)) for f in sorted(os.listdir(IN)) if f.endswith('.xlsx')
             and not f.startswith('~$')]
    manifest = sorted(f for f, _ in files)
    store = str(tmp_path / 'store')
    rc = default_inr_card('2026-06-30')

    def _wb_bytes(res, name):
        wb = mw.build_master(res.cir, rate_card=rc, files=manifest)
        p = str(tmp_path / name)
        mw.save_reproducible(wb, p)                           # byte-stable save (normalises zip timestamps)
        with open(p, 'rb') as fh:
            return fh.read()

    cold = pipeline.run(files, as_of='2026-06-30', org='warmcold', rate_card=rc, store_dir=store)
    assert len(golden_store.load(golden_store.org_dir(store, 'warmcold'))) > 0    # cold populated the store
    cold_bytes = _wb_bytes(cold, 'cold.xlsx')

    warm = pipeline.run(files, as_of='2026-06-30', org='warmcold', rate_card=rc, store_dir=store)
    warm_bytes = _wb_bytes(warm, 'warm.xlsx')

    assert warm_bytes == cold_bytes, \
        'warm run (served from the golden store) differs from cold — a cache HIT changed the answer'


# ══════════════════════════════════════════════════════════════════════════════════
# HOUSEKEEPING — the identity memo is LRU-BOUNDED (no unbounded growth) and eviction is
# value-neutral (an evicted file recomputes the IDENTICAL fingerprint, never a stale/wrong one).
# Reddens against the pre-cap plain dict: there len would reach 3 and 'a' would never re-parse.
# ══════════════════════════════════════════════════════════════════════════════════
def test_identity_cache_is_lru_bounded_and_eviction_recomputes_correctly(tmp_path, monkeypatch):
    from backend.dataimport.preingest3 import identity
    identity.clear_identity_cache()
    monkeypatch.setattr(identity, '_IDENTITY_CACHE_MAX', 2)          # tiny cap for the test

    def _mk(name, val):
        p = str(tmp_path / name)
        wb = openpyxl.Workbook(); wb.active['A1'] = val; wb.save(p)
        return p
    fa, fb, fc = _mk('a.xlsx', 'AA'), _mk('b.xlsx', 'BB'), _mk('c.xlsx', 'CC')

    fp_a = identity.compute_identity('a', fa).content_fp             # cache: {a}
    identity.compute_identity('b', fb)                              # cache: {a, b}
    identity.compute_identity('c', fc)                              # inserts c → evicts LRU (a)
    assert len(identity._IDENTITY_CACHE) == 2                        # BOUNDED (plain dict would be 3)

    calls = {'n': 0}
    real = identity._parse_workbook                        # reader-agnostic parse point (calamine or fallback)
    monkeypatch.setattr(identity, '_parse_workbook',
                        lambda *a, **k: (calls.__setitem__('n', calls['n'] + 1) or real(*a, **k)))
    identity.clear_parse_cache()                                     # parse-once: a real run starts with a COLD
    # per-run parse cache, so the recompute below is forced by the _IDENTITY_CACHE eviction — not served warm
    # by the unified parse cache. (Value-neutrality below holds regardless of which cache serves it.)
    id_a2 = identity.compute_identity('a', fa)
    assert calls['n'] == 1                                           # a was evicted → RE-parsed, not served stale
    assert id_a2.content_fp == fp_a                                  # recompute is IDENTICAL — never a wrong number
    identity.clear_parse_cache()
    identity.clear_identity_cache()
