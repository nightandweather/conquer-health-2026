"""Bounded offline references for draft/review requests, never patient evidence.

Uses only the supplied prepared index. Lexical search has no Korean morphology,
translation, or validated clinical relevance. Limits below are serialized UTF-8
bytes, not tokenizer counts or clinical guarantees. The caller owns session-time
checks and must not augment final-record requests with references.
"""
from __future__ import annotations

import copy
import json
import re
import time

from .guideline_context import prepare_guideline_request
from .guidelines import MAX_QUERY_BYTES, MAX_QUERY_TERMS

MAX_MESSAGE_BYTES = 65536
MAX_REFERENCE_MESSAGE_BYTES = 8192


class InputSizeError(ValueError):
    """Base messages are invalid text messages or exceed the serialized limit."""


def _serialized_bytes(value):
    # Match SDK 1.0.1's default JSON separators for the messages array.
    # This does not include the outer HTTP body or equal tokenizer counts.
    return json.dumps(value, ensure_ascii=False).encode('utf-8', errors='strict')


def validate_base_messages(messages):
    """Validate without modifying input; covers the entire messages JSON array."""
    if not isinstance(messages, list) or not messages:
        raise InputSizeError('Messages must be a nonempty list')
    if any(not isinstance(m, dict) or not isinstance(m.get('role'), str)
           or m.get('role') not in {'system', 'user', 'assistant'}
           or not isinstance(m.get('content'), str) for m in messages):
        raise InputSizeError('Only text system/user/assistant messages are supported')
    try:
        size = len(_serialized_bytes(messages))
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InputSizeError('Messages cannot be serialized as UTF-8 JSON') from exc
    if size > MAX_MESSAGE_BYTES:
        raise InputSizeError('Base messages exceed serialized UTF-8 byte limit')


def _query_from_patient_text(opening, patient_replies):
    if not isinstance(opening, str) or not isinstance(patient_replies, list):
        raise ValueError('Expected opening text and patient reply list')
    if any(not isinstance(reply, str) for reply in patient_replies):
        raise ValueError('Patient replies must be text')
    # Reserve a share for each recent reply so a long opening cannot crowd
    # both out. Keep whole lexical terms: no inference or translation.
    texts = [opening, *patient_replies[-2:]]
    sources = [[m.group(0) for m in re.finditer(r'[^\W_]+', t.casefold())]
               for t in texts]
    terms, seen = [], set()
    def add(term):
        if term in seen or len(terms) >= MAX_QUERY_TERMS:
            return False
        seen.add(term)
        if len((' '.join([*terms, term])).encode('utf-8')) > MAX_QUERY_BYTES:
            return False
        terms.append(term)
        return True
    quota = MAX_QUERY_TERMS // len(sources)
    for source in sources:
        added = 0
        for term in source:
            added += int(add(term))
            if added == quota:
                break
    for source in sources:
        for term in source:
            add(term)
    return ' '.join(terms), len(terms)
    return ' '.join(terms), len(terms)


class OfflineGuidelines:
    """Refresh once per action; reuse the cached references across chat retries.

    Call refresh with the opening and patient replies only. A search/verification
    failure clears old references and is counted separately from successful zero
    hits. No patient strings, queries, excerpts, paths, or error text are placed
    in telemetry. Caller must explicitly keep record-phase requests unaugmented.
    """
    def __init__(self, prepared: dict):
        if not isinstance(prepared, dict) or any(
                not isinstance(prepared.get(k), str) or not prepared[k]
                for k in ('index_path', 'source_dir')):
            raise ValueError('Prepared index_path and source_dir are required')
        self._db_path = prepared['index_path']
        self._source_dir = prepared['source_dir']
        self._reference_messages = []
        self.telemetry = {
            'refresh_attempts': 0, 'search_attempts': 0,
            'refresh_status_counts': {}, 'last_refresh_status': 'not_refreshed',
            'refresh_elapsed_seconds': 0.0, 'last_query_term_count': 0,
            'last_query_utf8_bytes': 0, 'current_reference_count': 0,
            'current_reference_message_utf8_bytes': 0,
            'augment_attempts': 0, 'augmented_requests': 0,
            'augment_status_counts': {}, 'last_augment_status': 'not_attempted',
            'base_input_errors': 0,
        }

    def _refresh_status(self, status):
        self.telemetry['last_refresh_status'] = status
        counts = self.telemetry['refresh_status_counts']
        counts[status] = counts.get(status, 0) + 1

    def _augment_status(self, status):
        self.telemetry['last_augment_status'] = status
        counts = self.telemetry['augment_status_counts']
        counts[status] = counts.get(status, 0) + 1

    def refresh(self, opening: str, patient_replies: list[str]):
        started = time.monotonic()
        self.telemetry['refresh_attempts'] += 1
        self._reference_messages = []
        for key in ('current_reference_count', 'current_reference_message_utf8_bytes',
                    'last_query_term_count', 'last_query_utf8_bytes'):
            self.telemetry[key] = 0
        try:
            try:
                query, count = _query_from_patient_text(opening, patient_replies)
            except (TypeError, ValueError, UnicodeError):
                self._refresh_status('invalid_query_input')
                return
            self.telemetry['last_query_term_count'] = count
            self.telemetry['last_query_utf8_bytes'] = len(query.encode('utf-8'))
            if not query:
                self._refresh_status('empty_query')
                return
            self.telemetry['search_attempts'] += 1
            try:
                # The empty placeholder contains no clinical content and is
                # removed. Existing helper performs source/hash verification.
                prepared = prepare_guideline_request(
                    [{'role': 'user', 'content': ''}], query, self._db_path,
                    source_dir=self._source_dir, limit=2, max_reference_chars=3200,
                )
                additions = prepared['messages'][:-1]
                count = prepared['reference_count']
                if count == 0:
                    self._refresh_status('no_hits')
                    return
                size = len(_serialized_bytes(additions))
                if size > MAX_REFERENCE_MESSAGE_BYTES and count > 1:
                    # Remove whole lower-ranked excerpts, never rewrite source
                    # text or byte offsets. Korean UTF-8 often needs this path.
                    context = json.loads(additions[1]['content'])
                    while size > MAX_REFERENCE_MESSAGE_BYTES and len(context['references']) > 1:
                        context['references'].pop()
                        additions[1]['content'] = json.dumps(context, ensure_ascii=False,
                                                            sort_keys=True, separators=(',', ':'))
                        size = len(_serialized_bytes(additions))
                    count = len(context['references'])
                if size > MAX_REFERENCE_MESSAGE_BYTES:
                    self._refresh_status('reference_message_size_excluded')
                    return
            except Exception:
                # Do not serialize the exception: it may contain paths/text.
                # Verification failure is NOT reported as a successful no-hit.
                self._refresh_status('search_or_verification_error')
                return
            self._reference_messages = copy.deepcopy(additions)
            self.telemetry['current_reference_count'] = count
            self.telemetry['current_reference_message_utf8_bytes'] = size
            self._refresh_status('references_ready')
        finally:
            self.telemetry['refresh_elapsed_seconds'] += max(0.0, time.monotonic() - started)

    def augment(self, messages: list[dict]) -> list[dict]:
        self.telemetry['augment_attempts'] += 1
        try:
            validate_base_messages(messages)
        except InputSizeError:
            self.telemetry['base_input_errors'] += 1
            self._augment_status('base_input_error')
            raise
        base = copy.deepcopy(messages)
        if not self._reference_messages:
            self._augment_status('no_cached_references')
            return base
        leading_systems = 0
        for message in base:
            if message['role'] != 'system':
                break
            leading_systems += 1
        candidate = (base[:leading_systems] + copy.deepcopy(self._reference_messages)
                     + base[leading_systems:])
        if len(_serialized_bytes(candidate)) > MAX_MESSAGE_BYTES:
            self._augment_status('full_message_size_excluded')
            return base
        self.telemetry['augmented_requests'] += 1
        self._augment_status('references_added')
        return candidate
