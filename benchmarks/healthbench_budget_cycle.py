"""Run one pinned development20 medium4096 candidate against cached medium2048.

The general regeneration runner remains fixed at its default 2048 budget. This
narrow runner requires both external source pins and never resumes or retries.
Use a fresh dedicated output directory with one writer; preparation, generation
and independent quality review are separate states.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import stat
import sys
import time

from . import healthbench_budget_preflight as gate
from . import healthbench_review as review
from .healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, RequestFailure,
                          Transport, canonical_hash, sum_usage, utc_now)
from .healthbench_codex_cycle import REVIEW_REQUIRED, generate_candidate, make_payload
from .healthbench_score import _data_index

CANDIDATE_ARM = 'grounded_medium_budget4096_v1'
PAIRED_ARMS = (gate.ARM, CANDIDATE_ARM)
TRANSPORT_CODES = {'http_error', 'connection_error', 'response_too_large',
                   'invalid_json_response', 'invalid_provider_response'}


class _Blocked(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _ArtifactChanged(_Blocked):
    def __init__(self, evidence):
        self.evidence = evidence
        super().__init__('run_artifact_changed')


class _SafeTransport:
    def __init__(self, client):
        self.client, self.calls, self.metadata_calls, self.interrupted = client, 0, 0, False
        self.last_digest, self.metadata_status = None, 'not_requested'
        self.expected_request_sha256 = None

    def complete(self, payload):
        if canonical_hash(payload) != self.expected_request_sha256:
            raise _Blocked('candidate_request_changed')
        self.calls += 1
        try:
            return self.client.complete(payload)
        except KeyboardInterrupt:
            self.interrupted = True
            raise RequestFailure('interrupted') from None
        except RequestFailure as exc:
            code = exc.code if exc.code in TRANSPORT_CODES else 'transport_failure'
            status = exc.http_status if type(exc.http_status) is int and 100 <= exc.http_status <= 599 else None
            raise RequestFailure(code, status) from None
        except Exception:
            raise RequestFailure('transport_failure') from None

    def metadata(self, expected_digest):
        self.metadata_calls += 1
        self.last_digest, self.metadata_status = None, 'unavailable'
        try:
            value = self.client.model_metadata(DEFAULT_MODEL)
        except Exception:
            raise _Blocked('model_metadata_unavailable') from None
        if (not isinstance(value, dict) or value.get('name') != DEFAULT_MODEL
                or not gate._valid_sha(value.get('digest'))):
            raise _Blocked('model_metadata_unavailable')
        if value['digest'] != expected_digest:
            self.last_digest, self.metadata_status = value['digest'], 'digest_mismatch'
            raise _Blocked('model_digest_changed')
        self.last_digest, self.metadata_status = value['digest'], 'available'


class _Files:
    """Write only captured run-directory/file identities through directory FDs."""

    def __init__(self, root, root_manifest):
        self.root = gate._path(root)
        self.descriptors = {(): os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)}
        self.root_identity = gate._inode(os.fstat(self.descriptors[()]))
        self.files = {((), 'manifest.json'): gate._inode(os.stat('manifest.json', dir_fd=self.descriptors[()], follow_symlinks=False))}
        self.stamps = {((), 'manifest.json'): gate._stamp(os.stat('manifest.json', dir_fd=self.descriptors[()], follow_symlinks=False))}
        self.identities = {(): self.root_identity}
        self.expected = {((), 'manifest.json'): (json.dumps(root_manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')}
        self.verify()

    def check(self):
        gate._require(gate._path(self.root) == self.root
                      and gate._inode(self.root.stat()) == self.root_identity, 'Run output directory changed')
        for parts, fd in self.descriptors.items():
            if parts:
                current = os.stat(parts[-1], dir_fd=self.descriptors[parts[:-1]], follow_symlinks=False)
                gate._require(stat.S_ISDIR(current.st_mode) and gate._inode(current) == self.identities[parts]
                              and gate._inode(os.fstat(fd)) == self.identities[parts], 'Run child directory changed')

    def directory(self, parts):
        if parts in self.descriptors:
            return self.descriptors[parts]
        parent = self.directory(parts[:-1])
        os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
        captured = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        gate._require(stat.S_ISDIR(captured.st_mode) and gate._inode(captured) == gate._inode(os.fstat(fd))
                      and stat.S_IMODE(captured.st_mode) == 0o700, 'New run child directory changed')
        self.descriptors[parts], self.identities[parts] = fd, gate._inode(captured)
        self.check()
        return fd

    def write(self, relative, raw, *, append=False):
        self.check()
        path = Path(relative)
        gate._require(not path.is_absolute() and '..' not in path.parts, 'Invalid run artifact path')
        parts, name = path.parts[:-1], path.name
        parent = self.directory(parts)
        key = (parts, name)
        if key in self.files:
            self.verify([key])
        flags = os.O_WRONLY | os.O_NOFOLLOW
        if key not in self.files:
            flags |= os.O_CREAT | os.O_EXCL
        if append:
            flags |= os.O_APPEND
        fd = os.open(name, flags, 0o600, dir_fd=parent)
        try:
            identity = gate._inode(os.fstat(fd))
            gate._require(key not in self.files or identity == self.files[key], 'Run artifact identity changed')
            self.files[key] = identity
            os.fchmod(fd, 0o600)
            if not append:
                os.ftruncate(fd, 0)
            offset = 0
            while offset < len(raw):
                offset += os.write(fd, raw[offset:])
            os.fsync(fd)
            self.expected[key] = self.expected.get(key, b'') + raw if append else raw
            current = os.fstat(fd)
            gate._require(stat.S_ISREG(current.st_mode) and current.st_nlink == 1, 'Run artifacts must be unaliased regular files')
            self.stamps[key] = gate._stamp(current)
        finally:
            os.close(fd)
        self.check()

    def verify(self, keys=None):
        self.check()
        for key in list(self.files) if keys is None else keys:
            parts, name = key
            raw, identity, before_stamp, after_stamp, path_stamp = b'', None, None, None, None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.descriptors[parts])
                with os.fdopen(fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    identity, before_stamp = gate._inode(before), gate._stamp(before)
                    raw = stream.read()
                    after_stamp = gate._stamp(os.fstat(stream.fileno()))
                path_stamp = gate._stamp(os.stat(name, dir_fd=self.descriptors[parts], follow_symlinks=False))
            except OSError:
                pass
            stamp_matches = before_stamp == after_stamp == path_stamp == self.stamps[key]
            if identity != self.files[key] or raw != self.expected[key] or not stamp_matches:
                raise _ArtifactChanged({'artifact': str(Path(*parts, name)),
                                        'expected_byte_sha256': gate._sha(self.expected[key]),
                                        'actual_byte_sha256': gate._sha(raw),
                                        'identity_matches': identity == self.files[key], 'stamp_matches': stamp_matches})

    def json(self, relative, value):
        self.write(relative, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))

    def close(self):
        for fd in reversed(list(self.descriptors.values())):
            os.close(fd)


def _verify_sources(snapshots):
    try:
        for snapshot in snapshots:
            snapshot.verify()
    except (ValueError, OSError):
        raise _Blocked('source_changed') from None


def _guard(snapshots, client=None, digest=None, files=None):
    _verify_sources(snapshots)
    if files is not None:
        files.verify()
    if client is not None:
        client.metadata(digest)
        _verify_sources(snapshots)
        if files is not None:
            files.verify()


def _verify_record(row, request):
    try:
        attempt = row['attempts'][0]
        gate._require(len(row['attempts']) == 1 and type(row['selected_attempt']) is int
                      and row['selected_attempt'] == 1 and row['fallback_used'] is False
                      and type(attempt['number']) is int and attempt['number'] == 1,
                      'Candidate attempt selection changed')
        for key in ('answer', 'status', 'finish_reason', 'error', 'usage', 'seconds'):
            gate._equal(row[key], attempt[key], 'Candidate row and selected attempt disagree')
        for key in ('input_sha256', 'request_sha256'):
            gate._require(row[key] == attempt[key] == request[key], 'Candidate request provenance changed')
        for key, expected in {'max_tokens': 4096, 'temperature': 0, 'reasoning_effort': 'medium'}.items():
            gate._equal(attempt[key], expected, 'Candidate attempt settings changed')
        gate._require(row['arm'] == CANDIDATE_ARM and row['prompt_sha256'] == request['prompt_sha256']
                      and isinstance(row['answer'], str) and row['score'] is None
                      and row['score_status'] == REVIEW_REQUIRED, 'Candidate row identity changed')
        if row['status'] == 'completed':
            gate._require(row['answer'].strip() and row['finish_reason'] == 'stop' and row['error'] is None
                          and attempt['response_received'] is True, 'Candidate completion evidence changed')
        elif row['status'] == 'truncated':
            gate._require(row['answer'].strip() and row['finish_reason'] == 'length' and row['error'] is None
                          and attempt['response_received'] is True, 'Candidate truncation evidence changed')
        else:
            gate._require(row['status'] in {'failed', 'blocked'} and isinstance(row['error'], dict),
                          'Candidate failure evidence changed')
    except (ValueError, KeyError, TypeError, IndexError):
        raise _Blocked('candidate_record_invalid') from None


def _row_summary(rows, status, started_at, started, client, error):
    attempts = [row['attempts'][0] for row in rows]
    return {'status': status, 'started_at': started_at, 'finished_at': utc_now(),
            'seconds': round(time.monotonic() - started, 3), 'planned_answers': 20,
            'written_answers': len(rows), 'unattempted_answers': 20 - len(rows),
            'calls': client.calls if client else 0, 'model_metadata_calls': client.metadata_calls if client else 0,
            'answer_status_counts': dict(Counter(row['status'] for row in rows)),
            'finish_reason_counts': dict(Counter(str(row['finish_reason']) for row in rows)),
            'empty_final_answer_count': sum(not row['answer'].strip() for row in rows),
            'response_not_received_count': sum(attempt['response_received'] is False for attempt in attempts),
            'usage': sum_usage(attempts), 'error': error, 'score': None, 'score_status': REVIEW_REQUIRED}


def run_budget_cycle(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                     instructions_file: Path, out: Path, *, expected_comparison_sha256: str,
                     expected_source_summary_sha256: str, transport=None, timeout=300,
                     prepare_only: bool = False) -> tuple[dict, int]:
    """Preserve the cached control and make at most 20 single 4096 calls."""
    gate._require(type(prepare_only) is bool, 'prepare_only must be boolean')
    gate._require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                  'timeout must be finite and positive')
    proof, snapshots, items, controls, source, _ = gate._validate_budget_source(
        data, source_manifest, source_comparison_manifest, instructions_file, out,
        expected_comparison_sha256=expected_comparison_sha256,
        expected_source_summary_sha256=expected_source_summary_sha256)
    _data_index(items)
    digest, ids = proof['model']['digest'], proof['selected_prompt_ids']
    payloads = [make_payload(item, GROUNDED_SUFFIX, reasoning_effort='medium', max_tokens=4096) for item in items]
    requests = [{'prompt_id': item['prompt_id'], 'prompt_sha256': canonical_hash(item['prompt']),
                 'input_sha256': canonical_hash(payload['messages']), 'request_sha256': canonical_hash(payload)}
                for item, payload in zip(items, payloads)]
    for request, source_proof in zip(requests, proof['row_provenance']):
        gate._require(request['input_sha256'] == source_proof['input_sha256']
                      and request['request_sha256'] == source_proof['candidate_request_sha256'],
                      '4096 candidate does not match the validated max_tokens-only proof')
    candidate_config = source['arms'][gate.ARM] | {'max_tokens': 4096}
    started_at, started = utc_now(), time.monotonic()
    root_manifest = {'schema_version': 1, 'status': 'prepared' if prepare_only else 'running',
                     'started_at': started_at, 'experiment': 'cached medium2048 versus medium4096; development20 only',
                     'source_preflight': proof, 'paired_arms': list(PAIRED_ARMS), 'selected_prompt_ids': ids,
                     'execution_support': {'ready': True, 'runner': 'healthbench_budget_cycle'},
                     'prepare_only': prepare_only, 'score': None, 'score_status': REVIEW_REQUIRED}
    gate._publish_private(gate._path(out), root_manifest, snapshots)
    files = _Files(out, root_manifest)
    records, client, status, code, error, integrity_failures = [], None, root_manifest['status'], 0, None, []
    def save_json(relative, value):
        nonlocal status, code, error
        try:
            files.json(relative, value)
            return True
        except _ArtifactChanged as exc:
            status, code, error = 'blocked', 2, {'code': exc.code}
            if exc.evidence not in integrity_failures:
                integrity_failures.append(exc.evidence)
            return False
    candidate_manifest = {'schema_version': 1, 'status': status, 'started_at': started_at,
                          'dataset': deepcopy(source['dataset']), 'selected_prompt_ids': ids,
                          'selected_arms': [CANDIDATE_ARM], 'split': deepcopy(source['split']),
                          'arms': {CANDIDATE_ARM: candidate_config}, 'prepared_requests': requests,
                          'model': {'name': DEFAULT_MODEL, 'digest': digest, 'metadata_status': 'not_requested'},
                          'instructions': {'name': CANDIDATE_ARM, 'version': gate.REVISION,
                                           'byte_sha256': proof['instructions_byte_sha256']},
                          'config': {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'timeout': timeout,
                                     'reasoning_effort': 'medium', 'temperature': 0, 'max_tokens': 4096,
                                     'maximum_calls_per_case': 1, 'resume': False, 'prepare_only': prepare_only},
                          'source_byte_hashes': proof['source_byte_hashes'], 'score': None, 'score_status': REVIEW_REQUIRED}
    answers_snapshot = next(snapshot for snapshot in snapshots if snapshot.path == gate._path(source_manifest).parent / 'answers.jsonl')
    try:
        files.json('candidate/manifest.json', candidate_manifest)
        files.write('candidate/answers.jsonl', b'')
        files.write('comparison/control_answers.jsonl', answers_snapshot.raw)
        if not prepare_only:
            client = _SafeTransport(transport if transport is not None else
                                    Transport(DEFAULT_BASE_URL, os.environ.get('HEALTHBENCH_API_KEY', ''), timeout))
            try:
                for item, request in zip(items, requests):
                    _guard(snapshots, client, digest, files)
                    candidate_manifest['model']['metadata_status'] = 'available'
                    client.expected_request_sha256 = request['request_sha256']
                    record = generate_candidate(item, GROUNDED_SUFFIX, CANDIDATE_ARM, DEFAULT_MODEL, client,
                                                dataset_sha256=proof['dataset_byte_sha256'],
                                                block_on_transport_failure=True, reasoning_effort='medium', max_tokens=4096)
                    record.update(model_digest=digest, instructions_byte_sha256=proof['instructions_byte_sha256'],
                                  instructions_name=CANDIDATE_ARM, instructions_version=gate.REVISION,
                                  model_digest_status='expected_source_pin', digest_before=client.last_digest,
                                  digest_after=None, model_identity_verified=False)
                    records.append(record)
                    files.write('candidate/answers.jsonl', (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'), append=True)
                    _verify_record(record, request)
                    if client.interrupted:
                        status, code, error = 'interrupted', 130, {'code': 'interrupted'}
                        break
                    before_metadata = client.metadata_calls
                    try:
                        _guard(snapshots, client, digest, files)
                    except _Blocked:
                        if client.metadata_calls > before_metadata:
                            record['digest_after'] = client.last_digest
                        try:
                            files.write('candidate/answers.jsonl', b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8') for row in records))
                        except _ArtifactChanged as exc:
                            integrity_failures.append(exc.evidence)
                        raise
                    record.update(digest_after=client.last_digest, model_identity_verified=True)
                    files.write('candidate/answers.jsonl', b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8') for row in records))
                    if record['status'] == 'blocked':
                        status, code, error = 'blocked', 2, {'code': 'candidate_transport_failure'}
                        break
                else:
                    status = 'completed' if all(row['status'] == 'completed' for row in records) else 'partial'
                    code = 0 if status == 'completed' else 1
                    _guard(snapshots, client, digest, files)
            except _Blocked as exc:
                status, code, error = 'blocked', 2, {'code': exc.code}
                if isinstance(exc, _ArtifactChanged):
                    integrity_failures.append(exc.evidence)
            except KeyboardInterrupt:
                status, code, error = 'interrupted', 130, {'code': 'interrupted'}
        else:
            _guard(snapshots, files=files)
        summary = _row_summary(records, status, started_at, started, client, error)
        summary.update(selected_prompt_ids=ids, selected_arms=[CANDIDATE_ARM],
                       source_status_counts=proof['source_status_counts'], source_known_failure_count=3,
                       source_recorded_calls=20, source_usage=sum_usage([row['attempts'][0] for row in controls]))
        candidate_manifest.update(status=status, finished_at=summary['finished_at'], error=error)
        candidate_manifest['model']['metadata_status'] = client.metadata_status if client else 'not_requested'
        candidate_manifest['model']['digest_status'] = 'expected_source_pin'
        save_json('candidate/manifest.json', candidate_manifest)
        summary.update(status=status, error=error)
        save_json('candidate/summary.json', summary)
        candidate_by_id = {row['prompt_id']: row for row in records}
        control_lines = [line for line in answers_snapshot.raw.splitlines(keepends=True) if line.strip()]
        combined, provenance = [], []
        for control, original_line in zip(controls, control_lines):
            combined.append(original_line if original_line.endswith(b'\n') else original_line + b'\n')
            selected = [('baseline', control)]
            candidate = candidate_by_id.get(control['prompt_id'])
            if candidate is not None:
                combined.append((json.dumps(candidate, ensure_ascii=False) + '\n').encode('utf-8'))
                selected.append(('candidate', candidate))
            for label, row in selected:
                provenance.append({'source': label, 'prompt_id': row['prompt_id'], 'arm': row['arm'],
                                   'original_status': row['status'], 'full_row_sha256': canonical_hash(row),
                                   'input_sha256': row['input_sha256'], 'request_sha256': row['request_sha256']})
        comparison_status = 'partial' if status in {'completed', 'partial'} and len(records) == 20 else status
        comparison_manifest = {'schema_version': 1, 'status': comparison_status,
                               'finished_at': summary['finished_at'], 'dataset': deepcopy(source['dataset']),
                               'selected_prompt_ids': ids, 'selected_arms': list(PAIRED_ARMS),
                               'arms': {gate.ARM: deepcopy(source['arms'][gate.ARM]), CANDIDATE_ARM: candidate_config},
                               'planned_answers': 40, 'written_answers': 20 + len(records),
                               'original_control_manifest_status': 'partial', 'control_status_counts': proof['source_status_counts'],
                               'control_recorded_calls': 20, 'control_usage': summary['source_usage'],
                               'candidate_status': status, 'source_byte_hashes': proof['source_byte_hashes'],
                               'row_provenance': provenance, 'score': None, 'score_status': REVIEW_REQUIRED}
        files.write('comparison/answers.jsonl', b''.join(combined))
        files.json('comparison/manifest.json', comparison_manifest)
        provenance_manifest = {'source_byte_hashes': proof['source_byte_hashes'],
                               'control_answers_byte_sha256': gate._sha(answers_snapshot.raw),
                               'row_provenance': provenance, 'changed_fields': ['max_tokens']}
        files.json('comparison/provenance.json', provenance_manifest)
        def refresh_source_entries():
            notices = {entry['artifact']: entry for entry in integrity_failures}
            source_changed = bool(error and error['code'] == 'source_changed')
            candidate_manifest_key, candidate_answers_key = (('candidate',), 'manifest.json'), (('candidate',), 'answers.jsonl')
            expected_manifest_sha = gate._sha(files.expected[candidate_manifest_key])
            expected_answers_sha = gate._sha(files.expected[candidate_answers_key])
            entries = {
                'baseline': {'manifest_path': str(gate._path(source_manifest)),
                             'answers_path': str(answers_snapshot.path),
                             'manifest_byte_sha256': proof['source_byte_hashes']['source_manifest'],
                             'answers_byte_sha256': proof['source_byte_hashes']['source_answers'],
                             'original_manifest_status': 'partial',
                             'provenance_kind': 'externally pinned validated original snapshot',
                             'source_changed_after_validation': source_changed,
                             'current_source_matching': False if source_changed else 'verified_at_last_check'},
                'candidate': {'manifest_path': str(gate._path(out) / 'candidate/manifest.json'),
                              'answers_path': str(gate._path(out) / 'candidate/answers.jsonl'),
                              'manifest_byte_sha256': notices.get('candidate/manifest.json', {}).get('actual_byte_sha256', expected_manifest_sha),
                              'answers_byte_sha256': notices.get('candidate/answers.jsonl', {}).get('actual_byte_sha256', expected_answers_sha),
                              'expected_manifest_byte_sha256': expected_manifest_sha,
                              'expected_answers_byte_sha256': expected_answers_sha,
                              'original_manifest_status': 'unverified' if 'candidate/manifest.json' in notices else status,
                              'recorded_run_status': status,
                              'integrity_status': 'failed' if any(name.startswith('candidate/') for name in notices) else 'verified'}}
            comparison_manifest['sources'] = entries
            provenance_manifest['sources'] = entries
        review_status = {'status': 'not_prepared', 'ready_packet_count': 0, 'grading_status': 'not_graded',
                         'prepared_packet_count': 0, 'review_eligible': False,
                         'expected_packet_count': 20, 'pending_count': 20,
                         'available_nonempty_pair_count': sum(control['status'] == 'completed'
                             and (candidate_by_id.get(control['prompt_id']) or {}).get('status') in review.REVIEWABLE
                             and bool((candidate_by_id.get(control['prompt_id']) or {}).get('answer', '').strip())
                             for control in controls)}
        if comparison_status == 'partial' and len(records) == 20:
            try:
                _guard(snapshots, client, digest, files)
                files.check()
                review_status = review.create_review_packets(data, gate._path(out) / 'comparison/answers.jsonl',
                    gate._path(out) / 'comparison/manifest.json', gate._path(out) / 'comparison/review',
                    paired_arms=PAIRED_ARMS, include_known_inference_failures=True)
                review_status.update(prepared_packet_count=review_status['ready_packet_count'], review_eligible=True)
                expected_answers_sha = gate._sha(files.expected[(('comparison',), 'answers.jsonl')])
                if review_status.get('answers_snapshot_sha256') != expected_answers_sha:
                    raise _ArtifactChanged({'artifact': 'comparison/review/progress.json',
                                            'expected_answers_byte_sha256': expected_answers_sha,
                                            'observed_answers_byte_sha256': review_status.get('answers_snapshot_sha256')})
                _guard(snapshots, client, digest, files)
                files.check()
            except (_Blocked, ValueError, OSError, KeyboardInterrupt) as exc:
                status = 'interrupted' if isinstance(exc, KeyboardInterrupt) else 'blocked'
                code = 130 if status == 'interrupted' else 2
                error = {'code': exc.code if isinstance(exc, _Blocked) else
                         ('interrupted_during_review_preparation' if status == 'interrupted' else 'review_preparation_failure')}
                if isinstance(exc, _ArtifactChanged):
                    integrity_failures.append(exc.evidence)
                prepared = review_status.get('prepared_packet_count', 0)
                review_status = {'status': 'invalidated_after_preparation' if prepared else 'not_prepared_' + status,
                                 'ready_packet_count': 0, 'prepared_packet_count': prepared,
                                 'ready_packet_count_at_preparation': prepared, 'pending_count': 20 - prepared,
                                 'review_eligible': False, 'grading_status': 'not_graded', 'error': error}
                summary.update(status=status, error=error)
                candidate_manifest.update(status=status, error=error)
                comparison_manifest.update(status=status, candidate_status=status, error=error)
                save_json('candidate/manifest.json', candidate_manifest)
                save_json('candidate/summary.json', summary)
                save_json('comparison/manifest.json', comparison_manifest)
        elif status in {'blocked', 'interrupted'}:
            review_status.update(status='not_prepared_' + status, reason='run is incomplete; failure opt-in requires finalization')
        summary['model_metadata_calls'] = client.metadata_calls if client else 0
        summary.update(status=status, error=error, finished_at=utc_now(), seconds=round(time.monotonic() - started, 3))
        current_runtime = {'observed_digest': client.last_digest if client else None,
                           'metadata_status': client.metadata_status if client else 'not_requested',
                           'metadata_calls': client.metadata_calls if client else 0}
        summary['current_runtime_model'] = current_runtime
        candidate_manifest.update(status=status, error=error, current_runtime_model=current_runtime,
                                  finished_at=summary['finished_at'])
        candidate_manifest['model']['metadata_status'] = current_runtime['metadata_status']
        comparison_manifest.update(status='partial' if status in {'completed', 'partial'} and len(records) == 20 else status,
                                   candidate_status=status, error=error, current_runtime_model=current_runtime,
                                   finished_at=summary['finished_at'])
        summary['comparison_status'] = comparison_manifest['status']
        summary['review'] = review_status
        if integrity_failures:
            summary['integrity_failures'] = integrity_failures
            files.json('integrity_failure.json', {'status': 'blocked', 'calls': summary['calls'],
                                                 'error': error, 'artifacts': integrity_failures})
        save_json('candidate/manifest.json', candidate_manifest)
        save_json('candidate/summary.json', summary)
        refresh_source_entries()
        save_json('comparison/manifest.json', comparison_manifest)
        save_json('comparison/provenance.json', provenance_manifest)
        save_json('comparison/review_status.json', review_status)
        root_manifest.update(status=status, finished_at=summary['finished_at'], error=error,
                             calls=summary['calls'], model_metadata_calls=summary['model_metadata_calls'],
                             candidate_status=status, comparison_status=comparison_manifest['status'], review=review_status)
        save_json('manifest.json', root_manifest)
        save_json('summary.json', summary)
        try:
            _guard(snapshots, files=files)
        except _Blocked as exc:
            summary.update(status='blocked', error={'code': exc.code})
            code = 2
            if isinstance(exc, _ArtifactChanged) and exc.evidence not in integrity_failures:
                integrity_failures.append(exc.evidence)
            status, error = 'blocked', summary['error']
            prepared = review_status.get('prepared_packet_count', 0)
            review_status.update(status='invalidated_after_preparation' if prepared else 'not_prepared_blocked',
                                 prepared_packet_count=prepared, ready_packet_count=0, review_eligible=False,
                                 pending_count=20 - prepared, error=error)
            summary.update(review=review_status, comparison_status='blocked', integrity_failures=integrity_failures)
            candidate_manifest.update(status='blocked', error=error)
            comparison_manifest.update(status='blocked', candidate_status='blocked', error=error)
            root_manifest.update(status='blocked', error=error, candidate_status='blocked', comparison_status='blocked', review=review_status)
            save_json('candidate/manifest.json', candidate_manifest)
            save_json('candidate/summary.json', summary)
            refresh_source_entries()
            save_json('comparison/manifest.json', comparison_manifest)
            save_json('comparison/provenance.json', provenance_manifest)
            save_json('comparison/review_status.json', review_status)
            save_json('summary.json', summary)
            save_json('manifest.json', root_manifest)
            if integrity_failures:
                save_json('integrity_failure.json', {'status': 'blocked', 'calls': summary['calls'],
                                                      'error': error, 'artifacts': integrity_failures})
        return summary, code
    finally:
        files.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'source-manifest', 'source-comparison-manifest', 'instructions-file', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--expected-comparison-sha256', required=True)
    parser.add_argument('--expected-source-summary-sha256', required=True)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        summary, code = run_budget_cycle(args.data, args.source_manifest, args.source_comparison_manifest,
            args.instructions_file, args.out, expected_comparison_sha256=args.expected_comparison_sha256,
            expected_source_summary_sha256=args.expected_source_summary_sha256,
            timeout=args.timeout, prepare_only=args.prepare_only)
    except (ValueError, OSError):
        print('Budget cycle rejected or stopped; inspect the private run artifacts for recorded calls and state', file=sys.stderr)
        return 2
    print(json.dumps({'status': summary['status'], 'calls': summary['calls'],
                      'unattempted_answers': summary['unattempted_answers'],
                      'review_status': summary['review']['status'], 'score': None}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
