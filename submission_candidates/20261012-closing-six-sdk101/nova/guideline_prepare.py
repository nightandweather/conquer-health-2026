"""Prepare a fresh private guideline index without model or network calls.

The caller supplies an existing canonical absolute output root, directly or via
NOVA_OUTPUT_DIR. Linux /tmp is supported; resolve a trusted platform alias such
as macOS /tmp deliberately before passing it. Symlink path components are never
resolved implicitly. Shared writable roots must have the sticky bit set.

This adapter records capability/build failures instead of returning an empty
search fallback. It does not enforce CPU, memory, elapsed-time or disk limits and
does not establish NOVA server readiness or clinical retrieval quality. Directory
descriptors and identity checks reduce replacement risks, but cannot exclude a
hostile same-UID replacement between mkdir and the first inode observation or
between later path checks. Use one writer and keep the allocated directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from . import guidelines


class GuidelinePreparationError(RuntimeError):
    """Preparation failed; work_dir contains available private evidence, if set."""

    def __init__(self, stage, work_dir, cause):
        self.stage = stage
        self.work_dir = str(work_dir) if work_dir is not None else None
        self.evidence_path = (str(work_dir / "prepare-failed.json")
                              if work_dir is not None else None)
        self.evidence_write_error = None
        super().__init__(f"Offline guideline preparation failed at {stage}: "
                         f"{type(cause).__name__}: {cause}; work_dir={self.work_dir}")


def _canonical_directory(value, name):
    if not value:
        raise ValueError(f"{name} must name an existing canonical absolute directory")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != os.fspath(value):
        raise ValueError(f"{name} must be a canonical absolute path")
    return path


def _identity(fd):
    value = os.fstat(fd)
    return value.st_dev, value.st_ino


def _check_path(path, fd):
    current = guidelines._directory(path)
    try:
        if _identity(current) != _identity(fd):
            raise ValueError("Preparation directory was replaced")
    finally:
        os.close(current)


def _write_receipt(fd, name, value):
    output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=fd)
    with os.fdopen(output, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)


def _sqlite_capabilities():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        connection.execute("INSERT INTO probe(text) VALUES('synthetic')")
        if connection.execute("SELECT count(*) FROM probe WHERE probe MATCH 'synthetic'").fetchone() != (1,):
            raise RuntimeError("SQLite FTS5 verification failed")
        if connection.execute("SELECT json_extract('{\"probe\":1}', '$.probe')").fetchone() != (1,):
            raise RuntimeError("SQLite json_extract verification failed")
        if connection.execute("WITH probe AS MATERIALIZED (SELECT 1 AS value) SELECT value FROM probe").fetchone() != (1,):
            raise RuntimeError("SQLite MATERIALIZED CTE verification failed")
        return {"sqlite_version": sqlite3.sqlite_version, "fts5": True,
                "json_extract": True, "materialized_cte": True}
    finally:
        connection.close()


def _validated_aggregate(built, status, source, database):
    for key in ("document_count", "chunk_count", "source_markdown_bytes", "index_bytes"):
        if (type(built.get(key)) is not int or built[key] < 1
                or built[key] != status.get(key)):
            raise ValueError(f"Build/status aggregate mismatch: {key}")
    if built["chunk_count"] < built["document_count"]:
        raise ValueError("Each guideline must have at least one chunk")
    for key in ("corpus_manifest_sha256", "catalog_sha256"):
        if (not isinstance(built.get(key), str)
                or re.fullmatch(r"[0-9a-f]{64}", built[key]) is None
                or built[key] != status.get(key)):
            raise ValueError(f"Build/status hash mismatch: {key}")
    if (built.get("source_dir") != str(source) or status.get("source_dir") != str(source)
            or built.get("index_path") != str(database)
            or built.get("schema_version") != guidelines.SCHEMA_VERSION
            or status.get("schema_version") != guidelines.SCHEMA_VERSION
            or built.get("reference_scope") != guidelines.REFERENCE_SCOPE
            or status.get("reference_scope") != guidelines.REFERENCE_SCOPE
            or built.get("patient_observation") is not False
            or status.get("patient_observation") is not False
            or built.get("source_read_only") is not True
            or status.get("read_only_index") is not True
            or built.get("model_calls") != 0 or built.get("network_calls") != 0):
        raise ValueError("Build/status provenance mismatch")
    st = database.stat(follow_symlinks=False)
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o400 or st.st_size != built["index_bytes"]):
        raise ValueError("The prepared index must be an owner-only read-only regular file")
    return {key: built[key] for key in (
        "document_count", "chunk_count", "source_markdown_bytes", "index_bytes",
        "corpus_manifest_sha256", "catalog_sha256",
    )}


def prepare_offline_guidelines(output_root=None):
    """Build once from NOVA_GUIDELINES_DIR in a new 0700 work directory.

    output_root falls back only to NOVA_OUTPUT_DIR, never to an implicit location.
    Each call is a fresh preparation; an existing index is neither reused nor
    replaced. Failures raise GuidelinePreparationError with the original cause
    and private evidence location. Returned data contains paths/aggregates only.
    """
    started = time.monotonic()
    stage, work_dir = "configuration", None
    source_fd = root_fd = work_fd = None
    try:
        source = _canonical_directory(os.environ.get("NOVA_GUIDELINES_DIR"),
                                      "NOVA_GUIDELINES_DIR")
        root = _canonical_directory(output_root if output_root is not None
                                    else os.environ.get("NOVA_OUTPUT_DIR"), "output_root")
        if source == root or source in root.parents:
            raise ValueError("Output root must be outside the read-only source corpus")
        stage = "directory-validation"
        source_fd = guidelines._directory(source)
        root_fd = guidelines._directory(root)
        root_st = os.fstat(root_fd)
        if (stat.S_IMODE(root_st.st_mode) & 0o022) and not (root_st.st_mode & stat.S_ISVTX):
            raise ValueError("A shared writable output root requires the sticky bit")
        stage = "allocate-private-directory"
        name = "nova-guidelines-" + uuid.uuid4().hex
        os.mkdir(name, 0o700, dir_fd=root_fd)
        work_dir = root / name
        work_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
        work_st = os.fstat(work_fd)
        if work_st.st_uid != os.getuid() or stat.S_IMODE(work_st.st_mode) != 0o700:
            raise ValueError("Prepared work directory must be owned by this user with mode 0700")
        _check_path(root, root_fd)
        _check_path(work_dir, work_fd)
        database = work_dir / "guidelines.sqlite"
        base = {"schema_version": 1, "source_dir": str(source), "work_dir": str(work_dir),
                "index_path": str(database), "model_calls": 0, "network_calls": 0,
                "resource_limits_enforced": False, "nova_server_validated": False,
                "clinical_quality_verified": False}
        _write_receipt(work_fd, "prepare-started.json", {**base, "status": "started"})
        stage = "sqlite-capabilities"
        capabilities = _sqlite_capabilities()
        stage = "build-index"
        _check_path(root, root_fd)
        _check_path(source, source_fd)
        _check_path(work_dir, work_fd)
        built = guidelines.build_index(source, database)
        stage = "verify-index"
        _check_path(root, root_fd)
        _check_path(source, source_fd)
        _check_path(work_dir, work_fd)
        aggregate = _validated_aggregate(built, guidelines.index_status(database), source, database)
        result = {**base, "status": "prepared", "sqlite_capabilities": capabilities,
                  "aggregate": aggregate, "elapsed_seconds": round(time.monotonic() - started, 6),
                  "receipt_path": str(work_dir / "prepare-complete.json")}
        _write_receipt(work_fd, "prepare-complete.json", result)
        _check_path(root, root_fd)
        _check_path(work_dir, work_fd)
        return result
    except Exception as cause:
        error = GuidelinePreparationError(stage, work_dir, cause)
        if work_fd is not None:
            try:
                _write_receipt(work_fd, "prepare-failed.json", {
                    "status": "failed", "stage": stage, "work_dir": str(work_dir),
                    "exception_type": type(cause).__name__, "exception_message": str(cause),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "model_calls": 0, "network_calls": 0,
                    "resource_limits_enforced": False, "nova_server_validated": False,
                })
                _check_path(root, root_fd)
                _check_path(work_dir, work_fd)
            except Exception as receipt_error:
                error.evidence_write_error = f"{type(receipt_error).__name__}: {receipt_error}"
                error.evidence_path = None
        else:
            error.evidence_path = None
        raise error from cause
    finally:
        for fd in (work_fd, root_fd, source_fd):
            if fd is not None:
                os.close(fd)
