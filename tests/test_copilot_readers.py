"""Synthetic Copilot event accounting, with no vendor transcripts."""
import importlib.util
import json
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch

from sandbox import redirect_module_paths, restore_module_paths

LOADER = SourceFileLoader('copilot_readers', str(Path(__file__).resolve().parents[1] / 'bin/agentcat'))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
app = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(app)


def event(kind, **data):
    return {'type': kind, 'timestamp': '2026-01-02T03:04:05Z', 'data': data}


def usage(input_tokens=100, output=20, read=30, write=10, reasoning=5):
    return dict(inputTokens=input_tokens, outputTokens=output, cacheReadTokens=read,
                cacheWriteTokens=write, reasoningTokens=reasoning)


def shutdown(**models):
    return event('session.shutdown', modelMetrics={model: {'usage': values} for model, values in models.items()})


class CopilotReadersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        originals = redirect_module_paths(app, root, root / '.agentcat')
        self.addCleanup(restore_module_paths, app, originals)
        self.path = root / 'events.jsonl'

    def parse(self, records):
        self.path.write_text('\n'.join(json.dumps(row) for row in records))
        result = app.empty_provider_usage()
        count = app.parse_copilot_legacy_file(self.path, result)
        return count, result

    def assert_tokens(self, result, total=120, input_tokens=60, output=20, read=30, write=10):
        self.assertEqual(result['tokens']['all'], total)
        for field, expected in [('inputTokens', input_tokens), ('outputTokens', output),
                                ('cacheReadInputTokens', read), ('cacheCreationInputTokens', write)]:
            self.assertEqual(result['tokens'].get(field, 0), expected)

    def test_assistant_usage_only_deduplicates_calls(self):
        row = event('assistant.usage', model='test-model', apiCallId='one', **usage())
        count, result = self.parse([row, row, event('assistant.message', outputTokens=20)])
        self.assertEqual(count, 1)
        self.assert_tokens(result)
        self.assertIn('test-model', result['models'])

    def test_reported_model_call_shape_and_duplicate_id(self):
        row = event('model.model_call_success', callId='one', responseUsage={
            'prompt_tokens': 100, 'completion_tokens': 20,
            'prompt_tokens_details': {'cached_tokens': 30, 'cache_creation_tokens': 10},
            'completion_tokens_details': {'reasoning_tokens': 5}})
        count, result = self.parse([event('session.model_change', newModel='tracked'), row, row])
        self.assertEqual(count, 1)
        self.assert_tokens(result)
        self.assertIn('tracked', result['models'])

    def test_one_per_call_source_wins_within_a_segment(self):
        count, result = self.parse([
            event('model.model_call_success', responseUsage={'prompt_tokens': 900}),
            event('assistant.usage', model='chosen', **usage()),
            event('assistant.message', model='chosen', outputTokens=20), shutdown(chosen=usage())])
        self.assertEqual(count, 1)
        self.assert_tokens(result)
        self.assertEqual(set(result['models']), {'chosen'})

    def test_model_call_wins_over_shutdown_in_same_segment(self):
        count, result = self.parse([event('model.model_call_success', responseUsage={'completion_tokens': 7}),
                                    shutdown(other=usage())])
        self.assertEqual(count, 1)
        self.assertEqual(result['tokens']['all'], 7)

    def test_zero_per_call_usage_does_not_suppress_real_sources(self):
        zero = event('assistant.usage', inputTokens=0)
        for records, total in [([zero, event('model.model_call_success', responseUsage={'completion_tokens': 7})], 7),
                               ([zero, event('assistant.message', model='m', outputTokens=8)], 8),
                               ([zero, shutdown(m=usage())], 120)]:
            self.assertEqual(self.parse(records)[1]['tokens']['all'], total)

    def test_source_selection_changes_at_shutdown(self):
        call = event('assistant.usage', model='m', apiCallId='one', **usage(1000, 100, 0, 0))
        # First run 1100; cumulative second shutdown adds 4000 input + 200 output.
        records = [call, shutdown(m=usage(1000, 100, 0, 0)),
                   event('session.resume'), shutdown(m=usage(5000, 300, 0, 0))]
        self.assertEqual(self.parse(records)[1]['tokens']['all'], 5300)
        # A smaller independent run contributes 40 + 10 after the first 1100.
        records[-1] = shutdown(m=usage(40, 10, 0, 0))
        self.assertEqual(self.parse(records)[1]['tokens']['all'], 1150)
        self.assertEqual(self.parse([shutdown(m=usage(1000, 100, 0, 0)), call])[1]['tokens']['all'], 2200)

    def test_missing_shutdown_metrics_retain_counted_call_baseline(self):
        call = event('assistant.usage', model='m', apiCallId='one', **usage(1000, 100, 0, 0))
        records = [call, event('session.shutdown'), shutdown(m=usage(1500, 150, 0, 0))]
        self.assertEqual(self.parse(records)[1]['tokens']['all'], 1650)

    def test_empty_shutdown_preserves_segment_message_fallback(self):
        _, result = self.parse([shutdown(model=usage()), event('assistant.message', model='model', outputTokens=8),
                                event('session.shutdown'), event('assistant.message', outputTokens=7)])
        self.assert_tokens(result, 135, 60, 35, 30, 10)

    def test_unknown_model_and_calls_without_ids(self):
        count, result = self.parse([event('assistant.usage', **usage()), event('assistant.usage', **usage())])
        self.assertEqual(count, 2)
        self.assert_tokens(result, 240, 120, 40, 60, 20)
        self.assertIn('copilot-auto', result['models'])

    def test_shutdown_with_trailing_in_progress_output(self):
        count, result = self.parse([event('session.model_change', newModel='test-model'),
                                    event('assistant.message', outputTokens=20),
                                    shutdown(**{'test-model': usage()}), event('assistant.message', outputTokens=7)])
        self.assertEqual(count, 2)
        self.assert_tokens(result, 127, 60, 27, 30, 10)

    def test_cumulative_shutdowns(self):
        count, result = self.parse([shutdown(model=usage()), shutdown(model=usage(150, 35, 40, 15, 7))])
        self.assertEqual(count, 2)
        self.assert_tokens(result, 185, 95, 35, 40, 15)

    def test_per_run_shutdowns(self):
        count, result = self.parse([shutdown(model=usage()), shutdown(model=usage(50, 10, 10, 5, 2))])
        self.assertEqual(count, 2)
        self.assert_tokens(result, 180, 95, 30, 40, 15)

    def test_shutdown_delta_is_per_field_and_retains_absent_models(self):
        _, result = self.parse([shutdown(a=usage(1000, 500, 0, 0)), shutdown(a=usage(400, 800, 0, 0))])
        self.assert_tokens(result, 2200, 1400, 800, 0, 0)
        records = [shutdown(a=usage(1000, 100, 0, 0), b=usage(500, 50, 0, 0)),
                   shutdown(a=usage(1200, 120, 0, 0)),
                   shutdown(a=usage(1300, 130, 0, 0), b=usage(500, 50, 0, 0))]
        self.assertEqual(self.parse(records)[1]['tokens']['all'], 1980)
        self.assertEqual(app.reflect_parse_copilot_session(self.path)['tokens'], 1980)

    def test_legacy_output_only_is_unchanged(self):
        count, result = self.parse([event('assistant.message', outputTokens=99),
                                    event('session.model_change', newModel='old-model'),
                                    event('assistant.message', outputTokens=7)])
        self.assertEqual(count, 1)
        self.assertEqual(result['tokens']['all'], 7)

    def test_subset_bounds_and_separate_cache_classes(self):
        for values, expected in [(usage(100, 20, 80, 50), (120, 0, 20, 80, 20)),
                                 (usage(10, 20, 30, 5), (65, 10, 20, 30, 5))]:
            with self.subTest(values=values):
                _, result = self.parse([event('assistant.usage', **values)])
                self.assert_tokens(result, *expected)

    def test_usage_timestamp_and_mtime_fallback(self):
        row = event('assistant.usage', **usage())
        self.parse([row])
        with patch.object(app, 'add_usage_metrics', return_value=True) as add:
            app.parse_copilot_legacy_file(self.path, app.empty_provider_usage())
        self.assertEqual(add.call_args.args[3].isoformat(), '2026-01-02T03:04:05+00:00')
        row.pop('timestamp')
        self.parse([row])
        os.utime(self.path, (1700000000, 1700000000))
        with patch.object(app, 'add_usage_metrics', return_value=True) as add:
            app.parse_copilot_legacy_file(self.path, app.empty_provider_usage())
        self.assertEqual(add.call_args.args[3].timestamp(), 1700000000)

    def test_reflect_preserves_separate_cache_accounting_and_deduplicates(self):
        row = event('assistant.usage', apiCallId='same', model='test-model', **usage())
        self.parse([row, row, shutdown(model=usage(500))])
        self.assertEqual(app.reflect_parse_copilot_session(self.path)['tokens'], 160)
        self.parse([shutdown(model=usage()), shutdown(model=usage(150, 35, 40, 15, 7)),
                    event('assistant.message', model='model', outputTokens=7)])
        self.assertEqual(app.reflect_parse_copilot_session(self.path)['tokens'], 240)
        self.parse([shutdown(model=usage(1000, 30, 600, 0))])
        self.assertEqual(app.reflect_parse_copilot_session(self.path)['tokens'], 1630)
        self.parse([shutdown(model=usage(1000, 500, 0, 0)), shutdown(model=usage(400, 800, 0, 0))])
        self.assertEqual(app.reflect_parse_copilot_session(self.path)['tokens'], 2200)
