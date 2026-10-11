"""Regenerate development answers from a versioned general Codex-guided prompt.

This standalone Ollama experiment makes one model call per development case;
an independent Codex review remains required. It never sends per-case reviews,
rubrics, reference answers or canaries to the generation model. Each invocation
uses a new immutable output directory; automatic resumption is not supported.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from .healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, ROOT, RequestFailure,
                          Transport, build_payload, canonical_hash, load_dataset,
                          sum_usage, usage_values, utc_now, write_json)
from .healthbench_refine import DEVELOPMENT_LIMIT, baseline_snapshot


REVIEW_REQUIRED = 'independent_codex_review_required'


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


def make_payload(item: dict, instructions: str, model: str = DEFAULT_MODEL) -> dict:
    """Keep original roles/text, appending only the generic last-user suffix."""
    body = build_payload(item, 'grounded_call', model)
    body['messages'][-1]['content'] = item['prompt'][-1]['content'] + '\n\n[' + instructions + ']'
    return body


def generate_candidate(item: dict, instructions: str, name: str, model: str, transport,
                       *, dataset_sha256: str, block_on_transport_failure: bool = False) -> dict:
    body = make_payload(item, instructions, model)
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
              model: str = DEFAULT_MODEL, timeout: float = 300, transport=None) -> tuple[dict, int]:
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
    baseline = baseline_snapshot(baseline_manifest, prepare_only=prepare_only, dataset=dataset, model=model)
    instructions, source = load_instructions(instructions_file, name, name if version is None else version)
    payloads = [make_payload(item, instructions, model) for item in selected]
    client = transport if transport is not None else Transport(base_url, os.environ.get('HEALTHBENCH_API_KEY', ''), timeout)
    metadata = {'name': model, 'digest': None, 'metadata_status': 'not_requested'}
    if not prepare_only:
        baseline_digest = baseline.get('model', {}).get('digest')
        if not baseline_digest:
            raise ValueError('Live generation requires a recorded baseline model digest')
        metadata = client.model_metadata(model)
        if metadata.get('digest') != baseline_digest:
            raise ValueError('Current model digest must match the frozen baseline digest')
    started_at = utc_now()
    manifest = {'schema_version': 1, 'status': 'prepared' if prepare_only else 'running', 'started_at': started_at,
                'benchmark': 'HealthBench', 'experiment': 'Codex-guided general prompt regeneration; independent review pending',
                'dataset': dataset, 'selected_prompt_ids': dataset['selected_prompt_ids'], 'selected_arms': [name],
                'split': {'name': 'development', 'limit': DEVELOPMENT_LIMIT,
                          'selection': 'first 20 frozen baseline IDs; heldout excluded'},
                'baseline': baseline, 'instructions': source, 'model': metadata,
                'arms': {name: {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'low',
                                'max_attempts': 1, 'suffix': instructions, 'prompt_revision': source['version']}},
                'config': {'model': model, 'base_url': base_url.rstrip('/'), 'timeout': timeout,
                           'prepare_only': prepare_only, 'maximum_calls_per_case': 1, 'resume': False},
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
                    current = baseline_snapshot(baseline_manifest, prepare_only=False, dataset=dataset, model=model)
                    if current['sha256'] != baseline['sha256']:
                        raise ValueError('Baseline manifest changed during generation')
                    if hashlib.sha256(instructions_file.read_bytes()).hexdigest() != source['byte_sha256']:
                        raise ValueError('Instructions file changed during generation')
                    record = generate_candidate(item, instructions, name, model, client,
                                                dataset_sha256=dataset['sha256'], block_on_transport_failure=not records)
                    record.update(model_digest=metadata.get('digest'), instructions_byte_sha256=source['byte_sha256'],
                                  instructions_name=name, instructions_version=source['version'])
                    records.append(record)
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                    stream.flush()
                    if record['status'] == 'blocked':
                        status, code = 'blocked', 2
                        break
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
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--timeout', type=float, default=300)
    args = parser.parse_args(argv)
    try:
        summary, code = run_cycle(args.data, args.baseline_manifest, args.instructions_file, args.out,
                                  name=args.candidate_name, version=args.instructions_version,
                                  prepare_only=args.prepare_only, base_url=args.base_url, model=args.model, timeout=args.timeout)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(f"Codex-guided regeneration {summary['status']}: {summary['written_answers']}/"
          f"{summary['planned_answers']} development answers; {summary['calls']} calls; independent Codex review required")
    return code


if __name__ == '__main__':
    sys.exit(main())
