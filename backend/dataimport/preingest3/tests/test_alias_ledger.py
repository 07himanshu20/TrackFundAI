"""
Permanent regression fixture for U4 (Alias Ledger). Covers the four adversarial
cases that each map to a silent-failure path U4 must close:

  1. near-miss names          — the right company wins by floor+margin; the wrong
                                lookalike does not sneak in.
  2. entity-absent-schedule   — no candidate clears the floor → no_match → HALT
                                (a wrong one cannot win by a wide margin).
  3. legitimate two-file co.   — same company, P&L file + KPI file → NOT flagged
                                duplicate (keyed on entity+period+statement_type).
  4. stale-ledger entry        — a ledger hit whose entity left the schedule →
                                escalated, never trusted.

Run: python -m pytest backend/dataimport/preingest3/tests/test_alias_ledger.py
or standalone via the __main__ block.
"""
import os
import tempfile

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
os.environ.setdefault('TFAI_ENV', 'local')
import django  # noqa: E402
django.setup()

from dataimport.preingest3 import alias_ledger as al   # noqa: E402
from dataimport.preingest3 import gate                 # noqa: E402

SCHEDULE = [
    {'id': 'INV1', 'name': 'Hubler Technologies Pvt Ltd'},
    {'id': 'INV2', 'name': 'Aliste Technologies'},
    {'id': 'INV3', 'name': 'Agnikul Cosmos'},
    {'id': 'INV4', 'name': 'Clientell Software'},
]


def _fresh_ledger():
    d = tempfile.mkdtemp()
    # isolate gate stores too so reliability/audit don't leak across test runs
    gate._AUDIT_PATH = f'{d}/audit.json'
    gate._RELI_PATH = f'{d}/reli.json'
    # trust the alias context so a clean auto-accept isn't downgraded by strict mode
    for _ in range(al.gate.RELAX_AFTER_CONFIRMS):
        gate._bump('alias:default', confirm=True)
    return al.AliasLedger(org='default', path=f'{d}/ledger.json')


def test_1_near_miss_names():
    led = _fresh_ledger()
    # 'Hubler' filename should resolve to INV1, not to a near lookalike
    r = al.resolve_file(['AVF_2026_03_11_P_Hubler_MIS'], SCHEDULE, ledger=led,
                        content_fp='fp1', source_label='hubler.xlsx')
    assert r.status == 'resolved' and r.entity_id == 'INV1', r
    # a genuine near-miss that is NOT in the schedule must not resolve to Hubler
    r2 = al.resolve_file(['Hubli Logistics'], SCHEDULE, ledger=led, content_fp='fp2',
                         source_label='hubli.xlsx')
    assert r2.entity_id != 'INV1', r2
    print('  1 near-miss:            OK  ->', r.entity_id, '| lookalike held:', r2.status)


def test_2_entity_absent_from_schedule():
    led = _fresh_ledger()
    r = al.resolve_file(['Zephyr Robotics'], SCHEDULE, ledger=led, content_fp='fp3',
                        source_label='zephyr.xlsx')
    assert r.status == 'held' and r.tier == gate.HUMAN, r
    assert any('floor' in x for x in r.reasons), r.reasons
    print('  2 entity-absent:        OK  -> HALT/held (no_match), reasons:', r.reasons[-1][:50])


def test_3_legit_two_file_company():
    led = _fresh_ledger()
    pl = al.resolve_file(['Agnikul'], SCHEDULE, ledger=led, content_fp='fp4',
                         period='Feb-26', statement_type='pl', source_label='agnikul_pl.xlsx')
    kpi = al.resolve_file(['Agnikul'], SCHEDULE, ledger=led, content_fp='fp5',
                          period='Feb-26', statement_type='kpi', source_label='agnikul_kpi.xlsx')
    dups = al.find_duplicates([pl, kpi])
    assert pl.entity_id == kpi.entity_id == 'INV3', (pl, kpi)
    assert dups == [], dups          # different statement_type → NOT a duplicate
    # but two P&L files for the same company+period IS a duplicate → hold
    pl2 = al.resolve_file(['Agnikul'], SCHEDULE, ledger=led, content_fp='fp6',
                          period='Feb-26', statement_type='pl', source_label='agnikul_pl_v2.xlsx')
    dups2 = al.find_duplicates([pl, pl2])
    assert len(dups2) == 1 and 'HOLD' in dups2[0]['disposition'], dups2
    print('  3 two-file company:     OK  -> P&L+KPI not flagged; dup P&L flagged HOLD')


def test_4_stale_ledger_entry():
    led = _fresh_ledger()
    # ledger maps an old identifier to INV9, which is NOT in the current schedule
    led.learn(['LegacyCo MIS'], 'INV9')
    r = al.resolve_file(['LegacyCo MIS'], SCHEDULE, ledger=led, content_fp='fp7',
                        source_label='legacy.xlsx')
    assert r.status == 'held' and 'STALE' in ' '.join(r.reasons), r
    print('  4 stale-ledger:         OK  -> escalated (not trusted):', r.reasons[0][:44])


def test_5_reverse_coverage():
    resolved = ['INV1', 'INV3']
    missing = al.missing_investments(SCHEDULE, resolved)
    ids = {m['entity_id'] for m in missing}
    assert ids == {'INV2', 'INV4'}, missing
    print('  5 reverse-coverage:     OK  -> disclosed missing:', sorted(ids))


if __name__ == '__main__':
    test_1_near_miss_names()
    test_2_entity_absent_from_schedule()
    test_3_legit_two_file_company()
    test_4_stale_ledger_entry()
    test_5_reverse_coverage()
    print('ALL U4 ADVERSARIAL CASES PASS')
