"""LOCKED entity-resolution truth table — the guard that turns a mis-attribution
from 'I noticed LDC's 316 was labelled Hubler' into 'the fixture went red'.

Mis-attributing company A's numbers to company B is the worst failure in the
system (silent, no arithmetic catches it). The closed-set reverse matcher
(fund_anchor.resolve_closed_set) is fail-safe by construction — each known name is
tested independently, so five companies can never collapse onto one — but this
table proves it per file and catches any regression in the identifiers we feed it.

Each MIS file → the company key it MUST resolve to, or None (HOLD). A file
resolving to the WRONG company is the red line; HOLD is always acceptable.
"""
import os

import pytest

from backend.dataimport.preingest3 import fund_anchor

IN = 'backend/media/preingest/trivesta/100e86d5/in'
FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']

# file -> expected resolved company key (the schedule 'company' label), or None=HOLD
EXPECTED = {   # values are the normalised anchor KEYS (lexicon.normalise_label of the name)
    'AVF_2026_03_11_P_Hubler_MIS_Feb26.xlsx': 'hubbler',
    'AVF_2026_03_12_P_Agnikul_MIS_Feb26.xlsx': 'agnikul cosmos',
    'AVF_2026_03_16_P_Clientell_MIS_Feb26.xlsx': 'clientell',
    'AVF_2026_03_18_P_InstaAstro_MIS_Feb_2026.xlsx': 'instaastro',
    'AVF_2026_03_20_P_LDC_Detailed_MIS_Feb26.xlsx': 'ldc',
    'AVF_2026_03_26_P_Aliste_MIS_Feb26.xlsx': 'aliste technologies',
    'CPC_Monthly_MIS-_May_25_to_be_sent_to_EL.xlsx': 'cpc diagnostics pvt ltd',
    '0625_CPM_Monthly_Report-R1.xlsx': 'chemopharm sdn bhd',
    '01_Monthly_Financial_Presentation_2025_May_Analisa.xlsx': 'analisa resources sdn bhd',
    # CSS holds: its title 'Chemoscience Pte Ltd' sits on a sheet past the content
    # scan window. HOLD is safe (never mis-attributed); improving the scan would
    # bind it — until then this asserts it does NOT resolve to a WRONG company.
    '0525_CSS_Monthy_Report_-_Consol_Updated.xlsx': None,
}

_ANCHORS = {}


def _anchors():
    if not _ANCHORS:
        _ANCHORS.update(fund_anchor.build_fund_anchors([os.path.join(IN, f) for f in FUND]))
    return _ANCHORS


def _file_text(fn):
    from backend.dataimport.preingest3.profiler import profile_file, _cell_type
    prof = profile_file(fn, os.path.join(IN, fn))
    hints = []
    for s in prof['sheets'][:6]:
        rows = prof['grid'][s.sheet]
        for r in range(min(4, len(rows))):
            for v in rows[r]:
                if _cell_type(v) == 'text' and 4 < len(str(v).strip()) < 60:
                    hints.append(str(v).strip())
    return fn + ' ' + ' '.join(hints[:10])


@pytest.mark.parametrize('fn,expected', sorted(EXPECTED.items()))
def test_entity_resolves_correctly_or_holds(fn, expected):
    key = fund_anchor.resolve_closed_set(_file_text(fn), _anchors())
    assert key == expected, f'{fn} resolved to {key!r}, expected {expected!r}'


def test_no_file_resolves_to_a_wrong_company():
    """The red line: no MIS file may resolve to a company other than its own."""
    for fn, expected in EXPECTED.items():
        key = fund_anchor.resolve_closed_set(_file_text(fn), _anchors())
        assert key is None or key == expected, f'MIS-ATTRIBUTION: {fn} -> {key!r}'


if __name__ == '__main__':
    a = _anchors()
    for fn, expected in sorted(EXPECTED.items()):
        got = fund_anchor.resolve_closed_set(_file_text(fn), a)
        print(f'{"ok  " if got == expected else "FAIL"} {fn[:44]:44s} -> {got!r}')
