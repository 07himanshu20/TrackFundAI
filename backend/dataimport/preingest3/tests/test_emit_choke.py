"""Increment-5 verify-or-hold CHOKE — the Step-6 safety seam (option C).

v2 trust model: the deterministic path IS the verifier (trusted, cite-evidence-backstopped);
the MODEL is the thing being verified, so a model figure emits ONLY through _model_emit, on a
passing triangulation verdict (AUTO). The gate is RELOCATED from its call site into this one
door so a future model emit path cannot bypass verification.

Per the negative-control rule: it is not enough that the choke passes when nothing bypasses —
it must REDDEN against a bypass attempt. So these prove:
  • a model figure WITHOUT an AUTO verdict is HELD (the seam catches the bypass), and
  • a limbo figure (value, no hold reason, no disclosed gap) is fail-closed to a disclosed hold.
"""
from decimal import Decimal

from backend.dataimport.preingest3 import extract as ex
from backend.dataimport.preingest3.cir import Figure, Provenance


def _prov():
    return Provenance(source_file='f', content_fingerprint='cfp', sheet='S', cell='B2', row_label='Revenue')


def _emit():           # what the model WOULD emit on a passing verdict
    return Figure('revenue', Decimal('33'), None, _prov(), basis='point_in_time')


class _V:              # minimal triangulation verdict stand-in
    def __init__(self, status):
        self.status = status


# ── the model seam: emit ONLY on AUTO; anything else is a hold (the bypass is caught) ─────────
def test_model_emit_passes_only_on_auto_verdict():
    built = ex._model_emit('revenue', _V(ex.AUTO), _prov(), build=_emit)
    assert built is not None and built.value_cr == Decimal('33')       # AUTO → emits


def test_model_emit_NEGATIVE_CONTROL_holds_a_bypass_attempt():
    # a model figure reaching the choke WITHOUT a passing verdict must NOT emit — the seam.
    calls = {'n': 0}

    def _tracked_build():
        calls['n'] += 1
        return _emit()

    assert ex._model_emit('revenue', _V('human'), _prov(), build=_tracked_build) is None   # non-AUTO
    assert ex._model_emit('revenue', _V('audit'), _prov(), build=_tracked_build) is None    # non-AUTO
    assert ex._model_emit('revenue', None, _prov(), build=_tracked_build) is None            # no verdict
    assert calls['n'] == 0, 'build must NOT run on a held verdict — the emit is never constructed'


# ── the terminal-state invariant: no figure ships in limbo ───────────────────────────────────
def test_finalize_downgrades_limbo_to_disclosed_hold():   # negative control for the invariant
    fields = {
        'company': 'Acme',                                                    # non-Figure — untouched
        'revenue': Figure('revenue', Decimal('10'), None, _prov()),           # confirmed — untouched
        'ebitda': Figure('ebitda', None, None, _prov(), gap=True),            # gap — untouched
        'cash': Figure('cash', None, None, _prov()),                          # LIMBO — no value, not held/gap
        'opex': Figure('opex', Decimal('5'), None, _prov(), held=True),       # held WITHOUT a reason
    }
    ex._finalize_terminal_state(fields)
    assert fields['company'] == 'Acme'
    assert fields['revenue'].confirmed and fields['ebitda'].gap
    assert fields['cash'].held and (fields['cash'].hold_reason or '').strip()   # limbo → disclosed hold
    assert fields['opex'].held and (fields['opex'].hold_reason or '').strip()   # held-no-reason → reason


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS — the emit choke holds a bypass and no figure ships in limbo')
