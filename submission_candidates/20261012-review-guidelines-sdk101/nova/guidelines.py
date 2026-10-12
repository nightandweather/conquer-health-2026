"""Offline lexical search over the provided NOVA Markdown corpus.

The SQLite index contains copyrighted source excerpts: keep it private and do
not put it in a submission ZIP or Git. Build against NOVA_GUIDELINES_DIR on the
mounted corpus instead. Results are general references, not patient evidence,
diagnoses, or proof that a medical statement is correct. No model/network calls.

Sources are opened read-only with component-wise no-follow checks, fingerprinted
before/after reads and again before publication. Same-UID malicious concurrent
replacement cannot be completely excluded between checks; use a dedicated
private output directory and one writer. Search verifies the returned excerpts
against the original bytes. Unicode word matching has no Korean morphology,
translation, semantic retrieval, or clinical relevance validation.
"""

from __future__ import annotations

import argparse
import csv
from hashlib import sha256
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import time
import uuid
from urllib.parse import quote, urlsplit


SCHEMA_VERSION = 1
CHUNK_CHARS = 1600
CHUNK_OVERLAP = 160
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_QUERY_BYTES = 2048
MAX_QUERY_TERMS = 24
MAX_SEARCH_SECONDS = 5.0
CATALOG_COLUMNS = (
    "파일명", "제목", "발행기관", "연도", "언어", "문서 유형", "형식", "라이선스", "출처 URL"
)
HEADER_KEYS = ("title", "organization", "year", "language", "source_url", "license")
REFERENCE_SCOPE = "general_medical_reference"
LIMITATIONS = (
    "Lexical BM25 only; no Korean morphology, translation or semantic search.",
    "A source excerpt is not a patient observation or an automatic diagnosis.",
    "Coverage, clinical correctness and retrieval recall are not validated.",
    "Keep the local index and source excerpts private; preserve source/license.",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_uid,
            st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _absolute(path):
    # Do not resolve symlinks away: each original component must be checked.
    value = os.path.abspath(os.fspath(path))
    if ".." in Path(os.fspath(path)).parts:
        raise ValueError("Parent traversal is not allowed")
    return Path(value)


def _directory(path):
    path = _absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def _relative(name):
    p = PurePosixPath(name)
    if not name or p.is_absolute() or "\\" in name or p.as_posix() != name:
        raise ValueError("Invalid relative source path")
    if any(part in {".", "..", ""} for part in name.split("/")):
        raise ValueError("Invalid relative source path")
    return p


def _open_source(root_fd, name):
    parts = _relative(name).parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=current)
            os.close(current)
            current = nxt
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=current)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("Source must be a regular file")
        return fd
    finally:
        os.close(current)


def _read_source(root_fd, name, expected=None):
    fd = _open_source(root_fd, name)
    try:
        before = _fingerprint(os.fstat(fd))
        if before[5] > MAX_FILE_BYTES:
            raise ValueError("Source file exceeds the bounded read size")
        if expected is not None and tuple(expected) != before:
            raise ValueError("Source fingerprint changed")
        with os.fdopen(os.dup(fd), "rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES or len(raw) != before[5]:
            raise ValueError("Source changed while reading")
        if _fingerprint(os.fstat(fd)) != before:
            raise ValueError("Source changed while reading")
        check = _open_source(root_fd, name)
        try:
            if _fingerprint(os.fstat(check)) != before:
                raise ValueError("Source path changed while reading")
        finally:
            os.close(check)
        return raw, before
    finally:
        os.close(fd)


def _inventory(root_fd):
    """Only the catalog's Markdown corpus is accepted, never benchmark records."""
    names = []
    def visit(fd, prefix=""):
        for item in sorted(os.listdir(fd)):
            rel = prefix + item
            st = os.stat(item, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise ValueError("Symlinks in the source corpus are not allowed")
            if stat.S_ISDIR(st.st_mode):
                sub = os.open(item, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    visit(sub, rel + "/")
                finally:
                    os.close(sub)
            elif stat.S_ISREG(st.st_mode):
                if "/" in rel and rel.endswith(".md"):
                    names.append(rel)
                elif prefix or item not in {"README.md", "catalog.csv", "catalog.xlsx"}:
                    raise ValueError("Unexpected non-guideline source file")
            else:
                raise ValueError("Only regular corpus files are supported")
    visit(root_fd)
    return names


def _catalog(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if tuple(reader.fieldnames or ()) != CATALOG_COLUMNS:
        raise ValueError("The provided guideline catalog schema is required")
    rows = {}
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise ValueError("Malformed catalog row")
        name = row["파일명"]
        _relative(name)
        if "/" not in name or not name.endswith(".md") or name in rows:
            raise ValueError("Invalid or duplicate catalog document ID")
        if any(not row[k].strip() for k in CATALOG_COLUMNS if k != "연도"):
            raise ValueError("Missing required catalog metadata")
        if urlsplit(row["출처 URL"]).scheme not in {"http", "https"}:
            raise ValueError("A source HTTP(S) URL is required")
        rows[name] = row
    if not rows:
        raise ValueError("Empty guideline corpus")
    return rows


def _document(raw, catalog):
    lines = raw.splitlines(keepends=True)
    if not lines or lines[0].strip() != b"---":
        raise ValueError("Missing Markdown metadata header")
    headers = {}
    offset = len(lines[0])
    closed = False
    for line in lines[1:]:
        offset += len(line)
        if offset > 64 * 1024:
            raise ValueError("Metadata header is too large")
        if line.strip() == b"---":
            closed = True
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if ":" not in text:
            raise ValueError("Only the provided scalar metadata format is supported")
        key, value = text.split(":", 1)
        value = value.strip()
        if key not in HEADER_KEYS or key in headers:
            raise ValueError("Unknown or duplicate metadata field")
        if value.startswith('"'):
            value = json.loads(value)
            if not isinstance(value, str):
                raise ValueError("Metadata values must be strings")
        elif not re.fullmatch(r"[\w-]*", value):
            raise ValueError("Unsupported metadata scalar")
        headers[key] = value
    if not closed or set(headers) != set(HEADER_KEYS):
        raise ValueError("Incomplete Markdown metadata header")
    for key, column in (("title", "제목"), ("organization", "발행기관"), ("year", "연도"),
                        ("license", "라이선스"), ("source_url", "출처 URL")):
        if headers[key] != catalog[column]:
            raise ValueError("Catalog and Markdown metadata disagree")
    if {"ko": "한국어", "en": "영어"}.get(headers["language"]) != catalog["언어"]:
        raise ValueError("Catalog and Markdown language disagree")
    body = raw[offset:].decode("utf-8")
    if not body.strip():
        raise ValueError("Empty Markdown body")
    return headers, raw[:offset], body, offset


def _chunks(body, body_offset):
    """Bounded verbatim windows; byte offsets include the unchanged header."""
    start = 0
    byte_start = body_offset
    while start < len(body):
        end = min(start + CHUNK_CHARS, len(body))
        text = body[start:end]
        byte_end = byte_start + len(text.encode("utf-8"))
        yield text, byte_start, byte_end
        if end == len(body):
            break
        step = CHUNK_CHARS - CHUNK_OVERLAP
        byte_start += len(body[start:start + step].encode("utf-8"))
        start += step


def _source_dir(source_dir=None):
    value = source_dir or os.environ.get("NOVA_GUIDELINES_DIR")
    if not value:
        raise ValueError("Set NOVA_GUIDELINES_DIR or provide --source; no automatic download")
    return _absolute(value)


def build_index(source_dir, db_path):
    """Build a fresh private DB. Publication never replaces an existing path."""
    started = time.monotonic()
    root = _source_dir(source_dir)
    target = _absolute(db_path)
    if target == root or root in target.parents:
        raise ValueError("Index must be outside the read-only source corpus")
    root_fd = _directory(root)
    try:
        parent_fd = _directory(target.parent)
    except BaseException:
        os.close(root_fd)
        raise
    parent_st = os.fstat(parent_fd)
    if parent_st.st_uid != os.getuid() or stat.S_IMODE(parent_st.st_mode) & 0o077:
        os.close(root_fd)
        os.close(parent_fd)
        raise ValueError("Use an owner-only private output directory (0700)")
    temp_name = ".guidelines-" + uuid.uuid4().hex + ".sqlite"
    temp_path = target.parent / temp_name
    temp_fd = None
    connection = None
    try:
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("Refusing to overwrite an existing index")
        root_identity = (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino)
        names = _inventory(root_fd)
        catalog_raw, catalog_stat = _read_source(root_fd, "catalog.csv")
        catalog = _catalog(catalog_raw)
        if names != sorted(catalog):
            raise ValueError("Catalog and corpus paths must match exactly")
        source_stats = {"catalog.csv": catalog_stat}
        source_hashes = {"catalog.csv": sha256(catalog_raw).hexdigest()}
        for optional in ("README.md", "catalog.xlsx"):
            try:
                raw, st = _read_source(root_fd, optional)
            except FileNotFoundError:
                continue
            source_stats[optional] = st
            source_hashes[optional] = sha256(raw).hexdigest()
        temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=parent_fd)
        temp_identity = (os.fstat(temp_fd).st_dev, os.fstat(temp_fd).st_ino)
        connection = sqlite3.connect(str(temp_path))
        if (temp_path.stat().st_dev, temp_path.stat().st_ino) != temp_identity:
            raise ValueError("Index path changed during SQLite open")
        connection.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA cache_size=-32768;
            PRAGMA temp_store=FILE;
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE documents(id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL,
                header BLOB NOT NULL, sha256 TEXT NOT NULL, byte_length INTEGER NOT NULL,
                source_stat_json TEXT NOT NULL);
            CREATE TABLE chunks(id INTEGER PRIMARY KEY, document_id TEXT NOT NULL,
                title TEXT NOT NULL, text TEXT NOT NULL, byte_start INTEGER NOT NULL,
                byte_end INTEGER NOT NULL);
            CREATE INDEX chunk_document ON chunks(document_id,id);
            CREATE VIRTUAL TABLE search USING fts5(title, text, content='chunks',
                content_rowid='id', tokenize='unicode61 remove_diacritics 2');
        """)
        count_chunks = source_bytes = 0
        languages, organizations = {}, {}
        corpus_digest = sha256()
        with connection:
            for name in names:
                raw, st = _read_source(root_fd, name)
                headers, header_raw, body, body_offset = _document(raw, catalog[name])
                digest = sha256(raw).hexdigest()
                source_stats[name] = st
                source_hashes[name] = digest
                metadata = {"frontmatter": headers, "catalog": catalog[name]}
                connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)",
                    (name, _json(metadata), header_raw, digest, len(raw), _json(st)))
                chunks = [(name, headers["title"], text, lo, hi)
                          for text, lo, hi in _chunks(body, body_offset)]
                connection.executemany("INSERT INTO chunks(document_id,title,text,byte_start,byte_end) VALUES(?,?,?,?,?)", chunks)
                count_chunks += len(chunks)
                source_bytes += len(raw)
                languages[headers["language"]] = languages.get(headers["language"], 0) + 1
                organization = name.split("/", 1)[0]
                organizations[organization] = organizations.get(organization, 0) + 1
                corpus_digest.update((_json([name, digest, len(raw)]) + "\n").encode())
            connection.execute("INSERT INTO search(search) VALUES('rebuild')")
            metadata = {
                "schema_version": SCHEMA_VERSION, "source_dir": str(root),
                "document_count": len(names), "chunk_count": count_chunks,
                "source_markdown_bytes": source_bytes,
                "corpus_manifest_sha256": corpus_digest.hexdigest(),
                "catalog_sha256": source_hashes["catalog.csv"],
                "ancillary_sha256": {k: v for k, v in source_hashes.items() if k not in catalog},
                "languages": languages, "source_folders": organizations,
                "chunk_chars": CHUNK_CHARS, "chunk_overlap": CHUNK_OVERLAP,
                "reference_scope": REFERENCE_SCOPE,
                "patient_observation": False, "redistribution_allowed_by_code": False,
                "limitations": LIMITATIONS,
            }
            connection.executemany("INSERT INTO meta VALUES(?,?)", [(k, _json(v)) for k, v in metadata.items()])
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity verification failed")
        if _inventory(root_fd) != names:
            raise ValueError("Source inventory changed during build")
        for name, expected in source_stats.items():
            fd = _open_source(root_fd, name)
            try:
                if _fingerprint(os.fstat(fd)) != expected:
                    raise ValueError("Source fingerprint changed during build")
            finally:
                os.close(fd)
        check_root = _directory(root)
        check_parent = _directory(target.parent)
        try:
            if (os.fstat(check_root).st_dev, os.fstat(check_root).st_ino) != root_identity:
                raise ValueError("Source directory was replaced")
            if (os.fstat(check_parent).st_dev, os.fstat(check_parent).st_ino) != (parent_st.st_dev, parent_st.st_ino):
                raise ValueError("Output directory was replaced")
        finally:
            os.close(check_root)
            os.close(check_parent)
        connection.close()
        connection = None
        if (temp_path.stat().st_dev, temp_path.stat().st_ino) != temp_identity:
            raise ValueError("Index file was replaced before publication")
        os.fsync(temp_fd)
        os.fchmod(temp_fd, 0o400)
        # link() publishes a complete file atomically and refuses any existing name.
        os.link(temp_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False)
        os.unlink(temp_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        metadata.update({"index_bytes": os.fstat(temp_fd).st_size,
                         "build_seconds": round(time.monotonic() - started, 6),
                         "index_path": str(target), "model_calls": 0, "network_calls": 0,
                         "source_read_only": True})
        return metadata
    finally:
        if connection is not None:
            connection.close()
        if temp_fd is not None:
            try:
                st = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
                if (st.st_dev, st.st_ino) == (os.fstat(temp_fd).st_dev, os.fstat(temp_fd).st_ino):
                    os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(temp_fd)
        os.close(root_fd)
        os.close(parent_fd)


def _open_index(db_path):
    path = _absolute(db_path)
    parent = _directory(path.parent)
    try:
        fd = _open_source(parent, path.name)
    finally:
        os.close(parent)
    before = _fingerprint(os.fstat(fd))
    con = None
    try:
        uri = "file:" + quote(str(path), safe="/") + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
        if _fingerprint(path.stat(follow_symlinks=False)) != before:
            con.close()
            raise ValueError("Index path changed during read-only open")
        con.row_factory = sqlite3.Row
        metadata = {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT key,value FROM meta")}
        if metadata.get("schema_version") != SCHEMA_VERSION or metadata.get("reference_scope") != REFERENCE_SCOPE:
            con.close()
            raise ValueError("Unsupported guideline index")
        return con, fd, before, metadata
    except BaseException:
        if con is not None:
            con.close()
        os.close(fd)
        raise


def _check_index(path, fd, expected):
    if _fingerprint(os.fstat(fd)) != expected or _fingerprint(_absolute(path).stat(follow_symlinks=False)) != expected:
        raise ValueError("Index changed during retrieval")


def index_status(db_path):
    con, fd, before, metadata = _open_index(db_path)
    try:
        actual = con.execute("SELECT count(*) FROM documents").fetchone()[0]
        chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if (actual, chunks) != (metadata["document_count"], metadata["chunk_count"]):
            raise ValueError("Index count verification failed")
        _check_index(db_path, fd, before)
        return {**metadata, "index_bytes": before[5], "read_only_index": True,
                "source_revalidation": "Selected sources are checked on each search; status does not rescan the full corpus."}
    finally:
        con.close()
        os.close(fd)


def _query(query):
    if not isinstance(query, str) or len(query.encode("utf-8")) > MAX_QUERY_BYTES:
        raise ValueError("Query exceeds the byte limit")
    terms = list(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)))
    if len(terms) > MAX_QUERY_TERMS:
        raise ValueError("Query exceeds the term limit")
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def search_index(db_path, query, *, limit=5, source_dir=None, language=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    if language not in {None, "ko", "en"}:
        raise ValueError("language must be ko or en")
    match = _query(query)
    con, db_fd, before, metadata = _open_index(db_path)
    root_fd = None
    started = time.monotonic()
    try:
        root = _source_dir(source_dir or os.environ.get("NOVA_GUIDELINES_DIR") or metadata["source_dir"])
        root_fd = _directory(root)
        root_identity = (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino)
        catalog_raw, _ = _read_source(root_fd, "catalog.csv")
        if sha256(catalog_raw).hexdigest() != metadata["catalog_sha256"]:
            raise ValueError("Source catalog changed since indexing")
        results = []
        if match:
            deadline = time.monotonic() + MAX_SEARCH_SECONDS
            con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            # Rank row IDs/scores first. Do not sort hundreds of MB of source text
            # for common terms. Temp storage is disk-backed, with a bounded cache.
            con.execute("PRAGMA temp_store=FILE")
            con.execute("PRAGMA cache_size=-16384")
            rows = con.execute("""
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
                SELECT c.document_id, c.text, c.byte_start, c.byte_end,
                    chosen.score, d.metadata_json, d.sha256,
                    d.source_stat_json, d.header
                FROM chosen JOIN chunks c ON c.id=chosen.chunk_id
                    JOIN documents d ON d.id=c.document_id
                ORDER BY chosen.score, c.document_id
            """, (match, language, language, limit))
            seen = set()
            for row in rows:
                if row["document_id"] in seen:
                    continue
                seen.add(row["document_id"])
                raw, _ = _read_source(root_fd, row["document_id"], json.loads(row["source_stat_json"]))
                if sha256(raw).hexdigest() != row["sha256"]:
                    raise ValueError("Source bytes changed since indexing")
                lo, hi = row["byte_start"], row["byte_end"]
                excerpt = row["text"]
                if not 0 <= lo < hi <= len(raw) or raw[lo:hi] != excerpt.encode("utf-8"):
                    raise ValueError("Excerpt and source bytes disagree")
                info = json.loads(row["metadata_json"])
                headers, header_raw, _, _ = _document(raw, info["catalog"])
                if headers != info["frontmatter"] or header_raw != row["header"]:
                    raise ValueError("Indexed source metadata changed")
                results.append({"document_id": row["document_id"], **headers,
                    "document_type": info["catalog"]["문서 유형"], "format": info["catalog"]["형식"],
                    "catalog_metadata": info["catalog"], "source_sha256": row["sha256"],
                    "snippet": excerpt, "byte_start": lo, "byte_end": hi,
                    "snippet_sha256": sha256(raw[lo:hi]).hexdigest(),
                    "bm25_rank_value": row["score"], "reference_scope": REFERENCE_SCOPE,
                    "patient_observation": False, "source_bytes_verified": True})
                if len(results) == limit:
                    break
        check_root = _directory(root)
        try:
            if (os.fstat(check_root).st_dev, os.fstat(check_root).st_ino) != root_identity:
                raise ValueError("Source directory changed during retrieval")
        finally:
            os.close(check_root)
        _check_index(db_path, db_fd, before)
        return {"query": query, "results": results, "result_count": len(results),
                "search_seconds": round(time.monotonic() - started, 6),
                "reference_scope": REFERENCE_SCOPE, "patient_observation": False,
                "corpus_manifest_sha256": metadata["corpus_manifest_sha256"],
                "zero_hits_means_unknown": True, "limitations": LIMITATIONS,
                "model_calls": 0, "network_calls": 0}
    finally:
        if root_fd is not None:
            os.close(root_fd)
        con.close()
        os.close(db_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--source", default=None)
    build.add_argument("--db", required=True)
    search = commands.add_parser("search")
    search.add_argument("--db", required=True)
    search.add_argument("--source", default=None)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--language", choices=("ko", "en"))
    status = commands.add_parser("status")
    status.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            value = build_index(args.source, args.db)
        elif args.command == "search":
            value = search_index(args.db, args.query, limit=args.limit,
                                 source_dir=args.source, language=args.language)
        else:
            value = index_status(args.db)
    except (ValueError, OSError, sqlite3.Error, UnicodeError) as exc:
        parser.exit(2, f"Guideline index error: {exc}\n")
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
