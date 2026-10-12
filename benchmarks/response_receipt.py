"""Pure structural metadata for an already parsed chat response.

No transport, I/O, retry, success/clinical judgment or runtime integration is
performed. This helper cannot recover a missing raw response or establish why
an answer was empty. It preserves reported token metadata as provider reports,
not as measurements independently verified by this helper.

Only exact built-in JSON container/scalar types are traversed. Non-JSON objects
(including subclasses) receive a fixed type label without invoking their string
conversion, attribute access, iteration, or length methods. Returned containers
are newly allocated and contain only fixed labels, numeric metadata, an allowed
finish value, and a strict-UTF8 hash of the selected answer content. Reasoning
text is never copied or hashed.
"""
from __future__ import annotations

import hashlib
import math


# Observation-validation ranges only; these are not model limits or budgets.
MAX_REPORTED_TOKEN_COUNT = 2**63 - 1
MAX_SELECTED_CHOICE_INDEX = 2**63 - 1


_JSON_TYPES = ((type(None), 'null'), (bool, 'boolean'), (int, 'integer'),
               (float, 'number'), (str, 'string'), (list, 'array'), (dict, 'object'))


def _json_type(value):
    kind = type(value)
    if kind is float and not math.isfinite(value):
        return 'non_json'
    for known_type, label in _JSON_TYPES:
        if kind is known_type:
            return label
    return 'non_json'


def _field(parent, key):
    """Missing is known only for an available exact dict parent."""
    if type(parent) is not dict:
        return None, None
    # Iterate exact dict storage so non-JSON keys cannot trigger custom equality
    # through a deliberately colliding hash during a normal dict lookup.
    for stored_key, value in parent.items():
        if type(stored_key) is str and stored_key == key:
            return True, value
    return False, None


def _description(parent, key):
    present, value = _field(parent, key)
    return {'present': present, 'json_type': _json_type(value) if present else None}, value


def _text_metadata(parent, key, *, include_hash):
    result, value = _description(parent, key)
    result.update(character_length=None, utf8_byte_length=None, encoding=None)
    if include_hash:
        result['utf8_sha256'] = None
    if result['present'] is True and type(value) is str:
        result['character_length'] = len(value)
        try:
            encoded = value.encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            result['encoding'] = 'invalid_utf8'
        else:
            result.update(utf8_byte_length=len(encoded), encoding='valid_utf8')
            if include_hash:
                result['utf8_sha256'] = hashlib.sha256(encoded).hexdigest()
    return result


def _count(parent, key):
    result, value = _description(parent, key)
    if result['present'] is None:
        state = 'unavailable'
    elif result['present'] is False:
        state = 'missing'
    elif type(value) is int and 0 <= value <= MAX_REPORTED_TOKEN_COUNT:
        state = 'reported'
    else:
        state = 'invalid'
    result.update(state=state, value=value if state == 'reported' else None)
    return result


def response_metadata(response, *, selected_choice_index=0):
    """Return allowlisted JSON-serializable metadata, never response text.

    ``selected_choice_index`` is a zero-based list position, never a provider's
    ``index`` field. Its accepted range is 0..MAX_SELECTED_CHOICE_INDEX.
    An invalid argument raises the same fixed ValueError; an unavailable
    position remains unavailable without selecting another choice. Presence
    ``None`` means the parent container was unavailable, False means a missing
    key in an available dict, and True includes explicitly null values.

    Reported counts accept exact ints in 0..MAX_REPORTED_TOKEN_COUNT; larger
    values are invalid metadata and never copied. These observation-validation
    ranges do not describe model limits or budgets. Counts retain their original
    state/value. Effective total uses a valid
    reported total preferentially, otherwise derives only from valid reported
    prompt and completion counts, with an exact sum up to 2**64-2. Unknown counts
    stay None. No reconciliation
    of conflicting reported totals or validation of provider accuracy occurs.
    """
    if (type(selected_choice_index) is not int
            or not 0 <= selected_choice_index <= MAX_SELECTED_CHOICE_INDEX):
        raise ValueError('selected_choice_index must be a nonnegative exact int')

    choices_info, choices = _description(response, 'choices')
    choices_info['count'] = len(choices) if type(choices) is list else None
    choices_info['selected_position'] = selected_choice_index
    choice = None
    if choices_info['present'] is None:
        selection_state = 'unavailable_response'
    elif choices_info['present'] is False:
        selection_state = 'missing_choices'
    elif type(choices) is not list:
        selection_state = 'invalid_choices_container'
    elif selected_choice_index >= len(choices):
        selection_state = 'out_of_range'
    else:
        selection_state = 'available'
        choice = choices[selected_choice_index]
    choices_info.update(selection_state=selection_state,
                        selected_json_type=_json_type(choice) if selection_state == 'available' else None,
                        selected_is_object=type(choice) is dict if selection_state == 'available' else None)
    message_info, message = _description(choice, 'message')
    message_info['is_object'] = type(message) is dict if message_info['present'] is True else None
    finish_info, finish = _description(choice, 'finish_reason')
    recognized = type(finish) is str and finish in ('stop', 'length') if finish_info['present'] is True else None
    finish_info.update(allowlisted=recognized,
                       value=('stop' if finish == 'stop' else 'length') if recognized else None)

    usage_info, usage = _description(response, 'usage')
    usage_info['is_object'] = type(usage) is dict if usage_info['present'] is True else None
    counts = {key: _count(usage, key) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
    details = {}
    for container, key in (('completion_tokens_details', 'reasoning_tokens'),
                           ('prompt_tokens_details', 'cached_tokens')):
        description, nested = _description(usage, container)
        description['is_object'] = type(nested) is dict if description['present'] is True else None
        description[key] = _count(nested, key)
        details[container] = description
    total = counts['total_tokens']
    if total['state'] == 'reported':
        effective = {'value': total['value'], 'provenance': 'reported'}
    elif all(counts[key]['state'] == 'reported' for key in ('prompt_tokens', 'completion_tokens')):
        effective = {'value': counts['prompt_tokens']['value'] + counts['completion_tokens']['value'],
                     'provenance': 'derived_prompt_plus_completion'}
    else:
        effective = {'value': None, 'provenance': 'unknown'}

    return {'schema_version': 1, 'response_json_type': _json_type(response),
            'response_is_object': type(response) is dict, 'choices': choices_info,
            'message': message_info, 'content': _text_metadata(message, 'content', include_hash=True),
            'reasoning': _text_metadata(message, 'reasoning', include_hash=False),
            'reasoning_content': _text_metadata(message, 'reasoning_content', include_hash=False),
            'finish_reason': finish_info,
            'usage': {**usage_info, **counts, **details, 'effective_total_tokens': effective}}
