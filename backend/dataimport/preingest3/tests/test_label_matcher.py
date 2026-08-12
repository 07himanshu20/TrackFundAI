"""The ONE label matcher (lexicon.label_matches_any / label_coverage) — the universal
primitive every module matches labels through, so the raw-vs-normalised and substring bug
classes cannot recur in any module.

These are REDDENING CONTROLS pinned to the exact bugs a hand-rolled matcher reintroduced
(nav.py's parallel matcher, 2026-08-07): a synonym authored with '&'/punctuation was
silently dead against normalised text, and a bare 'p l' greedily matched an unrelated
'…ITD P&L' line. If lexicon ever regresses to raw-substring matching, these go RED.
"""
from backend.dataimport.preingest3 import lexicon


# ── the bug shape #1: punctuation-authored synonym must still match (normalise BOTH sides) ──
def test_punctuation_authored_synonym_matches_NEGATIVE_CONTROL():
    # 'Fund P&L' normalises to 'fund p l'; the cell 'Fund P&L (inception to date)' normalises
    # to 'fund p l' (parenthetical dropped) — they MUST meet. Under raw-substring matching the
    # '&' and case would make ' Fund P&L ' ∉ ' fund p l … ' → silently dead. This reddens then.
    assert lexicon.label_matches_any('Fund P&L (inception to date)', ['Fund P&L'])
    assert lexicon.label_matches_any('AMC Fee (p.a.)', ['AMC fee'])
    assert lexicon.label_matches_any('Carry @ 20%', ['carry'])


# ── the bug shape #2: whole-word, never greedy substring ──
def test_whole_word_not_greedy_NEGATIVE_CONTROL():
    # 'Fund P&L' → ['fund','p','l'] must NOT match an as-of line that merely contains 'p l'
    # ('…ITD P&L below') — there is no whole 'fund p l' run there. A bare-'p l' substring
    # matcher (the greedy bug) would wrongly match this and read the wrong section.
    assert not lexicon.label_matches_any('balances as on 30-Jun-26 for NAV; ITD P&L below', ['Fund P&L'])
    # classic substring bug: a short synonym never matches inside a larger word
    assert not lexicon.label_matches_any('coffee budget', ['fee'])
    assert not lexicon.label_matches_any('unrealised gain', ['realised'])   # 'realised' ⊄ 'unrealised'


# ── the as-of hint SHOULD match the same line (it names 'for NAV' as whole words) ──
def test_asof_hint_matches_by_whole_words():
    assert lexicon.label_matches_any('balances as on 30-Jun-26 for NAV; ITD P&L below', ['for NAV'])
    assert lexicon.label_matches_any('balances as on 30-Jun-26 for NAV', ['balances as on'])


# ── negator guard is inherited (structure, not a business blocklist) ──
def test_negator_guard_inherited():
    assert lexicon.label_matches_any('Operating income', ['operating income'])
    assert not lexicon.label_matches_any('Non-operating income', ['operating income'])


# ── coverage ranks the tightest match (a row that IS the label beats a heading) ──
def test_coverage_ranks_label_over_heading():
    tight = lexicon.label_coverage('Carry %', ['carry'])
    loose = lexicon.label_coverage('Management & performance fee working', ['performance fee'])
    assert tight > loose and tight > 0.5 and 0 < loose < 0.5
    assert lexicon.label_coverage('Total Revenue', ['salaries']) == 0.0   # no match → 0.0


# ── matches() is the SAME code path as the ad-hoc matcher (cannot drift) ──
def test_matches_delegates_to_shared_primitive():
    # concept matching and ad-hoc synonym matching share one implementation
    assert lexicon.matches('Total Revenue (₹ Cr)', 'revenue') == \
        lexicon.label_matches_any('Total Revenue (₹ Cr)', lexicon.synonyms('revenue'))


if __name__ == '__main__':
    for n, fn in sorted(globals().items()):
        if n.startswith('test_') and callable(fn):
            fn(); print(f'ok  {n}')
