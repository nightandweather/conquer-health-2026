"""Private loopback viewer for streamed HealthBench answer comparisons."""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import math
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VARIANTS = ('raw_default', 'lunit_call', 'grounded_call')


def read_stream(response, on_content):
    """Keep only final-answer content, usage and finish reason from SSE."""
    content, usage, finish = [], {}, None
    for line in response:
        if len(line) > 1_000_000:
            raise ValueError('oversized_stream_event')
        line = line.decode('utf-8').strip()
        if not line.startswith('data:'):
            continue
        value = line[5:].strip()
        if value == '[DONE]':
            break
        event = json.loads(value)
        if not isinstance(event, dict) or event.get('error'):
            raise ValueError('invalid_stream_event')
        if isinstance(event.get('usage'), dict):
            usage = event['usage']
        choices = event.get('choices', [])
        if not isinstance(choices, list):
            raise ValueError('invalid_stream_choices')
        for choice in choices:
            if not isinstance(choice, dict):
                raise ValueError('invalid_stream_choice')
            if choice.get('index', 0) != 0:
                continue
            delta = choice.get('delta', {})
            if not isinstance(delta, dict):
                raise ValueError('invalid_stream_delta')
            text = delta.get('content')
            if isinstance(text, str) and text:
                content.append(text)
                if sum(map(len, content)) > 500_000:
                    raise ValueError('oversized_answer')
                on_content(text)
            if choice.get('finish_reason') is not None:
                finish = choice['finish_reason']
    if finish is None:
        raise ValueError('missing_stream_finish_reason')
    return {'choices': [{'message': {'content': ''.join(content)}, 'finish_reason': finish}],
            'usage': usage}


def visible_record(row, run_name):
    return {key: row.get(key) for key in
            ('answer', 'status', 'finish_reason', 'seconds', 'fallback_used', 'usage')} | {
                'calls': len(row.get('attempts', [])), 'run': run_name}


def visible_assessment(value, row):
    """Display only a review explicitly tied to these saved answer bytes."""
    hb = importlib.import_module('benchmarks.healthbench')
    if (not isinstance(value, dict)
            or value.get('answer_sha256') != hb.canonical_hash(row.get('answer', ''))
            or value.get('status') not in {'reviewed', 'pending', 'uncertain', 'needs_review'}):
        return None
    score = value.get('score')
    if score is not None and (isinstance(score, bool) or not isinstance(score, (int, float))
                              or not math.isfinite(score) or not 0 <= score <= 100):
        return None
    if value['status'] != 'reviewed' and score is not None:
        return None
    note = value.get('note', '')
    return {'status': value['status'], 'score': score,
            'note': note[:500] if isinstance(note, str) else ''}


class ComparisonStore:
    def __init__(self, data: Path, limit: int = 3, comparison_dir: Path | None = None):
        hb = importlib.import_module('benchmarks.healthbench')
        self.items, metadata = hb.load_dataset(data, limit)
        self.data, self.limit, self.dataset_hash = data, limit, metadata['sha256']
        self.prompt_hashes = metadata['selected_prompt_sha256']
        self.comparison_dir, self.comparison_manifest = None, None
        self.variants = VARIANTS
        self.configs = hb.arm_config(VARIANTS)
        if comparison_dir is not None:
            path = comparison_dir.resolve()
            if not path.is_relative_to((ROOT / 'results').resolve()):
                raise ValueError('Comparison directory must be inside this repository results directory')
            manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
            if not isinstance(manifest, dict):
                raise ValueError('Comparison manifest requires an object')
            variants, ids = manifest.get('selected_arms'), manifest.get('selected_prompt_ids')
            dataset = manifest.get('dataset', {})
            configs = manifest.get('arms', {})
            if (not isinstance(variants, list) or not 2 <= len(variants) <= 6
                    or any(not isinstance(arm, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,80}', arm) is None
                           for arm in variants) or len(set(variants)) != len(variants)
                    or not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)
                    or len(set(ids)) != len(ids) or not set(ids).issubset(self.prompt_hashes)
                    or not isinstance(dataset, dict) or dataset.get('sha256') != self.dataset_hash
                    or not isinstance(dataset.get('selected_prompt_sha256'), dict)
                    or any(dataset['selected_prompt_sha256'].get(key) != self.prompt_hashes[key] for key in ids)
                    or not isinstance(configs, dict) or any(not isinstance(configs.get(arm), dict) for arm in variants)):
                raise ValueError('Comparison manifest arms or prompt identities do not match this dataset selection')
            self.items = [item for key in ids for item in self.items if item['prompt_id'] == key]
            self.comparison_dir, self.comparison_manifest = path, manifest
            self.variants, self.configs = tuple(variants), configs
        self.lock = threading.RLock()
        self.busy, self.active, self.run_name, self.error = False, None, None, None
        self.live = {}
        self.completed, self.planned = 0, 0
        self.worker = None
        if self.comparison_dir is not None:
            self.run_name = self.comparison_dir.relative_to((ROOT / 'results').resolve()).as_posix()
            self.planned = len(self.items) * len(self.variants)

    def display_configs(self):
        """Expose inference settings, never instruction text or private paths."""
        result = {}
        for arm in self.variants:
            result[arm] = {}
            for key in ('max_tokens', 'temperature', 'reasoning_effort', 'max_attempts', 'prompt_revision'):
                value = self.configs[arm].get(key)
                if isinstance(value, str):
                    result[arm][key] = value[:120]
                elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    result[arm][key] = value
        return result

    def source_run(self, key):
        """Return a repository-relative source name without disclosing file paths."""
        if self.comparison_manifest is None:
            return None
        sources = self.comparison_manifest.get('sources', {})
        for value in self.comparison_manifest.get('row_provenance', []):
            if isinstance(value, dict) and (value.get('prompt_id'), value.get('arm')) == key:
                source = sources.get(value.get('source'), {})
                path = source.get('answers_path') if isinstance(source, dict) else None
                if isinstance(path, str):
                    parent = Path(path).parent.resolve()
                    try:
                        return parent.relative_to((ROOT / 'results').resolve()).as_posix()
                    except ValueError:
                        return None
        return None

    def saved_records(self):
        records = {}
        allowed = {item['prompt_id'] for item in self.items}
        candidates = ([self.comparison_dir / 'answers.jsonl'] if self.comparison_dir is not None
                      else sorted((ROOT / 'results').glob('healthbench-*/answers.jsonl'),
                                  key=lambda p: p.stat().st_mtime))
        for path in candidates:
            if not path.exists():
                continue
            review_path = path.parent / 'assessments.json'
            reviews = {}
            if review_path.exists() and review_path.stat().st_size <= 1_000_000:
                try:
                    report = json.loads(review_path.read_text(encoding='utf-8'))
                    for review in report.get('answers', []):
                        if isinstance(review, dict):
                            reviews[(review.get('prompt_id'), review.get('arm'))] = review
                except (ValueError, AttributeError, TypeError):
                    pass
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue  # A writer may currently be flushing its last line.
                if not isinstance(row, dict):
                    continue
                dataset_match = row.get('dataset_sha256') == self.dataset_hash
                prompt_match = row.get('prompt_sha256') == self.prompt_hashes.get(row.get('prompt_id'))
                identity_match = (dataset_match and prompt_match if self.comparison_dir is not None
                                  else dataset_match or prompt_match)
                if (identity_match and row.get('prompt_id') in allowed and row.get('arm') in self.variants):
                    key = (row['prompt_id'], row['arm'])
                    run = self.run_name if self.comparison_dir is not None else path.parent.name
                    records[key] = visible_record(row, run)
                    source = self.source_run(key)
                    if source:
                        records[key]['source_run'] = source
                    assessment = visible_assessment(reviews.get(key), row)
                    if assessment is not None:
                        records[key]['assessment'] = assessment
        return records

    def snapshot(self):
        saved = self.saved_records()
        with self.lock:
            written = sum(value.get('run') == self.run_name for value in saved.values())
            cases = []
            for item in self.items:
                answers = {}
                for arm in self.variants:
                    key = (item['prompt_id'], arm)
                    value = deepcopy(saved.get(key, {'answer': '', 'status': 'not_run'}))
                    live = self.live.get(key)
                    if live and (not saved.get(key) or saved[key]['run'] != self.run_name):
                        value = deepcopy(live)
                    answers[arm] = value
                cases.append({'id': item['prompt_id'], 'messages': deepcopy(item['prompt']),
                              'answers': answers})
            result = {'model': 'gpt-oss:20b', 'cases': cases, 'busy': self.busy,
                    'active': deepcopy(self.active), 'run': self.run_name,
                    'error': self.error, 'completed': written if self.busy or self.comparison_dir is not None else self.completed,
                    'planned': self.planned, 'arms': list(self.variants),
                    'arm_config': self.display_configs(), 'read_only': self.comparison_dir is not None}
            if self.comparison_manifest is not None:
                sources = self.comparison_manifest.get('sources', {})
                result['run_summary'] = {
                    'status': self.comparison_manifest.get('status'),
                    'answer_status_counts': dict(Counter(row.get('status', 'unknown') for row in saved.values())),
                    'source_statuses': {label: value.get('original_manifest_status')
                                        for label, value in sources.items()
                                        if label in {'baseline', 'candidate'} and isinstance(value, dict)}}
            return result

    def start(self, arms):
        if self.comparison_dir is not None:
            raise RuntimeError('이 화면은 저장된 개선 실험의 읽기 전용 비교입니다.')
        if not isinstance(arms, list) or not arms or len(set(arms)) != len(arms):
            raise ValueError('설정을 하나 이상 선택하세요.')
        if any(arm not in VARIANTS for arm in arms):
            raise ValueError('지원하지 않는 설정입니다.')
        hb = importlib.reload(importlib.import_module('benchmarks.healthbench'))
        if any(arm not in hb.arm_config(tuple(arms)) for arm in arms):
            raise ValueError('설정이 아직 준비되지 않았습니다.')
        with self.lock:
            if self.busy:
                raise RuntimeError('이미 비교를 실행하고 있습니다.')
            self.busy, self.error, self.active = True, None, None
            self.completed, self.planned = 0, len(self.items) * len(arms)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
            self.run_name = 'healthbench-live-' + stamp + '-' + secrets.token_hex(3)
            self.live = {(item['prompt_id'], arm): {'answer': '', 'status': 'waiting',
                          'run': self.run_name} for item in self.items for arm in arms}
            self.worker = threading.Thread(target=self._run, args=(hb, tuple(arms)), daemon=True)
            self.worker.start()
        return self.run_name

    def _run(self, hb, arms):
        try:
            summary = self._run_impl(hb, arms)
            with self.lock:
                self.completed = summary['written_answers']
                if summary['status'] != 'completed':
                    self.error = '이번 실행 상태: ' + summary['status']
        except Exception:
            with self.lock:
                self.error = '실행을 완료하지 못했습니다. 로컬 모델 연결과 결과 기록을 확인하세요.'
        finally:
            with self.lock:
                self.busy, self.active = False, None
                self.live = {}

    def _run_impl(self, hb, arms):
        store = self
        request_map = {}
        for item in self.items:
            for arm in arms:
                for fallback in ((False, True) if arm == 'lunit_call' else (False,)):
                    payload = hb.build_payload(item, arm, fallback=fallback)
                    request_map[hb.canonical_hash(payload)] = (item['prompt_id'], arm, fallback)

        class StreamTransport(hb.Transport):
            def complete(self, payload):
                prompt_id, arm, fallback = request_map[hb.canonical_hash(payload)]
                key = (prompt_id, arm)
                with store.lock:
                    store.active = {'id': prompt_id, 'arm': arm, 'fallback': fallback,
                                    'started': time.time()}
                    store.live[key] = {'answer': '', 'status': 'generating',
                                       'run': store.run_name, 'fallback_used': fallback}
                body = payload | {'stream': True, 'stream_options': {'include_usage': True}}
                wire_record = {'prompt_id': prompt_id, 'arm': arm, 'fallback': fallback,
                               'logical_request_sha256': hb.canonical_hash(payload),
                               'wire_request_sha256': hb.canonical_hash(body),
                               'stream': True, 'stream_options': {'include_usage': True}}
                wire_path = ROOT / 'results' / store.run_name / 'wire_requests.jsonl'
                with wire_path.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(wire_record) + '\n')
                wire_path.chmod(0o600)
                request = urllib.request.Request(self.base_url + '/chat/completions',
                    data=json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json'})

                def on_content(text):
                    with store.lock:
                        store.live[key]['answer'] += text
                try:
                    with self.opener.open(request, timeout=self.timeout) as response:
                        result = read_stream(response, on_content)
                except urllib.error.HTTPError as exc:
                    raise hb.RequestFailure('http_error', exc.code) from None
                except (ValueError, UnicodeError):
                    raise hb.RequestFailure('invalid_stream_response') from None
                except (urllib.error.URLError, OSError, TimeoutError, hb.HTTPException):
                    raise hb.RequestFailure('connection_error') from None
                with store.lock:
                    store.live[key]['status'] = 'received'
                return result

        transport = StreamTransport(hb.DEFAULT_BASE_URL, timeout=300)
        summary, _ = hb.run_experiment(self.data, ROOT / 'results' / self.run_name,
            limit=self.limit, arms=arms, transport=transport)
        manifest_path = ROOT / 'results' / self.run_name / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['transport'] = {'stream': True, 'stream_options': {'include_usage': True},
                                 'wire_request_hashes': 'wire_requests.jsonl',
                                 'attempt_request_hashes': 'logical generation payload without stream options'}
        hb.write_json(manifest_path, manifest)
        return summary


def make_handler(store, token, host):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never write prompts, answers or capability tokens to server logs.

        def allowed(self, api=False):
            if self.headers.get('Host') != host:
                return False
            origin = self.headers.get('Origin')
            if origin is not None and origin != 'http://' + host:
                return False
            return not api or secrets.compare_digest(self.headers.get('X-Local-Token', ''), token)

        def send(self, code, data, content_type='application/json; charset=utf-8'):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def reply(self, code, value):
            self.send(code, json.dumps(value, ensure_ascii=False).encode('utf-8'))

        def do_GET(self):
            if not self.allowed(api=self.path.startswith('/api/')):
                self.reply(403, {'error': '접근할 수 없습니다.'})
            elif self.path == '/':
                html = (Path(__file__).with_name('dialogue.html')).read_text()
                html = html.replace('__LOCAL_TOKEN__', token)
                self.send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
            elif self.path == '/api/state':
                self.reply(200, store.snapshot())
            elif self.path == '/favicon.ico':
                self.send(204, b'')
            else:
                self.reply(404, {'error': '찾을 수 없습니다.'})

        def do_POST(self):
            if not self.allowed(api=True):
                self.reply(403, {'error': '접근할 수 없습니다.'})
                return
            if self.path != '/api/run':
                self.reply(404, {'error': '찾을 수 없습니다.'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('잘못된 요청 크기입니다.')
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON 요청이 필요합니다.')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or set(body) != {'arms'}:
                    raise ValueError('잘못된 비교 요청입니다.')
                run = store.start(body['arms'])
                self.reply(202, {'run': run})
            except RuntimeError as exc:
                self.reply(409, {'error': str(exc)})
            except (ValueError, TypeError):
                self.reply(400, {'error': '설정 또는 요청 형식을 확인하세요.'})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=ROOT / 'private_data/healthbench/legacy_consensus_val.json')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--limit', type=int, default=3)
    parser.add_argument('--start-arms', help='Optional explicit comparison to start after serving')
    parser.add_argument('--comparison-dir', type=Path, help='Read-only saved comparison inside results; retain its actual arm names')
    args = parser.parse_args()
    if not 1 <= args.limit <= 200 or not 1024 <= args.port <= 65535:
        parser.error('Use 1–200 prompts and a port between 1024 and 65535')
    if args.comparison_dir is not None and args.start_arms:
        parser.error('--comparison-dir is read-only and cannot be combined with --start-arms')
    store = ComparisonStore(args.data, args.limit, args.comparison_dir)
    host = f'127.0.0.1:{args.port}'
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store, secrets.token_urlsafe(32), host))
    if args.start_arms:
        store.start(args.start_arms.split(','))
    print('HealthBench comparison viewer: http://' + host, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
