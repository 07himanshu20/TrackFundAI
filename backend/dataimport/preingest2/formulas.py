"""
Formula engine — the derivations from formula.html (Phase-6 authoritative rules),
in pure deterministic Python. No model involvement; operates only on the frozen
ledger records + scheme LPA terms.

Produces the three derived sheets:
  • cover_snapshot  — fund-level performance & capital metrics
  • nav_buildup     — Fund NAV line-item build-up
  • waterfall       — European whole-fund waterfall steps

Every metric follows the priority ladders in formula.html; when inputs are
insufficient a metric is emitted blank with a reason — never a fabricated value.
"""
import re

from .schema import is_summary_label


def _f(v):
    if v is None or v == '':
        return None
    try:
        return float(str(v).replace(',', '').replace('%', '').strip())
    except (TypeError, ValueError):
        return None


def _sum(records, field):
    tot, seen = 0.0, False
    for r in records:
        n = _f(r.get(field))
        if n is not None:
            tot += n
            seen = True
    return tot if seen else None


def _dist_rows(exits_distributions):
    return [r for r in exits_distributions
            if any(r.get(k) not in (None, '') for k in
                   ('distribution_number', 'distribution_date', 'distribution_type',
                    'total_gross_amount', 'total_net_amount'))]


def _exit_rows(exits_distributions):
    return [r for r in exits_distributions
            if any(r.get(k) not in (None, '') for k in
                   ('exit_date', 'exit_type', 'proceeds', 'net_exit_proceeds',
                    'realized_gain_loss'))]


def _scheme_terms(master_records):
    """Parse key-value fund-master records into a term dict by fuzzy label."""
    kv = {}
    for r in master_records:
        li = r.get('line_item')
        if li in (None, ''):
            continue
        kv[str(li).strip().lower()] = r.get('value')

    def find(*keys):
        for k in keys:
            for lk, v in kv.items():
                if k in lk:
                    n = _f(v)
                    return n if n is not None else v
        return None

    return {
        'carry_pct': find('carried interest', 'carry %', 'carry percentage', 'carry'),
        'hurdle_pct': find('hurdle', 'preferred return'),
        'holdback_pct': find('holdback', 'clawback reserve', 'gp holdback'),
        'mgmt_fee_pct': find('management fee'),
        'units': find('total units', 'units issued'),
        'cash': find('cash & cash', 'cash and cash', 'undistributed cash', 'cash'),
        'receivables': find('receivable'),
        'mgmt_fee_payable': find('management fee payable', 'fee payable'),
        'other_liab': find('other liabilities', 'other liability'),
    }


def _xirr(cashflows):
    """XIRR by bisection over annual rate [-0.99, 10.0]; formula.html spec.
    cashflows = [(date_ordinal, amount), ...]."""
    if len(cashflows) < 2:
        return None
    t0 = min(d for d, _ in cashflows)

    def npv(rate):
        return sum(a / (1 + rate) ** ((d - t0) / 365.25) for d, a in cashflows)

    lo, hi = -0.99, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-9:
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    r = (lo + hi) / 2
    return r if -0.9999 <= r <= 9.9999 else None


def _ordinal(v):
    """Parse a date-ish value to an ordinal day count. Accepts iso/date."""
    from datetime import date, datetime
    if isinstance(v, (datetime, date)):
        return (v if isinstance(v, date) and not isinstance(v, datetime) else v.date()
                if isinstance(v, datetime) else v).toordinal()
    if v in (None, ''):
        return None
    s = str(v)
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).toordinal()
    m = re.match(r'(\d{1,2})[-/](\w{3})[-/](\d{2,4})', s)
    if m:
        mm = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7,
              'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}.get(m.group(2).lower()[:3])
        if mm:
            y = int(m.group(3))
            y += 2000 if y < 100 else 0
            try:
                return date(y, mm, int(m.group(1))).toordinal()
            except ValueError:
                return None
    return None


class FundMetrics:
    """Compute all fund-level derivations from the domain buckets."""

    def __init__(self, dom):
        self.commit = dom.get('investors_aml', []) + dom.get('commitments', [])
        self.calls = dom.get('capital_calls', [])
        self.invest = [r for r in dom.get('portfolio_investments', [])
                       if not is_summary_label(r.get('company_name'))]
        self.tranches = [r for r in dom.get('investment_tranches', [])
                         if not is_summary_label(r.get('company_name'))]
        self.vals = dom.get('valuations_kpis', [])
        self.exdist = dom.get('exits_distributions', [])
        self.terms = _scheme_terms(dom.get('fund_scheme_master', []))
        self.dists = _dist_rows(self.exdist)
        self.exits = _exit_rows(self.exdist)

    # ── capital metrics (formula.html §4) ────────────────────────────────
    def total_commitments(self):
        return _sum(self.commit, 'commitment_amount')

    def total_called(self):
        c = _sum(self.calls, 'total_call_amount')
        if c is not None:
            return c
        return _sum(self.commit, 'cumulative_called')      # Layer B fallback

    def uncalled(self):
        tc, cc = self.total_commitments(), self.total_called()
        return max(0.0, tc - cc) if (tc is not None and cc is not None) else None

    def total_invested(self):
        v = _sum(self.invest, 'total_invested')
        if v is not None:
            return v
        return _sum(self.tranches, 'tranche_amount')        # derive from tranches

    def total_distributions(self):
        v = _sum(self.dists, 'total_net_amount')
        if v is None:
            v = _sum(self.dists, 'total_gross_amount')
        if v is None:
            v = _sum(self.commit, 'cumulative_distributed')
        return v

    def total_realised(self):
        v = _sum(self.exits, 'net_exit_proceeds')
        return v if v is not None else _sum(self.exits, 'proceeds')

    def residual_fv(self):
        # latest fair value of holdings still held
        return _sum(self.vals, 'fair_value_of_holding')

    # ── waterfall (formula.html §3) ──────────────────────────────────────
    def gp_carry_gross(self):
        base = self.carry_base()
        cp = self.terms.get('carry_pct')
        if base is None or cp is None:
            return None
        return (cp / 100.0) * base

    def carry_base(self):
        realised = self.total_realised() or 0.0
        resid = self.residual_fv() or 0.0
        called = self.total_called()
        if called is None:
            return None
        return max(0.0, (realised + resid) - called)

    def gp_carry_net(self):
        gross = self.gp_carry_gross()
        hb = self.terms.get('holdback_pct')
        if gross is None or hb is None:
            return None
        return gross * (1 - hb / 100.0)

    def residual_nav(self):
        fv = self.residual_fv()
        if fv is None:
            return None
        accrued = self.gp_carry_gross() or 0.0
        return fv - max(0.0, accrued)

    def return_of_capital(self):
        called, dist = self.total_called(), self.total_distributions()
        if called is None or dist is None:
            return None
        return min(called, dist)

    def preferred_return(self):
        hp = self.terms.get('hurdle_pct')
        if hp is None or not self.calls:
            return None
        # single as_of = latest call date + 1yr proxy is avoided; use max call date
        ords = [_ordinal(r.get('call_date')) for r in self.calls]
        ords = [o for o in ords if o]
        if not ords:
            return None
        as_of = max(ords)
        tot = 0.0
        seen = False
        for r in self.calls:
            amt = _f(r.get('total_call_amount'))
            o = _ordinal(r.get('call_date'))
            if amt is None or o is None:
                continue
            yrs = (as_of - o) / 365.25
            tot += amt * ((1 + hp / 100.0) ** yrs - 1)
            seen = True
        return tot if seen else None

    # ── performance (formula.html §2) ────────────────────────────────────
    def moic(self):
        inv = self.total_invested()
        if not inv:
            return None
        realised = self.total_realised() or 0.0
        resid = self.residual_fv() or 0.0
        return (realised + resid) / inv

    def tvpi(self):
        called = self.total_called()
        if not called:
            return None
        dist = self.total_distributions() or 0.0
        rnav = self.residual_nav()
        if rnav is None:
            return None
        return (dist + rnav) / called

    def dpi(self):
        called = self.total_called()
        dist = self.total_distributions()
        if not called or dist is None:
            return None
        return dist / called

    def rvpi(self):
        called = self.total_called()
        rnav = self.residual_nav()
        if not called or rnav is None:
            return None
        return rnav / called

    def net_irr(self):
        cfs = []
        for r in self.calls:
            o = _ordinal(r.get('call_date'))
            a = _f(r.get('total_call_amount'))
            if o and a:
                cfs.append((o, -a))
        for r in self.dists:
            o = _ordinal(r.get('distribution_date'))
            a = _f(r.get('total_net_amount')) or _f(r.get('total_gross_amount'))
            if o and a:
                cfs.append((o, a))
        term = self.residual_fv()
        if term and cfs:
            as_of = max(o for o, _ in cfs)
            cfs.append((as_of, term))
        r = _xirr(cfs)
        return r * 100 if r is not None else None

    # ── Fund NAV (formula.html §5) ───────────────────────────────────────
    def fund_nav(self):
        realised_gain = _sum(self.exits, 'realized_gain_loss')
        if realised_gain is None:
            realised_gain = self.total_realised() or 0.0
        unreal = self.residual_fv()
        if unreal is None:
            return None, None
        cash = self.terms.get('cash') or 0.0
        recv = self.terms.get('receivables') or 0.0
        mfp = self.terms.get('mgmt_fee_payable') or 0.0
        carry_pay = self.gp_carry_gross() or 0.0
        other = self.terms.get('other_liab') or 0.0
        comps = [
            ('(+) Realised Gains on Exits', realised_gain),
            ('(+) Unrealised Value (Residual portfolio at FV)', unreal),
            ('(+) Cash & Cash Equivalents', cash),
            ('(+) Receivables', recv),
            ('(−) Management Fee Payable', -mfp),
            ('(−) Carry Payable', -carry_pay),
            ('(−) Other Liabilities', -other),
        ]
        nav = sum(v for _, v in comps)
        return nav, comps

    def nav_per_unit(self):
        nav, _ = self.fund_nav()
        u = self.terms.get('units')
        if nav is None or not u:
            return None
        return nav * 100.0 / u          # ₹ Cr → ₹ Lakhs per unit

    def catch_up(self):
        pref = self.preferred_return()
        cp = self.terms.get('carry_pct')
        if pref is None or cp is None:
            return None
        c = cp / 100.0
        if c >= 1:
            return None
        tier = pref * (c / (1 - c))
        dist = self.total_distributions()
        roc = self.return_of_capital()
        if dist is not None and roc is not None:
            avail = max(0.0, dist - roc - pref)
            return min(tier, avail)
        return tier

    def clawback(self):
        gp_dist = _sum(self.dists, 'gp_carry_amount')
        gross = self.gp_carry_gross()
        if gp_dist is None or gross is None:
            return None
        return max(0.0, gp_dist - gross)
