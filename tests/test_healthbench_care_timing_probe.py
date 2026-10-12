"""Synthetic HBT02 durability, source isolation, and failure checks; no HTTP."""
from copy import deepcopy
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from benchmarks import healthbench_care_timing_probe as probe
from benchmarks.healthbench import canonical_hash
from tests.test_healthbench_turn_probe import FakeTransport, TEMPLATE, response, sha

REAL_ROOT = Path(__file__).resolve().parents[1]


class CareTimingProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        results = self.root / 'results'
        results.mkdir()
        self.plan = results / 'design' / 'plan-v1.json'
        self.plan.parent.mkdir()
        self.out = results / probe.SCOPE_NAME / 'execution-v1'
        self.data = self.root / 'private_data/healthbench/legacy_consensus_val_roles.json'
        self.data.parent.mkdir(parents=True)
        self.data.write_bytes(b'SYNTHETIC_UNPARSED_HOLDOUT_AND_RUBRIC_SENTINELS')
        self.execution = self.root / 'benchmarks/healthbench_care_timing_probe.py'
        self.execution.parent.mkdir()
        self.execution.write_bytes(Path(probe.__file__).read_bytes())
        for relative in probe.SUPPORT_PATHS:
            target = self.root / relative
            target.write_bytes((REAL_ROOT / relative).read_bytes())
        self.sources, self.requests, prefixes = {}, {}, []
        for case_id in probe.CASE_IDS:
            messages = [{'role': 'system', 'content': probe.GROUNDED_SUFFIX + '\n\n' + probe.TASK_WRAPPER},
                        {'role': 'user', 'content': 'Synthetic patient supplied no additional facts.'}]
            number = 1
            if case_id == 'HB-V0015':
                number = 3
                messages.extend([{'role': 'assistant', 'content': 'UNCONFIRMED historical assistant one.'},
                                 {'role': 'user', 'content': 'Please explain that.'},
                                 {'role': 'assistant', 'content': 'UNCONFIRMED historical assistant two.'},
                                 {'role': 'user', 'content': 'How should I proceed?'}])
            old = {'model': probe.DEFAULT_MODEL, 'messages': messages, 'reasoning_effort': 'medium',
                   'max_tokens': 2048, 'temperature': 0}
            answer = 'TARGET_ANSWER_MUST_NOT_ENTER_REQUEST_' + case_id
            source = {'case_id': case_id, 'doctor_turn': number, 'request': old,
                      'request_sha256': canonical_hash(old), 'answer': answer,
                      'status': 'completed', 'finish_reason': 'stop',
                      'repair_sentinel': 'LATER_REPAIR_MUST_NOT_ENTER_REQUEST',
                      'review_sentinel': 'REVIEW_MUST_NOT_ENTER_REQUEST'}
            path = results / (case_id + '.json')
            self.write(path, source)
            candidate = deepcopy(old)
            candidate['messages'][0]['content'] += '\n\n' + probe.ADDITION
            self.sources[case_id], self.requests[case_id] = path, candidate
            prefixes.append({'case_id': case_id, 'doctor_turn_to_replace': number,
                'source_record': str(path.relative_to(self.root)), 'source_record_byte_sha256': sha(path.read_bytes()),
                'historical_request_sha256': canonical_hash(old),
                'historical_input_sha256': canonical_hash(messages),
                'historical_answer_utf8_sha256': sha(answer.encode()),
                'message_roles': [m['role'] for m in messages], 'message_count': len(messages),
                'planned_candidate_request_sha256': canonical_hash(candidate),
                'planned_candidate_input_sha256': canonical_hash(candidate['messages']),
                'old_system_utf8_sha256': sha(messages[0]['content'].encode()),
                'new_system_utf8_sha256': sha(candidate['messages'][0]['content'].encode())})
        self.prefix = self.plan.parent / 'frozen-prefix-manifest-v1.json'
        self.write(self.prefix, {'prefixes': prefixes})
        self.design = {'single_factor': {'exact_text': probe.ADDITION, 'utf8_sha256': probe.ADDITION_SHA256},
                       'fixed': {'settings': probe.SETTINGS, 'model_digest': probe.MODEL_DIGEST,
                                 'template_byte_sha256': sha(TEMPLATE.encode()),
                                 'source_dataset_sha256': sha(self.data.read_bytes())}}
        self.write(self.plan, self.design)
        patches = [patch.object(probe, 'ROOT', self.root),
                   patch.object(probe, 'EXECUTION_PATH', self.execution),
                   patch.object(probe, 'DESIGN_SHA256', sha(self.plan.read_bytes())),
                   patch.object(probe, 'PREFIX_SHA256', sha(self.prefix.read_bytes())),
                   patch.object(probe, 'DATASET_SHA256', sha(self.data.read_bytes())),
                   patch.object(probe, 'TEMPLATE_SHA256', sha(TEMPLATE.encode())),
                   patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('No HTTP permitted'))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def prepare(self):
        return probe.prepare(self.plan, self.out)

    def fake(self, *args, **kwargs):
        return FakeTransport(probe.MODEL_DIGEST, *args, **kwargs)

    def row(self, case_id='HB-V0000'):
        return json.loads((self.out / (case_id + '.json')).read_bytes())

    def test_prepare_no_transport_fixed_scope_private_and_repeat_prepare_forbidden(self):
        with patch.object(probe, 'Transport', side_effect=AssertionError('Preparation is offline')):
            self.assertEqual(self.prepare()['calls'], 0)
        for out in (self.out, self.out.with_name('different-output')):
            with self.assertRaises((ValueError, OSError)):
                probe.prepare(self.plan, out)
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertFalse(self.out.with_name('different-output').exists())

    def test_exact_one_system_suffix_and_original_prefix_without_target_or_hidden_fields(self):
        self.prepare()
        fake = self.fake()
        for case_id in probe.CASE_IDS:
            probe.step(self.out, case_id, transport=fake)
        self.assertEqual(fake.payloads, list(self.requests.values()))
        text = json.dumps(fake.payloads)
        for forbidden in ('TARGET_ANSWER', 'LATER_REPAIR', 'REVIEW_MUST', 'RUBRIC', 'HOLDOUT'):
            self.assertNotIn(forbidden, text)
        self.assertIn('UNCONFIRMED historical assistant', text)
        self.assertEqual(len(fake.payloads), 4)
        with self.assertRaises(ValueError):
            probe.step(self.out, transport=fake)
        self.assertEqual(len(fake.payloads), 4)

    def test_next_is_sequential_and_duplicate_case_cannot_repeat(self):
        self.prepare()
        fake = self.fake()
        self.assertEqual(probe.step(self.out, transport=fake)['case_id'], 'HB-V0000')
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(probe.step(self.out, transport=fake)['case_id'], 'HB-V0004')
        self.assertEqual(len(fake.payloads), 2)

    def test_pending_reservation_survives_interrupt_and_blocks_all_restarts_and_new_output(self):
        self.prepare()
        def interrupt(_):
            raise SystemExit('simulated process death')
        fake = self.fake(on_complete=interrupt)
        with self.assertRaises(SystemExit):
            probe.step(self.out, 'HB-V0000', transport=fake)
        reservation = self.out.parent / 'HB-V0000-reserved.json'
        self.assertTrue(reservation.exists())
        self.assertFalse((self.out.parent / 'HB-V0000-finished.json').exists())
        for case_id in ('HB-V0000', 'HB-V0004'):
            with self.assertRaises((ValueError, OSError)):
                probe.step(self.out, case_id, transport=fake)
        with self.assertRaises(ValueError):
            probe.prepare(self.plan, self.out.with_name('retry-out'))
        self.assertEqual(len(fake.payloads), 1)

    def test_durable_reservation_exists_before_actual_dispatch_and_concurrent_step_is_blocked(self):
        self.prepare()
        inner = self.fake()
        def inspect(_):
            self.assertTrue((self.out.parent / 'HB-V0000-reserved.json').exists())
            with self.assertRaises(OSError):
                probe.step(self.out, 'HB-V0004', transport=inner)
            with self.assertRaises(OSError):
                probe.prepare(self.plan, self.out.with_name('parallel'))
        fake = self.fake(on_complete=inspect)
        probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(inner.payloads, [])
        self.assertEqual(len(fake.payloads), 1)

    def test_manifest_blindmap_plan_prefix_source_execution_tamper_blocks_and_restore_cannot_resume(self):
        targets = [lambda: self.out / 'manifest.json', lambda: self.out / 'blind-map.json',
                   lambda: self.plan, lambda: self.prefix, lambda: self.sources['HB-V0000'],
                   lambda: self.execution, lambda: self.data,
                   lambda: self.root / probe.SUPPORT_PATHS[0]]
        # Separate disposable fixture for each mutation; no production state is changed.
        for target_fn in targets:
            with self.subTest(path=target_fn()):
                self.prepare()
                target = target_fn()
                raw = target.read_bytes()
                target.write_bytes(raw + b' ')
                fake = self.fake()
                with self.assertRaises((ValueError, OSError)):
                    probe.step(self.out, 'HB-V0000', transport=fake)
                target.write_bytes(raw)
                with self.assertRaises(ValueError):
                    probe.step(self.out, 'HB-V0000', transport=fake)
                self.assertEqual((fake.payloads, fake.metadata_calls), ([], 0))
                # Fixture reset only, never production. Disjoint root on next iteration.
                self.tearDown()
                self.doCleanups()
                self.setUp()

    def test_precall_model_or_template_mismatch_never_dispatches_and_is_permanent(self):
        self.prepare()
        fake = self.fake()
        fake.digest = '0' * 64
        result = probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual((result['status'], result['calls_this_step']), ('blocked', 0))
        fake.digest = probe.MODEL_DIGEST
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(fake.payloads, [])
        counts = json.loads((self.out.parent / 'pre-dispatch-failure.json').read_bytes())
        self.assertEqual(counts['model_metadata_calls'], 1)
        self.assertEqual(counts['actual_generation_calls'], 0)

    def test_pre_template_mismatch_does_not_dispatch(self):
        self.prepare()
        fake = self.fake()
        fake.template_result = {'template': 'CHANGED'}
        self.assertEqual(probe.step(self.out, transport=fake)['calls_this_step'], 0)
        self.assertEqual((fake.payloads, fake.metadata_calls, fake.template_queries), ([], 1, 1))

    def test_postpin_mismatch_preserves_received_answer_and_never_resumes(self):
        self.prepare()
        def changed(fake):
            fake.digest = '0' * 64
        fake = self.fake([response('Exact received answer\n한글')], on_complete=changed)
        result = probe.step(self.out, transport=fake)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.row()['answer'], 'Exact received answer\n한글')
        fake.digest = probe.MODEL_DIGEST
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0004', transport=fake)
        self.assertEqual(len(fake.payloads), 1)

    def test_post_template_or_source_change_blocks(self):
        self.prepare()
        fake = self.fake(on_complete=lambda _: self.data.write_bytes(b'changed dataset'))
        self.assertEqual(probe.step(self.out, transport=fake)['status'], 'blocked')
        self.assertEqual(len(fake.payloads), 1)
        self.assertTrue(self.row()['provider_response_received'])

    def test_missing_usage_stays_null_no_reasoning_saved(self):
        self.prepare()
        fake = self.fake([response(usage=False)])
        probe.step(self.out, transport=fake)
        self.assertEqual(self.row()['usage'], {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None})
        self.assertNotIn('HIDDEN_REASONING', json.dumps(self.row()))
        post = json.loads((self.out / 'HB-V0000-post.json').read_bytes())
        self.assertEqual((post['model_metadata_calls'], post['template_queries']), (2, 2))
        self.assertTrue(post['model_identity_verified'])
        self.assertTrue(post['runtime_template_verified'])

    def test_invalid_provider_envelope_preserves_literal_receipt_and_never_retries(self):
        self.prepare()
        fake = self.fake([response('Literal received malformed answer', finish=[])])
        result = probe.step(self.out, transport=fake)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.row()['received_answer'], 'Literal received malformed answer')
        self.assertEqual(self.row()['usage']['total_tokens'], 30)
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(len(fake.payloads), 1)

    def test_transport_failure_consumes_one_no_secret_error_and_no_retry(self):
        self.prepare()
        fake = self.fake([RuntimeError('DO_NOT_PERSIST_SECRET')])
        result = probe.step(self.out, transport=fake)
        self.assertEqual((result['status'], result['calls_this_step']), ('blocked', 1))
        self.assertNotIn('DO_NOT_PERSIST_SECRET', json.dumps(self.row()))
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(len(fake.payloads), 1)

    def test_empty_or_truncated_never_pass_review_export(self):
        self.prepare()
        fake = self.fake([response('', 'stop'), response('partial', 'length'), response(), response()])
        statuses = [probe.step(self.out, transport=fake)['status'] for _ in range(4)]
        self.assertEqual(statuses, ['failed', 'truncated', 'completed', 'completed'])
        with self.assertRaises(ValueError):
            probe.export_blind(self.out, self.out.with_name('blind-v1'))
        self.assertEqual(len(fake.payloads), 4)

    def test_record_and_sidecar_tamper_stop_remaining_calls(self):
        self.prepare()
        fake = self.fake()
        probe.step(self.out, transport=fake)
        path = self.out / 'HB-V0000-post.json'
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaises(ValueError):
            probe.step(self.out, transport=fake)
        self.assertEqual(len(fake.payloads), 1)

    def test_blind_map_is_pregeneration_pinned_and_export_has_only_context_and_coded_answers(self):
        self.prepare()
        mapping = (self.out / 'blind-map.json').read_bytes()
        with self.assertRaises(ValueError):
            probe.export_blind(self.out, self.out.with_name('too-early'))
        fake = self.fake()
        for _ in range(4):
            probe.step(self.out, transport=fake)
        dest = self.out.with_name('blind-v1')
        result = probe.export_blind(self.out, dest)
        self.assertEqual(result['packets'], 4)
        self.assertEqual(mapping, (self.out / 'blind-map.json').read_bytes())
        for path in dest.glob('*.json'):
            packet = json.loads(path.read_bytes())
            self.assertEqual(set(packet), {'case_code', 'context', 'replies'})
            self.assertEqual(set(packet['replies']), {'A', 'B'})
            self.assertTrue(all(m['role'] != 'system' for m in packet['context']))
            self.assertNotIn(probe.ADDITION, path.read_text())
            for value in packet['replies'].values():
                self.assertEqual(set(value), {'answer', 'status'})
        with self.assertRaises(OSError):
            probe.export_blind(self.out, dest)
        self.assertEqual(len(fake.payloads), 4)

    def test_paths_aliases_outside_scope_and_unknown_case_are_rejected(self):
        with self.assertRaises(ValueError):
            probe.prepare(self.plan, self.root / 'results' / 'outside')
        self.prepare()
        alias = self.out.with_name('alias')
        alias.symlink_to(self.out, target_is_directory=True)
        with self.assertRaises(ValueError):
            probe.step(alias, transport=self.fake())
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0020', transport=self.fake())

    def test_post_template_change_preserves_receipt_and_blocks_after_restore(self):
        self.prepare()
        fake = self.fake(on_complete=lambda value: setattr(value, 'template_result', {'template': 'CHANGED'}))
        self.assertEqual(probe.step(self.out, transport=fake)['status'], 'blocked')
        fake.template_result = {'template': TEMPLATE}
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0004', transport=fake)
        self.assertEqual(len(fake.payloads), 1)

    def test_request_change_at_dispatch_is_detected_after_durable_reservation(self):
        self.prepare()
        fake = self.fake()
        original = probe._SourceTransport.complete
        def tamper(client, payload):
            payload['messages'][-1]['content'] += 'UNAUTHORIZED_CHANGE'
            return original(client, payload)
        with patch.object(probe._SourceTransport, 'complete', tamper):
            with self.assertRaises(RuntimeError):
                probe.step(self.out, 'HB-V0000', transport=fake)
        self.assertEqual(fake.payloads, [])
        self.assertTrue((self.out.parent / 'HB-V0000-reserved.json').exists())
        with self.assertRaises(ValueError):
            probe.step(self.out, 'HB-V0000', transport=fake)

    def test_fixed_transport_uses_local_endpoint_no_key_and_ninety_seconds(self):
        self.prepare()
        fake = self.fake()
        with patch.object(probe, 'Transport', return_value=fake) as constructor:
            probe.step(self.out)
        constructor.assert_called_once_with(probe.DEFAULT_BASE_URL, '', 90)


if __name__ == '__main__':
    unittest.main()
