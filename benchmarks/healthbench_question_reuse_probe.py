"""HBT03: four pinned first-response question-reuse replacements, one attempt per case.

CLI: prepare --plan <saved design plan> --out <private scope>/execution-v1;
step --out <same> [--case-id HB-V0000]; export-blind --out <same> --dest
<private scope>/blind-v1. No import/preparation/export invokes a model. The
fixed scope accepts one preparation only. Never delete its owner, reservations,
or locks to retry: an unfinished reservation means dispatch is unknown.

This selected development regression has historical controls; scores remain
null. Blinding hides configuration/arm identities, not clinical content.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time

from .healthbench import (ROOT, DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX,
                          RequestFailure, Transport, canonical_hash, utc_now, usage_values)
from .healthbench_budget_cycle import _Blocked, _guard
from .healthbench_budget_preflight import _Snapshot, _path, _require, _sha, _inode
from .healthbench_codex_cycle import _source_object
from .healthbench_source_ledger_cycle import _SourceTransport
from .healthbench_turn_probe import (CASE_IDS, DATASET_SHA256, MODEL_DIGEST,
                                    TEMPLATE_SHA256, TASK_WRAPPER, SOURCE_MODULES, _json_bytes)

DESIGN_SHA256 = '93c605f5b61143a669380a83ecf3b396dca572f605ef44206020363ae34af924'
PREFIX_SHA256 = 'f7949cdd133a6d3372a952db6db7e592a9939488992a268d7100d6582c03ac58'
REFERENCE_SHA256 = '546a1dbc48df77a47f3a59c38c6e724f2a0763e1d29aad2972f26df7f715465e'
ADDITION = ('Before asking a question, check whether the user has already clearly answered it. '
            'Re-ask only to resolve ambiguity, conflicting information, or a relevant change over time, '
            'and make that reason explicit.')
ADDITION_SHA256 = '39647293df20d48c7f762084c37c64978ab3b03ed40bdcb258152bf04377a941'
SCOPE_NAME = 'codex-healthbench-question-reuse-probe-v1'
EXECUTION_PATH = Path(__file__).resolve()
SUPPORT_PATHS = (*SOURCE_MODULES, 'benchmarks/healthbench_turn_probe.py')
SETTINGS = {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'reasoning_effort': 'medium',
            'max_tokens': 2048, 'temperature': 0, 'timeout': 90}
SCORE_STATUS = 'selected_development_historical_control_unscored'


def _scope() -> Path:
    return _path(ROOT / 'results' / SCOPE_NAME)


def _inside(path: Path, parent: Path) -> Path:
    path = _path(Path(path))
    _require(path != parent and path.is_relative_to(parent), 'A dedicated private descendant path is required')
    return path


class _Directory:
    """Nofollow directory handles, immutable file creation and fsync barriers."""
    def __init__(self, path):
        self.path = _path(path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.identity = _inode(os.fstat(self.fd))
        self.check()

    def check(self):
        _require(_path(self.path) == self.path and _inode(self.path.stat()) == self.identity,
                 'Private directory identity changed')
        _require(stat.S_IMODE(os.fstat(self.fd).st_mode) == 0o700, 'Private directory permission changed')

    def write(self, name, value):
        _require(Path(name).name == name and name not in {'.', '..'}, 'Invalid artifact name')
        self.check()
        raw = _json_bytes(value)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)
        self.check()
        return _Snapshot(self.path / name)

    def close(self):
        os.close(self.fd)


def _mkdir(path):
    """Parents must already exist and have no aliases; create one private leaf."""
    path = _path(path)
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = _inode(os.fstat(parent))
        os.mkdir(path.name, mode=0o700, dir_fd=parent)
        os.fsync(parent)
        _require(_inode(path.parent.stat()) == identity, 'Private parent identity changed')
    finally:
        os.close(parent)
    return _Directory(path)


@contextmanager
def _locked_scope(*, create=False):
    scope = _scope()
    if create and not scope.exists():
        try:
            created = _mkdir(scope)
            created.close()
        except FileExistsError:
            pass
    directory = _Directory(scope)
    lock = None
    try:
        lock = os.open('.writer.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                       0o600, dir_fd=directory.fd)
        os.fsync(lock)
        os.fsync(directory.fd)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
            os.unlink('.writer.lock', dir_fd=directory.fd)
            os.fsync(directory.fd)
        directory.close()


def _sources(plan_path: Path):
    plan_file = _Snapshot(_inside(plan_path, _path(ROOT / 'results')))
    _require(plan_file.sha256 == DESIGN_SHA256, 'Saved design plan pin differs')
    plan = _source_object(plan_file.raw)
    prefix_file = _Snapshot(plan_file.path.parent / 'frozen-prefix-manifest-v1.json')
    _require(prefix_file.sha256 == PREFIX_SHA256, 'Frozen prefix manifest pin differs')
    prefixes = _source_object(prefix_file.raw)['prefixes']
    _require(plan['single_factor']['exact_text'] == ADDITION
             and _sha(ADDITION.encode()) == ADDITION_SHA256
             and plan['single_factor']['utf8_sha256'] == ADDITION_SHA256,
             'Candidate instruction differs')
    _require(canonical_hash(plan['fixed']['settings']) == canonical_hash(SETTINGS), 'Fixed settings differ')
    _require(plan['fixed']['model_digest'] == MODEL_DIGEST
             and plan['fixed']['template_byte_sha256'] == TEMPLATE_SHA256
             and plan['fixed']['source_dataset_sha256'] == DATASET_SHA256,
             'Fixed source/model/template pins differ')
    _require([value['case_id'] for value in prefixes] == list(CASE_IDS), 'Fixed case scope differs')
    snapshots = [plan_file, prefix_file, _Snapshot(_path(EXECUTION_PATH))]
    data = _Snapshot(_path(ROOT / 'private_data/healthbench/legacy_consensus_val_roles.json'))
    _require(data.sha256 == DATASET_SHA256, 'Dataset pin differs')
    snapshots.append(data)  # Byte pin only: no heldout/rubric content is parsed.
    for relative in SUPPORT_PATHS:
        captured = _Snapshot(_path(ROOT / relative))
        if relative == 'benchmarks/healthbench_turn_probe.py':
            _require(captured.sha256 == REFERENCE_SHA256, 'HBT01 reference runner pin differs')
        snapshots.append(captured)
    requests, controls = {}, {}
    for prefix in prefixes:
        captured = _Snapshot(_inside(ROOT / prefix['source_record'], _path(ROOT / 'results')))
        _require(captured.sha256 == prefix['source_record_byte_sha256'], 'Historical record pin differs')
        source = _source_object(captured.raw)
        old = source['request']
        case_id = prefix['case_id']
        _require(source['case_id'] == case_id and source['doctor_turn'] == prefix['doctor_turn_to_replace'] == 1
                 and source['status'] == 'completed' and source['finish_reason'] == 'stop'
                 and isinstance(source['answer'], str) and bool(source['answer'].strip()),
                 'Historical target identity or completion differs')
        _require(canonical_hash(old) == source['request_sha256'] == prefix['historical_request_sha256']
                 and canonical_hash(old['messages']) == prefix['historical_input_sha256']
                 and _sha(source['answer'].encode()) == prefix['historical_answer_utf8_sha256'],
                 'Historical payload or answer pin differs')
        _require(set(old) == {'model', 'messages', 'reasoning_effort', 'max_tokens', 'temperature'}
                 and canonical_hash({k: old[k] for k in old if k != 'messages'}) == canonical_hash(
                     {k: SETTINGS[k] for k in ('model', 'reasoning_effort', 'max_tokens', 'temperature')}),
                 'Historical request settings differ')
        messages = old['messages']
        _require([m['role'] for m in messages] == prefix['message_roles']
                 and len(messages) == prefix['message_count']
                 and all(set(m) == {'role', 'content'} and isinstance(m['content'], str) for m in messages)
                 and messages[0] == {'role': 'system', 'content': GROUNDED_SUFFIX + '\n\n' + TASK_WRAPPER}
                 and all(m['role'] in {'user', 'assistant'} for m in messages[1:])
                 and messages[-1]['role'] == 'user', 'Historical context structure differs')
        request = deepcopy(old)
        request['messages'][0]['content'] += '\n\n' + ADDITION
        _require(canonical_hash(request) == prefix['planned_candidate_request_sha256']
                 and canonical_hash(request['messages']) == prefix['planned_candidate_input_sha256']
                 and _sha(messages[0]['content'].encode()) == prefix['old_system_utf8_sha256']
                 and _sha(request['messages'][0]['content'].encode()) == prefix['new_system_utf8_sha256'],
                 'Exact one-factor candidate request differs')
        requests[case_id], controls[case_id] = request, source['answer']
        snapshots.append(captured)
    _guard(snapshots)
    return snapshots, requests, controls


def _file_pins(snapshots):
    return [{'path': str(value.path), 'byte_sha256': value.sha256} for value in snapshots]


def prepare(plan_path: Path, out: Path) -> dict:
    out = _inside(out, _scope())
    _require(out.parent == _scope(), 'Run must be an immediate child of the fixed experiment scope')
    snapshots, requests, _ = _sources(Path(plan_path))
    with _locked_scope(create=True) as scope:
        _require(not (scope.path / 'owner.json').exists() and not (scope.path / 'blocked.json').exists(),
                 'This candidate already has an experiment owner; another preparation is forbidden')
        run = _mkdir(out)
        try:
            ordering = list(CASE_IDS)
            secrets.SystemRandom().shuffle(ordering)
            labels = ['A', 'A', 'B', 'B']
            secrets.SystemRandom().shuffle(labels)
            blind_map = {'created_at': utc_now(), 'cases': {
                f'P{i:02d}': {'case_id': case_id, 'candidate_label': labels[i - 1]}
                for i, case_id in enumerate(ordering, 1)}}
            blind_file = run.write('blind-map.json', blind_map)
            manifest = {'schema_version': 1, 'experiment': 'HBT03', 'created_at': utc_now(),
                        'plan_path': str(snapshots[0].path), 'source_files': _file_pins(snapshots),
                        'case_ids': list(CASE_IDS), 'maximum_calls': 4, 'requests_per_case': 1,
                        'blind_map_byte_sha256': blind_file.sha256,
                        'request_sha256': {key: canonical_hash(value) for key, value in requests.items()},
                        'settings': SETTINGS, 'score': None, 'score_status': SCORE_STATUS}
            manifest_file = run.write('manifest.json', manifest)
            _guard([*snapshots, blind_file, manifest_file])
            scope.write('owner.json', {'out': str(out), 'manifest_byte_sha256': manifest_file.sha256,
                                      'blind_map_byte_sha256': blind_file.sha256,
                                      'candidate_instruction_sha256': ADDITION_SHA256})
        finally:
            run.close()
    return {'status': 'prepared', 'out': str(out), 'calls': 0, 'maximum_generation_calls': 4, 'score': None}


def _load(scope, out):
    owner_file = _Snapshot(scope.path / 'owner.json')
    owner = _source_object(owner_file.raw)
    _require(owner['out'] == str(out) and owner['candidate_instruction_sha256'] == ADDITION_SHA256,
             'Experiment owner differs')
    manifest_file, map_file = _Snapshot(out / 'manifest.json'), _Snapshot(out / 'blind-map.json')
    _require(manifest_file.sha256 == owner['manifest_byte_sha256']
             and map_file.sha256 == owner['blind_map_byte_sha256'], 'Run manifest or blind map changed')
    manifest = _source_object(manifest_file.raw)
    blind_map = _source_object(map_file.raw)
    mappings = blind_map['cases']
    _require(set(mappings) == {'P01', 'P02', 'P03', 'P04'}
             and sorted(value['case_id'] for value in mappings.values()) == sorted(CASE_IDS)
             and sorted(value['candidate_label'] for value in mappings.values()) == ['A', 'A', 'B', 'B'],
             'Balanced pre-generation blind map differs')
    snapshots, requests, controls = _sources(Path(manifest['plan_path']))
    _require(manifest['source_files'] == _file_pins(snapshots)
             and manifest['blind_map_byte_sha256'] == map_file.sha256
             and manifest['request_sha256'] == {k: canonical_hash(v) for k, v in requests.items()},
             'Prepared source/execution provenance changed')
    snapshots.extend([owner_file, manifest_file, map_file])
    records = {}
    for case_id in CASE_IDS:
        reservation_path = scope.path / (case_id + '-reserved.json')
        finished_path = scope.path / (case_id + '-finished.json')
        if not reservation_path.exists():
            _require(not finished_path.exists() and not (out / (case_id + '.json')).exists()
                     and not (out / (case_id + '-post.json')).exists(), 'Orphaned evidence cannot be resumed')
            continue
        reservation = _Snapshot(reservation_path)
        value = _source_object(reservation.raw)
        _require(value['out'] == str(out) and value['case_id'] == case_id
                 and value['request_sha256'] == canonical_hash(requests[case_id]), 'Reservation identity changed')
        _require(finished_path.exists(), 'Pending dispatch is unknown and cannot be retried')
        finished = _Snapshot(finished_path)
        receipt = _source_object(finished.raw)
        row_file, post_file = _Snapshot(out / (case_id + '.json')), _Snapshot(out / (case_id + '-post.json'))
        _require(receipt == {'reservation_byte_sha256': reservation.sha256,
                            'record_byte_sha256': row_file.sha256, 'post_byte_sha256': post_file.sha256},
                 'Persisted call evidence changed')
        row, post = _source_object(row_file.raw), _source_object(post_file.raw)
        _require(row['case_id'] == case_id and row['request'] == requests[case_id]
                 and row['request_sha256'] == canonical_hash(requests[case_id])
                 and row['input_sha256'] == canonical_hash(requests[case_id]['messages'])
                 and row['attempts'] == 1 and row['retries'] == 0 and row['score'] is None,
                 'Stored generation identity differs')
        _require(post['stop_reason'] is None and post['model_identity_verified'] is True
                 and post['runtime_template_verified'] is True and post['digest_after'] == MODEL_DIGEST
                 and post['template_after_sha256'] == TEMPLATE_SHA256,
                 'Earlier identity failure permanently stopped experiment')
        records[case_id] = row
        snapshots.extend([reservation, finished, row_file, post_file])
    _guard(snapshots)
    return snapshots, requests, controls, records, blind_map


def _block(scope, code):
    if not (scope.path / 'blocked.json').exists():
        scope.write('blocked.json', {'at': utc_now(), 'code': code, 'retry': False})


def step(out: Path, case_id: str | None = None, *, transport=None) -> dict:
    out = _inside(out, _scope())
    with _locked_scope() as scope:
        _require(not (scope.path / 'blocked.json').exists(), 'Experiment is permanently blocked')
        try:
            snapshots, requests, _, records, _ = _load(scope, out)
        except (ValueError, OSError, _Blocked, UnicodeError, KeyError, TypeError):
            _block(scope, 'source_or_evidence_changed_or_pending')
            raise
        _require(case_id is None or case_id in CASE_IDS, 'Unknown case id')
        if case_id is None:
            remaining = [key for key in CASE_IDS if key not in records]
            _require(bool(remaining), 'All four requests already consumed')
            case_id = remaining[0]
        _require(case_id not in records and len(records) < 4, 'Request already consumed; no repeat allowed')
        run = _Directory(out)
        try:
            client = _SourceTransport(transport if transport is not None else Transport(DEFAULT_BASE_URL, '', 90),
                                      template_pin=TEMPLATE_SHA256)
            try:
                _guard(snapshots, client, MODEL_DIGEST)
                run.check()
                scope.check()
            except (ValueError, OSError, _Blocked, KeyboardInterrupt):
                scope.write('pre-dispatch-failure.json', {'model_metadata_calls': client.metadata_calls,
                    'template_queries': client.template_queries, 'actual_generation_calls': 0,
                    'digest': client.last_digest, 'template_sha256': client.last_template_sha256})
                _block(scope, 'pre_dispatch_pin_check_failed')
                return {'status': 'blocked', 'calls_this_step': 0, 'score': None}
            payload = deepcopy(requests[case_id])
            request_hash = canonical_hash(payload)
            reservation = scope.write(case_id + '-reserved.json', {
                'out': str(out), 'case_id': case_id, 'request_sha256': request_hash,
                'reserved_at': utc_now(), 'sequence': len(records) + 1,
                'dispatch_if_unfinished': 'unknown_no_retry'})
            # Every file/identity guard is repeated after the durable reservation;
            # interruption here consumes permission conservatively without retry.
            _guard([*snapshots, reservation])
            client.expected_request_sha256 = request_hash
            started = time.monotonic()
            row = {'case_id': case_id, 'request': payload, 'request_sha256': request_hash,
                   'input_sha256': canonical_hash(payload['messages']), 'attempts': 1, 'retries': 0,
                   'started_at': utc_now(), 'status': 'failed', 'answer': '', 'finish_reason': None,
                   'error': None, 'usage': {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None},
                   'digest_before': client.last_digest, 'template_before_sha256': client.last_template_sha256,
                   'score': None, 'score_status': SCORE_STATUS}
            try:
                response = client.complete(payload)
                choice = response['choices'][0]
                row.update(answer=choice['message'].get('content') or '', finish_reason=choice['finish_reason'],
                           usage=usage_values(response))
                row['status'] = ('failed' if not row['answer'].strip() else
                                 'completed' if row['finish_reason'] == 'stop' else 'truncated')
                if row['status'] == 'failed':
                    row['error'] = {'code': 'empty_answer', 'http_status': None}
            except RequestFailure as exc:
                row.update(status='failed' if client.provider_response_received else 'blocked',
                           error={'code': exc.code, 'http_status': exc.http_status},
                           usage=deepcopy(client.provider_usage))
            row.update(seconds=round(time.monotonic() - started, 3), finished_at=utc_now(),
                       actual_generation_calls=client.calls,
                       provider_response_received=client.provider_response_received,
                       received_answer=client.received_answer_text, reasoning_retained=False)
            row_file = run.write(case_id + '.json', row)
            post_error = None
            try:
                _guard([*snapshots, reservation, row_file], client, MODEL_DIGEST)
                run.check()
                scope.check()
            except (ValueError, OSError, _Blocked, KeyboardInterrupt):
                post_error = 'post_dispatch_pin_check_failed'
            post_file = run.write(case_id + '-post.json', {
                'digest_after': client.last_digest, 'template_after_sha256': client.last_template_sha256,
                'model_identity_verified': post_error is None, 'runtime_template_verified': post_error is None,
                'model_metadata_calls': client.metadata_calls, 'template_queries': client.template_queries,
                'stop_reason': post_error})
            scope.write(case_id + '-finished.json', {'reservation_byte_sha256': reservation.sha256,
                        'record_byte_sha256': row_file.sha256, 'post_byte_sha256': post_file.sha256})
            if post_error or row['status'] == 'blocked' or client.interrupted:
                _block(scope, post_error or 'unknown_transport_outcome')
            return {'status': 'blocked' if post_error else row['status'], 'case_id': case_id,
                    'calls_this_step': client.calls, 'consumed_cases': len(records) + 1,
                    'record_path': str(row_file.path), 'score': None}
        finally:
            run.close()


def export_blind(out: Path, dest: Path) -> dict:
    """Export only common non-system history and coded literal next replies."""
    out, dest = _inside(out, _scope()), _inside(dest, _scope())
    _require(dest.parent == _scope(), 'Blind export must be a direct private scope child')
    with _locked_scope() as scope:
        _require(not (scope.path / 'blocked.json').exists(), 'Blocked experiment cannot export review packets')
        snapshots, requests, controls, records, blind_map = _load(scope, out)
        _require(set(records) == set(CASE_IDS) and all(row['status'] == 'completed'
                 and row['finish_reason'] == 'stop' and row['answer'].strip() for row in records.values()),
                 'Review requires all four complete replies and verified post-call pins')
        directory = _mkdir(dest)
        try:
            for code, mapping in blind_map['cases'].items():
                case_id, candidate_label = mapping['case_id'], mapping['candidate_label']
                row = records[case_id]
                arms = {candidate_label: {'answer': row['answer'], 'status': row['status']},
                        'B' if candidate_label == 'A' else 'A': {'answer': controls[case_id], 'status': 'completed'}}
                directory.write(code + '.json', {'case_code': code,
                    'context': deepcopy(requests[case_id]['messages'][1:]),
                    'replies': {label: arms[label] for label in ('A', 'B')}})
            _guard(snapshots)
            directory.check()
        finally:
            directory.close()
    return {'status': 'exported', 'packets': 4, 'dest': str(dest), 'score': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prep = commands.add_parser('prepare')
    prep.add_argument('--plan', required=True, type=Path)
    prep.add_argument('--out', required=True, type=Path)
    advance = commands.add_parser('step')
    advance.add_argument('--out', required=True, type=Path)
    advance.add_argument('--case-id', choices=CASE_IDS)
    export = commands.add_parser('export-blind')
    export.add_argument('--out', required=True, type=Path)
    export.add_argument('--dest', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = (prepare(args.plan, args.out) if args.command == 'prepare' else
                  step(args.out, args.case_id) if args.command == 'step' else export_blind(args.out, args.dest))
    except (ValueError, OSError, _Blocked, UnicodeError, KeyError, TypeError):
        print('Probe rejected or stopped; inspect private evidence. No automatic retry.', file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in {'prepared', 'completed', 'exported'} else 2


if __name__ == '__main__':
    sys.exit(main())
