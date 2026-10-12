import builtins
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from nova import guidelines as g
from nova.guideline_memory import prepare_memory_guidelines
from tests import test_guidelines as disk_tests


class MemoryGuidelineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = disk_tests.GuidelineTests()
        self.fixture.setUp()
        self.source = self.fixture.source
        self.index = None

    def tearDown(self):
        if self.index is not None:
            self.index.close()
        self.fixture.tearDown()

    def prepare(self):
        self.index = prepare_memory_guidelines(self.source)
        return self.index

    def test_prepare_full_inventory_hashes_and_contentless_storage(self):
        reads = []
        original = g._read_source
        def read(fd, name, expected=None):
            reads.append(name)
            return original(fd, name, expected)
        with patch.object(g, '_read_source', side_effect=read):
            index = self.prepare()
        metadata = index.metadata
        self.assertEqual(metadata['document_count'], 2)
        expected_chunks = 0
        for row in self.fixture.rows:
            raw = (self.source / row['파일명']).read_bytes()
            _, _, body, offset = g._document(raw, row)
            expected_chunks += len(list(g._chunks(body, offset)))
            self.assertEqual(reads.count(row['파일명']), 2)
        self.assertEqual(reads.count('catalog.csv'), 2)
        self.assertEqual(metadata['chunk_count'], expected_chunks)
        self.assertTrue(metadata['all_source_hashes_rechecked'])
        self.assertEqual(metadata['external_database_files'], 0)
        self.assertGreater(metadata['memory_page_bytes'], 0)
        self.assertEqual(index._connection.execute('PRAGMA temp_store').fetchone()[0], 2)
        self.assertTrue(all(not r[2] for r in index._connection.execute('PRAGMA database_list')))
        self.assertTrue(all(tuple(row) == (None, None) for row in
                            index._connection.execute('SELECT title,text FROM search')))
        columns = [r[1] for r in index._connection.execute('PRAGMA table_info(chunks)')]
        self.assertEqual(columns, ['id', 'document_id', 'byte_start', 'byte_end'])
        metadata['document_count'] = 99
        self.assertEqual(index.metadata['document_count'], 2)

    def test_no_external_writes_from_prepare_or_search(self):
        before = {str(p): p.read_bytes() for p in self.fixture.base.rglob('*') if p.is_file()}
        real_open, real_os_open = builtins.open, os.open
        real_connect = sqlite3.connect
        def guarded_open(file, mode='r', *args, **kwargs):
            if any(char in mode for char in 'wax+'):
                raise AssertionError('Unexpected external file write')
            return real_open(file, mode, *args, **kwargs)
        def guarded_os_open(file, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise AssertionError('Unexpected external file descriptor write')
            return real_os_open(file, flags, *args, **kwargs)
        def guarded_connect(database, *args, **kwargs):
            self.assertEqual(database, ':memory:')
            return real_connect(database, *args, **kwargs)
        with patch('builtins.open', side_effect=guarded_open), \
             patch('os.open', side_effect=guarded_os_open), \
             patch('sqlite3.connect', side_effect=guarded_connect):
            index = self.prepare()
            self.assertEqual(index.search('aspirin')['result_count'], 1)
        after = {str(p): p.read_bytes() for p in self.fixture.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(list(self.fixture.output.iterdir()), [])

    def test_matches_existing_disk_search_results_and_manifest(self):
        # The disk baseline is synthetic reference-only test setup; the memory
        # implementation itself never receives or writes that database path.
        disk_metadata = g.build_index(self.source, self.fixture.db)
        index = self.prepare()
        for key in ('document_count', 'chunk_count', 'source_markdown_bytes',
                    'corpus_manifest_sha256', 'catalog_sha256'):
            self.assertEqual(index.metadata[key], disk_metadata[key])
        for query in ('aspirin', '인플루엔자', 'aspirin 인플루엔자'):
            self.assertEqual(index.search(query)['results'],
                             g.search_index(self.fixture.db, query, limit=2)['results'])

    def test_verbatim_multibyte_ranges_and_general_reference_scope(self):
        index = self.prepare()
        result = index.search('인플루엔자')
        self.assertEqual(result['result_count'], 1)
        hit = result['results'][0]
        raw = (self.source / hit['document_id']).read_bytes()
        excerpt = raw[hit['byte_start']:hit['byte_end']]
        self.assertEqual(excerpt.decode(), hit['snippet'])
        self.assertEqual(sha256(excerpt).hexdigest(), hit['snippet_sha256'])
        self.assertTrue(hit['source_bytes_verified'])
        self.assertFalse(hit['patient_observation'])
        self.assertEqual(hit['reference_scope'], 'general_medical_reference')
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['network_calls'], 0)

    def test_results_are_distinct_documents_and_language_filter(self):
        self.fixture.add('EN/c.md', title='Synthetic aspirin second', body='aspirin evidence ' * 400)
        self.fixture.catalog()
        index = self.prepare()
        hits = index.search('aspirin', limit=2)['results']
        self.assertEqual(len(hits), 2)
        self.assertEqual(len({h['document_id'] for h in hits}), 2)
        self.assertEqual(index.search('aspirin', language='ko')['results'], [])
        self.assertEqual(index.search('aspirin', language='en')['result_count'], 2)

    def test_empty_and_missing_hits_are_explicit(self):
        index = self.prepare()
        for query in ('', '!!!', 'nonexistentqzyx'):
            result = index.search(query)
            self.assertEqual(result['results'], [])
            self.assertTrue(result['zero_hits_means_unknown'])

    def test_query_size_limits_and_syntax_inert(self):
        index = self.prepare()
        for query in (None, '한' * 1000, ' '.join('t' + str(n) for n in range(25))):
            with self.assertRaises(ValueError):
                index.search(query)
        for limit in (0, 11, True, 1.5):
            with self.assertRaises(ValueError):
                index.search('aspirin', limit=limit)
        with self.assertRaises(ValueError):
            index.search('aspirin', language='fr')
        self.assertEqual(index.search('aspirin" OR NOT * ); DROP TABLE documents; --')['result_count'], 1)
        self.assertEqual(index.metadata['document_count'], 2)

    def test_source_mutation_rejected(self):
        index = self.prepare()
        p = self.source / 'EN/a.md'
        p.write_text(p.read_text() + '\nchanged')
        with self.assertRaisesRegex(ValueError, 'Source fingerprint changed'):
            index.search('aspirin')

    def test_catalog_mutation_rejected(self):
        index = self.prepare()
        p = self.source / 'catalog.csv'
        p.write_bytes(p.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'catalog changed'):
            index.search('aspirin')

    def test_offset_and_metadata_tamper_rejected(self):
        index = self.prepare()
        con = index._connection
        con.execute('PRAGMA query_only=OFF')
        con.execute('UPDATE chunks SET byte_start=byte_start+1 WHERE document_id=?', ('EN/a.md',))
        con.commit()
        con.execute('PRAGMA query_only=ON')
        with self.assertRaisesRegex(ValueError, 'byte offsets changed'):
            index.search('aspirin')
        index.close()
        index = self.prepare()
        con = index._connection
        con.execute('PRAGMA query_only=OFF')
        metadata = json.loads(con.execute('SELECT metadata_json FROM documents WHERE id=?', ('EN/a.md',)).fetchone()[0])
        metadata['frontmatter']['title'] = 'Changed title'
        con.execute('UPDATE documents SET metadata_json=? WHERE id=?', (json.dumps(metadata), 'EN/a.md'))
        con.commit()
        con.execute('PRAGMA query_only=ON')
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            index.search('aspirin')

    def test_read_only_connection_and_close(self):
        index = self.prepare()
        with self.assertRaises(sqlite3.OperationalError):
            index._connection.execute('DELETE FROM chunks')
        index.close()
        index.close()
        with self.assertRaisesRegex(ValueError, 'closed'):
            index.search('aspirin')

    def test_progress_deadline_then_later_query_recovers(self):
        for n in range(20):
            self.fixture.add(f'EN/many{n}.md', title='Synthetic common', body='common word ' * 300)
        self.fixture.catalog()
        index = self.prepare()
        with patch.object(g, 'MAX_SEARCH_SECONDS', 0):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'interrupted'):
                index.search('common')
        self.assertEqual(index.search('common')['result_count'], 2)

    def test_env_default_and_missing_env_does_not_fetch(self):
        with patch.dict(os.environ, {'NOVA_GUIDELINES_DIR': str(self.source)}):
            self.index = prepare_memory_guidelines()
            self.assertEqual(self.index.metadata['document_count'], 2)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'no automatic download'):
                prepare_memory_guidelines()

    def test_catalog_missing_file_symlink_and_unexpected_files_rejected(self):
        p = self.source / 'EN/a.md'
        original = p.read_bytes()
        p.unlink()
        with self.assertRaisesRegex(ValueError, 'paths must match'):
            prepare_memory_guidelines(self.source)
        p.write_bytes(original)
        link = self.source / 'EN/link.md'
        link.symlink_to(p)
        with self.assertRaisesRegex(ValueError, 'Symlinks'):
            prepare_memory_guidelines(self.source)
        link.unlink()
        extra = self.source / 'patient.json'
        extra.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Unexpected'):
            prepare_memory_guidelines(self.source)

    def test_concurrent_source_change_during_prepare_rejected(self):
        inventory = g._inventory
        calls = 0
        def changed(fd):
            nonlocal calls
            calls += 1
            result = inventory(fd)
            if calls == 2:
                p = self.source / 'EN/a.md'
                p.write_text(p.read_text() + '\nchanged during build')
            return result
        with patch.object(g, '_inventory', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'fingerprint changed'):
                prepare_memory_guidelines(self.source)
        self.assertEqual(list(self.fixture.output.iterdir()), [])

    def test_prepare_failure_closes_sqlite(self):
        connect = sqlite3.connect
        captured = []
        def capture(*args, **kwargs):
            con = connect(*args, **kwargs); captured.append(con); return con
        with patch('sqlite3.connect', side_effect=capture), \
             patch.object(g, '_document', side_effect=ValueError('synthetic failure')):
            with self.assertRaisesRegex(ValueError, 'synthetic failure'):
                prepare_memory_guidelines(self.source)
        self.assertEqual(len(captured), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            captured[0].execute('SELECT 1')


if __name__ == '__main__':
    unittest.main()
