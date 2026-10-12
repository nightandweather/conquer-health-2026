"""Offline fixture ordering prototype, never a live transport adapter.

Only registry fixtures are accepted. No endpoint, credential or transport
argument exists. The parser is an explicit synthetic stub, not a claim of
production-parser equivalence. Receipts contain helper metadata and fixed
operational enums; fixture bodies and exception details are never written.

API: prepare(scope, fixture_names, out_name='run'); run_case(scope, 'F01',
faults=()); recover(scope); synthetic_eligibility(scope). Each test must use a
fresh disposable scope. Existing owner, locks and consumed reservations must
never be deleted to retry. Fsync fault tests are not universal power-loss proof.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .response_receipt import response_metadata
from . import response_receipt as _receipt_helper

CASE_IDS = ('F01', 'F02', 'F03', 'F04')
HELPER_SHA256 = 'b4487c37f09a2e8c56ecc81676a72130f7fb5ee8a5ec46825778c92dfb622ceb'
WRITE_STAGES = ('reservation', 'marker', 'structure', 'terminal')
WRITE_FAULTS = ('short_write', 'flush', 'file_fsync', 'dir_fsync', 'interrupt')
EVENT_STAGES = ('before_reservation', 'after_reservation', 'after_marker', 'after_fake',
                'after_return', 'after_structure', 'after_parser', 'helper', 'parser', 'post_pins')
SEMANTIC_OUTCOMES = ('complete', 'empty', 'truncated', 'rejected', 'parser_failed')
FIXTURE_NAMES = ('normal', 'null', 'empty', 'whitespace', 'length', 'malformed', 'bad_choice',
                 'reasoning_only', 'missing_usage', 'invalid_usage', 'overflow_usage',
                 'invalid_unicode', 'dispatch_error', 'parser_error')
_AUDIT = {'fake_invocations': 0, 'helper_calls': 0, 'parser_calls': 0,
          'fault_injections': 0, 'blocked_reentries': 0}


def audit_counters():
    """In-memory synthetic counters only, independent from recovered evidence."""
    return dict(_AUDIT)


@dataclass(frozen=True)
class Fault:
    stage: str
    operation: str

    def __post_init__(self):
        valid = ((self.stage in WRITE_STAGES or self.stage in ('failure', 'commit', 'blocked')) and self.operation in WRITE_FAULTS
                 or self.stage in EVENT_STAGES and self.operation in ('interrupt', 'exception'))
        if not valid:
            raise ValueError('Invalid fixed offline fault point')


class _Rejected(Exception):
    def __init__(self, code):
        self.code = code


def _require(condition, code='integrity_failed'):
    if not condition:
        raise _Rejected(code)


def _hit(faults, stage, operation):
    if any(f.stage == stage and f.operation == operation for f in faults):
        _AUDIT['fault_injections'] += 1
        if operation == 'interrupt':
            raise KeyboardInterrupt('SYNTHETIC_SECRET_INTERRUPT')
        raise OSError('SYNTHETIC_SECRET_STORAGE_OR_STAGE')


def _event(faults, stage):
    _hit(faults, stage, 'interrupt')
    _hit(faults, stage, 'exception')


def _bytes(value):
    return (json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'),
                       allow_nan=False) + '\n').encode('utf-8')


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return _hash(_bytes(value))


def _safe_path(path):
    _require(type(path) is Path or isinstance(path, Path), 'invalid_path')
    original = path
    path = Path(os.path.abspath(path))
    _require('..' not in original.parts and not any(p.is_symlink() for p in (path, *path.parents))
             and path == path.resolve(), 'invalid_path')
    return path


def _read_file(path):
    path = _safe_path(path)
    before = path.stat()
    _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read()
        after = os.fstat(fd)
        _require((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                 == (after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        final = path.stat()
        _require((final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        return raw
    finally:
        os.close(fd)


class _Directory:
    def __init__(self, path):
        self.path = _safe_path(path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        st = os.fstat(self.fd)
        self.identity = (st.st_dev, st.st_ino)
        try:
            self.check()
        except BaseException:
            os.close(self.fd)
            raise

    def check(self):
        path = _safe_path(self.path)
        st = path.stat()
        _require((st.st_dev, st.st_ino) == self.identity and stat.S_IMODE(st.st_mode) == 0o700)

    def close(self):
        os.close(self.fd)

    def exists(self, name):
        self.check()
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def read(self, name):
        self.check()
        raw = _read_file(self.path / name)
        value = json.loads(raw)
        _require(type(value) is dict and set(value) == {'body', 'sha256'}
                 and type(value['body']) is dict and value['sha256'] == _canonical(value['body']))
        self.check()
        return value['body'], _hash(raw)

    def write(self, name, body, *, stage='', faults=()):
        _require(Path(name).name == name and name not in ('.', '..'))
        self.check()
        raw = _bytes({'body': body, 'sha256': _canonical(body)})
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                if any(f.stage == stage and f.operation == 'short_write' for f in faults):
                    stream.write(raw[:max(1, len(raw) // 2)])
                    stream.flush()
                    _hit(faults, stage, 'short_write')
                stream.write(raw)
                _hit(faults, stage, 'interrupt')
                _hit(faults, stage, 'flush')
                stream.flush()
                _hit(faults, stage, 'file_fsync')
                os.fsync(fd)
            _hit(faults, stage, 'dir_fsync')
            os.fsync(self.fd)
        finally:
            os.close(fd)
        self.check()
        _require(_read_file(self.path / name) == raw)
        return _hash(raw)


def _mkdir(path):
    path = _safe_path(path)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        before = os.fstat(parent_fd)
        os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        after = path.parent.stat()
        _require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino))
    finally:
        os.close(parent_fd)
    return _Directory(path)


@contextmanager
def _locked(scope, *, create=False):
    scope = _safe_path(scope)
    if create and not scope.exists():
        try:
            made = _mkdir(scope)
            made.close()
        except FileExistsError:
            pass
    directory = _Directory(scope)
    lock_fd = None
    try:
        lock_fd = os.open('.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory.fd)
        os.fsync(lock_fd)
        os.fsync(directory.fd)
        yield directory
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            os.unlink('.lock', dir_fd=directory.fd)
            os.fsync(directory.fd)
        directory.close()


def _fixture(name):
    _require(name in FIXTURE_NAMES, 'invalid_fixture')
    content = {'null': None, 'empty': '', 'whitespace': ' \t\n', 'reasoning_only': None,
               'invalid_unicode': 'x\ud800y'}.get(name, 'SYNTHETIC_ANSWER_SECRET_한글')
    value = {'id': 'SYNTHETIC_ID_SECRET', 'choices': [
        {'index': 9, 'message': {'role': 'assistant', 'content': content,
                               'reasoning': 'SYNTHETIC_REASONING_SECRET'},
         'finish_reason': 'length' if name == 'length' else 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7}}
    if name == 'malformed':
        return {'choices': None, 'secret': 'SYNTHETIC_BAD_ENVELOPE_SECRET'}
    if name == 'bad_choice':
        value['choices'].insert(0, None)
    if name == 'missing_usage':
        value.pop('usage')
    if name == 'invalid_usage':
        value['usage'] = {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': True,
                          'completion_tokens_details': {'reasoning_tokens': 2},
                          'prompt_tokens_details': {'cached_tokens': None}}
    if name == 'overflow_usage':
        value['usage'] = {'prompt_tokens': 2**63 - 1, 'completion_tokens': 2**63 - 1,
                          'total_tokens': 2**63}
    if name == 'parser_error':
        value['synthetic_parser_exception'] = True
    return value


def _source_pins():
    module = Path(__file__).resolve()
    helper = module.with_name('response_receipt.py')
    helper_hash = _hash(_read_file(helper))
    _require(helper_hash == HELPER_SHA256, 'pin_mismatch')
    return {'adapter_sha256': _hash(_read_file(module)), 'helper_sha256': helper_hash,
            'fixture_table_sha256': _canonical({name: _fixture(name) for name in FIXTURE_NAMES}),
            'parser_stub_version': 1}


def _fake_dispatch(name):
    _AUDIT['fake_invocations'] += 1
    if name == 'dispatch_error':
        raise RuntimeError('SYNTHETIC_SECRET_TRANSPORT_BODY_AND_PATH')
    return _fixture(name)


def _classify_fixture(value):
    """Pure synthetic classification, also used to validate recovered semantics."""
    if type(value) is dict and value.get('synthetic_parser_exception') is True:
        raise RuntimeError('SYNTHETIC_SECRET_PARSER_BODY_AND_PATH')
    if type(value) is not dict or type(value.get('choices')) is not list or not value['choices']:
        return 'rejected'
    choice = value['choices'][0]
    if type(choice) is not dict or type(choice.get('message')) is not dict:
        return 'rejected'
    content, finish = choice['message'].get('content'), choice.get('finish_reason')
    if type(content) is not str:
        return 'empty' if content is None else 'rejected'
    try:
        content.encode('utf-8', errors='strict')
    except UnicodeEncodeError:
        return 'rejected'
    if not content.strip():
        return 'empty'
    return 'complete' if finish == 'stop' else 'truncated' if finish == 'length' else 'rejected'


def _expected_semantic(name):
    if name == 'dispatch_error':
        return None  # A fixture that never returns cannot produce a terminal.
    if name == 'parser_error':
        return 'parser_failed'
    return _classify_fixture(_fixture(name))


def _parser_stub(value):
    """Count actual stub entry; this is not production semantic equivalence."""
    _AUDIT['parser_calls'] += 1
    return _classify_fixture(value)


def _block(scope, code, faults=()):
    if not scope.exists('blocked.json'):
        try:
            scope.write('blocked.json', {'stage': 'blocked', 'code': code}, stage='blocked', faults=faults)
        except (Exception, KeyboardInterrupt):
            pass  # A partial block file is itself a permanent fail-closed marker.


def _fail(code, stage='entry'):
    return {'status': 'blocked', 'stage': stage, 'code': code,
            'new_model_calls': 0, 'new_live_metadata_queries': 0, 'new_live_template_queries': 0}


def prepare(scope: Path, fixture_names=('normal',) * 4, *, out_name='run'):
    """Bind one permanent owner to four registered fixture slots; no fake calls."""
    try:
        _require(type(fixture_names) in (tuple, list) and len(fixture_names) == 4
                 and all(type(name) is str and name in FIXTURE_NAMES for name in fixture_names), 'invalid_fixture')
        _require(type(out_name) is str and re.fullmatch('[a-z][a-z0-9_-]{0,31}', out_name) is not None,
                 'invalid_output_name')
        pins = _source_pins()
        with _locked(scope, create=True) as directory:
            _require(not directory.exists('owner.json') and not directory.exists('blocked.json'), 'owner_exists')
            run = _mkdir(directory.path / out_name)
            try:
                owner = {'kind': 'owner', 'out_name': out_name, 'source_pins': pins,
                         'fixtures': dict(zip(CASE_IDS, fixture_names)),
                         'request_sha256': {case: _canonical({'case': case, 'fixture': name,
                                                            'fixture_sha256': _canonical(_fixture(name))})
                                            for case, name in zip(CASE_IDS, fixture_names)}}
                _require(_bytes(_source_pins()) == _bytes(pins), 'pin_mismatch')
                directory.write('owner.json', owner)
            finally:
                run.close()
        return {'status': 'prepared', 'fixture_slots': 4, 'fake_invocations': 0, 'new_model_calls': 0}
    except _Rejected as error:
        return _fail(error.code, 'prepare')
    except (Exception, KeyboardInterrupt):
        return _fail('operation_failed', 'prepare')


def _load(scope):
    owner, owner_hash = scope.read('owner.json')
    _require(set(owner) == {'kind', 'out_name', 'source_pins', 'fixtures', 'request_sha256'}
             and owner['kind'] == 'owner' and set(owner['fixtures']) == set(CASE_IDS)
             and set(owner['request_sha256']) == set(CASE_IDS))
    _require(_bytes(_source_pins()) == _bytes(owner['source_pins']), 'pin_mismatch')
    _require(type(owner['out_name']) is str
             and re.fullmatch('[a-z][a-z0-9_-]{0,31}', owner['out_name']) is not None)
    for case_id, name in owner['fixtures'].items():
        _require(type(name) is str and name in FIXTURE_NAMES)
        _require(owner['request_sha256'][case_id] == _canonical(
            {'case': case_id, 'fixture': name, 'fixture_sha256': _canonical(_fixture(name))}))
    return owner, owner_hash, _Directory(scope.path / owner['out_name'])


def _artifact(scope, run, case_id, stage):
    directory = scope if stage in ('reservation', 'marker') else run
    name = case_id + '-' + stage + '.json'
    return directory, name


def _body(kind, case_id, owner_hash, payload):
    return {'kind': kind, 'case_id': case_id, 'owner_sha256': owner_hash, 'payload': payload}


def _read_chain(scope, run, owner, owner_hash, case_id):
    artifacts = {}
    for stage in (*WRITE_STAGES, 'commit', 'failure'):
        directory, name = _artifact(scope, run, case_id, stage)
        if directory.exists(name):
            body, digest = directory.read(name)
            _require(set(body) == {'kind', 'case_id', 'owner_sha256', 'payload'}
                     and body['kind'] == stage and body['case_id'] == case_id
                     and body['owner_sha256'] == owner_hash and type(body['payload']) is dict)
            artifacts[stage] = (body['payload'], digest)
    if not artifacts:
        return {'consumed': False, 'dispatch_possible': False, 'parsed_object_observed': None,
                'structural_receipt_persisted': None, 'parser_entered': None,
                'terminal_receipt_persisted': None, 'semantic_outcome': None, 'post_pin_check': None}
    _require('reservation' in artifacts)
    reservation, reserve_hash = artifacts['reservation']
    _require(_bytes(reservation) == _bytes({'request_sha256': owner['request_sha256'][case_id], 'retry': False}))
    state = {'consumed': True, 'dispatch_possible': None, 'parsed_object_observed': None,
             'structural_receipt_persisted': None, 'parser_entered': None,
             'terminal_receipt_persisted': None, 'semantic_outcome': None, 'post_pin_check': None}
    if 'marker' in artifacts:
        marker, marker_hash = artifacts['marker']
        _require(_bytes(marker) == _bytes({'reservation_sha256': reserve_hash, 'dispatch_possible': True}))
        state['dispatch_possible'] = True
    if 'structure' in artifacts:
        _require('marker' in artifacts)
        structure, structure_hash = artifacts['structure']
        _require(set(structure) == {'marker_sha256', 'parsed_object_observed', 'metadata'}
                 and structure['marker_sha256'] == marker_hash
                 and structure['parsed_object_observed'] is True
                 and _bytes(structure['metadata']) == _bytes(_receipt_helper.response_metadata(_fixture(owner['fixtures'][case_id]))))
        state['parsed_object_observed'] = True
        # A readable uncommitted artifact proves availability now, not completion
        # of an earlier fsync barrier. Terminal/failure links supply that evidence.
    if 'terminal' in artifacts:
        _require('structure' in artifacts)
        terminal, terminal_hash = artifacts['terminal']
        _require(set(terminal) == {'reservation_sha256', 'marker_sha256', 'structure_sha256',
                                  'parser_entered', 'semantic_outcome', 'post_pin_result',
                                  'post_pin_result_sha256'})
        pin_result = terminal['post_pin_result']
        _require(terminal['reservation_sha256'] == reserve_hash and terminal['marker_sha256'] == marker_hash
                 and terminal['structure_sha256'] == structure_hash
                 and terminal['post_pin_result_sha256'] == _canonical(pin_result)
                 and type(pin_result) is dict and set(pin_result) == {'matched', 'source_pins'}
                 and type(pin_result['matched']) is bool and _bytes(pin_result['source_pins']) == _bytes(owner['source_pins'])
                 and terminal['parser_entered'] is True
                 and terminal['semantic_outcome'] in SEMANTIC_OUTCOMES
                 and terminal['semantic_outcome'] == _expected_semantic(owner['fixtures'][case_id]))
        state.update(structural_receipt_persisted=True, parser_entered=True,
                     post_pin_check=pin_result['matched'])
    if 'commit' in artifacts:
        _require('terminal' in artifacts)
        commit, _ = artifacts['commit']
        _require(_bytes(commit) == _bytes({'terminal_sha256': terminal_hash, 'terminal_barrier_completed': True}))
        state.update(terminal_receipt_persisted=True, semantic_outcome=terminal['semantic_outcome'])
    if 'failure' in artifacts:
        failure, _ = artifacts['failure']
        _require(set(failure) == {'stage', 'code', 'observations'}
                 and failure['stage'] in (*WRITE_STAGES, *EVENT_STAGES, 'dispatch', 'entry', 'commit')
                 and failure['code'] in ('operation_failed', 'interrupted', 'pin_mismatch'))
        observations = failure['observations']
        _require(type(observations) is dict and set(observations) == {
            'parsed_object_observed', 'structural_receipt_persisted', 'parser_entered'}
            and all(v is True or v is None for v in observations.values()))
        for key, value in observations.items():
            if value is True:
                state[key] = True
        state['terminal_receipt_persisted'] = None
        state['semantic_outcome'] = None
        state['post_pin_check'] = None
    return state


def _recover_locked(scope):
    owner, owner_hash, run = _load(scope)
    try:
        cases = {case: _read_chain(scope, run, owner, owner_hash, case) for case in CASE_IDS}
        return {'status': 'blocked' if scope.exists('blocked.json') else 'observed', 'cases': cases}
    finally:
        run.close()


def recover(scope: Path):
    """Validate linked disk evidence; never invent execution facts or resend."""
    try:
        with _locked(scope) as directory:
            try:
                return _recover_locked(directory)
            except (Exception, KeyboardInterrupt):
                _block(directory, 'integrity_or_pin_failure')
                return _fail('incomplete_or_invalid_evidence', 'recovery')
    except (Exception, KeyboardInterrupt):
        return _fail('operation_failed', 'recovery')


def run_case(scope: Path, case_id: str, *, faults=()):
    """Invoke a registered fake once, always metadata-before-parser; no live API."""
    stage, observations = 'entry', {'parsed_object_observed': None,
                                  'structural_receipt_persisted': None, 'parser_entered': None}
    try:
        _require(type(case_id) is str and case_id in CASE_IDS, 'invalid_case')
        _require(type(faults) in (tuple, list) and all(type(f) is Fault for f in faults), 'invalid_faults')
        with _locked(scope) as directory:
            run = None
            try:
                _require(not directory.exists('blocked.json'), 'permanently_blocked')
                owner, owner_hash, run = _load(directory)
                prior = {case: _read_chain(directory, run, owner, owner_hash, case) for case in CASE_IDS}
                if any(p['post_pin_check'] is False for p in prior.values()):
                    raise _Rejected('previous_post_pin_failure')
                if any(p['consumed'] and p['terminal_receipt_persisted'] is not True for p in prior.values()):
                    raise _Rejected('unfinished_reservation')
                _require(not prior[case_id]['consumed'], 'already_consumed')
                stage = 'before_reservation'
                _event(faults, stage)
                stage = 'reservation'
                reservation_hash = directory.write(case_id + '-reservation.json',
                    _body(stage, case_id, owner_hash, {'request_sha256': owner['request_sha256'][case_id], 'retry': False}),
                    stage=stage, faults=faults)
                stage = 'after_reservation'
                _event(faults, stage)
                stage = 'marker'
                marker_hash = directory.write(case_id + '-marker.json', _body(stage, case_id, owner_hash,
                    {'reservation_sha256': reservation_hash, 'dispatch_possible': True}), stage=stage, faults=faults)
                stage = 'after_marker'
                _event(faults, stage)
                stage = 'dispatch'
                value = _fake_dispatch(owner['fixtures'][case_id])
                stage = 'after_fake'
                _event(faults, stage)
                observations['parsed_object_observed'] = True
                stage = 'after_return'
                _event(faults, stage)
                stage = 'helper'
                _event(faults, stage)
                _AUDIT['helper_calls'] += 1
                metadata = response_metadata(value)
                stage = 'structure'
                structure_hash = run.write(case_id + '-structure.json', _body(stage, case_id, owner_hash,
                    {'marker_sha256': marker_hash, 'parsed_object_observed': True, 'metadata': metadata}),
                    stage=stage, faults=faults)
                observations['structural_receipt_persisted'] = True
                stage = 'after_structure'
                _event(faults, stage)
                stage = 'parser'
                _event(faults, stage)
                observations['parser_entered'] = True
                try:
                    semantic = _parser_stub(value)
                except (Exception, KeyboardInterrupt):
                    semantic = 'parser_failed'
                _require(semantic in SEMANTIC_OUTCOMES)
                stage = 'after_parser'
                _event(faults, stage)
                stage = 'post_pins'
                matched = True
                try:
                    _event(faults, stage)
                    _require(_bytes(_source_pins()) == _bytes(owner['source_pins']), 'pin_mismatch')
                    directory.check()
                    run.check()
                    # Re-read the exact written links; changes stop all progression.
                    current = _read_chain(directory, run, owner, owner_hash, case_id)
                    _require(current['parsed_object_observed'] is True)
                    for ledger, name, digest in (
                        (directory, case_id + '-reservation.json', reservation_hash),
                        (directory, case_id + '-marker.json', marker_hash),
                        (run, case_id + '-structure.json', structure_hash)):
                        _require(ledger.read(name)[1] == digest)
                except (Exception, KeyboardInterrupt):
                    matched = False
                pin_result = {'matched': matched, 'source_pins': owner['source_pins']}
                stage = 'terminal'
                terminal_hash = run.write(case_id + '-terminal.json', _body(stage, case_id, owner_hash,
                    {'reservation_sha256': reservation_hash, 'marker_sha256': marker_hash,
                     'structure_sha256': structure_hash, 'parser_entered': True, 'semantic_outcome': semantic,
                     'post_pin_result': pin_result, 'post_pin_result_sha256': _canonical(pin_result)}), stage=stage, faults=faults)
                # The seal is created only after terminal file AND directory
                # barriers returned. Its valid bytes attest that earlier barrier,
                # never their own durability; no self-asserted fsync claim.
                stage = 'commit'
                run.write(case_id + '-commit.json', _body(stage, case_id, owner_hash,
                    {'terminal_sha256': terminal_hash, 'terminal_barrier_completed': True}),
                    stage=stage, faults=faults)
                if not matched:
                    _block(directory, 'post_pin_mismatch', faults)
                return {'status': 'recorded' if matched else 'blocked', 'semantic_outcome': semantic,
                        'post_pin_check': matched, 'new_model_calls': 0}
            except _Rejected as error:
                if error.code in ('already_consumed', 'permanently_blocked', 'unfinished_reservation'):
                    _AUDIT['blocked_reentries'] += 1
                _block(directory, 'integrity_or_pin_failure', faults)
                return _fail(error.code, stage)
            except (Exception, KeyboardInterrupt) as error:
                code = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'operation_failed'
                # This is a new, contemporaneous local failure receipt, never a
                # retrospective reconstruction of observations after restart.
                if run is not None and directory.exists(case_id + '-reservation.json'):
                    try:
                        run.write(case_id + '-failure.json', _body('failure', case_id, owner_hash,
                            {'stage': stage, 'code': code, 'observations': observations}),
                            stage='failure', faults=faults)
                    except (Exception, KeyboardInterrupt):
                        pass
                _block(directory, 'operation_failed', faults)
                return _fail(code, stage)
            finally:
                if run is not None:
                    run.close()
    except _Rejected as error:
        return _fail(error.code, stage)
    except (Exception, KeyboardInterrupt):
        return _fail('operation_failed', stage)


def synthetic_eligibility(scope: Path):
    """Only a synthetic all-four eligibility flag, never an export or score."""
    result = recover(scope)
    eligible = result.get('status') == 'observed' and all(
        row['terminal_receipt_persisted'] is True and row['semantic_outcome'] == 'complete'
        and row['post_pin_check'] is True and row['structural_receipt_persisted'] is True
        for row in result.get('cases', {}).values()) and len(result.get('cases', {})) == 4
    return {'synthetic_eligible': eligible, 'actual_export': False, 'new_model_calls': 0,
            'clinical_result': None, 'score': None}
