"""Generate HealthBench candidate answers with a local OpenAI-compatible model.

This is one free-response call per prompt/arm, not the NOVA consultation agent.
Rubrics are validated as dataset fields but never sent to the generation model.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = 'http://127.0.0.1:11434/v1'
DEFAULT_MODEL = 'gpt-oss:20b'
ARMS = ('raw_default', 'lunit_call')
SUFFIX = ("Do not substitute 'consult a professional' for an answer; answer as far as you can. "
          "Address every distinct thing the user asked about, and state the warning signs that would "
          "require prompt medical attention. If the user offers information you would need — lab or "
          "imaging results, a medication list, measurements — ask them for it. If one decisive detail "
          "is missing and your answer would change because of it, ask for that one thing, while still "
          "answering as far as you can without it.")
FALLBACK_NOTE = ('Named high/low levels adapt L2 on/off; low is reduced effort, '
                 'not equivalent to disabling reasoning.')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def dataset_provenance(path: Path, sha256: str, count: int) -> dict:
    sidecar = path.with_suffix('.manifest.json')
    if not sidecar.exists():
        return {'status': 'no_sidecar', 'dataset_kind': 'unclassified_healthbench_input'}
    raw = sidecar.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Dataset provenance sidecar is not valid JSON') from exc
    if not isinstance(value, dict) or value.get('converted_sha256') != sha256:
        raise ValueError('Dataset provenance converted_sha256 does not match the input file')
    if not isinstance(value.get('dataset_kind'), str) or not value['dataset_kind'].strip():
        raise ValueError('Dataset provenance requires dataset_kind')
    for field in ('normalized_context', 'lost_original_prompt_ids'):
        if not isinstance(value.get(field), bool):
            raise ValueError(f'Dataset provenance requires boolean {field}')
    if 'canonical_score_comparable' in value and not isinstance(value['canonical_score_comparable'], bool):
        raise ValueError('Dataset provenance canonical_score_comparable must be boolean')
    if 'count' in value and value['count'] != count:
        raise ValueError('Dataset provenance count does not match the input file')
    fields = ('schema_version', 'dataset_kind', 'normalized_context', 'lost_original_prompt_ids',
              'canonical_score_comparable', 'context_adapter', 'source_path', 'source_copy',
              'source_sha256', 'converted_sha256', 'ids_sha256', 'license_file', 'license_sha256',
              'count', 'rubric_count', 'history_count')
    provenance = {field: value[field] for field in fields if field in value}
    if any(not isinstance(value, (str, bool, int, float, type(None))) for value in provenance.values()):
        raise ValueError('Dataset provenance fields must be scalar metadata, not source records')
    provenance.update(status='validated', sidecar=str(sidecar.resolve()),
                      sidecar_sha256=hashlib.sha256(raw).hexdigest())
    return provenance


def load_dataset(path: Path, limit: int = 3) -> tuple[list[dict], dict]:
    if limit < 0:
        raise ValueError('--limit must be nonnegative (0 means the full dataset)')
    raw = path.read_bytes()
    try:
        content = json.loads(raw)
    except (ValueError, UnicodeError):
        try:
            content = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        except (ValueError, UnicodeError) as exc:
            raise ValueError('Dataset must be a JSON array or JSONL records') from exc
    if isinstance(content, dict) and isinstance(content.get('data'), list):
        content = content['data']
    if not isinstance(content, list) or not content:
        raise ValueError('Dataset must contain a nonempty list of HealthBench records')
    seen = set()
    for number, item in enumerate(content, 1):
        if not isinstance(item, dict):
            raise ValueError(f'Dataset record {number} is not an object')
        prompt_id, prompt, rubrics = item.get('prompt_id'), item.get('prompt'), item.get('rubrics')
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError(f'Dataset record {number} requires a nonempty string prompt_id')
        if prompt_id in seen:
            raise ValueError('Dataset contains duplicate prompt_id values')
        seen.add(prompt_id)
        if not isinstance(prompt, list) or not prompt:
            raise ValueError(f'Dataset record {number} requires prompt messages')
        for message in prompt:
            if (not isinstance(message, dict) or set(message) != {'role', 'content'}
                    or message.get('role') not in {'system', 'user', 'assistant'}
                    or not isinstance(message.get('content'), str) or not message['content'].strip()):
                raise ValueError(f'Dataset record {number} has unsupported prompt message fields')
        if prompt[-1]['role'] != 'user':
            raise ValueError(f'Dataset record {number} must end in a user message')
        if not isinstance(rubrics, list):
            raise ValueError(f'Dataset record {number} requires a rubrics list')
    ordered = sorted(content, key=lambda item: item['prompt_id'])
    selected = ordered[:limit] if limit else ordered
    metadata = {'path': str(path.resolve()), 'sha256': hashlib.sha256(raw).hexdigest(),
                'total_records': len(content), 'selection': 'prompt_id sorted; first limit records; 0 means all',
                'selected_prompt_ids': [item['prompt_id'] for item in selected],
                'selected_prompt_sha256': {item['prompt_id']: canonical_hash(item['prompt']) for item in selected}}
    metadata['provenance'] = dataset_provenance(path, metadata['sha256'], len(content))
    return selected, metadata


def build_payload(item: dict, arm: str, model: str = DEFAULT_MODEL, *, fallback: bool = False) -> dict:
    if arm not in ARMS or (fallback and arm != 'lunit_call'):
        raise ValueError('Unsupported generation arm or fallback')
    messages = deepcopy(item['prompt'])
    body = {'model': model, 'messages': messages, 'max_tokens': 2048}
    if arm == 'lunit_call':
        messages[-1]['content'] += '\n\n[' + SUFFIX + ']'
        body.update(temperature=0, max_tokens=6144, reasoning_effort='low' if fallback else 'high')
    return body


class RequestFailure(RuntimeError):
    """A sanitized transport failure; provider text and credentials are not retained."""

    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.code, self.http_status = code, http_status


class Transport:
    def __init__(self, base_url: str, api_key: str = '', timeout: float = 300):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('--base-url must be an HTTP(S) URL without embedded credentials, query or fragment')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('--timeout must be positive')
        self.base_url, self.api_key, self.timeout = base_url.rstrip('/'), api_key, timeout
        self.origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
        self.local = parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
        # Neither ambient proxy configuration nor redirect targets receive the key.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method: str, url: str, body=None, *, authenticated=True) -> dict:
        headers = {'Content-Type': 'application/json'}
        if authenticated and self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        request = urllib.request.Request(url, method=method, headers=headers,
                                         data=json.dumps(body, ensure_ascii=False).encode('utf-8')
                                         if body is not None else None)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(5_000_001)
        except urllib.error.HTTPError as exc:
            raise RequestFailure('http_error', exc.code) from None
        except (urllib.error.URLError, OSError, TimeoutError, HTTPException):
            raise RequestFailure('connection_error') from None
        if len(raw) > 5_000_000:
            raise RequestFailure('response_too_large')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RequestFailure('invalid_json_response') from None
        if not isinstance(value, dict) or value.get('error'):
            raise RequestFailure('invalid_provider_response')
        return value

    def complete(self, payload: dict) -> dict:
        return self.request('POST', self.base_url + '/chat/completions', payload)

    def model_metadata(self, model: str) -> dict:
        metadata = {'name': model, 'digest': None, 'metadata_status': 'unavailable'}
        if not self.local:
            return metadata
        try:
            response = self.request('GET', self.origin + '/api/tags', authenticated=False)
        except RequestFailure:
            return metadata
        models = response.get('models')
        if not isinstance(models, list):
            return metadata
        for item in models:
            if isinstance(item, dict) and model in {item.get('name'), item.get('model')}:
                metadata.update(digest=item.get('digest'), details=item.get('details'),
                                metadata_status='available')
                break
        return metadata


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def usage_values(response: dict) -> dict:
    usage = response.get('usage') if isinstance(response.get('usage'), dict) else {}
    result = {key: value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
              for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
              for value in [usage.get(key)]}
    if result['total_tokens'] is None and all(result[key] is not None for key in ('prompt_tokens', 'completion_tokens')):
        result['total_tokens'] = result['prompt_tokens'] + result['completion_tokens']
    return result


def sum_usage(attempts: list[dict]) -> dict:
    result = {}
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        values = [attempt['usage'][key] for attempt in attempts]
        result[key] = sum(values) if all(value is not None for value in values) else None
    return result


def generate(item: dict, arm: str, model: str, transport, *, dataset_sha256: str,
             block_on_transport_failure: bool = False) -> dict:
    attempts = []
    for fallback in (False, True) if arm == 'lunit_call' else (False,):
        body = build_payload(item, arm, model, fallback=fallback)
        started = time.monotonic()
        attempt = {'number': len(attempts) + 1, 'max_tokens': body['max_tokens'],
                   'temperature': body.get('temperature', 'server_default'),
                   'reasoning_effort': body.get('reasoning_effort', 'server_default'),
                   'request_sha256': canonical_hash(body), 'answer': '', 'finish_reason': None,
                   'usage': {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None},
                   'status': 'failed', 'error': None, 'response_received': False}
        try:
            response = transport.complete(body)
            choices = response.get('choices')
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
                attempt.update(status='failed', error={'code': 'unexpected_finish_reason', 'http_status': None})
            elif not attempt['answer'].strip():
                attempt.update(status='failed', error={'code': 'empty_answer', 'http_status': None})
            elif attempt['finish_reason'] == 'length':
                attempt['status'] = 'truncated'
            else:
                attempt['status'] = 'completed'
        except RequestFailure as exc:
            attempt.update(status='blocked' if block_on_transport_failure and not attempts else 'failed',
                           error={'code': exc.code, 'http_status': exc.http_status})
        attempt['seconds'] = round(time.monotonic() - started, 3)
        attempts.append(attempt)
        if (arm != 'lunit_call' or fallback or not attempt['response_received']
                or (attempt['answer'].strip() and attempt['finish_reason'] != 'length')):
            break
    chosen = attempts[-1]
    # If a low-effort retry fails, retain a usable high-effort truncated answer rather than discarding it.
    if not chosen['answer'].strip() and (not chosen['error'] or chosen['error']['code'] != 'unexpected_finish_reason'):
        chosen = next((attempt for attempt in reversed(attempts) if attempt['answer'].strip()), chosen)
    return {'prompt_id': item['prompt_id'], 'arm': arm, 'model': model,
            'dataset_sha256': dataset_sha256, 'prompt_sha256': canonical_hash(item['prompt']),
            'answer': chosen['answer'], 'status': chosen['status'], 'finish_reason': chosen['finish_reason'],
            'error': chosen['error'], 'attempts': attempts, 'usage': sum_usage(attempts),
            'seconds': round(sum(attempt['seconds'] for attempt in attempts), 3),
            'fallback_used': len(attempts) > 1, 'selected_attempt': chosen['number'],
            'score': None, 'score_status': 'not_scored'}


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    path.chmod(0o600)


def arm_config() -> dict:
    return {'raw_default': {'max_tokens': 2048, 'temperature': 'omitted; server default unknown',
                            'reasoning_effort': 'omitted; server default unknown', 'max_attempts': 1},
            'lunit_call': {'max_tokens': 6144, 'temperature': 0, 'reasoning_effort': 'high',
                           'suffix': SUFFIX, 'max_attempts': 2, 'fallback_reasoning_effort': 'low',
                           'fallback_condition': 'empty answer or finish_reason=length',
                           'fallback_note': FALLBACK_NOTE}}


def run_experiment(data: Path, out: Path, *, limit: int = 3, base_url: str = DEFAULT_BASE_URL,
                   model: str = DEFAULT_MODEL, timeout: float = 300, dry_run: bool = False,
                   transport=None) -> tuple[dict, int]:
    if not isinstance(model, str) or not model.strip():
        raise ValueError('--model must be a nonempty model name')
    selected, dataset = load_dataset(data, limit)
    if any((out / name).exists() for name in ('manifest.json', 'answers.jsonl', 'summary.json')):
        raise ValueError('Output already contains experiment artifacts; choose a new --out directory')
    client = transport if transport is not None else Transport(base_url, os.environ.get('HEALTHBENCH_API_KEY', ''), timeout)
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    started_at = utc_now()
    order = [{'prompt_id': item['prompt_id'], 'arms': list(ARMS if index % 2 == 0 else reversed(ARMS))}
             for index, item in enumerate(selected)]
    # Dry runs intentionally do not inspect the model server or generate an answers file.
    metadata = {'name': model, 'digest': None, 'metadata_status': 'not_requested'}
    manifest = {'schema_version': 1, 'started_at': started_at, 'status': 'dry_run' if dry_run else 'running',
                'benchmark': 'HealthBench', 'experiment': 'single free-response generation; not NOVA consultation agent',
                'dataset': dataset, 'selected_prompt_ids': dataset['selected_prompt_ids'],
                'config': {'data': str(data.resolve()), 'out': str(out.resolve()), 'limit': limit,
                           'base_url': base_url.rstrip('/'), 'model': model, 'timeout': timeout, 'dry_run': dry_run,
                           'api_key_env': 'HEALTHBENCH_API_KEY'},
                'model': metadata, 'arms': arm_config(), 'execution_order': order,
                'score': None, 'score_status': 'not_scored'}
    # Validate every planned request even when no transport calls are requested.
    for item in selected:
        for arm in ARMS:
            build_payload(item, arm, model)
    write_json(out / 'manifest.json', manifest)
    if not dry_run:
        metadata = client.model_metadata(model)
        manifest['model'] = metadata
        write_json(out / 'manifest.json', manifest)
    records = []
    status, exit_code = ('dry_run', 0) if dry_run else ('completed', 0)
    started = time.monotonic()
    if not dry_run:
        answer_path = out / 'answers.jsonl'
        descriptor = os.open(answer_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                for item, plan in zip(selected, order):
                    for arm in plan['arms']:
                        record = generate(item, arm, model, client, dataset_sha256=dataset['sha256'],
                                          block_on_transport_failure=not records)
                        record['model_digest'] = metadata.get('digest')
                        records.append(record)
                        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                        stream.flush()
                        if record['status'] == 'blocked':
                            status, exit_code = 'blocked', 2
                            break
                    if status == 'blocked':
                        break
        except KeyboardInterrupt:
            status, exit_code = 'interrupted', 130
        if status == 'completed' and any(record['status'] == 'failed' for record in records):
            status, exit_code = 'partial', 1
    summary = {'status': status, 'started_at': started_at, 'finished_at': utc_now(),
               'seconds': round(time.monotonic() - started, 3), 'selected_prompt_ids': dataset['selected_prompt_ids'],
               'planned_answers': len(selected) * len(ARMS), 'written_answers': len(records),
               'unattempted_answers': len(selected) * len(ARMS) - len(records),
               'answer_status_counts': dict(Counter(record['status'] for record in records)),
               'attempt_status_counts': dict(Counter(attempt['status'] for record in records for attempt in record['attempts'])),
               'calls': sum(len(record['attempts']) for record in records),
               'usage': sum_usage([attempt for record in records for attempt in record['attempts']]),
               'score': None, 'score_status': 'not_scored'}
    manifest.update(status=status, finished_at=summary['finished_at'])
    write_json(out / 'manifest.json', manifest)
    write_json(out / 'summary.json', summary)
    return summary, exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=3, help='First N sorted prompt IDs; 0 means all')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL, help='Explicit OpenAI-compatible endpoint')
    parser.add_argument('--model', default=DEFAULT_MODEL)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--dry-run', action='store_true', help='Validate inputs/configuration without network access')
    args = parser.parse_args(argv)
    try:
        args.out.resolve().relative_to((ROOT / 'results').resolve())
    except ValueError:
        parser.error('--out must be inside this repository\'s private results/ directory')
    try:
        summary, code = run_experiment(args.data, args.out, limit=args.limit, base_url=args.base_url,
                                       model=args.model, timeout=args.timeout, dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"HealthBench {summary['status']}: {summary['written_answers']}/{summary['planned_answers']} answers; "
          f"{summary['calls']} calls; output {args.out}")
    return code


if __name__ == '__main__':
    sys.exit(main())
