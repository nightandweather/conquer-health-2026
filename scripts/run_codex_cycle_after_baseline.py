"""One-shot local development generation after a frozen baseline completes.

Waits in this process; this is not a recurring automation. Source identities are
frozen while waiting. Candidate and baseline rows retain their original names,
statuses and full contents in the comparison snapshot. No quality score is made.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.healthbench import DEFAULT_BASE_URL, DEFAULT_MODEL, canonical_hash, load_dataset, utc_now, write_json
from benchmarks.healthbench_codex_cycle import (REVIEW_REQUIRED, ModelMetadataUnavailable, _output_path, codex_baseline_snapshot,
                                               load_instructions, make_payload, run_cycle)
from benchmarks.healthbench_score import _answer_index


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_manifest(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('Manifest requires a JSON object')
    return value, raw


def baseline_scope(value: dict) -> str:
    return canonical_hash({key: value.get(key) for key in
                           ('dataset', 'selected_prompt_ids', 'selected_arms', 'config', 'model', 'arms')})


def validate_baseline(value: dict, dataset: dict) -> None:
    config, model = value.get('config', {}), value.get('model', {})
    if (not isinstance(config, dict) or not isinstance(model, dict)
            or config.get('model') != DEFAULT_MODEL or config.get('base_url') != DEFAULT_BASE_URL
            or model.get('name') != DEFAULT_MODEL or not isinstance(model.get('digest'), str) or not model['digest']):
        raise ValueError('Queue requires the current local gpt-oss:20b baseline and recorded model digest')
    if (not isinstance(value.get('dataset'), dict) or value['dataset'].get('sha256') != dataset['sha256']
            or not isinstance(value.get('selected_prompt_ids'), list)
            or value['selected_prompt_ids'][:20] != dataset['selected_prompt_ids']):
        raise ValueError('Development dataset or first 20 baseline IDs disagree')
    hashes = value['dataset'].get('selected_prompt_sha256', {})
    if not isinstance(hashes, dict) or any(hashes.get(key) != expected for key, expected in
                                         dataset['selected_prompt_sha256'].items()):
        raise ValueError('Development prompt hashes disagree with the baseline')
    if not isinstance(value.get('arms'), dict) or 'grounded_call' not in value['arms']:
        raise ValueError('Baseline requires grounded_call as the comparison control')


def comparison_snapshot(data: Path, baseline_manifest: Path, candidate: Path, out: Path,
                        name: str, instructions: str, instructions_metadata: dict,
                        *, packet_runner=None, allow_finalized_dev_control: bool = False,
                        expected_baseline_snapshot: dict | None = None) -> dict:
    """Validate immutable source snapshots, then preserve actual selected rows."""
    out = _output_path(out)
    items, dataset = load_dataset(data, limit=0)
    data_rows = {item['prompt_id']: item for item in items}
    dev_ids = [item['prompt_id'] for item in items[:20]]
    baseline, baseline_raw = read_manifest(baseline_manifest)
    candidate_manifest, candidate_raw = read_manifest(candidate / 'manifest.json')
    if baseline.get('status') != 'completed' and not allow_finalized_dev_control:
        raise ValueError('Comparison requires a completed baseline')
    dev_items, dev_dataset = load_dataset(data, limit=20)
    validate_baseline(baseline, dev_dataset)
    frozen_baseline = codex_baseline_snapshot(baseline_manifest, prepare_only=False, dataset=dev_dataset,
                                             selected=dev_items, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                                             allow_finalized_dev_control=allow_finalized_dev_control)
    if expected_baseline_snapshot is not None and frozen_baseline != expected_baseline_snapshot:
        raise ValueError('Baseline source changed after the queue validated its development control')
    if (allow_finalized_dev_control and
            (candidate_manifest.get('baseline') != frozen_baseline
             or frozen_baseline['sha256'] != _sha(baseline_raw))):
        raise ValueError('Validated development control changed after candidate generation')
    if (candidate_manifest.get('dataset', {}).get('sha256') != dataset['sha256']
            or candidate_manifest.get('selected_prompt_ids') != dev_ids
            or candidate_manifest.get('selected_arms') != [name]
            or candidate_manifest.get('model', {}).get('name') != DEFAULT_MODEL
            or candidate_manifest.get('model', {}).get('digest') != baseline['model']['digest']
            or candidate_manifest.get('instructions', {}).get('byte_sha256') != instructions_metadata['byte_sha256']):
        raise ValueError('Candidate manifest identity differs from the queued development experiment')
    expected_config = {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'low',
                       'max_attempts': 1, 'suffix': instructions, 'prompt_revision': instructions_metadata['version']}
    if candidate_manifest.get('arms', {}).get(name) != expected_config:
        raise ValueError('Candidate arm configuration differs from the queued general prompt')
    sources, source_rows = {}, {}
    for label, path, raw_manifest, manifest_value in (
            ('baseline', baseline_manifest.parent / 'answers.jsonl', baseline_raw, baseline),
            ('candidate', candidate / 'answers.jsonl', candidate_raw, candidate_manifest)):
        raw = path.read_bytes()  # One byte snapshot per source, never reread.
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        _answer_index(rows, data_rows)
        source_rows[label] = rows
        sources[label] = {'manifest_byte_sha256': _sha(raw_manifest), 'answers_byte_sha256': _sha(raw),
                          'original_manifest_status': manifest_value.get('status'),
                          'manifest_path': str((baseline_manifest if label == 'baseline' else candidate / 'manifest.json').resolve()),
                          'answers_path': str(path.resolve())}
        if label == 'baseline' and allow_finalized_dev_control:
            control = frozen_baseline['development_control']
            if control['answers_byte_sha256'] != _sha(raw):
                raise ValueError('Validated development control answers changed while building comparison')
            sources[label]['validated_development_control'] = deepcopy(control)
    baseline_rows = {row['prompt_id']: row for row in source_rows['baseline'] if row['arm'] == 'grounded_call'}
    candidate_rows = {row['prompt_id']: row for row in source_rows['candidate']}
    if any(row['arm'] != name or row['prompt_id'] not in dev_ids for row in source_rows['candidate']):
        raise ValueError('Candidate source includes an unexpected arm or heldout prompt')
    copied, provenance = [], []
    for prompt_id in dev_ids:
        if prompt_id not in baseline_rows:
            raise ValueError('Completed baseline is missing a development control answer')
        for source, row in (('baseline', baseline_rows[prompt_id]), ('candidate', candidate_rows.get(prompt_id))):
            if row is None:
                continue  # Keep missing candidate answers missing, never synthesize zero/error rows.
            if (row.get('dataset_sha256') != dataset['sha256']
                    or row.get('prompt_sha256') != canonical_hash(data_rows[prompt_id]['prompt'])
                    or row.get('model') != DEFAULT_MODEL or row.get('model_digest') != baseline['model']['digest']):
                raise ValueError('Source answer dataset, prompt or model identity changed')
            if source == 'candidate':
                payload = make_payload(data_rows[prompt_id], instructions)
                if (row.get('request_sha256') != canonical_hash(payload)
                        or row.get('input_sha256') != canonical_hash(payload['messages'])
                        or row.get('instructions_byte_sha256') != instructions_metadata['byte_sha256']):
                    raise ValueError('Candidate payload or instruction hash changed')
            copied.append(deepcopy(row))
            provenance.append({'source': source, 'arm': row['arm'], 'prompt_id': prompt_id,
                               'original_status': row['status'], 'full_row_sha256': canonical_hash(row)})
    if out.exists():
        raise ValueError('Comparison output must be fresh')
    selected_dataset = deepcopy(dataset)
    selected_dataset.update(selected_prompt_ids=dev_ids,
                            selected_prompt_sha256={key: canonical_hash(data_rows[key]['prompt']) for key in dev_ids})
    comparison = {'schema_version': 1, 'status': 'snapshot', 'dataset': selected_dataset,
                  'selected_prompt_ids': dev_ids, 'selected_arms': ['grounded_call', name],
                  'arms': {'grounded_call': deepcopy(baseline['arms']['grounded_call']),
                           name: deepcopy(candidate_manifest['arms'][name])},
                  'sources': sources, 'row_provenance': provenance, 'planned_answers': len(dev_ids) * 2,
                  'written_answers': len(copied), 'score': None, 'score_status': REVIEW_REQUIRED}
    out.mkdir(parents=True, mode=0o700)
    write_json(out / 'manifest.json', comparison)
    descriptor = os.open(out / 'answers.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        for row in copied:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    if packet_runner is None:
        # Import only now, so the launched queue uses the installed generalized packet builder.
        from benchmarks.healthbench_review import create_review_packets
        packet_runner = create_review_packets
    progress = packet_runner(data, out / 'answers.jsonl', out / 'manifest.json', out / 'review',
                             paired_arms=('grounded_call', name))
    return {'planned_answers': len(dev_ids) * 2, 'written_answers': len(copied),
            'review': progress, 'sources': sources, 'score': None, 'score_status': REVIEW_REQUIRED}


def run_queue(data: Path, baseline_manifest: Path, instructions_file: Path, out: Path, *,
              name: str = 'codex_guided_v3', version: str | None = None, maximum_wait_seconds: float = 7200,
              cycle_runner=None, packet_runner=None, clock=time.monotonic, sleeper=time.sleep,
              allow_finalized_dev_control: bool = False) -> int:
    if (isinstance(maximum_wait_seconds, bool) or not isinstance(maximum_wait_seconds, (int, float))
            or not math.isfinite(maximum_wait_seconds) or maximum_wait_seconds <= 0):
        raise ValueError('Maximum wait must be positive and finite')
    out = _output_path(out)
    if out.exists():
        raise ValueError('Queue output must be a new private directory; no overwrite or resumption')
    selected, dataset = load_dataset(data, limit=20)
    baseline, initial_raw = read_manifest(baseline_manifest)
    validate_baseline(baseline, dataset)
    instructions, instruction_metadata = load_instructions(instructions_file, name, name if version is None else version)
    frozen_scope = baseline_scope(baseline)
    state = {'schema_version': 1, 'status': 'waiting_for_baseline', 'started_at': utc_now(),
             'baseline_scope_sha256': frozen_scope, 'baseline_initial_byte_sha256': _sha(initial_raw),
             'dataset_sha256': dataset['sha256'], 'development_prompt_ids': dataset['selected_prompt_ids'],
             'instructions': instruction_metadata, 'model': deepcopy(baseline['model']),
             'allow_finalized_dev_control': allow_finalized_dev_control,
             'score': None, 'score_status': REVIEW_REQUIRED}
    out.mkdir(parents=True, mode=0o700)
    write_json(out / 'queue.json', state)
    def stop(reason, code=2):
        state.update(status='interrupted' if code == 130 else 'blocked', reason=reason, finished_at=utc_now())
        write_json(out / 'queue.json', state)
        return code
    deadline = clock() + maximum_wait_seconds
    validated = None
    try:
        while True:
            current, raw = read_manifest(baseline_manifest)
            if baseline_scope(current) != frozen_scope:
                return stop('baseline_identity_changed')
            if _sha(data.read_bytes()) != dataset['sha256']:
                return stop('dataset_changed')
            if _sha(instructions_file.read_bytes()) != instruction_metadata['byte_sha256']:
                return stop('instructions_changed')
            status = current.get('status')
            if status == 'completed' or (allow_finalized_dev_control and status == 'partial'):
                break
            if status != 'running':
                return stop('baseline_' + str(status))
            remaining = deadline - clock()
            if remaining <= 0:
                return stop('maximum_wait_elapsed')
            sleeper(min(5, remaining))
        if allow_finalized_dev_control:
            validated = codex_baseline_snapshot(baseline_manifest, prepare_only=False, dataset=dataset,
                                                selected=selected, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                                                allow_finalized_dev_control=True)
            if validated['sha256'] != _sha(raw):
                return stop('baseline_changed_during_control_validation')
            state['validated_development_control'] = validated['development_control']
        state.update(status='running', baseline_finalized_byte_sha256=_sha(raw), original_baseline_status=status)
        if status == 'completed':
            state['baseline_completed_byte_sha256'] = _sha(raw)
        write_json(out / 'queue.json', state)
        runner = cycle_runner or run_cycle
        summary, code = runner(data, baseline_manifest, instructions_file, out / 'candidate',
                               name=name, version=version, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                               allow_finalized_dev_control=allow_finalized_dev_control,
                               expected_baseline_snapshot=validated)
        state['generation'] = summary
        if (out / 'candidate' / 'answers.jsonl').exists():
            state['comparison'] = comparison_snapshot(data, baseline_manifest, out / 'candidate', out / 'comparison',
                                                      name, instructions, instruction_metadata, packet_runner=packet_runner,
                                                      allow_finalized_dev_control=allow_finalized_dev_control,
                                                      expected_baseline_snapshot=validated)
        if code == 130:
            return stop('candidate_interrupted', 130)
        state.update(status='completed' if code == 0 and summary.get('status') == 'completed' else 'partial',
                     finished_at=utc_now())
        write_json(out / 'queue.json', state)
        return 0 if state['status'] == 'completed' else 1
    except KeyboardInterrupt:
        return stop('queue_interrupted', 130)
    except ModelMetadataUnavailable:
        state['generation'] = {'status': 'blocked', 'reason': 'model_metadata_unavailable',
                               'calls': 0, 'written_answers': 0, 'unattempted_answers': len(selected),
                               'score': None, 'score_status': REVIEW_REQUIRED}
        return stop('model_metadata_unavailable')
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return stop('queue_input_or_snapshot_error:' + type(exc).__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'baseline-manifest', 'instructions-file', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--candidate-name', default='codex_guided_v3')
    parser.add_argument('--instructions-version')
    parser.add_argument('--maximum-wait-seconds', type=float, default=7200)
    parser.add_argument('--allow-finalized-dev-control', action='store_true',
                        help='Explicitly validate completed dev20 controls while retaining original partial baseline status')
    args = parser.parse_args(argv)
    try:
        code = run_queue(args.data, args.baseline_manifest, args.instructions_file, args.out,
                         name=args.candidate_name, version=args.instructions_version,
                         maximum_wait_seconds=args.maximum_wait_seconds,
                         allow_finalized_dev_control=args.allow_finalized_dev_control)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print('Codex-guided development queue finished; independent Codex review remains pending.', flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main())
