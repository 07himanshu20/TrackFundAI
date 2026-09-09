"""Rate GOVERNOR — proven in BOTH directions (reddening discipline): DISABLED it is an instant
pass-through (the negative control = no governance → would flood), ENABLED it spaces issues to the
configured rate and, when the wait budget is exhausted, FAILS CLOSED to a hold (never a flood, never
a crash). Token-free: no model, no network — pure timing/logic."""
import threading
import time

import pytest

from backend.dataimport.preingest3 import rate_governor as rg
from backend.dataimport.preingest3 import llm
from backend.dataimport.preingest3.llm import CallResult, ERROR


# ── negative control: DISABLED = instant pass-through (no governance) ────────────────────────
def test_disabled_is_passthrough_instant():
    g = rg.RateGovernor(rpm=None, tpm=None)
    assert not g.enabled
    t0 = time.monotonic()
    for _ in range(50):
        assert g.acquire(10_000) is True          # even huge demand: no limits → instant
    assert time.monotonic() - t0 < 0.1            # would-flood baseline: nothing spaced


# ── ENABLED throttles: a drained RPM bucket makes the next issue WAIT ~1/rate ────────────────
def test_rpm_bucket_spaces_the_next_issue():
    g = rg.RateGovernor(rpm=60, max_wait_s=5)     # 1 token/sec
    g._req.tokens = 0                             # drained → next needs a full refill second
    t0 = time.monotonic()
    assert g.acquire(0) is True
    waited = time.monotonic() - t0
    assert 0.7 <= waited <= 2.0, waited           # spaced ~1s, NOT instant (contrast the control)


# ── FAIL-CLOSED: budget exhausted → acquire returns False (HOLD), consumes nothing ───────────
def test_failclosed_hold_when_wait_budget_exhausted():
    g = rg.RateGovernor(rpm=60, max_wait_s=0.3)   # needs ~1s but only waits 0.3s
    g._req.tokens = 0
    t0 = time.monotonic()
    assert g.acquire(0) is False                  # HOLD, not flood, not crash
    assert time.monotonic() - t0 < 1.0            # gave up within the budget
    assert g.holds == 1 and g.acquired == 0


# ── TPM dimension governs independently; an oversized demand is capped, never a deadlock ──────
def test_tpm_dimension_and_oversized_cap():
    g = rg.RateGovernor(tpm=600, max_wait_s=5)    # 10 tokens/sec
    g._tok.tokens = 0
    t0 = time.monotonic()
    assert g.acquire(10) is True                  # 10 tokens → ~1s refill
    assert 0.7 <= time.monotonic() - t0 <= 2.0
    g2 = rg.RateGovernor(tpm=600, max_wait_s=2)
    assert g2.acquire(10_000_000) is True         # demand > capacity → capped to capacity, not a hang


# ── thread-safe: shared governor across workers, all succeed under budget, no exception ───────
def test_thread_safe_shared_governor():
    g = rg.RateGovernor(rpm=6000, max_wait_s=5)   # generous → all succeed, tests locking not throttle
    errors = []
    def worker():
        try:
            assert g.acquire(1) is True
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    ts = [threading.Thread(target=worker) for _ in range(16)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errors
    assert g.acquired == 16


# ── integration: when the governor cannot acquire, llm.call_json HOLDS and never issues ───────
def test_calljson_holds_and_does_not_issue_when_governed(monkeypatch, tmp_path):
    called = {'n': 0}
    def _provider(*a, **k):
        called['n'] += 1
        return type('R', (), {'text': '{}'})()
    llm.set_model_provider(_provider)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path))          # cold → would issue if not governed
    monkeypatch.setattr(rg, 'acquire', lambda tokens: False)       # governor: no capacity → HOLD
    res = llm.call_json('locate_rows', 'sig', 'a prompt', use_cache=False)
    assert res.kind == ERROR                                       # held, fail-closed
    assert called['n'] == 0                                        # never issued to the provider
