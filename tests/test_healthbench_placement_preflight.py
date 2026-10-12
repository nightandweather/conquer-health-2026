"""Synthetic instruction-placement proofs and tamper tests; no real source reads."""

from copy import deepcopy
import json
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from benchmarks import healthbench_budget_preflight as gate
from benchmarks import healthbench_codex_cycle as cycle
from benchmarks import healthbench_placement_preflight as placement
from benchmarks.healthbench import (DEFAULT_MODEL, GROUNDED_SUFFIX, Transport,
                                    arm_config, build_payload, canonical_hash)
from tests import test_healthbench_budget_preflight as source_fixture


class InstructionPlacementPreflightTests(source_fixture.BudgetPreflightTests):
    """Run the existing gate's tamper suite through the placement public API."""

    def setUp(self):
        super().setUp()
        for index, item in enumerate(self.items):
            item['prompt'] = [
                {'role': 'user', 'content': f'SYNTHETIC_HISTORY_QUESTION_SENTINEL_{index}'},
                {'role': 'assistant', 'content': f'  SYNTHETIC_HISTORY_ANSWER_SENTINEL_{index}\n원문 [history]\t'},
                {'role': 'user', 'content': f'\nSYNTHETIC_QUESTION_SENTINEL_{index}\n\n[original quoted block]  '},
            ]
        self._rebuild_prompt_provenance()

    def _rebuild_prompt_provenance(self):
        """Adapt the existing 17+3 fixture without duplicating its row generator."""
        self.write(self.data, self.items)
        dataset_sha = source_fixture.sha(self.data.read_bytes())
        prompt_hashes = {item['prompt_id']: canonical_hash(item['prompt']) for item in self.items}
        self.manifest['dataset']['sha256'] = dataset_sha
        self.manifest['dataset']['selected_prompt_sha256'] = prompt_hashes
        self.manifest['baseline']['dataset_sha256'] = dataset_sha
        self.payloads = [cycle.make_payload(item, GROUNDED_SUFFIX, reasoning_effort='medium', max_tokens=2048)
                         for item in self.items]
        self.manifest['prepared_requests'] = []
        low_entries = self.manifest['baseline']['development_control']['row_provenance']
        for item, payload, row, low in zip(self.items, self.payloads, self.rows, low_entries):
            prompt_hash = prompt_hashes[item['prompt_id']]
            input_hash, request_hash = canonical_hash(payload['messages']), canonical_hash(payload)
            self.manifest['prepared_requests'].append({'prompt_id': item['prompt_id'],
                                                       'prompt_sha256': prompt_hash,
                                                       'input_sha256': input_hash,
                                                       'request_sha256': request_hash})
            row.update(dataset_sha256=dataset_sha, prompt_sha256=prompt_hash,
                       input_sha256=input_hash, request_sha256=request_hash)
            row['attempts'][0].update(input_sha256=input_hash, request_sha256=request_hash)
            low_payload = build_payload(item, 'grounded_call')
            low.update(input_sha256=canonical_hash(low_payload['messages']),
                       request_sha256=canonical_hash(low_payload))
        self.comparison_value['dataset'] = deepcopy(self.manifest['dataset'])
        self.comparison_value['sources']['baseline']['validated_development_control'] = deepcopy(
            self.manifest['baseline']['development_control'])
        self.repin()
        self.summary_pin = source_fixture.sha(self.summary.read_bytes())

    def run_preflight(self, **overrides):
        args = {'data': self.data, 'source_manifest': self.source,
                'source_comparison_manifest': self.comparison, 'instructions_file': self.instructions,
                'out': self.out, 'expected_comparison_sha256': self.pin,
                'expected_source_summary_sha256': self.summary_pin}
        args.update(overrides)
        with patch.object(Transport, '__init__', side_effect=AssertionError('no transport construction')) as ctor, \
                patch.object(Transport, 'request', side_effect=AssertionError('no HTTP')) as request, \
                patch.object(Transport, 'complete', side_effect=AssertionError('no generation')) as complete, \
                patch.object(Transport, 'model_metadata', side_effect=AssertionError('no metadata')) as metadata, \
                patch('urllib.request.build_opener', side_effect=AssertionError('no HTTP opener')) as opener, \
                patch('urllib.request.urlopen', side_effect=AssertionError('no HTTP URL')) as urlopen:
            result = placement.prepare_instruction_placement(**args)
            for mocked in (ctor, request, complete, metadata, opener, urlopen):
                mocked.assert_not_called()
        return result

    def test_valid_partial_control_prepares_twenty_hashes_and_zero_calls(self):
        paths = (self.data, self.source, self.answers, self.summary, self.comparison, self.instructions)
        before = {path: path.read_bytes() for path in paths}
        result = self.run_preflight()
        self.assertEqual(result['source_status_counts'], {'completed': 17, 'failed': 3})
        self.assertEqual(result['original_manifest_status'], 'partial')
        self.assertEqual(result['source_arm'], gate.ARM)
        self.assertEqual(result['known_failure_count'], 3)
        self.assertEqual(len(result['row_provenance']), 20)
        self.assertEqual(result['selected_prompt_ids'], self.ids)
        self.assertEqual([row['prompt_id'] for row in result['known_inference_failures']],
                         [self.ids[index] for index in (3, 5, 13)])
        for key in ('calls', 'generation_calls', 'model_metadata_calls', 'network_calls', 'transports_created'):
            self.assertEqual(result[key], 0)
        self.assertEqual(result['execution_support']['status'], 'NOT_READY')
        self.assertFalse(result['execution_support']['ready'])
        self.assertFalse(result['experiment']['mechanism_proven'])
        self.assertFalse(result['experiment']['quality_improvement_proven'])
        self.assertFalse(result['experiment']['isolates_priority_only'])
        self.assertEqual(result['experiment']['factor'], 'instruction_placement_package')
        self.assertEqual(result['experiment']['components_changed_together'],
                         ['role', 'message_boundary', 'position'])
        self.assertTrue(result['experiment']['instruction_wording_and_block_bytes_identical'])
        self.assertEqual(result['control_max_tokens'], 2048)
        self.assertEqual(result['treatment_max_tokens'], 2048)
        self.assertEqual(result['request_settings']['max_tokens'], 2048)
        self.assertNotIn('candidate_max_tokens', result)
        self.assertEqual(result['model']['digest'], self.digest)
        self.assertEqual(result['request_settings'],
                         {key: value for key, value in self.payloads[0].items() if key != 'messages'})
        block = '\n\n[' + GROUNDED_SUFFIX + ']'
        block_sha = source_fixture.sha(block.encode('utf-8'))
        self.assertEqual(result['frozen_block_byte_sha256'], block_sha)
        for item, payload, row, proof in zip(self.items, self.payloads, self.rows, result['row_provenance']):
            treatment = deepcopy(payload)
            treatment['messages'] = [{'role': 'system', 'content': block}, *deepcopy(item['prompt'])]
            self.assertEqual(proof['control_request_sha256'], row['request_sha256'])
            self.assertEqual(proof['control_request_sha256'], canonical_hash(payload))
            self.assertEqual(proof['control_input_sha256'], row['input_sha256'])
            self.assertEqual(proof['treatment_request_sha256'], canonical_hash(treatment))
            self.assertEqual(proof['treatment_input_sha256'], canonical_hash(treatment['messages']))
            self.assertEqual(proof['inverse_control_request_sha256'], row['request_sha256'])
            self.assertEqual(proof['original_messages_sha256'], canonical_hash(item['prompt']))
            self.assertEqual(proof['prompt_sha256'], row['prompt_sha256'])
            self.assertEqual(proof['frozen_block_byte_sha256'], block_sha)
            self.assertEqual(proof['full_row_sha256'], canonical_hash(row))
            self.assertEqual(proof['original_status'], row['status'])
            self.assertEqual(proof['changed_fields'], ['messages'])
            self.assertNotIn('candidate_request_sha256', proof)
        self.assertEqual([row['inference_failure_utility_score'] for row in result['known_inference_failures']], [0, 0, 0])
        self.assertTrue(all(row['rubric_labels_not_predicted'] for row in result['known_inference_failures']))
        self.assertEqual(before, {path: path.read_bytes() for path in paths})
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        manifest = self.out / 'manifest.json'
        self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o600)
        serialized = manifest.read_text()
        self.assertEqual(json.loads(serialized), result)
        for sentinel in ('SYNTHETIC_QUESTION_SENTINEL', 'SYNTHETIC_HISTORY_ANSWER_SENTINEL',
                         'SYNTHETIC_ANSWER_SENTINEL', 'SYNTHETIC_CRITERION_SENTINEL',
                         'SYNTHETIC_REFERENCE_SENTINEL', GROUNDED_SUFFIX):
            self.assertNotIn(sentinel, serialized)
        self.assertEqual(set(path.name for path in self.out.iterdir()), {'manifest.json'})

    def test_exact_block_relocation_preserves_raw_original_messages_and_inverse(self):
        originals = deepcopy(self.items)
        frozen = self.instructions.read_bytes().decode('utf-8')
        block = '\n\n[' + frozen + ']'
        for item, cached in zip(self.items, self.payloads):
            control, treatment = placement._placement_requests(item, frozen)
            self.assertEqual(control, cached)
            self.assertEqual(treatment['messages'][0], {'role': 'system', 'content': block})
            self.assertEqual(treatment['messages'][0]['content'].encode(), block.encode())
            self.assertEqual(treatment['messages'][1:], item['prompt'])
            self.assertIsNot(treatment['messages'][1], item['prompt'][0])
            self.assertEqual(control['messages'][-1]['content'], item['prompt'][-1]['content'] + block)
            self.assertEqual(control['messages'][:-1], item['prompt'][:-1])
            inverse = deepcopy(treatment)
            inverse['messages'] = inverse['messages'][1:]
            inverse['messages'][-1]['content'] += block
            self.assertEqual(canonical_hash(inverse), canonical_hash(cached))
            self.assertEqual({key: value for key, value in control.items() if key != 'messages'},
                             {key: value for key, value in treatment.items() if key != 'messages'})
        self.assertEqual(self.items, originals)

    def test_inverse_preserves_identical_blocks_already_present_in_original_text(self):
        block = '\n\n[' + GROUNDED_SUFFIX + ']'
        self.items[0]['prompt'][-1]['content'] += block + block
        self._rebuild_prompt_provenance()
        original = deepcopy(self.items[0])
        control, treatment = placement._placement_requests(self.items[0], GROUNDED_SUFFIX)
        self.assertEqual(control['messages'][-1]['content'], original['prompt'][-1]['content'] + block)
        self.assertEqual(treatment['messages'][1:], original['prompt'])
        self.assertEqual(self.items[0], original)
        result = self.run_preflight()
        self.assertEqual(result['row_provenance'][0]['inverse_control_request_sha256'],
                         self.rows[0]['request_sha256'])
        self.assertEqual(result['row_provenance'][0]['original_messages_sha256'],
                         canonical_hash(original['prompt']))

    def test_default_builder_and_role_configurations_remain_unchanged(self):
        original_builder, original_generator = cycle.make_payload, cycle.generate_candidate
        configs_before = deepcopy(arm_config())
        default_before = cycle.make_payload(self.items[0], GROUNDED_SUFFIX)
        low_before = build_payload(self.items[0], 'grounded_call')
        self.assertEqual(default_before['reasoning_effort'], 'low')
        self.assertEqual(default_before['max_tokens'], 2048)
        self.run_preflight()
        self.assertIs(cycle.make_payload, original_builder)
        self.assertIs(cycle.generate_candidate, original_generator)
        self.assertEqual(arm_config(), configs_before)
        self.assertEqual(cycle.make_payload(self.items[0], GROUNDED_SUFFIX), default_before)
        self.assertEqual(build_payload(self.items[0], 'grounded_call'), low_before)
        self.assertEqual(self.manifest['arms'][gate.ARM]['max_tokens'], 2048)

    def test_existing_system_message_rejected_across_all_twenty_including_failed_case(self):
        original = deepcopy(self.items)
        for index in (0, 3, 19):
            with self.subTest(index=index):
                self.items = deepcopy(original)
                self.items[index]['prompt'].insert(1, {'role': 'system', 'content': 'SYNTHETIC_SYSTEM_SENTINEL'})
                self._rebuild_prompt_provenance()
                with patch.object(gate, '_publish_private', side_effect=AssertionError('do not publish')) as publish:
                    with self.assertRaisesRegex(ValueError, 'no preexisting system'):
                        self.run_preflight()
                    publish.assert_not_called()
                self.assertFalse(self.out.exists())

    def test_source_validation_is_only_the_existing_budget_gate_and_proposal_is_discarded(self):
        validate = gate._validate_budget_source
        def poisoned_proposal(*args, **kwargs):
            proof, *rest = validate(*args, **kwargs)
            proof['candidate_max_tokens'] = 'BUDGET_PROPOSAL_MUST_BE_DISCARDED'
            proof['changed_fields'] = ['BUDGET_PROPOSAL_MUST_BE_DISCARDED']
            for row in proof['row_provenance']:
                row['candidate_request_sha256'] = 'BUDGET_PROPOSAL_MUST_BE_DISCARDED'
            return (proof, *rest)
        with patch.object(gate, '_validate_budget_source', side_effect=poisoned_proposal) as validated, \
                patch.object(gate, 'prepare_budget_preflight', side_effect=AssertionError('do not publish budget')):
            result = self.run_preflight()
        validated.assert_called_once()
        self.assertNotIn('BUDGET_PROPOSAL_MUST_BE_DISCARDED', json.dumps(result))
        self.assertEqual(result['changed_fields'], ['messages'])

    def test_derived_control_cannot_drift_from_cached_requests(self):
        make_payload = placement.make_payload
        def changed(*args, **kwargs):
            payload = make_payload(*args, **kwargs)
            payload['temperature'] = 1
            return payload
        with patch.object(placement, 'make_payload', side_effect=changed):
            self.assert_rejected()

    def test_source_model_name_or_metadata_state_changes_rejected_even_with_repin(self):
        original = deepcopy(self.manifest['model'])
        for key, value in (('name', 'another-model'), ('metadata_status', 'unavailable')):
            with self.subTest(key=key):
                self.manifest['model'] = deepcopy(original)
                self.manifest['model'][key] = value
                self.repin()
                self.assert_rejected()

    def test_source_change_during_placement_derivation_rejected_before_publication(self):
        make_payload = placement.make_payload
        def changed(*args, **kwargs):
            payload = make_payload(*args, **kwargs)
            self.summary.write_bytes(self.summary.read_bytes() + b' ')
            return payload
        with patch.object(placement, 'make_payload', side_effect=changed), \
                patch.object(gate, '_publish_private', side_effect=AssertionError('do not publish')) as publish:
            self.assert_rejected()
            publish.assert_not_called()

    def test_every_source_is_revalidated_after_derivation_and_after_publication(self):
        events = []
        verify = gate._Snapshot.verify
        requests = placement._placement_requests
        def recorded(snapshot):
            events.append(('published' if (self.out / 'manifest.json').exists() else 'verify', snapshot.path))
            verify(snapshot)
        def derived(*args, **kwargs):
            result = requests(*args, **kwargs)
            events.append(('derive', None))
            return result
        with patch.object(gate._Snapshot, 'verify', new=recorded), \
                patch.object(placement, '_placement_requests', side_effect=derived):
            self.run_preflight()
        last_derivation = max(index for index, event in enumerate(events) if event[0] == 'derive')
        before_publish = {path for event, path in events[last_derivation + 1:] if event == 'verify'}
        after_publish = {path for event, path in events[last_derivation + 1:] if event == 'published'}
        sources = {self.data, self.instructions, self.source, self.answers, self.summary, self.comparison}
        self.assertEqual(before_publish, sources)
        self.assertEqual(after_publish, sources)

    def test_validated_dataset_sidecar_is_included_in_all_snapshot_revalidation(self):
        sidecar = self.data.with_suffix('.manifest.json')
        self.write(sidecar, {'converted_sha256': source_fixture.sha(self.data.read_bytes())})
        self.manifest['dataset']['provenance'] = {
            'status': 'validated', 'sidecar': str(sidecar),
            'sidecar_sha256': source_fixture.sha(sidecar.read_bytes()),
            'converted_sha256': source_fixture.sha(self.data.read_bytes()),
        }
        self.comparison_value['dataset'] = deepcopy(self.manifest['dataset'])
        self.repin()
        verify = gate._Snapshot.verify
        verified_after_write = []
        def recorded(snapshot):
            if (self.out / 'manifest.json').exists():
                verified_after_write.append(snapshot.path)
            verify(snapshot)
        with patch.object(gate._Snapshot, 'verify', new=recorded):
            result = self.run_preflight()
        self.assertIn(sidecar, verified_after_write)
        self.assertEqual(len(result['source_snapshots']), 7)
        self.assertIn({'path': str(sidecar), 'byte_sha256': source_fixture.sha(sidecar.read_bytes())},
                      result['source_snapshots'])

    def test_cli_has_no_live_mode_and_requires_external_pin(self):
        args = ['--data', str(self.data), '--source-manifest', str(self.source),
                '--source-comparison-manifest', str(self.comparison), '--instructions-file', str(self.instructions),
                '--out', str(self.out)]
        pins = ['--expected-comparison-sha256', self.pin,
                '--expected-source-summary-sha256', self.summary_pin]
        for extra in ([], ['--expected-comparison-sha256', self.pin]):
            with self.subTest(extra=extra), self.assertRaises(SystemExit), patch('sys.stderr'):
                placement.main(args + extra)
        for option in ('--generate', '--resume', '--max-tokens', '--reasoning-effort', '--base-url'):
            with self.subTest(option=option), self.assertRaises(SystemExit), patch('sys.stderr'):
                placement.main(args + pins + [option])
        self.assertFalse(self.out.exists())
        with patch('builtins.print') as printed:
            self.assertEqual(placement.main(args + pins), 0)
        summary = json.loads(printed.call_args.args[0])
        self.assertEqual(summary['calls'], 0)
        self.assertEqual(summary['known_failure_count'], 3)
        self.assertEqual(summary['execution_support']['status'], 'NOT_READY')


if __name__ == '__main__':
    unittest.main()
