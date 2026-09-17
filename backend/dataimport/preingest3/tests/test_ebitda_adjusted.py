"""Lever 5 sub-A2 — the EBITDA adjusted-companion (`ebitda_adjusted`) + the fail-closed cross-company
total. Four-bar synthetic-adversarial coverage (byte-distinct from the corpus cells); the exact real
values (LDC ₹113.6891 ESOP, Analisa ₹0.9194 normalized) are proven in test_real_files_values /
test_foreign_coverage_gate.

PRINCIPLE: the comparable primary `ebitda` stays PLAIN/Standard; the adjusted is a DISCLOSED companion
carrying its adjustment_type, verified `adjusted == plain + stated adjustment` or held, and NEVER blended
into a cross-company total (which is plain-only, same-basis-or-INCOMPLETE).
"""
import types
from decimal import Decimal

from django.test import SimpleTestCase

from backend.dataimport.preingest3 import extract as ex
from backend.dataimport.preingest3.cir import Figure, Provenance
from backend.dataimport.preingest3.ratecard import default_inr_card
# preingest3_views imports Django models → import via the app-registered `dataimport.` path (see
# test_currency_report_api.py), NOT `backend.dataimport.`, or the model app_label won't resolve.
from dataimport.preingest3_views import _concept_total

_RC = default_inr_card('2026-06-30')


def _prov(sheet='S', row_label='EBITDA', cell='B2', derived=('B2',)):
    return Provenance(source_file='t.xlsx', content_fingerprint='fp', sheet=sheet, cell=cell,
                      row_label=row_label, derived_from=list(derived))


class Helpers(SimpleTestCase):
    def test_proxy_adjustment_type_is_nonstandard_only(self):
        # a NON-standard add-back (ESOP/exceptional) makes a proxy ADJUSTED; standard add-backs do not.
        self.assertEqual(ex._proxy_adjustment_type('Profit Before Tax, depreciation and ESOP'), 'ESOP')
        self.assertEqual(ex._proxy_adjustment_type('Profit before tax, depreciation and exceptional items'),
                         'exceptional')
        self.assertIsNone(ex._proxy_adjustment_type('Profit before tax and depreciation'))   # standard proxy
        self.assertIsNone(ex._proxy_adjustment_type('EBITDA'))

    def test_adjustment_type_label(self):
        self.assertEqual(ex._adjustment_type_label('Normalized EBITDA', 'Less Forex reclass'), 'forex-normalized')
        self.assertEqual(ex._adjustment_type_label('Adjusted EBITDA', 'ESOP charge'), 'ESOP')
        self.assertEqual(ex._adjustment_type_label('Normalized EBITDA', 'One-time adjustment'), 'normalized')

    def test_col_of_a1(self):
        self.assertEqual(ex._col_of_a1('A1'), 0)
        self.assertEqual(ex._col_of_a1('H11'), 7)
        self.assertEqual(ex._col_of_a1('AA3'), 26)
        self.assertIsNone(ex._col_of_a1('11'))

    def test_row_sum_at(self):
        rows = [['x', 10, 20, 'N/A'], ['y', 1, 2, 3]]
        self.assertEqual(ex._row_sum_at(rows, 0, [1, 2]), Decimal('30'))
        self.assertEqual(ex._row_sum_at(rows, 0, [1, 2, 3]), Decimal('30'))   # text cell skipped
        self.assertIsNone(ex._row_sum_at(rows, 0, [3]))                       # only non-numeric → None
        self.assertIsNone(ex._row_sum_at(rows, None, [1]))

    def test_find_normalized_and_adjustment_rows(self):
        rows = [['Line', 'C1'], ['EBITDA', 100], ['One-time adjustment', 20],
                ['Normalized EBITDA', 120], ['Revenue', 500]]
        cols = [types.SimpleNamespace(col=1)]
        self.assertEqual(ex._find_normalized_ebitda_row(rows, 0, cols), 3)   # 'Normalized EBITDA'
        self.assertEqual(ex._find_adjustment_row(rows, 0, cols), 2)          # 'One-time adjustment'
        # must-not-misfire: a plain-only sheet has neither
        plain = [['Line', 'C1'], ['EBITDA', 100], ['Revenue', 500]]
        self.assertIsNone(ex._find_normalized_ebitda_row(plain, 0, cols))
        self.assertIsNone(ex._find_adjustment_row(plain, 0, cols))


class DerivePath2Proxy(SimpleTestCase):
    def test_adjusted_proxy_moves_to_companion_and_holds_plain(self):
        # PATH 2 (LDC-shape): the sole stated figure is an ESOP-adjusted proxy → plain ebitda HOLDS, the
        # figure moves to ebitda_adjusted tagged ESOP. Comparable column never carries the adjusted value.
        primary = Figure('ebitda', Decimal('113.6891'), None,
                         _prov(row_label='Profit Before Tax, depreciation and ESOP'), basis='TTM', months=12)
        fields = {'ebitda': primary}
        ex._derive_ebitda_adjusted(fields, {'grid': {}}, geo_ccy=None, inr_mentioned=True,
                                   base_currency=None, anchor_cr=Decimal('611'), rate_card=_RC,
                                   as_of=(2026, 2))
        self.assertTrue(fields['ebitda'].held and fields['ebitda'].value_cr is None)
        adj = fields['ebitda_adjusted']
        self.assertTrue(adj.confirmed and adj.value_cr == Decimal('113.6891'))
        self.assertEqual(adj.adjustment_type, 'ESOP')

    def test_standard_proxy_stays_primary_no_companion(self):
        # a proxy with only STANDARD add-backs (no ESOP/exceptional) is the standard EBITDA — stays primary.
        primary = Figure('ebitda', Decimal('50'), None,
                         _prov(row_label='Profit before tax and depreciation'), basis='TTM', months=12)
        fields = {'ebitda': primary}
        ex._derive_ebitda_adjusted(fields, {'grid': {}}, geo_ccy=None, inr_mentioned=True,
                                   base_currency=None, anchor_cr=Decimal('611'), rate_card=_RC)
        self.assertTrue(fields['ebitda'].confirmed and fields['ebitda'].value_cr == Decimal('50'))
        self.assertNotIn('ebitda_adjusted', fields)


class DerivePath1InSheet(SimpleTestCase):
    # an INR-Cr comparison grid (period pair REPEATS so it is a grid) with plain EBITDA + a one-time
    # adjustment + normalized EBITDA; the primary was read at column B (derived_from=['B2']).
    def _grid(self, plain, adj, norm):
        return [['Summary P&L (INR Cr)', 'FY25', 'FY25'],
                ['EBITDA', plain, plain], ['One-time adjustment', adj, adj],
                ['Normalized EBITDA', norm, norm], ['Revenue', 500, 500]]

    def _run(self, plain, adj, norm):
        prov = _prov(sheet='S', row_label='EBITDA', cell='B2', derived=('B2',))
        primary = Figure('ebitda', Decimal(str(plain)), None, prov, basis='TTM', months=12)
        fields = {'ebitda': primary}
        prof = {'grid': {'S': self._grid(plain, adj, norm)}}
        ex._derive_ebitda_adjusted(fields, prof, geo_ccy=None, inr_mentioned=True, base_currency=None,
                                   anchor_cr=Decimal('100'), rate_card=_RC, as_of=(2025, 3))
        return fields

    def test_bridge_verifies_emits_companion(self):
        # MUST-HANDLE: normalized(120) == plain(100) + adjustment(20) → companion EMITS, tagged normalized,
        # and (same frame) larger than the plain primary. Primary stays plain/confirmed.
        fields = self._run(100, 20, 120)
        self.assertTrue(fields['ebitda'].confirmed)                      # primary unchanged
        adj = fields['ebitda_adjusted']
        self.assertTrue(adj.confirmed and adj.value_cr > fields['ebitda'].value_cr)
        self.assertEqual(adj.adjustment_type, 'normalized')

    def test_bridge_breaks_holds_companion(self):
        # MUST-NOT-MISFIRE: normalized(999) != plain(100) + adjustment(20) → companion HELD (verify-or-hold),
        # primary still plain/confirmed. Never emit an unverified adjusted figure.
        fields = self._run(100, 20, 999)
        self.assertTrue(fields['ebitda'].confirmed)
        adj = fields['ebitda_adjusted']
        self.assertTrue(adj.held and adj.value_cr is None)

    def test_no_normalized_row_no_companion(self):
        # a plain-only sheet (no normalized EBITDA row) → no companion at all.
        prov = _prov(sheet='S', row_label='EBITDA', cell='B2', derived=('B2',))
        fields = {'ebitda': Figure('ebitda', Decimal('100'), None, prov, basis='TTM', months=12)}
        prof = {'grid': {'S': [['Summary P&L (INR Cr)', 'FY25', 'FY25'], ['EBITDA', 100, 100],
                               ['Revenue', 500, 500]]}}
        ex._derive_ebitda_adjusted(fields, prof, geo_ccy=None, inr_mentioned=True, base_currency=None,
                                   anchor_cr=Decimal('100'), rate_card=_RC, as_of=(2025, 3))
        self.assertNotIn('ebitda_adjusted', fields)


class FailClosedTotal(SimpleTestCase):
    def _co(self, **figs):
        return {'figures': {c: v for c, v in figs.items()}}

    def _fj(self, value, basis='TTM', state='emitted'):
        return {'value_cr': value, 'basis': basis, 'state': state}

    def test_homogeneous_basis_sums(self):
        companies = {'a': self._co(ebitda=self._fj(10, 'TTM')), 'b': self._co(ebitda=self._fj(20, 'TTM'))}
        self.assertEqual(_concept_total(companies, 'ebitda'), 30)

    def test_mixed_basis_is_incomplete(self):
        # a TTM + a YTD EBITDA must NOT be blended into a single number.
        companies = {'a': self._co(ebitda=self._fj(10, 'TTM')), 'b': self._co(ebitda=self._fj(20, 'YTD'))}
        self.assertEqual(_concept_total(companies, 'ebitda'), 'INCOMPLETE')

    def test_a_held_member_is_incomplete(self):
        companies = {'a': self._co(ebitda=self._fj(10, 'TTM')),
                     'b': self._co(ebitda=self._fj(None, 'TTM', state='held'))}
        self.assertEqual(_concept_total(companies, 'ebitda'), 'INCOMPLETE')

    def test_adjusted_companion_is_never_summed_into_the_plain_total(self):
        # the plain ebitda total ignores ebitda_adjusted entirely (separate concept) — no plain+adjusted mix.
        companies = {'ldc': self._co(ebitda=self._fj(None, 'TTM', state='held'),
                                     ebitda_adjusted=self._fj(113.69, 'TTM')),
                     'x': self._co(ebitda=self._fj(15.0, 'TTM'))}
        # ebitda total: LDC held → INCOMPLETE (never silently drops LDC or pulls its adjusted)
        self.assertEqual(_concept_total(companies, 'ebitda'), 'INCOMPLETE')
        # the adjusted concept has its own (here homogeneous) total, kept separate
        self.assertEqual(_concept_total(companies, 'ebitda_adjusted'), 113.69)
