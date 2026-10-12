"""Synthetic budget/provenance/failure tests; no model or private data access."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import DEFAULT_MODEL, RequestFailure, canonical_hash
from benchmarks.healthbench_refine import (COMPLETION_BUDGET, RefinementConfig,
                                          build_stage_payload, parse_review,
                                          refine_answer, run_refinement)


def example(prompt_id='synthetic-id'):
    return {'prompt_id': prompt_id, 'prompt': [
        {'role': 'system', 'content': 'Synthetic source instruction.'},
        {'role': 'user', 'content': 'A user supplied fact.'},
        {'role': 'assistant', 'content': 'An unconfirmed assistant claim.'},
        {'role': 'user', 'content': 'Answer this final synthetic question.'}],
        'rubrics': [{'criterion': 'RUBRIC_SECRET_SENTINEL', 'points': 5}],
        'reference_answer': 'REFERENCE_SECRET_SENTINEL',
        'canary': 'CANARY_SECRET_SENTINEL'}


def response(answer='Complete draft.', finish='stop', prompt=10, completion=20):
    return {'choices': [{'message': {'content': answer, 'reasoning': 'HIDDEN_REASONING_SENTINEL'},
                         'finish_reason': finish}],
            'usage': {'prompt_tokens': prompt, 'completion_tokens': completion,
                      'total_tokens': prompt + completion}}


NO_ISSUES = json.dumps({'needs_revision': False, 'issues': []})
ISSUES = json.dumps({'needs_revision': True, 'issues': [
    {'category': 'grounding', 'description': 'Remove an unconfirmed assistant claim.'}]})


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self, responses, *, clock=None, durations=None):
        self.responses = iter(responses)
        self.payloads = []
        self.timeout = 300
        self.timeouts = []
        self.clock = clock
        self.durations = iter(durations or [])
        self.metadata_calls = 0

    def complete(self, payload):
        self.payloads.append(deepcopy(payload))
        self.timeouts.append(self.timeout)
        if self.clock:
            self.clock.now += next(self.durations)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value

    def model_metadata(self, model):
        self.metadata_calls += 1
        return {'name': model, 'digest': 'synthetic-digest', 'metadata_status': 'available'}


def generate(transport, **kwargs):
    return refine_answer(example(), DEFAULT_MODEL, transport,
                         dataset_sha256='synthetic-dataset-sha', **kwargs)


class RefinementTests(unittest.TestCase):
    def test_no_issue_stops_after_review_and_preserves_draft(self):
        transport = FakeTransport([response(), response(NO_ISSUES), response('Must never be called.')])
        value = generate(transport)
        self.assertEqual(value['calls'], 2)
        self.assertEqual(value['answer'], 'Complete draft.')
        self.assertEqual(value['selected_attempt'], 1)
        self.assertEqual(value['review_status'], 'no_issues_reported')
        self.assertEqual(value['usage'], {'prompt_tokens': 20, 'completion_tokens': 40, 'total_tokens': 60})
        self.assertIsNone(value['score'])

    def test_exactly_one_revision_with_all_attempt_costs_and_hashes(self):
        transport = FakeTransport([response(), response(ISSUES), response('Revised answer.'), response(ISSUES)])
        value = generate(transport)
        self.assertEqual(value['calls'], 3)
        self.assertEqual(value['answer'], 'Revised answer.')
        self.assertEqual(value['selected_attempt'], 3)
        self.assertEqual(value['attempts'][0]['answer'], 'Complete draft.')
        self.assertEqual(value['draft_answer_sha256'], canonical_hash('Complete draft.'))
        self.assertEqual(value['budget']['requested_completion_tokens'], COMPLETION_BUDGET)
        self.assertEqual([body['max_tokens'] for body in transport.payloads], [1536, 512, 2048])
        self.assertEqual(value['usage'], {'prompt_tokens': 30, 'completion_tokens': 60, 'total_tokens': 90})
        for attempt, payload in zip(value['attempts'], transport.payloads):
            self.assertEqual(attempt['request_sha256'], canonical_hash(payload))
        self.assertIsNone(value['score'])

    def test_single_call_is_identical_draft_control(self):
        one = FakeTransport([response()])
        loop = FakeTransport([response(), response(NO_ISSUES)])
        single = generate(one, variant='single_call')
        refined = generate(loop)
        self.assertEqual(one.payloads[0], loop.payloads[0])
        self.assertEqual(single['answer'], refined['attempts'][0]['answer'])
        self.assertEqual(single['calls'], 1)
        self.assertEqual(single['budget']['maximum_calls'], 1)
        self.assertEqual(single['budget']['completion_token_limit'], 1536)

    def test_all_stages_exclude_rubrics_canaries_and_reference_and_keep_history(self):
        item = example()
        original = deepcopy(item)
        transport = FakeTransport([response(), response(ISSUES), response('Revision.')])
        value = refine_answer(item, DEFAULT_MODEL, transport, dataset_sha256='sha')
        self.assertEqual(item, original)
        self.assertEqual(transport.payloads[0]['messages'][1:], original['prompt'])
        for payload in transport.payloads[1:]:
            messages = payload['messages']
            self.assertEqual([message['role'] for message in messages], ['system', 'user'])
            quoted = messages[-1]['content'].split('\n', 1)[1].rsplit('\n', 1)[0]
            source = json.loads(quoted)
            self.assertEqual(source['original_messages'], original['prompt'])
            self.assertEqual(source['draft_answer'], 'Complete draft.')
            self.assertNotIn('Complete draft.', messages[0]['content'])
        encoded = json.dumps(transport.payloads) + json.dumps(value)
        for secret in ('RUBRIC_SECRET_SENTINEL', 'CANARY_SECRET_SENTINEL',
                       'REFERENCE_SECRET_SENTINEL', 'HIDDEN_REASONING_SENTINEL'):
            self.assertNotIn(secret, encoded)

    def test_incomplete_draft_never_triggers_review(self):
        for draft in (response('Partial.', 'length'), response(''),
                      response('Content.', 'content_filter'), RequestFailure('connection_error')):
            with self.subTest(draft=repr(draft)):
                transport = FakeTransport([draft, response(ISSUES)])
                value = generate(transport)
                self.assertEqual(value['calls'], 1)
                self.assertNotEqual(value['status'], 'completed')
                self.assertTrue(value['review_status'].startswith('not_attempted_draft_'))

    def test_unusable_critique_retains_complete_draft_without_revision(self):
        for critique in (response('not JSON'), response(''), response(ISSUES, 'length'),
                         response('{"needs_revision":"false","issues":[]}'),
                         RequestFailure('http_error', 503)):
            with self.subTest(critique=repr(critique)):
                transport = FakeTransport([response(), critique, response('Unwanted revision.')])
                value = generate(transport)
                self.assertEqual(value['calls'], 2)
                self.assertEqual(value['answer'], 'Complete draft.')
                self.assertEqual(value['status'], 'completed')
                self.assertIsNone(value['review'])

    def test_incomplete_revision_cannot_replace_complete_draft(self):
        for revision in (response('Partial revision.', 'length'), response(''),
                         response('Bad status.', 'content_filter'), RequestFailure('connection_error')):
            with self.subTest(revision=repr(revision)):
                transport = FakeTransport([response(), response(ISSUES), revision])
                value = generate(transport)
                self.assertEqual(value['calls'], 3)
                self.assertEqual(value['answer'], 'Complete draft.')
                self.assertEqual(value['status'], 'completed')
                self.assertEqual(value['stop_reason'], 'unusable_revision_keep_draft')
                self.assertEqual(value['selected_attempt'], 1)

    def test_remaining_deadline_bounds_request_timeout_and_late_revision_is_rejected(self):
        clock = FakeClock()
        transport = FakeTransport([response(), response(ISSUES), response('Late complete revision.')],
                                  clock=clock, durations=[2, 3, 6])
        value = generate(transport, clock=clock, config=RefinementConfig(deadline_seconds=10,
                                                                       request_timeout_seconds=8))
        self.assertEqual(transport.timeouts, [8, 8, 5])
        self.assertEqual(transport.timeout, 300)
        self.assertEqual(value['attempts'][2]['status'], 'timed_out')
        self.assertEqual(value['answer'], 'Complete draft.')
        self.assertEqual(value['seconds'], 11)

    def test_late_critique_never_starts_revision(self):
        clock = FakeClock()
        transport = FakeTransport([response(), response(ISSUES), response('Unwanted revision.')],
                                  clock=clock, durations=[2, 9])
        value = generate(transport, clock=clock, config=RefinementConfig(deadline_seconds=10))
        self.assertEqual(value['calls'], 2)
        self.assertEqual(value['review_status'], 'critique_timed_out')
        self.assertEqual(value['answer'], 'Complete draft.')

    def test_unknown_usage_does_not_fabricate_cost_or_verified_budget(self):
        critique = response(NO_ISSUES)
        del critique['usage']
        value = generate(FakeTransport([response(), critique]))
        self.assertIsNone(value['usage']['total_tokens'])
        self.assertIsNone(value['budget']['reported_completion_tokens'])
        self.assertFalse(value['budget']['reported_completion_budget_verified'])

    def test_provider_ignoring_completion_limit_stops_and_is_visible(self):
        value = generate(FakeTransport([response(completion=1537), response(NO_ISSUES)]))
        self.assertEqual(value['calls'], 1)
        self.assertEqual(value['error']['code'], 'provider_exceeded_completion_limit')
        self.assertFalse(value['budget']['reported_completion_budget_verified'])

    def test_review_schema_rejects_duplicates_extras_and_unbounded_issues(self):
        invalid = [
            '{"needs_revision":true,"needs_revision":false,"issues":[]}',
            json.dumps({'needs_revision': False, 'issues': [], 'score': 99}),
            json.dumps({'needs_revision': False, 'issues': [{'category': 'accuracy', 'description': 'Issue'}]}),
            json.dumps({'needs_revision': True, 'issues': [{'category': 'accuracy', 'description': 'x' * 401}]}),
            json.dumps({'needs_revision': True, 'issues': [{'category': 'accuracy', 'description': 'Issue'}] * 9}),
            '```json\n' + NO_ISSUES + '\n```']
        for value in invalid:
            with self.subTest(value=value[:80]):
                with self.assertRaises(ValueError):
                    parse_review(value)
        self.assertEqual(parse_review(NO_ISSUES)['needs_revision'], False)

    def test_prepare_uses_only_first_twenty_sorted_and_no_network(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'prepared'
            data.write_text(json.dumps([example(f'id-{index:03}') for index in reversed(range(200))]))
            with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
                summary, code = run_refinement(data, out, prepare_only=True)
            self.assertEqual(code, 0)
            self.assertEqual(summary['calls'], 0)
            self.assertEqual(summary['selected_prompt_ids'], [f'id-{index:03}' for index in range(20)])
            self.assertFalse((out / 'answers.jsonl').exists())
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(manifest['status'], 'prepared')
            self.assertEqual(manifest['maximum_calls_per_item'], 3)
            for prepared in manifest['prepared_requests']:
                self.assertEqual(prepared['single_call_draft_request_sha256'],
                                 prepared['bounded_refine_draft_request_sha256'])
            for secret in ('RUBRIC_SECRET_SENTINEL', 'CANARY_SECRET_SENTINEL', 'REFERENCE_SECRET_SENTINEL'):
                self.assertNotIn(secret, json.dumps(manifest))

    def test_running_or_missing_baseline_blocks_before_any_network_or_output(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data.json'
            data.write_text(json.dumps([example()]))
            baseline = Path(folder) / 'baseline.json'
            baseline.write_text('{"status":"running"}')
            for supplied in (None, baseline):
                transport = FakeTransport([response()])
                out = Path(folder) / ('missing' if supplied is None else 'running')
                with self.assertRaisesRegex(ValueError, 'requires'):
                    run_refinement(data, out, baseline_manifest=supplied, transport=transport)
                self.assertEqual(transport.metadata_calls, 0)
                self.assertEqual(transport.payloads, [])
                self.assertFalse(out.exists())

    def test_completed_baseline_allows_control_and_persists_all_costs(self):
        with tempfile.TemporaryDirectory() as folder:
            data, out = Path(folder) / 'data.json', Path(folder) / 'control'
            data.write_text(json.dumps([example()]))
            baseline = Path(folder) / 'baseline.json'
            baseline.write_text(json.dumps({'status': 'completed',
                                            'dataset': {'sha256': hashlib.sha256(data.read_bytes()).hexdigest()},
                                            'selected_prompt_ids': ['synthetic-id'],
                                            'model': {'name': DEFAULT_MODEL, 'digest': 'synthetic-digest'}}))
            transport = FakeTransport([response()])
            summary, code = run_refinement(data, out, baseline_manifest=baseline,
                                           transport=transport, variant='single_call')
            self.assertEqual(code, 0)
            self.assertEqual(summary['calls'], 1)
            self.assertEqual(summary['usage']['total_tokens'], 30)
            record = json.loads((out / 'answers.jsonl').read_text())
            self.assertEqual(record['arm'], 'single_call')
            self.assertEqual(record['model_digest'], 'synthetic-digest')
            manifest = json.loads((out / 'manifest.json').read_text())
            self.assertEqual(manifest['maximum_calls_per_item'], 1)
            self.assertEqual(manifest['baseline']['status'], 'completed')
            with self.assertRaisesRegex(ValueError, 'already contains'):
                run_refinement(data, out, baseline_manifest=baseline, prepare_only=True)

    def test_baseline_dataset_development_ids_and_model_provenance_are_required(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / 'data.json'
            data.write_text(json.dumps([example(f'id-{index:03}') for index in range(200)]))
            baseline = Path(folder) / 'baseline.json'
            good = {'status': 'completed',
                    'dataset': {'sha256': hashlib.sha256(data.read_bytes()).hexdigest()},
                    'selected_prompt_ids': [f'id-{index:03}' for index in range(200)],
                    'model': {'name': DEFAULT_MODEL, 'digest': 'synthetic-digest'}}
            variants = [good | {'dataset': {'sha256': 'wrong'}},
                        good | {'selected_prompt_ids': list(reversed(good['selected_prompt_ids']))},
                        good | {'model': {'name': 'other-model', 'digest': 'synthetic-digest'}},
                        good | {'config': {'model': 'other-model'}},
                        good | {'model': {'name': DEFAULT_MODEL, 'digest': 'other-digest'}}]
            for index, value in enumerate(variants):
                with self.subTest(index=index):
                    baseline.write_text(json.dumps(value))
                    out = Path(folder) / f'bad-{index}'
                    transport = FakeTransport([])
                    with self.assertRaises(ValueError):
                        run_refinement(data, out, baseline_manifest=baseline, transport=transport)
                    self.assertEqual(transport.payloads, [])
                    self.assertFalse(out.exists())
            # Preparation may reference a running baseline but must retain its identity.
            baseline.write_text(json.dumps(good | {'status': 'running'}))
            summary, code = run_refinement(data, Path(folder) / 'prepared',
                                           baseline_manifest=baseline, prepare_only=True)
            self.assertEqual(code, 0)
            self.assertEqual(len(summary['selected_prompt_ids']), 20)
            baseline.write_text(json.dumps(good | {'status': 'running', 'dataset': {'sha256': 'wrong'}}))
            with self.assertRaisesRegex(ValueError, 'dataset SHA'):
                run_refinement(data, Path(folder) / 'wrong-prepared',
                               baseline_manifest=baseline, prepare_only=True)

    def test_invalid_effort_temperature_or_deadline_is_rejected(self):
        for kwargs in ({'draft_effort': 'high'}, {'temperature': 1}, {'temperature': False},
                       {'deadline_seconds': 0}, {'request_timeout_seconds': float('inf')}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    RefinementConfig(**kwargs)


if __name__ == '__main__':
    unittest.main()
