"""Synthetic provenance/tamper tests; no real data and no model calls."""

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import healthbench_budget_preflight as preflight
from benchmarks.healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX,
                                    Transport, arm_config, build_payload, canonical_hash, sum_usage)
from benchmarks.healthbench_codex_cycle import REVIEW_REQUIRED, make_payload


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class BudgetPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root_patch = patch.object(preflight, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.data = self.root / 'private_data' / 'synthetic_roles.json'
        self.instructions = self.root / 'benchmarks' / 'prompts' / 'grounded_v2_frozen.txt'
        self.source = self.root / 'results' / 'original' / 'candidate' / 'manifest.json'
        self.comparison = self.root / 'results' / 'original' / 'comparison' / 'manifest.json'
        self.answers = self.source.parent / 'answers.jsonl'
        self.summary = self.source.parent / 'summary.json'
        self.out = self.root / 'results' / 'budget-preflight'
        for path in (self.data, self.instructions, self.source, self.comparison):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.items = [{'prompt_id': f'DEV{i:04d}',
                       'prompt': [{'role': 'system', 'content': 'SYNTHETIC_SYSTEM_SENTINEL'},
                                  {'role': 'user', 'content': f'SYNTHETIC_QUESTION_SENTINEL_{i}'}],
                       'rubrics': [{'criterion': 'SYNTHETIC_CRITERION_SENTINEL', 'points': 1}],
                       'reference': 'SYNTHETIC_REFERENCE_SENTINEL'} for i in range(20)]
        self.write(self.data, self.items)
        self.instructions.write_bytes(GROUNDED_SUFFIX.encode('utf-8'))
        self.digest = sha(b'synthetic model digest')
        self.ids = [item['prompt_id'] for item in self.items]
        dataset_sha = sha(self.data.read_bytes())
        instruction_sha = sha(self.instructions.read_bytes())
        self.payloads = [make_payload(item, GROUNDED_SUFFIX, reasoning_effort='medium') for item in self.items]
        low_entries = []
        for item in self.items:
            payload = build_payload(item, 'grounded_call')
            low_entries.append({'prompt_id': item['prompt_id'], 'arm': 'grounded_call',
                                'original_status': 'completed', 'full_row_sha256': canonical_hash(['low', item['prompt_id']]),
                                'input_sha256': canonical_hash(payload['messages']),
                                'request_sha256': canonical_hash(payload)})
        control = {'policy': 'explicit_finalized_development_control', 'original_manifest_status': 'partial',
                   'finished_at': '2026-10-11T01:01:00+00:00', 'summary_byte_sha256': sha(b'low summary'),
                   'answers_byte_sha256': sha(b'low answers'), 'validated_count': 20,
                   'selected_prompt_ids': self.ids, 'arm': 'grounded_call',
                   'arm_config_sha256': canonical_hash(arm_config(('grounded_call',))['grounded_call']),
                   'model_digest': self.digest, 'row_provenance': low_entries}
        self.rows = []
        for index, (item, payload) in enumerate(zip(self.items, self.payloads)):
            failed = index in (3, 5, 13)
            answer = '' if failed else f'SYNTHETIC_ANSWER_SENTINEL_{index}'
            usage = {'prompt_tokens': 10, 'completion_tokens': 2048 if failed else 20,
                     'total_tokens': 2058 if failed else 30}
            error = {'code': 'empty_answer', 'http_status': None} if failed else None
            attempt = {'number': 1, 'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium',
                       'request_sha256': canonical_hash(payload), 'input_sha256': canonical_hash(payload['messages']),
                       'answer': answer, 'finish_reason': 'length' if failed else 'stop', 'usage': usage,
                       'status': 'failed' if failed else 'completed', 'error': error,
                       'response_received': True, 'seconds': 1.0}
            self.rows.append({'prompt_id': item['prompt_id'], 'arm': preflight.ARM, 'model': DEFAULT_MODEL,
                              'dataset_sha256': dataset_sha, 'prompt_sha256': canonical_hash(item['prompt']),
                              'input_sha256': attempt['input_sha256'], 'request_sha256': attempt['request_sha256'],
                              'answer': answer, 'status': attempt['status'], 'finish_reason': attempt['finish_reason'],
                              'error': error, 'attempts': [attempt], 'usage': deepcopy(usage), 'seconds': 1.0,
                              'fallback_used': False, 'selected_attempt': 1, 'model_digest': self.digest,
                              'instructions_byte_sha256': instruction_sha, 'instructions_name': preflight.ARM,
                              'instructions_version': preflight.REVISION, 'score': None, 'score_status': REVIEW_REQUIRED})
        self.manifest = {'schema_version': 1, 'status': 'partial', 'error': None,
                         'started_at': '2026-10-12T01:00:00+00:00', 'finished_at': '2026-10-12T01:01:00+00:00',
                         'dataset': {'path': str(self.data), 'sha256': dataset_sha, 'total_records': 20,
                                     'selected_prompt_ids': self.ids,
                                     'selected_prompt_sha256': {item['prompt_id']: canonical_hash(item['prompt']) for item in self.items},
                                     'provenance': {'status': 'no_sidecar'}},
                         'selected_prompt_ids': self.ids, 'selected_arms': [preflight.ARM],
                         'split': {'name': 'development', 'limit': 20,
                                   'selection': 'first 20 frozen baseline IDs; heldout excluded'},
                         'baseline': {'path': str(self.root / 'results' / 'low' / 'manifest.json'),
                                      'sha256': sha(b'low manifest'), 'status': 'partial',
                                      'dataset_sha256': dataset_sha, 'selected_prompt_ids_sha256': canonical_hash(self.ids),
                                      'model': {'name': DEFAULT_MODEL, 'digest': self.digest}, 'development_control': control},
                         'instructions': {'name': preflight.ARM, 'version': preflight.REVISION,
                                          'path': 'benchmarks/prompts/grounded_v2_frozen.txt',
                                          'byte_sha256': instruction_sha, 'git_tracked': True},
                         'model': {'name': DEFAULT_MODEL, 'digest': self.digest, 'metadata_status': 'available'},
                         'arms': {preflight.ARM: {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium',
                                                  'max_attempts': 1, 'suffix': GROUNDED_SUFFIX, 'prompt_revision': preflight.REVISION}},
                         'config': {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'timeout': 300,
                                    'prepare_only': False, 'maximum_calls_per_case': 1, 'resume': False,
                                    'allow_finalized_dev_control': True},
                         'prepared_requests': [{'prompt_id': item['prompt_id'], 'prompt_sha256': canonical_hash(item['prompt']),
                                                'input_sha256': canonical_hash(payload['messages']), 'request_sha256': canonical_hash(payload)}
                                               for item, payload in zip(self.items, self.payloads)],
                         'score': None, 'score_status': REVIEW_REQUIRED}
        self.summary_value = {'status': 'partial', 'started_at': self.manifest['started_at'],
                              'finished_at': self.manifest['finished_at'], 'seconds': 20.0,
                              'selected_prompt_ids': self.ids, 'selected_arms': [preflight.ARM],
                              'planned_answers': 20, 'written_answers': 20, 'unattempted_answers': 0, 'calls': 20,
                              'usage': sum_usage([row['attempts'][0] for row in self.rows]),
                              'answer_status_counts': {'completed': 17, 'failed': 3}, 'error': None,
                              'score': None, 'score_status': REVIEW_REQUIRED}
        self.comparison_value = {'schema_version': 1, 'status': 'snapshot', 'dataset': deepcopy(self.manifest['dataset']),
                                 'selected_prompt_ids': self.ids, 'selected_arms': ['grounded_call', preflight.ARM],
                                 'arms': {'grounded_call': arm_config(('grounded_call',))['grounded_call'],
                                          preflight.ARM: deepcopy(self.manifest['arms'][preflight.ARM])},
                                 'sources': {'baseline': {'validated_development_control': deepcopy(control),
                                                          'manifest_byte_sha256': sha(b'low manifest'),
                                                          'answers_byte_sha256': control['answers_byte_sha256'],
                                                          'original_manifest_status': 'partial',
                                                          'manifest_path': self.manifest['baseline']['path'],
                                                          'answers_path': str(Path(self.manifest['baseline']['path']).parent / 'answers.jsonl')}},
                                 'planned_answers': 40, 'written_answers': 40, 'score': None, 'score_status': REVIEW_REQUIRED}
        self.repin()
        self.summary_pin = sha(self.summary.read_bytes())

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def repin(self, *, rebuild_rows=True):
        self.write(self.source, self.manifest)
        self.answers.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in self.rows), encoding='utf-8')
        self.write(self.summary, self.summary_value)
        self.comparison_value['sources']['candidate'] = {'manifest_byte_sha256': sha(self.source.read_bytes()),
                                                         'answers_byte_sha256': sha(self.answers.read_bytes()),
                                                         'original_manifest_status': 'partial',
                                                         'manifest_path': str(self.source), 'answers_path': str(self.answers)}
        if rebuild_rows:
            self.comparison_value['row_provenance'] = []
            for low, row in zip(self.manifest['baseline']['development_control']['row_provenance'], self.rows):
                self.comparison_value['row_provenance'].extend([
                    {'source': 'baseline', 'arm': 'grounded_call', 'prompt_id': low['prompt_id'],
                     'original_status': 'completed', 'full_row_sha256': low['full_row_sha256']},
                    {'source': 'candidate', 'arm': row['arm'], 'prompt_id': row['prompt_id'],
                     'original_status': row['status'], 'full_row_sha256': canonical_hash(row)}])
        self.write(self.comparison, self.comparison_value)
        self.pin = sha(self.comparison.read_bytes())

    def run_preflight(self, **overrides):
        args = {'data': self.data, 'source_manifest': self.source, 'source_comparison_manifest': self.comparison,
                'instructions_file': self.instructions, 'out': self.out, 'expected_comparison_sha256': self.pin,
                'expected_source_summary_sha256': self.summary_pin}
        args.update(overrides)
        with patch.object(Transport, 'complete', side_effect=AssertionError('generation must never run')) as complete, \
                patch.object(Transport, 'model_metadata', side_effect=AssertionError('metadata must never run')) as metadata:
            result = preflight.prepare_budget_preflight(**args)
            complete.assert_not_called()
            metadata.assert_not_called()
        return result

    def assert_rejected(self, **overrides):
        with self.assertRaises((ValueError, OSError)):
            self.run_preflight(**overrides)
        self.assertFalse(self.out.exists())

    def test_valid_partial_control_prepares_twenty_hashes_and_zero_calls(self):
        before = {path: path.read_bytes() for path in (self.data, self.source, self.answers, self.summary, self.comparison, self.instructions)}
        result = self.run_preflight()
        self.assertEqual(result['source_status_counts'], {'completed': 17, 'failed': 3})
        self.assertEqual(result['original_manifest_status'], 'partial')
        self.assertEqual(result['known_failure_count'], 3)
        self.assertEqual(result['generation_calls'], 0)
        self.assertEqual(result['model_metadata_calls'], 0)
        self.assertFalse(result['execution_support']['ready'])
        self.assertEqual(result['execution_support']['status'], 'NOT_READY')
        for payload, proof, row in zip(self.payloads, result['row_provenance'], self.rows):
            candidate = deepcopy(payload)
            candidate['max_tokens'] = 4096
            self.assertEqual(proof['candidate_request_sha256'], canonical_hash(candidate))
            self.assertEqual(proof['control_request_sha256'], canonical_hash(payload))
            self.assertEqual(proof['input_sha256'], canonical_hash(payload['messages']))
            self.assertEqual(proof['full_row_sha256'], canonical_hash(row))
            self.assertEqual(proof['changed_fields'], ['max_tokens'])
        self.assertEqual([entry['inference_failure_utility_score'] for entry in result['known_inference_failures']], [0, 0, 0])
        self.assertTrue(all(entry['rubric_labels_not_predicted'] is True for entry in result['known_inference_failures']))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.out / 'manifest.json').stat().st_mode), 0o600)
        serialized = (self.out / 'manifest.json').read_text()
        for sentinel in ('SYNTHETIC_SYSTEM_SENTINEL', 'SYNTHETIC_QUESTION_SENTINEL', 'SYNTHETIC_ANSWER_SENTINEL',
                         'SYNTHETIC_CRITERION_SENTINEL', 'SYNTHETIC_REFERENCE_SENTINEL', GROUNDED_SUFFIX):
            self.assertNotIn(sentinel, serialized)
        self.assertEqual(set(path.name for path in self.out.iterdir()), {'manifest.json'})

    def test_external_pin_is_required_and_verified_before_dataset_or_source(self):
        for bad in (None, '', True, '0' * 64, self.pin.upper()):
            with self.subTest(pin=bad), patch.object(preflight, '_dataset', side_effect=AssertionError('No dataset parsing')):
                self.assert_rejected(expected_comparison_sha256=bad)

    def test_comparison_byte_tamper_is_rejected(self):
        self.comparison.write_bytes(self.comparison.read_bytes() + b' ')
        self.assert_rejected()

    def test_default_or_different_source_is_rejected(self):
        wrong = self.root / 'results' / 'wrong' / 'manifest.json'
        wrong.parent.mkdir()
        self.write(wrong, self.manifest)
        self.answers.replace(wrong.parent / 'answers.jsonl')
        self.summary.replace(wrong.parent / 'summary.json')
        self.assert_rejected(source_manifest=wrong)

    def test_manifest_byte_tamper_is_rejected(self):
        self.source.write_bytes(self.source.read_bytes() + b' ')
        self.assert_rejected()

    def test_answer_byte_tamper_is_rejected(self):
        self.answers.write_bytes(self.answers.read_bytes() + b'\n')
        self.assert_rejected()

    def test_instructions_require_exact_original_bytes(self):
        self.instructions.write_bytes(self.instructions.read_bytes() + b'\n')
        self.assert_rejected()

    def test_summary_counts_status_finished_usage_calls_are_strict(self):
        cases = [('status', 'completed'), ('finished_at', '2026-10-12T02:01:00+00:00'),
                 ('started_at', '2026-10-12T00:59:00+00:00'), ('planned_answers', True),
                 ('written_answers', 19), ('unattempted_answers', 1), ('calls', 19),
                 ('answer_status_counts', {'completed': 20}), ('error', {'code': 'hidden_failure'}),
                 ('usage', {'prompt_tokens': 200, 'completion_tokens': 1, 'total_tokens': 201})]
        original = deepcopy(self.summary_value)
        for key, value in cases:
            with self.subTest(key=key):
                self.summary_value = deepcopy(original)
                self.summary_value[key] = value
                self.repin()
                self.assert_rejected()
                self.summary_pin = sha(self.summary.read_bytes())
                self.assert_rejected()

    def test_summary_external_pin_cannot_be_refreshed_by_comparison_repin(self):
        self.summary_value['seconds'] = 21.0
        self.repin()
        self.assert_rejected()

    def test_summary_external_pin_is_required(self):
        for bad in (None, '', True, '0' * 64, self.summary_pin.upper()):
            with self.subTest(pin=bad):
                self.assert_rejected(expected_source_summary_sha256=bad)

    def test_arm_and_call_settings_are_strict_even_with_repin(self):
        original = deepcopy(self.manifest)
        cases = [('arms', preflight.ARM, 'temperature', False), ('arms', preflight.ARM, 'max_tokens', 4096),
                 ('arms', preflight.ARM, 'reasoning_effort', 'low'), ('arms', preflight.ARM, 'max_attempts', True),
                 ('arms', preflight.ARM, 'prompt_revision', 'new'), ('arms', preflight.ARM, 'suffix', 'new'),
                 ('config', None, 'maximum_calls_per_case', True), ('config', None, 'resume', True),
                 ('config', None, 'prepare_only', True), ('config', None, 'base_url', 'http://example.invalid/v1')]
        for section, arm, key, value in cases:
            with self.subTest(section=section, key=key):
                self.manifest = deepcopy(original)
                target = self.manifest[section] if arm is None else self.manifest[section][arm]
                target[key] = value
                self.repin()
                self.assert_rejected()

    def test_prepared_request_and_input_hash_tamper_is_rejected(self):
        for key in ('request_sha256', 'input_sha256', 'prompt_sha256'):
            with self.subTest(key=key):
                original = self.manifest['prepared_requests'][0][key]
                self.manifest['prepared_requests'][0][key] = '0' * 64
                self.repin()
                self.assert_rejected()
                self.manifest['prepared_requests'][0][key] = original

    def test_source_row_identity_and_attempt_hash_tamper_is_rejected(self):
        original = deepcopy(self.rows[0])
        cases = [('row', 'input_sha256', '0' * 64), ('row', 'request_sha256', '0' * 64),
                 ('row', 'prompt_sha256', '0' * 64), ('row', 'model_digest', '0' * 64),
                 ('row', 'instructions_byte_sha256', '0' * 64), ('row', 'instructions_version', 'other'),
                 ('row', 'instructions_name', 'alias'), ('row', 'fallback_used', True),
                 ('attempt', 'request_sha256', '0' * 64), ('attempt', 'input_sha256', '0' * 64),
                 ('attempt', 'reasoning_effort', 'low'), ('attempt', 'temperature', False),
                 ('attempt', 'max_tokens', 4096)]
        for scope, key, value in cases:
            with self.subTest(scope=scope, key=key):
                self.rows[0] = deepcopy(original)
                target = self.rows[0] if scope == 'row' else self.rows[0]['attempts'][0]
                target[key] = value
                self.repin()
                self.assert_rejected()

    def test_false_and_bool_selected_attempt_and_number_are_rejected(self):
        for value in (False, True, 0, 2, 1.0, '1'):
            with self.subTest(value=value):
                self.rows[0]['selected_attempt'] = value
                self.repin()
                self.assert_rejected()
        self.rows[0]['selected_attempt'] = 1
        for value in (False, True, 0, 2, 1.0, '1'):
            with self.subTest(number=value):
                self.rows[0]['attempts'][0]['number'] = value
                self.repin()
                self.assert_rejected()

    def test_hidden_success_or_additional_attempt_is_rejected(self):
        self.rows[3]['attempts'].append(deepcopy(self.rows[0]['attempts'][0]))
        self.repin()
        self.assert_rejected()

    def test_failure_requires_every_recorded_evidence_field(self):
        original = deepcopy(self.rows[3])
        cases = [('status', 'completed'), ('status', 'truncated'), ('finish_reason', 'stop'),
                 ('answer', 'fabricated success'), ('error', None),
                 ('error', {'code': 'request_failed', 'http_status': None}),
                 ('error', {'code': 'empty_answer', 'http_status': 500}),
                 ('usage', {'prompt_tokens': 10, 'completion_tokens': 2047, 'total_tokens': 2057})]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.rows[3] = deepcopy(original)
                self.rows[3][key] = deepcopy(value)
                self.rows[3]['attempts'][0][key] = deepcopy(value)
                self.repin()
                self.assert_rejected()
        self.rows[3] = deepcopy(original)
        for value in (False, 1, None):
            with self.subTest(response_received=value):
                self.rows[3]['attempts'][0]['response_received'] = value
                self.repin()
                self.assert_rejected()

    def test_completed_empty_and_completed_length_bypasses_are_rejected(self):
        original = deepcopy(self.rows[0])
        for key, value in (('answer', ''), ('answer', ' '), ('finish_reason', 'length'),
                           ('error', {'code': 'empty_answer', 'http_status': None})):
            with self.subTest(key=key):
                self.rows[0] = deepcopy(original)
                self.rows[0][key] = value
                self.rows[0]['attempts'][0][key] = value
                self.repin()
                self.assert_rejected()

    def test_missing_duplicate_reordered_and_holdout_rows_are_rejected(self):
        original = deepcopy(self.rows)
        variants = [original[:-1], original + [deepcopy(original[0])],
                    [original[1], original[0]] + original[2:]]
        heldout = deepcopy(original)
        heldout[-1]['prompt_id'] = 'HOLDOUT0020'
        variants.append(heldout)
        for index, rows in enumerate(variants):
            with self.subTest(variant=index):
                self.rows = deepcopy(rows)
                self.repin()
                self.assert_rejected()

    def test_pinned_full_row_hash_and_original_status_are_required(self):
        for key, value in (('full_row_sha256', '0' * 64), ('original_status', 'completed')):
            with self.subTest(key=key):
                self.repin()
                self.comparison_value['row_provenance'][7][key] = value
                self.repin(rebuild_rows=False)
                self.assert_rejected()

    def test_source_partial_cannot_be_relabelled_completed(self):
        self.manifest['status'] = 'completed'
        self.repin()
        self.assert_rejected()

    def test_digest_change_and_missing_digest_are_rejected(self):
        for value in ('0' * 64, None, ''):
            with self.subTest(value=value):
                self.manifest['model']['digest'] = value
                self.repin()
                self.assert_rejected()

    def test_dataset_byte_and_selection_tamper_are_rejected(self):
        self.items[0]['prompt'][1]['content'] = 'changed synthetic question'
        self.write(self.data, self.items)
        self.assert_rejected()

    def test_frozen_low_control_gate_cannot_be_relaxed(self):
        self.manifest['baseline']['development_control']['arm_config_sha256'] = '0' * 64
        self.comparison_value['sources']['baseline']['validated_development_control'] = deepcopy(self.manifest['baseline']['development_control'])
        self.repin()
        self.assert_rejected()

    def test_symlink_source_and_parent_aliases_are_rejected(self):
        alias = self.root / 'alias.json'
        alias.symlink_to(self.data)
        self.assert_rejected(data=alias)
        alias.unlink()
        alias = self.root / 'alias-directory'
        alias.symlink_to(self.source.parent)
        self.assert_rejected(source_manifest=alias / 'manifest.json')

    def test_hardlink_and_parent_traversal_aliases_are_rejected(self):
        alias = self.root / 'hardlink.json'
        os.link(self.data, alias)
        self.assert_rejected(data=alias)
        alias.unlink()
        self.assert_rejected(data=self.data.parent / '..' / self.data.parent.name / self.data.name)

    def test_output_is_private_fresh_and_never_overwritten(self):
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, 'fresh'):
            self.run_preflight()
        self.assertEqual(list(self.out.iterdir()), [])
        self.out.rmdir()
        result = self.run_preflight()
        before = (self.out / 'manifest.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'fresh'):
            self.run_preflight()
        self.assertEqual((self.out / 'manifest.json').read_bytes(), before)
        self.assertEqual(result['calls'], 0)

    def test_external_or_symlink_output_is_rejected(self):
        self.assert_rejected(out=self.root / 'public-output')
        alias = self.root / 'results' / 'alias'
        alias.symlink_to(self.source.parent)
        self.assert_rejected(out=alias / 'preflight')

    def test_concurrent_source_change_is_rejected_without_output(self):
        original = preflight.make_payload
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.summary.write_bytes(self.summary.read_bytes() + b' ')
            return result
        with patch.object(preflight, 'make_payload', side_effect=changed):
            self.assert_rejected()

    def test_source_change_after_manifest_write_rolls_back_output(self):
        original_verify = preflight._Snapshot.verify
        def change_after_write(snapshot):
            if snapshot.path == self.summary and self.out.exists():
                self.summary.write_bytes(self.summary.read_bytes() + b' ')
            original_verify(snapshot)
        with patch.object(preflight._Snapshot, 'verify', new=change_after_write):
            self.assert_rejected()

    def test_parent_symlink_swap_before_output_mkdir_never_writes_outside(self):
        parent = self.root / 'results' / 'publish-parent'
        parent.mkdir()
        outside = self.root / 'outside'
        outside.mkdir()
        target = parent / 'race-alias'
        original_mkdir = os.mkdir
        def swap(path, *args, **kwargs):
            if path == target.name and kwargs.get('dir_fd') is not None:
                parent.rename(self.root / 'saved-parent')
                parent.symlink_to(outside, target_is_directory=True)
            return original_mkdir(path, *args, **kwargs)
        with patch.object(os, 'mkdir', side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                self.run_preflight(out=target)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.root / 'saved-parent' / target.name).exists())

    def test_output_rename_before_open_preserves_other_writer_artifact(self):
        original_open = os.open
        moved = self.root / 'own-renamed-output'
        foreign = b'Other writer artifact'
        def swap(path, *args, **kwargs):
            if path == 'manifest.json' and kwargs.get('dir_fd') is not None:
                self.out.rename(moved)
                self.out.mkdir(mode=0o700)
                (self.out / 'manifest.json').write_bytes(foreign)
            return original_open(path, *args, **kwargs)
        with patch.object(os, 'open', side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                self.run_preflight()
        self.assertEqual((self.out / 'manifest.json').read_bytes(), foreign)
        self.assertTrue(moved.is_dir())
        self.assertFalse((moved / 'manifest.json').exists())

    def test_other_writer_manifest_before_exclusive_open_is_not_removed(self):
        original_open = os.open
        foreign = b'Other writer artifact'
        def inject(path, *args, **kwargs):
            if path == 'manifest.json' and kwargs.get('dir_fd') is not None:
                (self.out / 'manifest.json').write_bytes(foreign)
            return original_open(path, *args, **kwargs)
        with patch.object(os, 'open', side_effect=inject):
            with self.assertRaises(FileExistsError):
                self.run_preflight()
        self.assertEqual((self.out / 'manifest.json').read_bytes(), foreign)

    def test_output_swap_before_directory_fd_open_never_adopts_foreign_inode(self):
        original_open = os.open
        moved = self.root / 'own-renamed-output'
        def swap(path, flags, *args, **kwargs):
            if path == self.out.name and flags & os.O_DIRECTORY and kwargs.get('dir_fd') is not None:
                self.out.rename(moved)
                self.out.mkdir(mode=0o700)
            return original_open(path, flags, *args, **kwargs)
        with patch.object(os, 'open', side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                self.run_preflight()
        self.assertTrue(self.out.is_dir())
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertTrue(moved.is_dir())
        self.assertEqual(list(moved.iterdir()), [])

    def test_nonprivate_replacement_before_first_stat_is_not_adopted_or_chmodded(self):
        original_mkdir = os.mkdir
        moved = self.root / 'own-renamed-output'
        def swap(path, *args, **kwargs):
            result = original_mkdir(path, *args, **kwargs)
            if path == self.out.name and kwargs.get('dir_fd') is not None:
                self.out.rename(moved)
                original_mkdir(self.out, mode=0o755)
            return result
        with patch.object(os, 'mkdir', side_effect=swap):
            with self.assertRaises((ValueError, OSError)):
                self.run_preflight()
        self.assertTrue(self.out.is_dir())
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o755)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_malformed_nested_manifest_fields_are_rejected_without_output(self):
        original_manifest = deepcopy(self.manifest)
        original_comparison = deepcopy(self.comparison_value)
        cases = [('comparison', 'sources', []), ('source', 'instructions', []),
                 ('source', 'model', []), ('source', 'dataset', []), ('source', 'split', []),
                 ('source', 'prepared_requests', []), ('source', 'baseline', [])]
        for owner, key, value in cases:
            with self.subTest(owner=owner, key=key):
                self.manifest = deepcopy(original_manifest)
                self.comparison_value = deepcopy(original_comparison)
                self.repin()
                if owner == 'source':
                    self.manifest[key] = value
                    self.write(self.source, self.manifest)
                    self.comparison_value['sources']['candidate']['manifest_byte_sha256'] = sha(self.source.read_bytes())
                else:
                    self.comparison_value[key] = value
                self.write(self.comparison, self.comparison_value)
                self.pin = sha(self.comparison.read_bytes())
                self.assert_rejected()

    def test_usage_and_attempt_seconds_reject_bool_nonfinite_or_negative_values(self):
        original = deepcopy(self.rows[0])
        for value in (True, -1, float('nan'), float('inf')):
            with self.subTest(seconds=value):
                self.rows[0] = deepcopy(original)
                self.rows[0]['seconds'] = value
                self.rows[0]['attempts'][0]['seconds'] = value
                self.repin()
                self.assert_rejected()
        self.rows[0] = deepcopy(original)
        self.rows[0]['usage']['prompt_tokens'] = True
        self.rows[0]['attempts'][0]['usage']['prompt_tokens'] = True
        self.repin()
        self.assert_rejected()

    def test_summary_seconds_validate_even_with_new_explicit_summary_pin(self):
        for value in (True, -1, float('nan'), float('inf')):
            with self.subTest(seconds=value):
                self.summary_value['seconds'] = value
                self.repin()
                self.summary_pin = sha(self.summary.read_bytes())
                self.assert_rejected()

    def test_source_instruction_path_must_be_the_original_relative_regular_path(self):
        for value in ([], str(self.instructions), 'benchmarks/../benchmarks/prompts/grounded_v2_frozen.txt'):
            with self.subTest(path=value):
                self.manifest['instructions']['path'] = value
                self.repin()
                self.assert_rejected()

    def test_comparison_pin_failure_does_not_open_other_sources(self):
        opened = []
        original_init = preflight._Snapshot.__init__
        def recorded(snapshot, path):
            opened.append(path)
            original_init(snapshot, path)
        with patch.object(preflight._Snapshot, '__init__', new=recorded):
            self.assert_rejected(expected_comparison_sha256='0' * 64)
        self.assertEqual(opened, [self.comparison])

    def test_duplicate_json_keys_and_nonfinite_constants_are_rejected(self):
        self.source.write_bytes(b'{"status":"partial","status":"completed"}')
        self.comparison_value['sources']['candidate']['manifest_byte_sha256'] = sha(self.source.read_bytes())
        self.write(self.comparison, self.comparison_value)
        self.pin = sha(self.comparison.read_bytes())
        self.assert_rejected()
        self.source.write_bytes(b'{"status":"partial","number":NaN}')
        self.comparison_value['sources']['candidate']['manifest_byte_sha256'] = sha(self.source.read_bytes())
        self.write(self.comparison, self.comparison_value)
        self.pin = sha(self.comparison.read_bytes())
        self.assert_rejected()

    def test_cli_has_no_live_mode_and_requires_external_pin(self):
        args = ['--data', str(self.data), '--source-manifest', str(self.source),
                '--source-comparison-manifest', str(self.comparison), '--instructions-file', str(self.instructions),
                '--out', str(self.out)]
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            preflight.main(args)
        self.assertFalse(self.out.exists())
        with patch('builtins.print'):
            self.assertEqual(preflight.main(args + ['--expected-comparison-sha256', self.pin,
                                                   '--expected-source-summary-sha256', self.summary_pin]), 0)


if __name__ == '__main__':
    unittest.main()
