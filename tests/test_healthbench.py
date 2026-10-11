"""Synthetic request/response tests; no model inference or private dataset access."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import (ARMS, DEFAULT_MODEL, SUFFIX, RequestFailure,
                                    build_payload, generate, load_dataset, run_experiment)


def example(prompt_id='id-a'):
    return {'prompt_id': prompt_id, 'prompt': [
        {'role': 'system', 'content': 'Synthetic instruction.'},
        {'role': 'user', 'content': 'Earlier synthetic question.'},
        {'role': 'assistant', 'content': 'Earlier synthetic response.'},
        {'role': 'user', 'content': 'Final synthetic question.'}],
        'rubrics': [{'criterion': 'RUBRIC_SECRET_SENTINEL', 'points': 1}],
        'reference_answer': 'REFERENCE_SECRET_SENTINEL'}


def response(answer='Synthetic answer.', finish='stop', prompt=10, completion=20):
    return {'choices': [{'message': {'content': answer}, 'finish_reason': finish}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                      'total_tokens': prompt + completion}}


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


class HealthBenchTests(unittest.TestCase):
    def test_payloads_preserve_messages_without_rubrics_or_references(self):
        item = example()
        original = deepcopy(item)
        raw = build_payload(item, 'raw_default')
        lunit = build_payload(item, 'lunit_call')
        self.assertEqual(raw['messages'], original['prompt'])
        self.assertIsNot(raw['messages'], item['prompt'])
        self.assertNotIn('temperature', raw)
        self.assertNotIn('reasoning_effort', raw)
        self.assertEqual(raw['max_tokens'], 2048)
        self.assertEqual(lunit['messages'][:-1], original['prompt'][:-1])
        self.assertEqual(lunit['messages'][-1]['content'], original['prompt'][-1]['content'] + '\n\n[' + SUFFIX + ']')
        self.assertEqual(lunit['temperature'], 0)
        self.assertEqual(lunit['reasoning_effort'], 'high')
        self.assertEqual(lunit['max_tokens'], 6144)
        for payload in (raw, lunit):
            encoded = json.dumps(payload)
            self.assertNotIn('RUBRIC_SECRET_SENTINEL', encoded)
            self.assertNotIn('REFERENCE_SECRET_SENTINEL', encoded)
            payload['messages'][0]['content'] = 'Mutation only in a copied payload.'
        self.assertEqual(item, original)

    def test_only_empty_or_length_lunit_answers_retry_with_low_effort(self):
        for first, expected_calls in [(response(), 1), (response('', 'stop'), 2),
                                      (response('Cut off.', 'length'), 2),
                                      (RequestFailure('http_error', 503), 1)]:
            with self.subTest(first=type(first).__name__, calls=expected_calls):
                transport = FakeTransport([first, response('Retry answer.', prompt=30, completion=40)])
                record = generate(example(), 'lunit_call', DEFAULT_MODEL, transport, dataset_sha256='synthetic-sha')
                self.assertEqual(len(transport.payloads), expected_calls)
                if expected_calls == 2:
                    self.assertEqual(transport.payloads[1]['reasoning_effort'], 'low')
                    high = dict(transport.payloads[0], reasoning_effort='low')
                    self.assertEqual(high, transport.payloads[1])
                    self.assertEqual(record['usage'], {'prompt_tokens': 40, 'completion_tokens': 60, 'total_tokens': 100})
                    self.assertEqual(record['answer'], 'Retry answer.')
                    self.assertTrue(record['fallback_used'])
        raw_transport = FakeTransport([response('', 'length')])
        raw = generate(example(), 'raw_default', DEFAULT_MODEL, raw_transport, dataset_sha256='synthetic-sha')
        self.assertEqual(raw['status'], 'failed')
        self.assertEqual(len(raw_transport.payloads), 1)

    def test_failed_retry_preserves_prior_candidate_and_failure(self):
        transport = FakeTransport([response('Partial original answer.', 'length'), RequestFailure('connection_error')])
        record = generate(example(), 'lunit_call', DEFAULT_MODEL, transport, dataset_sha256='synthetic-sha')
        self.assertEqual(record['status'], 'truncated')
        self.assertEqual(record['answer'], 'Partial original answer.')
        self.assertEqual(record['selected_attempt'], 1)
        self.assertEqual(record['attempts'][1]['status'], 'failed')
        self.assertEqual(record['attempts'][1]['error']['code'], 'connection_error')
        self.assertIsNone(record['usage']['total_tokens'])
        self.assertIsNone(record['score'])
        self.assertEqual(record['score_status'], 'not_scored')

    def test_duplicate_ids_rejected_and_selection_is_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data.json'
            data.write_text(json.dumps([example('c'), example('a'), example('b')]), encoding='utf-8')
            selected, metadata = load_dataset(data, limit=2)
            self.assertEqual([item['prompt_id'] for item in selected], ['a', 'b'])
            self.assertEqual(metadata['selected_prompt_ids'], ['a', 'b'])
            self.assertEqual(len(load_dataset(data, limit=0)[0]), 3)
            data.write_text(json.dumps([example(), example()]), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                load_dataset(data)

    def test_unexpected_finish_reasons_are_failures_even_with_content(self):
        for finish in (None, 'content_filter', 'unknown'):
            with self.subTest(finish=finish):
                transport = FakeTransport([response('A partial answer remains.', finish)])
                result = generate(example(), 'lunit_call', DEFAULT_MODEL, transport, dataset_sha256='synthetic-sha')
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['error']['code'], 'unexpected_finish_reason')
                self.assertEqual(result['answer'], 'A partial answer remains.')
                self.assertEqual(len(transport.payloads), 1)

    def test_sidecar_provenance_is_bound_to_data_and_excludes_source_records(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data.json'
            raw = json.dumps([example()]).encode('utf-8')
            data.write_bytes(raw)
            sidecar = data.with_suffix('.manifest.json')
            provenance = {'dataset_kind': 'legacy_consensus_val_1', 'normalized_context': True,
                          'lost_original_prompt_ids': True, 'canonical_score_comparable': False,
                          'count': 1, 'converted_sha256': hashlib.sha256(raw).hexdigest(),
                          'records': [example()]}
            sidecar.write_text(json.dumps(provenance), encoding='utf-8')
            _, metadata = load_dataset(data)
            saved = metadata['provenance']
            self.assertEqual(saved['status'], 'validated')
            self.assertEqual(saved['dataset_kind'], 'legacy_consensus_val_1')
            self.assertTrue(saved['normalized_context'])
            self.assertTrue(saved['lost_original_prompt_ids'])
            self.assertNotIn('records', saved)
            self.assertNotIn('RUBRIC_SECRET_SENTINEL', json.dumps(saved))
            provenance['converted_sha256'] = 'wrong-hash'
            sidecar.write_text(json.dumps(provenance), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                load_dataset(data)

    def test_dry_run_makes_manifest_without_network_or_answers(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'output'
            data.write_text(json.dumps([example('b'), example('a')]), encoding='utf-8')
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Network forbidden')):
                summary, code = run_experiment(data, out, dry_run=True)
            self.assertEqual(code, 0)
            self.assertEqual(summary['status'], 'dry_run')
            self.assertEqual(summary['calls'], 0)
            self.assertFalse((out / 'answers.jsonl').exists())
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(manifest['status'], 'dry_run')
            self.assertEqual(manifest['selected_prompt_ids'], ['a', 'b'])
            self.assertEqual(manifest['execution_order'][0]['arms'], list(ARMS))
            self.assertEqual(manifest['execution_order'][1]['arms'], list(reversed(ARMS)))
            self.assertEqual(manifest['arms']['raw_default']['temperature'], 'omitted; server default unknown')
            self.assertIsNone(manifest['score'])

    def test_connection_failure_is_blocked_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'output'
            data.write_text(json.dumps([example()]), encoding='utf-8')
            transport = FakeTransport([RequestFailure('connection_error')])
            summary, code = run_experiment(data, out, transport=transport)
            self.assertNotEqual(code, 0)
            self.assertEqual(summary['status'], 'blocked')
            self.assertEqual(summary['unattempted_answers'], 1)
            records = [json.loads(line) for line in (out / 'answers.jsonl').read_text().splitlines()]
            self.assertEqual(records[0]['status'], 'blocked')
            self.assertEqual(records[0]['answer'], '')
            self.assertIsNone(records[0]['score'])
            self.assertEqual(json.loads((out / 'manifest.json').read_text())['status'], 'blocked')

    def test_later_failure_is_preserved_and_other_arm_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'output'
            data.write_text(json.dumps([example('b'), example('a')]), encoding='utf-8')
            transport = FakeTransport([response('Raw a.'), RequestFailure('http_error', 500),
                                       response('Lunit b.'), response('Raw b.')])
            summary, code = run_experiment(data, out, transport=transport)
            self.assertEqual(code, 1)
            self.assertEqual(summary['status'], 'partial')
            self.assertEqual(summary['answer_status_counts'], {'completed': 3, 'failed': 1})
            records = [json.loads(line) for line in (out / 'answers.jsonl').read_text().splitlines()]
            self.assertEqual([(item['prompt_id'], item['arm']) for item in records],
                             [('a', 'raw_default'), ('a', 'lunit_call'), ('b', 'lunit_call'), ('b', 'raw_default')])
            self.assertEqual(records[1]['attempts'][0]['error']['http_status'], 500)
            self.assertTrue(all(item['score'] is None for item in records))


if __name__ == '__main__':
    unittest.main()
