"""Disposable offline-only fixtures. No historical responses or live transport.

suite_summary() exposes observed counts for the most recent in-process suite;
run_history() retains separate development reruns. No fixture bodies persist
beyond TemporaryDirectory. The 14 named design groups are test methods, not
an assertion that exactly 14 fake invocations occurred.
"""
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from benchmarks import response_receipt_offline as adapter
from benchmarks.response_receipt import response_metadata

_RUN_HISTORY = []
_CURRENT = None


def suite_summary():
    return copy.deepcopy(_RUN_HISTORY[-1]) if _RUN_HISTORY else None


def run_history():
    return copy.deepcopy(_RUN_HISTORY)


class OfflineReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _CURRENT
        cls.start_counts = adapter.audit_counters()
        _CURRENT = {'run_in_process': len(_RUN_HISTORY) + 1, 'test_methods_executed': 0,
                    'test_names': [], 'fault_subcases': 0, 'network_attempts': 0}

    @classmethod
    def tearDownClass(cls):
        final = adapter.audit_counters()
        _CURRENT.update({key: final[key] - cls.start_counts[key] for key in final})
        _CURRENT['unique_test_methods'] = len(set(_CURRENT.pop('test_names')))
        _CURRENT['real_model_calls'] = 0
        _CURRENT['live_metadata_queries'] = 0
        _CURRENT['live_template_queries'] = 0
        _RUN_HISTORY.append(copy.deepcopy(_CURRENT))
        print('OFFLINE_SUITE_SUMMARY=' + json.dumps(suite_summary(), sort_keys=True))

    def setUp(self):
        _CURRENT['test_methods_executed'] += 1
        _CURRENT['test_names'].append(self._testMethodName)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.number = 0

        def denied(*args, **kwargs):
            _CURRENT['network_attempts'] += 1
            raise AssertionError('Network is forbidden in offline tests')

        for target in ('socket.socket', 'socket.create_connection', 'urllib.request.OpenerDirector.open'):
            guard = patch(target, side_effect=denied)
            guard.start()
            self.addCleanup(guard.stop)

    def fresh(self, first='normal', names=None):
        self.number += 1
        scope = self.base / ('scope' + str(self.number))
        actual = names or (first, 'normal', 'normal', 'normal')
        self.assertEqual(adapter.prepare(scope, actual)['status'], 'prepared')
        return scope

    def fault(self, scope, case, *faults):
        _CURRENT['fault_subcases'] += 1
        return adapter.run_case(scope, case, faults=faults)

    def count(self, name='fake_invocations'):
        return adapter.audit_counters()[name]

    def body(self, path):
        return json.loads(path.read_bytes())['body']

    def no_retry(self, scope, case='F01'):
        before = self.count()
        saved = {p.relative_to(scope): p.read_bytes() for p in scope.rglob('*.json') if not p.is_symlink()}
        self.assertEqual(adapter.run_case(scope, case)['status'], 'blocked')
        self.assertEqual(adapter.prepare(scope, out_name='other')['status'], 'blocked')
        self.assertEqual(adapter.run_case(scope, case)['status'], 'blocked')
        self.assertEqual(self.count(), before)
        for name, value in saved.items():
            self.assertEqual((scope / name).read_bytes(), value)
        self.assertFalse((scope / 'other').exists())
        self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])

    def test_G01_before_reservation_and_pin_block(self):
        scope = self.fresh()
        before = self.count()
        self.fault(scope, 'F01', adapter.Fault('before_reservation', 'interrupt'))
        self.assertEqual(self.count(), before)
        self.assertFalse((scope / 'F01-reservation.json').exists())
        self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])
        scope = self.fresh()
        pins = adapter._source_pins()
        with patch.object(adapter, '_source_pins', return_value=dict(pins, parser_stub_version=99)):
            self.assertEqual(adapter.run_case(scope, 'F01')['code'], 'pin_mismatch')
        self.no_retry(scope)
        self.assertEqual(self.count(), before)

    def test_G02_after_reservation_consumed_unknown(self):
        scope = self.fresh()
        before = self.count()
        self.fault(scope, 'F01', adapter.Fault('after_reservation', 'interrupt'))
        row = adapter.recover(scope)['cases']['F01']
        self.assertTrue(row['consumed'])
        self.assertIsNone(row['dispatch_possible'])
        self.assertIsNone(row['parsed_object_observed'])
        self.assertIsNone(row['terminal_receipt_persisted'])
        self.assertEqual(self.count(), before)
        self.no_retry(scope)

    def test_G03_marker_does_not_prove_dispatch(self):
        for boundary, added in (('after_marker', 0), ('after_fake', 1)):
            with self.subTest(boundary=boundary):
                scope = self.fresh()
                before = self.count()
                self.fault(scope, 'F01', adapter.Fault(boundary, 'interrupt'))
                row = adapter.recover(scope)['cases']['F01']
                self.assertTrue(row['dispatch_possible'])
                self.assertIsNone(row['parsed_object_observed'])
                self.assertIsNone(row['semantic_outcome'])
                self.assertEqual(self.count() - before, added)
                self.no_retry(scope)

    def test_G04_return_without_durable_observation(self):
        scope = self.fresh()
        before, parsers = self.count(), self.count('parser_calls')
        original = adapter._Directory.write

        def unavailable(directory, name, body, **kwargs):
            if kwargs.get('stage') in ('failure', 'blocked'):
                raise OSError('SYNTHETIC_UNSAVED_SECRET')
            return original(directory, name, body, **kwargs)

        with patch.object(adapter._Directory, 'write', unavailable):
            self.fault(scope, 'F01', adapter.Fault('after_return', 'interrupt'))
        row = adapter.recover(scope)['cases']['F01']
        self.assertIsNone(row['parsed_object_observed'])
        self.assertIsNone(row['structural_receipt_persisted'])
        self.assertEqual(self.count() - before, 1)
        self.assertEqual(self.count('parser_calls'), parsers)
        self.no_retry(scope)

    def test_G05_all_artifact_write_boundaries(self):
        for stage in adapter.WRITE_STAGES:
            for operation in adapter.WRITE_FAULTS:
                with self.subTest(stage=stage, operation=operation):
                    scope = self.fresh()
                    before, parsers = self.count(), self.count('parser_calls')
                    result = self.fault(scope, 'F01', adapter.Fault(stage, operation))
                    self.assertEqual(result['status'], 'blocked')
                    self.assertEqual(self.count() - before, int(stage in ('structure', 'terminal')))
                    self.assertEqual(self.count('parser_calls') - parsers, int(stage == 'terminal'))
                    directory = scope if stage in ('reservation', 'marker') else scope / 'run'
                    self.assertTrue((directory / ('F01-' + stage + '.json')).exists())
                    self.no_retry(scope)

    def test_G06_metadata_preserved_before_parser_or_terminal_failure(self):
        for stage in ('helper', 'after_structure', 'parser', 'after_parser'):
            with self.subTest(stage=stage):
                scope = self.fresh()
                before = self.count('parser_calls')
                self.fault(scope, 'F01', adapter.Fault(stage, 'interrupt'))
                self.assertEqual(self.count('parser_calls') - before, int(stage == 'after_parser'))
                if stage != 'helper':
                    saved = self.body(scope / 'run/F01-structure.json')['payload']['metadata']
                    self.assertEqual(saved, response_metadata(adapter._fixture('normal')))
                row = adapter.recover(scope)['cases']['F01']
                self.assertIsNone(row['terminal_receipt_persisted'])
                self.no_retry(scope)
        scope = self.fresh('parser_error')
        result = adapter.run_case(scope, 'F01')
        self.assertEqual(result['semantic_outcome'], 'parser_failed')
        row = adapter.recover(scope)['cases']['F01']
        self.assertTrue(row['structural_receipt_persisted'])
        self.assertTrue(row['parser_entered'])
        self.assertTrue(row['terminal_receipt_persisted'])
        self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])

    def test_G07_duplicate_and_new_output_scope(self):
        scope = self.fresh()
        before = self.count()
        self.assertEqual(adapter.run_case(scope, 'F01')['status'], 'recorded')
        self.no_retry(scope)
        self.assertEqual(self.count() - before, 1)
        self.assertEqual(set(inspect.signature(adapter.run_case).parameters), {'scope', 'case_id', 'faults'})
        self.assertEqual(set(inspect.signature(adapter.prepare).parameters), {'scope', 'fixture_names', 'out_name'})

    def test_G08_concurrent_and_stale_lock(self):
        scope = self.fresh()
        before = self.count()
        entered, release = threading.Event(), threading.Event()
        original = adapter._fake_dispatch

        def held(name):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(name)

        with patch.object(adapter, '_fake_dispatch', held), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(adapter.run_case, scope, 'F01')
            try:
                self.assertTrue(entered.wait(10))
                second = pool.submit(adapter.run_case, scope, 'F01').result(timeout=10)
                self.assertEqual(second['status'], 'blocked')
            finally:
                release.set()
            self.assertEqual(first.result(timeout=10)['status'], 'recorded')
        self.assertEqual(self.count() - before, 1)
        scope = self.fresh()
        (scope / '.lock').write_bytes(b'STALE_LOCK_SYNTHETIC')
        saved = (scope / '.lock').read_bytes()
        before = self.count()
        self.assertEqual(adapter.run_case(scope, 'F01')['status'], 'blocked')
        self.assertEqual((scope / '.lock').read_bytes(), saved)
        self.assertEqual(self.count(), before)

    def test_G09_integrity_partial_orphan_links_pins(self):
        for damage in ('partial', 'orphan', 'symlink', 'hardlink', 'checksum', 'owner'):
            with self.subTest(damage=damage):
                scope = self.fresh()
                before = self.count()
                path = scope / 'F01-reservation.json'
                if damage == 'partial':
                    path.write_bytes(b'{"body":')
                elif damage == 'orphan':
                    (scope / 'run/F01-terminal.json').write_bytes(b'{}')
                elif damage == 'symlink':
                    path.symlink_to(scope / 'owner.json')
                elif damage == 'hardlink':
                    os.link(scope / 'owner.json', path)
                else:
                    path = scope / 'owner.json'
                    original = path.read_bytes()
                    path.write_bytes(original + b'X' if damage == 'checksum' else b'{}')
                self.assertEqual(adapter.run_case(scope, 'F01')['status'], 'blocked')
                self.assertEqual(self.count(), before)
                if damage in ('checksum', 'owner'):
                    path.write_bytes(original)
                    self.assertEqual(adapter.run_case(scope, 'F01')['status'], 'blocked')
                    self.assertEqual(self.count(), before)
        scope = self.fresh()
        alias = self.base / 'alias'
        alias.symlink_to(scope, target_is_directory=True)
        self.assertEqual(adapter.run_case(alias, 'F01')['status'], 'blocked')
        self.assertEqual(adapter.prepare(self.base / 'bad', out_name='../escape')['status'], 'blocked')

    def test_G10_original_identity_unchanged_and_order(self):
        scope = self.fresh('bad_choice')
        value = adapter._fixture('bad_choice')
        original_value = copy.deepcopy(value)
        original_fake, original_helper, original_parser = adapter._fake_dispatch, adapter.response_metadata, adapter._parser_stub
        order, objects = [], []

        def same_fake(name):
            original_fake(name)  # actual registered invocation accounting
            return value

        def helper(obj):
            order.append('helper')
            objects.append(obj)
            self.assertFalse((scope / 'run/F01-structure.json').exists())
            return original_helper(obj)

        def parser(obj):
            order.append('parser')
            objects.append(obj)
            self.assertEqual(self.body(scope / 'run/F01-structure.json')['payload']['metadata'], original_helper(obj))
            return original_parser(obj)

        with patch.object(adapter, '_fake_dispatch', same_fake), patch.object(adapter, 'response_metadata', helper), patch.object(adapter, '_parser_stub', parser):
            result = adapter.run_case(scope, 'F01')
        self.assertEqual(order, ['helper', 'parser'])
        self.assertTrue(all(obj is value for obj in objects))
        self.assertEqual(value, original_value)
        self.assertEqual(result['semantic_outcome'], 'rejected')
        selected = self.body(scope / 'run/F01-structure.json')['payload']['metadata']['choices']
        self.assertEqual(selected['selected_position'], 0)

    def test_G11_structure_and_semantics_separate(self):
        expected = {'normal': 'complete', 'null': 'empty', 'empty': 'empty', 'whitespace': 'empty',
                    'length': 'truncated', 'malformed': 'rejected', 'bad_choice': 'rejected',
                    'invalid_unicode': 'rejected'}
        for name, outcome in expected.items():
            with self.subTest(fixture=name):
                scope = self.fresh(name)
                self.assertEqual(adapter.run_case(scope, 'F01')['semantic_outcome'], outcome)
                metadata = self.body(scope / 'run/F01-structure.json')['payload']['metadata']
                self.assertEqual(metadata, response_metadata(adapter._fixture(name)))
                row = adapter.recover(scope)['cases']['F01']
                self.assertTrue(row['terminal_receipt_persisted'])
                self.assertEqual(row['semantic_outcome'], outcome)

    def test_G12_usage_and_reasoning_no_normalization(self):
        for name in ('missing_usage', 'invalid_usage', 'overflow_usage', 'reasoning_only'):
            with self.subTest(fixture=name):
                scope = self.fresh(name)
                adapter.run_case(scope, 'F01')
                metadata = self.body(scope / 'run/F01-structure.json')['payload']['metadata']
                self.assertEqual(metadata, response_metadata(adapter._fixture(name)))
                encoded = json.dumps(metadata, allow_nan=False)
                self.assertNotIn('SYNTHETIC_REASONING_SECRET', encoded)
                self.assertNotIn(hashlib.sha256(b'SYNTHETIC_REASONING_SECRET').hexdigest(), encoded)
                self.assertNotIn('utf8_sha256', metadata['reasoning'])
                if name == 'reasoning_only':
                    self.assertEqual(adapter.recover(scope)['cases']['F01']['semantic_outcome'], 'empty')

    def test_G13_exception_text_and_fixture_privacy(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            for name, faults in (('dispatch_error', ()), ('parser_error', ()),
                                 ('normal', (adapter.Fault('helper', 'exception'),)),
                                 ('normal', (adapter.Fault('structure', 'short_write'),))):
                scope = self.fresh(name)
                result = self.fault(scope, 'F01', *faults) if faults else adapter.run_case(scope, 'F01')
                self.assertNotIn('SECRET', json.dumps(result))
            logs = b''.join(p.read_bytes() for p in self.base.rglob('*.json'))
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')
        self.assertNotIn(b'SECRET', logs)
        self.assertNotIn(b'traceback', logs.lower())
        self.assertNotIn(str(self.base).encode(), logs)
        self.assertNotIn(hashlib.sha256(b'SYNTHETIC_REASONING_SECRET').hexdigest().encode(), logs)

    def test_G14_four_complete_linked_postpins_gate(self):
        for last, fault in (('normal', None), ('empty', None), ('normal', adapter.Fault('post_pins', 'exception'))):
            with self.subTest(last=last, fault=fault):
                scope = self.fresh(names=('normal', 'normal', 'normal', last))
                for case in adapter.CASE_IDS[:3]:
                    self.assertEqual(adapter.run_case(scope, case)['status'], 'recorded')
                if fault:
                    self.fault(scope, 'F04', fault)
                else:
                    adapter.run_case(scope, 'F04')
                gate = adapter.synthetic_eligibility(scope)
                self.assertEqual(gate['synthetic_eligible'], last == 'normal' and fault is None)
                self.assertFalse(gate['actual_export'])
                self.assertIsNone(gate['clinical_result'])
                self.assertIsNone(gate['score'])
                self.assertEqual(gate['new_model_calls'], 0)
        scope = self.fresh()
        for case in adapter.CASE_IDS[:3]:
            adapter.run_case(scope, case)
        self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])

    def test_terminal_needs_post_barrier_seal_even_when_failure_saving_fails(self):
        for operation in ('flush', 'file_fsync', 'dir_fsync', 'interrupt'):
            with self.subTest(operation=operation):
                scope = self.fresh()
                for case in adapter.CASE_IDS[:3]:
                    adapter.run_case(scope, case)
                original = adapter._Directory.write

                def no_failure_saves(directory, name, body, **kwargs):
                    if kwargs.get('stage') in ('failure', 'blocked'):
                        raise OSError('SECRET_LOCAL_SAVE_ERROR')
                    return original(directory, name, body, **kwargs)

                with patch.object(adapter._Directory, 'write', no_failure_saves):
                    self.fault(scope, 'F04', adapter.Fault('terminal', operation))
                json.loads((scope / 'run/F04-terminal.json').read_bytes())  # readable full bytes
                self.assertFalse((scope / 'run/F04-commit.json').exists())
                row = adapter.recover(scope)['cases']['F04']
                self.assertIsNone(row['terminal_receipt_persisted'])
                self.assertIsNone(row['semantic_outcome'])
                self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])
                self.no_retry(scope, 'F04')

    def test_commit_faults_never_resend(self):
        for operation in adapter.WRITE_FAULTS:
            with self.subTest(operation=operation):
                scope = self.fresh()
                before = self.count()
                self.fault(scope, 'F01', adapter.Fault('commit', operation))
                self.assertEqual(self.count() - before, 1)
                self.no_retry(scope)

    def test_post_pin_failure_itself_blocks_next_slot_without_block_file(self):
        scope = self.fresh()
        original = adapter._Directory.write

        def no_block(directory, name, body, **kwargs):
            if kwargs.get('stage') == 'blocked':
                raise OSError('SECRET_BLOCK_FAILURE')
            return original(directory, name, body, **kwargs)

        with patch.object(adapter._Directory, 'write', no_block):
            self.fault(scope, 'F01', adapter.Fault('post_pins', 'exception'))
            self.assertFalse((scope / 'blocked.json').exists())
            before = self.count()
            self.assertEqual(adapter.run_case(scope, 'F02')['status'], 'blocked')
            self.assertEqual(self.count(), before)
        self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])

    def test_semantic_promotion_with_recomputed_checksums_is_rejected(self):
        for fixture in ('empty', 'whitespace', 'length', 'malformed', 'parser_error', 'reasoning_only'):
            with self.subTest(fixture=fixture):
                scope = self.fresh(names=('normal', 'normal', 'normal', fixture))
                for case in adapter.CASE_IDS:
                    adapter.run_case(scope, case)
                self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])
                terminal = scope / 'run/F04-terminal.json'
                body = self.body(terminal)
                body['payload']['semantic_outcome'] = 'complete'
                raw = adapter._bytes({'body': body, 'sha256': adapter._canonical(body)})
                terminal.write_bytes(raw)
                commit = scope / 'run/F04-commit.json'
                body = self.body(commit)
                body['payload']['terminal_sha256'] = hashlib.sha256(raw).hexdigest()
                commit.write_bytes(adapter._bytes({'body': body, 'sha256': adapter._canonical(body)}))
                before = self.count()
                self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])
                self.assertEqual(adapter.recover(scope)['status'], 'blocked')
                self.no_retry(scope, 'F04')
                self.assertEqual(self.count(), before)

    def test_fixed_boolean_payloads_reject_numeric_reencoding(self):
        for target, field, number in (('reservation', 'retry', 0), ('marker', 'dispatch_possible', 1),
                                      ('commit', 'terminal_barrier_completed', 1)):
            with self.subTest(target=target):
                scope = self.fresh()
                adapter.run_case(scope, 'F01')
                hashes = {}
                for stage in ('reservation', 'marker', 'structure', 'terminal', 'commit'):
                    parent = scope if stage in ('reservation', 'marker') else scope / 'run'
                    path = parent / ('F01-' + stage + '.json')
                    body = self.body(path)
                    payload = body['payload']
                    if stage == target:
                        payload[field] = number
                    for predecessor in ('reservation', 'marker', 'structure', 'terminal'):
                        key = predecessor + '_sha256'
                        if key in payload:
                            payload[key] = hashes[predecessor]
                    raw = adapter._bytes({'body': body, 'sha256': adapter._canonical(body)})
                    path.write_bytes(raw)
                    hashes[stage] = hashlib.sha256(raw).hexdigest()
                before = self.count()
                self.assertEqual(adapter.recover(scope)['status'], 'blocked')
                self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])
                self.no_retry(scope)
                self.assertEqual(self.count(), before)

    def test_forged_metadata_and_hash_changes_cannot_reach_gate(self):
        for mode in ('metadata', 'boolean_type', 'same_metadata_changed_bytes'):
            with self.subTest(mode=mode):
                scope = self.fresh()
                original = adapter._parser_stub

                def tamper(value):
                    semantic = original(value)
                    path = scope / 'run/F01-structure.json'
                    envelope = json.loads(path.read_bytes())
                    if mode == 'metadata':
                        envelope['body']['payload']['metadata']['content']['character_length'] = 999
                    elif mode == 'boolean_type':
                        envelope['body']['payload']['metadata']['choices']['count'] = True
                    envelope['sha256'] = adapter._canonical(envelope['body'])
                    path.write_text(json.dumps(envelope, indent=2), encoding='utf-8')
                    return semantic

                with patch.object(adapter, '_parser_stub', tamper):
                    result = adapter.run_case(scope, 'F01')
                self.assertEqual(result['status'], 'blocked')
                self.assertFalse(result['post_pin_check'])
                self.assertFalse(adapter.synthetic_eligibility(scope)['synthetic_eligible'])
                self.no_retry(scope)


if __name__ == '__main__':
    unittest.main()
