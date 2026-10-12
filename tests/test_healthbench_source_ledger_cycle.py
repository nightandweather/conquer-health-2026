"""Synthetic fixed source-index execution and stopped-run evidence, no HTTP."""
from copy import deepcopy
import io
import json
import os
from contextlib import redirect_stderr
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from benchmarks import healthbench_source_ledger_cycle as cycle
from benchmarks import healthbench_source_ledger_preflight as ledger
from benchmarks import healthbench_budget_cycle as budget
from benchmarks import healthbench_codex_cycle as legacy
from benchmarks import healthbench_review as review
from benchmarks.healthbench import DEFAULT_MODEL, GROUNDED_SUFFIX, RequestFailure, canonical_hash
from tests import test_healthbench_source_ledger_preflight as fixtures
from tests.test_healthbench_budget_cycle import response
from tests.test_healthbench_placement_cycle import FakeTransport, TEMPLATE, TEMPLATE_SHA


class SourceLedgerCycleTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.SourceLedgerPreflightTests(methodName='test_prepares_only_hashes_and_preserves_failed_truncated_sources_without_http')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        for mock in (patch.object(cycle, 'ROOT', self.fx.root), patch.object(review, 'ROOT', self.fx.root),
                     patch.object(ledger, 'TEMPLATE_SHA256', TEMPLATE_SHA),
                     patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network')),
                     patch.object(budget, '_run_fixed_cycle', side_effect=AssertionError('old 17+3 driver must not be used')),
                     patch.object(cycle.gate, '_validate_budget_source', side_effect=AssertionError('old source gate must not be used'))):
            mock.start()
            self.addCleanup(mock.stop)
        for relative in cycle.EXECUTION_CODE_PATHS:
            path = self.fx.root / relative
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('SYNTHETIC_EXECUTABLE_' + relative)
        patched = patch.object(cycle, 'EXPECTED_PREFLIGHT_SHA256', cycle.gate._sha((self.fx.root / 'benchmarks/healthbench_source_ledger_preflight.py').read_bytes()))
        patched.start()
        self.addCleanup(patched.stop)
        # Review packaging structurally validates the whole immutable container;
        # only selected dev20 messages/rubrics become packets or requests.
        for item in self.fx.items[20:]:
            item['prompt'] = [{'role':'user','content':'HOLDOUT_SENTINEL_MUST_NOT_ENTER_PACKET_OR_MODEL'}]
            item['rubrics'] = [{'criterion':'HOLDOUT_RUBRIC_MUST_NOT_BE_JUDGED','points':1}]
        self.fx.rebuild_inputs()
        sidecar = json.loads(self.fx.paths['data_sidecar'].read_bytes())
        sidecar.update(dataset_kind='synthetic_local_fixture', normalized_context=False,
                       lost_original_prompt_ids=False, canonical_score_comparable=False, count=200)
        self.fx.write(self.fx.paths['data_sidecar'], sidecar)
        self.fx.data_pins['data_sidecar'] = cycle.gate._sha(self.fx.paths['data_sidecar'].read_bytes())
        provenance = self.fx.manifest['dataset']['provenance']
        provenance.update(sidecar)
        provenance['sidecar_sha256'] = self.fx.data_pins['data_sidecar']
        self.fx.sync()

    def arguments(self, transport=None, **overrides):
        value = {'data': self.fx.data, 'source_manifest': self.fx.source, 'source_comparison_manifest': self.fx.comparison,
            'instructions_file': self.fx.instructions, 'producer_verification': self.fx.verification, 'out': self.fx.out,
            'transport': transport if transport is not None else FakeTransport(ledger.MODEL_DIGEST)}
        value.update(overrides)
        return value

    def run_cycle(self, transport=None, **overrides):
        return cycle.run_source_ledger_cycle(**self.arguments(transport, **overrides))

    def read(self, name):
        return json.loads((self.fx.out / name).read_bytes())

    def rows(self, name='candidate/answers.jsonl'):
        return [json.loads(line) for line in (self.fx.out / name).read_text().splitlines() if line.strip()]

    def test_fixed_payload_exact_extra_literal_message_no_assistant_promotion_and_legacy_bytes_unchanged(self):
        transport = FakeTransport(ledger.MODEL_DIGEST)
        before = [legacy.make_payload(item, GROUNDED_SUFFIX) for item in self.fx.items[:20]]
        summary, code = self.run_cycle(transport)
        self.assertEqual((summary['status'], summary['calls'], code), ('completed', 20, 0))
        self.assertEqual((summary['model_metadata_calls'], summary['template_queries']), (43, 43))
        self.assertEqual(transport.template_queries, 43)
        requests = self.read('candidate/manifest.json')['prepared_requests']
        rows = self.rows()
        for item, sent, request, row in zip(self.fx.items[:20], transport.payloads, requests, rows):
            control, expected, index = ledger._ledger_requests(item, GROUNDED_SUFFIX)
            self.assertEqual(sent, expected)
            self.assertEqual(sent['messages'][0], control['messages'][0])
            self.assertEqual(sent['messages'][2:], item['prompt'])
            self.assertEqual(json.loads(sent['messages'][1]['content']), index)
            for source in index['source_messages']:
                if source['role'] == 'assistant':
                    self.assertEqual(set(source), {'turn', 'role'})
            self.assertEqual(row['request_sha256'], canonical_hash(sent))
            self.assertEqual(row['input_sha256'], canonical_hash(sent['messages']))
            self.assertEqual(row['attempts'][0]['source_index_mode'], cycle.INDEX_MODE)
            self.assertEqual(row['attempts'][0]['source_index_sha256'], request['source_index_sha256'])
        self.assertEqual(before, [legacy.make_payload(item, GROUNDED_SUFFIX) for item in self.fx.items[:20]])
        self.assertEqual(summary['source_status_counts'], {'completed': 18, 'failed': 1, 'truncated': 1})
        self.assertEqual(summary['source_known_truncation_count'], 1)
        self.assertEqual(summary['review']['ready_packet_count'], 20)
        self.assertEqual(len(self.rows('comparison/answers.jsonl')), 40)
        self.assertEqual(self.read('comparison/manifest.json')['selected_arms'], list(cycle.PAIRED_ARMS))
        self.assertEqual(self.read('comparison/manifest.json')['original_control_manifest_status'], 'partial')
        self.assertEqual((self.fx.out / 'comparison/control_answers.jsonl').read_bytes(), self.fx.paths['source_answers'].read_bytes())
        self.assertEqual(self.rows('comparison/answers.jsonl')[::2], self.fx.rows)
        self.assertIsNone(summary['score'])
        self.assertEqual(summary['usage'], {'prompt_tokens': 200, 'completion_tokens': 400, 'total_tokens': 600})
        for path in (self.fx.out, self.fx.out/'candidate', self.fx.out/'comparison'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.fx.out/'candidate/answers.jsonl').stat().st_mode), 0o600)

    def test_prepare_only_keeps_zero_calls_queries_and_constructors(self):
        fake = FakeTransport(ledger.MODEL_DIGEST)
        with patch.object(cycle, 'Transport', side_effect=AssertionError('constructor')):
            summary, code = self.run_cycle(fake, prepare_only=True)
        self.assertEqual((summary['status'], summary['calls'], summary['model_metadata_calls'], summary['template_queries'], code), ('prepared', 0, 0, 0, 0))
        self.assertEqual(fake.payloads, [])
        self.assertEqual(fake.metadata_calls, 0)
        self.assertEqual(fake.template_queries, 0)
        self.assertEqual(self.read('preflight/manifest.json')['generation_calls'], 0)
        self.assertFalse(self.read('preflight/manifest.json')['execution_support']['ready'])
        self.assertTrue(self.read('manifest.json')['execution_support']['ready'])
        self.assertEqual(self.read('comparison/manifest.json')['written_answers'], 20)
        self.assertFalse(summary['review']['review_eligible'])

    def test_failed_empty_and_truncated_response_and_cached_failures_included_in_all_packets(self):
        answers = [response()] * 20
        answers[3] = response('', 'length', 2048)
        answers[16] = response('Partial Unicode 답변\n ', 'length', 2048)
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, answers))
        self.assertEqual((summary['status'], summary['calls'], code), ('partial', 20, 1))
        self.assertEqual(summary['answer_status_counts'], {'completed': 18, 'failed': 1, 'truncated': 1})
        self.assertEqual(summary['unattempted_answers'], 0)
        self.assertEqual(summary['empty_final_answer_count'], 1)
        self.assertEqual(self.rows()[3]['error'], {'code': 'empty_answer', 'http_status': None})
        self.assertTrue(self.rows()[3]['attempts'][0]['response_received'])
        self.assertEqual(self.rows()[16]['answer'], 'Partial Unicode 답변\n ')
        self.assertEqual(self.rows()[16]['status'], 'truncated')
        self.assertEqual(summary['review']['ready_packet_count'], 20)
        self.assertTrue(summary['review']['review_eligible'])
        self.assertFalse(summary['review'].get('quality_improvement_proven', False))

    def test_invalid_provider_response_preserves_one_actual_call_and_stops(self):
        bads = [None, {}, {'choices': []}, {'choices': [{}]}, {'choices':[{'message':{'content':{}},'finish_reason':'stop'}]},
            {'choices':[{'message':{'content':'safe'},'finish_reason':[]}]}]
        for number, bad in enumerate(bads):
            with self.subTest(number=number):
                transport = FakeTransport(ledger.MODEL_DIGEST, [bad])
                summary, code = self.run_cycle(transport, out=self.fx.out.parent / f'bad-provider-{number}')
                self.assertEqual((summary['status'], summary['calls'], summary['unattempted_answers'], code), ('blocked', 1, 19, 2))
                path = self.fx.out.parent / f'bad-provider-{number}' / 'candidate/answers.jsonl'
                row = json.loads(path.read_text())
                self.assertEqual(row['error'], {'code':'invalid_provider_response','http_status':None})
                self.assertTrue(row['attempts'][0]['provider_response_received'])
                self.assertFalse(row['attempts'][0]['response_received'])
                self.assertEqual(summary['review']['ready_packet_count'], 0)

    def test_invalid_unicode_provider_answer_is_sanitized_and_stops(self):
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, [response('x\ud800')]))
        self.assertEqual((summary['calls'], summary['status'], code), (1, 'blocked', 2))
        self.assertEqual(self.rows()[0]['answer'], '')
        self.assertEqual(self.rows()[0]['error']['code'], 'invalid_provider_response')
        self.assertNotIn('\\ud800', (self.fx.out/'candidate/answers.jsonl').read_text())

    def test_invalid_finish_preserves_literal_received_content_and_reported_usage(self):
        raw = '  RECEIVED MALFORMED ENVELOPE\n한글 '
        fake = FakeTransport(ledger.MODEL_DIGEST, [response(raw, [], 17)])
        summary, code = self.run_cycle(fake)
        attempt = self.rows()[0]['attempts'][0]
        self.assertEqual((summary['calls'], code), (1, 2))
        self.assertEqual(attempt['unparsed_received_answer'], raw)
        self.assertEqual(attempt['usage'], {'prompt_tokens':10,'completion_tokens':17,'total_tokens':27})
        self.assertEqual(summary['usage'], attempt['usage'])
        self.assertEqual(summary['provider_response_received_count'], 1)
        self.assertEqual(summary['malformed_provider_response_count'], 1)
        self.assertEqual(self.rows()[0]['answer'], '')
        self.assertFalse(summary['review']['review_eligible'])

    def test_null_provider_content_is_received_empty_failure_not_transport_block(self):
        answers = [response()] * 20
        answers[0] = response(None, 'length', 2048)
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, answers))
        self.assertEqual((summary['calls'], summary['status'], code), (20, 'partial', 1))
        self.assertEqual(self.rows()[0]['error']['code'], 'empty_answer')
        self.assertTrue(self.rows()[0]['attempts'][0]['provider_response_received'])

    def test_transport_exception_preserves_failed_actual_call_and_sanitizes_secret(self):
        fake = FakeTransport(ledger.MODEL_DIGEST, [RuntimeError('SECRET_SENTINEL')])
        summary, code = self.run_cycle(fake)
        self.assertEqual((summary['status'], summary['calls'], summary['unattempted_answers'], code), ('blocked', 1, 19, 2))
        self.assertEqual(self.rows()[0]['error'], {'code':'transport_failure','http_status':None})
        self.assertFalse(self.rows()[0]['attempts'][0]['provider_response_received'])
        self.assertNotIn('SECRET_SENTINEL', json.dumps(summary))

    def test_interrupt_during_actual_generation_keeps_attempt_and_zero_retries(self):
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, [KeyboardInterrupt()]))
        self.assertEqual((summary['status'], summary['calls'], summary['unattempted_answers'], code), ('interrupted', 1, 19, 130))
        self.assertEqual(len(self.rows()[0]['attempts']), 1)
        self.assertEqual(self.rows()[0]['error']['code'], 'interrupted')

    def test_digest_unavailable_or_changed_before_call_keeps_zero_generations(self):
        for number, result in enumerate(({'name':DEFAULT_MODEL, 'digest':'a'*64}, {}, RuntimeError('metadata-secret'))):
            fake = FakeTransport(ledger.MODEL_DIGEST)
            fake.metadata_result = result
            summary, code = self.run_cycle(fake, out=self.fx.out.parent / f'digest-{number}')
            self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (0, 20, 2))
            self.assertEqual(fake.payloads, [])
            self.assertEqual(fake.template_queries, 0)

    def test_template_missing_changed_or_invalid_unicode_blocks_before_generation(self):
        for number, template in enumerate(({}, {'template':'changed'}, {'template':'bad\ud800'}, RuntimeError('template-secret'))):
            fake = FakeTransport(ledger.MODEL_DIGEST)
            fake.template_result = template
            summary, code = self.run_cycle(fake, out=self.fx.out.parent / f'template-{number}')
            self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (0, 20, 2))
            self.assertEqual(summary['template_queries'], 1)
            self.assertFalse(summary['current_runtime_template']['verified'])

    def test_source_mutation_before_first_call_blocks_zero_calls(self):
        fake = FakeTransport(ledger.MODEL_DIGEST, on_metadata=lambda _: self.fx.paths['source_summary'].write_text('CHANGED'))
        summary, code = self.run_cycle(fake)
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (0, 20, 2))
        self.assertEqual(summary['error']['code'], 'source_changed')

    def test_source_mutation_during_response_keeps_received_bytes_and_stops_rest(self):
        fake = FakeTransport(ledger.MODEL_DIGEST, [response('RECEIVED EXACT\n한글 ')],
            on_complete=lambda _: self.fx.paths['source_answers'].write_text('CHANGED'))
        summary, code = self.run_cycle(fake)
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (1, 19, 2))
        self.assertEqual(self.rows()[0]['answer'], 'RECEIVED EXACT\n한글 ')
        self.assertFalse(self.rows()[0]['model_identity_verified'])
        self.assertTrue(self.read('comparison/manifest.json')['sources']['baseline']['source_changed_after_validation'])
        self.assertEqual(len(self.rows('comparison/answers.jsonl')), 21)

    def test_execution_code_mutation_during_response_stops_with_received_answer(self):
        path = self.fx.root / 'benchmarks/healthbench_source_ledger_cycle.py'
        fake = FakeTransport(ledger.MODEL_DIGEST, [response('CODE PIN RESPONSE')], on_complete=lambda _:path.write_text('CHANGED'))
        summary, code = self.run_cycle(fake)
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (1, 19, 2))
        self.assertEqual(summary['error']['code'], 'source_changed')
        self.assertEqual(self.rows()[0]['answer'], 'CODE PIN RESPONSE')
        self.assertEqual(summary['review']['ready_packet_count'], 0)

    def test_digest_changed_after_response_preserves_pre_post_evidence_and_stops(self):
        def changed(fake):
            if fake.metadata_calls == 2:
                fake.digest = 'b'*64
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, on_metadata=changed))
        row = self.rows()[0]
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (1, 19, 2))
        self.assertEqual(row['digest_before'], ledger.MODEL_DIGEST)
        self.assertEqual(row['digest_after'], 'b'*64)
        self.assertFalse(row['model_identity_verified'])

    def test_template_changed_after_response_preserves_pre_post_evidence_and_stops(self):
        def changed(fake):
            if fake.template_queries == 2:
                fake.template_result = {'template':'CHANGED'}
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, on_template=changed))
        row = self.rows()[0]
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (1, 19, 2))
        self.assertEqual(row['template_before_sha256'], TEMPLATE_SHA)
        self.assertNotEqual(row['template_after_sha256'], TEMPLATE_SHA)
        self.assertFalse(row['runtime_template_verified'])

    def test_incomplete_review_counts_nonempty_truncated_control_pair_after_twenty_calls(self):
        def changed(fake):
            if fake.metadata_calls == 41:
                self.fx.paths['source_summary'].write_text('CHANGED AFTER ALL GENERATIONS BEFORE PACKETS')
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, on_metadata=changed))
        self.assertEqual((summary['status'], summary['calls'], summary['unattempted_answers'], code), ('blocked',20,0,2))
        self.assertEqual(summary['review']['status'], 'not_prepared_blocked')
        self.assertEqual(summary['review']['available_nonempty_pair_count'], 19)
        self.assertEqual(summary['review']['prepared_packet_count'], 0)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(self.fx.rows[16]['status'], 'truncated')
        self.assertTrue(self.fx.rows[16]['answer'].strip())
        self.assertEqual(self.rows()[16]['status'], 'completed')

    def test_late_source_mutation_after_all_packets_preserves_actual_calls_and_packet_count(self):
        create = review.create_review_packets
        def changed(*args, **kwargs):
            result = create(*args, **kwargs)
            self.fx.paths['source_summary'].write_text('LATE CHANGED')
            return result
        with patch.object(review, 'create_review_packets', side_effect=changed):
            summary, code = self.run_cycle()
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (20, 0, 2))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(len(self.rows()), 20)
        self.assertEqual(self.read('comparison/manifest.json')['candidate_status'], 'blocked')

    def test_packet_mutation_after_capture_invalidates_review_and_keeps_completed_count(self):
        def changed(fake):
            if fake.metadata_calls == 43:
                path = next((self.fx.out/'comparison/review/packets').glob('*.json'))
                value = json.loads(path.read_bytes())
                value['candidates']['A']['answer'] = 'TAMPERED'
                path.write_text(json.dumps(value))
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, on_metadata=changed))
        self.assertEqual((summary['status'], summary['calls'], summary['unattempted_answers'], code), ('blocked',20,0,2))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(len(self.rows()), 20)
        self.assertIn('integrity_failures', summary)

    def test_packet_changed_between_expected_validation_and_fd_capture_cannot_be_trusted(self):
        original_open = os.open
        changed = False
        def altered(path, flags, *args, **kwargs):
            nonlocal changed
            if (not changed and isinstance(path,str) and path.endswith('.json')
                    and path not in ('manifest.json','private_manifest.json','progress.json')
                    and kwargs.get('dir_fd') is not None and flags & os.O_WRONLY == 0):
                target = self.fx.out/'comparison/review/packets'/path
                if target.exists():
                    changed = True
                    value = json.loads(target.read_bytes())
                    value['candidates']['A']['answer'] = 'CHANGED BETWEEN READS'
                    target.write_text(json.dumps(value))
            return original_open(path,flags,*args,**kwargs)
        with patch.object(cycle.os, 'open', side_effect=altered):
            summary, code = self.run_cycle()
        self.assertTrue(changed)
        self.assertEqual((summary['calls'], code), (20,2))
        self.assertEqual(summary['review']['prepared_packet_count'], 20)
        self.assertEqual(summary['review']['ready_packet_count'], 0)
        self.assertFalse(summary['review']['review_eligible'])

    def test_candidate_artifact_tamper_after_response_keeps_independent_received_snapshot(self):
        def changed(fake):
            if fake.metadata_calls == 2:
                (self.fx.out/'candidate/answers.jsonl').write_text('TAMPERED')
        summary, code = self.run_cycle(FakeTransport(ledger.MODEL_DIGEST, [response('RECEIVED BEFORE ARTIFACT CHANGE')], on_metadata=changed))
        self.assertEqual((summary['calls'], summary['unattempted_answers'], code), (1,19,2))
        saved = json.loads((self.fx.out/'received_answers_snapshot.jsonl').read_text())
        self.assertEqual(saved['answer'], 'RECEIVED BEFORE ARTIFACT CHANGE')
        self.assertEqual(summary['received_answers_snapshot']['count'], 1)
        self.assertFalse(summary['review']['review_eligible'])
        self.assertEqual(self.read('comparison/manifest.json')['sources']['candidate']['integrity_status'], 'failed')

    def test_payload_changed_after_proof_before_send_is_not_counted_as_model_call(self):
        original = cycle._generate
        def altered(item, payload, request, client):
            payload['max_tokens'] = 4096
            return original(item, payload, request, client)
        fake = FakeTransport(ledger.MODEL_DIGEST)
        with patch.object(cycle, '_generate', side_effect=altered):
            summary, code = self.run_cycle(fake)
        self.assertEqual((summary['calls'], code), (0, 2))
        self.assertEqual(fake.payloads, [])
        self.assertEqual(summary['error']['code'], 'candidate_request_changed')

    def test_existing_alias_outside_and_invalid_controls_rejected_without_calls(self):
        for number, (key, value) in enumerate((('timeout',True), ('timeout',0),('timeout',float('inf')),('prepare_only',1))):
            fake = FakeTransport(ledger.MODEL_DIGEST)
            with self.assertRaises(ValueError):
                self.run_cycle(fake, **{key:value}, out=self.fx.out.parent / f'bad-config-{number}')
            self.assertEqual(fake.payloads, [])
        self.run_cycle(prepare_only=True)
        fake = FakeTransport(ledger.MODEL_DIGEST)
        with self.assertRaises(ValueError):
            self.run_cycle(fake)
        with self.assertRaises(ValueError):
            self.run_cycle(fake, out=self.fx.root / 'public-output')
        alias = self.fx.root / 'results' / 'alias'
        alias.symlink_to(self.fx.out, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.run_cycle(fake, out=alias / 'child')
        self.assertEqual(fake.payloads, [])

    def test_source_pin_and_producer_tampering_rejected_without_new_pin_overrides(self):
        for number, path in enumerate((self.fx.source, self.fx.paths['source_answers'], self.fx.paths['source_summary'], self.fx.comparison,
                self.fx.instructions, self.fx.verification, self.fx.root/'benchmarks/healthbench_budget_cycle.py')):
            raw = path.read_bytes()
            path.write_bytes(raw+b'\n')
            fake = FakeTransport(ledger.MODEL_DIGEST)
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.run_cycle(fake, out=self.fx.out.parent/f'bad-source-{number}')
            self.assertEqual(fake.payloads, [])
            path.write_bytes(raw)
        with self.assertRaises(TypeError):
            self.run_cycle(expected_source_summary_sha256='a'*64)

    def test_executor_cli_cannot_override_source_pins_recipe_model_or_resume(self):
        arguments = ['--data',str(self.fx.data),'--source-manifest',str(self.fx.source), '--source-comparison-manifest',str(self.fx.comparison),
            '--instructions-file',str(self.fx.instructions),'--producer-verification',str(self.fx.verification),'--out',str(self.fx.out),'--prepare-only']
        for option in ('--model','--resume','--code-root','--expected-source-sha256','--reasoning-effort','--max-tokens','--candidate-arm'):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cycle.main(arguments+[option,'changed'])
        with patch.object(cycle,'Transport',side_effect=AssertionError('no construction')):
            self.assertEqual(cycle.main(arguments), 0)


if __name__ == '__main__':
    unittest.main()
