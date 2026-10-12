"""Synthetic budget generation and integrity tests; no HTTP, Git or real data."""

from copy import deepcopy
import json
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from benchmarks import healthbench_budget_cycle as budget
from benchmarks import healthbench_review as review
from benchmarks.healthbench import DEFAULT_MODEL, RequestFailure, canonical_hash
from tests import test_healthbench_budget_preflight as fixtures


def response(answer='Synthetic new answer', finish='stop', completion=20):
    return {'choices': [{'message': {'content': answer, 'reasoning': 'HIDDEN_REASONING_SENTINEL'},
                         'finish_reason': finish}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': completion, 'total_tokens': 10 + completion}}


class FakeTransport:
    def __init__(self, digest, responses=None, on_metadata=None, on_complete=None):
        self.digest = digest
        self.responses = list(responses if responses is not None else [response()] * 20)
        self.payloads, self.metadata_calls = [], 0
        self.on_metadata, self.on_complete = on_metadata, on_complete
        self.metadata_result = None

    def model_metadata(self, model):
        self.metadata_calls += 1
        if self.on_metadata:
            self.on_metadata(self)
        if isinstance(self.metadata_result, Exception):
            raise self.metadata_result
        if self.metadata_result is not None:
            return self.metadata_result
        return {'name': model, 'digest': self.digest, 'metadata_status': 'available'}

    def complete(self, payload):
        self.payloads.append(deepcopy(payload))
        if self.on_complete:
            self.on_complete(self)
        result = self.responses[len(self.payloads) - 1]
        if isinstance(result, BaseException):
            raise result
        return deepcopy(result)


class BudgetCycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.BudgetPreflightTests(methodName='test_valid_partial_control_prepares_twenty_hashes_and_zero_calls')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        review_root = patch.object(review, 'ROOT', self.fx.root)
        review_root.start()
        self.addCleanup(review_root.stop)
        network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No actual HTTP'))
        network.start()
        self.addCleanup(network.stop)

    def run_cycle(self, transport=None, **overrides):
        if transport is None:
            transport = FakeTransport(self.fx.digest)
        args = {'data': self.fx.data, 'source_manifest': self.fx.source,
                'source_comparison_manifest': self.fx.comparison, 'instructions_file': self.fx.instructions,
                'out': self.fx.out, 'expected_comparison_sha256': self.fx.pin,
                'expected_source_summary_sha256': self.fx.summary_pin, 'transport': transport}
        args.update(overrides)
        return budget.run_budget_cycle(**args)

    def read(self, relative):
        return json.loads((self.fx.out / relative).read_bytes())

    def rows(self, relative='candidate/answers.jsonl'):
        return [json.loads(line) for line in (self.fx.out / relative).read_text().splitlines() if line.strip()]

    def assert_invalid(self, transport, **kwargs):
        with self.assertRaises((ValueError, OSError)):
            self.run_cycle(transport, **kwargs)
        self.assertEqual(transport.payloads, [])
        self.assertEqual(transport.metadata_calls, 0)
        self.assertFalse(self.fx.out.exists())

    def test_prepare_only_zero_calls_ready_runner_and_unchanged_legacy_proof(self):
        transport = FakeTransport(self.fx.digest)
        summary, code = self.run_cycle(transport, prepare_only=True)
        self.assertEqual((summary['status'], summary['calls'], code), ('prepared', 0, 0))
        self.assertEqual((transport.metadata_calls, transport.payloads), (0, []))
        self.assertEqual(summary['unattempted_answers'], 20)
        self.assertTrue(self.read('manifest.json')['execution_support']['ready'])
        self.assertFalse(self.read('manifest.json')['source_preflight']['execution_support']['ready'])
        self.assertEqual(self.read('candidate/manifest.json')['config']['max_tokens'], 4096)
        self.assertEqual(self.read('comparison/manifest.json')['status'], 'prepared')
        self.assertEqual(self.read('comparison/manifest.json')['written_answers'], 20)
        self.assertEqual(len(self.rows('comparison/answers.jsonl')), 20)
        self.assertFalse((self.fx.out / 'comparison/review').exists())

    def test_twenty_single_4096_calls_preserve_control_and_prepare_twenty_blinded_pairs(self):
        paths = (self.fx.data, self.fx.source, self.fx.summary, self.fx.answers, self.fx.comparison, self.fx.instructions)
        before = {path: path.read_bytes() for path in paths}
        transport = FakeTransport(self.fx.digest)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('completed', 20, 0))
        self.assertEqual(summary['unattempted_answers'], 0)
        self.assertEqual(summary['source_status_counts'], {'completed': 17, 'failed': 3})
        self.assertEqual(summary['comparison_status'], 'partial')
        self.assertEqual(summary['review']['ready_packet_count'], 20)
        self.assertEqual(summary['review']['grading_status'], 'not_graded')
        self.assertIsNone(summary['score'])
        self.assertEqual(summary['model_metadata_calls'], transport.metadata_calls)
        self.assertGreater(transport.metadata_calls, 20)
        candidate_rows = self.rows()
        for expected, payload, row in zip(self.fx.payloads, transport.payloads, candidate_rows):
            self.assertEqual(payload, expected | {'max_tokens': 4096})
            self.assertEqual(row['arm'], budget.CANDIDATE_ARM)
            self.assertEqual(row['attempts'][0]['max_tokens'], 4096)
            self.assertEqual(row['selected_attempt'], 1)
            self.assertEqual(len(row['attempts']), 1)
            self.assertFalse(row['fallback_used'])
            self.assertEqual(row['request_sha256'], canonical_hash(payload))
            self.assertEqual(row['input_sha256'], canonical_hash(expected['messages']))
            self.assertTrue(row['model_identity_verified'])
            self.assertEqual((row['digest_before'], row['digest_after']), (self.fx.digest, self.fx.digest))
        combined = self.rows('comparison/answers.jsonl')
        controls = [row for row in combined if row['arm'] == budget.gate.ARM]
        self.assertEqual(controls, self.fx.rows)
        self.assertEqual((self.fx.out / 'comparison/control_answers.jsonl').read_bytes(), before[self.fx.answers])
        manifest = self.read('comparison/manifest.json')
        self.assertEqual(manifest['status'], 'partial')
        self.assertEqual(manifest['written_answers'], 40)
        self.assertEqual(manifest['original_control_manifest_status'], 'partial')
        self.assertEqual(manifest['sources']['baseline']['original_manifest_status'], 'partial')
        self.assertEqual(manifest['sources']['baseline']['manifest_path'], str(self.fx.source))
        self.assertEqual(manifest['sources']['baseline']['answers_path'], str(self.fx.answers))
        self.assertEqual(manifest['sources']['baseline']['manifest_byte_sha256'], fixtures.sha(before[self.fx.source]))
        self.assertEqual(manifest['sources']['baseline']['answers_byte_sha256'], fixtures.sha(before[self.fx.answers]))
        self.assertEqual(manifest['sources']['candidate']['original_manifest_status'], 'completed')
        self.assertEqual(manifest['sources']['candidate']['manifest_byte_sha256'], fixtures.sha((self.fx.out / 'candidate/manifest.json').read_bytes()))
        self.assertEqual(manifest['sources']['candidate']['answers_byte_sha256'], fixtures.sha((self.fx.out / 'candidate/answers.jsonl').read_bytes()))
        self.assertEqual(summary['source_usage'], self.fx.summary_value['usage'])
        for row, proof in zip(combined, manifest['row_provenance']):
            self.assertEqual(canonical_hash(row), proof['full_row_sha256'])
            self.assertEqual(row['status'], proof['original_status'])
            self.assertEqual(proof['source'], 'baseline' if row['arm'] == budget.gate.ARM else 'candidate')
        private = self.read('comparison/review/private_manifest.json')
        self.assertTrue(private['include_known_inference_failures'])
        self.assertEqual(private['paired_arms'], list(budget.PAIRED_ARMS))
        self.assertEqual(len(private['packets']), 20)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertEqual(stat.S_IMODE(self.fx.out.stat().st_mode), 0o700)
        for path in self.fx.out.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        serialized = json.dumps(transport.payloads)
        for forbidden in ('CRITERION_SENTINEL', 'REFERENCE_SENTINEL', 'HIDDEN_REASONING_SENTINEL'):
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, (self.fx.out / 'candidate/answers.jsonl').read_text())

    def test_synthetic_holdout_and_evaluation_fields_never_enter_generation(self):
        holdout = deepcopy(self.fx.items[0])
        holdout['prompt_id'] = 'ZZ-HOLDOUT'
        holdout['prompt'][1]['content'] = 'HOLDOUT_SECRET_SENTINEL'
        self.fx.items[0].update(canary='CANARY_SECRET_SENTINEL', review='REVIEW_SECRET_SENTINEL')
        self.fx.write(self.fx.data, self.fx.items + [holdout])
        dataset_sha = fixtures.sha(self.fx.data.read_bytes())
        self.fx.manifest['dataset'].update(sha256=dataset_sha, total_records=21)
        self.fx.manifest['baseline']['dataset_sha256'] = dataset_sha
        self.fx.manifest['baseline']['selected_prompt_ids_sha256'] = canonical_hash(self.fx.ids + ['ZZ-HOLDOUT'])
        for row in self.fx.rows:
            row['dataset_sha256'] = dataset_sha
        self.fx.comparison_value['dataset'] = deepcopy(self.fx.manifest['dataset'])
        self.fx.repin()
        transport = FakeTransport(self.fx.digest)
        summary, _ = self.run_cycle(transport)
        self.assertEqual(summary['calls'], 20)
        for secret in ('HOLDOUT_SECRET_SENTINEL', 'CANARY_SECRET_SENTINEL', 'REVIEW_SECRET_SENTINEL', 'CRITERION_SENTINEL'):
            self.assertNotIn(secret, json.dumps(transport.payloads))

    def test_empty_4096_and_nonempty_length_keep_separate_partial_evidence(self):
        responses = [response()] * 20
        responses[0] = response('', 'length', 4096)
        responses[1] = response('Partial synthetic answer', 'length', 4096)
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, responses))
        self.assertEqual((summary['status'], summary['calls'], code), ('partial', 20, 1))
        self.assertEqual(summary['answer_status_counts'], {'failed': 1, 'truncated': 1, 'completed': 18})
        self.assertEqual(summary['empty_final_answer_count'], 1)
        self.assertEqual(summary['review']['ready_packet_count'], 20)
        rows = self.rows()
        self.assertEqual(rows[0]['error']['code'], 'empty_answer')
        self.assertEqual(rows[0]['usage']['completion_tokens'], 4096)
        self.assertEqual(rows[1]['status'], 'truncated')
        self.assertEqual(self.read('comparison/manifest.json')['control_status_counts'], {'completed': 17, 'failed': 3})

    def test_transport_failure_stops_immediately_without_retry_or_failure_optin(self):
        transport = FakeTransport(self.fx.digest, [response(), response(), RequestFailure('connection_error')] + [response()] * 17)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], summary['unattempted_answers'], code), ('blocked', 3, 17, 2))
        self.assertEqual(summary['answer_status_counts'], {'completed': 2, 'blocked': 1})
        self.assertEqual(summary['response_not_received_count'], 1)
        self.assertEqual(self.read('comparison/manifest.json')['status'], 'blocked')
        self.assertEqual(self.read('comparison/manifest.json')['written_answers'], 23)
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertFalse((self.fx.out / 'comparison/review').exists())

    def test_interrupt_preserves_attempt_and_unattempted_cases(self):
        transport = FakeTransport(self.fx.digest, [response(), KeyboardInterrupt()] + [response()] * 18)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('interrupted', 2, 130))
        self.assertEqual(summary['unattempted_answers'], 18)
        self.assertEqual(self.rows()[1]['error']['code'], 'interrupted')
        self.assertFalse(self.rows()[1]['model_identity_verified'])

    def test_missing_or_changed_initial_digest_blocks_zero_inference(self):
        for digest in (None, '', '0' * 64):
            with self.subTest(digest=digest):
                transport = FakeTransport(digest)
                self.fx.out = self.fx.root / 'results' / ('digest-' + str(digest))
                summary, code = self.run_cycle(transport)
                self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 0, 2))
                self.assertEqual(transport.payloads, [])
                self.assertEqual(summary['unattempted_answers'], 20)

    def test_metadata_exception_and_provider_exception_details_are_sanitized(self):
        transport = FakeTransport(self.fx.digest)
        transport.metadata_result = RuntimeError('API_SECRET_SENTINEL ORIGINAL_QUESTION_SENTINEL')
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['calls'], code), (0, 2))
        self.assertEqual(summary['error']['code'], 'model_metadata_unavailable')
        for path in self.fx.out.rglob('*.json'):
            self.assertNotIn('API_SECRET_SENTINEL', path.read_text())

    def test_provider_transport_code_cannot_leak_keys(self):
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, [RequestFailure('API_SECRET_SENTINEL')] * 20))
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
        self.assertEqual(self.rows()[0]['error']['code'], 'transport_failure')
        self.assertNotIn('API_SECRET_SENTINEL', json.dumps(summary))

    def test_model_change_after_received_answer_preserves_answer_and_marks_unverified(self):
        def change(transport):
            if len(transport.payloads) == 1:
                transport.digest = '0' * 64
        transport = FakeTransport(self.fx.digest, on_complete=change)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
        row = self.rows()[0]
        self.assertEqual(row['answer'], 'Synthetic new answer')
        self.assertEqual(row['status'], 'completed')
        self.assertFalse(row['model_identity_verified'])
        self.assertEqual((row['digest_before'], row['digest_after']), (self.fx.digest, '0' * 64))
        self.assertEqual(row['model_digest_status'], 'expected_source_pin')
        self.assertEqual(self.read('comparison/manifest.json')['status'], 'blocked')

    def test_source_change_during_metadata_blocks_before_first_generation(self):
        def change(transport):
            if transport.metadata_calls == 1:
                self.fx.instructions.write_bytes(self.fx.instructions.read_bytes() + b' ')
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_metadata=change))
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 0, 2))
        self.assertEqual(summary['error']['code'], 'source_changed')

    def test_source_change_after_first_call_uses_original_cached_snapshot(self):
        original_answers = self.fx.answers.read_bytes()
        original_source_sha = fixtures.sha(self.fx.source.read_bytes())
        def change(transport):
            if len(transport.payloads) == 1:
                self.fx.answers.write_bytes(b'CHANGED_EXTERNAL_SOURCE')
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_complete=change))
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
        self.assertEqual((self.fx.out / 'comparison/control_answers.jsonl').read_bytes(), original_answers)
        self.assertEqual(self.read('comparison/manifest.json')['source_byte_hashes']['source_manifest'], original_source_sha)
        self.assertFalse(self.read('comparison/manifest.json')['sources']['baseline']['current_source_matching'])
        self.assertTrue(self.read('comparison/manifest.json')['sources']['baseline']['source_changed_after_validation'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['status'], 'completed')
        self.assertFalse(self.rows()[0]['model_identity_verified'])
        self.assertEqual(self.fx.answers.read_bytes(), b'CHANGED_EXTERNAL_SOURCE')

    def test_source_change_after_twentieth_call_never_claims_ready_twenty_pairs(self):
        def change(transport):
            if len(transport.payloads) == 20:
                self.fx.source.write_bytes(self.fx.source.read_bytes() + b' ')
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_complete=change))
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 20, 2))
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertEqual(self.read('comparison/manifest.json')['status'], 'blocked')

    def test_source_pin_prepared_hash_config_summary_and_attempt_tamper_block_zero_calls(self):
        manifest_original, rows_original = deepcopy(self.fx.manifest), deepcopy(self.fx.rows)
        cases = ('pin', 'summary', 'input', 'request', 'settings', 'failure_evidence', 'selected_attempt')
        for case in cases:
            with self.subTest(case=case):
                self.fx.manifest, self.fx.rows = deepcopy(manifest_original), deepcopy(rows_original)
                self.fx.summary_value['seconds'] = 20.0
                if case == 'summary':
                    self.fx.summary_value['seconds'] = 21.0
                elif case in ('input', 'request'):
                    self.fx.manifest['prepared_requests'][0][case + '_sha256'] = '0' * 64
                elif case == 'settings':
                    self.fx.manifest['arms'][budget.gate.ARM]['max_tokens'] = 4096
                elif case == 'failure_evidence':
                    self.fx.rows[3]['attempts'][0]['response_received'] = False
                elif case == 'selected_attempt':
                    self.fx.rows[0]['selected_attempt'] = True
                self.fx.repin()
                transport = FakeTransport(self.fx.digest)
                self.assert_invalid(transport, **({'expected_comparison_sha256': '0' * 64} if case == 'pin' else {}))

    def test_existing_output_never_resumes_or_overwrites(self):
        self.run_cycle(prepare_only=True)
        before = {path: path.read_bytes() for path in self.fx.out.rglob('*') if path.is_file()}
        transport = FakeTransport(self.fx.digest)
        with self.assertRaisesRegex(ValueError, 'fresh'):
            self.run_cycle(transport)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual((transport.payloads, transport.metadata_calls), ([], 0))

    def test_invalid_timeout_and_prepare_flag_are_rejected_before_calls(self):
        for invalid in (True, None, 0, -1, float('nan'), '300'):
            with self.subTest(timeout=invalid):
                self.assert_invalid(FakeTransport(self.fx.digest), timeout=invalid)
        self.assert_invalid(FakeTransport(self.fx.digest), prepare_only=1)

    def test_generated_comparison_answer_tamper_during_packetting_blocks_and_preserves_evidence(self):
        original = review.create_review_packets
        def tamper(*args, **kwargs):
            path = Path(args[1])
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            next(row for row in rows if row['arm'] == budget.CANDIDATE_ARM)['answer'] = 'SYNTHETIC_TAMPERED_ANSWER'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            return original(*args, **kwargs)
        with patch.object(review, 'create_review_packets', side_effect=tamper):
            summary, code = self.run_cycle()
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 20, 2))
        self.assertEqual(summary['error']['code'], 'run_artifact_changed')
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertIn('SYNTHETIC_TAMPERED_ANSWER', (self.fx.out / 'comparison/answers.jsonl').read_text())
        self.assertEqual(self.read('comparison/manifest.json')['status'], 'blocked')
        evidence = self.read('integrity_failure.json')['artifacts']
        self.assertTrue(any(entry['artifact'] in {'comparison/answers.jsonl', 'comparison/review/progress.json'} for entry in evidence))
        self.assertTrue(any(entry.get('expected_answers_byte_sha256') != entry.get('observed_answers_byte_sha256')
                            for entry in evidence if 'expected_answers_byte_sha256' in entry))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertFalse(summary['review']['review_eligible'])

    def test_transient_comparison_tamper_and_restore_cannot_detach_review_packets(self):
        original = review.create_review_packets
        def tamper_restore(*args, **kwargs):
            path = Path(args[1])
            saved = path.read_bytes()
            rows = [json.loads(line) for line in saved.decode().splitlines()]
            next(row for row in rows if row['arm'] == budget.CANDIDATE_ARM)['answer'] = 'SYNTHETIC_TRANSIENT_TAMPER'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            try:
                return original(*args, **kwargs)
            finally:
                path.write_bytes(saved)
        with patch.object(review, 'create_review_packets', side_effect=tamper_restore):
            summary, code = self.run_cycle()
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 20, 2))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertNotIn('SYNTHETIC_TRANSIENT_TAMPER', (self.fx.out / 'comparison/answers.jsonl').read_text())
        self.assertTrue(any(entry.get('stamp_matches') is False for entry in self.read('integrity_failure.json')['artifacts']))

    def test_late_model_drift_preserves_prepared_packet_count_and_current_runtime_state(self):
        def change(transport):
            if transport.metadata_calls == 43:
                transport.digest = '0' * 64
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_metadata=change))
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 20, 2))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertEqual(summary['review']['pending_count'], 0)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(summary['current_runtime_model']['metadata_status'], 'digest_mismatch')
        self.assertEqual(self.read('candidate/manifest.json')['current_runtime_model']['observed_digest'], '0' * 64)
        manifest = self.read('comparison/manifest.json')
        self.assertEqual(manifest['sources']['candidate']['original_manifest_status'], 'blocked')
        self.assertEqual(manifest['sources']['candidate']['manifest_byte_sha256'], fixtures.sha((self.fx.out / 'candidate/manifest.json').read_bytes()))

    def test_late_metadata_interrupt_finalizes_actual_twenty_calls_and_prepared_packets(self):
        def interrupt(transport):
            if transport.metadata_calls == 43:
                raise KeyboardInterrupt()
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_metadata=interrupt))
        self.assertEqual((summary['status'], summary['calls'], code), ('interrupted', 20, 130))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(self.read('candidate/manifest.json')['status'], 'interrupted')
        self.assertEqual(self.read('comparison/manifest.json')['status'], 'interrupted')
        self.assertEqual(self.read('manifest.json')['status'], 'interrupted')

    def test_generated_candidate_bytes_tamper_stops_remaining_calls(self):
        def tamper(transport):
            if transport.metadata_calls == 2:
                path = self.fx.out / 'candidate/answers.jsonl'
                path.write_bytes(path.read_bytes() + b' ')
        transport = FakeTransport(self.fx.digest, on_metadata=tamper)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
        self.assertEqual(len(transport.payloads), 1)
        self.assertTrue((self.fx.out / 'candidate/answers.jsonl').read_bytes().endswith(b' '))
        self.assertEqual(summary['error']['code'], 'run_artifact_changed')

    def test_cli_requires_both_external_pins_and_prepare_only_has_no_http(self):
        args = ['--data', str(self.fx.data), '--source-manifest', str(self.fx.source),
                '--source-comparison-manifest', str(self.fx.comparison), '--instructions-file', str(self.fx.instructions),
                '--out', str(self.fx.out), '--prepare-only']
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            budget.main(args)
        with patch('builtins.print'):
            self.assertEqual(budget.main(args + ['--expected-comparison-sha256', self.fx.pin,
                                                '--expected-source-summary-sha256', self.fx.summary_pin]), 0)


if __name__ == '__main__':
    unittest.main()
