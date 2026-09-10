"""TIER-S SCAFFOLD self-test (token-free): prove the INSTRUMENT is sound before spending any tokens on
a locating run. Three properties, all fingerprint/re-read only (no model):
  1. content_fp uniqueness (item 4) — every variant is a distinct file, so dedupe cannot collapse it and
     a coverage-robustness run is not vacuous.
  2. layout actually changed — layout_fp of each variant differs from the source (a real perturbation,
     not a no-op copy).
  3. ground-truth map integrity — each planted sentinel really lives at its recorded cell in the saved
     variant, so a later `located == sentinel` assertion is checking against a correct map.

The locating RUN (assert the locator returns each sentinel across the perturbations) is the only
token-spend and is DEFERRED to a greenlight — it is fail-safe (a miss is a flagged blank, never a wrong
number) so it does NOT gate go-live."""
import os
import shutil
import tempfile

import openpyxl
import pytest

from backend.dataimport.preingest3.identity import compute_identity
from backend.dataimport.preingest3.tests import tier_s


def _source(path):
    """A representative multi-sheet MIS-shaped source: text label -> numeric value rows."""
    wb = openpyxl.Workbook()
    for s, name in enumerate(['P&L', 'Balance', 'KPI']):
        ws = wb.create_sheet(name) if s else wb.active
        if s == 0:
            wb.active.title = name
        ws.append(['Particulars', 'Apr-25', 'May-25'])
        ws.append(['Revenue', 100, 110])
        ws.append(['EBITDA', 20, 22])
        ws.append(['Total', 120, 132])
    wb.save(path)
    return path


def test_tier_s_scaffold_is_sound():
    with tempfile.TemporaryDirectory() as d:
        src = _source(os.path.join(d, 'src.xlsx'))
        src_id = compute_identity(src, src)

        variants = []
        all_planted = []
        for seed in range(3):
            dst = os.path.join(d, f'variant_{seed}.xlsx')
            planted = tier_s.scramble(src, dst, seed=seed)
            assert planted, f'variant {seed} planted no sentinels — source has no text->numeric rows'
            tier_s.verify_ground_truth(dst, planted)          # (3) map integrity
            var_id = compute_identity(dst, dst)
            assert var_id.layout_fp != src_id.layout_fp, f'variant {seed} did not change layout'
            variants.append(dst)
            all_planted.append(planted)

        # (1) content_fp uniqueness across source + all variants (item 4 — no dedupe collapse)
        tier_s.assert_distinct_content_fp([src] + variants)

        # sentinels are genuinely distinct per variant (why content_fp differs)
        sentinels = {p.sentinel for pl in all_planted for p in pl}
        assert len(sentinels) == sum(len(pl) for pl in all_planted), 'sentinels collided across variants'


def test_content_fp_uniqueness_guard_reddens_on_collision():
    """Negative control (item 4): assert_distinct_content_fp must FAIL when two files share a content_fp
    (a byte-copy has identical parsed content) — proving the discipline catches the dedupe-collapse bug
    it targets, not just passing vacuously on already-distinct inputs."""
    with tempfile.TemporaryDirectory() as d:
        a = _source(os.path.join(d, 'a.xlsx'))
        b = os.path.join(d, 'b.xlsx')
        shutil.copyfile(a, b)                                  # same parsed content -> same content_fp
        with pytest.raises(AssertionError, match='DUPLICATE content_fp'):
            tier_s.assert_distinct_content_fp([a, b])
