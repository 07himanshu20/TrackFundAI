"""Presentation-tier discriminator (net Step-5 axis-1). Pure, synthetic — no real files.

Locks the calibrated design + the advisor's three hardenings (2026-07-27):
  • GL-code fraction is the PRIMARY dump signature (real 0% vs SAP dump 54-89%), threshold 0.30.
  • the STRUCTURAL backstop closes the name-only-dump hole NOW (not "someday"), guarded here.
  • a KNOWN-LIMITATION strict-xfail documents the alphanumeric/dotted-code gap the numeric regex
    leaves open — so the moment that hole is closed the xfail flips to a passing test.
The asymmetry (mis-demoting a real statement is safe; letting a dump through is not) is encoded:
the structural bar is conservative, and a real-statement shape is never flagged.
"""
import pytest

from backend.dataimport.preingest3 import tiers, family


def _rows(labels, first_col_pad=0):
    """A minimal grid: one label per row in column `first_col_pad`. axis row is row 0."""
    grid = [['PERIOD']]                       # row 0 = axis header
    for l in labels:
        grid.append([''] * first_col_pad + [l])
    return grid


# ── GL-code signature (primary) ──────────────────────────────────────────────
def test_gl_code_fraction_separates_dump_from_statement():
    dump = ['83231010 - Sponsorship-cash', '410000 Turnover', '520000 COGS', '600100 Salaries']
    stmt = ['Turnover', 'Cost of Sales', 'Gross Profit', 'Operating Profit', 'EBITDA']
    assert tiers.gl_code_fraction(dump) == 1.0
    assert tiers.gl_code_fraction(stmt) == 0.0


def test_is_dump_on_long_gl_code_export():
    # a LONG code-labelled export → dump. Length is NECESSARY: the same code density on a SHORT
    # sheet is NOT a dump (next test), because a real statement can carry codes too.
    labels = [f'{410000 + i} Account {i}' for i in range(tiers.DUMP_MIN_LABELS + 20)]
    assert tiers.gl_code_fraction(labels) == 1.0
    assert tiers.is_dump(_rows(labels), 1, 0) is True


def test_short_code_labelled_statement_is_not_a_dump():
    # THE ANALISA REGRESSION, generalised: a real P&L labelled by account CODE (77% GL-code on
    # Analisa `ProfitLoss (23)`) but SHORT and hierarchical must NOT be demoted. Length necessary.
    labels = ['410000 Turnover', '520000 COGS', 'TOTAL REVENUE', '600100 Salaries',
              'Gross Profit', 'Operating Profit', 'EBITDA', 'Net Profit']
    assert tiers.gl_code_fraction(labels) > tiers.DUMP_GL_FRACTION      # code-heavy …
    assert tiers.is_dump(_rows(labels), 1, 0) is False                  # … but short ⇒ not a dump


def test_real_statement_is_not_a_dump():
    labels = ['Turnover', 'Cost of Sales', 'Gross Profit', 'Operating Expenses',
              'Operating Profit', 'EBITDA', 'Depreciation', 'Profit Before Tax', 'Net Profit']
    assert tiers.is_dump(_rows(labels), 1, 0) is False


# ── structural backstop (name-only dump) ─────────────────────────────────────
def test_structural_backstop_flags_name_only_ledger():
    # the hole the GL-code signal leaves: hundreds of flat account NAMES, no numeric prefix,
    # no roll-up lines. Scores 0% GL-code, so ONLY the structural signature can catch it.
    names = [f'Sundry account {i}' for i in range(tiers.DUMP_MIN_LABELS + 20)]
    assert tiers.gl_code_fraction(names) == 0.0            # invisible to the GL-code signal
    assert tiers.is_structural_dump(names) is True
    assert tiers.is_dump(_rows(names), 1, 0) is True


def test_structural_backstop_does_not_flag_a_long_real_statement():
    # a long detailed statement still ROLLS UP — it carries subtotal lines. Even past the length
    # gate the subtotal fraction stays well above the floor, so it is never mis-demoted (safe dir).
    labels = []
    for i in range(tiers.DUMP_MIN_LABELS + 20):
        labels.append(f'Line item {i}')
        if i % 8 == 0:
            labels.append(f'Total of group {i}')          # a real hierarchy's roll-up lines
    assert len(labels) >= tiers.DUMP_MIN_LABELS
    assert tiers.is_structural_dump(labels) is False


def test_short_flat_sheet_is_not_a_structural_dump():
    # below the length gate, a flat sheet is small enough to be a real statement — never demoted.
    assert tiers.is_structural_dump(['Bank charges', 'Salaries', 'Rent', 'Utilities']) is False


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN LIMITATION: the GL-code regex matches a leading 3+ DIGIT code only. A LONG dump labelled "
    "by an ALPHANUMERIC/DOTTED scheme ('GL-4000 Turnover', '4000.01 Bank') scores 0% GL-code and, if "
    "it also carries roll-up captions (so the flat-structural backstop won't fire either), passes "
    "axis-1 as a statement. Length is satisfied here — the ONLY reason it is missed is the numeric-"
    "only regex. Closing it means generalising the code regex; when that lands this xpasses — flip "
    "to a normal test. Documented, not silent (advisor 2026-07-27)."))
def test_alphanumeric_coded_dump_is_currently_missed():
    labels = []
    for i in range(tiers.DUMP_MIN_LABELS + 20):            # LONG, so length is NOT why it's missed
        labels.append(f'GL-{4000 + i} Account {i}')
        if i % 10 == 0:
            labels.append(f'Sub-total block {i}')          # captions present → structural won't fire
    assert tiers.is_dump(_rows(labels), 1, 0) is True       # DESIRED; currently False (regex miss)


@pytest.mark.xfail(strict=True, reason=(
    "KNOWN LIMITATION / the length-gate's ONE unguarded direction (advisor 2026-07-27). DUMP_MIN_LABELS "
    "is a DATA-CALIBRATED constant in the (133, 371] gap of THESE files, not a principle. Its unrescued "
    "failure case: a LONG *and* code-heavy REAL statement — a big company's detailed coded P&L at ~300 "
    "lines @ ~86% GL-code. It passes the necessary length gate AND trips the GL-code corroborator, so "
    "is_dump demotes it — a FALSE POSITIVE. It IS hierarchical (carries roll-up subtotals, so the "
    "structural backstop correctly abstains), yet GL-code density alone demotes it, and no current "
    "signal reliably separates a long-coded STATEMENT from a long-coded DUMP. It fails CLOSED (the "
    "statement is dropped as a source → a summary sheet wins Agnikul-style, or the concept holds; NO "
    "wrong number), so it is acceptable — but it costs COVERAGE and WILL appear in the 30-file set. "
    "Auto-alerts here so the threshold's blind direction is visible, not silent. Revisit DUMP_MIN_LABELS "
    "when a real file lands in the gap. When a rescue lands (a family-CONFIRMS override, or a hierarchy "
    "metric that separates), this xpasses — flip it to a normal test."))
def test_long_coded_real_statement_is_currently_demoted():
    labels = []
    for i in range(tiers.DUMP_MIN_LABELS + 100):          # LONG: passes the necessary length gate
        labels.append(f'{410000 + i} Detail account {i}')
        if i % 6 == 0:
            labels.append(f'Total of division {i}')        # a REAL hierarchy's roll-ups → structural abstains
    assert tiers.gl_code_fraction(labels) >= tiers.DUMP_GL_FRACTION    # code-heavy (corroborator fires)
    assert tiers.is_structural_dump(labels) is False                  # but genuinely hierarchical
    assert tiers.is_dump(_rows(labels), 1, 0) is False    # DESIRED (it's a real statement); currently True


# ── structure_rank (co-CONFIRMED tiebreak, above lex-min) ────────────────────
def test_structure_rank_prefers_statement_over_flat():
    stmt = _rows(['Revenue', 'Total Income', 'Net Profit'])
    flat = _rows(['Sundry a', 'Sundry b', 'Sundry c'])
    assert tiers.structure_rank(stmt, 1, 0) == 0            # has roll-up → statement-shaped
    assert tiers.structure_rank(flat, 1, 0) == 1            # no roll-up → flat, deprioritised


# ── verdict_rank (axis-2 ordering, a sub-tiebreak only) ──────────────────────
def test_verdict_rank_orders_confirmed_over_insufficient_over_contradicted():
    assert tiers.verdict_rank([family.CONFIRMED]) == 0
    assert tiers.verdict_rank([family.INSUFFICIENT]) == 1
    assert tiers.verdict_rank([family.CONTRADICTED]) == 2
    assert tiers.verdict_rank([family.CONTRADICTED, family.CONFIRMED]) == 0   # best wins
    assert tiers.verdict_rank([]) == 1                                        # empty = neutral
