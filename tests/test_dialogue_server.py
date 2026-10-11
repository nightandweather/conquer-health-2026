"""Synthetic checks for private display and streamed answer reconstruction."""
from io import BytesIO
import json
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from local_tools.dialogue_server import ComparisonStore, make_handler, read_stream, visible_record


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


if __name__ == '__main__':
    unittest.main()
