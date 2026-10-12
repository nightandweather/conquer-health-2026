"""Synthetic parsed-response metadata checks; no real prompts or endpoints."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from benchmarks.response_receipt import (MAX_REPORTED_TOKEN_COUNT, MAX_SELECTED_CHOICE_INDEX,
                                         response_metadata)


def response(content='Synthetic visible answer.', *, finish='stop', usage=None):
    value = {'choices': [{'index': 999, 'message': {'role': 'assistant', 'content': content},
                          'finish_reason': finish}]}
    if usage is not None:
        value['usage'] = usage
    return value


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class ResponseReceiptTests(unittest.TestCase):
    def test_defaults_select_position_zero_not_provider_index(self):
        value = response('SELECTED_SYNTHETIC_TEXT')
        value['choices'].append({'index': 0, 'message': {'content': 'OTHER_SYNTHETIC_TEXT'},
                                 'finish_reason': 'length'})
        result = response_metadata(value)
        self.assertEqual(result['choices']['count'], 2)
        self.assertEqual(result['choices']['selected_position'], 0)
        self.assertEqual(result['content']['utf8_sha256'], sha('SELECTED_SYNTHETIC_TEXT'))
        self.assertEqual(result['finish_reason']['value'], 'stop')
        serialized = json.dumps(result, allow_nan=False)
        self.assertNotIn('SELECTED_SYNTHETIC_TEXT', serialized)
        self.assertNotIn('OTHER_SYNTHETIC_TEXT', serialized)
        self.assertNotIn('999', serialized)

    def test_explicit_position_and_out_of_range_never_fall_back(self):
        value = response('First.')
        value['choices'].append({'message': {'content': 'Second.'}, 'finish_reason': 'length'})
        selected = response_metadata(value, selected_choice_index=1)
        self.assertEqual(selected['content']['utf8_sha256'], sha('Second.'))
        self.assertEqual(selected['finish_reason']['value'], 'length')
        missing = response_metadata(value, selected_choice_index=2)
        self.assertEqual(missing['choices']['selection_state'], 'out_of_range')
        self.assertIsNone(missing['content']['present'])
        self.assertIsNone(missing['content']['utf8_sha256'])
        self.assertIsNone(missing['choices']['selected_json_type'])

    def test_invalid_index_arguments_raise_constant_error(self):
        for index in (-1, True, False, 0.0, 'SECRET_INDEX', None, [], {}):
            with self.subTest(index_type=type(index).__name__):
                with self.assertRaises(ValueError) as caught:
                    response_metadata(response(), selected_choice_index=index)
                self.assertEqual(str(caught.exception), 'selected_choice_index must be a nonnegative exact int')

    def test_malformed_response_types_remain_structural_and_json_serializable(self):
        for value, label in ((None, 'null'), (False, 'boolean'), (0, 'integer'),
                             (1.25, 'number'), ('SECRET_ENVELOPE', 'string'), ([], 'array'),
                             (float('nan'), 'non_json'), (float('inf'), 'non_json')):
            with self.subTest(label=label):
                result = response_metadata(value)
                self.assertEqual(result['response_json_type'], label)
                self.assertFalse(result['response_is_object'])
                self.assertEqual(result['choices']['selection_state'], 'unavailable_response')
                self.assertIsNone(result['usage']['prompt_tokens']['value'])
                self.assertEqual(result['usage']['prompt_tokens']['state'], 'unavailable')
                self.assertNotIn('SECRET_ENVELOPE', json.dumps(result, allow_nan=False))

    def test_missing_null_empty_and_invalid_choices_are_distinct(self):
        variants = [({}, 'missing_choices', False, None),
                    ({'choices': None}, 'invalid_choices_container', True, None),
                    ({'choices': {}}, 'invalid_choices_container', True, None),
                    ({'choices': []}, 'out_of_range', True, 0)]
        for value, state, present, count in variants:
            result = response_metadata(value)
            self.assertEqual(result['choices']['selection_state'], state)
            self.assertIs(result['choices']['present'], present)
            self.assertEqual(result['choices']['count'], count)

    def test_invalid_selected_choice_does_not_use_valid_next_choice(self):
        value = {'choices': ['SECRET_BAD_CHOICE', {'message': {'content': 'Do not fall back.'}}]}
        result = response_metadata(value)
        self.assertEqual(result['choices']['selected_json_type'], 'string')
        self.assertFalse(result['choices']['selected_is_object'])
        self.assertIsNone(result['message']['present'])
        self.assertIsNone(result['content']['present'])
        self.assertNotIn('SECRET_BAD_CHOICE', json.dumps(result))

    def test_missing_null_nonobject_message_are_distinct(self):
        for choice, present, kind in (({}, False, None), ({'message': None}, True, 'null'),
                                      ({'message': []}, True, 'array'),
                                      ({'message': 'SECRET_MESSAGE'}, True, 'string')):
            result = response_metadata({'choices': [choice]})
            self.assertIs(result['message']['present'], present)
            self.assertEqual(result['message']['json_type'], kind)
            self.assertIsNone(result['content']['present'])
            self.assertNotIn('SECRET_MESSAGE', json.dumps(result))

    def test_missing_null_empty_content_are_distinct(self):
        value = response()
        del value['choices'][0]['message']['content']
        missing = response_metadata(value)['content']
        null = response_metadata(response(None))['content']
        empty = response_metadata(response(''))['content']
        self.assertIs(missing['present'], False)
        self.assertIsNone(missing['json_type'])
        self.assertIs(null['present'], True)
        self.assertEqual(null['json_type'], 'null')
        self.assertIsNone(null['character_length'])
        self.assertEqual((empty['character_length'], empty['utf8_byte_length']), (0, 0))
        self.assertEqual(empty['encoding'], 'valid_utf8')
        self.assertEqual(empty['utf8_sha256'], sha(''))

    def test_whitespace_is_measured_without_success_classification(self):
        content = ' \t\n'
        result = response_metadata(response(content))
        self.assertEqual(result['content']['character_length'], 3)
        self.assertEqual(result['content']['utf8_byte_length'], 3)
        self.assertEqual(result['content']['utf8_sha256'], sha(content))
        for forbidden in ('success', 'failed', 'score', 'truncated', 'clinical'):
            self.assertNotIn(forbidden, result)

    def test_multilingual_unicode_lengths_and_hash_strict_utf8(self):
        content = '한글 A🩺e\u0301'
        result = response_metadata(response(content))['content']
        self.assertEqual(result['character_length'], len(content))
        self.assertEqual(result['utf8_byte_length'], len(content.encode('utf-8')))
        self.assertEqual(result['utf8_sha256'], sha(content))
        self.assertEqual(result['encoding'], 'valid_utf8')

    def test_unpaired_surrogate_keeps_character_length_without_replacement_hash(self):
        result = response_metadata(response('x\ud800y'))['content']
        self.assertEqual(result['character_length'], 3)
        self.assertEqual(result['encoding'], 'invalid_utf8')
        self.assertIsNone(result['utf8_byte_length'])
        self.assertIsNone(result['utf8_sha256'])
        json.dumps(result, allow_nan=False)

    def test_nonstring_content_is_not_stringified_or_hashed(self):
        for value, label in ((False, 'boolean'), (0, 'integer'), (1.5, 'number'),
                             (['SECRET_ARRAY'], 'array'), ({'SECRET_KEY': 'SECRET_VALUE'}, 'object')):
            result = response_metadata(response(value))['content']
            self.assertEqual(result['json_type'], label)
            self.assertIsNone(result['character_length'])
            self.assertIsNone(result['utf8_sha256'])
            self.assertNotIn('SECRET', json.dumps(result))

    def test_reasoning_fields_presence_types_lengths_without_text_or_hash(self):
        first, second = 'SECRET_REASONING_한글', 'SECRET_REASONING_CONTENT_🩺'
        value = response(None)
        value['choices'][0]['message'].update(reasoning=first, reasoning_content=second)
        result = response_metadata(value)
        for key, text in (('reasoning', first), ('reasoning_content', second)):
            self.assertTrue(result[key]['present'])
            self.assertEqual(result[key]['json_type'], 'string')
            self.assertEqual(result[key]['character_length'], len(text))
            self.assertEqual(result[key]['utf8_byte_length'], len(text.encode()))
            self.assertFalse(any('hash' in name or 'sha' in name or name == 'text' for name in result[key]))
        serialized = json.dumps(result)
        for forbidden in (first, second, sha(first), sha(second)):
            self.assertNotIn(forbidden, serialized)
        self.assertIsNone(result['content']['character_length'])
        self.assertIsNone(result['content']['utf8_sha256'])

    def test_reasoning_missing_null_empty_invalid_unicode_and_container(self):
        missing = response_metadata(response())['reasoning']
        self.assertFalse(missing['present'])
        for value, label, length, encoding in ((None, 'null', None, None), ('', 'string', 0, 'valid_utf8'),
                                               ('\udfff', 'string', 1, 'invalid_utf8'),
                                               ({'secret': 'value'}, 'object', None, None)):
            item = response()
            item['choices'][0]['message']['reasoning'] = value
            result = response_metadata(item)['reasoning']
            self.assertEqual((result['json_type'], result['character_length'], result['encoding']),
                             (label, length, encoding))
            self.assertNotIn('utf8_sha256', result)

    def test_finish_reason_allowlist_and_structural_unknown_values(self):
        for value in ('stop', 'length'):
            result = response_metadata(response(finish=value))['finish_reason']
            self.assertTrue(result['allowlisted'])
            self.assertEqual(result['value'], value)
        for value in ('SECRET_FINISH', '', None, True, 12, [], {'SECRET_FINISH': 'SECRET_VALUE'}):
            result = response_metadata(response(finish=value))['finish_reason']
            self.assertTrue(result['present'])
            self.assertFalse(result['allowlisted'])
            self.assertIsNone(result['value'])
            self.assertNotIn('SECRET', json.dumps(result))
        item = response()
        del item['choices'][0]['finish_reason']
        result = response_metadata(item)['finish_reason']
        self.assertFalse(result['present'])
        self.assertIsNone(result['allowlisted'])

    def test_valid_exact_nonnegative_counts_including_reported_zero(self):
        usage = {'prompt_tokens': 0, 'completion_tokens': 2, 'total_tokens': 2}
        result = response_metadata(response(usage=usage))['usage']
        for key, count in usage.items():
            self.assertEqual(result[key]['state'], 'reported')
            self.assertEqual(result[key]['value'], count)
        self.assertEqual(result['effective_total_tokens'], {'value': 2, 'provenance': 'reported'})

    def test_missing_usage_container_and_fields_preserve_unknown_not_zero(self):
        missing = response_metadata(response())['usage']
        self.assertFalse(missing['present'])
        self.assertEqual(missing['prompt_tokens']['state'], 'unavailable')
        empty = response_metadata(response(usage={}))['usage']
        self.assertTrue(empty['present'])
        for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            self.assertEqual(empty[key]['state'], 'missing')
            self.assertIsNone(empty[key]['value'])
        self.assertEqual(empty['effective_total_tokens'], {'value': None, 'provenance': 'unknown'})

    def test_invalid_usage_container_preserves_presence_type_without_values(self):
        for value, label in ((None, 'null'), (False, 'boolean'), ([], 'array'), ('SECRET_USAGE', 'string')):
            item = response()
            item['usage'] = value
            result = response_metadata(item)['usage']
            self.assertTrue(result['present'])
            self.assertEqual(result['json_type'], label)
            self.assertFalse(result['is_object'])
            self.assertEqual(result['total_tokens']['state'], 'unavailable')
            self.assertNotIn('SECRET_USAGE', json.dumps(result))

    def test_bool_negative_float_string_null_and_nonfinite_counts_are_invalid(self):
        for value in (True, False, -1, 1.0, '22', None, [], {}, float('nan'), float('inf')):
            result = response_metadata(response(usage={'prompt_tokens': value}))['usage']['prompt_tokens']
            self.assertTrue(result['present'])
            self.assertEqual(result['state'], 'invalid')
            self.assertIsNone(result['value'])
            json.dumps(result, allow_nan=False)

    def test_missing_total_derives_but_original_missing_state_stays(self):
        result = response_metadata(response(usage={'prompt_tokens': 5, 'completion_tokens': 7}))['usage']
        self.assertEqual(result['total_tokens']['state'], 'missing')
        self.assertFalse(result['total_tokens']['present'])
        self.assertIsNone(result['total_tokens']['value'])
        self.assertEqual(result['effective_total_tokens'], {'value': 12, 'provenance': 'derived_prompt_plus_completion'})

    def test_invalid_total_derives_but_invalid_state_and_type_stay(self):
        result = response_metadata(response(usage={'prompt_tokens': 5, 'completion_tokens': 7,
                                                    'total_tokens': True}))['usage']
        self.assertEqual(result['total_tokens']['state'], 'invalid')
        self.assertEqual(result['total_tokens']['json_type'], 'boolean')
        self.assertIsNone(result['total_tokens']['value'])
        self.assertEqual(result['effective_total_tokens'], {'value': 12, 'provenance': 'derived_prompt_plus_completion'})

    def test_total_never_derives_from_unknown_or_invalid_parts(self):
        for usage in ({'prompt_tokens': 5}, {'completion_tokens': 5},
                      {'prompt_tokens': False, 'completion_tokens': 5}):
            self.assertEqual(response_metadata(response(usage=usage))['usage']['effective_total_tokens'],
                             {'value': None, 'provenance': 'unknown'})

    def test_reported_total_wins_without_silent_reconciliation(self):
        result = response_metadata(response(usage={'prompt_tokens': 5, 'completion_tokens': 7,
                                                   'total_tokens': 99}))['usage']
        self.assertEqual(result['total_tokens']['value'], 99)
        self.assertEqual(result['effective_total_tokens'], {'value': 99, 'provenance': 'reported'})

    def test_nested_counts_allowlist_and_missing_invalid_provenance(self):
        usage = {'completion_tokens_details': {'reasoning_tokens': 3, 'SECRET_EXTRA': 'SECRET_VALUE'},
                 'prompt_tokens_details': {'cached_tokens': 0, 'audio_tokens': 12345}}
        result = response_metadata(response(usage=usage))['usage']
        self.assertEqual(result['completion_tokens_details']['reasoning_tokens']['value'], 3)
        self.assertEqual(result['prompt_tokens_details']['cached_tokens']['value'], 0)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('audio_tokens', json.dumps(result))
        item = response(usage={'completion_tokens_details': {'reasoning_tokens': False},
                               'prompt_tokens_details': []})
        result = response_metadata(item)['usage']
        self.assertEqual(result['completion_tokens_details']['reasoning_tokens']['state'], 'invalid')
        self.assertEqual(result['prompt_tokens_details']['cached_tokens']['state'], 'unavailable')
        absent = response_metadata(response(usage={}))['usage']['prompt_tokens_details']
        self.assertFalse(absent['present'])
        self.assertIsNone(absent['cached_tokens']['value'])

    def test_input_unchanged_and_output_containers_independent(self):
        item = response(usage={'prompt_tokens': 3, 'completion_tokens': 4,
                               'completion_tokens_details': {'reasoning_tokens': 2}})
        before = deepcopy(item)
        result = response_metadata(item)
        self.assertEqual(item, before)
        result['usage']['prompt_tokens']['value'] = 900
        result['choices']['count'] = 900
        self.assertEqual(item, before)
        item['usage']['completion_tokens_details']['reasoning_tokens'] = 700
        self.assertEqual(result['usage']['completion_tokens_details']['reasoning_tokens']['value'], 2)

    def test_unknown_ids_roles_keys_and_other_choices_never_echo(self):
        item = response('SELECTED_CONTENT_SECRET')
        item.update(id='SECRET_ID', model='SECRET_MODEL', arbitrary={'SECRET_KEY': 'SECRET_VALUE'})
        item['choices'][0]['message']['role'] = 'SECRET_ROLE'
        item['choices'][0]['message']['arbitrary'] = 'SECRET_MESSAGE_FIELD'
        item['choices'].append({'message': {'reasoning': 'UNSELECTED_REASONING',
                                          'content': 'UNSELECTED_CONTENT'}})
        serialized = json.dumps(response_metadata(item), allow_nan=False)
        for text in ('SECRET', 'UNSELECTED', 'arbitrary'):
            self.assertNotIn(text, serialized)

    def test_nonjson_objects_and_subclasses_are_not_interrogated(self):
        class SecretType:
            def __str__(self):
                raise AssertionError('String conversion prohibited')
            def __repr__(self):
                raise AssertionError('Representation prohibited')
        class SecretDict(dict):
            def __contains__(self, _):
                raise AssertionError('Subclass traversal prohibited')
            def __len__(self):
                raise AssertionError('Subclass length prohibited')
        class SecretString(str):
            def encode(self, *args, **kwargs):
                raise AssertionError('Subclass encoding prohibited')
        for item in (SecretType(), SecretDict(), SecretString('SECRET')):
            result = response_metadata(item)
            self.assertEqual(result['response_json_type'], 'non_json')
            self.assertNotIn('Secret', json.dumps(result))
        result = response_metadata(response(SecretString('SECRET')))['content']
        self.assertEqual(result['json_type'], 'non_json')
        self.assertIsNone(result['utf8_sha256'])

    def test_custom_metaclass_hash_or_equality_is_not_invoked_for_type_labels(self):
        class Meta(type):
            def __hash__(self):
                raise AssertionError('Do not hash an untrusted class')
            def __eq__(self, _):
                raise AssertionError('Do not compare an untrusted class')
        class Opaque(metaclass=Meta):
            pass
        result = response_metadata(Opaque())
        self.assertEqual(result['response_json_type'], 'non_json')
        item = response(Opaque())
        item['choices'][0]['finish_reason'] = Opaque()
        self.assertEqual(response_metadata(item)['content']['json_type'], 'non_json')

    def test_nonjson_object_keys_do_not_trigger_custom_equality(self):
        class CollidingKey:
            def __hash__(self):
                return hash('choices')
            def __eq__(self, _):
                raise AssertionError('Do not interrogate non-JSON object keys')
        result = response_metadata({CollidingKey(): 'SECRET_VALUE'})
        self.assertFalse(result['choices']['present'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_index_bound_accepts_max_and_rejects_overflow_without_stringifying(self):
        self.assertEqual(MAX_SELECTED_CHOICE_INDEX, 2**63 - 1)
        result = response_metadata({}, selected_choice_index=MAX_SELECTED_CHOICE_INDEX)
        self.assertEqual(result['choices']['selected_position'], MAX_SELECTED_CHOICE_INDEX)
        json.dumps(result, allow_nan=False)
        for value in (MAX_SELECTED_CHOICE_INDEX + 1, 10**4300):
            with self.assertRaises(ValueError) as caught:
                response_metadata({}, selected_choice_index=value)
            self.assertEqual(str(caught.exception), 'selected_choice_index must be a nonnegative exact int')

    def test_overflow_reported_count_is_invalid_null_and_does_not_mutate_input(self):
        self.assertEqual(MAX_REPORTED_TOKEN_COUNT, 2**63 - 1)
        for count in (MAX_REPORTED_TOKEN_COUNT + 1, 10**4300):
            item = response(usage={'prompt_tokens': count, 'completion_tokens': 1,
                                   'total_tokens': count,
                                   'completion_tokens_details': {'reasoning_tokens': count},
                                   'prompt_tokens_details': {'cached_tokens': count}})
            before = deepcopy(item)
            result = response_metadata(item)
            self.assertEqual(item, before)
            for reported in (result['usage']['prompt_tokens'], result['usage']['total_tokens'],
                             result['usage']['completion_tokens_details']['reasoning_tokens'],
                             result['usage']['prompt_tokens_details']['cached_tokens']):
                self.assertEqual(reported['state'], 'invalid')
                self.assertEqual(reported['json_type'], 'integer')
                self.assertIsNone(reported['value'])
            self.assertEqual(result['usage']['effective_total_tokens'],
                             {'value': None, 'provenance': 'unknown'})
            json.dumps(result, allow_nan=False)

    def test_previously_serializable_4300_digit_parts_are_not_added_to_overflowing_total(self):
        count = 10**4300 - 1
        item = response(usage={'prompt_tokens': count, 'completion_tokens': count})
        before = deepcopy(item)
        result = response_metadata(item)
        self.assertEqual(item, before)
        self.assertEqual(result['usage']['prompt_tokens']['state'], 'invalid')
        self.assertEqual(result['usage']['completion_tokens']['state'], 'invalid')
        self.assertEqual(result['usage']['total_tokens']['state'], 'missing')
        self.assertEqual(result['usage']['effective_total_tokens'],
                         {'value': None, 'provenance': 'unknown'})
        json.dumps(result, allow_nan=False)

    def test_maximum_valid_counts_derive_exact_larger_sum_with_original_total_preserved(self):
        for raw_total in (None, MAX_REPORTED_TOKEN_COUNT + 1, 10**4300):
            usage = {'prompt_tokens': MAX_REPORTED_TOKEN_COUNT,
                     'completion_tokens': MAX_REPORTED_TOKEN_COUNT}
            if raw_total is not None:
                usage['total_tokens'] = raw_total
            item = response(usage=usage)
            before = deepcopy(item)
            result = response_metadata(item)
            self.assertEqual(item, before)
            self.assertEqual(result['usage']['total_tokens']['state'],
                             'missing' if raw_total is None else 'invalid')
            self.assertIsNone(result['usage']['total_tokens']['value'])
            self.assertEqual(result['usage']['effective_total_tokens'],
                             {'value': 2**64 - 2, 'provenance': 'derived_prompt_plus_completion'})
            json.dumps(result, allow_nan=False)
        reported = response_metadata(response(usage={'total_tokens': MAX_REPORTED_TOKEN_COUNT}))
        self.assertEqual(reported['usage']['total_tokens']['state'], 'reported')
        self.assertEqual(reported['usage']['effective_total_tokens'],
                         {'value': MAX_REPORTED_TOKEN_COUNT, 'provenance': 'reported'})

    def test_helper_has_no_file_network_or_runtime_calls(self):
        with patch('builtins.open', side_effect=AssertionError('No file I/O')),\
             patch('socket.socket', side_effect=AssertionError('No network')):
            result = response_metadata(response())
        self.assertEqual(result['content']['encoding'], 'valid_utf8')


if __name__ == '__main__':
    unittest.main()
