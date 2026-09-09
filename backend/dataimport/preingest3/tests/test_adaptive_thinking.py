"""Adaptive (escalation) thinking — the DORMANT safety net, proven in both directions (reddening).
Default OFF → no escalation (byte-identical fast path). ON → a concept Tier-1 leaves UNDETERMINED is
re-located with thinking ON (bounded to that concept); a concept Tier-1 confirms ABSENT is NOT
escalated (never burn a call on a genuine absence). Also proves call_json passes the per-call thinking
override through. Token-free — no model, no network. Benefit on messy files is DEFERRED to the broader
corpus (Tier-1 leaves ~no undetermined concepts on the 15 known files) — this proves the PLUMBING."""
import types

from backend.dataimport.preingest3 import locator, llm


# ── call_json passes the per-call thinking override through to the provider ───────────────────
def test_calljson_thinking_override_passthrough(monkeypatch, tmp_path):
    seen = {}
    def prov(prompt, **kw):
        seen.clear(); seen.update(kw); return type('R', (), {'text': '{}'})()
    llm.set_model_provider(prov)
    monkeypatch.setattr(llm, '_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(llm.rate_governor, 'acquire', lambda tokens: True)
    llm.call_json('locate_rows', 's1', 'p', thinking={'thinking_budget': 2048}, use_cache=False)
    assert seen.get('thinking_budget') == 2048                 # override wins
    llm.call_json('locate_rows', 's2', 'p', use_cache=False)   # default (2.5 → budget 0)
    assert seen.get('thinking_budget') == 0


def _run(monkeypatch, adaptive_on, tier1):
    monkeypatch.setenv('PREINGEST3_ADAPTIVE_THINKING', 'on' if adaptive_on else 'off')
    monkeypatch.setattr(locator, 'request_concepts', lambda t: list(t))   # no anchor expansion — focus A/B
    calls = []
    def fake(st, grid, concepts, *, content_fp, mode, thinking=None):
        calls.append({'mode': mode, 'thinking': thinking, 'concepts': list(concepts)})
        return tier1(mode, concepts)
    monkeypatch.setattr(locator, '_locate_rows_call', fake)
    out = locator.locate_rows(types.SimpleNamespace(sheet='S'), {'S': []}, ['A', 'B'], content_fp='cfp')
    return out, calls


def _rr(c, r):
    return locator.RowRecord(concept=c, form='direct', row=r, operand_rows=[], row_label=c)


def _A_only(mode, concepts):                 # A located; B UNDETERMINED (silent drop, not absent)
    return {'records': [_rr('A', 1)], 'returned': {'A'}, 'absent': set()}


# ── negative control: OFF → dormant, no escalation, B stays missing ──────────────────────────
def test_adaptive_off_is_dormant(monkeypatch):
    out, calls = _run(monkeypatch, False, _A_only)
    assert 'rows-escalate' not in [c['mode'] for c in calls]   # never escalates
    assert 'B' in out['missing']


# ── ON → escalates the UNDETERMINED concept with thinking ON, bounded to it, and recovers it ──
def test_adaptive_on_escalates_and_recovers(monkeypatch):
    def tier1(mode, concepts):
        if mode == 'rows-escalate':
            return {'records': [_rr('B', 2)], 'returned': {'B'}, 'absent': set()}
        return _A_only(mode, concepts)
    out, calls = _run(monkeypatch, True, tier1)
    esc = [c for c in calls if c['mode'] == 'rows-escalate']
    assert len(esc) == 1                                        # fired once
    assert esc[0]['concepts'] == ['B']                         # bounded to the hard concept only
    tk = esc[0]['thinking'] or {}
    assert tk.get('thinking_budget', 0) > 0 or tk.get('thinking_level')   # thinking ON
    assert 'B' not in out['missing']                           # coverage recovered


# ── ON but B CONFIRMED ABSENT → do NOT escalate (never burn a call on a genuine absence) ──────
def test_adaptive_on_skips_confirmed_absent(monkeypatch):
    def tier1(mode, concepts):
        return {'records': [_rr('A', 1)], 'returned': {'A'}, 'absent': {'B'}}
    out, calls = _run(monkeypatch, True, tier1)
    assert 'rows-escalate' not in [c['mode'] for c in calls]   # confirmed-absent is not escalated
