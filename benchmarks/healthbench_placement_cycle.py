"""Run one fixed development20 system-prefix treatment against cached medium2048.

The original control remains the pinned partial source with 17 completed and
three empty-answer failures. All twenty treatment cases use one medium2048
call each. Role, message boundary and position change as one placement package;
priority mechanisms and quality improvement require independent evidence.
Prepare mode creates no transport and performs no runtime metadata queries.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from . import healthbench_budget_cycle as driver
from . import healthbench_budget_preflight as gate
from . import healthbench_placement_preflight as preflight
from .healthbench import GROUNDED_SUFFIX, canonical_hash
from .healthbench_codex_cycle import make_payload

CANDIDATE_ARM = 'grounded_medium_system2048_v1'
PAIRED_ARMS = (gate.ARM, CANDIDATE_ARM)
EXPECTED_TEMPLATE_SHA256 = 'fa6710a93d78da62641e192361344be7a8c0a1c3737f139cf89f20ce1626b99c'


def _prepare_placement_plan(source_proof, items, controls, source):
    """Use the frozen preflight's placement transform on the validated source."""
    block = '\n\n[' + GROUNDED_SUFFIX + ']'
    block_sha = gate._sha(block.encode('utf-8'))
    payloads, row_proofs = [], []
    for item, row, cached in zip(items, controls, source['prepared_requests']):
        control, treatment = preflight._placement_requests(item, GROUNDED_SUFFIX)
        gate._require(canonical_hash(control) == cached['request_sha256']
                      and canonical_hash(control['messages']) == cached['input_sha256'],
                      'Placement control must match the original cached medium2048 request')
        actual = make_payload(item, GROUNDED_SUFFIX, reasoning_effort='medium', max_tokens=2048,
                              instruction_placement='system_prefix')
        gate._equal(actual, treatment, 'Placement runner must use the frozen exact-block transformation')
        inverse = deepcopy(treatment)
        inverse['messages'] = deepcopy(treatment['messages'][1:])
        inverse['messages'][-1]['content'] += treatment['messages'][0]['content']
        gate._equal(inverse, control, 'Placement inverse must recover the original control request')
        payloads.append(treatment)
        row_proofs.append({'prompt_id': item['prompt_id'], 'source_arm': gate.ARM,
                           'original_status': row['status'], 'full_row_sha256': canonical_hash(row),
                           'prompt_sha256': cached['prompt_sha256'],
                           'original_messages_sha256': canonical_hash(item['prompt']),
                           'control_input_sha256': cached['input_sha256'],
                           'treatment_input_sha256': canonical_hash(treatment['messages']),
                           'control_request_sha256': cached['request_sha256'],
                           'treatment_request_sha256': canonical_hash(treatment),
                           'inverse_control_request_sha256': canonical_hash(inverse),
                           'frozen_block_byte_sha256': block_sha, 'changed_fields': ['messages']})
    # Select only source facts; discard every budget-proposal field produced by
    # the shared source validator, including its proposed request hashes.
    keys = ('schema_version', 'selected_prompt_ids', 'dataset_byte_sha256',
            'instructions_byte_sha256', 'instructions_revision', 'model', 'endpoint',
            'known_failure_count', 'source_status_counts', 'original_manifest_status',
            'source_finished_at', 'source_byte_hashes', 'external_comparison_sha256_pin',
            'external_source_summary_sha256_pin', 'known_inference_failures',
            'medical_judgments', 'criteria_predicted', 'score', 'score_status', 'fixed_low_control_gate')
    proof = {key: deepcopy(source_proof[key]) for key in keys}
    proof.update(status='validated', mode='fixed_instruction_placement', calls=0,
                 source_arm=gate.ARM, control_max_tokens=2048, treatment_max_tokens=2048,
                 reasoning_effort='medium', temperature=0, max_attempts=1,
                 changed_fields=['messages'], frozen_block_byte_sha256=block_sha,
                 row_provenance=row_proofs,
                 experiment={'factor': 'instruction_placement_package',
                             'components_changed_together': ['role', 'message_boundary', 'position'],
                             'instruction_wording_and_block_bytes_identical': True,
                             'isolates_priority_only': False, 'mechanism_proven': False,
                             'quality_improvement_proven': False})
    return proof, payloads


def run_placement_cycle(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                        instructions_file: Path, out: Path, *, expected_comparison_sha256: str,
                        expected_source_summary_sha256: str, transport=None, timeout=300,
                        prepare_only: bool = False) -> tuple[dict, int]:
    """Choose the fixed placement recipe; expose no arm, payload or template overrides."""
    return driver._run_fixed_cycle(data, source_manifest, source_comparison_manifest,
        instructions_file, out, expected_comparison_sha256=expected_comparison_sha256,
        expected_source_summary_sha256=expected_source_summary_sha256,
        transport=transport, timeout=timeout, prepare_only=prepare_only,
        experiment='instruction_placement')


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
        summary, code = run_placement_cycle(args.data, args.source_manifest, args.source_comparison_manifest,
            args.instructions_file, args.out, expected_comparison_sha256=args.expected_comparison_sha256,
            expected_source_summary_sha256=args.expected_source_summary_sha256,
            timeout=args.timeout, prepare_only=args.prepare_only)
    except (ValueError, OSError):
        print('Placement cycle rejected or stopped; inspect the private run artifacts for recorded calls and state',
              file=sys.stderr)
        return 2
    print(json.dumps({'status': summary['status'], 'calls': summary['calls'],
                      'template_queries': summary['template_queries'],
                      'unattempted_answers': summary['unattempted_answers'],
                      'review_status': summary['review']['status'], 'score': None}, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
