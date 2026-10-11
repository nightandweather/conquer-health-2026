"""Synthetic generation-boundary/provenance tests with a temporary Git repo."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import DEFAULT_MODEL, RequestFailure, canonical_hash
from benchmarks.healthbench_codex_cycle import REVIEW_REQUIRED, make_payload, run_cycle


def response(answer='Synthetic candidate answer.', finish='stop'):
    return {'choices': [{'message': {'content': answer, 'reasoning': 'HIDDEN_REASONING_SENTINEL'},
                         'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


class FakeTransport:
    def __init__(self, responses, *, digest='synthetic-digest', on_complete=None):
        self.responses = iter(responses)
        self.payloads = []
        self.digest = digest
        self.metadata_calls = 0
        self.on_complete = on_complete

    def complete(self, payload):
        self.payloads.append(deepcopy(payload))
        if self.on_complete:
            self.on_complete()
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value

    def model_metadata(self, model):
        self.metadata_calls += 1
        return {'name': model, 'digest': self.digest, 'metadata_status': 'available'}


class CodexCycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch('benchmarks.healthbench_codex_cycle.ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        self.instructions = self.root / 'benchmarks' / 'prompts' / 'codex_guided_v3.txt'
        self.instructions.parent.mkdir(parents=True)
        self.instructions.write_text('Use confirmed user facts and readable Markdown.\n', encoding='utf-8')
        subprocess.run(['git', '-C', str(self.root), 'add', '--', str(self.instructions)],
                       check=True, capture_output=True)
        self.data = self.root / 'data.json'
        self.items = [{'prompt_id': f'id-{index:03}', 'prompt': [
            {'role': 'system', 'content': 'Synthetic source instruction'},
            {'role': 'user', 'content': f'Confirmed synthetic fact {index}'},
            {'role': 'assistant', 'content': 'Unconfirmed earlier assistant claim'},
            {'role': 'user', 'content': 'Final synthetic request'}],
            'rubrics': [{'criterion': 'RUBRIC_SECRET_SENTINEL', 'points': 5}],
            'reference_answer': 'REFERENCE_SECRET_SENTINEL', 'canary': 'CANARY_SECRET_SENTINEL',
            'reviewer_feedback': 'CASE_CRITICISM_SECRET_SENTINEL'} for index in range(200)]
        self.data.write_text(json.dumps(list(reversed(self.items))))
        self.baseline = self.root / 'baseline.json'
        self.baseline_value = {'status': 'completed',
                               'dataset': {'sha256': hashlib.sha256(self.data.read_bytes()).hexdigest()},
                               'selected_prompt_ids': [item['prompt_id'] for item in self.items],
                               'model': {'name': DEFAULT_MODEL, 'digest': 'synthetic-digest'}}
        self.write_baseline()
        self.out = self.root / 'results' / 'candidate'

    def write_baseline(self):
        self.baseline.write_text(json.dumps(self.baseline_value))

    def run_cycle(self, **kwargs):
        return run_cycle(self.data, self.baseline, self.instructions, self.out, **kwargs)

    def test_payload_preserves_original_roles_text_and_excludes_evaluation_fields(self):
        item = deepcopy(self.items[0])
        original = deepcopy(item)
        text = self.instructions.read_text()
        payload = make_payload(item, text)
        self.assertEqual(payload['messages'][:-1], original['prompt'][:-1])
        self.assertEqual(payload['messages'][-1]['role'], original['prompt'][-1]['role'])
        self.assertEqual(payload['messages'][-1]['content'], original['prompt'][-1]['content'] + '\n\n[' + text + ']')
        self.assertEqual(payload['temperature'], 0)
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['max_tokens'], 2048)
        self.assertEqual(item, original)
        encoded = json.dumps(payload)
        for secret in ('RUBRIC_SECRET_SENTINEL', 'REFERENCE_SECRET_SENTINEL',
                       'CANARY_SECRET_SENTINEL', 'CASE_CRITICISM_SECRET_SENTINEL'):
            self.assertNotIn(secret, encoded)

    def test_prepare_running_baseline_has_no_network_or_answers_and_records_hashes(self):
        self.baseline_value['status'] = 'running'
        self.write_baseline()
        transport = FakeTransport([])
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
            summary, code = self.run_cycle(prepare_only=True, transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(summary['status'], 'prepared')
        self.assertEqual(summary['calls'], 0)
        self.assertEqual(summary['planned_answers'], 20)
        self.assertEqual(transport.metadata_calls, 0)
        self.assertEqual(transport.payloads, [])
        self.assertFalse((self.out / 'answers.jsonl').exists())
        manifest = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(manifest['instructions']['byte_sha256'], hashlib.sha256(self.instructions.read_bytes()).hexdigest())
        self.assertTrue(manifest['instructions']['git_tracked'])
        self.assertEqual(manifest['instructions']['name'], 'codex_guided_v3')
        self.assertEqual(manifest['instructions']['version'], 'codex_guided_v3')
        self.assertEqual(manifest['selected_prompt_ids'], [item['prompt_id'] for item in self.items[:20]])
        for prepared, item in zip(manifest['prepared_requests'], self.items[:20]):
            payload = make_payload(item, self.instructions.read_text())
            self.assertEqual(prepared['prompt_sha256'], canonical_hash(item['prompt']))
            self.assertEqual(prepared['input_sha256'], canonical_hash(payload['messages']))
            self.assertEqual(prepared['request_sha256'], canonical_hash(payload))
        self.assertIsNone(manifest['score'])
        self.assertEqual(manifest['score_status'], REVIEW_REQUIRED)

    def test_live_is_exactly_twenty_single_calls_and_never_selects_heldout(self):
        transport = FakeTransport([response()] * 20)
        summary, code = self.run_cycle(transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(summary['calls'], 20)
        self.assertEqual(len(transport.payloads), 20)
        self.assertEqual(summary['usage'], {'prompt_tokens': 200, 'completion_tokens': 400, 'total_tokens': 600})
        records = [json.loads(line) for line in (self.out / 'answers.jsonl').read_text().splitlines()]
        self.assertEqual([record['prompt_id'] for record in records], [item['prompt_id'] for item in self.items[:20]])
        for record, payload in zip(records, transport.payloads):
            self.assertEqual(record['arm'], 'codex_guided_v3')
            self.assertEqual(len(record['attempts']), 1)
            self.assertEqual(record['request_sha256'], canonical_hash(payload))
            self.assertEqual(record['input_sha256'], canonical_hash(payload['messages']))
            self.assertEqual(record['model_digest'], 'synthetic-digest')
            self.assertEqual(record['score_status'], REVIEW_REQUIRED)
            self.assertIsNone(record['score'])
        encoded = json.dumps(records) + json.dumps(transport.payloads)
        self.assertNotIn('HIDDEN_REASONING_SENTINEL', encoded)
        self.assertNotIn('CASE_CRITICISM_SECRET_SENTINEL', encoded)

    def test_running_baseline_rejects_live_before_metadata_or_output(self):
        self.baseline_value['status'] = 'running'
        self.write_baseline()
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, 'completed baseline'):
            self.run_cycle(transport=transport)
        self.assertEqual(transport.metadata_calls, 0)
        self.assertEqual(transport.payloads, [])
        self.assertFalse(self.out.exists())

    def test_dataset_dev_id_or_model_mismatch_rejects_prepare_and_live(self):
        good = deepcopy(self.baseline_value)
        for mutation in ({'dataset': {'sha256': 'wrong'}},
                         {'selected_prompt_ids': good['selected_prompt_ids'][20:]},
                         {'model': {'name': 'other-model', 'digest': 'synthetic-digest'}}):
            for prepared in (True, False):
                with self.subTest(mutation=mutation, prepared=prepared):
                    self.baseline_value = good | mutation
                    self.write_baseline()
                    transport = FakeTransport([])
                    with self.assertRaises(ValueError):
                        self.run_cycle(prepare_only=prepared, transport=transport)
                    self.assertEqual(transport.metadata_calls, 0)
                    self.assertFalse(self.out.exists())

    def test_different_or_unknown_current_model_digest_prevents_inference(self):
        for digest in ('different-digest', None):
            with self.subTest(digest=digest):
                transport = FakeTransport([], digest=digest)
                with self.assertRaisesRegex(ValueError, 'digest'):
                    self.run_cycle(transport=transport)
                self.assertEqual(transport.metadata_calls, 1)
                self.assertEqual(transport.payloads, [])
                self.assertFalse(self.out.exists())

    def test_untracked_or_external_instructions_are_rejected(self):
        untracked = self.root / 'untracked.txt'
        untracked.write_text('Generic instructions.')
        with self.assertRaisesRegex(ValueError, 'Git tracked'):
            run_cycle(self.data, self.baseline, untracked, self.out, prepare_only=True)
        with tempfile.TemporaryDirectory() as external:
            outside = Path(external).resolve() / 'outside.txt'
            outside.write_text('Generic instructions.')
            with self.assertRaisesRegex(ValueError, 'current repository'):
                run_cycle(self.data, self.baseline, outside, self.out, prepare_only=True)
        self.assertFalse(self.out.exists())

    def test_existing_prepared_run_is_never_overwritten_or_resumed(self):
        self.run_cycle(prepare_only=True)
        before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.out.iterdir()}
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, 'cannot be resumed or overwritten'):
            self.run_cycle(transport=transport)
        self.assertEqual(transport.metadata_calls, 0)
        self.assertEqual(before, {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.out.iterdir()})

    def test_transport_failure_is_one_call_blocked_with_unattempted_cases(self):
        transport = FakeTransport([RequestFailure('connection_error')])
        summary, code = self.run_cycle(transport=transport)
        self.assertEqual(code, 2)
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['calls'], 1)
        self.assertEqual(summary['unattempted_answers'], 19)
        record = json.loads((self.out / 'answers.jsonl').read_text())
        self.assertEqual(record['status'], 'blocked')
        self.assertIsNone(record['usage']['total_tokens'])

    def test_truncation_empty_and_failed_statuses_never_retry(self):
        transport = FakeTransport([response('Partial answer.', 'length'), response(''),
                                   response('Unexpected finish.', 'content_filter')] + [response()] * 17)
        summary, code = self.run_cycle(transport=transport)
        self.assertEqual(code, 1)
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['calls'], 20)
        records = [json.loads(line) for line in (self.out / 'answers.jsonl').read_text().splitlines()]
        self.assertEqual([record['status'] for record in records[:3]], ['truncated', 'failed', 'failed'])
        self.assertTrue(all(len(record['attempts']) == 1 for record in records))

    def test_changed_instructions_stop_before_second_call(self):
        transport = FakeTransport([response()], on_complete=lambda: self.instructions.write_text('Changed generic prompt.'))
        summary, code = self.run_cycle(transport=transport)
        self.assertEqual(code, 2)
        self.assertEqual(summary['status'], 'blocked')
        self.assertEqual(summary['calls'], 1)
        self.assertEqual(summary['written_answers'], 1)

    def test_reserved_arm_name_or_missing_baseline_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'distinguish'):
            self.run_cycle(name='grounded_call', prepare_only=True)
        with self.assertRaisesRegex(ValueError, 'baseline-manifest'):
            run_cycle(self.data, None, self.instructions, self.out, prepare_only=True)
        self.assertFalse(self.out.exists())


if __name__ == '__main__':
    unittest.main()
