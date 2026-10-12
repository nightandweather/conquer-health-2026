"""E04 frozen-input, dispatch and format tests; no model semantic validation."""
import ast
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_record_contract_submission as builder
from tests import test_guidelines as fixtures
from tests.test_nova_review_once import FakeClient, completion

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = builder.SOURCE


def load_candidate():
    spec = importlib.util.spec_from_file_location('nova_sdk', CANDIDATE / 'nova_sdk.py')
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    spec = importlib.util.spec_from_file_location('record_contract_candidate', CANDIDATE / 'run.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'nova_sdk': sdk}):
        spec.loader.exec_module(module)
    return module


class RecordContractAgentTests(unittest.TestCase):
    setUp = fixtures.GuidelineTests.setUp
    tearDown = fixtures.GuidelineTests.tearDown
    add = fixtures.GuidelineTests.add
    catalog = fixtures.GuidelineTests.catalog

    @classmethod
    def setUpClass(cls):
        cls.m = load_candidate()

    def prepare(self):
        with patch.dict(os.environ, {'NOVA_GUIDELINES_DIR': str(self.source),
                                     'NOVA_OUTPUT_DIR': str(self.output)}, clear=True), \
                patch('socket.socket', side_effect=AssertionError('No network')):
            return self.m.prepare()

    def execute(self, client, state):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream), patch('socket.socket', side_effect=AssertionError('No network')):
            result = self.m.consult(client, state)
        self.assertNotIn('합성 환자', stream.getvalue())
        self.assertNotIn('aspirin evidence', stream.getvalue())
        return result, json.loads(stream.getvalue())

    def test_actual_record_has_changed_prompt_original_observations_and_no_retrieval(self):
        state = self.prepare()
        answer = {'S': {'pmh': '약 ( )'}, 'O': {'tests': ''},
                  'P': {'tests': '합성 검사 계획: 실제 결과 없음',
                        'treatment': '알레르기·복용 약 미확인: 확인 후 치료 결정',
                        'follow_up': '추가 확인 필요',
                        'education': '실제 설명 없음; 설명 계획은 추가 확인 후 결정'},
                  'diagnosis': '합성 주진단'}
        client = FakeClient([completion({'action': 'SAY', 'content': '미실행 초안'}),
                             completion({'action': 'SAY', 'content': '실제 확인 질문'}), completion(answer)])
        client.session_value['initial']['opening'] = 'aspirin'
        result, stats = self.execute(client, state)
        self.assertEqual(result['synthetic_result'], 'record-submitted')
        self.assertEqual(client.actions, [('SAY', '실제 확인 질문')])
        self.assertEqual(len(client.calls), 3)
        draft, review, record = [call[0][0] for call in client.calls]
        self.assertEqual(draft[1:3], review[1:3])
        self.assertIn('aspirin evidence', draft[2]['content'])
        self.assertEqual(record[0], {'role': 'system', 'content': self.m.RECORD_PROMPT})
        self.assertEqual(builder.sha(record[0]['content'].encode()), builder.PROMPT_SHA)
        self.assertEqual(len(record), 2)
        self.assertEqual(record[1]['content'], self.m.situation(client.session(), [
            '의사: 실제 확인 질문', '환자: 합성 환자의 응답']))
        self.assertNotIn('미실행 초안', record[1]['content'])
        self.assertNotIn('NOVA_GUIDELINE_REFERENCES', json.dumps(record))
        self.assertNotIn('aspirin evidence', json.dumps(record))
        self.assertEqual(client.diagnoses[0]['note']['O']['tests'], '')
        self.assertEqual(client.diagnoses[0]['note']['S']['pmh'], '약 ( )')
        for field, value in answer['P'].items():
            self.assertEqual(client.diagnoses[0]['note']['P'][field], value)
        self.assertEqual([call[1] for call in client.calls], [
            {'max_tokens': 2048, 'reasoning_effort': 'medium'},
            {'max_tokens': 2048, 'reasoning_effort': 'medium'},
            {'max_tokens': 4096, 'reasoning_effort': 'medium'}])
        self.assertEqual([stats['chat_phases'][phase]['attempted'] for phase in ('draft', 'review', 'record')], [1, 1, 1])

    def test_exam_result_transcript_has_no_invented_turn_number(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'EXAM', 'content': '합성 진찰'}),
                             completion({'action': 'EXAM', 'content': '합성 진찰'}),
                             completion({'diagnosis': '합성 진단'})])
        self.execute(client, state)
        record_user = client.calls[-1][0][0][1]['content']
        self.assertEqual(record_user, self.m.situation(client.session(), ['진찰(합성 진찰): 합성 진찰 소견']))
        self.assertNotIn('턴', record_user)
        self.assertNotIn('검사 결과', record_user)

    def test_three_invalid_record_attempts_keep_transcript_and_empty_unknown_fields(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'SAY', 'content': '합성 질문'}),
                             completion({'action': 'SAY', 'content': '합성 질문'}),
                             completion('not JSON'), completion({}), completion('')])
        _, stats = self.execute(client, state)
        self.assertEqual(stats['chat_phases']['record']['attempted'], 3)
        self.assertEqual(client.diagnoses[0]['diagnosis'], '진단 미정')
        self.assertEqual(client.diagnoses[0]['note']['S']['hpi'], '의사: 합성 질문\n환자: 합성 환자의 응답')
        self.assertEqual(client.diagnoses[0]['note']['S']['pmh'], '')
        self.assertTrue(all(value == '' for value in client.diagnoses[0]['note']['O'].values()))
        self.assertTrue(all(value == '' for value in client.diagnoses[0]['note']['P'].values()))
        for call in client.calls[-3:]:
            self.assertEqual(call[0][0][0]['content'], self.m.RECORD_PROMPT)
            self.assertEqual(call[1]['max_tokens'], 4096)

    def test_record_provider_failure_is_preserved_without_new_retry_policy(self):
        state = self.prepare()
        client = FakeClient([self.m.NovaError('SESSION_EXPIRED')], turns=0)
        _, stats = self.execute(client, state)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(stats['chat_phases']['record']['errors'], 1)
        self.assertEqual(client.diagnoses[0]['diagnosis'], '진단 미정')
        self.assertEqual(stats['all_chat']['unknown_completion_calls'], 1)

    def test_retrieval_source_mutation_clears_references_and_retains_record_contract(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'DONE'}), completion({'action': 'DONE'}),
                             completion({'diagnosis': '합성 진단'})])
        client.session_value['initial']['opening'] = 'aspirin'
        (self.source / 'EN/a.md').write_text('tampered synthetic source')
        self.execute(client, state)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual([len(call[0][0]) for call in client.calls], [2, 2, 2])
        self.assertNotIn('tampered synthetic source', json.dumps(client.calls))
        self.assertEqual(client.calls[-1][0][0][0]['content'], self.m.RECORD_PROMPT)

    def test_oversized_base_history_still_blocks_requests_without_truncation(self):
        state = self.prepare()
        client = FakeClient([])
        client.session_value['initial']['opening'] = '가' * 30000
        _, stats = self.execute(client, state)
        self.assertEqual(client.calls, [])
        self.assertEqual(stats['all_chat']['attempted'], 0)
        self.assertEqual(stats['input_byte_limit_stops'], 2)
        self.assertEqual(client.diagnoses[0]['diagnosis'], '진단 미정')


class RecordContractBuildTests(unittest.TestCase):
    def setUp(self):
        self.baseline = builder.capture(builder.BASE)
        self.candidate = builder.capture(builder.SOURCE)

    def test_exact_byte_and_ast_contract_and_all_ten_baseline_pins(self):
        proof = builder.validate_captured(self.baseline, self.candidate)
        self.assertTrue(proof['outside_assignment_bytes_identical'])
        self.assertEqual(proof['other_runtime_files_identical'], 9)
        self.assertEqual(proof['candidate_record_prompt_sha256'], builder.PROMPT_SHA)

    def test_each_baseline_file_drift_is_rejected(self):
        for name in builder.MEMBERS:
            with self.subTest(name=name):
                mutated = dict(self.baseline)
                mutated[name] += b'\n'
                with self.assertRaisesRegex(ValueError, 'Frozen E03 source drift'):
                    builder.validate_captured(mutated, self.candidate)

    def test_each_nonprompt_candidate_file_drift_is_rejected(self):
        for name in builder.MEMBERS[1:]:
            with self.subTest(name=name):
                mutated = dict(self.candidate)
                mutated[name] += b'\n'
                with self.assertRaisesRegex(ValueError, 'Non-prompt runtime drift'):
                    builder.validate_captured(self.baseline, mutated)

    def test_even_nonsemantic_bytes_outside_assignment_are_rejected(self):
        mutated = dict(self.candidate)
        mutated['run.py'] += b'\n'
        with self.assertRaisesRegex(ValueError, 'outside RECORD_PROMPT'):
            builder.validate_captured(self.baseline, mutated)

    def test_unapproved_prompt_or_unchanged_baseline_cannot_be_packaged(self):
        for run in (self.baseline['run.py'], self.candidate['run.py'].replace(
                '예선에서는 O.tests'.encode(), '본선에서는 O.tests'.encode())):
            mutated = dict(self.candidate)
            mutated['run.py'] = run
            with self.assertRaisesRegex(ValueError, 'Unapproved RECORD_PROMPT'):
                builder.validate_captured(self.baseline, mutated)

    def test_nonliteral_or_repeated_prompt_assignment_is_rejected(self):
        for raw in (b'RECORD_PROMPT = str("x")\n', b'RECORD_PROMPT = "x"\nRECORD_PROMPT = "y"\n'):
            with self.assertRaises(ValueError):
                builder.prompt_parts(raw)

    def test_missing_member_fails_closed(self):
        mutated = dict(self.candidate)
        del mutated['nova/guideline_memory.py']
        with self.assertRaisesRegex(ValueError, 'member set drift'):
            builder.validate_captured(self.baseline, mutated)

    def test_runtime_symlink_and_parent_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for kind in ('file', 'parent'):
                target = root / kind
                shutil.copytree(builder.SOURCE, target)
                if kind == 'file':
                    (target / 'run.py').unlink()
                    (target / 'run.py').symlink_to(builder.SOURCE / 'run.py')
                else:
                    shutil.rmtree(target / 'nova')
                    (target / 'nova').symlink_to(builder.SOURCE / 'nova', target_is_directory=True)
                with self.assertRaises(ValueError):
                    builder.capture(target)

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            marker = path / 'keep'
            marker.write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                builder.build(path)
            self.assertEqual(marker.read_bytes(), b'existing')

    def test_source_change_during_offline_check_prevents_output(self):
        # Isolated calls are exercised separately below; this checks final recapture.
        with tempfile.TemporaryDirectory() as tmp:
            base, candidate = self.baseline, self.candidate
            mutated = dict(candidate)
            mutated['run.py'] += b'\n'
            proof = builder.validate_captured(base, candidate)
            before = {'record_system_sha256': proof['baseline_record_prompt_sha256']}
            after = {'record_system_sha256': builder.PROMPT_SHA}
            output = Path(tmp) / 'build'
            with patch.object(builder, 'capture', side_effect=[base, candidate, base, mutated]), \
                    patch.object(builder, 'isolated_check', side_effect=[before, after]):
                with self.assertRaisesRegex(ValueError, 'changed during check'):
                    builder.build(output)
            self.assertFalse(output.exists())

    def test_isolated_actual_zip_payload_differs_only_in_record_system_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = builder.build(Path(tmp) / 'build')
            self.assertEqual(receipt['offline_checks']['synthetic_chat_calls'], 5)
            self.assertEqual(receipt['offline_checks']['real_model_calls'], 0)
            self.assertTrue(receipt['offline_checks']['network_denied'])
            self.assertTrue(receipt['offline_checks']['record_no_retrieval'])
            self.assertEqual(receipt['offline_checks']['normalized_actual_calls_sha256'],
                             receipt['baseline_offline_checks']['normalized_actual_calls_sha256'])
            self.assertEqual(receipt['offline_checks']['limits'], [30, 180, 30, 200, 100000, 500000, 12, 12])
            self.assertEqual(receipt['sample_submissions'], 0)
            self.assertFalse(receipt['clinical_quality_verified'])


if __name__ == '__main__':
    unittest.main()
