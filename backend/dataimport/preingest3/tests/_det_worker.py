"""Determinism-gate SUBPROCESS worker (invoked by test_determinism_gate.py).

Emits a CANONICAL serialization of the deterministic CIR for ONE file so two
SEPARATE processes — run under different PYTHONHASHSEED — can be diffed byte-for-byte.
Cross-process is the ONLY way to catch set/dict-iteration-order nondeterminism (e.g.
`_best_sheet`'s pick over 68 sheets, unit-hint set scans): an in-process 'run twice'
check shares one hash seed and would pass while real production runs diverge.

Run as a module from the backend/ dir so `dataimport...` resolves:
    PYTHONHASHSEED=<n> python -m dataimport.preingest3.tests._det_worker <IN_DIR> <FILE> [TOKEN]
Prints JSON (sort_keys) of every emitted/held Figure to stdout. use_model=False:
the deterministic path only — the gate proves the CODE is order-invariant before the
model is ever wired in, so any future nondeterminism is unambiguously the model's.

Inputs mirror EXACTLY what _run_job passes in production: default_inr_card (INR-only —
foreign-currency money correctly HOLDS) + fund anchors (domicile + anchor_cr, which
pipeline.run builds internally from the fund files in the run). Pinning to the shipping
config is deliberate: a determinism claim true only of a richer, non-shipping config
(e.g. a supplied multi-ccy card) would be a green production doesn't actually have. If
TOKEN is given and matches a fund anchor, the anchor's domicile + anchor_cr are passed in.
"""
import json
import os
import sys
import warnings

warnings.filterwarnings('ignore')

from dataimport.preingest3 import fund_anchor                    # noqa: E402
from dataimport.preingest3.extract import extract_company        # noqa: E402
from dataimport.preingest3.ratecard import default_inr_card      # noqa: E402
from dataimport.preingest3.cir import Figure                     # noqa: E402

_FUND = ['TFAI_Investments_and_Deployment.xlsx', 'TFAI_Valuations_and_Exits_Q2FY26.xlsx']


def main() -> int:
    in_dir, fname = sys.argv[1], sys.argv[2]
    token = (sys.argv[3].lower() if len(sys.argv) > 3 else None)
    entity = os.path.splitext(fname)[0]
    domicile = anchor_cr = None
    if token:
        anchors = fund_anchor.build_fund_anchors([os.path.join(in_dir, f) for f in _FUND])
        ca = next((c for k, c in anchors.items() if token in k.lower()), None)
        if ca is not None:
            domicile, anchor_cr = ca.domicile, ca.anchor_cr
    rec = extract_company(entity, os.path.join(in_dir, fname),
                          rate_card=default_inr_card('2026-02-28'), entity=entity,
                          domicile=domicile, anchor_cr=anchor_cr, use_model=False)
    out = {}
    for v in rec.fields.values():
        if isinstance(v, Figure):
            out[v.concept] = {
                'value': (str(v.value_cr) if v.value_cr is not None else None),
                'cell': v.provenance.cell,
                'sheet': v.provenance.sheet,
                'basis': str(v.basis),
                'held': v.held,
                'reason': (v.hold_reason or '')[:60],
            }
    print(json.dumps(out, sort_keys=True, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
