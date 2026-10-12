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
    def __init__(self, client, *, template_pin=None):
        self.client, self.calls, self.metadata_calls, self.interrupted = client, 0, 0, False
        self.last_digest, self.metadata_status = None, 'not_requested'
        self.expected_request_sha256 = None
        self.template_pin, self.template_queries = template_pin, 0
        self.last_template_sha256, self.template_status = None, 'not_requested'

    def complete(self, payload):
        if canonical_hash(payload) != self.expected_request_sha256:
            raise _Blocked('candidate_request_changed')
        self.calls += 1
        try:
            response = self.client.complete(payload)
            if self.template_pin is not None:
                choices = response.get('choices') if isinstance(response, dict) else None
                if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                        or not isinstance(choices[0].get('message'), dict)):
                    raise RequestFailure('invalid_provider_response')
                choice = choices[0]
                answer, finish = choice['message'].get('content'), choice.get('finish_reason')
                if ((answer is not None and not isinstance(answer, str))
                        or not isinstance(finish, str) or finish not in ('stop', 'length')):
                    raise RequestFailure('invalid_provider_response')
                try:
                    if answer is not None:
                        answer.encode('utf-8')
                except UnicodeError:
                    raise RequestFailure('invalid_provider_response') from None
            return response
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
        if self.template_pin is not None:
            self.template_status = 'not_checked_after_latest_model_query'
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
        if self.template_pin is not None:
            self.template_queries += 1
            self.last_template_sha256, self.template_status = None, 'unavailable'
            try:
                value = self.client.request('POST', DEFAULT_BASE_URL.removesuffix('/v1') + '/api/show',
                                            {'model': DEFAULT_MODEL}, authenticated=False)
            except KeyboardInterrupt:
                self.template_status = 'interrupted'
                raise
            except Exception:
                raise _Blocked('model_template_unavailable') from None
            template = value.get('template') if isinstance(value, dict) else None
            if not isinstance(template, str) or not template.strip():
                raise _Blocked('model_template_unavailable')
            try:
                self.last_template_sha256 = gate._sha(template.encode('utf-8'))
            except UnicodeError:
                raise _Blocked('model_template_unavailable') from None
            if self.last_template_sha256 != self.template_pin:
                self.template_status = 'hash_mismatch'
                raise _Blocked('model_template_changed')
            self.template_status = 'available'


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


def _verify_record(row, request, *, experiment='max_tokens'):
    try:
        gate._require(type(experiment) is str and experiment in ('max_tokens', 'instruction_placement'),
                      'Unknown fixed experiment')
        if experiment == 'instruction_placement':
            from .healthbench_placement_cycle import CANDIDATE_ARM as candidate_arm
            max_tokens = 2048
        else:
            candidate_arm, max_tokens = CANDIDATE_ARM, 4096
        attempt = row['attempts'][0]
        gate._require(len(row['attempts']) == 1 and type(row['selected_attempt']) is int
                      and row['selected_attempt'] == 1 and row['fallback_used'] is False
                      and type(attempt['number']) is int and attempt['number'] == 1,
                      'Candidate attempt selection changed')
        for key in ('answer', 'status', 'finish_reason', 'error', 'usage', 'seconds'):
            gate._equal(row[key], attempt[key], 'Candidate row and selected attempt disagree')
        for key in ('input_sha256', 'request_sha256'):
            gate._require(row[key] == attempt[key] == request[key], 'Candidate request provenance changed')
        for key, expected in {'max_tokens': max_tokens, 'temperature': 0, 'reasoning_effort': 'medium'}.items():
            gate._equal(attempt[key], expected, 'Candidate attempt settings changed')
        gate._require(row['arm'] == candidate_arm and row['prompt_sha256'] == request['prompt_sha256']
                      and isinstance(row['answer'], str) and row['score'] is None
                      and row['score_status'] == REVIEW_REQUIRED, 'Candidate row identity changed')
        if experiment == 'instruction_placement':
            gate._require(row['instruction_placement'] == attempt['instruction_placement'] == 'system_prefix'
                          and row['model'] == DEFAULT_MODEL and row['prompt_id'] == request['prompt_id'],
                          'Candidate placement identity changed')
            for key in ('dataset_sha256', 'model_digest', 'instructions_byte_sha256',
                        'instructions_name', 'instructions_version'):
                gate._require(row[key] == request[key], 'Candidate placement provenance changed')
        if row['status'] == 'completed':
            gate._require(row['answer'].strip() and row['finish_reason'] == 'stop' and row['error'] is None
                          and attempt['response_received'] is True, 'Candidate completion evidence changed')
        elif row['status'] == 'truncated':
            gate._require(row['answer'].strip() and row['finish_reason'] == 'length' and row['error'] is None
                          and attempt['response_received'] is True, 'Candidate truncation evidence changed')
        else:
            gate._require(row['status'] in {'failed', 'blocked'} and isinstance(row['error'], dict),
                          'Candidate failure evidence changed')
            if experiment == 'instruction_placement':
                gate._require(set(row['error']) == {'code', 'http_status'}, 'Candidate failure fields changed')
                if row['status'] == 'failed':
                    gate._require(attempt['response_received'] is True and row['error']['http_status'] is None
                                  and row['error']['code'] in {'empty_answer', 'unexpected_finish_reason'},
                                  'Candidate received failure evidence changed')
                    if row['error']['code'] == 'empty_answer':
                        gate._require(not row['answer'].strip(), 'Empty-answer failure content changed')
                else:
                    gate._require(attempt['response_received'] is False
                                  and row['error']['code'] in TRANSPORT_CODES | {'transport_failure', 'interrupted'},
                                  'Candidate transport failure evidence changed')
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


def _run_fixed_cycle(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                     instructions_file: Path, out: Path, *, expected_comparison_sha256: str,
                     expected_source_summary_sha256: str, transport=None, timeout=300,
                     prepare_only: bool = False, experiment: str) -> tuple[dict, int]:
    """Share integrity guards for exactly two fixed recipes; accept no payload overrides."""
    gate._require(type(experiment) is str and experiment in ('max_tokens', 'instruction_placement'),
                  'Only the two fixed budget and placement experiments are supported')
    gate._require(type(prepare_only) is bool, 'prepare_only must be boolean')
    gate._require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                  'timeout must be finite and positive')
    proof, snapshots, items, controls, source, _ = gate._validate_budget_source(
        data, source_manifest, source_comparison_manifest, instructions_file, out,
        expected_comparison_sha256=expected_comparison_sha256,
        expected_source_summary_sha256=expected_source_summary_sha256)
    _data_index(items)
    digest, ids = proof['model']['digest'], proof['selected_prompt_ids']
    placement = experiment == 'instruction_placement'
    template_pin = None
    if placement:
        from .healthbench_placement_cycle import (CANDIDATE_ARM as candidate_arm,
                                                 EXPECTED_TEMPLATE_SHA256, _prepare_placement_plan)
        proof, payloads = _prepare_placement_plan(proof, items, controls, source)
        max_tokens, instruction_placement, changed_fields = 2048, 'system_prefix', ['messages']
        template_pin = EXPECTED_TEMPLATE_SHA256
        runner, description = 'healthbench_placement_cycle', 'cached medium2048 user suffix versus medium2048 system prefix; development20 only'
    else:
        candidate_arm, max_tokens, instruction_placement, changed_fields = CANDIDATE_ARM, 4096, 'user_suffix', ['max_tokens']
        payloads = [make_payload(item, GROUNDED_SUFFIX, reasoning_effort='medium', max_tokens=4096) for item in items]
        runner, description = 'healthbench_budget_cycle', 'cached medium2048 versus medium4096; development20 only'
    paired_arms = (gate.ARM, candidate_arm)
    requests = [{'prompt_id': item['prompt_id'], 'prompt_sha256': canonical_hash(item['prompt']),
                 'input_sha256': canonical_hash(payload['messages']), 'request_sha256': canonical_hash(payload)}
                for item, payload in zip(items, payloads)]
    for request, source_proof in zip(requests, proof['row_provenance']):
        expected_input = source_proof['treatment_input_sha256'] if placement else source_proof['input_sha256']
        expected_request = source_proof['treatment_request_sha256'] if placement else source_proof['candidate_request_sha256']
        gate._require(request['input_sha256'] == expected_input and request['request_sha256'] == expected_request,
                      'Candidate does not match the validated fixed experiment proof')
        if placement:
            request.update(dataset_sha256=proof['dataset_byte_sha256'], model_digest=digest,
                           instructions_byte_sha256=proof['instructions_byte_sha256'],
                           instructions_name=candidate_arm, instructions_version=gate.REVISION,
                           instruction_placement=instruction_placement)
    candidate_config = source['arms'][gate.ARM] | {'max_tokens': max_tokens}
    if placement:
        candidate_config['instruction_placement'] = instruction_placement
    for snapshot in snapshots:
        snapshot.verify()
    started_at, started = utc_now(), time.monotonic()
    root_manifest = {'schema_version': 1, 'status': 'prepared' if prepare_only else 'running',
                     'started_at': started_at, 'experiment': description,
                     'source_preflight': proof, 'paired_arms': list(paired_arms), 'selected_prompt_ids': ids,
                     'execution_support': {'ready': True, 'runner': runner},
                     'prepare_only': prepare_only, 'score': None, 'score_status': REVIEW_REQUIRED}
    if placement:
        root_manifest['cached_source_runtime_template'] = {
            'byte_sha256': template_pin, 'metadata_source': 'cached source-time runtime snapshot; no new control query',
            'bound_source_model_digest': digest,
            'bound_source_manifest_byte_sha256': proof['source_byte_hashes']['source_manifest'],
            'plain_api_system_mapping': 'supported by this fixed pinned runtime template',
        }
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
                          'selected_arms': [candidate_arm], 'split': deepcopy(source['split']),
                          'arms': {candidate_arm: candidate_config}, 'prepared_requests': requests,
                          'model': {'name': DEFAULT_MODEL, 'digest': digest, 'metadata_status': 'not_requested'},
                          'instructions': {'name': candidate_arm, 'version': gate.REVISION,
                                           'byte_sha256': proof['instructions_byte_sha256']},
                          'config': {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'timeout': timeout,
                                     'reasoning_effort': 'medium', 'temperature': 0, 'max_tokens': max_tokens,
                                     'maximum_calls_per_case': 1, 'resume': False, 'prepare_only': prepare_only},
                          'source_byte_hashes': proof['source_byte_hashes'], 'score': None, 'score_status': REVIEW_REQUIRED}
    if placement:
        candidate_manifest['config']['instruction_placement'] = instruction_placement
        candidate_manifest['cached_source_runtime_template'] = deepcopy(root_manifest['cached_source_runtime_template'])
    answers_snapshot = next(snapshot for snapshot in snapshots if snapshot.path == gate._path(source_manifest).parent / 'answers.jsonl')
    try:
        files.json('candidate/manifest.json', candidate_manifest)
        files.write('candidate/answers.jsonl', b'')
        files.write('comparison/control_answers.jsonl', answers_snapshot.raw)
        if not prepare_only:
            client = _SafeTransport(transport if transport is not None else
                                    Transport(DEFAULT_BASE_URL, os.environ.get('HEALTHBENCH_API_KEY', ''), timeout),
                                    template_pin=template_pin)
            try:
                for item, request in zip(items, requests):
                    _guard(snapshots, client, digest, files)
                    candidate_manifest['model']['metadata_status'] = 'available'
                    client.expected_request_sha256 = request['request_sha256']
                    record = generate_candidate(item, GROUNDED_SUFFIX, candidate_arm, DEFAULT_MODEL, client,
                                                dataset_sha256=proof['dataset_byte_sha256'],
                                                block_on_transport_failure=True, reasoning_effort='medium', max_tokens=max_tokens,
                                                instruction_placement=instruction_placement)
                    record.update(model_digest=digest, instructions_byte_sha256=proof['instructions_byte_sha256'],
                                  instructions_name=candidate_arm, instructions_version=gate.REVISION,
                                  model_digest_status='expected_source_pin', digest_before=client.last_digest,
                                  digest_after=None, model_identity_verified=False)
                    if placement:
                        record.update(template_before_sha256=client.last_template_sha256,
                                      template_after_sha256=None, runtime_template_verified=False)
                    records.append(record)
                    files.write('candidate/answers.jsonl', (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'), append=True)
                    _verify_record(record, request, experiment=experiment)
                    if client.interrupted:
                        status, code, error = 'interrupted', 130, {'code': 'interrupted'}
                        break
                    before_metadata = client.metadata_calls
                    before_template = client.template_queries
                    try:
                        _guard(snapshots, client, digest, files)
                    except _Blocked:
                        if client.metadata_calls > before_metadata:
                            record['digest_after'] = client.last_digest
                        if placement and client.template_queries > before_template:
                            record['template_after_sha256'] = client.last_template_sha256
                        try:
                            files.write('candidate/answers.jsonl', b''.join((json.dumps(row, ensure_ascii=False) + '\n').encode('utf-8') for row in records))
                        except _ArtifactChanged as exc:
                            integrity_failures.append(exc.evidence)
                        raise
                    record.update(digest_after=client.last_digest, model_identity_verified=True)
                    if placement:
                        record.update(template_after_sha256=client.last_template_sha256, runtime_template_verified=True)
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
        summary.update(selected_prompt_ids=ids, selected_arms=[candidate_arm],
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
                               'selected_prompt_ids': ids, 'selected_arms': list(paired_arms),
                               'arms': {gate.ARM: deepcopy(source['arms'][gate.ARM]), candidate_arm: candidate_config},
                               'planned_answers': 40, 'written_answers': 20 + len(records),
                               'original_control_manifest_status': 'partial', 'control_status_counts': proof['source_status_counts'],
                               'control_recorded_calls': 20, 'control_usage': summary['source_usage'],
                               'candidate_status': status, 'source_byte_hashes': proof['source_byte_hashes'],
                               'row_provenance': provenance, 'score': None, 'score_status': REVIEW_REQUIRED}
        files.write('comparison/answers.jsonl', b''.join(combined))
        files.json('comparison/manifest.json', comparison_manifest)
        provenance_manifest = {'source_byte_hashes': proof['source_byte_hashes'],
                               'control_answers_byte_sha256': gate._sha(answers_snapshot.raw),
                               'row_provenance': provenance, 'changed_fields': changed_fields}
        if placement:
            provenance_manifest['placement_request_provenance'] = deepcopy(proof['row_provenance'])
            comparison_manifest['cached_source_runtime_template'] = deepcopy(root_manifest['cached_source_runtime_template'])
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
                    paired_arms=paired_arms, include_known_inference_failures=True)
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
        if placement:
            current_template = {'expected_byte_sha256': template_pin,
                                'observed_byte_sha256': client.last_template_sha256 if client else None,
                                'metadata_status': client.template_status if client else 'not_requested',
                                'template_queries': client.template_queries if client else 0,
                                'verified': bool(client and client.metadata_status == 'available'
                                                 and client.template_status == 'available'),
                                'query': 'POST /api/show; exact template UTF-8 bytes; metadata only'}
            summary.update(template_queries=current_template['template_queries'],
                           current_runtime_template=current_template,
                           cached_source_runtime_template=deepcopy(root_manifest['cached_source_runtime_template']))
            for manifest in (candidate_manifest, comparison_manifest, root_manifest):
                manifest['current_runtime_template'] = deepcopy(current_template)
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
        if placement:
            root_manifest['template_queries'] = summary['template_queries']
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


def run_budget_cycle(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                     instructions_file: Path, out: Path, *, expected_comparison_sha256: str,
                     expected_source_summary_sha256: str, transport=None, timeout=300,
                     prepare_only: bool = False) -> tuple[dict, int]:
    """Preserve the unchanged fixed medium4096 budget experiment."""
    return _run_fixed_cycle(data, source_manifest, source_comparison_manifest, instructions_file, out,
        expected_comparison_sha256=expected_comparison_sha256,
        expected_source_summary_sha256=expected_source_summary_sha256,
        transport=transport, timeout=timeout, prepare_only=prepare_only, experiment='max_tokens')


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
