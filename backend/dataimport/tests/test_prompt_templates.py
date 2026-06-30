"""
Smoke tests for Phase 3 prompt templates.

These tests catch the entire class of bugs caused by an un-escaped `{` or `}`
inside an f-string body — which fires `ValueError: Invalid format specifier`
at runtime inside parallel worker threads and kills every Phase 3 chunk.

WHY THIS TEST EXISTS:
  On 2026-06-30 the L1 prompt template (which was previously built with a
  single multi-thousand-line f-string) gained a JSON example block with
  literal `{` and `}` characters. The build crashed; the import failed.
  All three templates were subsequently migrated to sentinel-based
  templating (plain triple-quoted strings + `.replace('__SENTINEL__', value)`).

  These tests run a lightweight assertion in CI / on every test sweep
  that every template still builds without raising, so the regression
  cannot land silently.

Universal across funds / sources: nothing here is fund-specific — the test
calls each public builder with placeholder strings and asserts (a) it
returns a non-trivial string and (b) the placeholders survive into the
output (so we know the templating substitution actually fired).
"""
from django.test import SimpleTestCase


class PromptTemplateBuildTests(SimpleTestCase):
    """Build every Phase 3 prompt template once with placeholder inputs."""

    def test_layer1_builds(self):
        from dataimport.phase3_layers.prompts.layer1_identity import (
            LAYER1_PROMPT_TEMPLATE,
        )
        out = LAYER1_PROMPT_TEMPLATE('STUB WORKBOOK BODY')
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 5000,
                           'L1 prompt should be substantial (≥5000 chars)')
        self.assertIn('STUB WORKBOOK BODY', out,
                      'workbook_text placeholder must survive substitution')
        self.assertIn('LAYER 1 SCOPE', out,
                      'L1 scope marker missing — template body damaged')
        self.assertIn('workbook_aggregates', out,
                      'Option C workbook_aggregates section missing')
        # No leftover sentinels
        self.assertNotIn('__VOCAB_', out, 'unsubstituted vocab sentinel left')
        self.assertNotIn('__SCHEMA__', out, 'unsubstituted schema sentinel left')

    def test_layer2_builds(self):
        from dataimport.phase3_layers.prompts.layer2_universe import (
            LAYER2_PROMPT_TEMPLATE,
        )
        out = LAYER2_PROMPT_TEMPLATE('STUB WORKBOOK BODY', 'STUB IDENTITY', 'STUB SCOPE')
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 5000)
        self.assertIn('STUB WORKBOOK BODY', out)
        self.assertIn('STUB IDENTITY', out)
        self.assertIn('STUB SCOPE', out)
        self.assertIn('LAYER 2 SCOPE', out)
        self.assertNotIn('__VOCAB_', out)
        self.assertNotIn('__SCHEMA__', out)

    def test_layer3_builds(self):
        from dataimport.phase3_layers.prompts.layer3_timeseries import (
            LAYER3_PROMPT_TEMPLATE,
        )
        out = LAYER3_PROMPT_TEMPLATE('STUB WORKBOOK BODY', 'STUB IDENTITY', 'STUB SCOPE')
        self.assertIsInstance(out, str)
        self.assertGreater(len(out), 5000)
        self.assertIn('STUB WORKBOOK BODY', out)
        self.assertIn('STUB IDENTITY', out)
        self.assertIn('STUB SCOPE', out)
        self.assertIn('LAYER 3 SCOPE', out)
        self.assertNotIn('__VOCAB_', out)
        self.assertNotIn('__SCHEMA__', out)

    def test_layer_templates_contain_no_unescaped_format_specifiers(self):
        """Regression guard — none of the three templates should attempt to
        evaluate an f-string format expression on their body content. We
        assert this by ensuring every builder returns successfully with
        all placeholder substitutions surviving."""
        from dataimport.phase3_layers.prompts.layer1_identity import (
            LAYER1_PROMPT_TEMPLATE,
        )
        from dataimport.phase3_layers.prompts.layer2_universe import (
            LAYER2_PROMPT_TEMPLATE,
        )
        from dataimport.phase3_layers.prompts.layer3_timeseries import (
            LAYER3_PROMPT_TEMPLATE,
        )

        for label, fn, args in [
            ('L1', LAYER1_PROMPT_TEMPLATE, ('WB',)),
            ('L2', LAYER2_PROMPT_TEMPLATE, ('WB', 'ID', 'SC')),
            ('L3', LAYER3_PROMPT_TEMPLATE, ('WB', 'ID', 'SC')),
        ]:
            with self.subTest(template=label):
                try:
                    out = fn(*args)
                except ValueError as e:
                    self.fail(
                        f'{label} prompt builder raised ValueError — '
                        f'likely an un-escaped {{...}} inside an f-string body. '
                        f'Convert that body to plain string + .replace() sentinel. '
                        f'Original error: {e}'
                    )
                self.assertIsInstance(out, str)
                self.assertGreater(len(out), 1000,
                                   f'{label} output suspiciously short')

    def test_workbook_aggregates_schema_documentation_present(self):
        """The Option C JSON example block must survive in L1 output."""
        from dataimport.phase3_layers.prompts.layer1_identity import (
            LAYER1_PROMPT_TEMPLATE,
        )
        out = LAYER1_PROMPT_TEMPLATE('STUB')
        self.assertIn('"metric"', out,
                      'workbook_aggregates JSON example missing — '
                      'a curly-brace escape regression likely happened')
        self.assertIn('"cell"', out)
        self.assertIn('"sheet"', out)
