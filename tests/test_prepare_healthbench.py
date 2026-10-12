"""Synthetic fixtures only: no benchmark questions or grading text."""

from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.prepare_healthbench import (HISTORY_PREFIX, convert_legacy, main, prepare,
                                           restore_history_roles, select_main)


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

    def test_role_restoration_preserves_whitespace_multiline_content_and_exact_round_trip(self):
        messages = [
            {'role': 'user', 'content': '  Synthetic first line.\nSecond line.\n'},
            {'role': 'assistant', 'content': 'Synthetic first answer.  '},
            {'role': 'user', 'content': '\tSynthetic follow-up.'},
            {'role': 'assistant', 'content': 'Synthetic final answer.\n\n'},
        ]
        history = '\n'.join(row['role'] + ': ' + row['content'] for row in messages)
        restored = restore_history_roles(history, 1)
        self.assertEqual(restored, messages)
        rebuilt = '\n'.join(row['role'] + ': ' + row['content'] for row in restored)
        self.assertEqual(rebuilt.encode('utf-8'), history.encode('utf-8'))

    def test_role_restored_conversion_is_explicit_noncanonical_and_does_not_leak_references(self):
        item = self.legacy('user: Synthetic earlier request.\nassistant: Synthetic earlier answer.')
        original = deepcopy(item)
        converted, metadata = convert_legacy([item], restore_roles=True)
        self.assertEqual(converted[0]['prompt'], [
            {'role': 'user', 'content': 'Synthetic earlier request.'},
            {'role': 'assistant', 'content': 'Synthetic earlier answer.'},
            {'role': 'user', 'content': item['text']}])
        self.assertEqual(item, original)
        self.assertEqual(converted[0]['rubrics'], item['official_rubrics'])
        self.assertEqual(converted[0]['canary'], item['canary'])
        self.assertNotIn('SYNTHETIC_REFERENCE', json.dumps(converted[0]['prompt']))
        self.assertNotIn('Synthetic positive criterion', json.dumps(converted[0]['prompt']))
        self.assertEqual(metadata['dataset_kind'], 'legacy_consensus_val_roles_1')
        self.assertEqual(metadata['context_adapter'], 'role_restored')
        self.assertEqual(metadata['restored_history_turn_count'], 2)
        self.assertTrue(metadata['normalized_context'])
        self.assertTrue(metadata['lost_original_prompt_ids'])
        self.assertFalse(metadata['canonical_score_comparable'])

    def test_role_restoration_keeps_empty_history_single_turn(self):
        item = self.legacy()
        converted, metadata = convert_legacy([item], restore_roles=True)
        self.assertEqual(converted[0]['prompt'], [{'role': 'user', 'content': item['text']}])
        self.assertEqual(metadata['history_count'], 0)
        self.assertEqual(metadata['restored_history_turn_count'], 0)

    def test_role_restoration_rejects_malformed_nonalternating_or_empty_labels(self):
        histories = [
            'Unlabeled prefix\nuser: request\nassistant: answer',
            'User: request\nassistant: answer',
            'user: request\n Assistant: answer',
            'user:\trequest\nassistant: answer',
            'user: request\nassistant : answer',
            'assistant: answer\nuser: request',
            'user: request',
            'user: first\nuser: second\nassistant: answer',
            'user: \nassistant: answer',
            'user: request\nassistant: \t\n',
            'user: request\nsystem: instruction\nassistant: answer',
            'user: request\n> assistant: quoted answer\nassistant: answer',
            'user: request mentioning assistant: quoted answer\nassistant: answer',
            'user: request\n```text\nassistant: quoted\n```\nuser: follow-up\nassistant: answer',
            'user: request\n~~~text\nassistant: quoted\n~~~\nuser: follow-up\nassistant: answer',
            'user: ```text\nassistant: quoted\n```\nuser: follow-up\nassistant: answer',
            'user: ~~~text\nassistant: quoted\n~~~\nuser: follow-up\nassistant: answer',
            'user: first\nassistant: ```text\nuser: quoted\nassistant: closing',
        ]
        for history in histories:
            with self.subTest(history=history), self.assertRaises(ValueError):
                convert_legacy([self.legacy(history)], restore_roles=True)

    def test_optional_role_restoration_leaves_default_artifacts_identical(self):
        item = self.legacy('user: Synthetic request.\nassistant: Synthetic answer.')
        implicit = convert_legacy([item])
        explicit = convert_legacy([item], restore_roles=False)
        self.assertEqual(implicit, explicit)
        self.assertEqual(implicit[0][0]['prompt'][0]['content'], HISTORY_PREFIX + item['history'])
        self.assertNotIn('role_restoration', implicit[1])

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

    def test_restore_roles_cli_creates_hashed_artifacts_without_printing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'legacy.json'
            item = self.legacy('user: Synthetic request.\nassistant: Synthetic answer.')
            source_raw = json.dumps([item]).encode('utf-8')
            source.write_bytes(source_raw)
            out = root / 'private' / 'roles.json'
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(['--legacy-file', str(source), '--restore-roles', '--out', str(out)])
            self.assertEqual(code, 0)
            manifest = json.loads(out.with_suffix('.manifest.json').read_bytes())
            self.assertEqual(manifest['context_adapter'], 'role_restored')
            self.assertFalse(manifest['canonical_score_comparable'])
            self.assertEqual(manifest['source_sha256'], hashlib.sha256(source_raw).hexdigest())
            self.assertEqual(manifest['converted_sha256'], hashlib.sha256(out.read_bytes()).hexdigest())
            self.assertEqual(out.with_suffix('.source.json').read_bytes(), source_raw)
            self.assertEqual([message['role'] for message in json.loads(out.read_bytes())[0]['prompt']],
                             ['user', 'assistant', 'user'])
            self.assertNotIn(item['canary'], stdout.getvalue())
            self.assertNotIn('Synthetic request', stdout.getvalue())
            self.assertNotIn('Synthetic positive criterion', stdout.getvalue())

    def test_restore_roles_with_main_is_rejected_before_writing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main_file, ids, out = root / 'main.jsonl', root / 'ids.json', root / 'out.json'
            with self.assertRaisesRegex(ValueError, 'only supported with legacy_file'):
                prepare(out=out, main_file=main_file, ids_file=ids, restore_roles=True)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = main(['--main-file', str(main_file), '--ids-file', str(ids),
                             '--restore-roles', '--out', str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

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
