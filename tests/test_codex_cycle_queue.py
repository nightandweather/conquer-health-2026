"""One-shot queue dependency/source gates using synthetic model transports."""

from copy import deepcopy
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, RequestFailure, arm_config,
                                    build_payload, canonical_hash, load_dataset)
from benchmarks import healthbench_codex_cycle as cycle
from scripts import run_codex_cycle_after_baseline as queue


class FakeClock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class FakeTransport:
    def __init__(self, *, partial=False):
        self.calls = 0
        self.partial = partial

    def model_metadata(self, model):
        return {'name': model, 'digest': 'synthetic-digest', 'metadata_status': 'available'}

    def complete(self, payload):
        self.calls += 1
        return {'choices': [{'message': {'content': 'Synthetic generated answer.'},
                             'finish_reason': 'length' if self.partial and self.calls == 1 else 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}


class CodexCycleQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch.object(cycle, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True, capture_output=True)
        self.instructions = self.root / 'instructions.txt'
        self.instructions.write_text('Use confirmed facts and readable Markdown.')
        subprocess.run(['git', '-C', str(self.root), 'add', '--', str(self.instructions)],
                       check=True, capture_output=True)
        self.items = [{'prompt_id': f'id-{index:03}', 'prompt': [{'role': 'user', 'content': f'Synthetic {index}'}],
                       'rubrics': [{'criterion': 'Synthetic positive criterion', 'points': 5}]}
                      for index in range(200)]
        self.data = self.root / 'data.json'
        self.data.write_text(json.dumps(self.items))
        _, dataset = load_dataset(self.data, limit=0)
        baseline_run = self.root / 'results' / 'baseline'
        baseline_run.mkdir(parents=True)
        self.baseline = baseline_run / 'manifest.json'
        self.baseline_value = {'status': 'running', 'dataset': dataset,
                               'selected_prompt_ids': dataset['selected_prompt_ids'],
                               'selected_arms': ['raw_default', 'grounded_call'],
                               'config': {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL},
                               'model': {'name': DEFAULT_MODEL, 'digest': 'synthetic-digest'},
                               'arms': {'raw_default': {}, 'grounded_call': {'max_tokens': 2048,
                                                                            'temperature': 0, 'reasoning_effort': 'low'}}}
        self.write_baseline()
        self.baseline_rows = [{'prompt_id': item['prompt_id'], 'arm': arm,
                               'answer': f'Original {arm} answer {index}', 'status': 'completed',
                               'model': DEFAULT_MODEL, 'model_digest': 'synthetic-digest',
                               'dataset_sha256': dataset['sha256'], 'prompt_sha256': canonical_hash(item['prompt']),
                               'attempts': [{'original_marker': index}], 'original_private_field': ['retain', index]}
                              for index, item in enumerate(self.items) for arm in ('raw_default', 'grounded_call')]
        (baseline_run / 'answers.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.baseline_rows))
        self.out = self.root / 'results' / 'queue'
        self.clock = FakeClock()
        self.generated = []
        self.packet_calls = []

    def write_baseline(self):
        self.baseline.write_text(json.dumps(self.baseline_value))

    def fake_cycle(self, *args, **kwargs):
        self.generated.append((args, kwargs))
        return cycle.run_cycle(*args, **kwargs, transport=FakeTransport())

    def fake_packets(self, *args, **kwargs):
        self.packet_calls.append((args, kwargs))
        return {'selected_count': 20, 'ready_packet_count': 20, 'pending_count': 0}

    def run_queue(self, sleeper=None, **kwargs):
        return queue.run_queue(self.data, self.baseline, self.instructions, self.out,
                               cycle_runner=self.fake_cycle, packet_runner=self.fake_packets,
                               clock=self.clock, sleeper=sleeper or (lambda seconds: None), **kwargs)

    def finalize_partial_control(self):
        self.baseline_value.update(status='partial', finished_at='2026-10-11T06:12:48+00:00',
                                   arms=arm_config(('raw_default', 'grounded_call')))
        self.write_baseline()
        for index, row in enumerate(self.baseline_rows):
            if row['arm'] == 'grounded_call':
                item = self.items[index // 2]
                payload = build_payload(item, 'grounded_call')
                row.update(finish_reason='stop', error=None, selected_attempt=1, fallback_used=False)
                row['attempts'] = [{'number': 1, 'max_tokens': payload['max_tokens'],
                                    'temperature': payload['temperature'], 'reasoning_effort': payload['reasoning_effort'],
                                    'request_sha256': canonical_hash(payload), 'answer': row['answer'],
                                    'status': 'completed', 'finish_reason': 'stop', 'error': None,
                                    'response_received': True}]
        self.baseline_rows[-2].update(status='failed', answer='')
        self.write_control_rows()

    def write_control_rows(self):
        (self.baseline.parent / 'answers.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.baseline_rows))
        summary = {'status': self.baseline_value['status'], 'finished_at': self.baseline_value['finished_at'],
                   'selected_prompt_ids': self.baseline_value['selected_prompt_ids'],
                   'selected_arms': self.baseline_value['selected_arms'], 'planned_answers': 400,
                   'written_answers': len(self.baseline_rows), 'unattempted_answers': 400 - len(self.baseline_rows),
                   'answer_status_counts': dict(Counter(row['status'] for row in self.baseline_rows)),
                   'calls': sum(len(row['attempts']) for row in self.baseline_rows)}
        (self.baseline.parent / 'summary.json').write_text(json.dumps(summary))

    def test_running_dependency_waits_five_seconds_then_generates_only_dev_twenty(self):
        def complete(seconds):
            self.assertEqual(seconds, 5)
            self.assertEqual(self.generated, [])
            self.clock.now += seconds
            self.baseline_value['status'] = 'completed'
            self.write_baseline()
        self.assertEqual(self.run_queue(sleeper=complete), 0)
        self.assertEqual(len(self.generated), 1)
        args, kwargs = self.generated[0]
        self.assertEqual(kwargs['model'], DEFAULT_MODEL)
        self.assertEqual(kwargs['base_url'], DEFAULT_BASE_URL)
        rows = [json.loads(line) for line in (self.out / 'comparison' / 'answers.jsonl').read_text().splitlines()]
        self.assertEqual(len(rows), 40)
        self.assertEqual({row['prompt_id'] for row in rows}, {item['prompt_id'] for item in self.items[:20]})
        self.assertEqual({row['arm'] for row in rows}, {'grounded_call', 'codex_guided_v3'})
        self.assertEqual(self.packet_calls[0][1]['paired_arms'], ('grounded_call', 'codex_guided_v3'))
        state = json.loads((self.out / 'queue.json').read_text())
        self.assertEqual(state['status'], 'completed')
        self.assertIsNone(state['score'])
        self.assertEqual(state['score_status'], 'independent_codex_review_required')
        self.assertEqual((self.out / 'queue.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def test_failed_interrupted_or_partial_baseline_never_calls_generation(self):
        for status in ('failed', 'interrupted', 'partial', 'blocked'):
            with self.subTest(status=status):
                self.out = self.root / 'results' / ('queue-' + status)
                self.baseline_value['status'] = status
                self.write_baseline()
                self.assertEqual(self.run_queue(), 2)
                self.assertEqual(self.generated, [])
                self.assertFalse((self.out / 'candidate').exists())
                self.assertEqual(json.loads((self.out / 'queue.json').read_text())['reason'], 'baseline_' + status)

    def test_baseline_identity_or_instruction_change_while_waiting_blocks_all_calls(self):
        original = deepcopy(self.baseline_value)
        for target in ('baseline', 'instructions', 'data'):
            with self.subTest(target=target):
                self.out = self.root / 'results' / ('queue-change-' + target)
                self.baseline_value = deepcopy(original)
                self.write_baseline()
                self.instructions.write_text('Use confirmed facts and readable Markdown.')
                self.data.write_text(json.dumps(self.items))
                def change(seconds):
                    self.clock.now += seconds
                    if target == 'baseline':
                        self.baseline_value['arms']['grounded_call']['temperature'] = 0.2
                        self.baseline_value['status'] = 'completed'
                        self.write_baseline()
                    elif target == 'instructions':
                        self.instructions.write_text('Changed generic instructions.')
                    else:
                        self.data.write_bytes(self.data.read_bytes() + b'\n')
                self.assertEqual(self.run_queue(sleeper=change), 2)
                self.assertEqual(self.generated, [])
                expected = {'baseline': 'baseline_identity_changed', 'instructions': 'instructions_changed',
                            'data': 'dataset_changed'}[target]
                self.assertEqual(json.loads((self.out / 'queue.json').read_text())['reason'], expected)

    def test_wait_timeout_never_generates(self):
        def sleep(seconds):
            self.clock.now += seconds
        self.assertEqual(self.run_queue(sleeper=sleep, maximum_wait_seconds=7), 2)
        self.assertEqual(self.clock.now, 7)
        self.assertEqual(self.generated, [])
        self.assertEqual(json.loads((self.out / 'queue.json').read_text())['reason'], 'maximum_wait_elapsed')

    def test_comparison_preserves_complete_original_rows_and_hash_provenance(self):
        self.baseline_value['status'] = 'completed'
        self.write_baseline()
        self.assertEqual(self.run_queue(), 0)
        comparison = self.out / 'comparison'
        rows = [json.loads(line) for line in (comparison / 'answers.jsonl').read_text().splitlines()]
        controls = [row for row in rows if row['arm'] == 'grounded_call']
        originals = [row for row in self.baseline_rows if row['arm'] == 'grounded_call'][:20]
        self.assertEqual(controls, originals)
        manifest = json.loads((comparison / 'manifest.json').read_text())
        for provenance in manifest['row_provenance']:
            row = next(row for row in rows if row['arm'] == provenance['arm']
                       and row['prompt_id'] == provenance['prompt_id'])
            self.assertEqual(provenance['full_row_sha256'], canonical_hash(row))
            self.assertEqual(provenance['original_status'], row['status'])
        self.assertEqual(manifest['sources']['baseline']['original_manifest_status'], 'completed')
        self.assertEqual(manifest['sources']['candidate']['original_manifest_status'], 'completed')

    def test_partial_candidate_retains_original_truncation_and_review_pending(self):
        self.baseline_value['status'] = 'completed'
        self.write_baseline()
        def partial_cycle(*args, **kwargs):
            return cycle.run_cycle(*args, **kwargs, transport=FakeTransport(partial=True))
        code = queue.run_queue(self.data, self.baseline, self.instructions, self.out,
                               cycle_runner=partial_cycle, packet_runner=self.fake_packets)
        self.assertEqual(code, 1)
        state = json.loads((self.out / 'queue.json').read_text())
        self.assertEqual(state['status'], 'partial')
        self.assertIsNone(state['score'])
        rows = [json.loads(line) for line in (self.out / 'comparison' / 'answers.jsonl').read_text().splitlines()]
        candidate = next(row for row in rows if row['arm'] == 'codex_guided_v3' and row['prompt_id'] == 'id-000')
        self.assertEqual(candidate['status'], 'truncated')
        self.assertEqual(candidate['finish_reason'], 'length')

    def test_tampered_candidate_or_heldout_row_is_rejected_before_comparison_writes(self):
        self.baseline_value['status'] = 'completed'
        self.write_baseline()
        candidate = self.root / 'results' / 'source-candidate'
        cycle.run_cycle(self.data, self.baseline, self.instructions, candidate, transport=FakeTransport())
        text, source = cycle.load_instructions(self.instructions, 'codex_guided_v3', 'codex_guided_v3')
        answer_path = candidate / 'answers.jsonl'
        original_raw = answer_path.read_bytes()
        for change in ({'request_sha256': 'wrong'}, {'model_digest': 'other-digest'}, {'prompt_id': 'id-020'}):
            with self.subTest(change=change):
                rows = [json.loads(line) for line in original_raw.decode().splitlines()]
                rows[0].update(change)
                answer_path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
                target = self.root / 'results' / ('compare-' + next(iter(change)))
                with self.assertRaises(ValueError):
                    queue.comparison_snapshot(self.data, self.baseline, candidate, target,
                                              'codex_guided_v3', text, source, packet_runner=self.fake_packets)
                self.assertFalse(target.exists())

    def test_nonlocal_baseline_or_existing_queue_fails_before_any_calls(self):
        self.baseline_value['config']['base_url'] = 'https://example.org/v1'
        self.write_baseline()
        with self.assertRaisesRegex(ValueError, 'current local'):
            self.run_queue()
        self.assertFalse(self.out.exists())
        self.baseline_value['config']['base_url'] = DEFAULT_BASE_URL
        self.write_baseline()
        self.out.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'new private directory'):
            self.run_queue()
        self.assertEqual(self.generated, [])

    def test_generalized_packet_builder_creates_actual_twenty_blind_pairs(self):
        from benchmarks.healthbench_review import create_review_packets
        self.baseline_value['status'] = 'completed'
        self.write_baseline()
        with patch('benchmarks.healthbench_review.ROOT', self.root):
            code = queue.run_queue(self.data, self.baseline, self.instructions, self.out,
                                   cycle_runner=self.fake_cycle, packet_runner=create_review_packets)
        self.assertEqual(code, 0)
        review = self.out / 'comparison' / 'review'
        progress = json.loads((review / 'progress.json').read_text())
        self.assertEqual(progress['ready_packet_count'], 20)
        self.assertEqual(progress['pending_count'], 0)
        private = json.loads((review / 'private_manifest.json').read_text())
        self.assertEqual(private['paired_arms'], ['grounded_call', 'codex_guided_v3'])
        for entry in private['packets'].values():
            self.assertEqual(set(entry['candidate_map'].values()), {'grounded_call', 'codex_guided_v3'})
            self.assertIn(entry['prompt_id'], [item['prompt_id'] for item in self.items[:20]])

    def test_failed_and_missing_candidates_remain_actual_errors_and_pending(self):
        from benchmarks.healthbench_review import create_review_packets
        self.baseline_value['status'] = 'completed'
        self.write_baseline()
        class FailedTransport(FakeTransport):
            def complete(self, payload):
                raise RequestFailure('connection_error')
        def blocked_cycle(*args, **kwargs):
            return cycle.run_cycle(*args, **kwargs, transport=FailedTransport())
        with patch('benchmarks.healthbench_review.ROOT', self.root):
            code = queue.run_queue(self.data, self.baseline, self.instructions, self.out,
                                   cycle_runner=blocked_cycle, packet_runner=create_review_packets)
        self.assertEqual(code, 1)
        state = json.loads((self.out / 'queue.json').read_text())
        self.assertEqual(state['status'], 'partial')
        self.assertEqual(state['comparison']['planned_answers'], 40)
        self.assertEqual(state['comparison']['written_answers'], 21)
        self.assertEqual(state['comparison']['review']['ready_packet_count'], 0)
        self.assertEqual(state['comparison']['review']['pending_count'], 20)
        rows = [json.loads(line) for line in (self.out / 'comparison' / 'answers.jsonl').read_text().splitlines()]
        candidates = [row for row in rows if row['arm'] == 'codex_guided_v3']
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['status'], 'blocked')
        self.assertEqual(candidates[0]['error']['code'], 'connection_error')

    def test_explicit_finalized_partial_control_generates_dev_pairs_without_relabeling_source(self):
        self.finalize_partial_control()
        source_paths = [self.baseline, self.baseline.parent / 'summary.json', self.baseline.parent / 'answers.jsonl']
        original = {path: path.read_bytes() for path in source_paths}
        self.assertEqual(self.run_queue(allow_finalized_dev_control=True), 0)
        self.assertEqual(len(self.generated), 1)
        self.assertTrue(self.generated[0][1]['allow_finalized_dev_control'])
        state = json.loads((self.out / 'queue.json').read_text())
        self.assertEqual(state['original_baseline_status'], 'partial')
        self.assertNotIn('baseline_completed_byte_sha256', state)
        self.assertEqual(state['validated_development_control']['validated_count'], 20)
        self.assertEqual(state['validated_development_control']['answers_byte_sha256'],
                         hashlib.sha256(original[source_paths[2]]).hexdigest())
        comparison = json.loads((self.out / 'comparison' / 'manifest.json').read_text())
        self.assertEqual(comparison['sources']['baseline']['original_manifest_status'], 'partial')
        self.assertEqual(comparison['sources']['baseline']['validated_development_control'],
                         state['validated_development_control'])
        rows = [json.loads(line) for line in (self.out / 'comparison' / 'answers.jsonl').read_text().splitlines()]
        self.assertEqual([row for row in rows if row['arm'] == 'grounded_call'],
                         [row for row in self.baseline_rows if row['arm'] == 'grounded_call'][:20])
        self.assertEqual(original, {path: path.read_bytes() for path in source_paths})

    def test_explicit_partial_control_rejects_invalid_sources_before_invoking_runner(self):
        self.finalize_partial_control()
        good_rows, good_manifest = deepcopy(self.baseline_rows), deepcopy(self.baseline_value)
        for variant in ('failed', 'truncated', 'missing', 'duplicate', 'request_hash', 'unfinalized', 'wrong_summary'):
            with self.subTest(variant=variant):
                self.out = self.root / 'results' / ('explicit-invalid-' + variant)
                self.baseline_rows = deepcopy(good_rows)
                self.baseline_value = deepcopy(good_manifest)
                if variant in ('failed', 'truncated'):
                    self.baseline_rows[1]['status'] = variant
                elif variant == 'missing':
                    self.baseline_rows.pop(1)
                elif variant == 'duplicate':
                    self.baseline_rows.append(deepcopy(self.baseline_rows[1]))
                elif variant == 'request_hash':
                    self.baseline_rows[1]['attempts'][0]['request_sha256'] = 'wrong'
                elif variant == 'unfinalized':
                    self.baseline_value.pop('finished_at')
                self.write_baseline()
                if variant != 'unfinalized':
                    self.write_control_rows()
                if variant == 'wrong_summary':
                    summary_path = self.baseline.parent / 'summary.json'
                    summary = json.loads(summary_path.read_text())
                    summary['finished_at'] = '2026-10-11T07:00:00+00:00'
                    summary_path.write_text(json.dumps(summary))
                self.assertEqual(self.run_queue(allow_finalized_dev_control=True), 2)
                self.assertEqual(self.generated, [])
                self.assertFalse((self.out / 'candidate').exists())
                state = json.loads((self.out / 'queue.json').read_text())
                self.assertEqual(state['status'], 'blocked')

    def test_explicit_comparison_rejects_control_changes_after_candidate_generation(self):
        self.finalize_partial_control()
        candidate = self.root / 'results' / 'candidate-source'
        cycle.run_cycle(self.data, self.baseline, self.instructions, candidate,
                        transport=FakeTransport(), allow_finalized_dev_control=True)
        text, source = cycle.load_instructions(self.instructions, 'codex_guided_v3', 'codex_guided_v3')
        self.baseline_rows[1]['private_metadata'] = 'Source changed after generation'
        self.write_control_rows()
        comparison = self.root / 'results' / 'comparison-source-changed'
        with self.assertRaisesRegex(ValueError, 'changed after candidate generation'):
            queue.comparison_snapshot(self.data, self.baseline, candidate, comparison,
                                      'codex_guided_v3', text, source, packet_runner=self.fake_packets,
                                      allow_finalized_dev_control=True)
        self.assertFalse(comparison.exists())

    def test_unavailable_model_metadata_is_recorded_as_blocked_with_zero_inference_calls(self):
        self.finalize_partial_control()
        class UnavailableTransport(FakeTransport):
            def model_metadata(self, model):
                return {'name': model, 'digest': None, 'metadata_status': 'unavailable'}
        transport = UnavailableTransport()
        def unavailable_cycle(*args, **kwargs):
            return cycle.run_cycle(*args, **kwargs, transport=transport)
        code = queue.run_queue(self.data, self.baseline, self.instructions, self.out,
                               cycle_runner=unavailable_cycle, packet_runner=self.fake_packets,
                               allow_finalized_dev_control=True)
        self.assertEqual(code, 2)
        self.assertEqual(transport.calls, 0)
        state = json.loads((self.out / 'queue.json').read_text())
        self.assertEqual((state['status'], state['reason']), ('blocked', 'model_metadata_unavailable'))
        self.assertEqual(state['generation']['calls'], 0)
        self.assertEqual(state['generation']['written_answers'], 0)
        self.assertFalse((self.out / 'candidate').exists())

    def test_queue_to_runner_control_mutation_blocks_before_model_metadata_or_inference(self):
        self.finalize_partial_control()
        transport = FakeTransport()
        metadata_calls = []
        original_metadata = transport.model_metadata
        def metadata(model):
            metadata_calls.append(model)
            return original_metadata(model)
        transport.model_metadata = metadata
        def mutate_before_runner(*args, **kwargs):
            self.baseline_rows[1]['private_metadata'] = 'Changed between queue gate and runner'
            self.write_control_rows()
            return cycle.run_cycle(*args, **kwargs, transport=transport)
        code = queue.run_queue(self.data, self.baseline, self.instructions, self.out,
                               cycle_runner=mutate_before_runner, packet_runner=self.fake_packets,
                               allow_finalized_dev_control=True)
        self.assertEqual(code, 2)
        self.assertEqual((transport.calls, metadata_calls, self.packet_calls), (0, [], []))
        self.assertFalse((self.out / 'candidate').exists())
        state = json.loads((self.out / 'queue.json').read_text())
        self.assertEqual(state['status'], 'blocked')

    def test_finalized_status_matches_legacy_failed_semantics_without_rejecting_unselected_truncation(self):
        self.finalize_partial_control()
        self.baseline_value['status'] = 'completed'
        self.write_baseline()
        self.write_control_rows()
        self.assertEqual(self.run_queue(allow_finalized_dev_control=True), 2)
        self.assertEqual(self.generated, [])
        # Legacy completed means every call finished without a failed answer;
        # unrelated raw truncation does not disqualify completed dev controls.
        self.baseline_rows[-2].update(status='truncated', answer='Synthetic unrelated partial raw answer')
        self.write_control_rows()
        self.out = self.root / 'results' / 'completed-with-raw-truncation'
        self.assertEqual(self.run_queue(allow_finalized_dev_control=True), 0)
        self.assertEqual(len(self.generated), 1)


if __name__ == '__main__':
    unittest.main()
