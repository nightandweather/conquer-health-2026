"""Bounded synthetic composition with the unchanged _SourceTransport.

Only unittest entry points run this harness. Six groups preregister three direct
and seven observed branches, each with a permanent reservation. No live client,
credentials, historical answers, generation runner or clinical gate is used.
The outer wrapper entry count is NOT a semantic-parser entry count. Recorded
legacy flags remain distinct from committed structural-journal evidence.
"""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import importlib
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PINNED_SOURCES = {
    'benchmarks/healthbench.py': '411a9cf555986970214ed4a1074b4218522665603aa0411c7ecc956e5d3b963a',
    'benchmarks/healthbench_budget_cycle.py': 'e9d6396469f26f5917d09900b7361ef53a77767d93bb2ade7f88ae2b316218d6',
    'benchmarks/healthbench_source_ledger_cycle.py': '95e03201429bc02d0a9c843e135fa628f2df15816d1112a2d3dd9134665808f5',
    'benchmarks/response_receipt.py': 'b4487c37f09a2e8c56ecc81676a72130f7fb5ee8a5ec46825778c92dfb622ceb',
    'benchmarks/response_receipt_offline.py': '40a757be23b386b92006536b7a8c80debcac03e537689e6742608cee48faf91d',
    'tests/test_response_receipt_offline.py': '6236dfda020711e0783bbebe86bc61eca9080affe18fb4eb4183ebdc27849d48',
}
BRANCHES = {
    'C01-direct': ('normal', 'direct'), 'C01-observed': ('normal', 'observed'),
    'C02-direct': ('empty', 'direct'), 'C02-observed': ('empty', 'observed'),
    'C03-direct': ('invalid_finish', 'direct'), 'C03-observed': ('invalid_finish', 'observed'),
    'C04-observed': ('fake_error', 'observed'),
    'C05-saved': ('normal', 'structure_fail'), 'C05-unsaved': ('normal', 'compound_fail'),
    'C06-observed': ('normal', 'interrupt'),
}
_RUN_HISTORY = []


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fingerprint(value):
    return _sha(_bytes(value))


def _payload():
    return {'model': 'synthetic-only', 'messages': [{'role': 'user', 'content': 'SYNTHETIC_REQUEST_SECRET'}],
            'max_tokens': 2048, 'reasoning_effort': 'medium', 'temperature': 0}


def _response(kind):
    return {'id': 'SYNTHETIC_RESPONSE_ID_SECRET', 'choices': [{'index': 41,
        'message': {'role': 'assistant', 'content': '' if kind == 'empty' else '가상 답변 SYNTHETIC_FINAL_SECRET',
                    'reasoning': 'SYNTHETIC_REASONING_SECRET'},
        'finish_reason': 'length' if kind == 'empty' else 'SYNTHETIC_INVALID_FINISH_SECRET' if kind == 'invalid_finish' else 'stop'}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4}}


def suite_summary():
    """Observed latest suite counts; each separate invocation remains separate."""
    return deepcopy(_RUN_HISTORY[-1]) if _RUN_HISTORY else None


def run_history():
    return deepcopy(_RUN_HISTORY)


class _Blocked(Exception):
    pass


class _NoResend(Exception):
    def __init__(self, code):
        self.code = code


def _check(value):
    if not value:
        raise _Blocked('fixed_synthetic_contract_block')


class _Fixture:
    def __init__(self, matrix, kind, payload):
        self.matrix, self.kind, self.payload = matrix, kind, payload
        self.value = _response(kind)
        self.before = _bytes(self.value)
        self.calls = 0

    def complete(self, payload):
        _check(self.calls == 0 and payload is self.payload and _bytes(payload) == _bytes(_payload()))
        self.calls += 1
        self.matrix.counts['fake_invocations'] += 1
        if self.kind == 'fake_error':
            raise OSError('SYNTHETIC_FAKE_EXCEPTION_SECRET')
        return self.value


class _Observer:
    def __init__(self, matrix, fixture, mode, write):
        self.matrix, self.fixture, self.mode, self.write = matrix, fixture, mode, write
        self.seen = self.helper_input = self.returned = None

    def complete(self, payload):
        value = self.fixture.complete(payload)
        self.seen = value
        self.matrix.counts['memory_only_objects_seen'] += 1
        self.helper_input = value
        self.matrix.counts['helper_entries'] += 1
        metadata = self.matrix.helper.response_metadata(value)
        try:
            self.write('structure', {'metadata': metadata}, fail=self.mode in ('structure_fail', 'compound_fail'))
        except (Exception, KeyboardInterrupt):
            try:
                self.write('failure', {'code': 'structure_write_failed', 'parsed_object_observed': True},
                           fail=self.mode == 'compound_fail')
            except (Exception, KeyboardInterrupt):
                pass
            raise OSError('SYNTHETIC_STORAGE_EXCEPTION_SECRET') from None
        if self.mode == 'interrupt':
            raise KeyboardInterrupt('SYNTHETIC_INTERRUPT_SECRET')
        self.returned = value
        self.matrix.counts['observer_normal_returns'] += 1
        return value


class _Matrix:
    """Test-only registry; persistence uses frozen primitives, not prior gates."""
    def __init__(self, root, hb, source, helper, storage, counts, dependency_paths):
        self.root, self.hb, self.source, self.helper, self.storage = root, hb, source, helper, storage
        self.counts, self.dependency_paths = counts, dependency_paths
        self.expected_pins = self.snapshot()
        self.scope = root / 'registry'
        self.expected_owner = {'kind': 'parser_contract_fixture_owner', 'out_name': 'run',
            'source_pins': self.expected_pins, 'request_sha256': _fingerprint(_payload()),
            'branches': {branch: {'fixture': kind, 'mode': mode, 'fixture_sha256': _fingerprint(_response(kind))}
                         for branch, (kind, mode) in BRANCHES.items()}}
        with storage._locked(self.scope, create=True) as directory:
            run = storage._mkdir(self.scope / 'run')
            run.close()
            directory.write('owner.json', self.expected_owner)
        self.initial_owner_raw = storage._read_file(self.scope / 'owner.json')

    def snapshot(self):
        actual = {name: _sha((ROOT / name).read_bytes()) for name in self.dependency_paths}
        _check(all(actual.get(name) == digest for name, digest in PINNED_SOURCES.items()))
        actual['fixture_registry_sha256'] = _fingerprint({key: {'mode': mode, 'response': _response(kind)}
                                                        for key, (kind, mode) in BRANCHES.items()})
        return actual

    def _load(self, directory):
        owner, owner_hash = directory.read('owner.json')
        _check(_bytes(owner) == _bytes(self.expected_owner))
        _check(self.storage._read_file(self.scope / 'owner.json') == self.initial_owner_raw)
        _check(_bytes(self.snapshot()) == _bytes(owner['source_pins']))
        return owner, owner_hash

    def _block(self, directory):
        if not directory.exists('blocked.json'):
            directory.write('blocked.json', {'code': 'fixed_pre_dispatch_block'})

    def execute(self, branch, *, out_name='run', request=None):
        """No retry API. A changed request/out/source produces no outer entry."""
        started = time.monotonic()
        try:
            _check(branch in BRANCHES)
            with self.storage._locked(self.scope) as directory:
                run = None
                try:
                    _check(not directory.exists('blocked.json'))
                    owner, owner_hash = self._load(directory)
                    payload = _payload() if request is None else request
                    _check(_fingerprint(payload) == owner['request_sha256'])
                    if out_name != owner['out_name']:
                        raise _NoResend('alternate_output_rejected')
                    if directory.exists(branch + '-reservation.json'):
                        raise _NoResend('already_consumed')
                    payload_before = _bytes(payload)
                    kind, mode = BRANCHES[branch]
                    run = self.storage._Directory(self.scope / out_name)
                    reservation = directory.write(branch + '-reservation.json',
                        {'branch': branch, 'owner_sha256': owner_hash, 'request_sha256': owner['request_sha256']})
                    marker = directory.write(branch + '-marker.json',
                        {'branch': branch, 'reservation_sha256': reservation, 'dispatch_possible': True})
                    committed = {}

                    def write(stage, data, fail=False):
                        body = {'branch': branch, 'kind': stage, 'owner_sha256': owner_hash,
                                'marker_sha256': marker, 'data': data}
                        faults = (self.storage.Fault('structure', 'file_fsync'),) if fail else ()
                        digest = run.write(branch + '-' + stage + '.json', body,
                                           stage='structure', faults=faults)
                        seal = run.write(branch + '-' + stage + '-commit.json',
                                         {'branch': branch, 'kind': stage + '_commit',
                                          'artifact_sha256': digest, 'prior_barrier_completed': True})
                        committed[stage] = seal
                        return seal

                    fixture = _Fixture(self, kind, payload)
                    client = fixture if mode == 'direct' else _Observer(self, fixture, mode, write)
                    outer = self.source._SourceTransport(client)
                    outer.expected_request_sha256 = owner['request_sha256']
                    self.counts['outer_complete_entries'] += 1
                    outer_started = time.monotonic()
                    returned, error = None, None
                    try:
                        returned = outer.complete(payload)
                    except self.hb.RequestFailure as caught:
                        _check(caught.code in ('invalid_provider_response', 'transport_failure', 'interrupted'))
                        error = caught.code
                    elapsed = time.monotonic() - outer_started
                    self.counts['outer_elapsed_seconds_sum'] += elapsed
                    _check(_bytes(payload) == payload_before and _bytes(fixture.value) == fixture.before)
                    _check(_bytes(self.snapshot()) == _bytes(owner['source_pins']))
                    _check(self.storage._read_file(self.scope / 'owner.json') == self.initial_owner_raw)
                    if mode != 'direct' and client.seen is not None:
                        _check(client.seen is fixture.value and client.helper_input is fixture.value)
                    if returned is not None:
                        _check(returned is fixture.value)
                        if mode != 'direct':
                            _check(client.returned is returned)
                    received = outer.received_answer_text
                    legacy = {'calls': outer.calls, 'provider_response_received': outer.provider_response_received,
                        'interrupted': outer.interrupted, 'error_code': error,
                        'normal_outer_return': returned is not None,
                        'received_answer_sha256': _sha(received.encode('utf-8')) if isinstance(received, str) else None,
                        'received_answer_characters': len(received) if isinstance(received, str) else None,
                        'provider_usage': deepcopy(outer.provider_usage), 'metadata_calls': outer.metadata_calls,
                        'template_queries': outer.template_queries}
                    # Only independently committed journal links, never client.seen,
                    # enter this operational result. Legacy False is not absence.
                    result = {'legacy': legacy, 'reservation_sha256': reservation, 'marker_sha256': marker,
                              'structure_commit_sha256': committed.get('structure'),
                              'failure_commit_sha256': committed.get('failure'),
                              'post_source_sha256': _fingerprint(owner['source_pins'])}
                    write('terminal', result)
                    self.counts['legacy_error_outcomes'] += int(error is not None)
                    self.counts['legacy_normal_returns'] += int(returned is not None)
                    return {'status': 'recorded', 'legacy': legacy, 'request_sha256': _fingerprint(payload),
                            'fixture_sha256': _fingerprint(fixture.value), 'outer_elapsed_seconds': elapsed,
                            'operation_elapsed_seconds': time.monotonic() - started,
                            'identity': {'request_is_original': True,
                                'helper_input_is_fake_object': None if mode == 'direct' or client.seen is None else client.helper_input is fixture.value,
                                'observer_return_is_fake_object': None if mode == 'direct' or client.returned is None else client.returned is fixture.value,
                                'outer_return_is_fake_object': None if returned is None else returned is fixture.value},
                            'payload_unchanged': True, 'fixture_unchanged': True}
                except _NoResend as blocked:
                    return {'status': 'blocked', 'code': blocked.code}
                except (Exception, KeyboardInterrupt):
                    self._block(directory)
                    return {'status': 'blocked', 'code': 'fixed_pre_dispatch_or_integrity_block'}
                finally:
                    if run is not None:
                        run.close()
        except (Exception, KeyboardInterrupt):
            return {'status': 'blocked', 'code': 'fixed_scope_block'}

    def recover(self, branch):
        """Read linked commit evidence only; uncommitted bytes imply unknown."""
        try:
            with self.storage._locked(self.scope) as directory:
                owner, owner_hash = self._load(directory)
                reservation, reserve_hash = directory.read(branch + '-reservation.json')
                marker, marker_hash = directory.read(branch + '-marker.json')
                _check(reservation == {'branch': branch, 'owner_sha256': owner_hash,
                                       'request_sha256': owner['request_sha256']})
                _check(_bytes(marker) == _bytes({'branch': branch, 'reservation_sha256': reserve_hash,
                                               'dispatch_possible': True}))
                run = self.storage._Directory(self.scope / 'run')
                try:
                    seals = {}

                    def read_committed(stage):
                        seal_name = branch + '-' + stage + '-commit.json'
                        if not run.exists(seal_name):
                            return None
                        seal, seal_hash = run.read(seal_name)
                        body, digest = run.read(branch + '-' + stage + '.json')
                        _check(_bytes(seal) == _bytes({'branch': branch, 'kind': stage + '_commit',
                            'artifact_sha256': digest, 'prior_barrier_completed': True}))
                        _check(set(body) == {'branch', 'kind', 'owner_sha256', 'marker_sha256', 'data'}
                            and body['branch'] == branch and body['kind'] == stage
                            and body['owner_sha256'] == owner_hash and body['marker_sha256'] == marker_hash)
                        seals[stage] = seal_hash
                        return body['data']

                    structure, failure, terminal = (read_committed(stage) for stage in ('structure', 'failure', 'terminal'))
                    observed, persisted = None, None
                    if structure is not None:
                        kind = BRANCHES[branch][0]
                        _check(_bytes(structure) == _bytes({'metadata': self.helper.response_metadata(_response(kind))}))
                        observed = persisted = True
                    if failure is not None:
                        _check(_bytes(failure) == _bytes({'code': 'structure_write_failed', 'parsed_object_observed': True}))
                        observed = True
                    if terminal is not None:
                        _check(terminal['reservation_sha256'] == reserve_hash and terminal['marker_sha256'] == marker_hash
                            and terminal['structure_commit_sha256'] == seals.get('structure')
                            and terminal['failure_commit_sha256'] == seals.get('failure')
                            and terminal['post_source_sha256'] == _fingerprint(owner['source_pins']))
                    return {'status': 'observed', 'consumed': True, 'dispatch_possible': True,
                        'parsed_object_observed': observed, 'structural_receipt_persisted': persisted,
                        'operational_result_persisted': True if terminal is not None else None,
                        'legacy': terminal['legacy'] if terminal is not None else None,
                        'metadata': structure['metadata'] if structure is not None else None}
                finally:
                    run.close()
        except (Exception, KeyboardInterrupt):
            return {'status': 'blocked', 'parsed_object_observed': None, 'structural_receipt_persisted': None}


class ParserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()
        cls.counts = {key: 0 for key in ('fake_invocations', 'helper_entries', 'outer_complete_entries',
            'observer_normal_returns', 'memory_only_objects_seen', 'network_attempts',
            'production_transport_constructor_attempts', 'production_transport_method_attempts',
            'legacy_error_outcomes', 'legacy_normal_returns', 'test_methods_executed')}
        cls.counts['outer_elapsed_seconds_sum'] = 0.0
        cls.stack = ExitStack()
        cls.order = []
        cls.test_result = None
        try:
            def network_denied(*args, **kwargs):
                cls.counts['network_attempts'] += 1
                raise AssertionError('Fixed offline network denial')
            for obj, name in ((socket, 'socket'), (socket, 'create_connection'),
                              (urllib.request, 'urlopen'), (urllib.request.OpenerDirector, 'open')):
                cls.stack.enter_context(patch.object(obj, name, side_effect=network_denied))
            cls.order.append('stdlib_guards_installed')
            _check(all(_sha((ROOT / path).read_bytes()) == digest for path, digest in PINNED_SOURCES.items()))
            # healthbench top-level code was statically inspected; all modules
            # still import under counted stdlib guards, including cached aliases.
            cls.hb = importlib.import_module('benchmarks.healthbench')
            cls.order.append('healthbench_imported')
            def constructor_denied(*args, **kwargs):
                cls.counts['production_transport_constructor_attempts'] += 1
                raise AssertionError('Fixed offline construction denial')
            def transport_denied(*args, **kwargs):
                cls.counts['production_transport_method_attempts'] += 1
                raise AssertionError('Fixed offline transport denial')
            cls.stack.enter_context(patch.object(cls.hb.Transport, '__init__', side_effect=constructor_denied))
            for name in ('request', 'complete', 'model_metadata'):
                cls.stack.enter_context(patch.object(cls.hb.Transport, name, side_effect=transport_denied))
            cls.order.append('actual_transport_class_blocked')
            cls.source = importlib.import_module('benchmarks.healthbench_source_ledger_cycle')
            cls.order.append('source_wrapper_imported')
            _check(cls.source.Transport is cls.hb.Transport)
            cls.helper = importlib.import_module('benchmarks.response_receipt')
            cls.storage = importlib.import_module('benchmarks.response_receipt_offline')
            dependencies = set(PINNED_SOURCES) | {'tests/test_response_receipt_parser_contract.py'}
            for module in tuple(sys.modules.values()):
                name = getattr(module, '__name__', '')
                filename = getattr(module, '__file__', None)
                if name.startswith('benchmarks.') and filename:
                    path = Path(filename).resolve()
                    if path.is_relative_to(ROOT) and path.suffix == '.py':
                        dependencies.add(path.relative_to(ROOT).as_posix())
            cls.dependencies = tuple(sorted(dependencies))
            cls.temp = tempfile.TemporaryDirectory()
            cls.stack.callback(cls.temp.cleanup)  # cleanup precedes guard removal
            cls.base = Path(cls.temp.name).resolve()
            cls.matrix = _Matrix(cls.base, cls.hb, cls.source, cls.helper, cls.storage, cls.counts, cls.dependencies)
            cls.rows = {}
        except BaseException:
            cls.stack.close()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            final_pins = cls.matrix.snapshot()
            _check(final_pins == cls.matrix.expected_pins)
            summary = dict(cls.counts, run_in_process=len(_RUN_HISTORY) + 1,
                test_failures=len(cls.test_result.failures) if cls.test_result else None,
                test_errors=len(cls.test_result.errors) if cls.test_result else None,
                guard_installation_order=list(cls.order), imported_source_sha256=final_pins,
                matrix_branch_count=len(cls.rows), branches=deepcopy(cls.rows),
                new_actual_model_calls=0, new_actual_metadata_queries=0, new_actual_template_queries=0,
                new_NOVA_submissions=0, clinical_comparison=None, runtime_integration=False)
        finally:
            cls.stack.close()
        summary['elapsed_seconds_including_cleanup'] = time.monotonic() - cls.started
        summary['network_attempts'] = cls.counts['network_attempts']
        _RUN_HISTORY.append(summary)
        print('PARSER_CONTRACT_SUITE_SUMMARY=' + json.dumps(summary, sort_keys=True, allow_nan=False))

    def setUp(self):
        self.counts['test_methods_executed'] += 1
        type(self).test_result = self._outcome.result

    def branch(self, branch):
        before = deepcopy(self.counts)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = self.matrix.execute(branch)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')
        self.assertEqual(result['status'], 'recorded')
        delta = {key: self.counts[key] - before[key] for key in
                 ('fake_invocations', 'helper_entries', 'outer_complete_entries', 'observer_normal_returns')}
        self.assertEqual(delta['fake_invocations'], 1)
        self.assertEqual(delta['outer_complete_entries'], 1)
        recovered = self.matrix.recover(branch)
        self.assertEqual(recovered['status'], 'observed')
        self.assertEqual(recovered['legacy'], result['legacy'])
        self.rows[branch] = {'observed_counts': delta, 'legacy': result['legacy'],
            'parsed_object_observed': recovered['parsed_object_observed'],
            'structural_receipt_persisted': recovered['structural_receipt_persisted'],
            'operational_result_persisted': recovered['operational_result_persisted'],
            'outer_elapsed_seconds': result['outer_elapsed_seconds'],
            'operation_elapsed_seconds': result['operation_elapsed_seconds'],
            'identity': result['identity'],
            'request_sha256': result['request_sha256'], 'fixture_sha256': result['fixture_sha256']}
        return result, recovered

    def no_resend(self, branch):
        counts = deepcopy(self.counts)
        saved = {p.relative_to(self.matrix.scope): p.read_bytes() for p in self.matrix.scope.rglob('*.json')}
        self.assertFalse((self.matrix.scope / 'blocked.json').exists())
        self.assertEqual(self.matrix.execute(branch), {'status': 'blocked', 'code': 'already_consumed'})
        self.assertEqual(self.matrix.execute(branch, out_name='other'),
                         {'status': 'blocked', 'code': 'alternate_output_rejected'})
        self.assertFalse((self.matrix.scope / 'blocked.json').exists())
        self.assertEqual(self.counts['fake_invocations'], counts['fake_invocations'])
        self.assertEqual(self.counts['outer_complete_entries'], counts['outer_complete_entries'])
        self.assertFalse((self.matrix.scope / 'other').exists())
        for name, raw in saved.items():
            self.assertEqual((self.matrix.scope / name).read_bytes(), raw)

    def test_C01_utf8_missing_total_and_zero_call_preguards(self):
        direct, _ = self.branch('C01-direct')
        observed, receipt = self.branch('C01-observed')
        self.assertEqual(direct['legacy'], observed['legacy'])
        self.assertEqual(direct['fixture_sha256'], observed['fixture_sha256'])
        self.assertEqual(direct['request_sha256'], observed['request_sha256'])
        self.assertTrue(observed['legacy']['normal_outer_return'])
        self.assertEqual(observed['legacy']['provider_usage']['total_tokens'], 7)
        self.assertEqual(receipt['metadata']['usage']['total_tokens']['state'], 'missing')
        self.assertEqual(receipt['metadata']['usage']['effective_total_tokens'], {'value': 7, 'provenance': 'derived_prompt_plus_completion'})
        for mode in ('source', 'request'):
            guard_root = self.base / ('guard-' + mode)
            guard_root.mkdir(mode=0o700)
            matrix = _Matrix(guard_root, self.hb, self.source, self.helper, self.storage, self.counts, self.dependencies)
            counts = deepcopy(self.counts)
            if mode == 'source':
                with patch.object(matrix, 'snapshot', return_value={'altered': True}):
                    self.assertEqual(matrix.execute('C01-direct')['status'], 'blocked')
            else:
                request = _payload()
                request['max_tokens'] = 1
                self.assertEqual(matrix.execute('C01-direct', request=request)['status'], 'blocked')
            self.assertEqual(matrix.execute('C01-direct')['status'], 'blocked')
            self.assertEqual(self.counts['fake_invocations'], counts['fake_invocations'])
            self.assertEqual(self.counts['outer_complete_entries'], counts['outer_complete_entries'])

    def test_C02_blank_length_is_only_envelope_return(self):
        direct, _ = self.branch('C02-direct')
        observed, receipt = self.branch('C02-observed')
        self.assertEqual(direct['legacy'], observed['legacy'])
        self.assertEqual(direct['fixture_sha256'], observed['fixture_sha256'])
        self.assertTrue(observed['legacy']['normal_outer_return'])
        self.assertEqual(observed['legacy']['received_answer_characters'], 0)
        self.assertEqual(receipt['metadata']['content']['character_length'], 0)
        self.assertEqual(receipt['metadata']['finish_reason']['value'], 'length')
        self.assertNotIn('utf8_sha256', receipt['metadata']['reasoning'])
        self.assertNotIn('completed', observed)

    def test_C03_invalid_finish_preserves_literal_fingerprint(self):
        direct, _ = self.branch('C03-direct')
        observed, receipt = self.branch('C03-observed')
        self.assertEqual(direct['legacy'], observed['legacy'])
        self.assertEqual(direct['fixture_sha256'], observed['fixture_sha256'])
        self.assertEqual(observed['legacy']['error_code'], 'invalid_provider_response')
        self.assertTrue(observed['legacy']['provider_response_received'])
        self.assertFalse(observed['legacy']['normal_outer_return'])
        self.assertEqual(observed['legacy']['received_answer_sha256'], receipt['metadata']['content']['utf8_sha256'])
        self.assertIsNone(receipt['metadata']['finish_reason']['value'])

    def test_C04_exception_before_fake_return(self):
        result, receipt = self.branch('C04-observed')
        self.assertEqual(result['legacy']['error_code'], 'transport_failure')
        self.assertFalse(result['legacy']['provider_response_received'])
        self.assertIsNone(receipt['parsed_object_observed'])
        self.assertIsNone(receipt['structural_receipt_persisted'])
        self.assertEqual(self.rows['C04-observed']['observed_counts']['helper_entries'], 0)

    def test_C05_storage_failure_independent_journal(self):
        saved, yes = self.branch('C05-saved')
        unsaved, unknown = self.branch('C05-unsaved')
        self.assertEqual(saved['legacy'], unsaved['legacy'])
        self.assertEqual(saved['legacy']['error_code'], 'transport_failure')
        self.assertFalse(saved['legacy']['provider_response_received'])
        self.assertTrue(yes['parsed_object_observed'])
        self.assertIsNone(unknown['parsed_object_observed'])
        for branch, receipt in (('C05-saved', yes), ('C05-unsaved', unknown)):
            self.assertIsNone(receipt['structural_receipt_persisted'])
            self.assertEqual(self.rows[branch]['observed_counts']['observer_normal_returns'], 0)
            path = self.matrix.scope / ('run/' + branch + '-structure.json')
            json.loads(path.read_bytes())  # readable full bytes, without a commit
            self.assertFalse(path.with_name(branch + '-structure-commit.json').exists())
        # C06 repeats each consumed branch without a global marker masking the
        # reservation and alternate-output checks. No guard authorizes retry.

    def test_C06_committed_structure_before_interruption(self):
        result, receipt = self.branch('C06-observed')
        self.assertEqual(result['legacy']['error_code'], 'interrupted')
        self.assertFalse(result['legacy']['provider_response_received'])
        self.assertTrue(result['legacy']['interrupted'])
        self.assertTrue(receipt['parsed_object_observed'])
        self.assertTrue(receipt['structural_receipt_persisted'])
        self.assertEqual(self.rows['C06-observed']['observed_counts']['observer_normal_returns'], 0)
        for branch in BRANCHES:
            self.no_resend(branch)
        persisted = b''.join(p.read_bytes() for p in self.base.rglob('*.json'))
        self.assertNotIn(b'SECRET', persisted)
        self.assertNotIn(str(self.base).encode(), persisted)
        self.assertNotIn(_sha(b'SYNTHETIC_REASONING_SECRET').encode(), persisted)
        self.assertEqual(self.counts['fake_invocations'], 10)
        self.assertEqual(self.counts['helper_entries'], 6)
        self.assertEqual(self.counts['outer_complete_entries'], 10)
        self.assertEqual(self.counts['observer_normal_returns'], 3)
        self.assertEqual(self.counts['network_attempts'], 0)
        self.assertEqual(self.counts['production_transport_constructor_attempts'], 0)
        self.assertEqual(self.counts['production_transport_method_attempts'], 0)
        self.assertEqual(self.order, ['stdlib_guards_installed', 'healthbench_imported',
                                      'actual_transport_class_blocked', 'source_wrapper_imported'])


if __name__ == '__main__':
    unittest.main()
