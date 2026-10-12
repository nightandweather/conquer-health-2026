"""Use an in-memory index with E02 reference boundaries; never store a disk DB."""
import copy
import hashlib
import json
import time
from .guideline_context import REFERENCE_POLICY
from .guideline_runtime import (OfflineGuidelines, MAX_REFERENCE_MESSAGE_BYTES,
                               _serialized_bytes, _query_from_patient_text)


class MemoryGuidelines(OfflineGuidelines):
    def __init__(self, index):
        super().__init__({'index_path': ':memory:', 'source_dir': index.source_dir})
        self.index = index
        self.telemetry['index_storage'] = 'sqlite_memory_only'

    def close(self):
        self.index.close()

    def _prepare_context(self, query):
        retrieval = self.index.search(query, limit=2)
        references, chars = [], 0
        for result in retrieval['results']:
            if (result.get('reference_scope') != 'general_medical_reference'
                    or result.get('patient_observation') is not False
                    or result.get('source_bytes_verified') is not True):
                raise ValueError('Unverified reference provenance')
            snippet = result['snippet']
            if hashlib.sha256(snippet.encode('utf-8')).hexdigest() != result['snippet_sha256']:
                raise ValueError('Reference excerpt hash mismatch')
            if chars + len(snippet) > 3200:
                break
            reference = {key: result[key] for key in (
                'document_id', 'title', 'organization', 'year', 'language',
                'source_url', 'license', 'document_type', 'source_sha256',
                'snippet', 'byte_start', 'byte_end', 'snippet_sha256')}
            reference.update(reference_scope='general_medical_reference', patient_observation=False)
            references.append(reference)
            chars += len(snippet)
        context = {'schema_version': 1, 'kind': 'NOVA_GUIDELINE_REFERENCES', 'references': references}
        return {'reference_count': len(references), 'messages': [
            {'role': 'system', 'content': REFERENCE_POLICY},
            {'role': 'user', 'content': json.dumps(context, ensure_ascii=False,
                                                 sort_keys=True, separators=(',', ':'))},
            {'role': 'user', 'content': ''}]}

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
                prepared = self._prepare_context(query)
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

