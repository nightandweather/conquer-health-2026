"""Synthetic generation-boundary/provenance tests with a temporary Git repo."""

from copy import deepcopy
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, RequestFailure, arm_config,
                                    build_payload, canonical_hash, generate, load_dataset)
from benchmarks import healthbench_codex_cycle as cycle
from benchmarks.healthbench_codex_cycle import REVIEW_REQUIRED, make_payload, run_cycle


def response(answer='Synthetic candidate answer.', finish='stop'):
    return {'choices': [{'message': {'content': answer, 'reasoning': 'HIDDEN_REASONING_SENTINEL'},
                         'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


class BudgetTokenPayloadTests(unittest.TestCase):
    """Budget-helper coverage without the cycle fixture's temporary Git calls."""

    def item(self):
        return {'prompt_id': 'synthetic-budget', 'prompt': [{'role': 'user', 'content': 'Synthetic question'}],
                'rubrics': [{'criterion': 'RUBRIC_SECRET_SENTINEL', 'points': 1}],
                'reference': 'REFERENCE_SECRET_SENTINEL', 'canary': 'CANARY_SECRET_SENTINEL'}

    def test_default_and_explicit_2048_preserve_legacy_bytes(self):
        item, instructions = self.item(), 'General frozen instruction'
        legacy = build_payload(item, 'grounded_call')
        legacy['messages'][-1]['content'] = item['prompt'][-1]['content'] + '\n\n[' + instructions + ']'
        for payload in (make_payload(item, instructions), make_payload(item, instructions, max_tokens=2048)):
            self.assertEqual(json.dumps(payload, ensure_ascii=False).encode(), json.dumps(legacy, ensure_ascii=False).encode())
            self.assertEqual(canonical_hash(payload), canonical_hash(legacy))

    def test_4096_changes_only_budget_and_reaches_the_real_attempt(self):
        item, instructions = self.item(), 'General frozen instruction'
        control = make_payload(item, instructions, reasoning_effort='medium')
        expected = control | {'max_tokens': 4096}
        transport = FakeTransport([response()])
        row = cycle.generate_candidate(item, instructions, 'budget', DEFAULT_MODEL, transport,
                                       dataset_sha256='synthetic', reasoning_effort='medium', max_tokens=4096)
        self.assertEqual(transport.payloads, [expected])
        self.assertEqual(row['attempts'][0]['max_tokens'], 4096)
        self.assertEqual(row['request_sha256'], canonical_hash(expected))
        self.assertEqual(row['input_sha256'], canonical_hash(control['messages']))
        self.assertEqual(row['selected_attempt'], 1)
        self.assertEqual(len(row['attempts']), 1)
        self.assertFalse(row['fallback_used'])

    def test_invalid_budget_is_rejected_before_transport(self):
        for invalid in (None, True, False, 0, -1, 1024, 6144, 2048.0, 4096.0, '4096', [], {}):
            with self.subTest(value=invalid):
                transport = FakeTransport([])
                with self.assertRaisesRegex(ValueError, 'max_tokens'):
                    make_payload(self.item(), 'General', max_tokens=invalid)
                with self.assertRaisesRegex(ValueError, 'max_tokens'):
                    cycle.generate_candidate(self.item(), 'General', 'budget', DEFAULT_MODEL, transport,
                                             dataset_sha256='synthetic', max_tokens=invalid)
                self.assertEqual(transport.payloads, [])
                self.assertEqual(transport.metadata_calls, 0)

    def test_explicit_user_suffix_preserves_exact_2048_and_4096_requests_and_records(self):
        item, instructions = self.item(), 'General\n[frozen instruction]\n'
        for tokens in (2048, 4096):
            with self.subTest(tokens=tokens):
                implicit = make_payload(item, instructions, reasoning_effort='medium', max_tokens=tokens)
                explicit = make_payload(item, instructions, reasoning_effort='medium', max_tokens=tokens,
                                        instruction_placement='user_suffix')
                self.assertEqual(json.dumps(implicit, ensure_ascii=False).encode(),
                                 json.dumps(explicit, ensure_ascii=False).encode())
                transport = FakeTransport([response()])
                row = cycle.generate_candidate(item, instructions, 'legacy', DEFAULT_MODEL, transport,
                    dataset_sha256='synthetic', reasoning_effort='medium', max_tokens=tokens)
                self.assertEqual(transport.payloads, [implicit])
                self.assertNotIn('instruction_placement', row)
                self.assertNotIn('instruction_placement', row['attempts'][0])

    def test_system_prefix_exact_block_raw_messages_and_actual_attempt(self):
        item, instructions = self.item(), GROUNDED_SUFFIX
        item['prompt'] = [{'role': 'user', 'content': '  raw fact\n'},
                          {'role': 'assistant', 'content': 'Unconfirmed prior claim'},
                          {'role': 'user', 'content': 'Question\n\n[' + instructions + ']'}]
        original = deepcopy(item)
        control = make_payload(item, instructions, reasoning_effort='medium', max_tokens=2048)
        treatment = make_payload(item, instructions, reasoning_effort='medium', max_tokens=2048,
                                 instruction_placement='system_prefix')
        self.assertEqual(treatment['messages'],
                         [{'role': 'system', 'content': '\n\n[' + instructions + ']'}, *original['prompt']])
        self.assertEqual({key for key in control if control[key] != treatment[key]}, {'messages'})
        self.assertEqual(item, original)
        transport = FakeTransport([response()])
        row = cycle.generate_candidate(item, instructions, 'grounded_medium_system2048_v1', DEFAULT_MODEL,
            transport, dataset_sha256='synthetic', reasoning_effort='medium', max_tokens=2048,
            instruction_placement='system_prefix')
        self.assertEqual(transport.payloads, [treatment])
        self.assertEqual(row['instruction_placement'], 'system_prefix')
        self.assertEqual(row['attempts'][0]['instruction_placement'], 'system_prefix')
        self.assertEqual(row['attempts'][0]['max_tokens'], 2048)
        self.assertEqual(row['request_sha256'], canonical_hash(treatment))
        self.assertEqual(row['input_sha256'], canonical_hash(treatment['messages']))

    def test_invalid_placement_system_conflict_or_arm_rejects_before_transport(self):
        for value in (None, True, '', 'system', 'SYSTEM_PREFIX', [], {}):
            with self.subTest(value=value):
                transport = FakeTransport([])
                with self.assertRaises(ValueError):
                    make_payload(self.item(), 'General', instruction_placement=value)
                with self.assertRaises(ValueError):
                    cycle.generate_candidate(self.item(), 'General', 'legacy', DEFAULT_MODEL, transport,
                        dataset_sha256='synthetic', instruction_placement=value)
                self.assertEqual(transport.payloads, [])
        item = self.item()
        item['prompt'].insert(0, {'role': 'system', 'content': 'Original system'})
        with self.assertRaisesRegex(ValueError, 'preexisting system'):
            make_payload(item, 'General', instruction_placement='system_prefix')
        for name, effort, tokens, placement in (('legacy', 'medium', 2048, 'system_prefix'),
                ('grounded_medium_system2048_v1', 'low', 2048, 'system_prefix'),
                ('grounded_medium_system2048_v1', 'medium', 4096, 'system_prefix'),
                ('grounded_medium_system2048_v1', 'medium', 2048, 'user_suffix')):
            transport = FakeTransport([])
            with self.assertRaises(ValueError):
                cycle.generate_candidate(self.item(), 'General', name, DEFAULT_MODEL, transport,
                    dataset_sha256='synthetic', reasoning_effort=effort, max_tokens=tokens,
                    instruction_placement=placement)
            self.assertEqual(transport.payloads, [])
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, 'frozen instructions'):
            cycle.generate_candidate(self.item(), GROUNDED_SUFFIX + '\n', 'grounded_medium_system2048_v1',
                DEFAULT_MODEL, transport, dataset_sha256='synthetic', reasoning_effort='medium', max_tokens=2048,
                instruction_placement='system_prefix')
        self.assertEqual(transport.payloads, [])


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

    def finalize_partial_control(self):
        _, dataset = load_dataset(self.data, limit=0)
        self.baseline_value.update(status='partial', finished_at='2026-10-11T06:12:48+00:00',
                                   dataset=dataset, selected_arms=['raw_default', 'grounded_call'],
                                   config={'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL},
                                   arms=arm_config(('raw_default', 'grounded_call')))
        self.write_baseline()
        self.control_rows = []
        for index, item in enumerate(self.items):
            for arm in ('raw_default', 'grounded_call'):
                transport = FakeTransport([RequestFailure('connection_error')] if index == 199 and arm == 'raw_default'
                                          else [response(f'Synthetic source {index} {arm}')])
                row = generate(item, arm, DEFAULT_MODEL, transport, dataset_sha256=dataset['sha256'])
                row['model_digest'] = 'synthetic-digest'
                self.control_rows.append(row)
        self.write_control_rows()

    def write_control_rows(self):
        (self.baseline.parent / 'answers.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.control_rows))
        summary = {'status': self.baseline_value['status'], 'finished_at': self.baseline_value['finished_at'],
                   'selected_prompt_ids': self.baseline_value['selected_prompt_ids'],
                   'selected_arms': self.baseline_value['selected_arms'], 'planned_answers': 400,
                   'written_answers': len(self.control_rows), 'unattempted_answers': 400 - len(self.control_rows),
                   'answer_status_counts': dict(Counter(row['status'] for row in self.control_rows)),
                   'calls': sum(len(row['attempts']) for row in self.control_rows)}
        (self.baseline.parent / 'summary.json').write_text(json.dumps(summary))

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

    def test_default_low_payload_retains_legacy_serialized_bytes(self):
        text = self.instructions.read_text()
        legacy = build_payload(self.items[0], 'grounded_call')
        legacy['messages'][-1]['content'] = self.items[0]['prompt'][-1]['content'] + '\n\n[' + text + ']'
        for payload in (make_payload(self.items[0], text),
                        make_payload(self.items[0], text, reasoning_effort='low')):
            self.assertEqual(json.dumps(payload, ensure_ascii=False).encode(),
                             json.dumps(legacy, ensure_ascii=False).encode())

    def test_medium_generation_changes_only_effort_and_keeps_input_hash(self):
        results = {}
        for effort in ('low', 'medium'):
            self.out = self.root / 'results' / effort
            transport = FakeTransport([response()] * 20)
            summary, code = self.run_cycle(transport=transport, reasoning_effort=effort)
            self.assertEqual((summary['calls'], code), (20, 0))
            records = [json.loads(line) for line in (self.out / 'answers.jsonl').read_text().splitlines()]
            manifest = json.loads((self.out / 'manifest.json').read_text())
            self.assertEqual(manifest['arms']['codex_guided_v3']['reasoning_effort'], effort)
            for payload, row, prepared in zip(transport.payloads, records, manifest['prepared_requests']):
                self.assertEqual(len(row['attempts']), 1)
                self.assertEqual(row['attempts'][0]['reasoning_effort'], effort)
                self.assertEqual(row['request_sha256'], canonical_hash(payload))
                self.assertEqual(row['attempts'][0]['request_sha256'], prepared['request_sha256'])
                self.assertEqual(row['input_sha256'], canonical_hash(payload['messages']))
                self.assertEqual(row['attempts'][0]['input_sha256'], prepared['input_sha256'])
                self.assertEqual((payload['max_tokens'], payload['temperature']), (2048, 0))
            results[effort] = (transport.payloads, records)
        for low, medium, low_row, medium_row in zip(results['low'][0], results['medium'][0],
                                                   results['low'][1], results['medium'][1]):
            self.assertEqual({key for key in low if low[key] != medium[key]}, {'reasoning_effort'})
            self.assertEqual(low['messages'], medium['messages'])
            self.assertEqual(low_row['input_sha256'], medium_row['input_sha256'])
            self.assertNotEqual(low_row['request_sha256'], medium_row['request_sha256'])

    def test_high_payload_is_supported_without_other_request_changes(self):
        text = self.instructions.read_text()
        low = make_payload(self.items[0], text)
        self.assertEqual(make_payload(self.items[0], text, reasoning_effort='high'), low | {'reasoning_effort': 'high'})

    def test_invalid_reasoning_effort_rejects_payload_generation_and_cycle_before_io(self):
        for effort in (None, True, False, '', 'LOW', ' medium ', 'unknown', 1, [], {}):
            with self.subTest(effort=effort):
                transport = FakeTransport([])
                with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
                    with self.assertRaisesRegex(ValueError, 'reasoning-effort'):
                        make_payload(self.items[0], self.instructions.read_text(), reasoning_effort=effort)
                    with self.assertRaisesRegex(ValueError, 'reasoning-effort'):
                        cycle.generate_candidate(self.items[0], 'Generic instructions', 'codex_guided_v3',
                                                 DEFAULT_MODEL, transport, dataset_sha256='synthetic',
                                                 reasoning_effort=effort)
                    for prepare_only in (False, True):
                        with self.assertRaisesRegex(ValueError, 'reasoning-effort'):
                            self.run_cycle(transport=transport, prepare_only=prepare_only, reasoning_effort=effort)
                self.assertEqual((transport.metadata_calls, transport.payloads), (0, []))
                self.assertFalse(self.out.exists())

    def test_cli_passes_default_low_and_selected_levels_and_rejects_unknown(self):
        argv = ['--data', str(self.data), '--baseline-manifest', str(self.baseline),
                '--instructions-file', str(self.instructions), '--out', str(self.out), '--prepare-only']
        summary = {'status': 'prepared', 'written_answers': 0, 'planned_answers': 20, 'calls': 0}
        for effort in (None, 'low', 'medium', 'high'):
            with self.subTest(effort=effort), patch.object(cycle, 'run_cycle', return_value=(summary, 0)) as runner:
                with patch('sys.stdout'):
                    self.assertEqual(cycle.main(argv + ([] if effort is None else ['--reasoning-effort', effort])), 0)
                self.assertEqual(runner.call_args.kwargs['reasoning_effort'], effort or 'low')
        with patch.object(cycle, 'run_cycle') as runner, patch('sys.stderr'):
            with self.assertRaises(SystemExit) as error:
                cycle.main(argv + ['--reasoning-effort', 'unknown'])
            self.assertEqual(error.exception.code, 2)
            runner.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_medium_candidate_keeps_finalized_control_fixed_at_low(self):
        self.finalize_partial_control()
        transport = FakeTransport([response()] * 20)
        summary, code = self.run_cycle(transport=transport, reasoning_effort='medium', allow_finalized_dev_control=True)
        self.assertEqual((summary['calls'], code), (20, 0))
        manifest = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(manifest['baseline']['development_control']['arm_config_sha256'],
                         canonical_hash(arm_config(('grounded_call',))['grounded_call']))
        self.assertTrue(all(payload['reasoning_effort'] == 'medium' for payload in transport.payloads))
        self.baseline_value['arms']['grounded_call']['reasoning_effort'] = 'medium'
        self.write_baseline()
        self.out = self.root / 'results' / 'wrong-control'
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, 'fixed arm configuration'):
            self.run_cycle(transport=transport, reasoning_effort='medium', allow_finalized_dev_control=True)
        self.assertEqual((transport.metadata_calls, transport.payloads), (0, []))
        self.assertFalse(self.out.exists())

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

    def test_finalized_partial_control_requires_explicit_option_and_preserves_original_sources(self):
        self.finalize_partial_control()
        source_paths = [self.baseline, self.baseline.parent / 'summary.json', self.baseline.parent / 'answers.jsonl']
        before = {path: path.read_bytes() for path in source_paths}
        transport = FakeTransport([])
        with self.assertRaisesRegex(ValueError, 'completed baseline'):
            self.run_cycle(transport=transport)
        self.assertEqual(transport.metadata_calls, 0)
        transport = FakeTransport([response()] * 20)
        summary, code = self.run_cycle(transport=transport, allow_finalized_dev_control=True)
        self.assertEqual((summary['status'], summary['calls'], code), ('completed', 20, 0))
        manifest = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(manifest['baseline']['status'], 'partial')
        provenance = manifest['baseline']['development_control']
        self.assertEqual(provenance['original_manifest_status'], 'partial')
        self.assertEqual(provenance['validated_count'], 20)
        self.assertEqual(provenance['answers_byte_sha256'], hashlib.sha256(before[source_paths[2]]).hexdigest())
        for entry, row in zip(provenance['row_provenance'], [row for row in self.control_rows if row['arm'] == 'grounded_call'][:20]):
            self.assertEqual(entry['full_row_sha256'], canonical_hash(row))
        self.assertEqual(before, {path: path.read_bytes() for path in source_paths})

    def test_prepared_explicit_control_records_provenance_without_network(self):
        self.finalize_partial_control()
        transport = FakeTransport([])
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No network')):
            summary, code = self.run_cycle(prepare_only=True, allow_finalized_dev_control=True, transport=transport)
        self.assertEqual((summary['calls'], code, transport.metadata_calls), (0, 0, 0))
        self.assertFalse((self.out / 'answers.jsonl').exists())
        manifest = json.loads((self.out / 'manifest.json').read_text())
        self.assertEqual(manifest['baseline']['development_control']['validated_count'], 20)

    def test_explicit_control_rejects_missing_duplicate_failed_truncated_empty_or_hash_mismatched_rows(self):
        self.finalize_partial_control()
        originals = deepcopy(self.control_rows)
        variants = ('missing', 'duplicate', 'failed', 'truncated', 'empty', 'request_hash', 'input_hash',
                    'model_digest', 'prompt_hash', 'attempt_settings', 'selected_attempt', 'answer_mismatch')
        for variant in variants:
            with self.subTest(variant=variant):
                self.control_rows = deepcopy(originals)
                row = self.control_rows[1]
                if variant == 'missing':
                    self.control_rows.pop(1)
                elif variant == 'duplicate':
                    self.control_rows.append(deepcopy(row))
                elif variant in ('failed', 'truncated'):
                    row['status'] = variant
                elif variant == 'empty':
                    row['answer'] = ' '
                elif variant == 'request_hash':
                    row['attempts'][0]['request_sha256'] = 'wrong'
                elif variant == 'input_hash':
                    row['input_sha256'] = 'wrong'
                elif variant == 'model_digest':
                    row['model_digest'] = 'different'
                elif variant == 'prompt_hash':
                    row['prompt_sha256'] = 'wrong'
                elif variant == 'attempt_settings':
                    row['attempts'][0]['reasoning_effort'] = 'high'
                elif variant == 'selected_attempt':
                    row['selected_attempt'] = True
                else:
                    row['attempts'][0]['answer'] = 'Different stored attempt answer'
                self.write_control_rows()
                transport = FakeTransport([])
                with self.assertRaises(ValueError):
                    self.run_cycle(transport=transport, allow_finalized_dev_control=True)
                self.assertEqual((transport.metadata_calls, len(transport.payloads)), (0, 0))
                self.assertFalse(self.out.exists())

    def test_explicit_control_rejects_unfinalized_corrupt_summary_or_changed_config(self):
        self.finalize_partial_control()
        good = deepcopy(self.baseline_value)
        for variant in ('running', 'finished_at', 'summary', 'config', 'endpoint', 'corrupt_json', 'duplicate_json_field'):
            with self.subTest(variant=variant):
                self.baseline_value = deepcopy(good)
                self.write_baseline()
                self.write_control_rows()
                if variant == 'running':
                    self.baseline_value['status'] = 'running'
                    self.write_baseline()
                elif variant == 'finished_at':
                    self.baseline_value.pop('finished_at')
                    self.write_baseline()
                elif variant == 'summary':
                    path = self.baseline.parent / 'summary.json'
                    summary = json.loads(path.read_text())
                    summary['written_answers'] = 399
                    path.write_text(json.dumps(summary))
                elif variant == 'config':
                    self.baseline_value['arms']['grounded_call']['temperature'] = False
                    self.write_baseline()
                elif variant == 'endpoint':
                    self.baseline_value['config']['base_url'] = 'https://example.org/v1'
                    self.write_baseline()
                else:
                    path = self.baseline.parent / 'answers.jsonl'
                    path.write_text('{invalid' if variant == 'corrupt_json' else '{"arm":"raw_default","arm":"grounded_call"}\n')
                transport = FakeTransport([])
                with self.assertRaises(ValueError):
                    self.run_cycle(transport=transport, allow_finalized_dev_control=True)
                self.assertEqual(transport.metadata_calls, 0)
                self.assertFalse(self.out.exists())

    def test_explicit_control_source_change_during_generation_stops_before_second_call(self):
        self.finalize_partial_control()
        def change_source():
            self.control_rows[1]['private_metadata'] = 'Source changed during generation'
            self.write_control_rows()
        transport = FakeTransport([response()], on_complete=change_source)
        summary, code = self.run_cycle(transport=transport, allow_finalized_dev_control=True)
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
        self.assertEqual(summary['error']['code'], 'provenance_changed')


if __name__ == '__main__':
    unittest.main()
