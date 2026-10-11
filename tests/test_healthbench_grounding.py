"""Synthetic grounding-arm and selection tests; no private records or model calls."""

from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import (ARMS, AVAILABLE_ARMS, DEFAULT_MODEL, GROUNDED_SUFFIX,
                                    RequestFailure, arm_config, build_payload, canonical_hash,
                                    generate, main, normalize_arms, run_experiment)


def example(prompt_id='synthetic-a'):
    return {'prompt_id': prompt_id, 'prompt': [
        {'role': 'system', 'content': 'Synthetic system instruction.'},
        {'role': 'user', 'content': 'Synthetic earlier request.'},
        {'role': 'assistant', 'content': 'Synthetic earlier reply.'},
        {'role': 'user', 'content': 'Create a factual template from the provided facts.'}],
        'rubrics': [{'criterion': 'SYNTHETIC_PRIVATE_RUBRIC', 'points': 1}],
        'reference_answer': 'SYNTHETIC_PRIVATE_REFERENCE'}


def response(answer='Synthetic candidate.', finish='stop'):
    return {'choices': [{'message': {'content': answer}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': 7, 'completion_tokens': 11, 'total_tokens': 18}}


class FakeTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []
        self.metadata_calls = 0

    def complete(self, payload):
        self.payloads.append(deepcopy(payload))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value

    def model_metadata(self, model):
        self.metadata_calls += 1
        return {'name': model, 'digest': 'synthetic-digest', 'metadata_status': 'available'}


class HealthBenchGroundingTests(unittest.TestCase):
    def test_grounding_instruction_is_last_user_only_and_excludes_private_fields(self):
        item = example()
        original = deepcopy(item)
        payload = build_payload(item, 'grounded_call')
        self.assertEqual(payload['messages'][:-1], original['prompt'][:-1])
        self.assertEqual(payload['messages'][-1]['role'], 'user')
        self.assertEqual(payload['messages'][-1]['content'],
                         original['prompt'][-1]['content'] + '\n\n[' + GROUNDED_SUFFIX + ']')
        self.assertEqual((payload['temperature'], payload['reasoning_effort'], payload['max_tokens']),
                         (0, 'low', 2048))
        self.assertNotIn('SYNTHETIC_PRIVATE_RUBRIC', json.dumps(payload))
        self.assertNotIn('SYNTHETIC_PRIVATE_REFERENCE', json.dumps(payload))
        payload['messages'][-1]['content'] = 'Synthetic mutation.'
        self.assertEqual(item, original)
        # These are general factual-record constraints, independent of a dataset's rubrics.
        for requirement in ('explicitly provided facts', 'negative symptoms', 'normal examination',
                            'measurements', 'medicines', 'treatment plans', '[not provided]',
                            'clinical trial', 'medication dose', 'only when it is necessary',
                            'concisely within the requested scope'):
            self.assertIn(requirement, GROUNDED_SUFFIX)

    def test_grounded_call_never_automatically_retries(self):
        for first, status in [(response(), 'completed'),
                              (response('Synthetic partial.', 'length'), 'truncated'),
                              (response('', 'stop'), 'failed'),
                              (response('', 'length'), 'failed'),
                              (RequestFailure('connection_error'), 'failed')]:
            with self.subTest(status=status, response_type=type(first).__name__):
                transport = FakeTransport([first, response('Must not be requested.')])
                item = example()
                record = generate(item, 'grounded_call', DEFAULT_MODEL, transport,
                                  dataset_sha256='synthetic-sha')
                self.assertEqual(len(transport.payloads), 1)
                self.assertEqual(len(record['attempts']), 1)
                self.assertFalse(record['fallback_used'])
                self.assertEqual(record['selected_attempt'], 1)
                self.assertEqual(record['status'], status)
                self.assertEqual(record['prompt_sha256'], canonical_hash(item['prompt']))
                self.assertIsNone(record['score'])
        with self.assertRaisesRegex(ValueError, 'fallback'):
            build_payload(example(), 'grounded_call', fallback=True)

    def test_defaults_and_selected_configuration_are_distinct(self):
        self.assertEqual(ARMS, ('raw_default', 'lunit_call'))
        self.assertEqual(AVAILABLE_ARMS, ('raw_default', 'lunit_call', 'grounded_call'))
        self.assertEqual(tuple(arm_config()), ARMS)
        chosen = arm_config('raw_default,grounded_call')
        self.assertEqual(tuple(chosen), ('raw_default', 'grounded_call'))
        self.assertEqual(chosen['grounded_call']['max_attempts'], 1)
        self.assertNotIn('fallback_condition', chosen['grounded_call'])
        self.assertEqual(arm_config(['lunit_call'])['lunit_call']['max_attempts'], 2)

    def test_arm_selection_accepts_ordered_api_and_cli_values(self):
        self.assertEqual(normalize_arms(' grounded_call , raw_default '),
                         ('grounded_call', 'raw_default'))
        self.assertEqual(normalize_arms(['grounded_call']), ('grounded_call',))
        self.assertEqual(normalize_arms(ARMS), ARMS)

    def test_bad_arm_selection_fails_before_reading_data_or_creating_output(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'missing.json', Path(folder) / 'output'
            for invalid in ('', ',', 'raw_default,', 'unknown',
                            'raw_default,raw_default', [], [False], {'raw_default'}):
                with self.subTest(selection_type=type(invalid).__name__):
                    with patch('benchmarks.healthbench.load_dataset',
                               side_effect=AssertionError('Dataset read is forbidden')):
                        with self.assertRaisesRegex(ValueError, '--arms'):
                            run_experiment(data, out, arms=invalid, dry_run=True)
                    self.assertFalse(out.exists())

    def test_selected_pair_records_only_requested_arms_and_uses_matching_counts(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'output'
            data.write_text(json.dumps([example('synthetic-b'), example('synthetic-a')]), encoding='utf-8')
            transport = FakeTransport([response() for _ in range(4)])
            selected_arms = ['raw_default', 'grounded_call']
            summary, code = run_experiment(data, out, arms=selected_arms, transport=transport)
            self.assertEqual(code, 0)
            self.assertEqual(summary['status'], 'completed')
            self.assertEqual(summary['selected_arms'], selected_arms)
            self.assertEqual((summary['planned_answers'], summary['written_answers'],
                              summary['unattempted_answers'], summary['calls']), (4, 4, 0, 4))
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(manifest['selected_arms'], selected_arms)
            self.assertEqual(manifest['config']['arms'], selected_arms)
            self.assertEqual(list(manifest['arms']), selected_arms)
            self.assertEqual(manifest['execution_order'], [
                {'prompt_id': 'synthetic-a', 'arms': selected_arms},
                {'prompt_id': 'synthetic-b', 'arms': list(reversed(selected_arms))}])
            records = [json.loads(line) for line in (out / 'answers.jsonl').read_text().splitlines()]
            self.assertEqual([(record['prompt_id'], record['arm']) for record in records],
                             [('synthetic-a', 'raw_default'), ('synthetic-a', 'grounded_call'),
                              ('synthetic-b', 'grounded_call'), ('synthetic-b', 'raw_default')])
            self.assertTrue(all(record['model_digest'] == 'synthetic-digest' for record in records))
            self.assertEqual(transport.metadata_calls, 1)

    def test_three_arm_dry_run_preserves_requested_order_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'output'
            data.write_text(json.dumps([example('synthetic-b'), example('synthetic-a')]), encoding='utf-8')
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')):
                summary, code = run_experiment(data, out, arms=AVAILABLE_ARMS, dry_run=True)
            self.assertEqual(code, 0)
            self.assertEqual(summary['planned_answers'], 6)
            self.assertEqual(summary['unattempted_answers'], 6)
            self.assertEqual(summary['calls'], 0)
            self.assertFalse((out / 'answers.jsonl').exists())
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(list(manifest['arms']), list(AVAILABLE_ARMS))
            self.assertEqual(manifest['execution_order'][1]['arms'], list(reversed(AVAILABLE_ARMS)))

    def test_three_arm_connection_blockage_does_not_claim_unattempted_results(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'output'
            data.write_text(json.dumps([example('synthetic-a'), example('synthetic-b')]), encoding='utf-8')
            transport = FakeTransport([RequestFailure('connection_error')])
            summary, code = run_experiment(data, out, arms=AVAILABLE_ARMS, transport=transport)
            self.assertEqual(code, 2)
            self.assertEqual(summary['status'], 'blocked')
            self.assertEqual((summary['planned_answers'], summary['written_answers'],
                              summary['unattempted_answers']), (6, 1, 5))
            self.assertIsNone(summary['score'])
            self.assertEqual(len(transport.payloads), 1)

    def test_cli_selected_arms_dry_run_reaches_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            data, out = root / 'data.json', root / 'results' / 'selected'
            data.write_text(json.dumps([example()]), encoding='utf-8')
            with patch('benchmarks.healthbench.ROOT', root), redirect_stdout(io.StringIO()):
                with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')):
                    code = main(['--data', str(data), '--out', str(out), '--dry-run',
                                 '--arms', 'raw_default,grounded_call'])
            self.assertEqual(code, 0)
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(list(manifest['arms']), ['raw_default', 'grounded_call'])


if __name__ == '__main__':
    unittest.main()
