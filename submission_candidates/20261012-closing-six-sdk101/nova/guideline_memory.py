"""Ephemeral RAM-only lexical index of the mounted NOVA guideline corpus.

No SQLite/database/output file is created. FTS5 stores token postings, while
source excerpts are recovered from verified original byte ranges on search.
SQLite pages and temporary query work consume RAM; their page-byte count is not
peak process/cgroup memory. Clinical relevance, Korean morphology and translation
are not validated. Descriptor/stat checks reduce races but do not isolate against
a malicious same-UID process. Use one owner and close when the session ends.
"""
from __future__ import annotations

import copy
from hashlib import sha256
import json
import os
import sqlite3
import time

from . import guidelines as g


class MemoryGuidelineIndex:
    """Prepared, read-only in-memory SQLite connection and pinned source metadata."""
    def __init__(self, connection, source_dir, source_identity, metadata):
        self._connection = connection
        self.source_dir = str(source_dir)
        self._source_identity = source_identity
        self._metadata = metadata
        self._closed = False

    @property
    def metadata(self):
        return copy.deepcopy(self._metadata)

    def close(self):
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def search(self, query, limit=2, language=None):
        """Return one best chunk per document, up to limit distinct documents.

        Catalog and selected documents are revalidated against preparation pins.
        Unselected documents are not rescanned on every query. The SQLite VM has
        a five-second progress deadline; source-file reads are not a hard-deadline
        operation. The caller must recheck the overall session deadline.
        """
        if self._closed:
            raise ValueError('Memory guideline index is closed')
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
            raise ValueError('limit must be between 1 and 10')
        if language not in {None, 'ko', 'en'}:
            raise ValueError('language must be ko or en')
        match = g._query(query)
        started = time.monotonic()
        con = self._connection
        root_fd = g._directory(self.source_dir)
        try:
            if _identity(root_fd) != self._source_identity:
                raise ValueError('Source directory changed since preparation')
            catalog_raw, _ = g._read_source(root_fd, 'catalog.csv')
            if sha256(catalog_raw).hexdigest() != self._metadata['catalog_sha256']:
                raise ValueError('Source catalog changed since preparation')
            results = []
            if match:
                deadline = time.monotonic() + g.MAX_SEARCH_SECONDS
                con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                rows = con.execute('''
                    WITH scored AS MATERIALIZED (
                        SELECT search.rowid AS chunk_id, bm25(search,5.0,1.0) AS score
                        FROM search WHERE search MATCH ?
                    ), best AS (
                        SELECT c.document_id, min(s.score) AS score
                        FROM scored s JOIN chunks c ON c.id=s.chunk_id
                            JOIN documents d ON d.id=c.document_id
                        WHERE (? IS NULL OR json_extract(d.metadata_json,'$.frontmatter.language')=?)
                        GROUP BY c.document_id
                        ORDER BY score,c.document_id LIMIT ?
                    ), chosen AS (
                        SELECT b.document_id,b.score,min(c.id) AS chunk_id
                        FROM best b JOIN chunks c ON c.document_id=b.document_id
                            JOIN scored s ON s.chunk_id=c.id AND s.score=b.score
                        GROUP BY b.document_id,b.score
                    )
                    SELECT c.id AS chunk_id,c.document_id,c.byte_start,c.byte_end,
                        chosen.score,d.metadata_json,d.header,d.sha256,d.source_stat_json,
                        d.first_chunk_id,d.chunk_count
                    FROM chosen JOIN chunks c ON c.id=chosen.chunk_id
                        JOIN documents d ON d.id=c.document_id
                    ORDER BY chosen.score,c.document_id
                ''', (match, language, language, limit))
                for row in rows:
                    raw, _ = g._read_source(root_fd, row['document_id'],
                                             json.loads(row['source_stat_json']))
                    if sha256(raw).hexdigest() != row['sha256']:
                        raise ValueError('Source bytes changed since preparation')
                    info = json.loads(row['metadata_json'])
                    headers, header_raw, body, body_offset = g._document(raw, info['catalog'])
                    if headers != info['frontmatter'] or header_raw != row['header']:
                        raise ValueError('Indexed source metadata changed')
                    ordinal = row['chunk_id'] - row['first_chunk_id']
                    if not 0 <= ordinal < row['chunk_count']:
                        raise ValueError('Indexed chunk identity is invalid')
                    expected = next((chunk for i, chunk in enumerate(g._chunks(body, body_offset))
                                     if i == ordinal), None)
                    lo, hi = row['byte_start'], row['byte_end']
                    if expected is None or (lo, hi) != expected[1:]:
                        raise ValueError('Indexed byte offsets changed')
                    excerpt = expected[0]
                    if not 0 <= lo < hi <= len(raw) or raw[lo:hi] != excerpt.encode('utf-8'):
                        raise ValueError('Excerpt and source bytes disagree')
                    results.append({'document_id': row['document_id'], **headers,
                        'document_type': info['catalog']['문서 유형'],
                        'format': info['catalog']['형식'], 'catalog_metadata': info['catalog'],
                        'source_sha256': row['sha256'], 'snippet': excerpt,
                        'byte_start': lo, 'byte_end': hi,
                        'snippet_sha256': sha256(raw[lo:hi]).hexdigest(),
                        'bm25_rank_value': row['score'],
                        'reference_scope': g.REFERENCE_SCOPE,
                        'patient_observation': False, 'source_bytes_verified': True})
            check_root = g._directory(self.source_dir)
            try:
                if _identity(check_root) != self._source_identity:
                    raise ValueError('Source directory was replaced during retrieval')
            finally:
                os.close(check_root)
            return {'query': query, 'results': results, 'result_count': len(results),
                    'search_seconds': round(time.monotonic() - started, 6),
                    'reference_scope': g.REFERENCE_SCOPE, 'patient_observation': False,
                    'corpus_manifest_sha256': self._metadata['corpus_manifest_sha256'],
                    'zero_hits_means_unknown': True, 'limitations': g.LIMITATIONS,
                    'model_calls': 0, 'network_calls': 0}
        finally:
            con.set_progress_handler(None, 0)
            os.close(root_fd)


def _identity(fd):
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


def prepare_memory_guidelines(source_dir=None):
    """Build a fresh RAM index; hash every source and recheck every hash at end.

    Uses NOVA_GUIDELINES_DIR when source_dir is omitted. Source files are read-only.
    Failure closes SQLite and propagates; no output directory or fallback corpus
    is created. This function does not enforce CPU/RAM/overall-time limits.
    """
    started = time.monotonic()
    root = g._source_dir(source_dir)
    root_fd = g._directory(root)
    con = None
    try:
        root_identity = _identity(root_fd)
        names = g._inventory(root_fd)
        catalog_raw, catalog_stat = g._read_source(root_fd, 'catalog.csv')
        catalog = g._catalog(catalog_raw)
        if names != sorted(catalog):
            raise ValueError('Catalog and corpus paths must match exactly')
        source_stats = {'catalog.csv': catalog_stat}
        source_hashes = {'catalog.csv': sha256(catalog_raw).hexdigest()}
        for optional in ('README.md', 'catalog.xlsx'):
            try:
                raw, st = g._read_source(root_fd, optional)
            except FileNotFoundError:
                continue
            source_stats[optional] = st
            source_hashes[optional] = sha256(raw).hexdigest()
        con = sqlite3.connect(':memory:')
        con.row_factory = sqlite3.Row
        con.executescript('''
            PRAGMA journal_mode=MEMORY;
            PRAGMA temp_store=MEMORY;
            PRAGMA cache_size=-32768;
            CREATE TABLE documents(id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL,
                header BLOB NOT NULL, sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL,
                source_stat_json TEXT NOT NULL, first_chunk_id INTEGER NOT NULL,
                chunk_count INTEGER NOT NULL);
            CREATE TABLE chunks(id INTEGER PRIMARY KEY, document_id TEXT NOT NULL,
                byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL);
            CREATE INDEX chunk_document ON chunks(document_id,id);
            CREATE VIRTUAL TABLE search USING fts5(title,text,content='',
                tokenize='unicode61 remove_diacritics 2');
        ''')
        if con.execute('PRAGMA temp_store').fetchone()[0] != 2:
            raise ValueError('SQLite in-memory temporary storage is required')
        if any(row[2] for row in con.execute('PRAGMA database_list')):
            raise ValueError('SQLite database must not have an external file')
        chunk_count = source_bytes = 0
        languages, organizations = {}, {}
        corpus_digest = sha256()
        with con:
            for name in names:
                raw, st = g._read_source(root_fd, name)
                headers, header_raw, body, body_offset = g._document(raw, catalog[name])
                digest = sha256(raw).hexdigest()
                source_stats[name], source_hashes[name] = st, digest
                first_chunk_id = chunk_count + 1
                chunks, postings = [], []
                for text, lo, hi in g._chunks(body, body_offset):
                    chunk_count += 1
                    chunks.append((chunk_count, name, lo, hi))
                    postings.append((chunk_count, headers['title'], text))
                info = {'frontmatter': headers, 'catalog': catalog[name]}
                con.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',
                    (name, g._json(info), header_raw, digest, len(raw), g._json(st),
                     first_chunk_id, len(chunks)))
                con.executemany('INSERT INTO chunks VALUES(?,?,?,?)', chunks)
                con.executemany('INSERT INTO search(rowid,title,text) VALUES(?,?,?)', postings)
                source_bytes += len(raw)
                languages[headers['language']] = languages.get(headers['language'], 0) + 1
                folder = name.split('/', 1)[0]
                organizations[folder] = organizations.get(folder, 0) + 1
                corpus_digest.update((g._json([name, digest, len(raw)]) + '\n').encode())
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity verification failed')
        # Validate contentless posting structure without requesting external text.
        con.execute("INSERT INTO search(search) VALUES('integrity-check')")
        con.commit()
        if g._inventory(root_fd) != names:
            raise ValueError('Source inventory changed during preparation')
        for name, expected_stat in source_stats.items():
            verified, _ = g._read_source(root_fd, name, expected_stat)
            if sha256(verified).hexdigest() != source_hashes[name]:
                raise ValueError('Source hash changed during preparation')
        check_root = g._directory(root)
        try:
            if _identity(check_root) != root_identity:
                raise ValueError('Source directory changed during preparation')
        finally:
            os.close(check_root)
        if con.execute('SELECT count(*) FROM chunks').fetchone()[0] != chunk_count:
            raise ValueError('Chunk count mismatch')
        if con.execute('SELECT count(*) FROM search').fetchone()[0] != chunk_count:
            raise ValueError('FTS posting count mismatch')
        con.execute('PRAGMA query_only=ON')
        page_bytes = (con.execute('PRAGMA page_count').fetchone()[0]
                      * con.execute('PRAGMA page_size').fetchone()[0])
        metadata = {
            'schema_version': 1, 'backend': 'sqlite_memory_contentless_fts5',
            'source_dir': str(root), 'document_count': len(names),
            'chunk_count': chunk_count, 'source_markdown_bytes': source_bytes,
            'corpus_manifest_sha256': corpus_digest.hexdigest(),
            'catalog_sha256': source_hashes['catalog.csv'],
            'ancillary_sha256': {k: v for k, v in source_hashes.items() if k not in catalog},
            'languages': languages, 'source_folders': organizations,
            'chunk_chars': g.CHUNK_CHARS, 'chunk_overlap': g.CHUNK_OVERLAP,
            'reference_scope': g.REFERENCE_SCOPE, 'patient_observation': False,
            'source_read_only': True, 'all_source_hashes_rechecked': True,
            'external_database_files': 0, 'sqlite_temp_store': 'MEMORY',
            'memory_page_bytes': page_bytes,
            'memory_page_bytes_are_not_peak_process_memory': True,
            'build_seconds': round(time.monotonic() - started, 6),
            'model_calls': 0, 'network_calls': 0,
            'resource_limits_enforced': False, 'clinical_quality_verified': False,
            'redistribution_allowed_by_code': False, 'limitations': g.LIMITATIONS,
        }
        result = MemoryGuidelineIndex(con, root, root_identity, metadata)
        con = None  # Ownership transfers only after all source and DB checks.
        return result
    finally:
        if con is not None:
            con.close()
        os.close(root_fd)
