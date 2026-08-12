"""Permanent regression fixture for concept row-binding + hint tokenisation.

Each case maps to a silent-wrong-number bug found on the real trivesta files:
  • ratio row      — InstaAstro bound '% of revenue' → revenue summed to ~0.
  • cross-concept  — Analisa bound 'Sales - Others / Cash' → cash 0; CPC bound
                     'Cash Flow from Operations' → wrong cash.
  • component      — Aliste bound 'Commission Revenue' (a component) not the total.
  • whole-word unit— CSS read unit 'cr' out of 'Description' → revenue ×crore
                     (4,051,537 Cr). Hints must be whole tokens.
"""
from backend.dataimport.preingest3 import extract as ex
from backend.dataimport.preingest3.lexicon import normalise_label


def _toks(label):
    return set(normalise_label(label).split())


def test_whole_word_unit_not_substring():
    assert ex._token_present('cr', 'in rs cr')
    assert ex._token_present('cr', '(₹ cr)')
    assert not ex._token_present('cr', 'description')       # the CSS explosion
    assert not ex._token_present('cr', 'across the group')
    assert not ex._token_present('rs', 'hours worked')      # currency 'rs' not in 'hours'
    assert ex._token_present('₹', 'revenue ₹ lakhs')        # symbol still matches


def test_ratio_label_rejected_for_money():
    assert ex._is_ratio_label('% of revenue')
    assert ex._is_ratio_label('Automation Revenue %')
    assert not ex._is_ratio_label('Total Revenue')


def test_total_preferred_over_component():
    total = ex._label_score('Total Revenue billed', 'revenue', _toks('Total Revenue billed'))
    comp = ex._label_score('Commission Revenue', 'revenue', _toks('Commission Revenue'))
    assert total > comp


def test_closing_preferred_over_opening():
    closing = ex._label_score('Cash & Cash Equivalents -Closing Balance', 'cash',
                              _toks('Cash & Cash Equivalents -Closing Balance'))
    opening = ex._label_score('Cash & Cash Equivalents -Opening Balance', 'cash',
                              _toks('Cash & Cash Equivalents -Opening Balance'))
    assert closing > opening


def test_lone_opening_cash_holds_not_wrong_period():
    # Period-boundary guard (3c): an 'opening/beginning' balance is LAST period's close.
    # _AGG_ANTI RANKS closing above opening when both exist, but does not BLOCK a LONE
    # opening row from binding (nothing out-scores it) — so generic `cash` on an
    # opening-only sheet would emit the wrong period. HOLD instead. (Real-file census:
    # 0 of 15 files bind an opening row today, so this guard is a fail-closed latent-fix.)
    import datetime
    from backend.dataimport.preingest3.periods import detect_period_axis
    hdr = ['', datetime.datetime(2026, 1, 1), datetime.datetime(2026, 2, 1)]

    def bind(datarows, concept='cash'):
        rows = [hdr] + datarows
        ax = detect_period_axis(rows)
        lc = ex._sheet_label_col(rows, ax.axis_rows[0])
        return ex._find_concept_row(rows, lc, concept, ax.axis_rows[0] + 1, len(rows), ax.columns)

    assert bind([['Opening cash & bank balance', 100, 110]]) is None          # lone opening → HOLD
    assert bind([['Opening cash & bank balance', 100, 110],
                 ['Closing cash & bank balance', 110, 120]]) == 2             # both → the CLOSING row
    assert bind([['Closing cash & bank balance', 110, 120]]) == 1             # lone closing → bindable
    assert bind([['Cash & bank', 110, 120]]) == 1                            # plain as-of → unaffected


def test_cross_concept_ambiguous_label_is_not_sole_best():
    # 'Sales - Others / Cash' matches revenue ('sales') at least as strongly as
    # cash → cash must NOT be its sole strongest match (so it is skipped for cash).
    label = 'Sales - Others / Cash'
    toks = _toks(label)
    cash = ex._label_score(label, 'cash', toks)
    rival = max(ex._label_score(label, c, toks) for c in ex._DISAMBIG if c != 'cash')
    assert rival >= cash                                    # ambiguous → skipped


def test_plain_cash_line_is_sole_best():
    # 'Cash in Bank' matches only cash → bindable (the Agnikul regression).
    label = 'Cash in Bank'
    toks = _toks(label)
    cash = ex._label_score(label, 'cash', toks)
    rival = max(ex._label_score(label, c, toks) for c in ex._DISAMBIG if c != 'cash')
    assert cash > rival and cash >= ex._CONCEPT_ROW_FLOOR


def test_ebitda_metric_consistency():
    # clean-by-LABEL = explicit EBITDA or an OPERATING-base D&A add-back (= EBIT+D&A
    # by identity, i.e. before interest AND tax). A PBT/net-profit + D&A line is a
    # PROXY (post-interest/tax) — NOT clean by label; it must pass the arithmetic
    # identity or be held. EBIT/PBT with no add-back are never clean.
    assert ex._ebitda_is_clean('EBITDA')
    assert ex._ebitda_is_clean('Operating Profit before Depreciation & Amortisation')
    assert not ex._ebitda_is_clean('Profit Before Tax, depreciation and ESOP')  # proxy, not auto-clean
    assert not ex._ebitda_is_clean('Net Profit before Depreciation')            # post-interest/tax + D&A
    assert not ex._ebitda_is_clean('Operating Profit')          # EBIT
    assert not ex._ebitda_is_clean('Profit Before Tax')         # PBT
    assert not ex._ebitda_is_clean('Operating Income')          # EBIT


def test_ebitda_label_class_names_the_base():
    # the row-picker half of the identity: it must name WHICH metric the label is,
    # so a proxy is routed to arithmetic verification, not silently accepted.
    assert ex._ebitda_label_class('EBITDA') == 'ebitda'
    assert ex._ebitda_label_class('Operating Profit before Depreciation') == 'operating_addback'
    assert ex._ebitda_label_class('Profit Before Tax, depreciation and ESOP') == 'proxy_addback'
    assert ex._ebitda_label_class('Net Profit before Depreciation') == 'proxy_addback'
    assert ex._ebitda_label_class('Operating Profit') == 'not_ebitda'
    assert ex._ebitda_label_class('Profit Before Tax') == 'not_ebitda'


def test_ebitda_arithmetic_identity_verifies_proxy():
    # EBITDA ≈ EBIT + D&A on a shared column PROVES a proxy-labelled row is really
    # EBITDA; a break means it is a different metric → not verified; absent parts
    # → None (caller holds as an unverified proxy). num col = index 1.
    #                label                    latest-period value
    rows = [['Line',                          'Feb-26'],
            ['Operating Profit',              100.0],      # EBIT
            ['Depreciation & Amortisation',   20.0],       # D&A
            ['Profit before tax and depreciation', 120.0]] # candidate: 100+20 == 120
    assert ex._verify_ebitda_arithmetic(rows, 0, [1], 3) is True
    rows[3][1] = 95.0                                       # 100+20 != 95 → broke
    assert ex._verify_ebitda_arithmetic(rows, 0, [1], 3) is False
    no_da = [['Line', 'Feb-26'], ['Operating Profit', 100.0],
             ['Profit before tax and depreciation', 120.0]]
    assert ex._verify_ebitda_arithmetic(no_da, 0, [1], 2) is None  # no D&A line → can't check


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('ALL PASS')
