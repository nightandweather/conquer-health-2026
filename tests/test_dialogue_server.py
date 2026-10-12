"""Synthetic checks for private display and streamed answer reconstruction."""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from local_tools.dialogue_server import ComparisonStore, make_handler, read_stream, visible_record
from benchmarks.healthbench import canonical_hash, load_dataset


def events(*values):
    return BytesIO(b''.join(('data: ' + json.dumps(value) + '\n\n').encode()
                           for value in values) + b'data: [DONE]\n\n')


class DialogueTests(unittest.TestCase):
    def test_stream_keeps_answer_and_usage_without_reasoning(self):
        chunks = []
        response = read_stream(events(
            {'choices': [{'delta': {'reasoning': 'private reasoning', 'content': 'Hello'}}]},
            {'choices': [{'delta': {'content': ' world'}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
        ), chunks.append)
        self.assertEqual(chunks, ['Hello', ' world'])
        self.assertEqual(response['choices'][0]['message']['content'], 'Hello world')
        self.assertEqual(response['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(response['usage']['total_tokens'], 7)
        self.assertNotIn('reasoning', json.dumps(response))

    def test_unfinished_or_error_stream_is_not_completed(self):
        with self.assertRaises(ValueError):
            read_stream(events({'choices': [{'delta': {'content': 'partial'}}]}), lambda _: None)
        with self.assertRaises(ValueError):
            read_stream(events({'error': 'provider error'}), lambda _: None)
        for value in ({'choices': [None]}, {'choices': 'invalid'}, {'choices': [{'delta': None}]}):
            with self.assertRaises(ValueError):
                read_stream(events(value), lambda _: None)

    def test_preparation_failure_releases_busy_without_error_details(self):
        store = ComparisonStore.__new__(ComparisonStore)
        store.lock = threading.RLock()
        store.busy, store.active, store.live, store.error = True, {'arm': 'synthetic'}, {}, None
        with patch.object(store, '_run_impl', side_effect=ValueError('private error payload')):
            store._run(None, ('synthetic',))
        self.assertFalse(store.busy)
        self.assertIsNone(store.active)
        self.assertTrue(store.error)
        self.assertNotIn('private error payload', store.error)

    def test_visible_record_excludes_source_fields_and_attempt_contents(self):
        row = {'answer': 'synthetic answer', 'status': 'completed', 'canary': 'hidden',
               'rubrics': ['hidden'], 'private_path': 'hidden', 'api_key': 'hidden',
               'attempts': [{'answer': 'old answer', 'reasoning': 'hidden'}]}
        value = visible_record(row, 'synthetic-run')
        self.assertEqual(value['answer'], 'synthetic answer')
        self.assertEqual(value['calls'], 1)
        self.assertNotIn('hidden', json.dumps(value))
        self.assertNotIn('old answer', json.dumps(value))

    def test_http_requires_capability_host_and_same_origin(self):
        class Store:
            def snapshot(self):
                return {'cases': [], 'busy': False}
            def start(self, arms):
                return 'synthetic-run'
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(Store(), 'synthetic-token', 'temporary'))
        host = '127.0.0.1:' + str(server.server_port)
        server.RequestHandlerClass = make_handler(Store(), 'synthetic-token', host)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'http://' + host
        def request(path, headers=None, body=None):
            return urllib.request.urlopen(urllib.request.Request(url + path,
                headers=headers or {}, data=body), timeout=3)
        try:
            with self.assertRaises(urllib.error.HTTPError) as blocked:
                request('/api/state')
            self.assertEqual(blocked.exception.code, 403)
            with request('/api/state', {'X-Local-Token': 'synthetic-token'}) as response:
                self.assertEqual(json.load(response)['cases'], [])
            for headers in ({'Host': 'evil.example', 'X-Local-Token': 'synthetic-token'},
                            {'Origin': 'https://evil.example', 'X-Local-Token': 'synthetic-token'}):
                with self.assertRaises(urllib.error.HTTPError) as blocked:
                    request('/api/state', headers)
                self.assertEqual(blocked.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as missing:
                request('/private_data/secret.json')
            self.assertEqual(missing.exception.code, 404)
            with request('/api/run', {'X-Local-Token': 'synthetic-token', 'Content-Type': 'application/json'},
                         json.dumps({'arms': ['grounded_call']}).encode()) as response:
                self.assertEqual(response.status, 202)
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                request('/api/run', {'X-Local-Token': 'synthetic-token', 'Content-Type': 'application/json'},
                        json.dumps({'arms': ['grounded_call'], 'data': 'private file'}).encode())
            self.assertEqual(invalid.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


class SavedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'synthetic.json'
        self.item = {'prompt_id': 'SYNTHETIC-001', 'prompt': [{'role': 'user', 'content': 'Synthetic question.'}],
                     'rubrics': []}
        self.data.write_text(json.dumps([self.item]))
        _, metadata = load_dataset(self.data, 1)
        self.comparison = self.root / 'results' / 'synthetic-loop' / 'round-01' / 'comparison'
        self.comparison.mkdir(parents=True)
        self.manifest = {'status': 'snapshot', 'dataset': metadata,
            'selected_prompt_ids': ['SYNTHETIC-001'], 'selected_arms': ['grounded_call', 'codex_guided_v3'],
            'arms': {'grounded_call': {'max_tokens': 2048, 'reasoning_effort': 'low'},
                     'codex_guided_v3': {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'low',
                                         'prompt_revision': 'synthetic-v3', 'suffix': 'hidden instruction',
                                         'private_path': 'hidden private path'}},
            'sources': {'baseline': {'original_manifest_status': 'partial', 'manifest_path': 'hidden path',
                                     'answers_path': str(self.root / 'results' / 'healthbench-baseline' / 'answers.jsonl')},
                        'candidate': {'original_manifest_status': 'completed',
                                      'answers_path': str(self.comparison.parent / 'candidate' / 'answers.jsonl')}},
            'row_provenance': [{'prompt_id': self.item['prompt_id'], 'arm': 'grounded_call', 'source': 'baseline'},
                               {'prompt_id': self.item['prompt_id'], 'arm': 'codex_guided_v3', 'source': 'candidate'}]}
        self.rows = [{'prompt_id': self.item['prompt_id'], 'arm': arm, 'answer': f'Synthetic {arm} answer.',
                      'dataset_sha256': metadata['sha256'], 'prompt_sha256': canonical_hash(self.item['prompt']),
                      'status': status, 'attempts': [{}], 'seconds': 2}
                     for arm, status in [('grounded_call', 'completed'), ('codex_guided_v3', 'truncated')]]
        self.write_sources()
        self.root_patch = patch('local_tools.dialogue_server.ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write_sources(self):
        (self.comparison / 'manifest.json').write_text(json.dumps(self.manifest))
        (self.comparison / 'answers.jsonl').write_text('\n'.join(json.dumps(row) for row in self.rows) + '\n')

    def test_nested_comparison_preserves_arm_identity_status_and_hash_bound_reviews(self):
        reports = [{'prompt_id': row['prompt_id'], 'arm': row['arm'], 'status': 'reviewed', 'score': 50,
                    'answer_sha256': canonical_hash(row['answer']), 'note': 'Synthetic review.'} for row in self.rows]
        reports[1]['answer_sha256'] = canonical_hash('A different saved answer.')
        (self.comparison / 'assessments.json').write_text(json.dumps({'answers': reports}))
        state = ComparisonStore(self.data, 1, self.comparison).snapshot()
        self.assertEqual(state['arms'], ['grounded_call', 'codex_guided_v3'])
        self.assertTrue(state['read_only'])
        self.assertEqual(state['completed'], 2)
        self.assertEqual(state['planned'], 2)
        self.assertEqual(state['run'], 'synthetic-loop/round-01/comparison')
        self.assertEqual(state['run_summary']['source_statuses']['baseline'], 'partial')
        self.assertEqual(state['run_summary']['answer_status_counts'], {'completed': 1, 'truncated': 1})
        answers = state['cases'][0]['answers']
        self.assertEqual(answers['codex_guided_v3']['status'], 'truncated')
        self.assertEqual(answers['codex_guided_v3']['answer'], self.rows[1]['answer'])
        self.assertEqual(answers['grounded_call']['source_run'], 'healthbench-baseline')
        self.assertEqual(answers['codex_guided_v3']['source_run'], 'synthetic-loop/round-01/candidate')
        self.assertEqual(answers['grounded_call']['assessment']['score'], 50)
        self.assertNotIn('assessment', answers['codex_guided_v3'])
        self.assertNotIn('hidden', json.dumps(state))
        self.assertNotIn('raw_default', answers)

    def test_selected_comparison_cannot_trigger_default_or_candidate_generation(self):
        store = ComparisonStore(self.data, 1, self.comparison)
        with patch.object(store, '_run_impl') as generation:
            for arms in (['raw_default', 'grounded_call'], ['grounded_call', 'codex_guided_v3']):
                with self.assertRaisesRegex(RuntimeError, '읽기 전용'):
                    store.start(arms)
            generation.assert_not_called()
        self.assertFalse(store.busy)

    def test_records_from_other_run_or_wrong_prompt_are_not_mixed_into_comparison(self):
        other = self.root / 'results' / 'healthbench-newer'
        other.mkdir()
        (other / 'answers.jsonl').write_text(json.dumps(self.rows[0] | {'answer': 'Newer unrelated answer.'}) + '\n')
        self.rows[1]['prompt_sha256'] = canonical_hash([{'role': 'user', 'content': 'Different prompt.'}])
        self.write_sources()
        state = ComparisonStore(self.data, 1, self.comparison).snapshot()
        answers = state['cases'][0]['answers']
        self.assertEqual(answers['grounded_call']['answer'], self.rows[0]['answer'])
        self.assertEqual(answers['codex_guided_v3']['status'], 'not_run')
        self.assertEqual(state['completed'], 1)

    def test_manifest_identity_and_directory_scope_are_validated(self):
        outside = self.root / 'outside'
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, 'inside'):
            ComparisonStore(self.data, 1, outside)
        for change in ({'selected_arms': ['grounded_call', 'grounded_call']},
                       {'selected_arms': ['grounded_call', '<script>']},
                       {'selected_prompt_ids': ['UNKNOWN']}, {'dataset': {'sha256': 'wrong'}}):
            with self.subTest(change=change):
                (self.comparison / 'manifest.json').write_text(json.dumps(self.manifest | change))
                with self.assertRaises(ValueError):
                    ComparisonStore(self.data, 1, self.comparison)

    def test_default_view_keeps_existing_three_generation_arms(self):
        store = ComparisonStore(self.data, 1)
        state = store.snapshot()
        self.assertEqual(state['arms'], ['raw_default', 'lunit_call', 'grounded_call'])
        self.assertFalse(state['read_only'])
        self.assertEqual(set(state['cases'][0]['answers']), set(state['arms']))


if __name__ == '__main__':
    unittest.main()
