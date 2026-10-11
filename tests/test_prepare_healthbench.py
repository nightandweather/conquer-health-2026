"""Synthetic fixtures only: no benchmark questions or grading text."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.prepare_healthbench import HISTORY_PREFIX, convert_legacy, prepare, select_main


class PrepareHealthBenchTests(unittest.TestCase):
    def legacy(self, history=''):
        return {'qid': 'synthetic-local-1', 'text': 'Synthetic latest request.',
                'history': history, 'canary': 'synthetic-canary',
                'official_rubrics': [{'criterion': 'Synthetic positive criterion.', 'points': 5,
                                     'cluster': 'synthetic'},
                                    {'criterion': 'Synthetic penalty criterion.', 'points': -3}],
                'gold_keywords': ['SYNTHETIC_REFERENCE_MUST_NOT_BE_IN_PROMPT'],
                'gold_doc_ids': ['SYNTHETIC_DOCUMENT_MUST_NOT_BE_IN_PROMPT']}

    def canonical(self, pid):
        return {'prompt_id': pid,
                'prompt': [{'role': 'system', 'content': 'Synthetic system instruction.'},
                           {'role': 'user', 'content': 'Synthetic earlier turn.'},
                           {'role': 'assistant', 'content': 'Synthetic earlier answer.'},
                           {'role': 'user', 'content': 'Synthetic final request.'}],
                'rubrics': [{'criterion': 'Synthetic scoring rule.', 'points': 1}],
                'canary': 'synthetic-main-canary',
                'ideal_completions_data': {'ideal_completion': 'SYNTHETIC_REFERENCE'},
                'extra_metadata': {'preserve': True}}

    def test_legacy_retains_flattened_context_without_role_reconstruction(self):
        history = '  User: first\nAssistant: archive label, not a recoverable role\n'
        item = self.legacy(history)
        original = deepcopy(item)
        converted, manifest = convert_legacy([item])
        self.assertEqual(converted[0]['prompt'], [
            {'role': 'user', 'content': HISTORY_PREFIX + history},
            {'role': 'user', 'content': item['text']}])
        self.assertEqual(converted[0]['rubrics'], item['official_rubrics'])
        self.assertEqual(converted[0]['canary'], item['canary'])
        self.assertEqual(converted[0]['prompt_id'], item['qid'])
        self.assertEqual(item, original)
        self.assertEqual(manifest['history_count'], 1)
        self.assertTrue(manifest['normalized_context'])
        self.assertTrue(manifest['lost_original_prompt_ids'])
        self.assertFalse(manifest['canonical_score_comparable'])

    def test_legacy_empty_history_and_reference_fields_never_become_messages(self):
        item = self.legacy()
        converted, manifest = convert_legacy([item])
        self.assertEqual(converted[0]['prompt'], [{'role': 'user', 'content': item['text']}])
        self.assertNotIn('SYNTHETIC_REFERENCE', json.dumps(converted[0]['prompt']))
        self.assertNotIn('gold_keywords', converted[0])
        self.assertNotIn('gold_doc_ids', converted[0])
        self.assertEqual(manifest['history_count'], 0)
        self.assertEqual(manifest['rubric_count'], 2)

    def test_main_selection_preserves_entire_canonical_record_and_sorts_ids(self):
        rows = [self.canonical('z'), self.canonical('a'), self.canonical('unselected')]
        original = deepcopy(rows)
        ids = {'prompt_ids': ['z', 'a'], 'selection': 'synthetic selection'}
        converted, manifest = select_main(rows, ids)
        self.assertEqual(converted, [rows[1], rows[0]])
        self.assertEqual(rows, original)
        self.assertEqual(manifest['count'], 2)
        self.assertEqual(manifest['source_count'], 3)
        self.assertFalse(manifest['normalized_context'])
        self.assertEqual(manifest['selection_record'], {'selection': 'synthetic selection'})
        self.assertNotIn('SYNTHETIC_REFERENCE', json.dumps(converted[0]['prompt']))

    def test_main_missing_id_is_an_error(self):
        with self.assertRaisesRegex(ValueError, 'missing from Main'):
            select_main([self.canonical('a')], {'prompt_ids': ['a', 'missing']})

    def test_main_duplicate_source_or_requested_ids_are_errors(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            select_main([self.canonical('a'), self.canonical('a')], {'prompt_ids': ['a']})
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            select_main([self.canonical('a')], {'prompt_ids': ['a', 'a']})

    def test_legacy_duplicate_id_and_structured_history_are_errors(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            convert_legacy([self.legacy(), self.legacy()])
        with self.assertRaisesRegex(ValueError, 'string text and history'):
            convert_legacy([self.legacy([{'role': 'user', 'content': 'Synthetic'}])])

    def test_prepare_copies_source_license_and_hashes_exact_artifact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'legacy.json'
            source.write_text(json.dumps([self.legacy()]), encoding='utf-8')
            out = root / 'private' / 'legacy.json'
            manifest = prepare(out=out, legacy_file=source)
            self.assertEqual(manifest['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest['converted_sha256'], hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertEqual(out.with_suffix('.source.json').read_bytes(), source.read_bytes())
            self.assertTrue((out.parent / 'LICENSE-HealthBench.txt').is_file())
            self.assertEqual(json.loads(out.with_suffix('.manifest.json').read_bytes()), manifest)

    def test_prepare_main_copies_ids_unchanged_and_enforces_exclusive_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / 'main.jsonl'
            main.write_text(json.dumps(self.canonical('a')) + '\n', encoding='utf-8')
            ids = root / 'ids.json'
            ids.write_bytes(b'{"prompt_ids": ["a"], "selection": "synthetic"}\n')
            out = root / 'private' / 'conquer_val.json'
            manifest = prepare(out=out, main_file=main, ids_file=ids)
            self.assertEqual((out.parent / 'conquer_val_ids.json').read_bytes(), ids.read_bytes())
            self.assertEqual(manifest['ids_sha256'], hashlib.sha256(ids.read_bytes()).hexdigest())
            for kwargs in ({}, {'legacy_file': ids, 'main_file': main},
                           {'legacy_file': ids, 'ids_file': ids}, {'main_file': main}):
                with self.assertRaises(ValueError):
                    prepare(out=out, **kwargs)

    def test_prepare_main_reuses_ids_already_in_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / 'main.jsonl'
            main.write_text(json.dumps(self.canonical('a')) + '\n', encoding='utf-8')
            ids = root / 'conquer_val_ids.json'
            ids_raw = b'{ "prompt_ids": ["a"], "selection": "synthetic" }\n'
            ids.write_bytes(ids_raw)
            inode = ids.stat().st_ino
            out = root / 'conquer_val.json'
            manifest = prepare(out=out, main_file=main, ids_file=ids)
            self.assertEqual(ids.read_bytes(), ids_raw)
            self.assertEqual(ids.stat().st_ino, inode)
            self.assertEqual(manifest['ids_copy'], ids.name)
            self.assertEqual(manifest['ids_sha256'], hashlib.sha256(ids_raw).hexdigest())
            self.assertEqual(json.loads(out.read_bytes()), [self.canonical('a')])

    def test_reusing_ids_does_not_allow_other_outputs_to_overwrite_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / 'main.jsonl'
            main_raw = (json.dumps(self.canonical('a')) + '\n').encode('utf-8')
            main.write_bytes(main_raw)
            ids = root / 'conquer_val_ids.json'
            ids_raw = b'{"prompt_ids": ["a"]}\n'
            ids.write_bytes(ids_raw)
            for out in (main, ids):
                with self.assertRaisesRegex(ValueError, 'overwrite an input'):
                    prepare(out=out, main_file=main, ids_file=ids)
            self.assertEqual(main.read_bytes(), main_raw)
            self.assertEqual(ids.read_bytes(), ids_raw)


if __name__ == '__main__':
    unittest.main()
