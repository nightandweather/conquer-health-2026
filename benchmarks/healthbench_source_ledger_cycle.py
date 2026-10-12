"""Execute one fixed development20 literal source-index candidate.

This independent runner validates the immutable 18 completed/1 empty failed/
1 truncated placement profile; it never relaxes or substitutes pins into the
older 17+3 source gate. Only the deterministic user JSON index is inserted.
A fresh private directory, one writer, and one call per item are required.
Preparation, generation and external medical review remain separate stages.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
import os
import stat
from pathlib import Path
import sys
import time

from . import healthbench_budget_preflight as gate
from . import healthbench_source_ledger_preflight as ledger
from . import healthbench_review as review
from .healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX,
    ROOT, RequestFailure, Transport, canonical_hash, sum_usage, usage_values, utc_now)
from .healthbench_codex_cycle import REVIEW_REQUIRED, _source_object
from .healthbench_budget_cycle import (_Files, _Blocked, _ArtifactChanged,
    _SafeTransport, _guard, _row_summary, TRANSPORT_CODES)
from .healthbench_score import _data_index

CANDIDATE_ARM = 'grounded_medium_user_index2048_v1'
PAIRED_ARMS = (ledger.SOURCE_ARM, CANDIDATE_ARM)
INDEX_MODE = 'literal_user_turn_index_v1'
EXPECTED_PREFLIGHT_SHA256 = '86c71383d3ad6e5a3862420a3a76f5c77f594b6d9e1971aeb64914e9da950ec2'
# No alternate code root or caller-supplied replacement pins. New runner and
# support imports are snapshotted from the active checkout before preparation.
EXECUTION_CODE_PATHS = (
    'benchmarks/healthbench_source_ledger_cycle.py',
    'benchmarks/healthbench_source_ledger_preflight.py',
    'benchmarks/healthbench_budget_preflight.py',
    'benchmarks/healthbench.py', 'benchmarks/healthbench_review.py',
    'benchmarks/healthbench_score.py',
    *ledger._PRODUCER_CODE_SHA256,
)


class _SourceTransport(_SafeTransport):
    """Keep actual call and malformed-provider receipt evidence without secrets."""
    def complete(self, payload):
        if canonical_hash(payload) != self.expected_request_sha256:
            raise _Blocked('candidate_request_changed')
        self.calls += 1
        self.provider_response_received = False
        self.received_answer_text = None
        self.provider_usage = {'prompt_tokens':None,'completion_tokens':None,'total_tokens':None}
        try:
            response = self.client.complete(payload)
            self.provider_response_received = True
            if isinstance(response, dict):
                self.provider_usage = usage_values(response)
            choices = response.get('choices') if isinstance(response, dict) else None
            if (not isinstance(choices, list) or not choices or not isinstance(choices[0], dict)
                    or not isinstance(choices[0].get('message'), dict)):
                raise RequestFailure('invalid_provider_response')
            choice = choices[0]
            answer, finish = choice['message'].get('content'), choice.get('finish_reason')
            if isinstance(answer, str):
                try:
                    answer.encode('utf-8')
                except UnicodeError:
                    raise RequestFailure('invalid_provider_response') from None
                self.received_answer_text = answer
            if ((answer is not None and not isinstance(answer, str))
                    or not isinstance(finish, str) or finish not in ('stop', 'length')):
                raise RequestFailure('invalid_provider_response')
            if answer is not None:
                try:
                    answer.encode('utf-8')
                except UnicodeError:
                    raise RequestFailure('invalid_provider_response') from None
            return response
        except KeyboardInterrupt:
            self.interrupted = True
            raise RequestFailure('interrupted') from None
        except RequestFailure as exc:
            code = exc.code if exc.code in TRANSPORT_CODES | {'interrupted'} else 'transport_failure'
            http_status = exc.http_status if type(exc.http_status) is int and 100 <= exc.http_status <= 599 else None
            raise RequestFailure(code, http_status) from None
        except Exception:
            raise RequestFailure('transport_failure') from None


def _generate(item, payload, request, client):
    """Send the already proven exact payload once; no generator reformatting."""
    started = time.monotonic()
    attempt = {'number': 1, 'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium',
        'request_sha256': canonical_hash(payload), 'input_sha256': canonical_hash(payload['messages']),
        'source_index_mode': INDEX_MODE, 'instruction_placement': 'system_prefix',
        'source_index_sha256': request['source_index_sha256'],
        'source_index_message_byte_sha256': request['source_index_message_byte_sha256'],
        'answer': '', 'finish_reason': None, 'usage': {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None},
        'status': 'failed', 'error': None, 'response_received': False, 'provider_response_received': False}
    try:
        response = client.complete(payload)
        choice = response['choices'][0]
        attempt.update(answer=choice['message'].get('content') or '', finish_reason=choice['finish_reason'],
                       usage=usage_values(response), response_received=True)
        if not attempt['answer'].strip():
            raise RequestFailure('empty_answer')
        attempt['status'] = 'completed' if attempt['finish_reason'] == 'stop' else 'truncated'
    except RequestFailure as exc:
        attempt.update(status='failed' if attempt['response_received'] else 'blocked',
                       error={'code': exc.code, 'http_status': exc.http_status})
    attempt['provider_response_received'] = client.provider_response_received
    if not attempt['response_received']:
        attempt['usage'] = deepcopy(client.provider_usage)
    if not attempt['response_received'] and client.received_answer_text is not None:
        # Preserve a valid literal final-content string even when its envelope
        # is invalid. It is never treated as a completed or reviewable answer.
        attempt['unparsed_received_answer'] = client.received_answer_text
    attempt['seconds'] = round(time.monotonic() - started, 3)
    row = {'prompt_id': item['prompt_id'], 'arm': CANDIDATE_ARM, 'model': DEFAULT_MODEL,
        **{key: request[key] for key in ('dataset_sha256', 'model_digest', 'prompt_sha256',
          'instructions_byte_sha256', 'instructions_name', 'instructions_version',
          'input_sha256', 'request_sha256', 'source_index_sha256', 'source_index_message_byte_sha256')},
        'instruction_placement': 'system_prefix', 'source_index_mode': INDEX_MODE,
        'attempts': [attempt], 'fallback_used': False, 'selected_attempt': 1,
        'score': None, 'score_status': REVIEW_REQUIRED}
    row.update({key: deepcopy(attempt[key]) for key in ('answer', 'status', 'finish_reason', 'error', 'usage', 'seconds')})
    return row


def _verify_record(row, request):
    try:
        attempt = row['attempts'][0]
        gate._require(len(row['attempts']) == 1 and type(row['selected_attempt']) is int
            and row['selected_attempt'] == 1 and row['fallback_used'] is False
            and type(attempt['number']) is int and attempt['number'] == 1,
            'Candidate call/attempt selection differs')
        for key in ('answer', 'status', 'finish_reason', 'error', 'usage', 'seconds'):
            gate._equal(row[key], attempt[key], 'Candidate and attempt evidence differ')
        for key in ('request_sha256', 'input_sha256', 'source_index_sha256', 'source_index_message_byte_sha256'):
            gate._equal(row[key], request[key], 'Candidate input provenance differs')
            gate._equal(attempt[key], request[key], 'Attempt input provenance differs')
        for key, expected in {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium',
                              'source_index_mode': INDEX_MODE, 'instruction_placement': 'system_prefix'}.items():
            gate._equal(attempt[key], expected, 'Candidate actual settings differ')
        gate._require(row['source_index_mode'] == INDEX_MODE and row['instruction_placement'] == 'system_prefix'
            and row['model'] == DEFAULT_MODEL and row['arm'] == CANDIDATE_ARM
            and isinstance(row['answer'], str) and row['score'] is None and row['score_status'] == REVIEW_REQUIRED,
            'Candidate fixed identity differs')
        row['answer'].encode('utf-8')
        for key in ('prompt_id', 'prompt_sha256', 'dataset_sha256', 'model_digest',
                    'instructions_byte_sha256', 'instructions_name', 'instructions_version'):
            gate._equal(row[key], request[key], 'Candidate source identity differs')
        if row['status'] in ('completed', 'truncated'):
            gate._require(row['answer'].strip() and row['error'] is None and attempt['response_received'] is True
                and attempt['provider_response_received'] is True
                and row['finish_reason'] == ('stop' if row['status'] == 'completed' else 'length'), 'Completion evidence differs')
        elif row['status'] == 'failed':
            gate._require(not row['answer'].strip() and row['error'] == {'code': 'empty_answer', 'http_status': None}
                and attempt['response_received'] is True and attempt['provider_response_received'] is True
                and row['finish_reason'] in ('stop', 'length'), 'Empty response evidence differs')
        else:
            gate._require(row['status'] == 'blocked' and not row['answer'] and attempt['response_received'] is False
                and isinstance(row['error'], dict) and set(row['error']) == {'code', 'http_status'}
                and row['error']['code'] in TRANSPORT_CODES | {'transport_failure', 'interrupted'}, 'Transport evidence differs')
    except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
        raise _Blocked('candidate_record_invalid') from None


def _capture_review_artifacts(files, items, controls, records, review_progress, dataset_sha, arm_configs):
    """Verify deterministic packets and enroll externally created files in FDs.

    Packet creation still uses the unchanged shared packager. Enrollment has
    the same documented first-inode-capture limitation as run publication.
    """
    files.check()
    directory_parts = [('comparison', 'review'), ('comparison', 'review', 'packets')]
    for parts in directory_parts:
        parent = files.descriptors[parts[:-1]]
        captured = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            gate._require(stat.S_ISDIR(captured.st_mode) and stat.S_IMODE(captured.st_mode) == 0o700
                and gate._inode(captured) == gate._inode(os.fstat(fd)), 'Review directory changed before capture')
        except Exception:
            os.close(fd)
            raise
        files.descriptors[parts], files.identities[parts] = fd, gate._inode(captured)
    root = files.root / 'comparison' / 'review'
    validated_raw = {}
    for name in ('private_manifest.json','progress.json'):
        validated_raw[('comparison','review',name)] = (root/name).read_bytes()
    private = _source_object(validated_raw[('comparison','review','private_manifest.json')])
    progress = _source_object(validated_raw[('comparison','review','progress.json')])
    gate._equal(progress, review_progress, 'Review progress changed before capture')
    gate._equal(private.get('paired_arms'), list(PAIRED_ARMS), 'Review pair changed')
    gate._equal(private.get('dataset_sha256'), dataset_sha, 'Review data pin changed')
    ids = [item['prompt_id'] for item in items]
    scope = {'dataset_sha256':dataset_sha,'selected_prompt_ids':ids,
             'selected_prompt_sha256':{item['prompt_id']:canonical_hash(item['prompt']) for item in items},
             'arms':arm_configs}
    expected_header = {'schema_version':1,'review_kind':'blinded qualitative rubric review; ungraded',
        'dataset_sha256':dataset_sha,'generation_scope_sha256':canonical_hash(scope),
        'selected_prompt_ids':ids,'reviewer_count':review.REVIEWER_COUNT,
        'reviewer_assignment':'sorted original manifest index modulo 4', 'paired_arms':list(PAIRED_ARMS),
        'include_known_inference_failures':True}
    gate._equal({key:value for key,value in private.items() if key!='packets'}, expected_header,
                'Review header differs from fixed generation scope')
    expected_progress = {'schema_version':1,'status':'ready','selected_count':20,'expected_answer_count':40,
        'observed_answer_count':40,'ready_packet_count':20,'new_packet_count':20,'pending_count':0,
        'answers_snapshot_sha256':gate._sha(files.expected[(('comparison',),'answers.jsonl')]),
        'pending':[],'grading_status':'not_graded'}
    gate._equal(progress, expected_progress, 'Review progress differs from fixed final answers')
    entries, row_index = private.get('packets'), {(row['arm'],row['prompt_id']):row for row in [*controls,*records]}
    gate._require(isinstance(entries,dict) and len(entries) == 20, 'Review must retain twenty pairs')
    expected_names = set()
    for index, item in enumerate(items):
        packet, mapping = review._packet(item, row_index, dataset_sha, index % review.REVIEWER_COUNT, paired_arms=PAIRED_ARMS)
        saved = entries.get(packet['packet_id'])
        expected = mapping | {'first_source_answers_sha256': progress['answers_snapshot_sha256']}
        gate._equal(saved, expected | {'entry_sha256':canonical_hash(expected)}, 'Review mapping differs from received original rows')
        packet_path = root / 'packets' / mapping['packet_filename']
        raw = packet_path.read_bytes()
        gate._equal(_source_object(raw), packet, 'Review packet differs from received original rows')
        validated_raw[('comparison','review','packets',mapping['packet_filename'])] = raw
        expected_names.add(mapping['packet_filename'])
    gate._require(set(os.listdir(files.descriptors[('comparison','review','packets')])) == expected_names,
                'Unexpected or missing review files')
    for relative in [('comparison','review','private_manifest.json'), ('comparison','review','progress.json'),
                     *[('comparison','review','packets',name) for name in sorted(expected_names)]]:
        parts, name = relative[:-1], relative[-1]
        parent = files.descriptors[parts]
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(fd,'rb') as stream:
            before = os.fstat(stream.fileno())
            gate._require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600,
                          'Review files must remain private unaliased regular files')
            raw = stream.read()
            after = os.fstat(stream.fileno())
        gate._require(gate._stamp(before) == gate._stamp(after) == gate._stamp(os.stat(name,dir_fd=parent,follow_symlinks=False)),
                      'Review artifact changed during capture')
        gate._require(raw == validated_raw[relative], 'Review file changed between validation and capture')
        key = (parts,name)
        files.files[key], files.stamps[key], files.expected[key] = gate._inode(before), gate._stamp(before), raw
    files.verify()


def run_source_ledger_cycle(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                            instructions_file: Path, producer_verification: Path, out: Path,
                            *, transport=None, timeout=300, prepare_only: bool = False) -> tuple[dict, int]:
    """Run exactly the one fixed source-index recipe. No resume or pin overrides."""
    gate._require(type(prepare_only) is bool, 'prepare_only must be boolean')
    gate._require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                  'timeout must be finite and positive')
    output, code_root = gate._path(out), gate._path(ROOT)
    private_root = gate._path(ROOT / 'results')
    gate._require(output != private_root and output.is_relative_to(private_root), 'Output must be a dedicated private results directory')
    gate._require(not output.exists(), 'Output must be fresh; overwrite and resume are forbidden')
    code_snapshots = [gate._Snapshot(code_root / name) for name in EXECUTION_CODE_PATHS]
    code_sha = {name: snapshot.sha256 for name, snapshot in zip(EXECUTION_CODE_PATHS, code_snapshots)}
    gate._equal(code_sha['benchmarks/healthbench_source_ledger_preflight.py'], EXPECTED_PREFLIGHT_SHA256,
                'Fixed source-index preflight code differs')
    for name, expected in ledger._PRODUCER_CODE_SHA256.items():
        gate._equal(code_sha[name], expected, 'Pinned producer executable differs')
    gate._require(not any(entry.path.is_relative_to(output) for entry in code_snapshots), 'Output cannot contain execution code')
    started_at, started = utc_now(), time.monotonic()
    root_manifest = {'schema_version': 1, 'status': 'preparing', 'started_at': started_at,
        'experiment': 'fixed system medium2048 cache versus added literal user index; development20 only',
        'execution_support': {'ready': True, 'runner': 'healthbench_source_ledger_cycle'},
        'paired_arms': list(PAIRED_ARMS), 'prepare_only': prepare_only,
        'execution_code_sha256': code_sha, 'score': None, 'score_status': REVIEW_REQUIRED,
        'publication_limit': 'portable POSIX mkdir to first inode capture cannot exclude same-UID empty-directory replacement'}
    gate._publish_private(output, root_manifest, code_snapshots)
    files = _Files(output, root_manifest)
    try:
        proof = ledger.prepare_source_ledger(data, source_manifest, source_comparison_manifest,
            instructions_file, producer_verification, output / 'preflight')
        # Reopen the returned complete fixed profile and reject mutation since
        # its raw pins were validated. Also guard the point-in-time proof itself.
        snapshots = list(code_snapshots)
        seen = {entry.path for entry in snapshots}
        for entry in proof['source_snapshots']:
            if gate._path(Path(entry['path'])) in seen:
                actual = next(value for value in snapshots if value.path == gate._path(Path(entry['path'])))
            else:
                actual = gate._Snapshot(Path(entry['path']))
                snapshots.append(actual)
                seen.add(actual.path)
            gate._equal(actual.sha256, entry['byte_sha256'], 'Source changed after fixed preflight')
        prepared_snapshot = gate._Snapshot(output / 'preflight' / 'manifest.json')
        gate._equal(_source_object(prepared_snapshot.raw), proof, 'Prepared proof changed after publication')
        snapshots.append(prepared_snapshot)
        gate._require(len({entry.stamp[:2] for entry in snapshots}) == len(snapshots), 'Sources and execution files must remain distinct unaliased files')
        items, _ = gate._dataset(next(entry for entry in snapshots if entry.path == gate._path(data)))
        _data_index(items)
        source_snapshot = next(entry for entry in snapshots if entry.path == gate._path(source_manifest))
        source = _source_object(source_snapshot.raw)
        answers_snapshot = next(entry for entry in snapshots if entry.path == gate._path(source_manifest).parent / 'answers.jsonl')
        controls = ledger._rows(answers_snapshot, 'Source answers')
        digest, ids, template_pin = ledger.MODEL_DIGEST, proof['selected_prompt_ids'], ledger.TEMPLATE_SHA256
        candidate_arm, paired_arms, max_tokens, instruction_placement = CANDIDATE_ARM, PAIRED_ARMS, 2048, 'system_prefix'
        changed_fields, placement = ['messages'], True
        payloads, requests = [], []
        for item, evidence, control_row in zip(items, proof['row_provenance'], controls):
            control, payload, index = ledger._ledger_requests(item, GROUNDED_SUFFIX)
            inverse = deepcopy(payload)
            del inverse['messages'][1]
            gate._equal(canonical_hash(inverse), control_row['request_sha256'], 'Inverse does not recover cached request')
            gate._equal(evidence['candidate_request_sha256'], canonical_hash(payload), 'Source-index payload differs from fixed proof')
            gate._equal(evidence['candidate_input_sha256'], canonical_hash(payload['messages']), 'Source-index messages differ from fixed proof')
            gate._equal(evidence['full_row_sha256'], canonical_hash(control_row), 'Cached original row differs')
            request = {'prompt_id': item['prompt_id'], 'prompt_sha256': canonical_hash(item['prompt']),
                'input_sha256': canonical_hash(payload['messages']), 'request_sha256': canonical_hash(payload),
                'dataset_sha256': proof['dataset_byte_sha256'], 'model_digest': digest,
                'instructions_byte_sha256': proof['instructions_byte_sha256'],
                'instructions_name': candidate_arm, 'instructions_version': ledger.REVISION,
                'instruction_placement': 'system_prefix', 'source_index_mode': INDEX_MODE,
                'source_index_sha256': canonical_hash(index),
                'source_index_message_byte_sha256': gate._sha(payload['messages'][1]['content'].encode('utf-8'))}
            payloads.append(payload)
            requests.append(request)
        gate._equal([item['prompt_id'] for item in items], ledger.IDS, 'Development selection changed')
        gate._require(len(payloads) == len(controls) == 20, 'Exactly twenty cached controls and candidates are required')
        candidate_config = deepcopy(source['arms'][ledger.SOURCE_ARM])
        candidate_config['source_index_mode'] = INDEX_MODE
        root_manifest.update(status='prepared' if prepare_only else 'running', source_preflight=proof,
            selected_prompt_ids=ids, cached_source_runtime_template={
                'byte_sha256': template_pin, 'metadata_source': 'cached source-time runtime snapshot; no new control query',
                'bound_source_model_digest': digest, 'bound_source_manifest_byte_sha256': proof['source_byte_hashes']['source_manifest'],
                'plain_api_system_mapping': 'supported by this fixed pinned runtime template'})
        _guard(snapshots, files=files)
        files.json('manifest.json', root_manifest)
    except (ValueError, OSError, _Blocked, UnicodeError) as exc:
        try:
            root_manifest.update(status='blocked', finished_at=utc_now(), calls=0,
                                 error={'code': 'source_or_executor_preflight_failed'}, review={'ready_packet_count': 0})
            files.json('manifest.json', root_manifest)
        finally:
            files.close()
        raise ValueError('Source-index execution preflight rejected; no generation was attempted') from None
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
                          'instructions': {'name': candidate_arm, 'version': ledger.REVISION,
                                           'byte_sha256': proof['instructions_byte_sha256']},
                          'config': {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'timeout': timeout,
                                     'reasoning_effort': 'medium', 'temperature': 0, 'max_tokens': max_tokens,
                                     'maximum_calls_per_case': 1, 'resume': False, 'prepare_only': prepare_only},
                          'source_byte_hashes': proof['source_byte_hashes'], 'execution_code_sha256': deepcopy(code_sha), 'score': None, 'score_status': REVIEW_REQUIRED}
    if placement:
        candidate_manifest['config']['instruction_placement'] = instruction_placement
        candidate_manifest['config']['source_index_mode'] = INDEX_MODE
        candidate_manifest['execution_code_sha256'] = deepcopy(code_sha)
        candidate_manifest['cached_source_runtime_template'] = deepcopy(root_manifest['cached_source_runtime_template'])
    answers_snapshot = next(snapshot for snapshot in snapshots if snapshot.path == gate._path(source_manifest).parent / 'answers.jsonl')
    try:
        files.json('candidate/manifest.json', candidate_manifest)
        files.write('candidate/answers.jsonl', b'')
        files.write('comparison/control_answers.jsonl', answers_snapshot.raw)
        if not prepare_only:
            client = _SourceTransport(transport if transport is not None else
                                    Transport(DEFAULT_BASE_URL, os.environ.get('HEALTHBENCH_API_KEY', ''), timeout),
                                    template_pin=template_pin)
            try:
                for item, request, payload in zip(items, requests, payloads):
                    _guard(snapshots, client, digest, files)
                    candidate_manifest['model']['metadata_status'] = 'available'
                    client.expected_request_sha256 = request['request_sha256']
                    record = _generate(item, payload, request, client)
                    record.update(model_digest=digest, instructions_byte_sha256=proof['instructions_byte_sha256'],
                                  instructions_name=candidate_arm, instructions_version=ledger.REVISION,
                                  model_digest_status='expected_source_pin', digest_before=client.last_digest,
                                  digest_after=None, model_identity_verified=False)
                    if placement:
                        record.update(template_before_sha256=client.last_template_sha256,
                                      template_after_sha256=None, runtime_template_verified=False)
                    records.append(record)
                    files.write('candidate/answers.jsonl', (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8'), append=True)
                    _verify_record(record, request)
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
                       source_status_counts=proof['source_status_counts'], source_known_failure_count=1, source_known_truncation_count=1,
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
                               'arms': {ledger.SOURCE_ARM: deepcopy(source['arms'][ledger.SOURCE_ARM]), candidate_arm: candidate_config},
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
            provenance_manifest['source_index_request_provenance'] = deepcopy(proof['row_provenance'])
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
                         'available_nonempty_pair_count': sum(control['status'] in review.REVIEWABLE and bool(control['answer'].strip())
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
                created_progress = deepcopy(review_status)
                review_status.update(prepared_packet_count=review_status['ready_packet_count'], review_eligible=True)
                _capture_review_artifacts(files, items, controls, records, created_progress, proof['dataset_byte_sha256'], comparison_manifest['arms'])
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
        summary['request_seconds'] = round(sum(row['seconds'] for row in records), 3)
        summary['provider_response_received_count'] = sum(row['attempts'][0]['provider_response_received'] for row in records)
        summary['malformed_provider_response_count'] = sum(bool(row['error'] and row['error']['code'] == 'invalid_provider_response') for row in records)
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
            files.write('received_answers_snapshot.jsonl', b''.join((json.dumps(row, ensure_ascii=False)+'\n').encode('utf-8') for row in records))
            summary['received_answers_snapshot'] = {'path':str(output/'received_answers_snapshot.jsonl'),
                'byte_sha256':gate._sha(files.expected[((),'received_answers_snapshot.jsonl')]), 'count':len(records)}
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
                files.write('received_answers_snapshot.jsonl', b''.join((json.dumps(row, ensure_ascii=False)+'\n').encode('utf-8') for row in records))
                summary['received_answers_snapshot'] = {'path':str(output/'received_answers_snapshot.jsonl'),
                    'byte_sha256':gate._sha(files.expected[((),'received_answers_snapshot.jsonl')]), 'count':len(records)}
                save_json('summary.json', summary)
                save_json('integrity_failure.json', {'status': 'blocked', 'calls': summary['calls'],
                                                      'error': error, 'artifacts': integrity_failures})
        return summary, code
    finally:
        files.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'source-manifest', 'source-comparison-manifest', 'instructions-file', 'producer-verification', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        summary, code = run_source_ledger_cycle(args.data, args.source_manifest, args.source_comparison_manifest,
            args.instructions_file, args.producer_verification, args.out, timeout=args.timeout, prepare_only=args.prepare_only)
    except (ValueError, OSError, UnicodeError):
        print('Source-index cycle rejected or stopped; inspect private artifacts for actual calls and state', file=sys.stderr)
        return 2
    print(json.dumps({'status': summary['status'], 'calls': summary['calls'], 'model_metadata_calls': summary['model_metadata_calls'],
        'template_queries': summary['template_queries'], 'unattempted_answers': summary['unattempted_answers'],
        'review_status': summary['review']['status'], 'score': None}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
