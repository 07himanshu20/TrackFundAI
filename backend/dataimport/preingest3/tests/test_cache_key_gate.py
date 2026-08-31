"""Increment-5 cache-key gate — the reuse cache must never serve a prior-config record.

The bug this locks out is LIVE embryonic code: before Inc-5, pipeline keyed the per-file
`reuse`/`extraction` cache on content_fp ALONE, so a file's INR-config record would be served
under a new rate card / as_of / anchor — a silent stale number that goes live the moment the
coverage phase changes config (U6 multi-currency, next as_of).

Why a plain cold≡warm test is INSUFFICIENT (advisor catch): with the SAME config, the buggy
content_fp-only key returns the CORRECT cached result — the gate would be green while the bug
is fully live. So the gate needs the ADVERSARIAL config case: a POISON record cached under
config A must be

  • SERVED under config A  → proves the reuse path is actually exercised (not a vacuous pass),
  • NOT served under config B → proves the key includes the config (no stale-config serve).

content_fp-only would leak the poison into config B; the composite key
(identity.extraction_cache_key) recomputes ckB ≠ ckA → miss → fresh extraction.
"""
import os
import tempfile

import openpyxl

from backend.dataimport.preingest3 import pipeline
from backend.dataimport.preingest3.alias_ledger import AliasLedger
from backend.dataimport.preingest3.cir import Record
from backend.dataimport.preingest3.identity import extraction_cache_key
from backend.dataimport.preingest3.ratecard import default_inr_card


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _fixture(d):
    fund = _xlsx(os.path.join(d, 'fund_schedule.xlsx'),
                 [['Company', 'Cost', 'Fair Value', 'Ownership %'],
                  ['Acme Labs', 40, 90, 25],
                  ['Zephyr Diagnostics', 60, 150, 18]])
    # distinctive filename → closed-set matcher resolves it to the anchor (so extraction +
    # the reuse cache actually engage; an unresolved MIS holds and never touches the cache).
    mis = _xlsx(os.path.join(d, 'Zephyr Diagnostics monthly.xlsx'),
                [['Particulars', 'Apr-25', 'May-25', 'Jun-25'],
                 ['Revenue', 10, 11, 12],
                 ['EBITDA', 2, 2, 3],
                 ['Closing Cash', 5, 6, 7],
                 ['Headcount', 20, 21, 22]])
    return [('fund_schedule', fund), ('Zephyr Diagnostics monthly', mis)]


def _store(d, tag):
    return AliasLedger(org='cachegate', path=os.path.join(d, f'aliases_{tag}.json'))


def _cached_record(run_result):
    recs = [r for r in run_result.extraction.values()]
    assert len(recs) == 1, 'expected exactly one resolved MIS in the extraction cache'
    return recs[0]


# ── the key is PROVABLY complete: every value-affecting input flips it ─────────
def test_cache_key_includes_every_value_affecting_input():
    base = dict(as_of='2026-02-28', rate_card_id='rc_A', anchor_cr='100',
                domicile='IN', use_model=False)
    k0 = extraction_cache_key('CFP', **base)
    assert extraction_cache_key('CFP2', **base) != k0                      # content_fp
    assert extraction_cache_key('CFP', **{**base, 'as_of': '2026-01-31'}) != k0
    assert extraction_cache_key('CFP', **{**base, 'rate_card_id': 'rc_B'}) != k0
    assert extraction_cache_key('CFP', **{**base, 'anchor_cr': '200'}) != k0
    assert extraction_cache_key('CFP', **{**base, 'domicile': 'SG'}) != k0
    assert extraction_cache_key('CFP', **{**base, 'use_model': True}) != k0
    assert extraction_cache_key('CFP', **base, logic_version='lv_ZZZ') != k0  # code version
    assert extraction_cache_key('CFP', **base, contract_sig='ct_ZZZ') != k0   # B4: contract
    assert extraction_cache_key('CFP', **base) == k0                       # deterministic
    # entity is NOT a parameter — excluded by construction (proven pure attribution), so it
    # can never cause a false cache miss.


# ── B4: the contract (schema + seed-AND-learned lexicon + identities/checks/tolerances) is
#    in the key, and — the live hole — a LEARNED-lexicon change re-keys it. net_logic_version
#    hashes preingest3 .py source only, so it misses (a) the out-of-package OUTPUT SCHEMA and
#    (b) the runtime-grown learned-synonym JSON; contract_signature carries both. ───────────
def test_learned_lexicon_growth_rekeys_extraction_cache_REDDENING(monkeypatch):
    from backend.dataimport.preingest3 import contract, lexicon

    base = dict(as_of='2026-02-28', rate_card_id='rc_A', anchor_cr='100',
                domicile='IN', use_model=False)

    # empty learned lexicon → baseline contract signature and baseline extraction key
    monkeypatch.setattr(lexicon, '_load_learned', lambda: {})
    sig_empty = contract.contract_signature()
    k_empty = extraction_cache_key('CFP', **base)

    # a reviewer approves a NEW synonym → the learned lexicon grows (exactly what learn() does)
    monkeypatch.setattr(lexicon, '_load_learned', lambda: {'revenue': ['gross billings topline']})
    sig_grown = contract.contract_signature()
    k_grown = extraction_cache_key('CFP', **base)

    assert sig_grown != sig_empty, \
        'reviewer-approved (learned) synonym did NOT re-key the contract — live D8 staleness hole'
    assert k_grown != k_empty, \
        'learned-lexicon growth did not reach the extraction cache key — stale extraction served'

    # NEGATIVE CONTROL: a SEED-ONLY signature (the pre-fix behaviour) is BLIND to learned growth
    # — proving it is specifically the learned-lexicon dimension that closes the hole, not luck.
    import hashlib
    import json as _json

    def _seed_only_sig():
        blob = _json.dumps({'lexicon': {k: sorted(v)
                                        for k, v in contract.CONCEPT_LEXICON.items()}},
                           sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()

    s0 = _seed_only_sig()
    monkeypatch.setattr(lexicon, '_load_learned', lambda: {'revenue': ['another fresh label']})
    assert _seed_only_sig() == s0, \
        'seed-only signature reacted to learned growth — negative control is not isolating the bug'


def test_contract_dimension_is_load_bearing_not_decorative():
    # The pre-fix key formula EXCLUDED the contract entirely; this pins that the dimension we
    # added is the thing doing the work — two different contracts must diverge, same must agree.
    base = dict(as_of='2026-02-28', rate_card_id='rc_A', anchor_cr='100',
                domicile='IN', use_model=False)
    a = extraction_cache_key('CFP', **base, contract_sig='ct_A')
    b = extraction_cache_key('CFP', **base, contract_sig='ct_B')
    assert a != b, 'distinct contracts collapsed to one key — contract dimension is not wired'
    assert a == extraction_cache_key('CFP', **base, contract_sig='ct_A')  # deterministic


# ── the adversarial config case: no stale-config serve (the assertion that catches the bug) ──
def test_reuse_cache_does_not_serve_prior_config_record():
    with tempfile.TemporaryDirectory() as d:
        files = _fixture(d)
        card_a = default_inr_card('2026-06-30')
        card_b = default_inr_card('2026-03-31')
        assert card_a.card_id != card_b.card_id, 'need two distinct configs'

        # cold run under config A → resolves, extracts, caches under ckA
        r_a = pipeline.run(files, as_of='2026-06-30', org='cachegate',
                           rate_card=card_a, alias_store=_store(d, 'a'))
        rep = next(fr for fr in r_a.files if fr.entity_id == 'Zephyr Diagnostics')
        assert rep.status == 'attributed', 'fixture MIS must resolve, else the cache never engages'
        (ck_a, _rec_a), = r_a.extraction.items()

        # a POISON record with an unmistakable sentinel, keyed under config A's ck
        poison = Record('mis', entity_id='Zephyr Diagnostics',
                        fields={'company': 'Zephyr Diagnostics', '_poison': True})

        # (1) SAME config A + reuse=poison → the poison IS served (reuse path is LIVE) ──
        r_a2 = pipeline.run(files, as_of='2026-06-30', org='cachegate', rate_card=card_a,
                            alias_store=_store(d, 'a2'), reuse={ck_a: poison})
        assert _cached_record(r_a2).fields.get('_poison') is True, \
            'same-config reuse must SERVE the cached record — else this gate is vacuous'

        # (2) config B + reuse=poison(keyed under A) → poison NOT served (key includes config) ──
        r_b = pipeline.run(files, as_of='2026-03-31', org='cachegate', rate_card=card_b,
                           alias_store=_store(d, 'b'), reuse={ck_a: poison})
        assert '_poison' not in _cached_record(r_b).fields, \
            'config B served config A\'s cached record — the cache key is config-incomplete (stale serve)'


# ── logic_version completeness: whole-package, not a hand-list ────────────────
def test_logic_version_covers_whole_library_not_a_handlist():
    # A from-memory 'value modules' list silently missed 6 modules when the extract_company
    # call graph was traced (gate/lexicon/profiler/ratecard/cir/identity) — a fix to any would
    # NOT have invalidated the cache. The whole-package hash covers them by construction; this
    # asserts the traced value-affecting modules are in the hashed set.
    from backend.dataimport.preingest3 import identity as I
    here = os.path.dirname(os.path.abspath(I.__file__))
    pyfiles = {f for f in os.listdir(here) if f.endswith('.py')}
    for must in ('extract.py', 'family.py', 'tiers.py', 'reconcile.py', 'periods.py',
                 'gate.py', 'lexicon.py', 'profiler.py', 'ratecard.py', 'contract.py',
                 'units.py', 'quantity.py', 'cir.py', 'identity.py'):
        assert must in pyfiles, f'{must} absent — logic_version would not cover it (stale-logic hole)'
    assert I.net_logic_version() == I.net_logic_version()          # deterministic + memoised
    assert I.net_logic_version().startswith('lv_')


def test_logic_version_reddens_on_any_module_change():
    # NEGATIVE CONTROL (standing rule): the version must CHANGE when a module changes or is
    # added — otherwise a shipped fix hides behind a stale hit. Proven on a controlled dir so
    # the mechanism, not the current package snapshot, is what's verified.
    from backend.dataimport.preingest3.identity import _hash_py_dir
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, 'a.py'), 'w') as f:
            f.write('x = 1\n')
        with open(os.path.join(d, 'b.py'), 'w') as f:
            f.write('y = 2\n')
        h0 = _hash_py_dir(d)
        with open(os.path.join(d, 'b.py'), 'w') as f:             # edit a module
            f.write('y = 3\n')
        h1 = _hash_py_dir(d)
        assert h1 != h0, 'version did not change on a module EDIT — stale-logic hole'
        with open(os.path.join(d, 'c.py'), 'w') as f:             # add a NEW module
            f.write('z = 4\n')
        assert _hash_py_dir(d) != h1, 'version did not change when a module was ADDED — coverage hole'


if __name__ == '__main__':
    test_cache_key_includes_every_value_affecting_input()
    test_reuse_cache_does_not_serve_prior_config_record()
    test_logic_version_covers_whole_library_not_a_handlist()
    test_logic_version_reddens_on_any_module_change()
    print('ALL PASS — cache key is config-complete; no stale-config serve; logic_version whole-package')
