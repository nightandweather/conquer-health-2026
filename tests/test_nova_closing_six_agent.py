"""E05 boundary/dispatch and frozen-source tests; no clinical-quality inference."""
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

from scripts import build_closing_six_submission as builder
from tests import test_guidelines as fixtures
from tests.test_nova_review_once import FakeClient, completion

SAY_HINT = '\n이제 환자에게 가능성 높은 진단과 다음 계획을 쉬운 말로 설명하세요(SAY).'
EXAM_HINT = '\n문진을 마무리하고 필요한 신체 진찰(EXAM)을 하세요.'


def load_module(directory, name):
    spec = importlib.util.spec_from_file_location('nova_sdk', directory / 'nova_sdk.py')
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    spec = importlib.util.spec_from_file_location(name, directory / 'run.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'nova_sdk': sdk}):
        spec.loader.exec_module(module)
    return module


class ClosingSixAgentTests(unittest.TestCase):
    setUp = fixtures.GuidelineTests.setUp
    tearDown = fixtures.GuidelineTests.tearDown
    add = fixtures.GuidelineTests.add
    catalog = fixtures.GuidelineTests.catalog

    @classmethod
    def setUpClass(cls):
        cls.m = load_module(builder.SOURCE, 'closing_six_candidate')
        cls.baseline = load_module(builder.BASE, 'closing_six_control')

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

    def test_all_remaining_turn_requests_only_differ_at_six_through_three(self):
        session = FakeClient([]).session()
        transcript = ['의사: 합성 질문', '환자: 합성 응답']
        original = copy.deepcopy(transcript)
        for remaining in range(1, 31):
            with self.subTest(remaining=remaining):
                old = FakeClient([completion({'action': 'EXAM', 'content': '합성 진찰'})])
                new = FakeClient([completion({'action': 'EXAM', 'content': '합성 진찰'})])
                self.assertEqual(self.baseline.next_step(old, session, transcript, remaining),
                                 self.m.next_step(new, session, transcript, remaining))
                old_request = old.calls[0][0][0][1]['content']
                new_request = new.calls[0][0][0][1]['content']
                if remaining in (6, 5, 4, 3):
                    self.assertIn(EXAM_HINT, old_request)
                    self.assertIn(SAY_HINT, new_request)
                    self.assertEqual(old_request.replace(EXAM_HINT, SAY_HINT), new_request)
                else:
                    self.assertEqual(old.calls, new.calls)
                if remaining == 7:
                    self.assertIn(EXAM_HINT, new_request)
                if remaining in (2, 1):
                    self.assertIn(SAY_HINT, new_request)
                if remaining > 10:
                    self.assertNotIn(EXAM_HINT, new_request)
                    self.assertNotIn(SAY_HINT, new_request)
                self.assertEqual(new.calls[0][0][0][0]['content'], self.baseline.STEP_PROMPT)
                self.assertEqual(new.calls[0][1], {'max_tokens': 2048, 'reasoning_effort': 'medium'})
        self.assertEqual(transcript, original)

    def test_review_can_dispatch_exam_under_six_turn_say_hint_and_record_stays_e03(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'SAY', 'content': '합성 미실행 설명'}),
                             completion({'action': 'EXAM', 'content': '합성 실제 진찰'}),
                             completion({'O': {'exam': '합성 진찰 소견'}, 'diagnosis': '합성 주진단'})], turns=6)
        client.session_value['initial']['opening'] = 'aspirin'
        result, stats = self.execute(client, state)
        self.assertEqual(result['synthetic_result'], 'record-submitted')
        self.assertEqual(client.actions, [('EXAM', '합성 실제 진찰')])
        draft, review, record = [call[0][0] for call in client.calls]
        self.assertIn(SAY_HINT, draft[-1]['content'])
        self.assertEqual(draft[1:3], review[1:3])
        self.assertIn('aspirin evidence', draft[2]['content'])
        self.assertEqual(review[0]['content'], self.baseline.REVIEW_PROMPT)
        self.assertEqual(record[0]['content'], self.baseline.RECORD_PROMPT)
        self.assertEqual(record[1]['content'], self.baseline.situation(client.session(),
                         ['진찰(합성 실제 진찰): 합성 진찰 소견']))
        self.assertNotIn('NOVA_GUIDELINE_REFERENCES', json.dumps(record))
        self.assertNotIn('합성 미실행 설명', record[1]['content'])
        self.assertEqual([call[1] for call in client.calls], [
            {'max_tokens': 2048, 'reasoning_effort': 'medium'},
            {'max_tokens': 2048, 'reasoning_effort': 'medium'},
            {'max_tokens': 4096, 'reasoning_effort': 'medium'}])
        self.assertEqual([stats['chat_phases'][p]['attempted'] for p in ('draft', 'review', 'record')], [1, 1, 1])

    def test_invalid_review_falls_back_to_valid_draft_without_extra_review_attempt(self):
        for output in (completion(''), completion({'action': 'SAY', 'content': '잘린 설명'}, finish='length')):
            with self.subTest(output=output):
                state = self.prepare()
                client = FakeClient([completion({'action': 'EXAM', 'content': '합성 초안 진찰'}),
                                     output, completion({'diagnosis': '합성 주진단'})], turns=6)
                _, stats = self.execute(client, state)
                self.assertEqual(client.actions, [('EXAM', '합성 초안 진찰')])
                self.assertEqual(stats['review_fallback'], 1)
                self.assertEqual(stats['chat_phases']['review']['attempted'], 1)
                self.assertEqual(len(client.calls), 3)
                self.assertIn(SAY_HINT, client.calls[0][0][0][-1]['content'])

    def test_invalid_draft_retry_retains_hint_and_has_no_new_action_gate(self):
        state = self.prepare()
        client = FakeClient([completion('not JSON'), completion({'action': 'EXAM', 'content': '합성 진찰'}),
                             completion({'action': 'EXAM', 'content': '합성 진찰'}),
                             completion({'diagnosis': '합성 주진단'})], turns=6)
        _, stats = self.execute(client, state)
        self.assertEqual(client.actions, [('EXAM', '합성 진찰')])
        self.assertEqual(stats['chat_phases']['draft']['attempted'], 2)
        for call in client.calls[:2]:
            self.assertIn(SAY_HINT, call[0][0][-1]['content'])
            self.assertEqual(call[1]['max_tokens'], 2048)
        self.assertIn('직전 출력은 형식에 맞지 않았습니다', client.calls[1][0][0][-1]['content'])

    def test_done_still_ends_without_forced_say(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'DONE'}), completion({'action': 'DONE'}),
                             completion({'diagnosis': '합성 주진단'})], turns=6)
        _, stats = self.execute(client, state)
        self.assertEqual(client.actions, [])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(stats['review_keep'], 1)
        self.assertEqual(stats['chat_phases']['record']['attempted'], 1)

    def test_retrieval_source_drift_clears_reference_without_changing_new_hint(self):
        state = self.prepare()
        (self.source / 'EN/a.md').write_text('tampered synthetic source')
        client = FakeClient([completion({'action': 'DONE'}), completion({'action': 'DONE'}),
                             completion({'diagnosis': '합성 주진단'})], turns=6)
        client.session_value['initial']['opening'] = 'aspirin'
        self.execute(client, state)
        self.assertEqual([len(call[0][0]) for call in client.calls], [2, 2, 2])
        self.assertIn(SAY_HINT, client.calls[0][0][0][-1]['content'])
        self.assertNotIn('tampered synthetic source', json.dumps(client.calls))

    def test_time_margin_still_skips_action_and_reserves_original_record_request(self):
        state = self.prepare()
        client = FakeClient([completion({'diagnosis': '합성 주진단'})], turns=6)
        with patch.object(self.m, 'seconds_left', return_value=179):
            _, stats = self.execute(client, state)
        self.assertEqual(client.actions, [])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][0][0][0]['content'], self.baseline.RECORD_PROMPT)
        self.assertEqual(client.calls[0][1]['max_tokens'], 4096)
        self.assertEqual(stats['chat_phases']['record']['attempted'], 1)


class ClosingSixBuildTests(unittest.TestCase):
    def setUp(self):
        self.baseline = builder.capture(builder.BASE)
        self.candidate = builder.capture(builder.SOURCE)

    def test_exactly_one_threshold_byte_and_nine_identical_runtime_files(self):
        proof = builder.validate_captured(self.baseline, self.candidate)
        self.assertEqual(proof['changed_run_bytes'], 1)
        self.assertEqual(proof['other_runtime_files_identical'], 9)
        i = proof['changed_byte_offset']
        self.assertEqual(self.baseline['run.py'][i:i+1], b'2')
        self.assertEqual(self.candidate['run.py'][i:i+1], b'6')

    def test_drift_in_every_frozen_e03_member_is_rejected(self):
        for name in builder.MEMBERS:
            with self.subTest(name=name):
                modified = dict(self.baseline)
                modified[name] += b'\n'
                with self.assertRaisesRegex(ValueError, 'Frozen E03 source drift'):
                    builder.validate_captured(modified, self.candidate)

    def test_drift_in_every_other_candidate_member_is_rejected(self):
        for name in builder.MEMBERS[1:]:
            with self.subTest(name=name):
                modified = dict(self.candidate)
                modified[name] += b'\n'
                with self.assertRaisesRegex(ValueError, 'Other runtime file drift'):
                    builder.validate_captured(self.baseline, modified)

    def test_any_other_run_difference_or_old_threshold_is_rejected(self):
        for run in (self.baseline['run.py'], self.candidate['run.py'] + b'\n',
                    self.candidate['run.py'].replace(b'if turns_left <= 6:', b'if turns_left <= 5:'),
                    self.candidate['run.py'].replace('추가 검사 계획'.encode(), '검사와 이유'.encode())):
            modified = dict(self.candidate)
            modified['run.py'] = run
            with self.assertRaisesRegex(ValueError, 'exactly the closing threshold byte'):
                builder.validate_captured(self.baseline, modified)

    def test_missing_or_added_members_fail_closed(self):
        missing = dict(self.candidate)
        del missing['nova_sdk.py']
        extra = dict(self.candidate, **{'unexpected.py': b''})
        for modified in (missing, extra):
            with self.assertRaisesRegex(ValueError, 'member set drift'):
                builder.validate_captured(self.baseline, modified)

    def test_source_file_and_nested_directory_symlinks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for kind in ('file', 'directory'):
                target = Path(tmp) / kind
                shutil.copytree(builder.SOURCE, target)
                if kind == 'file':
                    (target / 'run.py').unlink()
                    (target / 'run.py').symlink_to(builder.SOURCE / 'run.py')
                else:
                    shutil.rmtree(target / 'nova')
                    (target / 'nova').symlink_to(builder.SOURCE / 'nova', target_is_directory=True)
                with self.assertRaises(ValueError):
                    builder.capture(target)

    def test_existing_output_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / 'keep').write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                builder.build(output)
            self.assertEqual((output / 'keep').read_bytes(), b'existing')

    def test_source_drift_after_checks_blocks_persistent_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'build'
            modified = dict(self.candidate)
            modified['run.py'] += b'\n'
            with patch.object(builder, 'capture', side_effect=[self.baseline, self.candidate, self.baseline, modified]), \
                    patch.object(builder, 'isolated_check', return_value={}), \
                    patch.object(builder, 'validate_receipts'):
                with self.assertRaisesRegex(ValueError, 'changed during check'):
                    builder.build(output)
            self.assertFalse(output.exists())

    def test_isolated_actual_zip_boundary_payloads_match_expected_only_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = builder.build(Path(tmp) / 'build')
        before, after = receipt['baseline_offline_checks'], receipt['offline_checks']
        self.assertEqual(after['synthetic_chat_calls'], 21)
        self.assertEqual(after['real_model_calls'], 0)
        self.assertTrue(after['network_denied'])
        self.assertTrue(after['record_no_retrieval'])
        self.assertTrue(after['review_can_keep_exam_despite_say_hint'])
        self.assertEqual([row['actual_hint'] for row in after['boundaries']],
                         ['EXAM', 'SAY', 'SAY', 'SAY', 'SAY', 'SAY', 'SAY'])
        self.assertEqual(before['prompt_sha256'], after['prompt_sha256'])
        self.assertEqual(after['limits'], [30, 180, 30, 200, 100000, 500000, 12, 12])
        builder.validate_receipts(before, after)
        changed = copy.deepcopy(after)
        changed['boundaries'][1]['review_and_record_calls_sha256'] = 'drift'
        with self.assertRaisesRegex(ValueError, 'outside hint'):
            builder.validate_receipts(before, changed)
        changed = copy.deepcopy(after)
        changed['boundaries'][1]['actual_hint'] = 'EXAM'
        with self.assertRaisesRegex(ValueError, 'Wrong actual closing hint boundary'):
            builder.validate_receipts(before, changed)
        self.assertEqual(receipt['sample_submissions'], 0)
        self.assertFalse(receipt['clinical_quality_verified'])


if __name__ == '__main__':
    unittest.main()
