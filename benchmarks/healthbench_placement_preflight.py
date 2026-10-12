"""Prepare a pinned instruction-placement comparison without any model calls.

Validate the unchanged original medium2048 development20 source using the
budget gate, then discard its budget proposal. This experiment moves the exact
last-user instruction block into a leading system message. Role, message
boundary and position change together as one instruction-placement package;
neither an instruction-priority mechanism nor better quality is established.
Only hashes and scalar provenance are published. Requests stay in memory.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from . import healthbench_budget_preflight as gate
from .healthbench import DEFAULT_MODEL, canonical_hash, utc_now
from .healthbench_codex_cycle import REVIEW_REQUIRED, make_payload


def _placement_requests(item: dict, frozen_text: str) -> tuple[dict, dict]:
    """Keep the old builder byte-for-byte and relocate its exact suffix block."""
    original = item['prompt']
    gate._require(not any(message['role'] == 'system' for message in original),
                  'Instruction placement requires no preexisting system messages')
    block = '\n\n[' + frozen_text + ']'
    control = make_payload(item, frozen_text, DEFAULT_MODEL,
                           reasoning_effort='medium', max_tokens=2048)
    treatment = deepcopy(control)
    treatment['messages'] = [{'role': 'system', 'content': block}, *deepcopy(original)]

    changed = sorted(key for key in set(control) | set(treatment)
                     if canonical_hash(control.get(key)) != canonical_hash(treatment.get(key)))
    gate._equal(changed, ['messages'], 'Instruction placement may change only messages')
    gate._equal(treatment['messages'][1:], original,
                'Treatment must preserve every original message role and content')
    gate._equal(control['messages'][:-1], original[:-1],
                'Control must preserve messages before the last user message')
    gate._equal(control['messages'][-1],
                {'role': original[-1]['role'], 'content': original[-1]['content'] + block},
                'Control must append the exact frozen instruction block')
    gate._require(treatment['messages'][0]['content'].encode('utf-8') == block.encode('utf-8'),
                  'Treatment instruction block bytes differ')
    inverse = deepcopy(treatment)
    inverse['messages'] = deepcopy(treatment['messages'][1:])
    inverse['messages'][-1]['content'] += treatment['messages'][0]['content']
    gate._equal(inverse, control, 'Inverse placement must recover the cached control request')
    return control, treatment


def prepare_instruction_placement(data: Path, source_manifest: Path,
                                  source_comparison_manifest: Path, instructions_file: Path,
                                  out: Path, *, expected_comparison_sha256: str,
                                  expected_source_summary_sha256: str) -> dict:
    """Prepare hashes only; no role-aware execution runner is provided.

    Use a fresh private output directory with one preparing writer. Publication
    reuses the budget gate's pinned directory FDs and source revalidation. Its
    portable POSIX mkdir -> first-stat same-UID replacement limitation remains.
    """
    source_proof, snapshots, items, rows, source, _ = gate._validate_budget_source(
        data, source_manifest, source_comparison_manifest, instructions_file, out,
        expected_comparison_sha256=expected_comparison_sha256,
        expected_source_summary_sha256=expected_source_summary_sha256)
    # The gate's proposal is exclusively a budget experiment. Never use its
    # candidate request hashes, changed_fields or candidate token budget here.
    gate._require(all(message['role'] != 'system'
                      for item in items for message in item['prompt']),
                  'Instruction placement requires no preexisting system messages in all development20 records')
    instruction_snapshot = next(entry for entry in snapshots
                                if entry.path == gate._path(instructions_file))
    frozen_text = instruction_snapshot.raw.decode('utf-8')
    block = '\n\n[' + frozen_text + ']'
    block_sha = gate._sha(block.encode('utf-8'))
    source_requests = source['prepared_requests']
    proofs = []
    settings = None
    for item, row, cached_request in zip(items, rows, source_requests):
        control, treatment = _placement_requests(item, frozen_text)
        control_hash = canonical_hash(control)
        control_input_hash = canonical_hash(control['messages'])
        gate._require(control_hash == cached_request['request_sha256']
                      and control_input_hash == cached_request['input_sha256'],
                      'Control must exactly match the original cached medium2048 request and input')
        current_settings = {key: deepcopy(value) for key, value in control.items() if key != 'messages'}
        gate._equal({key: value for key, value in treatment.items() if key != 'messages'},
                    current_settings, 'Treatment request settings must equal control settings')
        if settings is None:
            settings = current_settings
        else:
            gate._equal(current_settings, settings, 'All placement requests must use the same settings')
        inverse = deepcopy(treatment)
        inverse['messages'] = deepcopy(treatment['messages'][1:])
        inverse['messages'][-1]['content'] += treatment['messages'][0]['content']
        proofs.append({'prompt_id': item['prompt_id'], 'source_arm': gate.ARM,
                       'original_status': row['status'], 'full_row_sha256': canonical_hash(row),
                       'prompt_sha256': canonical_hash(item['prompt']),
                       'original_messages_sha256': canonical_hash(treatment['messages'][1:]),
                       'control_input_sha256': control_input_hash,
                       'treatment_input_sha256': canonical_hash(treatment['messages']),
                       'control_request_sha256': control_hash,
                       'treatment_request_sha256': canonical_hash(treatment),
                       'inverse_control_request_sha256': canonical_hash(inverse),
                       'frozen_block_byte_sha256': block_sha, 'changed_fields': ['messages']})

    result = {'schema_version': 1, 'status': 'prepared', 'prepared_at': utc_now(),
              'mode': 'local_prepare_only', 'calls': 0, 'generation_calls': 0,
              'model_metadata_calls': 0, 'network_calls': 0, 'transports_created': 0,
              'split': {'name': 'development', 'limit': 20, 'heldout_included': False},
              'selected_prompt_ids': source_proof['selected_prompt_ids'],
              'dataset_byte_sha256': source_proof['dataset_byte_sha256'],
              'instructions_byte_sha256': source_proof['instructions_byte_sha256'],
              'instructions_revision': gate.REVISION, 'frozen_block_byte_sha256': block_sha,
              'model': deepcopy(source_proof['model']), 'endpoint': source_proof['endpoint'],
              'source_arm': gate.ARM, 'request_settings': settings,
              'reasoning_effort': 'medium', 'temperature': 0, 'max_attempts': 1,
              'control_max_tokens': 2048, 'treatment_max_tokens': 2048,
              'changed_fields': ['messages'],
              'experiment': {'factor': 'instruction_placement_package',
                             'components_changed_together': ['role', 'message_boundary', 'position'],
                             'control': 'exact frozen block appended to last user content',
                             'treatment': 'exact frozen block in leading system message before unchanged original messages',
                             'instruction_wording_and_block_bytes_identical': True,
                             'isolates_priority_only': False,
                             'mechanism_hypothesis': 'role-dependent instruction handling',
                             'mechanism_proven': False, 'quality_improvement_proven': False},
              'known_failure_count': source_proof['known_failure_count'],
              'source_status_counts': deepcopy(source_proof['source_status_counts']),
              'original_manifest_status': source_proof['original_manifest_status'],
              'source_finished_at': source_proof['source_finished_at'],
              'source_byte_hashes': deepcopy(source_proof['source_byte_hashes']),
              'source_snapshots': [{'path': str(entry.path), 'byte_sha256': entry.sha256}
                                   for entry in snapshots],
              'external_comparison_sha256_pin': expected_comparison_sha256,
              'external_source_summary_sha256_pin': expected_source_summary_sha256,
              'row_provenance': proofs,
              'known_inference_failures': deepcopy(source_proof['known_inference_failures']),
              'medical_judgments': 'not_performed; source inference failures retain utility zero separately',
              'criteria_predicted': False, 'score': None, 'score_status': REVIEW_REQUIRED,
              'execution_support': {'ready': False, 'status': 'NOT_READY',
                                    'reason': 'a role-aware instruction-placement execution runner is absent'},
              'fixed_low_control_gate': 'unchanged', 'default_payload_builder': 'unchanged'}
    # Revalidate every source after constructing the in-memory requests, before
    # publishing. The publisher revalidates them after its write and rolls back
    # its own inode on failure, retaining its documented concurrency limits.
    for entry in snapshots:
        entry.verify()
    gate._publish_private(gate._path(out), result, snapshots)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'source-manifest', 'source-comparison-manifest', 'instructions-file', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--expected-comparison-sha256', required=True,
                        help='Externally pinned SHA256 of the unchanged source comparison manifest raw bytes')
    parser.add_argument('--expected-source-summary-sha256', required=True,
                        help='Externally pinned SHA256 of the unchanged medium2048 source summary raw bytes')
    args = parser.parse_args(argv)
    try:
        result = prepare_instruction_placement(
            args.data, args.source_manifest, args.source_comparison_manifest,
            args.instructions_file, args.out,
            expected_comparison_sha256=args.expected_comparison_sha256,
            expected_source_summary_sha256=args.expected_source_summary_sha256)
    except (ValueError, OSError, UnicodeError) as exc:
        print('Instruction-placement preflight rejected: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({'status': result['status'], 'calls': result['calls'],
                      'known_failure_count': result['known_failure_count'],
                      'execution_support': result['execution_support']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
