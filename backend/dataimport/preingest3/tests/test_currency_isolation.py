"""Thread-isolation of the run-scoped currency ledger — the safety keystone for AI-on concurrency.

The AI-on speed path fans I/O-bound locator calls across worker threads (one file per thread). Each
file resolves its OWN currencies; if two files resolved in parallel wrote into a SHARED active ledger,
one file's currency evidence would bleed into another's — the silent ~18× class (MYR recorded/served
as INR) the whole U6 design exists to prevent. `_ACTIVE` is a ContextVar precisely so each thread's
active ledger is independent.

These prove BOTH directions per the negative-control rule: the ContextVar keeps concurrent threads
isolated (the fix), and a single SHARED ledger under the same concurrent pattern DOES mix currencies
(the bug the fix prevents — so the isolation assertion genuinely reddens)."""
import threading

from backend.dataimport.preingest3 import currency_ledger as cl
from backend.dataimport.preingest3.currency_ledger import CurrencyLedger

_N = 50


def _worker(ledger, ccy, barrier):
    cl.set_active(ledger)               # each thread activates ITS OWN ledger (its own ContextVar slot)
    barrier.wait()                      # force both set_active calls to land first → real race window
    for _ in range(_N):
        cl.observe(currency=ccy, escalate=False, reason='t', flags=())


def test_active_ledger_is_thread_isolated():
    la, lb = CurrencyLedger(), CurrencyLedger()
    barrier = threading.Barrier(2)
    ta = threading.Thread(target=_worker, args=(la, 'MYR', barrier))
    tb = threading.Thread(target=_worker, args=(lb, 'INR', barrier))
    ta.start(); tb.start(); ta.join(); tb.join()
    # each ledger saw ONLY its own currency — no cross-contamination (would FAIL with the old global)
    assert {o.currency for o in la.observations()} == {'MYR'}
    assert {o.currency for o in lb.observations()} == {'INR'}
    assert len(la.observations()) == _N and len(lb.observations()) == _N


def test_shared_ledger_cross_contaminates_control():
    # NEGATIVE CONTROL: one SHARED ledger (what a plain module-global effectively is under concurrency)
    # receives BOTH threads' observations → currencies MIX. This is the exact bug the ContextVar
    # prevents, and it proves the isolation test above reddens against a real failure, not a no-op.
    shared = CurrencyLedger()
    barrier = threading.Barrier(2)

    def obs(ccy):
        barrier.wait()
        for _ in range(_N):
            shared.record(currency=ccy, escalate=False, reason='t', flags=())
    ta = threading.Thread(target=obs, args=('MYR',))
    tb = threading.Thread(target=obs, args=('INR',))
    ta.start(); tb.start(); ta.join(); tb.join()
    assert {o.currency for o in shared.observations()} == {'MYR', 'INR'}   # mixed — the contamination


def test_serial_semantics_are_byte_identical():
    # single-threaded behaviour is unchanged: a ContextVar with default None behaves exactly like the
    # old global — no active ledger → observe is a no-op; set → records; reset → no-op again.
    cl.set_active(None)
    assert cl.active() is None
    L = CurrencyLedger(); cl.set_active(L)
    cl.observe(currency='SGD', escalate=False, reason='r', flags=())
    assert [o.currency for o in L.observations()] == ['SGD']
    cl.set_active(None)
    cl.observe(currency='USD', escalate=False, reason='r', flags=())      # no active → dropped
    assert len(L.observations()) == 1
