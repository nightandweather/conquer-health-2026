"""Synthetic fixed placement execution, runtime-template and integrity tests."""

from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from benchmarks import healthbench_budget_cycle as budget
from benchmarks import healthbench_codex_cycle as cycle
from benchmarks import healthbench_placement_cycle as placement
from benchmarks import healthbench_review as review
from benchmarks.healthbench import DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, RequestFailure, canonical_hash
from tests import test_healthbench_budget_cycle as budget_tests
from tests import test_healthbench_budget_preflight as source_tests
from tests import test_healthbench_placement_preflight as placement_tests

TEMPLATE = 'SYNTHETIC_TEMPLATE\n{{ exact system mapping }}\n '
TEMPLATE_SHA = source_tests.sha(TEMPLATE.encode('utf-8'))


class FakeTransport(budget_tests.FakeTransport):
    def __init__(self, digest, responses=None, *, on_template=None, **kwargs):
        super().__init__(digest, responses, **kwargs)
        self.template_queries, self.template_requests = 0, []
        self.template_result, self.on_template = {'template': TEMPLATE}, on_template

    def request(self, method, url, body, *, authenticated):
        self.template_queries += 1
        self.template_requests.append((method, url, deepcopy(body), authenticated))
        if self.on_template:
            self.on_template(self)
        if isinstance(self.template_result, BaseException):
            raise self.template_result
        return deepcopy(self.template_result)


class ProductionTemplatePinTests(unittest.TestCase):
    def test_template_pin_is_fixed_production_snapshot(self):
        self.assertEqual(placement.EXPECTED_TEMPLATE_SHA256,
                         'fa6710a93d78da62641e192361344be7a8c0a1c3737f139cf89f20ce1626b99c')


class PlacementCycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = placement_tests.InstructionPlacementPreflightTests(
            methodName='test_valid_partial_control_prepares_twenty_hashes_and_zero_calls')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        for patched in (patch.object(review, 'ROOT', self.fx.root),
                        patch.object(placement, 'EXPECTED_TEMPLATE_SHA256', TEMPLATE_SHA),
                        patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No actual HTTP'))):
            patched.start()
            self.addCleanup(patched.stop)

    def arguments(self, transport=None, **overrides):
        args = {'data': self.fx.data, 'source_manifest': self.fx.source,
                'source_comparison_manifest': self.fx.comparison, 'instructions_file': self.fx.instructions,
                'out': self.fx.out, 'expected_comparison_sha256': self.fx.pin,
                'expected_source_summary_sha256': self.fx.summary_pin,
                'transport': transport if transport is not None else FakeTransport(self.fx.digest)}
        args.update(overrides)
        return args

    def run_cycle(self, transport=None, **overrides):
        return placement.run_placement_cycle(**self.arguments(transport, **overrides))

    read = budget_tests.BudgetCycleTests.read
    rows = budget_tests.BudgetCycleTests.rows
    assert_invalid = budget_tests.BudgetCycleTests.assert_invalid
    test_source_pin_prepared_hash_config_summary_and_attempt_tamper_block_zero_calls = (
        budget_tests.BudgetCycleTests.test_source_pin_prepared_hash_config_summary_and_attempt_tamper_block_zero_calls)
    test_existing_output_never_resumes_or_overwrites = budget_tests.BudgetCycleTests.test_existing_output_never_resumes_or_overwrites
    test_invalid_timeout_and_prepare_flag_are_rejected_before_calls = (
        budget_tests.BudgetCycleTests.test_invalid_timeout_and_prepare_flag_are_rejected_before_calls)

    def test_prepare_only_zero_tags_template_generation_or_transport_construction(self):
        transport = FakeTransport(self.fx.digest)
        with patch.object(budget, 'Transport', side_effect=AssertionError('No transport construction')):
            summary, code = self.run_cycle(transport, prepare_only=True)
        self.assertEqual((summary['status'], summary['calls'], summary['template_queries'], code), ('prepared', 0, 0, 0))
        self.assertEqual((transport.metadata_calls, transport.template_queries, transport.payloads), (0, 0, []))
        self.assertEqual(summary['current_runtime_template']['metadata_status'], 'not_requested')
        self.assertFalse(summary['current_runtime_template']['verified'])
        self.assertTrue(self.read('manifest.json')['execution_support']['ready'])
        self.assertEqual(self.read('candidate/manifest.json')['config']['max_tokens'], 2048)
        self.assertEqual(self.read('candidate/manifest.json')['config']['instruction_placement'], 'system_prefix')
        proof = self.read('manifest.json')['source_preflight']
        self.assertEqual(proof['changed_fields'], ['messages'])
        self.assertNotIn('candidate_max_tokens', proof)
        self.assertEqual(len(proof['row_provenance']), 20)
        self.assertEqual(self.read('comparison/manifest.json')['written_answers'], 20)
        self.assertFalse((self.fx.out / 'comparison/review').exists())

    def test_synthetic_holdout_and_evaluation_fields_never_enter_generation(self):
        holdout = deepcopy(self.fx.items[0])
        holdout['prompt_id'] = 'ZZ-HOLDOUT'
        holdout['prompt'][1]['content'] = 'HOLDOUT_SECRET_SENTINEL'
        self.fx.items[0].update(canary='CANARY_SECRET_SENTINEL', review='REVIEW_SECRET_SENTINEL')
        self.fx.write(self.fx.data, self.fx.items + [holdout])
        dataset_sha = source_tests.sha(self.fx.data.read_bytes())
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
        for secret in ('HOLDOUT_SECRET_SENTINEL', 'CANARY_SECRET_SENTINEL', 'REVIEW_SECRET_SENTINEL',
                       'CRITERION_SENTINEL', 'REFERENCE_SENTINEL', 'HIDDEN_REASONING_SENTINEL'):
            self.assertNotIn(secret, json.dumps(transport.payloads))
            self.assertNotIn(secret, (self.fx.out / 'candidate/answers.jsonl').read_text())

    def test_twenty_exact_system2048_calls_all_original_failures_and_blinded_pairs(self):
        paths = (self.fx.data, self.fx.source, self.fx.summary, self.fx.answers, self.fx.comparison, self.fx.instructions)
        before = {path: path.read_bytes() for path in paths}
        transport = FakeTransport(self.fx.digest)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('completed', 20, 0))
        self.assertEqual((summary['model_metadata_calls'], summary['template_queries']), (43, 43))
        self.assertEqual((transport.metadata_calls, transport.template_queries), (43, 43))
        self.assertTrue(summary['current_runtime_template']['verified'])
        self.assertEqual(summary['current_runtime_template']['observed_byte_sha256'], TEMPLATE_SHA)
        self.assertIn('cached source-time', summary['cached_source_runtime_template']['metadata_source'])
        self.assertEqual(summary['cached_source_runtime_template']['bound_source_model_digest'], self.fx.digest)
        block = '\n\n[' + GROUNDED_SUFFIX + ']'
        proof = self.read('manifest.json')['source_preflight']
        for item, old, payload, row, provenance in zip(self.fx.items, self.fx.payloads, transport.payloads,
                                                     self.rows(), proof['row_provenance']):
            expected = deepcopy(old)
            expected['messages'] = [{'role': 'system', 'content': block}, *deepcopy(item['prompt'])]
            self.assertEqual(payload, expected)
            self.assertEqual({key for key in old if old[key] != payload[key]}, {'messages'})
            self.assertEqual(row['arm'], placement.CANDIDATE_ARM)
            self.assertEqual(row['instruction_placement'], 'system_prefix')
            self.assertEqual(row['attempts'][0]['instruction_placement'], 'system_prefix')
            self.assertEqual(row['attempts'][0]['max_tokens'], 2048)
            self.assertEqual(row['request_sha256'], canonical_hash(payload))
            self.assertEqual(row['input_sha256'], canonical_hash(payload['messages']))
            self.assertEqual(provenance['inverse_control_request_sha256'], canonical_hash(old))
            self.assertEqual(provenance['control_request_sha256'], canonical_hash(old))
            self.assertEqual(provenance['treatment_request_sha256'], canonical_hash(payload))
            self.assertEqual(provenance['original_messages_sha256'], canonical_hash(item['prompt']))
            self.assertTrue(row['runtime_template_verified'])
            self.assertEqual((row['template_before_sha256'], row['template_after_sha256']), (TEMPLATE_SHA, TEMPLATE_SHA))
            self.assertTrue(row['model_identity_verified'])
            self.assertEqual(len(row['attempts']), 1)
            self.assertFalse(row['fallback_used'])
        self.assertEqual(transport.template_requests,
                         [('POST', DEFAULT_BASE_URL.removesuffix('/v1') + '/api/show', {'model': DEFAULT_MODEL}, False)] * 43)
        combined = self.rows('comparison/answers.jsonl')
        self.assertEqual([row for row in combined if row['arm'] == budget.gate.ARM], self.fx.rows)
        self.assertEqual((self.fx.out / 'comparison/control_answers.jsonl').read_bytes(), before[self.fx.answers])
        self.assertEqual(summary['source_status_counts'], {'completed': 17, 'failed': 3})
        self.assertEqual(summary['comparison_status'], 'partial')
        self.assertEqual(summary['review']['ready_packet_count'], 20)
        private = self.read('comparison/review/private_manifest.json')
        self.assertEqual(private['paired_arms'], list(placement.PAIRED_ARMS))
        self.assertTrue(private['include_known_inference_failures'])
        self.assertEqual(self.read('comparison/provenance.json')['changed_fields'], ['messages'])
        for row, provenance in zip(combined, self.read('comparison/manifest.json')['row_provenance']):
            self.assertEqual(provenance['full_row_sha256'], canonical_hash(row))
        self.assertIsNone(summary['score'])
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_twenty_empty2048_answers_are_partial_failures_with_null_quality_and_terminal_review(self):
        transport = FakeTransport(self.fx.digest, [budget_tests.response('', 'length', 2048)] * 20)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('partial', 20, 1))
        self.assertEqual(summary['answer_status_counts'], {'failed': 20})
        self.assertEqual(summary['review']['ready_packet_count'], 20)
        self.assertIsNone(summary['score'])
        for row in self.rows():
            self.assertEqual(row['status'], 'failed')
            self.assertEqual(row['error']['code'], 'empty_answer')
            self.assertEqual(row['attempts'][0]['max_tokens'], 2048)
            self.assertIsNone(row['score'])
        self.assertEqual(self.read('comparison/manifest.json')['control_status_counts'], {'completed': 17, 'failed': 3})

    def test_transport_failure_or_interruption_never_retries_or_prepares_incomplete_pairs(self):
        for failure, status, code in ((RequestFailure('connection_error'), 'blocked', 2),
                                      (KeyboardInterrupt(), 'interrupted', 130)):
            with self.subTest(status=status):
                self.fx.out = self.fx.root / 'results' / status
                transport = FakeTransport(self.fx.digest, [budget_tests.response(), failure] + [budget_tests.response()] * 18)
                summary, actual_code = self.run_cycle(transport)
                self.assertEqual((summary['status'], summary['calls'], actual_code), (status, 2, code))
                self.assertEqual(summary['unattempted_answers'], 18)
                self.assertEqual(summary['review']['ready_packet_count'], 0)
                self.assertFalse((self.fx.out / 'comparison/review').exists())

    def test_malformed_completion_preserves_one_attempt_without_raw_provider_fields(self):
        for index, (answer, finish) in enumerate((('Answer', {}), ('Answer', []),
                                                ('\ud800', 'stop'), ('Answer', 'PROVIDER_SECRET_SENTINEL'))):
            with self.subTest(index=index):
                self.fx.out = self.fx.root / 'results' / f'malformed-response-{index}'
                transport = FakeTransport(self.fx.digest, [budget_tests.response(answer, finish)])
                summary, code = self.run_cycle(transport)
                self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
                self.assertEqual(summary['unattempted_answers'], 19)
                row = self.rows()[0]
                self.assertEqual(row['error']['code'], 'invalid_provider_response')
                self.assertEqual(row['status'], 'blocked')
                self.assertEqual(len(row['attempts']), 1)
                self.assertEqual(summary['review']['ready_packet_count'], 0)
                self.assertNotIn('PROVIDER_SECRET_SENTINEL', (self.fx.out / 'candidate/answers.jsonl').read_text())

    def test_template_unavailable_malformed_or_exception_stops_zero_generation(self):
        for index, value in enumerate((None, {}, {'template': None}, {'template': 17}, {'template': ' \n'},
                                       {'template': '\ud800'},
                                       RuntimeError('TEMPLATE_SECRET_SENTINEL'))):
            with self.subTest(value=value):
                self.fx.out = self.fx.root / 'results' / f'template-{index}'
                transport = FakeTransport(self.fx.digest)
                transport.template_result = value
                summary, code = self.run_cycle(transport)
                self.assertEqual((summary['status'], summary['calls'], summary['template_queries'], code), ('blocked', 0, 1, 2))
                self.assertEqual(summary['model_metadata_calls'], 1)
                self.assertEqual(transport.payloads, [])
                self.assertEqual(summary['error']['code'], 'model_template_unavailable')
                self.assertFalse(summary['current_runtime_template']['verified'])
                self.assertNotIn('TEMPLATE_SECRET_SENTINEL', json.dumps(summary))

    def test_template_raw_bytes_are_not_stripped_normalized_or_trusted_from_provider_hash(self):
        for index, text in enumerate((TEMPLATE + '\n', TEMPLATE.strip(), TEMPLATE.replace('\n', '\r\n'))):
            with self.subTest(index=index):
                self.fx.out = self.fx.root / 'results' / f'raw-{index}'
                transport = FakeTransport(self.fx.digest)
                transport.template_result = {'template': text, 'template_sha256': TEMPLATE_SHA}
                summary, code = self.run_cycle(transport)
                self.assertEqual((summary['calls'], code), (0, 2))
                self.assertEqual(summary['error']['code'], 'model_template_changed')
                self.assertEqual(summary['current_runtime_template']['observed_byte_sha256'], source_tests.sha(text.encode()))

    def test_initial_digest_change_prevents_template_query_and_generation(self):
        transport = FakeTransport('0' * 64)
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['calls'], summary['model_metadata_calls'], summary['template_queries'], code), (0, 1, 0, 2))
        self.assertEqual(summary['error']['code'], 'model_digest_changed')

    def test_template_drift_after_first_or_twentieth_call_stops_and_preserves_rows(self):
        for count in (1, 20):
            with self.subTest(count=count):
                self.fx.out = self.fx.root / 'results' / f'drift-{count}'
                def changed(transport):
                    if len(transport.payloads) == count:
                        transport.template_result = {'template': 'changed template'}
                transport = FakeTransport(self.fx.digest, on_complete=changed)
                summary, code = self.run_cycle(transport)
                self.assertEqual((summary['status'], summary['calls'], code), ('blocked', count, 2))
                self.assertEqual(summary['error']['code'], 'model_template_changed')
                self.assertEqual(len(self.rows()), count)
                self.assertFalse(self.rows()[-1]['runtime_template_verified'])
                self.assertEqual(summary['review']['ready_packet_count'], 0)

    def test_late_template_drift_or_interrupt_invalidates_prepared_packets_and_tracks_actual_queries(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                self.fx.out = self.fx.root / 'results' / f'late-{interrupted}'
                def changed(transport):
                    if transport.template_queries == 43:
                        if interrupted:
                            raise KeyboardInterrupt()
                        transport.template_result = {'template': 'changed late template'}
                summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_template=changed))
                self.assertEqual((summary['status'], summary['calls'], code),
                                 ('interrupted' if interrupted else 'blocked', 20, 130 if interrupted else 2))
                self.assertEqual(summary['template_queries'], 43)
                self.assertEqual(summary['review']['prepared_packet_count'], 20)
                self.assertFalse(summary['review']['review_eligible'])
                self.assertEqual(summary['review']['ready_packet_count'], 0)
                self.assertFalse(summary['current_runtime_template']['verified'])

    def test_source_or_output_mutation_during_template_query_blocks_before_generation(self):
        for owner in ('source', 'output'):
            with self.subTest(owner=owner):
                self.fx.out = self.fx.root / 'results' / f'mutation-{owner}'
                def changed(transport):
                    if transport.template_queries == 1:
                        path = self.fx.instructions if owner == 'source' else self.fx.out / 'candidate/answers.jsonl'
                        path.write_bytes(path.read_bytes() + b' ')
                original = self.fx.instructions.read_bytes()
                summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_template=changed))
                self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 0, 2))
                self.assertEqual(summary['error']['code'], 'source_changed' if owner == 'source' else 'run_artifact_changed')
                self.fx.instructions.write_bytes(original)
                self.fx.repin()

    def test_source_change_after_answer_preserves_original_cached_failure_rows(self):
        original = self.fx.answers.read_bytes()
        def changed(transport):
            if len(transport.payloads) == 1:
                self.fx.answers.write_bytes(b'CHANGED_SOURCE_SENTINEL')
        summary, code = self.run_cycle(FakeTransport(self.fx.digest, on_complete=changed))
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 1, 2))
        self.assertEqual((self.fx.out / 'comparison/control_answers.jsonl').read_bytes(), original)
        self.assertEqual(summary['error']['code'], 'source_changed')
        self.assertFalse(self.read('comparison/manifest.json')['sources']['baseline']['current_source_matching'])

    def test_existing_system_anywhere_in_selected_twenty_is_rejected_before_metadata(self):
        originals = deepcopy(self.fx.items)
        for index in (0, 3, 19):
            with self.subTest(index=index):
                self.fx.items = deepcopy(originals)
                self.fx.items[index]['prompt'].insert(1, {'role': 'system', 'content': 'Original system'})
                self.fx._rebuild_prompt_provenance()
                transport = FakeTransport(self.fx.digest)
                self.assert_invalid(transport)
                self.assertEqual(transport.template_queries, 0)

    def test_unknown_experiments_and_arbitrary_payload_arm_or_template_options_reject(self):
        transport = FakeTransport(self.fx.digest)
        for value in (None, True, '', 'system', 'unknown', []):
            with self.subTest(value=value), patch.object(budget.gate, '_validate_budget_source',
                                                        side_effect=AssertionError('No source reads')):
                with self.assertRaises(ValueError):
                    budget._run_fixed_cycle(**self.arguments(transport), experiment=value)
        for override in ({'payloads': []}, {'candidate_arm': 'arbitrary'}, {'expected_template_sha256': TEMPLATE_SHA}):
            with self.subTest(override=override), self.assertRaises(TypeError):
                self.run_cycle(transport, **override)
        self.assertEqual((transport.payloads, transport.metadata_calls, transport.template_queries), ([], 0, 0))

    def test_actual_payload_hash_guard_stops_changed_placement_before_generation(self):
        original = cycle.make_payload
        def changed(*args, **kwargs):
            payload = original(*args, **kwargs)
            if kwargs.get('instruction_placement') == 'system_prefix':
                payload['messages'][0]['content'] += '\nchanged'
            return payload
        transport = FakeTransport(self.fx.digest)
        with patch.object(cycle, 'make_payload', side_effect=changed):
            summary, code = self.run_cycle(transport)
        self.assertEqual((summary['calls'], code), (0, 2))
        self.assertEqual(summary['error']['code'], 'candidate_request_changed')
        self.assertEqual(transport.payloads, [])

    def test_generated_row_or_attempt_tamper_blocks_remaining_calls(self):
        original = budget.generate_candidate
        for scope, key, value in (('attempt', 'max_tokens', 4096), ('attempt', 'instruction_placement', 'user_suffix'),
                                 ('row', 'instruction_placement', 'user_suffix'), ('attempt', 'request_sha256', '0' * 64),
                                 ('row', 'model', 'changed-model'), ('row', 'answer', 'changed-answer')):
            with self.subTest(scope=scope, key=key):
                self.fx.out = self.fx.root / 'results' / f'record-{scope}-{key}'
                def changed(*args, **kwargs):
                    row = original(*args, **kwargs)
                    (row if scope == 'row' else row['attempts'][0])[key] = value
                    return row
                transport = FakeTransport(self.fx.digest)
                with patch.object(budget, 'generate_candidate', side_effect=changed):
                    summary, code = self.run_cycle(transport)
                self.assertEqual((summary['calls'], code), (1, 2))
                self.assertEqual(summary['error']['code'], 'candidate_record_invalid')
                self.assertEqual(len(transport.payloads), 1)
                self.assertEqual(summary['review']['ready_packet_count'], 0)

    def test_transient_packet_source_tamper_and_restore_invalidates_review_with_stamps(self):
        original = review.create_review_packets
        def changed(*args, **kwargs):
            path = Path(args[1])
            saved = path.read_bytes()
            rows = [json.loads(line) for line in saved.decode().splitlines()]
            next(row for row in rows if row['arm'] == placement.CANDIDATE_ARM)['answer'] = 'TRANSIENT_TAMPER_SENTINEL'
            path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            try:
                return original(*args, **kwargs)
            finally:
                path.write_bytes(saved)
        with patch.object(review, 'create_review_packets', side_effect=changed):
            summary, code = self.run_cycle()
        self.assertEqual((summary['status'], summary['calls'], code), ('blocked', 20, 2))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertTrue(any(row.get('stamp_matches') is False for row in self.read('integrity_failure.json')['artifacts']))

    def test_cli_prepare_only_requires_pins_and_rejects_experiment_overrides(self):
        args = ['--data', str(self.fx.data), '--source-manifest', str(self.fx.source),
                '--source-comparison-manifest', str(self.fx.comparison), '--instructions-file', str(self.fx.instructions),
                '--out', str(self.fx.out), '--prepare-only']
        pins = ['--expected-comparison-sha256', self.fx.pin, '--expected-source-summary-sha256', self.fx.summary_pin]
        with self.assertRaises(SystemExit), patch('sys.stderr'):
            placement.main(args)
        for option in ('--model', '--base-url', '--max-tokens', '--instruction-placement', '--expected-template-sha256'):
            with self.subTest(option=option), self.assertRaises(SystemExit), patch('sys.stderr'):
                placement.main(args + pins + [option])
        with patch.object(budget, 'Transport', side_effect=AssertionError('No actual transport')), patch('builtins.print'):
            self.assertEqual(placement.main(args + pins), 0)


if __name__ == '__main__':
    unittest.main()
