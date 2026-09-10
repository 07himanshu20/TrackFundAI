"""AI-on concurrency dispatch (_run_extractions thread pool) — proven token-free with a stub extractor.

The model-on speed path fans I/O-bound locator work across a thread pool. These prove the four
properties that make that safe and honest, WITHOUT any Vertex call:
  • PARITY   — threaded (max_workers>1) result is byte-identical to serial (max_workers=1): same
               records, same per-file currency observations, same order (ex.map preserves input order).
  • ISOLATION— each file's currency observations stay its own under concurrency (per-thread ledger).
  • METRICS  — each worker's own (ContextVar-local) call metrics are merged back into the parent.
  • FAIL-CLOSED — a per-file failure (a throttle/429, a timeout) becomes a HELD record, never a crash
               and never a wrong number; the run still balances (every task returns something).
"""
from decimal import Decimal

from backend.dataimport.preingest3 import pipeline, currency_ledger, llm
from backend.dataimport.preingest3.cir import Record


def _tasks(n=6):
    # (ck, company, path, domicile, anchor_cr, label) — the shape _run_extractions expects
    return [(f'ck{i}', f'Co{i}', f'/f{i}', None, Decimal('1'), f'Co{i}') for i in range(n)]


def _make_fake(ccy_by_path, fail_paths=()):
    """A deterministic stand-in for extract_company: 'makes a model call' (bumps this thread's
    metrics), records ITS file's currency, returns a Record — or raises (simulating a throttle)."""
    def fake(company, path, *, rate_card, entity, domicile, base_currency, anchor_cr, use_model):
        mm = llm.current_metrics()
        if mm is not None:
            mm.calls += 1
        if path in fail_paths:
            if mm is not None:
                mm.error += 1
            raise RuntimeError('simulated throttle 429')
        if mm is not None:
            mm.ok += 1
        currency_ledger.observe(currency=ccy_by_path[path], escalate=False, reason='t', flags=())
        return Record('mis', entity_id=company, fields={'company': company})
    return fake


def test_threaded_matches_serial_byte_for_byte_and_isolates_currency(monkeypatch):
    ccy = {f'/f{i}': ('MYR' if i % 2 else 'INR') for i in range(6)}
    monkeypatch.setattr(pipeline, 'extract_company', _make_fake(ccy))

    llm.new_metrics()
    serial = pipeline._run_extractions(_tasks(), rate_card=None, use_model=True, max_workers=1)
    llm.new_metrics()
    threaded = pipeline._run_extractions(_tasks(), rate_card=None, use_model=True, max_workers=4)

    assert list(serial.keys()) == list(threaded.keys())            # same order (deterministic)
    for ck in serial:
        assert serial[ck][0].entity_id == threaded[ck][0].entity_id
        s_ccy = [o.currency for o in serial[ck][1]]
        t_ccy = [o.currency for o in threaded[ck][1]]
        assert s_ccy == t_ccy                                      # same currency obs per file
        assert len(set(t_ccy)) == 1                                # ISOLATION: only its own currency


def test_threaded_run_is_deterministic_across_runs(monkeypatch):
    ccy = {f'/f{i}': ('MYR' if i % 2 else 'INR') for i in range(6)}
    monkeypatch.setattr(pipeline, 'extract_company', _make_fake(ccy))
    llm.new_metrics()
    a = pipeline._run_extractions(_tasks(), rate_card=None, use_model=True, max_workers=4)
    llm.new_metrics()
    b = pipeline._run_extractions(_tasks(), rate_card=None, use_model=True, max_workers=4)
    assert list(a.keys()) == list(b.keys())
    assert {ck: [o.currency for o in obs] for ck, (_r, obs) in a.items()} == \
           {ck: [o.currency for o in obs] for ck, (_r, obs) in b.items()}


def test_parent_metrics_merge_every_worker_thread(monkeypatch):
    ccy = {f'/f{i}': 'INR' for i in range(6)}
    monkeypatch.setattr(pipeline, 'extract_company', _make_fake(ccy))
    m = llm.new_metrics()
    pipeline._run_extractions(_tasks(), rate_card=None, use_model=True, max_workers=4)
    assert m.calls == 6 and m.ok == 6                              # every thread's calls merged up


def test_per_file_throttle_becomes_held_never_crashes(monkeypatch):
    ccy = {f'/f{i}': 'INR' for i in range(6)}
    monkeypatch.setattr(pipeline, 'extract_company', _make_fake(ccy, fail_paths={'/f2', '/f4'}))
    m = llm.new_metrics()
    res = pipeline._run_extractions(_tasks(), rate_card=None, use_model=True, max_workers=4)
    assert len(res) == 6                                           # no file lost, no crash
    held = [ck for ck, (rec, _o) in res.items() if '_note' in rec.fields]
    assert len(held) == 2                                          # the 2 throttled files → held records
    assert m.calls == 6 and m.error == 2                          # all attempted; failures counted
