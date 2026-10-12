"""Regenerate development answers from a versioned general Codex-guided prompt.

This standalone Ollama experiment makes one model call per development case;
an independent Codex review remains required. It never sends per-case reviews,
rubrics, reference answers or canaries to the generation model. Each invocation
uses a new immutable output directory; automatic resumption is not supported.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, ROOT, RequestFailure, arm_config,
                          Transport, build_payload, canonical_hash, load_dataset,
                          sum_usage, usage_values, utc_now, write_json)
from .healthbench_refine import DEVELOPMENT_LIMIT, baseline_snapshot
from .healthbench_score import _answer_index


REVIEW_REQUIRED = 'independent_codex_review_required'
REASONING_EFFORTS = ('low', 'medium', 'high')


def validate_reasoning_effort(reasoning_effort: str) -> None:
    if not isinstance(reasoning_effort, str) or reasoning_effort not in REASONING_EFFORTS:
        raise ValueError('--reasoning-effort must be low, medium or high')


class ModelMetadataUnavailable(ValueError):
    """Model identity could not be read; no inference was attempted."""


def _source_object(raw: bytes) -> dict:
    def unique_fields(pairs):
        value = {}
        for key, field in pairs:
            if key in value:
                raise ValueError('Source JSON contains duplicate fields')
            value[key] = field
        return value
    def reject_constant(value):
        raise ValueError('Source JSON contains a nonfinite constant')
    value = json.loads(raw, object_pairs_hook=unique_fields, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError('Source JSON requires an object')
    return value


def codex_baseline_snapshot(path: Path, *, prepare_only: bool, dataset: dict,
                            selected: list[dict], model: str, base_url: str,
                            allow_finalized_dev_control: bool = False) -> dict:
    """Keep the strict default; explicitly validate a finalized dev-only control.

    The alternative does not relabel the original run as completed. All source
    rows are checked structurally; only the first 20 control inputs are examined
    and reused. Source byte hashes and complete dev row hashes freeze this gate.
    """
    if not allow_finalized_dev_control:
        return baseline_snapshot(path, prepare_only=prepare_only, dataset=dataset, model=model)
    raw_manifest = path.read_bytes()
    value = _source_object(raw_manifest)
    if value.get('status') not in {'completed', 'partial'}:
        raise ValueError('Development control requires a finalized completed or partial baseline')
    finished = value.get('finished_at')
    try:
        if not isinstance(finished, str) or datetime.fromisoformat(finished).utcoffset() is None:
            raise ValueError('Missing timezone')
    except ValueError as exc:
        raise ValueError('Development control requires a valid baseline finished_at') from exc
    result = baseline_snapshot(path, prepare_only=True, dataset=dataset, model=model)
    # Guard against reading a concurrently replaced manifest through two paths.
    if result['sha256'] != hashlib.sha256(raw_manifest).hexdigest():
        raise ValueError('Baseline manifest changed while validating development control')
    ids, arms = value.get('selected_prompt_ids'), value.get('selected_arms')
    config, metadata = value.get('config'), value.get('model')
    hashes = value['dataset'].get('selected_prompt_sha256')
    expected_arm = arm_config(('grounded_call',))['grounded_call']
    if (len(selected) != DEVELOPMENT_LIMIT or len(ids) < DEVELOPMENT_LIMIT
            or dataset['selected_prompt_ids'] != [item['prompt_id'] for item in selected]
            or value['dataset'].get('selected_prompt_ids') != ids
            or not isinstance(hashes, dict)
            or any(hashes.get(key) != expected
                   for key, expected in dataset['selected_prompt_sha256'].items())
            or not isinstance(arms, list) or not arms or any(not isinstance(arm, str) for arm in arms)
            or len(arms) != len(set(arms)) or 'grounded_call' not in arms
            or not isinstance(config, dict) or config.get('model') != model
            or config.get('base_url') != base_url.rstrip('/')
            or not isinstance(metadata, dict) or metadata.get('name') != model
            or not isinstance(metadata.get('digest'), str) or not metadata['digest']
            or not isinstance(value.get('arms'), dict)
            or canonical_hash(value['arms'].get('grounded_call')) != canonical_hash(expected_arm)):
        raise ValueError('Development control dataset, IDs, model, endpoint or fixed arm configuration disagree')
    summary_path, answers_path = path.parent / 'summary.json', path.parent / 'answers.jsonl'
    raw_summary, raw_answers = summary_path.read_bytes(), answers_path.read_bytes()
    summary = _source_object(raw_summary)
    rows = [_source_object(line.encode('utf-8')) for line in raw_answers.decode('utf-8').splitlines() if line.strip()]
    _answer_index(rows, {prompt_id: {} for prompt_id in ids})
    expected_keys = {(prompt_id, arm) for prompt_id in ids for arm in arms}
    if {(row['prompt_id'], row['arm']) for row in rows} != expected_keys:
        raise ValueError('Finalized baseline is missing rows or includes an unexpected arm')
    if any(not isinstance(row.get('attempts'), list) or not row['attempts']
           or any(not isinstance(attempt, dict) for attempt in row['attempts']) for row in rows):
        raise ValueError('Finalized baseline has corrupt attempt records')
    counts = dict(Counter(row['status'] for row in rows))
    calls = sum(len(row['attempts']) for row in rows)
    if (summary.get('status') != value['status'] or summary.get('finished_at') != finished
            or summary.get('selected_prompt_ids') != ids or summary.get('selected_arms') != arms
            or summary.get('planned_answers') != len(expected_keys)
            or summary.get('written_answers') != len(rows) or summary.get('unattempted_answers') != 0
            or summary.get('answer_status_counts') != counts or summary.get('calls') != calls
            or not set(counts).issubset({'completed', 'truncated', 'failed'})
            or (value['status'] == 'partial' and not counts.get('failed'))
            or (value['status'] == 'completed' and counts.get('failed'))):
        raise ValueError('Finalized baseline summary disagrees with manifest or actual source rows')
    controls = {row['prompt_id']: row for row in rows if row['arm'] == 'grounded_call'}
    provenance = []
    for item in selected:
        row = controls[item['prompt_id']]
        payload = build_payload(item, 'grounded_call', model)
        request_hash, input_hash = canonical_hash(payload), canonical_hash(payload['messages'])
        if (row.get('status') != 'completed' or not isinstance(row.get('answer'), str) or not row['answer'].strip()
                or row.get('finish_reason') != 'stop' or row.get('error') is not None
                or row.get('dataset_sha256') != dataset['sha256']
                or row.get('prompt_sha256') != canonical_hash(item['prompt'])
                or row.get('model') != model or row.get('model_digest') != metadata['digest']
                or len(row['attempts']) != 1 or type(row.get('selected_attempt')) is not int or row['selected_attempt'] != 1
                or row.get('fallback_used') is not False):
            raise ValueError('Development control requires 20 nonempty completed answers with matching source identity')
        attempt = row['attempts'][0]
        if (type(attempt.get('number')) is not int or attempt['number'] != 1 or attempt.get('status') != 'completed'
                or attempt.get('finish_reason') != 'stop' or attempt.get('response_received') is not True
                or attempt.get('error') is not None or attempt.get('answer') != row['answer']
                or attempt.get('request_sha256') != request_hash
                or any(canonical_hash(attempt.get(key)) != canonical_hash(payload[key])
                       for key in ('max_tokens', 'temperature', 'reasoning_effort'))
                or ('request_sha256' in row and row['request_sha256'] != request_hash)
                or ('input_sha256' in row and row['input_sha256'] != input_hash)
                or ('input_sha256' in attempt and attempt['input_sha256'] != input_hash)):
            raise ValueError('Development control completion, payload hashes or inference settings disagree')
        provenance.append({'prompt_id': item['prompt_id'], 'arm': 'grounded_call', 'original_status': row['status'],
                           'full_row_sha256': canonical_hash(row), 'input_sha256': input_hash,
                           'request_sha256': request_hash})
    result['development_control'] = {'policy': 'explicit_finalized_development_control',
                                    'original_manifest_status': value['status'], 'finished_at': finished,
                                    'summary_byte_sha256': hashlib.sha256(raw_summary).hexdigest(),
                                    'answers_byte_sha256': hashlib.sha256(raw_answers).hexdigest(),
                                    'validated_count': DEVELOPMENT_LIMIT, 'selected_prompt_ids': dataset['selected_prompt_ids'],
                                    'arm': 'grounded_call', 'arm_config_sha256': canonical_hash(expected_arm),
                                    'model_digest': metadata['digest'], 'row_provenance': provenance}
    return result


def _output_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    private_root = (ROOT / 'results').resolve()
    resolved = absolute.resolve()
    try:
        resolved.relative_to(private_root)
    except ValueError as exc:
        raise ValueError('--out must be inside this repository\'s private results/ directory') from exc
    if resolved == private_root:
        raise ValueError('--out requires a dedicated run directory')
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ValueError('Output cannot use symlink aliases')
    return resolved


def load_instructions(path: Path, name: str, version: str) -> tuple[str, dict]:
    """Require one actual Git-tracked regular UTF-8 source file in this repo."""
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name):
        raise ValueError('Candidate name requires a short lowercase identifier')
    if name in {'raw_default', 'grounded_call', 'lunit_call', 'single_call', 'bounded_refine'}:
        raise ValueError('Candidate name must distinguish this regeneration from existing arms')
    if not isinstance(version, str) or not version.strip() or len(version) > 100:
        raise ValueError('Instructions version must be nonempty and at most 100 characters')
    absolute = Path(os.path.abspath(path))
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ValueError('Instructions must not use symlink aliases')
    try:
        relative = absolute.resolve().relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError('Instructions must be inside the current repository') from exc
    if not absolute.is_file():
        raise ValueError('Instructions require a regular text file')
    raw = absolute.read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Instructions must be UTF-8 text') from exc
    if not text.strip() or '\x00' in text or len(raw) > 40000:
        raise ValueError('Instructions must be nonempty bounded text')
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--stage', '-z', '--', relative.as_posix()],
                             capture_output=True, timeout=10, check=False)
    records = [record for record in tracked.stdout.split(b'\x00') if record]
    if tracked.returncode or len(records) != 1 or b'\t' not in records[0]:
        raise ValueError('Instructions file must be Git tracked before preparation or generation')
    header, tracked_path = records[0].split(b'\t', 1)
    fields = header.split()
    if (len(fields) != 3 or fields[2] != b'0' or fields[0] not in {b'100644', b'100755'}
            or tracked_path != relative.as_posix().encode('utf-8')):
        raise ValueError('Instructions must be an unambiguous Git-tracked regular file')
    head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', 'HEAD'],
                          capture_output=True, timeout=10, check=False)
    metadata = {'name': name, 'version': version, 'path': relative.as_posix(),
                'byte_sha256': hashlib.sha256(raw).hexdigest(), 'git_tracked': True,
                'git_index_blob_sha': fields[1].decode('ascii'),
                'git_head': head.stdout.decode('ascii').strip() if head.returncode == 0 else None,
                'source_kind': 'versioned general instructions; no case-specific review inputs'}
    return text, metadata


def make_payload(item: dict, instructions: str, model: str = DEFAULT_MODEL, *,
                 reasoning_effort: str = 'low') -> dict:
    """Keep original roles/text, appending only the generic last-user suffix."""
    validate_reasoning_effort(reasoning_effort)
    body = build_payload(item, 'grounded_call', model)
    body['reasoning_effort'] = reasoning_effort
    body['messages'][-1]['content'] = item['prompt'][-1]['content'] + '\n\n[' + instructions + ']'
    return body


def generate_candidate(item: dict, instructions: str, name: str, model: str, transport,
                       *, dataset_sha256: str, block_on_transport_failure: bool = False,
                       reasoning_effort: str = 'low') -> dict:
    body = make_payload(item, instructions, model, reasoning_effort=reasoning_effort)
    started = time.monotonic()
    attempt = {'number': 1, 'max_tokens': body['max_tokens'], 'temperature': body['temperature'],
               'reasoning_effort': body['reasoning_effort'], 'request_sha256': canonical_hash(body),
               'input_sha256': canonical_hash(body['messages']), 'answer': '', 'finish_reason': None,
               'usage': {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None},
               'status': 'failed', 'error': None, 'response_received': False}
    try:
        response = transport.complete(body)
        choices = response.get('choices') if isinstance(response, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise RequestFailure('invalid_completion_response')
        choice = choices[0]
        message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
        answer = message.get('content')
        if answer is not None and not isinstance(answer, str):
            raise RequestFailure('invalid_answer_content')
        attempt.update(answer=answer or '', finish_reason=choice.get('finish_reason'),
                       usage=usage_values(response), response_received=True)
        if attempt['finish_reason'] not in {'stop', 'length'}:
            raise RequestFailure('unexpected_finish_reason')
        if not attempt['answer'].strip():
            raise RequestFailure('empty_answer')
        attempt['status'] = 'completed' if attempt['finish_reason'] == 'stop' else 'truncated'
    except RequestFailure as exc:
        attempt.update(status='blocked' if block_on_transport_failure and not attempt['response_received'] else 'failed',
                       error={'code': exc.code, 'http_status': exc.http_status})
    attempt['seconds'] = round(time.monotonic() - started, 3)
    return {'prompt_id': item['prompt_id'], 'arm': name, 'model': model,
            'dataset_sha256': dataset_sha256, 'prompt_sha256': canonical_hash(item['prompt']),
            'input_sha256': attempt['input_sha256'], 'request_sha256': attempt['request_sha256'],
            'answer': attempt['answer'], 'status': attempt['status'], 'finish_reason': attempt['finish_reason'],
            'error': attempt['error'], 'attempts': [attempt], 'usage': sum_usage([attempt]),
            'seconds': attempt['seconds'], 'fallback_used': False, 'selected_attempt': 1,
            'score': None, 'score_status': REVIEW_REQUIRED}


def run_cycle(data: Path, baseline_manifest: Path, instructions_file: Path, out: Path, *,
              name: str = 'codex_guided_v3', version: str | None = None,
              prepare_only: bool = False, base_url: str = DEFAULT_BASE_URL,
              model: str = DEFAULT_MODEL, timeout: float = 300, transport=None,
              allow_finalized_dev_control: bool = False,
              expected_baseline_snapshot: dict | None = None,
              reasoning_effort: str = 'low') -> tuple[dict, int]:
    validate_reasoning_effort(reasoning_effort)
    if not isinstance(model, str) or not model.strip():
        raise ValueError('--model requires a nonempty name')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('--timeout requires a positive finite value')
    if not isinstance(baseline_manifest, Path):
        raise ValueError('--baseline-manifest is required for development selection provenance')
    out = _output_path(out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError('Output must be empty; existing prepared, completed or interrupted runs cannot be resumed or overwritten')
    selected, dataset = load_dataset(data, DEVELOPMENT_LIMIT)
    baseline = codex_baseline_snapshot(baseline_manifest, prepare_only=prepare_only, dataset=dataset, selected=selected,
                                       model=model, base_url=base_url, allow_finalized_dev_control=allow_finalized_dev_control)
    if expected_baseline_snapshot is not None and baseline != expected_baseline_snapshot:
        raise ValueError('Baseline source changed after the queue validated its development control')
    instructions, source = load_instructions(instructions_file, name, name if version is None else version)
    payloads = [make_payload(item, instructions, model, reasoning_effort=reasoning_effort) for item in selected]
    client = transport if transport is not None else Transport(base_url, os.environ.get('HEALTHBENCH_API_KEY', ''), timeout)
    metadata = {'name': model, 'digest': None, 'metadata_status': 'not_requested'}
    if not prepare_only:
        baseline_digest = baseline.get('model', {}).get('digest')
        if not baseline_digest:
            raise ValueError('Live generation requires a recorded baseline model digest')
        metadata = client.model_metadata(model)
        if not isinstance(metadata, dict) or not metadata.get('digest'):
            raise ModelMetadataUnavailable('Current model metadata is unavailable; digest cannot be verified')
        if metadata.get('digest') != baseline_digest:
            raise ValueError('Current model digest must match the frozen baseline digest')
    started_at = utc_now()
    manifest = {'schema_version': 1, 'status': 'prepared' if prepare_only else 'running', 'started_at': started_at,
                'benchmark': 'HealthBench', 'experiment': 'Codex-guided general prompt regeneration; independent review pending',
                'dataset': dataset, 'selected_prompt_ids': dataset['selected_prompt_ids'], 'selected_arms': [name],
                'split': {'name': 'development', 'limit': DEVELOPMENT_LIMIT,
                          'selection': 'first 20 frozen baseline IDs; heldout excluded'},
                'baseline': baseline, 'instructions': source, 'model': metadata,
                'arms': {name: {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': reasoning_effort,
                                'max_attempts': 1, 'suffix': instructions, 'prompt_revision': source['version']}},
                'config': {'model': model, 'base_url': base_url.rstrip('/'), 'timeout': timeout,
                           'prepare_only': prepare_only, 'maximum_calls_per_case': 1, 'resume': False,
                           'allow_finalized_dev_control': allow_finalized_dev_control},
                'prepared_requests': [{'prompt_id': item['prompt_id'], 'prompt_sha256': canonical_hash(item['prompt']),
                                       'input_sha256': canonical_hash(payload['messages']),
                                       'request_sha256': canonical_hash(payload)}
                                      for item, payload in zip(selected, payloads)],
                'score': None, 'score_status': REVIEW_REQUIRED}
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    write_json(out / 'manifest.json', manifest)
    records, status, code = [], 'prepared' if prepare_only else 'completed', 0
    run_error = None
    started = time.monotonic()
    if not prepare_only:
        descriptor = os.open(out / 'answers.jsonl', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                for item in selected:
                    current = codex_baseline_snapshot(baseline_manifest, prepare_only=False, dataset=dataset, selected=selected,
                                                       model=model, base_url=base_url,
                                                       allow_finalized_dev_control=allow_finalized_dev_control)
                    if current != baseline:
                        raise ValueError('Baseline source changed during generation')
                    if hashlib.sha256(instructions_file.read_bytes()).hexdigest() != source['byte_sha256']:
                        raise ValueError('Instructions file changed during generation')
                    record = generate_candidate(item, instructions, name, model, client,
                                                dataset_sha256=dataset['sha256'], block_on_transport_failure=not records,
                                                reasoning_effort=reasoning_effort)
                    record.update(model_digest=metadata.get('digest'), instructions_byte_sha256=source['byte_sha256'],
                                  instructions_name=name, instructions_version=source['version'])
                    records.append(record)
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                    stream.flush()
                    if record['status'] == 'blocked':
                        status, code = 'blocked', 2
                        break
                current = codex_baseline_snapshot(baseline_manifest, prepare_only=False, dataset=dataset, selected=selected,
                                                   model=model, base_url=base_url,
                                                   allow_finalized_dev_control=allow_finalized_dev_control)
                if current != baseline:
                    raise ValueError('Baseline source changed during generation')
        except KeyboardInterrupt:
            status, code = 'interrupted', 130
        except ValueError as exc:
            status, code = 'blocked', 2
            run_error = {'code': 'provenance_changed', 'detail': str(exc)[:300]}
        except OSError:
            status, code = 'blocked', 2
            run_error = {'code': 'output_or_source_io_failure'}
        if status == 'completed' and any(record['status'] != 'completed' for record in records):
            status, code = 'partial', 1
    summary = {'status': status, 'started_at': started_at, 'finished_at': utc_now(),
               'seconds': round(time.monotonic() - started, 3), 'selected_prompt_ids': dataset['selected_prompt_ids'],
               'selected_arms': [name], 'planned_answers': len(selected), 'written_answers': len(records),
               'unattempted_answers': len(selected) - len(records), 'calls': len(records),
               'usage': sum_usage([attempt for record in records for attempt in record['attempts']]),
               'answer_status_counts': dict(Counter(record['status'] for record in records)),
               'error': run_error,
               'score': None, 'score_status': REVIEW_REQUIRED}
    manifest.update(status=status, finished_at=summary['finished_at'], error=run_error)
    write_json(out / 'manifest.json', manifest)
    write_json(out / 'summary.json', summary)
    return summary, code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'baseline-manifest', 'instructions-file', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--candidate-name', default='codex_guided_v3')
    parser.add_argument('--instructions-version')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--allow-finalized-dev-control', action='store_true',
                        help='Explicitly validate completed dev20 grounded_call controls from a finalized partial baseline')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--reasoning-effort', choices=REASONING_EFFORTS, default='low')
    args = parser.parse_args(argv)
    try:
        summary, code = run_cycle(args.data, args.baseline_manifest, args.instructions_file, args.out,
                                  name=args.candidate_name, version=args.instructions_version,
                                  prepare_only=args.prepare_only, base_url=args.base_url, model=args.model, timeout=args.timeout,
                                  reasoning_effort=args.reasoning_effort,
                                  allow_finalized_dev_control=args.allow_finalized_dev_control)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(f"Codex-guided regeneration {summary['status']}: {summary['written_answers']}/"
          f"{summary['planned_answers']} development answers; {summary['calls']} calls; independent Codex review required")
    return code


if __name__ == '__main__':
    sys.exit(main())
