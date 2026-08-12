"""Fund-region determinism SUBPROCESS worker (invoked by test_fund_output.py).

Emits a CANONICAL serialization of the assembled FUND region (Fund_Summary +
LP_Register + Fund_Terms) plus the term-reconciliation checks for the real fund
files, so two SEPARATE processes under different PYTHONHASHSEED can be diffed
byte-for-byte. Cross-process is the ONLY way to catch set/dict-iteration-order
nondeterminism — the same discipline the MIS determinism gate uses (_det_worker),
now applied to the fund output so Fund_Terms is under the SAME cross-process gate,
not merely a same-run re-hash.

    PYTHONHASHSEED=<n> python -m dataimport.preingest3.tests._fund_det_worker <IN_DIR>
"""
import json
import os
import sys
import warnings

warnings.filterwarnings('ignore')

from dataimport.preingest3 import pipeline, assemble               # noqa: E402
from dataimport.preingest3.ratecard import default_inr_card        # noqa: E402

_FUND = ['TFAI_Fund_Terms_and_LP_Register_wip.xlsx',
         'TFAI_Fund_Accounts_Fees_Budget_Compliance.xlsx',
         'TFAI_Capital_Calls_and_Distributions_draft.xlsx']


def main() -> int:
    in_dir = sys.argv[1]
    res = pipeline.run([(f, os.path.join(in_dir, f)) for f in _FUND], as_of='2026-06-30',
                       org='det', rate_card=default_inr_card('2026-06-30'))
    wb = assemble.build(res.cir, rate_card=default_inr_card('2026-06-30'))
    out = {'sheets': {}, 'checks': []}
    for sn in ('Fund_Summary', 'LP_Register', 'Fund_Terms'):
        out['sheets'][sn] = [['' if c is None else str(c) for c in row]
                             for row in wb[sn].iter_rows(values_only=True)]
    out['checks'] = sorted(f'{c["id"]}|{c["status"]}' for c in res.cir.checks
                           if 'term' in c['id'] or 'management_fee' in c['id']
                           or 'nav' in c['id'] or c['id'] == 'fee_base_vs_nav')
    print(json.dumps(out, sort_keys=True, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
