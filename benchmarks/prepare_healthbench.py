"""Prepare local HealthBench data without downloading or calling a model.

Legacy archives lost the original message roles and prompt IDs. Their adapter
preserves flattened history as a single user message; it cannot restore the
canonical benchmark. Canonical Main selection retains every original field.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile


HISTORY_PREFIX = 'Prior conversation saved as text (roles are labels in this archive):\n'
LICENSE_PATH = Path(__file__).with_name('LICENSE-HealthBench.txt')


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def read_json(path: Path):
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('Input file is not valid JSON') from None
    return value, raw


def read_main_jsonl(path: Path) -> tuple[list[dict], bytes]:
    raw = path.read_bytes()
    try:
        records = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise ValueError('Main file must contain valid JSONL records') from None
    return records, raw


def validate_rubrics(rubrics, record_number: int) -> None:
    if not isinstance(rubrics, list) or not rubrics:
        raise ValueError(f'Record {record_number} requires a nonempty rubrics list')
    for rubric in rubrics:
        if (not isinstance(rubric, dict)
                or not isinstance(rubric.get('criterion'), str)
                or not rubric['criterion'].strip()
                or isinstance(rubric.get('points'), bool)
                or not isinstance(rubric.get('points'), (int, float))
                or not math.isfinite(rubric['points'])):
            raise ValueError(f'Record {record_number} has an invalid rubric')


def convert_legacy(records: list[dict]) -> tuple[list[dict], dict]:
    if not isinstance(records, list) or not records:
        raise ValueError('Legacy input must be a nonempty JSON array')
    converted, seen = [], set()
    history_count = 0
    for number, item in enumerate(records, 1):
        if not isinstance(item, dict):
            raise ValueError(f'Legacy record {number} is not an object')
        qid, text, history = item.get('qid'), item.get('text'), item.get('history', '')
        if not isinstance(qid, str) or not qid.strip():
            raise ValueError(f'Legacy record {number} requires a nonempty qid')
        if qid in seen:
            raise ValueError('Legacy input contains duplicate qid values')
        seen.add(qid)
        if not isinstance(text, str) or not text.strip() or not isinstance(history, str):
            raise ValueError(f'Legacy record {number} requires string text and history')
        if not isinstance(item.get('canary'), str):
            raise ValueError(f'Legacy record {number} requires a string canary')
        validate_rubrics(item.get('official_rubrics'), number)
        prompt = []
        if history:
            history_count += 1
            prompt.append({'role': 'user', 'content': HISTORY_PREFIX + history})
        prompt.append({'role': 'user', 'content': text})
        converted.append({'canary': item['canary'], 'prompt_id': qid,
                          'prompt': prompt, 'rubrics': deepcopy(item['official_rubrics'])})
    return converted, {
        'dataset_kind': f'legacy_consensus_val_{len(converted)}',
        'normalized_context': True,
        'lost_original_prompt_ids': True,
        'canonical_score_comparable': False,
        'context_adapter': 'flattened history retained verbatim as an earlier user message',
        'selection': 'all legacy records in source order; qid retained as prompt_id',
        'count': len(converted),
        'rubric_count': sum(len(item['rubrics']) for item in converted),
        'history_count': history_count,
    }


def select_main(records: list[dict], ids_document: dict) -> tuple[list[dict], dict]:
    if not isinstance(ids_document, dict):
        raise ValueError('IDs file must be a JSON object containing prompt_ids')
    ids = ids_document.get('prompt_ids')
    if (not isinstance(ids, list) or not ids
            or any(not isinstance(pid, str) or not pid.strip() for pid in ids)):
        raise ValueError('IDs file requires a nonempty list of string prompt_ids')
    if len(ids) != len(set(ids)):
        raise ValueError('IDs file contains duplicate prompt_id values')
    if not isinstance(records, list) or not records:
        raise ValueError('Main input must contain records')
    by_id = {}
    for number, item in enumerate(records, 1):
        if not isinstance(item, dict):
            raise ValueError(f'Main record {number} is not an object')
        pid = item.get('prompt_id')
        if not isinstance(pid, str) or not pid.strip():
            raise ValueError(f'Main record {number} requires a nonempty prompt_id')
        if pid in by_id:
            raise ValueError('Main input contains duplicate prompt_id values')
        by_id[pid] = item
    missing = set(ids) - by_id.keys()
    if missing:
        raise ValueError(f'{len(missing)} requested prompt IDs are missing from Main')
    selected = []
    for number, pid in enumerate(sorted(ids), 1):
        item = by_id[pid]
        prompt = item.get('prompt')
        if not isinstance(prompt, list) or not prompt:
            raise ValueError(f'Selected Main record {number} requires prompt messages')
        for message in prompt:
            if (not isinstance(message, dict)
                    or message.get('role') not in {'system', 'user', 'assistant'}
                    or not isinstance(message.get('content'), str)
                    or not message['content'].strip()):
                raise ValueError(f'Selected Main record {number} has an invalid message')
        if prompt[-1]['role'] != 'user':
            raise ValueError(f'Selected Main record {number} must end in a user message')
        validate_rubrics(item.get('rubrics'), number)
        selected.append(deepcopy(item))
    return selected, {
        'dataset_kind': f'conquer_val_{len(selected)}',
        'normalized_context': False,
        'lost_original_prompt_ids': False,
        'canonical_score_comparable': True,
        'selection': 'Main prompt_id intersection with explicit IDs; prompt_id ascending',
        'selection_record': deepcopy({key: value for key, value in ids_document.items()
                                      if key != 'prompt_ids'}),
        'source_count': len(records),
        'count': len(selected),
        'rubric_count': sum(len(item['rubrics']) for item in selected),
        'history_count': sum(len(item['prompt']) > 1 for item in selected),
    }


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def prepare(*, out: Path, legacy_file: Path | None = None,
            main_file: Path | None = None, ids_file: Path | None = None) -> dict:
    if (legacy_file is None) == (main_file is None):
        raise ValueError('Choose exactly one of legacy_file or main_file')
    if legacy_file is not None and ids_file is not None:
        raise ValueError('ids_file is only supported with main_file')
    if main_file is not None and ids_file is None:
        raise ValueError('main_file requires ids_file')
    license_raw = LICENSE_PATH.read_bytes()
    manifest_path = out.with_suffix('.manifest.json')
    copies = {out.parent / 'LICENSE-HealthBench.txt': license_raw}
    if legacy_file is not None:
        records, source_raw = read_json(legacy_file)
        converted, metadata = convert_legacy(records)
        source_path = legacy_file
        source_copy = out.with_suffix('.source.json')
        copies[source_copy] = source_raw
        metadata['source_copy'] = source_copy.name
    else:
        records, source_raw = read_main_jsonl(main_file)
        ids_document, ids_raw = read_json(ids_file)
        converted, metadata = select_main(records, ids_document)
        source_path = main_file
        ids_copy = out.parent / 'conquer_val_ids.json'
        if ids_copy.resolve() == ids_file.resolve():
            if ids_copy.read_bytes() != ids_raw:
                raise ValueError('IDs file changed while preparing artifacts')
            # The private IDs archive is already in place. Reuse its exact bytes.
        else:
            copies[ids_copy] = ids_raw
        metadata.update(ids_sha256=sha256(ids_raw), ids_copy=ids_copy.name)
    converted_raw = json_bytes(converted)
    metadata.update(schema_version=1, source_path=str(source_path.resolve()),
                    source_sha256=sha256(source_raw), converted_sha256=sha256(converted_raw),
                    converted_bytes=len(converted_raw), output_file=out.name,
                    license_file='LICENSE-HealthBench.txt', license_sha256=sha256(license_raw))
    targets = [out, manifest_path, *copies]
    if len({str(path.resolve()) for path in targets}) != len(targets):
        raise ValueError('Output path collides with a companion artifact')
    inputs = {str(path.resolve()) for path in (legacy_file, main_file, ids_file) if path is not None}
    if any(str(path.resolve()) in inputs for path in targets):
        raise ValueError('An output artifact would overwrite an input file')
    for path, raw in copies.items():
        atomic_write(path, raw)
    atomic_write(out, converted_raw)
    atomic_write(manifest_path, json_bytes(metadata))
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--legacy-file', type=Path)
    source.add_argument('--main-file', type=Path)
    parser.add_argument('--ids-file', type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        metadata = prepare(out=args.out, legacy_file=args.legacy_file,
                           main_file=args.main_file, ids_file=args.ids_file)
    except (ValueError, OSError) as exc:
        print(f'Preparation failed: {exc}', file=sys.stderr)
        return 2
    # Only artifact metadata is printed, never prompts, rubrics, canaries or IDs.
    print(json.dumps({key: metadata[key] for key in ('dataset_kind', 'count', 'rubric_count',
                                                   'history_count', 'converted_sha256')},
                     sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
