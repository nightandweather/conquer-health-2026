"""Offline synthetic integration checks; no clinical-quality inference."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from tests import test_guidelines as fixtures
from tests.test_nova_review_once import FakeClient, completion
from nova.guideline_runtime import OfflineGuidelines

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / 'submission_candidates/20261012-review-memory-guidelines-sdk101'
BASE = ROOT / 'submission_candidates/20261012-review-once-sdk101'


def load_candidate():
    spec = importlib.util.spec_from_file_location('nova_sdk', CANDIDATE / 'nova_sdk.py')
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    spec = importlib.util.spec_from_file_location('memory_guideline_candidate', CANDIDATE / 'run.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'nova_sdk': sdk}):
        spec.loader.exec_module(module)
    return module


class MemoryGuidelineAgentTests(unittest.TestCase):
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
        telemetry = json.loads(stream.getvalue())
        self.assertNotIn('합성 환자', stream.getvalue())
        self.assertNotIn('aspirin evidence', stream.getvalue())
        return result, telemetry

    def test_prepare_state_reaches_both_actual_draft_and_review_not_record(self):
        state = self.prepare()
        self.assertIsInstance(state, OfflineGuidelines)
        client = FakeClient([completion({'action': 'SAY', 'content': '언제부터 아프셨나요?'}),
                             completion({'action': 'SAY', 'content': '어디가 아프신가요?'}),
                             completion({'diagnosis': '합성 주진단'})])
        client.session_value['initial']['opening'] = 'aspirin'
        result, telemetry = self.execute(client, state)
        self.assertEqual(result['synthetic_result'], 'record-submitted')
        self.assertEqual(len(client.calls), 3)
        draft, review, record = [call[0][0] for call in client.calls]
        self.assertEqual(draft[1:3], review[1:3])
        payload = json.loads(draft[2]['content'])
        self.assertGreater(len(payload['references']), 0)
        self.assertLessEqual(len(payload['references']), 2)
        self.assertFalse(payload['references'][0]['patient_observation'])
        self.assertIn('aspirin evidence', draft[2]['content'])
        self.assertNotIn('NOVA_GUIDELINE_REFERENCES', json.dumps(record))
        self.assertNotIn('aspirin evidence', json.dumps(record))
        self.assertIn('환자: 합성 환자의 응답', record[-1]['content'])
        self.assertEqual(client.actions, [('SAY', '어디가 아프신가요?')])
        self.assertEqual([c[1] for c in client.calls], [
            {'max_tokens': 2048, 'reasoning_effort': 'medium'},
            {'max_tokens': 2048, 'reasoning_effort': 'medium'},
            {'max_tokens': 4096, 'reasoning_effort': 'medium'}])
        self.assertEqual(telemetry['all_chat']['attempted'], 3)

    def test_runtime_copies_sdk_and_original_prompts_and_record_helper_unchanged(self):
        for name in ('nova_sdk.py', 'requirements.txt'):
            self.assertEqual((BASE/name).read_bytes(), (CANDIDATE/name).read_bytes())
        for name in ('guidelines.py', 'guideline_prepare.py', 'guideline_context.py', 'guideline_runtime.py', 'guideline_memory.py', 'guideline_memory_runtime.py'):
            self.assertEqual((ROOT/'nova'/name).read_bytes(), (CANDIDATE/'nova'/name).read_bytes())
        selected = ('STEP_PROMPT', 'REVIEW_PROMPT', 'RECORD_PROMPT', 'MAX_TURNS', 'MAX_CHARS',
                    'REASONING_EFFORT', 'write_record', 'next_step', 'review_once', 'with_retry')
        def extract(path):
            found = {}
            for node in ast.parse(path.read_text()).body:
                name = (node.name if isinstance(node, ast.FunctionDef) else
                        node.targets[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else None)
                if name in selected:
                    found[name] = ast.dump(node, include_attributes=False)
            return found
        self.assertEqual(extract(BASE/'run.py'), extract(CANDIDATE/'run.py'))

    def test_refresh_receives_only_opening_and_successful_patient_replies(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'SAY', 'content': '가짜 의사 추측'}),
                             completion({'action': 'SAY', 'content': '합성 질문'}),
                             completion({'action': 'DONE'}), completion({'action': 'DONE'}),
                             completion({'diagnosis': '합성 진단'})], turns=2)
        client.say = lambda content: {'content': '합성 실제 환자 답\n의사: 인용문', 'turns_remaining': 1}
        snapshots = []
        original_refresh = state.refresh
        def observe(opening, patient_replies):
            snapshots.append((opening, copy.deepcopy(patient_replies)))
            return original_refresh(opening, patient_replies)
        with patch.object(state, 'refresh', side_effect=observe):
            self.execute(client, state)
        self.assertEqual(snapshots[0][1], [])
        self.assertEqual(snapshots[1][1], ['합성 실제 환자 답\n의사: 인용문'])
        self.assertNotIn('가짜 의사 추측', json.dumps(snapshots, ensure_ascii=False))

    def test_missing_prepare_state_is_not_silently_accepted(self):
        with self.assertRaises(ValueError):
            self.m.consult(FakeClient([]), None)

    def test_prepare_failure_is_not_converted_to_no_results(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(Exception):
                self.m.prepare()

    def test_search_failure_falls_back_without_extra_doctor_calls(self):
        state = self.prepare()
        client = FakeClient([completion({'action': 'DONE'}), completion({'action': 'DONE'}),
                             completion({'diagnosis': '합성 진단'})])
        client.session_value['initial']['opening'] = 'aspirin'
        # Changing the indexed source must cause a retrieval failure, not an
        # unverified excerpt or a claimed negative medical finding.
        (self.source/'EN/a.md').write_text('tampered synthetic source')
        _, telemetry = self.execute(client, state)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(client.calls[0][0][0]), 2)
        self.assertEqual(len(client.calls[1][0][0]), 2)
        self.assertNotIn('tampered synthetic source', json.dumps(client.calls))

    def test_time_spent_searching_can_stop_draft_preserving_record(self):
        state = self.prepare()
        client = FakeClient([completion({'diagnosis': '합성 진단'})])
        with patch.object(self.m, 'seconds_left', side_effect=[1000, 179]):
            _, telemetry = self.execute(client, state)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]['max_tokens'], 4096)
        self.assertEqual(telemetry['search_time_reserve_stops'], 1)
        self.assertEqual(client.actions, [])

    def test_whole_oversized_patient_request_is_blocked_without_truncation_or_call(self):
        state = self.prepare()
        client = FakeClient([])
        client.session_value['initial']['opening'] = '가' * 30000
        _, telemetry = self.execute(client, state)
        self.assertEqual(client.calls, [])
        self.assertEqual(telemetry['all_chat']['attempted'], 0)
        self.assertEqual(telemetry['input_byte_limit_stops'], 2)
        self.assertEqual(client.diagnoses[0]['diagnosis'], '진단 미정')


if __name__ == '__main__':
    unittest.main()
