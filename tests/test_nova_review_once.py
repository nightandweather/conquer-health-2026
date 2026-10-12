"""Synthetic contract/dispatch tests, not evidence of Doctor semantic ability."""

import ast
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'submission_candidates' / '20261012-official-baseline-sdk101'
CANDIDATE = ROOT / 'submission_candidates' / '20261012-review-once-sdk101'


def load_candidate():
    sdk_spec = importlib.util.spec_from_file_location('nova_sdk', CANDIDATE / 'nova_sdk.py')
    sdk = importlib.util.module_from_spec(sdk_spec)
    sdk_spec.loader.exec_module(sdk)
    spec = importlib.util.spec_from_file_location('nova_review_once_candidate', CANDIDATE / 'run.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'nova_sdk': sdk}):
        spec.loader.exec_module(module)
    return module


def completion(value, *, finish='stop', usage=True):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    response = {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text},
                             'finish_reason': finish}]}
    if usage:
        response['usage'] = {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25}
    return response


class FakeClient:
    def __init__(self, outputs, *, turns=1, timeout=240):
        self.outputs = list(outputs)
        self.calls, self.actions, self.diagnoses = [], [], []
        self.timeout = timeout
        self.session_value = {'initial': {'opening': '합성 환자의 첫 진술', 'vital_signs': '합성 활력 정보'},
                              'turns_remaining': turns,
                              'deadline': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}

    def session(self):
        return copy.deepcopy(self.session_value)

    def chat(self, *args, **kwargs):
        self.calls.append((copy.deepcopy(args), copy.deepcopy(kwargs)))
        if not self.outputs:
            raise AssertionError('Unexpected extra Doctor call')
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return copy.deepcopy(output)

    def say(self, content):
        self.actions.append(('SAY', content))
        return {'content': '합성 환자의 응답', 'turns_remaining': 0}

    def exam(self, content):
        self.actions.append(('EXAM', content))
        return {'result': '합성 진찰 소견', 'turns_remaining': 0}

    def diagnose(self, **kwargs):
        self.diagnoses.append(copy.deepcopy(kwargs))
        return {'synthetic_result': 'record-submitted'}


class ReviewOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_candidate()

    def consult(self, client):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), patch('socket.socket', side_effect=AssertionError('No network')):
            result = self.m.consult(client, None)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        telemetry = json.loads(lines[0])
        self.assertNotIn('합성 환자', lines[0])
        self.assertNotIn('content', telemetry)
        return result, telemetry

    def review(self, output, draft=('SAY', '언제부터 아프셨나요?'), *, client=None, transcript=None):
        client = client or FakeClient([output])
        telemetry = self.m.new_review_telemetry()
        result = self.m.review_once(client, client.session(), transcript or [], 10, draft, telemetry)
        return result, telemetry, client

    def test_original_sdk_requirements_prompts_helpers_and_settings_unchanged(self):
        for name in ('nova_sdk.py', 'requirements.txt'):
            self.assertEqual((CANDIDATE / name).read_bytes(), (BASE / name).read_bytes())
        baseline = ast.parse((BASE / 'run.py').read_text())
        candidate = ast.parse((CANDIDATE / 'run.py').read_text())
        def selected(tree):
            values = {}
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in (
                    'prepare', 'parse_json', 'with_retry', 'ask_doctor', 'situation',
                    'seconds_left', 'next_step', 'write_record'):
                    values[node.name] = ast.dump(node, include_attributes=False)
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in (
                            'MAX_TURNS', 'TIME_MARGIN', 'MAX_CHARS', 'REASONING_EFFORT',
                            'STEP_PROMPT', 'RECORD_PROMPT', 'STOP_CODES', 'RETRY_CODES'):
                            values[target.id] = ast.dump(node, include_attributes=False)
            return values
        self.assertEqual(selected(baseline), selected(candidate))
        self.assertEqual(self.m.MAX_TURNS, 30)
        self.assertEqual(self.m.MAX_CHARS, 30)
        self.assertEqual(self.m.REASONING_EFFORT, 'medium')

    def test_corrected_action_is_only_action_dispatched_and_record_settings_preserved(self):
        client = FakeClient([completion({'action': 'SAY', 'content': '언제부터 아프셨나요?'}),
                             completion({'action': 'SAY', 'content': '걱정되는 점부터 말씀해 주세요'}),
                             completion({'diagnosis': '합성 주진단'})])
        result, stats = self.consult(client)
        self.assertEqual(result, {'synthetic_result': 'record-submitted'})
        self.assertEqual(client.actions, [('SAY', '걱정되는 점부터 말씀해 주세요')])
        self.assertEqual([call[1] for call in client.calls],
                         [{'max_tokens': 2048, 'reasoning_effort': 'medium'},
                          {'max_tokens': 2048, 'reasoning_effort': 'medium'},
                          {'max_tokens': 4096, 'reasoning_effort': 'medium'}])
        self.assertEqual(stats['review_applied'], 1)
        self.assertEqual(stats['all_chat']['attempted'], 3)
        self.assertEqual([stats['chat_phases'][name]['attempted'] for name in ('draft', 'review', 'record')], [1, 1, 1])
        record_text = client.calls[-1][0][0][1]['content']
        self.assertIn('의사: 걱정되는 점부터 말씀해 주세요', record_text)
        self.assertNotIn('의사: 언제부터 아프셨나요?', record_text)
        self.assertNotIn('실행 전 초안', record_text)

    def test_review_prompt_covers_patient_question_ambiguity_repetition_and_source_contract(self):
        transcript = ['의사: 언제 시작됐나요?', '환자: 어제부터요.',
                      '환자: 그런데 왜 이런 건가요?', '환자: 약은 잘 모르겠어요.']
        original = copy.deepcopy(transcript)
        reviewed, _, client = self.review(completion({'action': 'SAY', 'content': '약 이름을 확인할 수 있나요?'}), transcript=transcript)
        self.assertEqual(reviewed, ('SAY', '약 이름을 확인할 수 있나요?'))
        self.assertEqual(transcript, original)
        system, request = client.calls[0][0][0]
        for text in ('방금 물은 질문', '걱정', '이미 구체적으로 답한 질문', '미응답·모호한 답·서로 모순되는 답',
                     '확인하지 않은', '환자의 말, 실제 진찰 결과', 'DONE 초안도 검토', '30자'):
            self.assertIn(text, system['content'])
        self.assertIn('\n'.join(original), request['content'])
        self.assertIn('남은 턴: 10', request['content'])
        self.assertIn('아직 환자나 도구에 전달하지 않음', request['content'])
        self.assertEqual(client.actions, [])

    def test_synthetic_prior_answer_can_keep_or_replace_draft_without_claiming_semantic_skill(self):
        transcript = ['의사: 언제 시작됐나요?', '환자: 어제 시작했습니다.']
        draft = ('SAY', '언제 시작됐나요?')
        kept, keep_stats, _ = self.review(completion({'action': draft[0], 'content': draft[1]}), draft, transcript=transcript)
        changed, change_stats, _ = self.review(completion({'action': 'SAY', 'content': '어디가 가장 아프신가요?'}), draft, transcript=transcript)
        self.assertEqual(kept, draft)
        self.assertEqual(keep_stats['review_keep'], 1)
        self.assertEqual(changed, ('SAY', '어디가 가장 아프신가요?'))
        self.assertEqual(change_stats['review_applied'], 1)

    def test_done_draft_is_reviewed_and_can_be_reopened_as_say(self):
        client = FakeClient([completion({'action': 'DONE'}),
                             completion({'action': 'SAY', 'content': '더 궁금하신 점이 있나요?'}),
                             completion({'diagnosis': '합성 주진단'})])
        _, stats = self.consult(client)
        self.assertEqual(client.actions, [('SAY', '더 궁금하신 점이 있나요?')])
        self.assertEqual(stats['review_applied'], 1)
        self.assertIn('"action": "DONE"', client.calls[1][0][0][1]['content'])

    def test_review_can_change_say_to_done_and_keep_done(self):
        for draft in ({'action': 'SAY', 'content': '언제 시작됐나요?'}, {'action': 'DONE'}):
            with self.subTest(draft=draft):
                client = FakeClient([completion(draft), completion({'action': 'DONE'}), completion({'diagnosis': '합성 주진단'})])
                _, stats = self.consult(client)
                self.assertEqual(client.actions, [])
                self.assertEqual(stats['review_attempted'], 1)
                self.assertEqual(stats['review_keep'] if draft['action'] == 'DONE' else stats['review_applied'], 1)

    def test_review_can_dispatch_one_exam(self):
        client = FakeClient([completion({'action': 'SAY', 'content': '다른 증상은 있나요?'}),
                             completion({'action': 'EXAM', 'content': '복부 압통을 확인하겠습니다'}),
                             completion({'diagnosis': '합성 주진단'})])
        self.consult(client)
        self.assertEqual(client.actions, [('EXAM', '복부 압통을 확인하겠습니다')])

    def test_invalid_empty_truncated_schema_or_envelope_keeps_draft_without_retry(self):
        invalid = [None, {}, {'choices': []}, {'choices': [1]},
                   completion(''), completion(' '), completion('not JSON'),
                   completion('```json\n{"action":"DONE"}\n```'),
                   completion({'action': 'DONE'}, finish='length'),
                   completion({'action': 'DONE'}, finish=None),
                   completion({'action': 'SAY', 'content': '가' * 31}),
                   completion({'action': 'SAY', 'content': 12}),
                   completion({'action': 'TEST', 'content': '검사'}),
                   completion({'action': 'SAY', 'content': '질문', 'critique': 'extra'}),
                   completion({'action': 'DONE', 'content': ''}),
                   completion('{"action":"DONE","action":"SAY","content":"말"}'),
                   completion([{'action': 'DONE'}])]
        extra = completion({'action': 'DONE'})
        extra['choices'][0]['message']['tool_calls'] = [{'type': 'function'}]
        invalid.append(extra)
        for response in invalid:
            with self.subTest(response=response):
                draft = ('SAY', '언제 시작됐나요?')
                result, stats, client = self.review(response, draft)
                self.assertEqual(result, draft)
                self.assertEqual(stats['review_attempted'], 1)
                self.assertEqual(stats['review_fallback'], 1)
                self.assertEqual(len(client.calls), 1)

    def test_nova_error_review_falls_back_once_while_baseline_retry_still_works(self):
        result, stats, client = self.review(self.m.NovaError('PROVIDER_UNAVAILABLE'))
        self.assertEqual(result, ('SAY', '언제부터 아프셨나요?'))
        self.assertEqual(stats['fallback_reasons'], {'nova_error': 1})
        self.assertEqual(len(client.calls), 1)
        original = FakeClient([self.m.NovaError('PROVIDER_UNAVAILABLE'), completion({'action': 'DONE'})])
        with patch.object(self.m.time, 'sleep') as sleep:
            self.m.ask_doctor(original, self.m.STEP_PROMPT, 'synthetic', 2048)
        self.assertEqual(len(original.calls), 2)
        sleep.assert_called_once_with(2)

    def test_time_reserve_uses_actual_client_timeout_and_skips_without_call(self):
        for timeout, remaining, expected in ((240, 419, 0), (240, 420, 1), (17, 196, 0), (17, 197, 1)):
            with self.subTest(timeout=timeout, remaining=remaining):
                client = FakeClient([completion({'action': 'DONE'})], timeout=timeout)
                with patch.object(self.m, 'seconds_left', return_value=remaining):
                    _, stats, _ = self.review(None, client=client)
                self.assertEqual(len(client.calls), expected)
                self.assertEqual(stats['review_skipped'], 1 - expected)

    def test_invalid_timeout_or_missing_deadline_skip_without_model_request(self):
        for timeout in (None, True, 0, -1, float('inf'), '240'):
            with self.subTest(timeout=timeout):
                _, stats, client = self.review(None, client=FakeClient([], timeout=timeout))
                self.assertEqual(stats['skip_reasons'], {'invalid_client_timeout': 1})
                self.assertEqual(client.calls, [])
        client = FakeClient([])
        client.session_value.pop('deadline')
        _, stats, _ = self.review(None, client=client)
        self.assertEqual(stats['skip_reasons'], {'missing_deadline': 1})

    def test_after_review_record_reserve_stops_patient_dispatch(self):
        client = FakeClient([completion({'action': 'SAY', 'content': '언제 시작됐나요?'}),
                             completion({'action': 'SAY', 'content': '어디가 아프신가요?'}),
                             completion({'diagnosis': '합성 주진단'})])
        with patch.object(self.m, 'seconds_left', side_effect=[1000, 1000, 180]):
            _, stats = self.consult(client)
        self.assertEqual(client.actions, [])
        self.assertEqual(len(client.diagnoses), 1)
        self.assertEqual(stats['record_reserve_stops'], 1)

    def test_reported_usage_and_missing_usage_are_separate(self):
        _, stats, _ = self.review(completion({'action': 'DONE'}))
        self.assertEqual(stats['reported_usage_totals']['completion_tokens'], 5)
        self.assertEqual(stats['reported_usage_field_reports']['completion_tokens'], 1)
        _, unknown, _ = self.review(completion({'action': 'DONE'}, usage=False))
        self.assertEqual(unknown['usage_missing_calls'], 1)
        self.assertEqual(unknown['reported_usage_field_reports']['completion_tokens'], 0)

    def test_proxy_forwards_identical_payload_and_accounts_each_original_retry(self):
        underlying = FakeClient([self.m.NovaError('API_UNAVAILABLE'), completion({'action': 'DONE'})])
        stats = self.m.new_review_telemetry()
        client = self.m._BudgetedClient(underlying, stats)
        with patch.object(self.m.time, 'sleep'):
            self.m.ask_doctor(client, self.m.STEP_PROMPT, 'UNCHANGED SYNTHETIC REQUEST', 2048)
        self.assertEqual(underlying.calls[0], underlying.calls[1])
        self.assertEqual(underlying.calls[0][1], {'max_tokens': 2048, 'reasoning_effort': 'medium'})
        self.assertEqual(stats['all_chat']['attempted'], 2)
        self.assertEqual(stats['all_chat']['errors'], 1)
        self.assertEqual(stats['all_chat']['response_returned'], 1)
        self.assertEqual(stats['all_chat']['unknown_completion_budget_charged'], 2048)
        self.assertEqual(stats['all_chat']['completion_budget_charged'], 2053)
        self.assertEqual(stats['all_chat']['reported_usage_totals']['completion_tokens'], 5)

    def test_proxy_never_sends_201st_call(self):
        underlying = FakeClient([completion({'action': 'DONE'})] * 200)
        stats = self.m.new_review_telemetry()
        client = self.m._BudgetedClient(underlying, stats)
        for _ in range(200):
            client.chat([], max_tokens=2048, reasoning_effort='medium')
        with self.assertRaises(self.m.NovaError) as caught:
            client.chat([], max_tokens=2048, reasoning_effort='medium')
        self.assertEqual(caught.exception.code, 'LOCAL_BUDGET_EXHAUSTED')
        self.assertEqual(len(underlying.calls), 200)
        self.assertEqual(stats['all_chat']['local_blocked'], 1)

    def test_unknown_usage_charged_at_maximum_and_token_budget_blocks_before_call(self):
        underlying = FakeClient([completion({'action': 'DONE'}, usage=False)] * 24)
        stats = self.m.new_review_telemetry()
        client = self.m._BudgetedClient(underlying, stats)
        client.phase = 'record'
        for _ in range(24):
            client.chat([], max_tokens=4096, reasoning_effort='medium')
        with self.assertRaises(self.m.NovaError):
            client.chat([], max_tokens=4096, reasoning_effort='medium')
        self.assertEqual(len(underlying.calls), 24)
        self.assertEqual(stats['all_chat']['completion_budget_charged'], 98304)
        self.assertEqual(stats['all_chat']['unknown_completion_budget_charged'], 98304)
        self.assertEqual(stats['all_chat']['reported_usage_field_reports']['completion_tokens'], 0)

    def test_review_reserves_record_calls_and_one_record_output_budget(self):
        for field, value, reason in (('attempted', 188, 'chat_attempt_reserve'),
                                     ('completion_budget_charged', 93857, 'completion_token_reserve')):
            with self.subTest(field=field):
                original = FakeClient([])
                stats = self.m.new_review_telemetry()
                stats['all_chat'][field] = value
                proxy = self.m._BudgetedClient(original, stats)
                draft = ('SAY', '언제 시작됐나요?')
                self.assertEqual(self.m.review_once(proxy, original.session(), [], 3, draft, stats), draft)
                self.assertEqual(stats['skip_reasons'], {reason: 1})
                self.assertEqual(original.calls, [])

    def test_loop_preserves_call_reserve_for_record(self):
        original = FakeClient([completion({'diagnosis': '합성 주진단'})])
        stats = self.m.new_review_telemetry()
        stats['all_chat']['attempted'] = 177
        proxy = self.m._BudgetedClient(original, stats)
        self.m._consult_with_review(proxy, None, stats)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(original.calls[0][1]['max_tokens'], 4096)
        self.assertEqual(stats['loop_budget_stops'], {'chat_attempt_reserve': 1})
        self.assertEqual(original.actions, [])

    def test_known_zero_completion_is_not_counted_as_unknown(self):
        response = completion({'action': 'DONE'})
        response['usage']['completion_tokens'] = 0
        underlying = FakeClient([response])
        stats = self.m.new_review_telemetry()
        proxy = self.m._BudgetedClient(underlying, stats)
        proxy.chat([], max_tokens=2048)
        self.assertEqual(stats['all_chat']['completion_budget_charged'], 0)
        self.assertEqual(stats['all_chat']['unknown_completion_calls'], 0)
        self.assertEqual(stats['all_chat']['reported_usage_field_reports']['completion_tokens'], 1)

    def test_known_prompt_limit_blocks_new_call_and_unknown_prompt_is_separate(self):
        response = completion({'action': 'DONE'})
        response['usage']['prompt_tokens'] = 500000
        original = FakeClient([response])
        stats = self.m.new_review_telemetry()
        proxy = self.m._BudgetedClient(original, stats)
        proxy.chat([], max_tokens=2048)
        with self.assertRaises(self.m.NovaError):
            proxy.chat([], max_tokens=2048)
        self.assertEqual(len(original.calls), 1)
        self.assertEqual(stats['all_chat']['reported_usage_totals']['prompt_tokens'], 500000)
        self.assertEqual(stats['all_chat']['unknown_prompt_usage_calls'], 0)
        unknown = completion({'action': 'DONE'})
        del unknown['usage']['prompt_tokens']
        unknown_stats = self.m.new_review_telemetry()
        self.m._BudgetedClient(FakeClient([unknown]), unknown_stats).chat([], max_tokens=2048)
        self.assertEqual(unknown_stats['all_chat']['unknown_prompt_usage_calls'], 1)
        self.assertEqual(unknown_stats['all_chat']['reported_usage_field_reports']['prompt_tokens'], 0)

    def test_thirty_action_loop_has_sixty_one_calls_without_extra_review_or_action(self):
        draft = completion({'action': 'SAY', 'content': '합성 질문 하나'})
        record = completion({'diagnosis': '합성 주진단'})
        client = FakeClient([draft, draft] * 30 + [record], turns=50)
        def reply(content):
            client.actions.append(('SAY', content))
            return {'content': '합성 응답', 'turns_remaining': 50}
        client.say = reply
        _, stats = self.consult(client)
        self.assertEqual(len(client.actions), 30)
        self.assertEqual(len(client.calls), 61)
        self.assertEqual(stats['review_attempted'], 30)
        self.assertEqual(stats['review_keep'], 30)
        self.assertEqual(stats['chat_phases']['record']['attempted'], 1)

    def test_each_draft_retry_preserves_one_record_output_budget(self):
        underlying = FakeClient([self.m.NovaError('API_UNAVAILABLE'), completion({'diagnosis': '합성 주진단'}, usage=False)])
        stats = self.m.new_review_telemetry()
        stats['all_chat']['completion_budget_charged'] = 93856
        proxy = self.m._BudgetedClient(underlying, stats)
        with patch.object(self.m.time, 'sleep'):
            with self.assertRaises(self.m.NovaError) as caught:
                self.m.ask_doctor(proxy, self.m.STEP_PROMPT, 'synthetic', 2048)
        self.assertEqual(caught.exception.code, 'LOCAL_BUDGET_EXHAUSTED')
        self.assertEqual(len(underlying.calls), 1)
        self.assertEqual(proxy.remaining_completion_budget, 4096)
        proxy.phase = 'record'
        proxy.chat([], max_tokens=4096, reasoning_effort='medium')
        self.assertEqual(len(underlying.calls), 2)
        self.assertEqual(stats['chat_phases']['draft']['attempted'], 1)
        self.assertEqual(stats['chat_phases']['draft']['local_blocked'], 1)
        self.assertEqual(stats['chat_phases']['record']['attempted'], 1)

    def test_review_known_prompt_limit_skips_without_attempt_or_forward(self):
        underlying = FakeClient([])
        stats = self.m.new_review_telemetry()
        stats['all_chat']['reported_usage_totals']['prompt_tokens'] = 500000
        proxy = self.m._BudgetedClient(underlying, stats)
        draft = ('SAY', '언제 시작됐나요?')
        actual = self.m.review_once(proxy, underlying.session(), [], 5, draft, stats)
        self.assertEqual(actual, draft)
        self.assertEqual(stats['skip_reasons'], {'reported_prompt_token_limit': 1})
        self.assertEqual(stats['review_attempted'], 0)
        self.assertEqual(stats['all_chat']['attempted'], 0)
        self.assertEqual(underlying.calls, [])

    def test_unpaired_surrogates_fall_back_and_valid_escaped_emoji_is_accepted(self):
        for text in ('{"action":"SAY","content":"\\ud800"}',
                     '{"action":"SAY","content":"\\udfff"}',
                     json.dumps({'action': 'SAY', 'content': '\ud800'}, ensure_ascii=False),
                     json.dumps({'action': 'SAY', 'content': '\udfff'}, ensure_ascii=False)):
            with self.subTest(escaped=ascii(text)):
                draft = ('SAY', '언제 시작됐나요?')
                result, stats, client = self.review(completion(text), draft)
                self.assertEqual(result, draft)
                self.assertEqual(stats['fallback_reasons'], {'invalid_unicode': 1})
                self.assertEqual(len(client.calls), 1)
        result, stats, _ = self.review(completion('{"action":"SAY","content":"\\ud83d\\ude42"}'))
        self.assertEqual(result, ('SAY', '🙂'))
        self.assertEqual(stats['review_applied'], 1)

    def test_finish_reason_buckets_and_server_call_count_remain_separate(self):
        responses = [completion({'action': 'DONE'}, finish=value) for value in ('stop', 'length', 'synthetic-sensitive-value', None)]
        client = FakeClient(responses)
        stats = self.m.new_review_telemetry()
        proxy = self.m._BudgetedClient(client, stats)
        for _ in responses:
            proxy.chat([], max_tokens=2048)
        expected = {'stop': 1, 'length': 1, 'other': 1, 'missing': 1}
        self.assertEqual(stats['all_chat']['finish_reason_counts'], expected)
        self.assertEqual(stats['chat_phases']['draft']['finish_reason_counts'], expected)
        self.assertIsNone(stats['server_model_calls'])
        self.assertNotIn('synthetic-sensitive-value', json.dumps(stats))


if __name__ == '__main__':
    unittest.main()
