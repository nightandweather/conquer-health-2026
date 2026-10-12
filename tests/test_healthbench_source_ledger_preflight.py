"""Literal source-index/inverse and fixed-profile tests using synthetic files only.

The fixed byte/code profile is patched only inside this fixture, never exposed
as an API/CLI override. No real dataset, cached answer or runtime is accessed.
"""

from copy import deepcopy
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import healthbench_budget_preflight as gate
from benchmarks import healthbench_source_ledger_preflight as ledger
from benchmarks.healthbench import DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, Transport, canonical_hash, sum_usage
from benchmarks.healthbench_codex_cycle import REVIEW_REQUIRED, make_payload


class SourceLedgerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for module in (gate, ledger):
            patched = patch.object(module, 'ROOT', self.root)
            patched.start()
            self.addCleanup(patched.stop)
        self.data = self.root / 'private_data' / 'synthetic.json'
        self.instructions = self.root / 'benchmarks' / 'prompts' / 'grounded_v2_frozen.txt'
        self.source = self.root / 'results' / 'placement' / 'candidate' / 'manifest.json'
        self.comparison = self.root / 'results' / 'placement' / 'comparison' / 'manifest.json'
        self.original = self.root / 'results' / 'original' / 'candidate' / 'manifest.json'
        self.original_comparison = self.original.parent.parent / 'comparison' / 'manifest.json'
        self.verification = self.root / 'results' / 'placement' / 'execution-preflight-verification.json'
        self.out = self.root / 'results' / 'source-index-proof'
        self.paths = {'data': self.data, 'instructions': self.instructions, 'source_manifest': self.source,
                      'source_answers': self.source.parent / 'answers.jsonl', 'source_summary': self.source.parent / 'summary.json',
                      'comparison_manifest': self.comparison, 'comparison_answers': self.comparison.parent / 'answers.jsonl',
                      'producer_verification': self.verification, 'data_sidecar': self.data.with_suffix('.manifest.json')}
        self.original_paths = {'source_manifest': self.original, 'source_answers': self.original.parent / 'answers.jsonl',
                               'source_summary': self.original.parent / 'summary.json', 'comparison_manifest': self.original_comparison}
        self.code_pins = {}
        for relative in ledger._PRODUCER_CODE_SHA256:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('SYNTHETIC_PRODUCER_' + relative)
            self.code_pins[relative] = gate._sha(path.read_bytes())
        for path in (*self.paths.values(), *self.original_paths.values()):
            path.parent.mkdir(parents=True, exist_ok=True)
        self.instructions.write_bytes(GROUNDED_SUFFIX.encode('utf-8'))
        self.items = []
        for number in range(200):
            if number >= 20:
                # IDs are structurally scanned; holdout prompts/rubrics must never be inspected.
                self.items.append({'prompt_id': f'HB-V{number:04d}', 'prompt': 'HOLDOUT_DO_NOT_PROCESS', 'rubrics': None})
                continue
            messages = []
            for turn in range((3, 3, 2, 2, 1)[number] if number < 5 else 0):
                messages.extend([{'role': 'user', 'content': f' USER_SENTINEL_{number}_{turn}: "없다" [quote] <tag>\n한글\t '},
                                 {'role': 'assistant', 'content': f'ASSISTANT_DO_NOT_COPY_{number}_{turn}'}])
            messages.append({'role': 'user', 'content': f'\nCURRENT_SENTINEL_{number}: not confirmed, "[source]" </tag>\t '})
            self.items.append({'prompt_id': f'HB-V{number:04d}', 'prompt': messages,
                               'rubrics': [{'criterion': 'RUBRIC_DO_NOT_COPY', 'points': 1}], 'reference': 'REFERENCE_DO_NOT_COPY'})
        self.rows, self.original_rows = [], []
        self.start, self.finish = '2026-10-12T05:10:06+00:00', '2026-10-12T05:12:55+00:00'
        self.manifest, self.comparison_value, self.summary = {}, {}, {}
        self.data_pins, self.original_pins = {}, {}
        for name, value in (('_SOURCE_BYTE_SHA256', self.data_pins), ('_UPSTREAM_BYTE_SHA256', self.original_pins),
                            ('_PRODUCER_CODE_SHA256', self.code_pins)):
            mocked = patch.object(ledger, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.rebuild_inputs()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    @staticmethod
    def write_rows(path, rows):
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))

    def make_row(self, item, number, placement):
        payload = make_payload(item, GROUNDED_SUFFIX, DEFAULT_MODEL, reasoning_effort='medium', max_tokens=2048,
                               instruction_placement='system_prefix' if placement else 'user_suffix')
        failed = number == 3 if placement else number in (3, 5, 13)
        truncated = placement and number == 16
        status = 'failed' if failed else 'truncated' if truncated else 'completed'
        usage = {'prompt_tokens': (393 if number == 19 else 380) if placement else 20,
                 'completion_tokens': 2048 if failed or truncated else (747 if number == 19 else 735) if placement else 20}
        usage['total_tokens'] = sum(usage.values())
        error = {'code': 'empty_answer', 'http_status': None} if failed else None
        attempt = {'number': 1, 'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium',
                   'request_sha256': canonical_hash(payload), 'input_sha256': canonical_hash(payload['messages']),
                   'answer': '' if failed else f'ANSWER_DO_NOT_COPY_{number}', 'status': status,
                   'finish_reason': 'length' if failed or truncated else 'stop', 'error': error,
                   'usage': usage, 'seconds': 1.0, 'response_received': True}
        arm = ledger.SOURCE_ARM if placement else ledger.UPSTREAM_ARM
        row = {'prompt_id': item['prompt_id'], 'arm': arm, 'model': DEFAULT_MODEL, 'model_digest': ledger.MODEL_DIGEST,
               'dataset_sha256': self.data_pins['data'], 'prompt_sha256': canonical_hash(item['prompt']),
               'instructions_byte_sha256': self.data_pins['instructions'], 'instructions_name': arm, 'instructions_version': ledger.REVISION,
               'attempts': [attempt], 'fallback_used': False, 'selected_attempt': 1, 'score': None, 'score_status': REVIEW_REQUIRED}
        row.update({key: deepcopy(attempt[key]) for key in ('request_sha256', 'input_sha256', 'answer', 'status', 'finish_reason', 'error', 'usage', 'seconds')})
        if placement:
            row.update(instruction_placement='system_prefix', digest_before=ledger.MODEL_DIGEST, digest_after=ledger.MODEL_DIGEST,
                       template_before_sha256=ledger.TEMPLATE_SHA256, template_after_sha256=ledger.TEMPLATE_SHA256,
                       model_identity_verified=True, runtime_template_verified=True, model_digest_status='expected_source_pin')
            attempt['instruction_placement'] = 'system_prefix'
        return row

    def rebuild_inputs(self):
        self.write(self.data, self.items)
        self.data_pins['data'] = gate._sha(self.data.read_bytes())
        self.data_pins['instructions'] = gate._sha(self.instructions.read_bytes())
        self.rows = [self.make_row(item, number, True) for number, item in enumerate(self.items[:20])]
        self.original_rows = [self.make_row(item, number, False) for number, item in enumerate(self.items[:20])]
        self.write(self.paths['data_sidecar'], {'converted_sha256': self.data_pins['data'], 'status': 'validated'})
        self.data_pins['data_sidecar'] = gate._sha(self.paths['data_sidecar'].read_bytes())
        metadata = {'path': str(self.data), 'sha256': self.data_pins['data'], 'total_records': 200,
                    'selected_prompt_ids': ledger.IDS,
                    'selected_prompt_sha256': {item['prompt_id']: canonical_hash(item['prompt']) for item in self.items[:20]},
                    'provenance': {'status': 'validated', 'converted_sha256': self.data_pins['data'],
                                   'sidecar': str(self.paths['data_sidecar']), 'sidecar_sha256': self.data_pins['data_sidecar']}}
        arm = {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium', 'max_attempts': 1,
               'suffix': GROUNDED_SUFFIX, 'prompt_revision': ledger.REVISION, 'instruction_placement': 'system_prefix'}
        runtime = {'observed_digest': ledger.MODEL_DIGEST, 'metadata_status': 'available', 'metadata_calls': 43}
        template = {'expected_byte_sha256': ledger.TEMPLATE_SHA256, 'observed_byte_sha256': ledger.TEMPLATE_SHA256,
                    'metadata_status': 'available', 'template_queries': 43, 'verified': True,
                    'query': 'POST /api/show; exact template UTF-8 bytes; metadata only'}
        self.manifest = {'schema_version': 1, 'status': 'partial', 'started_at': self.start, 'finished_at': self.finish,
                         'error': None, 'score': None, 'score_status': REVIEW_REQUIRED, 'dataset': metadata,
                         'selected_prompt_ids': ledger.IDS, 'selected_arms': [ledger.SOURCE_ARM],
                         'split': {'name': 'development', 'limit': 20, 'selection': 'first 20 frozen baseline IDs; heldout excluded'},
                         'arms': {ledger.SOURCE_ARM: arm},
                         'model': {'name': DEFAULT_MODEL, 'digest': ledger.MODEL_DIGEST, 'metadata_status': 'available', 'digest_status': 'expected_source_pin'},
                         'instructions': {'name': ledger.SOURCE_ARM, 'version': ledger.REVISION, 'byte_sha256': self.data_pins['instructions']},
                         'config': {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'timeout': 300, 'reasoning_effort': 'medium',
                                    'temperature': 0, 'max_tokens': 2048, 'maximum_calls_per_case': 1, 'resume': False,
                                    'prepare_only': False, 'instruction_placement': 'system_prefix'},
                         'current_runtime_model': runtime, 'current_runtime_template': template,
                         'prepared_requests': [{**{key: row[key] for key in ('prompt_id', 'prompt_sha256', 'input_sha256', 'request_sha256', 'dataset_sha256', 'model_digest',
                                                                           'instructions_byte_sha256', 'instructions_name', 'instructions_version')}, 'instruction_placement': 'system_prefix'} for row in self.rows]}
        self.summary = {'status': 'partial', 'started_at': self.start, 'finished_at': self.finish, 'seconds': 21.0, 'error': None,
                        'score': None, 'score_status': REVIEW_REQUIRED, 'selected_prompt_ids': ledger.IDS, 'selected_arms': [ledger.SOURCE_ARM],
                        'current_runtime_model': runtime, 'current_runtime_template': template, 'planned_answers': 20, 'written_answers': 20,
                        'unattempted_answers': 0, 'calls': 20, 'model_metadata_calls': 43, 'template_queries': 43,
                        'answer_status_counts': ledger.STATUS_COUNTS, 'finish_reason_counts': {'stop': 18, 'length': 2}, 'empty_final_answer_count': 1,
                        'response_not_received_count': 0, 'usage': ledger.SOURCE_USAGE, 'source_status_counts': {'completed': 17, 'failed': 3},
                        'source_known_failure_count': 3, 'source_recorded_calls': 20, 'comparison_status': 'partial'}
        self.comparison_value = {'schema_version': 1, 'status': 'partial', 'finished_at': self.finish, 'error': None, 'score': None,
                                 'score_status': REVIEW_REQUIRED, 'dataset': metadata, 'selected_prompt_ids': ledger.IDS,
                                 'selected_arms': [ledger.UPSTREAM_ARM, ledger.SOURCE_ARM],
                                 'arms': {ledger.SOURCE_ARM: arm, ledger.UPSTREAM_ARM: {k: v for k, v in arm.items() if k != 'instruction_placement'}},
                                 'current_runtime_model': runtime, 'current_runtime_template': template, 'planned_answers': 40, 'written_answers': 40,
                                 'original_control_manifest_status': 'partial', 'control_status_counts': {'completed': 17, 'failed': 3},
                                 'control_recorded_calls': 20, 'candidate_status': 'partial'}
        self.sync()

    def sync(self):
        # Repinning is fixture-local, allowing schema tests past the immutable raw-byte gate.
        self.write(self.original, {'synthetic': 'ORIGINAL_MANIFEST'})
        self.write_rows(self.original_paths['source_answers'], self.original_rows)
        self.write(self.original_paths['source_summary'], {'synthetic': 'ORIGINAL_SUMMARY'})
        self.write(self.original_comparison, {'synthetic': 'ORIGINAL_COMPARISON'})
        self.original_pins.update({key: gate._sha(path.read_bytes()) for key, path in self.original_paths.items()})
        cached_template = {'byte_sha256': ledger.TEMPLATE_SHA256, 'metadata_source': 'cached source-time runtime snapshot; no new control query',
                           'bound_source_model_digest': ledger.MODEL_DIGEST, 'bound_source_manifest_byte_sha256': self.original_pins['source_manifest'],
                           'plain_api_system_mapping': 'supported by this fixed pinned runtime template'}
        for value in (self.manifest, self.summary, self.comparison_value):
            value['cached_source_runtime_template'] = cached_template
        for value in (self.manifest, self.comparison_value):
            value['source_byte_hashes'] = dict(self.original_pins)
        usage = sum_usage([row['attempts'][0] for row in self.original_rows])
        self.summary['source_usage'] = usage
        self.comparison_value['control_usage'] = usage
        self.write(self.source, self.manifest)
        self.write_rows(self.paths['source_answers'], self.rows)
        self.write(self.paths['source_summary'], self.summary)
        self.data_pins.update({name: gate._sha(self.paths[name].read_bytes()) for name in ('source_manifest', 'source_answers', 'source_summary')})
        self.comparison_value['sources'] = {'baseline': {'manifest_path': str(self.original), 'answers_path': str(self.original_paths['source_answers']),
            'manifest_byte_sha256': self.original_pins['source_manifest'], 'answers_byte_sha256': self.original_pins['source_answers'],
            'original_manifest_status': 'partial', 'provenance_kind': 'externally pinned validated original snapshot',
            'source_changed_after_validation': False, 'current_source_matching': 'verified_at_last_check'},
            'candidate': {'manifest_path': str(self.source), 'answers_path': str(self.paths['source_answers']),
            'manifest_byte_sha256': self.data_pins['source_manifest'], 'answers_byte_sha256': self.data_pins['source_answers'],
            'expected_manifest_byte_sha256': self.data_pins['source_manifest'], 'expected_answers_byte_sha256': self.data_pins['source_answers'],
            'original_manifest_status': 'partial', 'recorded_run_status': 'partial', 'integrity_status': 'verified'}}
        self.comparison_value['row_provenance'] = [{'source': label, **{key: row[key] for key in ('prompt_id', 'arm', 'input_sha256', 'request_sha256')},
              'original_status': row['status'], 'full_row_sha256': canonical_hash(row)}
              for pair in zip(self.original_rows, self.rows) for label, row in zip(('baseline', 'candidate'), pair)]
        self.write(self.comparison, self.comparison_value)
        self.write_rows(self.paths['comparison_answers'], [row for pair in zip(self.original_rows, self.rows) for row in pair])
        self.write(self.verification, {'status': 'actual_runner_prepared_no_calls', 'calls': 0, 'model_metadata_queries': 0,
                   'template_queries': 0, 'source_status_counts': {'completed': 17, 'failed': 3}, 'role_runner_source_factor': ['messages'], 'code_sha256': self.code_pins})
        self.data_pins.update({name: gate._sha(path.read_bytes()) for name, path in self.paths.items()})

    def run_preflight(self, **overrides):
        args = {'data': self.data, 'source_manifest': self.source, 'source_comparison_manifest': self.comparison,
                'instructions_file': self.instructions, 'producer_verification': self.verification, 'out': self.out}
        args.update(overrides)
        with patch.object(Transport, '__init__', side_effect=AssertionError('no transport')) as constructor, \
                patch.object(Transport, 'request', side_effect=AssertionError('no HTTP')) as request, \
                patch.object(Transport, 'complete', side_effect=AssertionError('no generation')) as complete, \
                patch.object(Transport, 'model_metadata', side_effect=AssertionError('no metadata')) as metadata, \
                patch('urllib.request.urlopen', side_effect=AssertionError('no URL open')) as urlopen, \
                patch('urllib.request.build_opener', side_effect=AssertionError('no opener')) as opener, \
                patch.object(gate, '_validate_budget_source', side_effect=AssertionError('old gate must not be called')) as old_gate:
            result = ledger.prepare_source_ledger(**args)
            for mock in (constructor, request, complete, metadata, urlopen, opener, old_gate):
                mock.assert_not_called()
        return result

    def reject(self):
        with self.assertRaises((ValueError, OSError, UnicodeError)):
            self.run_preflight()
        self.assertFalse(self.out.exists())

    def test_all_twenty_requests_inverse_preserves_unicode_negation_quotes_and_roles(self):
        before = deepcopy(self.items)
        for item in self.items[:20]:
            control, candidate, index = ledger._ledger_requests(item, GROUNDED_SUFFIX)
            self.assertEqual(candidate['messages'][0], control['messages'][0])
            self.assertEqual(candidate['messages'][2:], item['prompt'])
            self.assertEqual(candidate['messages'][1]['role'], 'user')
            decoded = json.loads(candidate['messages'][1]['content'])
            self.assertEqual(decoded, index)
            for turn, (message, entry) in enumerate(zip(item['prompt'], decoded['source_messages']), 1):
                self.assertEqual(entry['role'], message['role'])
                self.assertEqual(entry['turn'], turn)
                if message['role'] == 'user':
                    self.assertEqual(entry['user_text'].encode('utf-8'), message['content'].encode('utf-8'))
                    self.assertEqual(set(entry), {'role', 'turn', 'user_text'})
                else:
                    self.assertEqual(set(entry), {'role', 'turn'})
                    self.assertNotIn(message['content'], candidate['messages'][1]['content'])
            inverse = deepcopy(candidate)
            del inverse['messages'][1]
            self.assertEqual(canonical_hash(inverse), canonical_hash(control))
            self.assertNotEqual(canonical_hash(candidate), canonical_hash(control))
            self.assertEqual({k: v for k, v in candidate.items() if k != 'messages'}, {k: v for k, v in control.items() if k != 'messages'})
        self.assertEqual(self.items, before)

    def test_prepares_only_hashes_and_preserves_failed_truncated_sources_without_http(self):
        originals = {path: path.read_bytes() for path in (*self.paths.values(), *self.original_paths.values())}
        result = self.run_preflight()
        self.assertEqual(result['source_status_counts'], {'completed': 18, 'failed': 1, 'truncated': 1})
        self.assertEqual(result['source_usage']['total_tokens'], 24951)
        self.assertEqual(result['original_role_counts'], {'user': 31, 'assistant': 11})
        self.assertEqual(result['original_multiturn_case_count'], 5)
        self.assertEqual(result['indexed_user_text_count'], 31)
        self.assertEqual(result['assistant_content_copies'], 0)
        self.assertEqual(result['selected_prompt_ids'], ledger.IDS)
        self.assertEqual(len(result['row_provenance']), 20)
        self.assertEqual(result['execution_support']['status'], 'NOT_READY')
        self.assertFalse(result['execution_support']['ready'])
        self.assertFalse(result['experiment']['source_text_is_verified_patient_fact'])
        self.assertFalse(result['experiment']['quality_improvement_proven'])
        for key in ('calls', 'generation_calls', 'network_calls', 'transports_created', 'model_metadata_calls', 'template_queries'):
            self.assertEqual(result[key], 0)
        serialized = (self.out / 'manifest.json').read_text()
        for sentinel in ('USER_SENTINEL', 'CURRENT_SENTINEL', 'ASSISTANT_DO_NOT_COPY', 'ANSWER_DO_NOT_COPY', 'RUBRIC_DO_NOT_COPY', 'REFERENCE_DO_NOT_COPY', 'HOLDOUT_DO_NOT_PROCESS', GROUNDED_SUFFIX):
            self.assertNotIn(sentinel, serialized)
        self.assertEqual(json.loads(serialized), result)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.out / 'manifest.json').stat().st_mode), 0o600)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)

    def test_raw_pins_cannot_be_overridden_by_callers(self):
        with self.assertRaises(TypeError):
            self.run_preflight(expected_source_sha256='0' * 64)
        with self.assertRaises(TypeError):
            self.run_preflight(code_root=self.root / 'pristine-alternate')
        self.assertFalse(self.out.exists())

    def test_every_fixed_file_and_producer_code_change_is_rejected_without_refresh(self):
        for path in (*self.paths.values(), *self.original_paths.values(), *(self.root / name for name in self.code_pins)):
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                path.write_bytes(raw + b' ')
                self.reject()
                path.write_bytes(raw)

    def test_source_manifest_semantic_changes_fail_even_under_synthetic_repin(self):
        changes = [('status', 'completed'), ('selected_prompt_ids', list(reversed(ledger.IDS))),
                   ('selected_arms', ['other']), ('split', {'name': 'heldout', 'limit': 20}),
                   ('model', {'name': DEFAULT_MODEL, 'digest': '0' * 64}), ('instructions', {'name': 'other'}),
                   ('config', {**self.manifest['config'], 'max_tokens': 4096}),
                   ('arms', {ledger.SOURCE_ARM: {**self.manifest['arms'][ledger.SOURCE_ARM], 'reasoning_effort': 'low'}})]
        original = deepcopy(self.manifest)
        for key, value in changes:
            with self.subTest(key=key):
                self.manifest = deepcopy(original)
                self.manifest[key] = value
                self.sync()
                self.reject()

    def test_summary_count_call_status_or_usage_tamper_rejected(self):
        original = deepcopy(self.summary)
        for key, value in [('calls', 19), ('written_answers', 19), ('unattempted_answers', 1), ('model_metadata_calls', 42),
                           ('template_queries', 42), ('usage', {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}),
                           ('answer_status_counts', {'completed': 19, 'failed': 1}), ('response_not_received_count', 1), ('empty_final_answer_count', 0)]:
            with self.subTest(key=key):
                self.summary = deepcopy(original)
                self.summary[key] = value
                self.sync()
                self.reject()

    def test_rows_require_one_actual_attempt_and_exact_settings_hashes_model_and_template(self):
        original = deepcopy(self.rows)
        changes = [('attempts', []), ('selected_attempt', True), ('fallback_used', True), ('input_sha256', '0' * 64),
                   ('request_sha256', '0' * 64), ('prompt_sha256', '0' * 64), ('model_digest', '0' * 64),
                   ('digest_before', '0' * 64), ('digest_after', '0' * 64), ('template_before_sha256', '0' * 64),
                   ('template_after_sha256', '0' * 64), ('runtime_template_verified', False), ('model_identity_verified', False),
                   ('instruction_placement', 'user_suffix'), ('instructions_name', 'other')]
        for key, value in changes:
            with self.subTest(key=key):
                self.rows = deepcopy(original)
                self.rows[0][key] = value
                self.sync()
                self.reject()
        for key, value in [('number', 2), ('response_received', False), ('max_tokens', 4096), ('temperature', 1),
                           ('reasoning_effort', 'low'), ('instruction_placement', 'user_suffix'), ('input_sha256', '0' * 64)]:
            with self.subTest(attempt_key=key):
                self.rows = deepcopy(original)
                self.rows[0]['attempts'][0][key] = value
                self.sync()
                self.reject()

    def test_failed_and_truncated_evidence_and_selected_response_cannot_be_changed(self):
        original = deepcopy(self.rows)
        changes = [(3, 'answer', 'nonempty'), (3, 'finish_reason', 'stop'), (3, 'error', None),
                   (16, 'answer', ''), (16, 'finish_reason', 'stop'), (16, 'error', {'code': 'invented'}),
                   (0, 'status', 'blocked'), (0, 'answer', ''), (0, 'score', 100)]
        for number, key, value in changes:
            with self.subTest(number=number, key=key):
                self.rows = deepcopy(original)
                self.rows[number][key] = value
                self.rows[number]['attempts'][0][key] = value
                self.sync()
                self.reject()
        self.rows = deepcopy(original)
        self.rows[0]['attempts'][0]['answer'] = 'DIFFERENT_SELECTED_RESPONSE'
        self.sync()
        self.reject()

    def test_comparison_row_order_provenance_or_source_pin_tamper_rejected(self):
        for kind in ('order', 'provenance', 'source_pin', 'count'):
            with self.subTest(kind=kind):
                self.sync()
                if kind == 'order':
                    rows = [row for pair in zip(self.original_rows, self.rows) for row in pair]
                    rows[0], rows[1] = rows[1], rows[0]
                    self.write_rows(self.paths['comparison_answers'], rows)
                    self.data_pins['comparison_answers'] = gate._sha(self.paths['comparison_answers'].read_bytes())
                else:
                    value = json.loads(self.comparison.read_text())
                    if kind == 'provenance': value['row_provenance'][0]['full_row_sha256'] = '0' * 64
                    if kind == 'source_pin': value['sources']['candidate']['answers_byte_sha256'] = '0' * 64
                    if kind == 'count': value['written_answers'] = 39
                    self.write(self.comparison, value)
                    self.data_pins['comparison_manifest'] = gate._sha(self.comparison.read_bytes())
                self.reject()

    def test_original_role_or_text_change_cannot_match_fixed_cached_requests(self):
        for role in ('system', 'assistant'):
            with self.subTest(role=role):
                changed = deepcopy(self.items)
                changed[5]['prompt'][0]['role'] = role
                self.write(self.data, changed)
                self.data_pins['data'] = gate._sha(self.data.read_bytes())
                self.reject()
        self.rebuild_inputs()
        self.items[5]['prompt'][0]['content'] += 'NEGATION_CHANGED'
        self.write(self.data, self.items)
        self.data_pins['data'] = gate._sha(self.data.read_bytes())
        self.reject()

    def test_index_rejects_existing_system_blank_or_nontext_messages(self):
        for message in ({'role': 'system', 'content': 'arbitrary'}, {'role': 'user', 'content': '  '},
                        {'role': 'user', 'content': None}, {'role': 'tool', 'content': 'arbitrary'},
                        {'role': [], 'content': 'arbitrary'}, {'role': {}, 'content': 'arbitrary'}):
            with self.subTest(message=message):
                item = {'prompt': [message, {'role': 'user', 'content': 'last'}]}
                with self.assertRaises(ValueError): ledger._ledger_requests(item, GROUNDED_SUFFIX)
        with self.assertRaises(ValueError): ledger._ledger_requests(self.items[0], GROUNDED_SUFFIX + ' ')

    def test_fresh_private_output_no_overwrite_alias_or_outside_directory(self):
        self.out.mkdir()
        self.reject_existing(self.out)
        self.out.rmdir()
        for outside in (self.root / 'public' / 'proof', self.root / 'results'):
            with self.assertRaises(ValueError): self.run_preflight(out=outside)
        alias = self.root / 'results' / 'alias'
        alias.symlink_to(self.source.parent, target_is_directory=True)
        with self.assertRaises(ValueError): self.run_preflight(source_manifest=alias / 'manifest.json')

    def reject_existing(self, path):
        sentinel = path / 'owned-by-other'
        sentinel.write_text('KEEP')
        with self.assertRaises(ValueError): self.run_preflight()
        self.assertEqual(sentinel.read_text(), 'KEEP')
        sentinel.unlink()

    def test_before_publication_source_change_refuses_and_writes_nothing(self):
        build = ledger._ledger_requests
        def change(item, frozen):
            result = build(item, frozen)
            self.instructions.write_bytes(self.instructions.read_bytes() + b' ')
            return result
        with patch.object(ledger, '_ledger_requests', side_effect=change), \
                patch.object(gate, '_publish_private', side_effect=AssertionError('must not publish')) as publish:
            self.reject()
            publish.assert_not_called()

    def test_during_and_after_publication_source_changes_remove_owned_artifact(self):
        fsync = os.fsync
        def change_during(fd):
            self.paths['source_answers'].write_bytes(self.paths['source_answers'].read_bytes() + b' ')
            return fsync(fd)
        with patch.object(gate.os, 'fsync', side_effect=change_during): self.reject()
        self.sync()
        verify = gate._Snapshot.verify
        def change_after(snapshot):
            if (self.out / 'manifest.json').exists() and snapshot.path == self.data:
                snapshot.path.write_bytes(snapshot.path.read_bytes() + b' ')
            return verify(snapshot)
        with patch.object(gate._Snapshot, 'verify', new=change_after): self.reject()

    def test_same_bytes_source_replacement_is_rejected_by_inode(self):
        build = ledger._ledger_requests
        changed = False
        def replace(item, frozen):
            nonlocal changed
            result = build(item, frozen)
            if not changed:
                replacement = self.data.with_suffix('.replacement')
                replacement.write_bytes(self.data.read_bytes())
                replacement.replace(self.data)
                changed = True
            return result
        with patch.object(ledger, '_ledger_requests', side_effect=replace): self.reject()

    def test_cli_exposes_no_generation_or_pin_override(self):
        required = ['--data', str(self.data), '--source-manifest', str(self.source),
                    '--source-comparison-manifest', str(self.comparison), '--instructions-file', str(self.instructions),
                    '--producer-verification', str(self.verification), '--out', str(self.out)]
        for argument in ('--execute', '--expected-source-summary-sha256', '--expected-comparison-sha256', '--resume', '--code-root'):
            errors = io.StringIO()
            with self.subTest(argument=argument), redirect_stderr(errors), self.assertRaises(SystemExit):
                ledger.main([*required, argument])
            self.assertIn('unrecognized arguments', errors.getvalue())


if __name__ == '__main__':
    unittest.main()
