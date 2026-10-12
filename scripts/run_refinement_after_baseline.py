"""Run two bounded development trials after the frozen local comparison finishes.

This is one sequential computation, not a recurring task. The importing process
keeps the prompt code it loaded at startup. No generation begins while the
baseline is running, and any failed/interrupted baseline stops the queue.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.healthbench import canonical_hash, load_dataset, utc_now, write_json
from benchmarks.healthbench_refine import RefinementConfig, run_refinement


def read_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Baseline manifest must be an object')
    return value


def baseline_scope(value: dict) -> str:
    return canonical_hash({key: value.get(key) for key in
                           ('dataset', 'selected_prompt_ids', 'config', 'model', 'arms')})


def draft_controls(run: Path) -> None:
    """Export actually observed draft attempts; these are not extra model calls."""
    controls = []
    for line in (run / 'answers.jsonl').read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        draft = next((x for x in row['attempts'] if x['stage'] == 'draft'), None)
        if draft is None:
            continue
        control = deepcopy(row)
        control.update(arm='draft_control', answer=draft['answer'], status=draft['status'],
                       finish_reason=draft['finish_reason'], error=draft['error'],
                       attempts=[draft], selected_attempt=draft['number'], calls=1,
                       usage=draft['usage'], seconds=draft['seconds'],
                       review=None, review_status='not_applied_to_observed_draft',
                       stop_reason='observed_draft_control',
                       selection_policy='original observed draft, extracted without a new generation call',
                       comparison_note='draft cost is measured within the refinement run; no independent single-call timing run')
        control['budget'] = {'maximum_calls': 1, 'maximum_revisions': 0,
                             'completion_token_limit': draft['max_tokens']}
        controls.append(control)
    path = run / 'draft_controls.jsonl'
    with path.open('x', encoding='utf-8') as stream:
        for row in controls:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
    path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--baseline-manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--maximum-wait-seconds', type=float, default=7200)
    args = parser.parse_args()
    try:
        args.out.resolve().relative_to((ROOT / 'results').resolve())
        if args.out.exists() or not 0 < args.maximum_wait_seconds <= 14400:
            raise ValueError('Use a new private results directory and a wait limit of 1..14400 seconds')
        _, dataset = load_dataset(args.data, 20)
        baseline = read_manifest(args.baseline_manifest)
        frozen_scope = baseline_scope(baseline)
        if (baseline.get('dataset', {}).get('sha256') != dataset['sha256']
                or baseline.get('selected_prompt_ids', [])[:20] != dataset['selected_prompt_ids']):
            raise ValueError('Development data does not match the frozen baseline')
        model = baseline.get('config', {}).get('model')
        base_url = baseline.get('config', {}).get('base_url')
        if model != 'gpt-oss:20b' or base_url not in {'http://127.0.0.1:11434/v1', 'http://localhost:11434/v1'}:
            raise ValueError('This queue expects the current local gpt-oss:20b comparison')
        args.out.mkdir(parents=True, mode=0o700)
        state = {'status': 'waiting_for_baseline', 'started_at': utc_now(),
                 'dataset_sha256': dataset['sha256'], 'development_count': len(dataset['selected_prompt_ids']),
                 'trials': {}, 'score': None, 'score_status': 'independent_codex_review_required'}
        write_json(args.out / 'queue.json', state)
        print('Waiting for frozen baseline; no refinement calls yet.', flush=True)
        until = time.monotonic() + args.maximum_wait_seconds
        while True:
            baseline = read_manifest(args.baseline_manifest)
            status = baseline.get('status')
            if baseline_scope(baseline) != frozen_scope:
                state.update(status='blocked', reason='baseline_identity_changed', finished_at=utc_now())
                write_json(args.out / 'queue.json', state)
                return 2
            if status == 'completed':
                break
            if status != 'running' or time.monotonic() >= until:
                state.update(status='blocked', reason='baseline_' + str(status), finished_at=utc_now())
                write_json(args.out / 'queue.json', state)
                return 2
            time.sleep(5)
        state.update(status='running', baseline_sha256=canonical_hash(baseline))
        write_json(args.out / 'queue.json', state)
        for effort in ('low', 'medium'):
            target = args.out / effort
            config = RefinementConfig(draft_effort=effort)
            print('Starting development trial: draft effort=' + effort, flush=True)
            summary, code = run_refinement(args.data, target, baseline_manifest=args.baseline_manifest,
                                           model=model, base_url=base_url, config=config)
            if (target / 'answers.jsonl').exists():
                draft_controls(target)
            state['trials'][effort] = summary
            write_json(args.out / 'queue.json', state)
            if code == 2 or code == 130:
                state.update(status='blocked', reason='trial_' + summary['status'], finished_at=utc_now())
                write_json(args.out / 'queue.json', state)
                return code
        state.update(status='completed' if all(x['status'] == 'completed' for x in state['trials'].values())
                     else 'partial', finished_at=utc_now())
        write_json(args.out / 'queue.json', state)
        print('Development trials finished; independent grading remains pending.', flush=True)
        return 0 if state['status'] == 'completed' else 1
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
