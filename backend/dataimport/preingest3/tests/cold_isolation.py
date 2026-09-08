"""CANONICAL CACHE INVENTORY + cold/warm isolation for preingest3 MEASUREMENT harnesses.

WHY THIS EXISTS. Four separate measurements were contaminated by a persistent store the harness
forgot to isolate: (1) copied/duplicate input files, (2) a wrong golden baseline path, (3) the
model-call disk cache, (4) the layout-template registry. Each was caught only after it had spent a
run, because there was no single authoritative list of every memory the system keeps. THIS MODULE
IS THAT LIST, and `isolate_cold()` isolates every carrier BY CONSTRUCTION — so "cold" can never
again silently mean "warm in one store nobody remembered". Isolation uses FRESH TEMP PATHS; the real
media/* stores are never written, so a cold measurement leaves production state untouched.

════════════════════════════════════════════════════════════════════════════════════════════════
THE COMPLETE INVENTORY — every persistent / cross-run state in preingest3 (grep-verified 2026-09-08)
════════════════════════════════════════════════════════════════════════════════════════════════
A) MODEL-DRIVEN / BEHAVIOUR-CARRYING — these make a run "warm"; ALL must be isolated for a COLD run:
   1. llm._CACHE_DIR           disk  media/preingest3_calls/           prompt-hash -> model reply
   2. templates._STORE         disk  media/preingest3_templates.json   layout_fp -> located rows  [confound #4]
   3. golden_store (store_dir)  disk  <store_dir>/org-<tag>/            full extracted CIR per ck
                                       -> OFF when the caller passes store_dir=None (the default). A cold
                                          harness MUST pass store_dir=None; this module cannot enforce a
                                          call arg, so it ASSERTS the caller's intent via note only.
   4. lexicon._LEARNED_PATH     disk  media/preingest3_lexicon.json     reviewer-approved synonyms (Signal 2)
        + lexicon._syn_cache / lexicon._learned_cache  (in-proc memo of the above)
        -> a warm learned lexicon resolves more labels deterministically => FEWER model calls, so a warm
           lexicon makes a "cold" run unrealistically cheap AND smarter than a real first client (who has
           zero approvals). Isolate to the shipped SEED only (contract.CONCEPT_LEXICON stays; it ships).
   5. gate._RELI_PATH          disk  media/preingest3_reliability.json reliability strict/relaxed per context
        -> READ during extraction (extract.py: reliability_mode(context)!='relaxed'; gate.py: =='strict'),
           so a warm reliability file changes emit/escalation behaviour. Written only on reviewer
           confirm/overturn (gate._bump), never auto during headless extraction — but still isolate: a real
           first client starts at the default mode.
B) LOGGING / INSTRUMENTATION — accumulate but do NOT change results; isolated only for cleanliness:
   6. gate._AUDIT_PATH         disk  media/preingest3_audit.json       reviewer audit log (no decision feedback)
C) IN-PROCESS, run-scoped — reset every run (pipeline.run already clears the ones marked *):
   7. identity._IDENTITY_CACHE  mem  byte_fp -> (content_fp, layout_fp)        [clear_identity_cache]
   8. identity._PARSE_CACHE     mem  parse-once unified grid cache             [clear_parse_cache *]
   9. identity._FALLBACK_LOG    mem  G-FALLBACK instrumentation list           [clear_fallback_log]
  10. profiler cache            mem  per-file profile                          [clear_profile_cache *]
  11. lexicon in-proc memo      mem  (covered by #4 reset)
  12. currency_ledger._ACTIVE   ctxvar  per-run currency verdicts             [pipeline.run sets fresh *]
  13. reuse (run kwarg)         mem  in-run extraction reuse map               [pipeline.run defaults {} *]
D) NOT A CROSS-RUN ANSWER CACHE — no action needed:
   - master_workbook._BUILD_HDR (output-build state, id(ws)-keyed)
   - identity workbook byte reads (fingerprinting, not a cache)
   - alias resolution: in prod a DB-backed org-scoped store (AliasLedger(org=...)); in a headless run with
     a fresh org= it starts empty, and it is not model-cost-driving for the STATEMENT locate path. A cold
     harness uses a fresh org= per run, which is empty by construction (documented, nothing to patch here).

WARM (re-upload / steady-state) measurement = the INVERSE: leave 1,2,4,5 populated (do NOT call this),
so you measure the returning-client case. Never mix: a run is cold in ALL carriers or it is not cold.
"""
from __future__ import annotations

import json
import os

try:                                             # absolute (harness: backend on sys.path)
    from dataimport.preingest3 import llm, templates, lexicon, gate, identity, profiler
except ImportError:                              # relative (pytest package import)
    from .. import llm, templates, lexicon, gate, identity, profiler

# The A+B disk stores, by the attribute the module reads at call time -> isolate by reassigning it.
_DISK_STORES = (
    ('llm._CACHE_DIR', llm, '_CACHE_DIR', 'calls'),               # a DIRECTORY
    ('templates._STORE', templates, '_STORE', 'templates.json'),
    ('lexicon._LEARNED_PATH', lexicon, '_LEARNED_PATH', 'lexicon.json'),
    ('gate._RELI_PATH', gate, '_RELI_PATH', 'reliability.json'),
    ('gate._AUDIT_PATH', gate, '_AUDIT_PATH', 'audit.json'),
)


def _stat(p: str):
    exists = os.path.exists(p)
    size = (sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(p) for f in fs)
            if exists and os.path.isdir(p) else (os.path.getsize(p) if exists else 0))
    return exists, size


def real_store_snapshot() -> dict:
    """(name -> {path, exists, size}) for every REAL disk store, so a harness can prove before==after
    that a cold run left production state untouched. Reads the LIVE module attrs, so call it BEFORE
    isolate_cold() — the captured `path` is what assert_untouched() re-stats afterward."""
    out = {}
    for name, mod, attr, _ in _DISK_STORES:
        p = getattr(mod, attr)
        exists, size = _stat(p)
        out[name] = {'path': p, 'exists': exists, 'size': size}
    return out


def isolate_cold(tag: str, scratch_dir: str) -> dict:
    """Point EVERY model-driven / behaviour-carrying store at a FRESH temp path under
    scratch_dir/tag, reset every in-process cache, and ASSERT each isolated store starts empty.
    Returns {name: temp_path}. Caller MUST also pass store_dir=None to pipeline.run (golden store)
    and a fresh org= (alias store). Raises AssertionError if any store is not genuinely fresh —
    a cold run can never silently start warm."""
    base = os.path.join(scratch_dir, f'cold_{tag}')
    os.makedirs(base, exist_ok=True)
    mapping = {}
    for name, mod, attr, leaf in _DISK_STORES:
        p = os.path.join(base, leaf)
        setattr(mod, attr, p)
        mapping[name] = p
    # in-process caches (belt-and-braces; pipeline.run also clears * ones)
    identity.clear_identity_cache()
    identity.clear_parse_cache()
    identity.clear_fallback_log()
    profiler.clear_profile_cache()
    lexicon._syn_cache = {}
    lexicon._learned_cache = {'key': None, 'data': {}}
    _assert_cold(mapping)
    return mapping


def _assert_cold(mapping: dict) -> None:
    # every isolated disk path is absent (fresh) ...
    for name, p in mapping.items():
        assert not os.path.exists(p), f'COLD HYGIENE FAIL: {name} already exists at {p}'
    # ... and every store's own loader reports empty, through the SAME code the pipeline uses
    assert templates._load().get('layouts') == {}, 'COLD HYGIENE FAIL: templates registry not empty'
    assert not lexicon._load_learned(), 'COLD HYGIENE FAIL: learned lexicon not empty'
    assert gate._load(gate._RELI_PATH) in ({}, None), 'COLD HYGIENE FAIL: reliability store not empty'


def assert_untouched(before: dict) -> None:
    """Prove a cold run left the REAL stores byte-for-byte as they were (same exists/size). Re-stats the
    REAL paths captured in `before` (NOT the live module attrs, which isolate_cold reassigned to temp)."""
    for name, b in before.items():
        exists, size = _stat(b['path'])
        assert (exists, size) == (b['exists'], b['size']), (
            f'REAL STORE MUTATED by a cold run: {name} exists/size {b["exists"]}/{b["size"]} '
            f'-> {exists}/{size} at {b["path"]} (isolation leaked)')
