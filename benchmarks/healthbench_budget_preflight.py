"""Prepare a pinned development-only 2048 -> 4096 comparison without HTTP.

This is a provenance gate, not a generation runner or a medical evaluator. The
existing runner's fixed-low development-control gate is never invoked or changed.
Only hashes and scalar provenance are written; payloads and clinical text stay in
memory. An external SHA pin is mandatory, even when the source looks plausible.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

from .healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, ROOT,
                          arm_config, build_payload, canonical_hash, sum_usage, utc_now)
from .healthbench_codex_cycle import REVIEW_REQUIRED, _source_object, make_payload

ARM = 'grounded_medium_v1'
REVISION = 'grounded-v2'
DEVELOPMENT_LIMIT = 20
SOURCE_STATUS_COUNTS = {'completed': 17, 'failed': 3}
USAGE_KEYS = {'prompt_tokens', 'completion_tokens', 'total_tokens'}


def _require(condition, label: str) -> None:
    if not condition:
        raise ValueError(label)


def _equal(actual, expected, label: str) -> None:
    # JSON canonical equality distinguishes False from 0 and True from 1.
    _require(canonical_hash(actual) == canonical_hash(expected), label)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _valid_sha(value) -> bool:
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _path(path: Path) -> Path:
    _require(isinstance(path, Path), 'Source and output paths must be Path objects')
    absolute = Path(os.path.abspath(path))
    _require('..' not in path.parts, 'Path aliases are not permitted')
    _require(not any(part.is_symlink() for part in (absolute, *absolute.parents)),
             'Symlink aliases are not permitted')
    _require(absolute == absolute.resolve(), 'Path aliases are not permitted')
    return absolute


def _stamp(value) -> tuple:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            value.st_ctime_ns, value.st_nlink)


class _Snapshot:
    """Reject replacements, aliases and in-place changes across preparation."""

    def __init__(self, path: Path):
        self.path = _path(path)
        before = self.path.stat()
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1,
                 'Sources must be unaliased regular files')
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            _require(_stamp(opened) == _stamp(before), 'Source changed while opening')
            self.raw = stream.read()
            _require(_stamp(os.fstat(stream.fileno())) == _stamp(opened),
                     'Source changed while reading')
        self.stamp = _stamp(before)
        self.sha256 = _sha(self.raw)
        self.verify()

    def verify(self) -> None:
        _require(_path(self.path) == self.path and _stamp(self.path.stat()) == self.stamp,
                 'Source changed during preparation')
        _require(_sha(self.path.read_bytes()) == self.sha256,
                 'Source bytes changed during preparation')
        _require(_stamp(self.path.stat()) == self.stamp,
                 'Source changed during revalidation')


def _time(value, label: str) -> datetime:
    _require(isinstance(value, str), label)
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(label) from exc
    _require(result.utcoffset() is not None, label)
    return result


def _usage(value, label: str) -> dict:
    _require(isinstance(value, dict) and set(value) == USAGE_KEYS, label)
    _require(all(type(number) is int and number >= 0 for number in value.values()), label)
    _require(value['total_tokens'] == value['prompt_tokens'] + value['completion_tokens'], label)
    return value


def _inode(value) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _check_anchors(anchors: list[tuple]) -> None:
    for parent_fd, name, child_fd, identity in anchors:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require(stat.S_ISDIR(current.st_mode) and _inode(current) == identity
                 and _inode(os.fstat(child_fd)) == identity,
                 'Output directory ancestry changed during preparation')


def _publish_private(output: Path, result: dict, snapshots: list[_Snapshot]) -> None:
    """Walk nofollow directory FDs; cleanup only our captured inode identities.

    Absolute path rechecks alone cannot anchor writes against parent renames.
    All mkdir/open/write/unlink/rmdir operations below use pinned directory FDs.
    After inode capture, a renamed output's private file is cleaned through its
    FD; replacement writers' captured directory entries are not removed. POSIX
    mkdir does not atomically return a directory FD: a same-UID replacement
    before the first stat cannot be excluded by this portable publication gate.
    """
    descriptors, anchors, owned_directories = [], [], []
    output_fd = None
    manifest_identity = None
    try:
        root_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(root_fd)
        parent_fd = root_fd
        private_parts = _path(ROOT / 'results').parts
        for depth, name in enumerate(output.parts[1:], 1):
            final = depth == len(output.parts) - 1
            created = False
            if final:
                os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                created = True
                created_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                _require(stat.S_ISDIR(created_stat.st_mode) and stat.S_IMODE(created_stat.st_mode) == 0o700,
                         'New output directory identity or private permissions changed')
                created_identity = _inode(created_stat)
                owned_directories.append((parent_fd, name, created_identity))
            else:
                try:
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                except FileNotFoundError:
                    _require(depth >= len(private_parts) - 1,
                             'Repository ancestry must already exist')
                    try:
                        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                        created = True
                    except FileExistsError:
                        pass
                    if created:
                        created_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                        _require(stat.S_ISDIR(created_stat.st_mode) and stat.S_IMODE(created_stat.st_mode) == 0o700,
                                 'New output parent identity or private permissions changed')
                        created_identity = _inode(created_stat)
                        owned_directories.append((parent_fd, name, created_identity))
                    child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            if final:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
            descriptors.append(child_fd)
            identity = _inode(os.fstat(child_fd))
            anchors.append((parent_fd, name, child_fd, identity))
            if created:
                _require(identity == created_identity, 'New output directory changed before open')
                _require(stat.S_IMODE(os.fstat(child_fd).st_mode) == 0o700,
                         'New output directory private permissions changed before open')
            _check_anchors(anchors)
            parent_fd = child_fd
        output_fd = parent_fd
        _check_anchors(anchors)
        _require(not os.listdir(output_fd), 'Fresh output directory already contains another artifact')
        descriptor = os.open('manifest.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=output_fd)
        manifest_identity = _inode(os.fstat(descriptor))
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            _check_anchors(anchors)
            stream.write((json.dumps(result, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
            _check_anchors(anchors)
        for entry in snapshots:
            entry.verify()
        _check_anchors(anchors)
        _equal(sorted(os.listdir(output_fd)), ['manifest.json'], 'Output changed during publication')
    except BaseException:
        if output_fd is not None and manifest_identity is not None:
            try:
                existing = os.stat('manifest.json', dir_fd=output_fd, follow_symlinks=False)
                if stat.S_ISREG(existing.st_mode) and _inode(existing) == manifest_identity:
                    os.unlink('manifest.json', dir_fd=output_fd)
            except OSError:
                pass
        for parent_fd, name, identity in reversed(owned_directories):
            try:
                existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISDIR(existing.st_mode) and _inode(existing) == identity:
                    os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _dataset(snapshot: _Snapshot) -> tuple[list[dict], list[str]]:
    # Strict JSON parsing applies to every key, but only selected prompts are
    # inspected. Holdout questions and rubric contents are never processed.
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, 'Dataset contains duplicate JSON fields')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Dataset contains a nonfinite constant')
    content = json.loads(snapshot.raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    if isinstance(content, dict):
        content = content.get('data')
    _require(isinstance(content, list) and len(content) >= DEVELOPMENT_LIMIT,
             'Dataset needs at least 20 development records')
    ids = []
    for item in content:
        _require(isinstance(item, dict), 'Dataset record must be an object')
        prompt_id = item.get('prompt_id')
        _require(isinstance(prompt_id, str) and prompt_id.strip() == prompt_id and prompt_id,
                 'Dataset ID must be a nonempty unaliased string')
        ids.append(prompt_id)
    _require(len(set(ids)) == len(ids), 'Dataset contains duplicate IDs')
    selected = sorted(content, key=lambda item: item['prompt_id'])[:DEVELOPMENT_LIMIT]
    for item in selected:
        prompt = item.get('prompt')
        _require(isinstance(prompt, list) and prompt and isinstance(item.get('rubrics'), list),
                 'Development data requires original messages and a rubric list')
        for message in prompt:
            _require(isinstance(message, dict) and set(message) == {'role', 'content'}
                     and isinstance(message['role'], str) and message['role'] in {'system', 'user', 'assistant'}
                     and isinstance(message['content'], str) and message['content'].strip(),
                     'Development prompt has unsupported messages')
        _require(prompt[-1]['role'] == 'user', 'Development prompt must end in user role')
    return selected, sorted(ids)


def _validate_low_provenance(source: dict, comparison: dict, items: list[dict],
                             ids: list[str], all_ids: list[str], dataset_sha: str, digest: str) -> None:
    baseline = source.get('baseline')
    _require(isinstance(baseline, dict), 'Source baseline provenance is missing')
    control = baseline.get('development_control')
    _require(isinstance(control, dict), 'Frozen low control provenance is missing')
    comparison_baseline = comparison['sources'].get('baseline')
    _require(isinstance(comparison_baseline, dict), 'Comparison baseline provenance missing')
    _equal(control, comparison_baseline.get('validated_development_control'),
           'Comparison and source frozen low control provenance disagree')
    _equal(baseline.get('model'), {'name': DEFAULT_MODEL, 'digest': digest},
           'Frozen low control model disagrees')
    _require(baseline.get('dataset_sha256') == dataset_sha and _valid_sha(baseline.get('sha256'))
             and baseline.get('selected_prompt_ids_sha256') == canonical_hash(all_ids),
             'Frozen low control dataset provenance disagrees')
    _require(isinstance(baseline.get('path'), str)
             and comparison_baseline.get('manifest_path') == baseline['path']
             and comparison_baseline.get('answers_path') == str(Path(baseline['path']).parent / 'answers.jsonl')
             and comparison_baseline.get('manifest_byte_sha256') == baseline['sha256']
             and comparison_baseline.get('answers_byte_sha256') == control.get('answers_byte_sha256')
             and comparison_baseline.get('original_manifest_status') == baseline.get('status'),
             'Frozen low control source paths, byte hashes or original status disagree')
    _require(control.get('policy') == 'explicit_finalized_development_control'
             and control.get('original_manifest_status') == baseline.get('status')
             and baseline.get('status') in {'partial', 'completed'},
             'Frozen low control original status or policy disagrees')
    _time(control.get('finished_at'), 'Frozen low control must be finalized')
    _require(_valid_sha(control.get('summary_byte_sha256'))
             and _valid_sha(control.get('answers_byte_sha256')), 'Frozen low control byte hashes missing')
    _equal(control.get('validated_count'), 20, 'Frozen low control count differs')
    _equal(control.get('selected_prompt_ids'), ids, 'Frozen low control IDs differ')
    _require(control.get('arm') == 'grounded_call' and control.get('model_digest') == digest
             and control.get('arm_config_sha256') == canonical_hash(arm_config(('grounded_call',))['grounded_call']),
             'Frozen low control fixed-low gate differs')
    expected = []
    entries = control.get('row_provenance')
    _require(isinstance(entries, list) and len(entries) == 20, 'Frozen low control rows missing')
    for item, entry in zip(items, entries):
        payload = build_payload(item, 'grounded_call', DEFAULT_MODEL)
        _require(isinstance(entry, dict) and _valid_sha(entry.get('full_row_sha256')),
                 'Frozen low control row hash missing')
        expected.append({'prompt_id': item['prompt_id'], 'arm': 'grounded_call',
                         'original_status': 'completed', 'full_row_sha256': entry['full_row_sha256'],
                         'input_sha256': canonical_hash(payload['messages']),
                         'request_sha256': canonical_hash(payload)})
    _equal(entries, expected, 'Frozen low control completed row provenance differs')


def _validate_budget_source(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                             instructions_file: Path, out: Path, *,
                             expected_comparison_sha256: str,
                             expected_source_summary_sha256: str) -> tuple:
    """Validate the pinned 17+3 control and publish only prepare-only proof.

    There is intentionally no transport, model-metadata lookup, generation mode,
    retry option, source override or success-only selection path in this API.
    Use a fresh dedicated run directory with one preparing writer; portable
    POSIX publication cannot make mkdir and the first inode capture atomic.
    """
    _require(_valid_sha(expected_comparison_sha256), 'An external lowercase comparison SHA256 pin is required')
    _require(_valid_sha(expected_source_summary_sha256), 'An external lowercase source summary SHA256 pin is required')
    comparison_snapshot = _Snapshot(source_comparison_manifest)
    _require(comparison_snapshot.sha256 == expected_comparison_sha256,
             'Comparison raw bytes do not match the external SHA256 pin')
    comparison = _source_object(comparison_snapshot.raw)
    _require(isinstance(comparison.get('sources'), dict), 'Comparison requires source provenance objects')
    _require(comparison.get('status') == 'snapshot', 'Comparison must be a finalized snapshot')
    _equal(comparison.get('schema_version'), 1, 'Comparison schema differs')
    source_snapshot = _Snapshot(source_manifest)
    answers_snapshot = _Snapshot(source_snapshot.path.parent / 'answers.jsonl')
    summary_snapshot = _Snapshot(source_snapshot.path.parent / 'summary.json')
    _require(summary_snapshot.sha256 == expected_source_summary_sha256,
             'Source summary raw bytes do not match the external SHA256 pin')
    data_snapshot = _Snapshot(data)
    instructions_snapshot = _Snapshot(instructions_file)
    snapshots = [comparison_snapshot, source_snapshot, answers_snapshot, summary_snapshot,
                 data_snapshot, instructions_snapshot]
    _require(len({(entry.stamp[0], entry.stamp[1]) for entry in snapshots}) == len(snapshots),
             'Sources must be distinct files without aliases')
    source = _source_object(source_snapshot.raw)
    summary = _source_object(summary_snapshot.raw)
    pinned_source = comparison['sources'].get('candidate')
    _equal(pinned_source, {'manifest_byte_sha256': source_snapshot.sha256,
                          'answers_byte_sha256': answers_snapshot.sha256,
                          'original_manifest_status': 'partial',
                          'manifest_path': str(source_snapshot.path),
                          'answers_path': str(answers_snapshot.path)},
           'Source does not match the externally pinned comparison candidate')
    _require(source.get('status') == 'partial' and source.get('error') is None,
             'Original source must remain the finalized partial run')
    _equal(source.get('schema_version'), 1, 'Source schema differs')
    started = _time(source.get('started_at'), 'Source started_at is invalid')
    finished = _time(source.get('finished_at'), 'Source finished_at is invalid')
    _require(finished >= started, 'Source finalization precedes its start')
    items, all_ids = _dataset(data_snapshot)
    ids = [item['prompt_id'] for item in items]
    prompt_hashes = {item['prompt_id']: canonical_hash(item['prompt']) for item in items}
    metadata = source.get('dataset')
    _require(isinstance(metadata, dict), 'Source dataset provenance missing')
    _require(metadata.get('path') == str(data_snapshot.path) and metadata.get('sha256') == data_snapshot.sha256,
             'Source dataset path or raw byte hash differs')
    _equal(metadata.get('total_records'), len(all_ids), 'Source dataset count differs')
    _equal(metadata.get('selected_prompt_ids'), ids, 'Source dataset development IDs differ')
    _equal(metadata.get('selected_prompt_sha256'), prompt_hashes, 'Source prompt hashes differ')
    _equal(source.get('selected_prompt_ids'), ids, 'Source development selection differs')
    _equal(source.get('selected_arms'), [ARM], 'Source arm identity differs')
    _equal(source.get('split'), {'name': 'development', 'limit': 20,
                               'selection': 'first 20 frozen baseline IDs; heldout excluded'},
           'Source must use the fixed development20 split')
    _equal(comparison.get('dataset'), metadata, 'Comparison dataset provenance differs')
    _equal(comparison.get('selected_prompt_ids'), ids, 'Comparison development IDs differ')
    _equal(comparison.get('selected_arms'), ['grounded_call', ARM], 'Comparison arm aliases differ')
    for key in ('planned_answers', 'written_answers'):
        _equal(comparison.get(key), 40, 'Comparison must retain all 40 original rows')
    for value in (source, summary, comparison):
        _require(value.get('score') is None and value.get('score_status') == REVIEW_REQUIRED,
                 'Source scores must remain unjudged')
    original_bytes = GROUNDED_SUFFIX.encode('utf-8')
    _require(instructions_snapshot.raw == original_bytes,
             'Instructions must be the exact frozen original GROUNDED_SUFFIX bytes')
    instructions = source.get('instructions')
    _require(isinstance(instructions, dict) and instructions.get('name') == ARM
             and instructions.get('version') == REVISION
             and instructions.get('byte_sha256') == instructions_snapshot.sha256
             and instructions.get('git_tracked') is True,
             'Source frozen instruction identity differs')
    instruction_path = instructions.get('path')
    _require(isinstance(instruction_path, str) and instruction_path
             and not Path(instruction_path).is_absolute() and '..' not in Path(instruction_path).parts
             and str((ROOT / instruction_path).absolute()) == str(instructions_snapshot.path),
             'Source instruction path differs')
    expected_arm = {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium',
                    'max_attempts': 1, 'suffix': GROUNDED_SUFFIX, 'prompt_revision': REVISION}
    _equal(source.get('arms'), {ARM: expected_arm}, 'Source fixed medium2048 configuration differs')
    _equal(comparison.get('arms'), {'grounded_call': arm_config(('grounded_call',))['grounded_call'],
                                   ARM: expected_arm}, 'Comparison fixed arm configurations differ')
    _equal(source.get('config'), {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL,
                                 'timeout': 300, 'prepare_only': False, 'maximum_calls_per_case': 1,
                                 'resume': False, 'allow_finalized_dev_control': True},
           'Source endpoint, settings or call policy differs')
    model = source.get('model')
    _require(isinstance(model, dict) and model.get('name') == DEFAULT_MODEL
             and model.get('metadata_status') == 'available' and _valid_sha(model.get('digest')),
             'Source recorded model digest is missing or invalid')
    digest = model['digest']
    _validate_low_provenance(source, comparison, items, ids, all_ids, data_snapshot.sha256, digest)
    provenance = metadata.get('provenance')
    if isinstance(provenance, dict) and provenance.get('status') == 'validated':
        sidecar = _Snapshot(data_snapshot.path.with_suffix('.manifest.json'))
        snapshots.append(sidecar)
        _require(provenance.get('sidecar') == str(sidecar.path)
                 and provenance.get('sidecar_sha256') == sidecar.sha256,
                 'Dataset provenance sidecar changed')
        sidecar_value = _source_object(sidecar.raw)
        _require(sidecar_value.get('converted_sha256') == data_snapshot.sha256,
                 'Dataset provenance sidecar raw hash differs')
        for key, value in sidecar_value.items():
            if key in provenance:
                _equal(provenance[key], value, 'Dataset provenance sidecar fields differ')
    payloads = [make_payload(item, GROUNDED_SUFFIX, DEFAULT_MODEL, reasoning_effort='medium') for item in items]
    prepared = [{'prompt_id': item['prompt_id'], 'prompt_sha256': prompt_hashes[item['prompt_id']],
                 'input_sha256': canonical_hash(payload['messages']), 'request_sha256': canonical_hash(payload)}
                for item, payload in zip(items, payloads)]
    _equal(source.get('prepared_requests'), prepared, 'Source prepared request/input hashes differ')
    rows = [_source_object(line.encode('utf-8')) for line in answers_snapshot.raw.decode('utf-8').splitlines()
            if line.strip()]
    _require(len(rows) == 20 and [row.get('prompt_id') for row in rows] == ids,
             'Source rows must be exactly the development20 IDs once each in original order')
    comparison_rows = comparison.get('row_provenance')
    _require(isinstance(comparison_rows, list) and len(comparison_rows) == 40,
             'Comparison row provenance must retain every original row')
    expected_provenance = []
    low_entries = source['baseline']['development_control']['row_provenance']
    proofs, failures = [], []
    for item, payload, row, request, low_entry in zip(items, payloads, rows, prepared, low_entries):
        _require(row.get('arm') == ARM and row.get('model') == DEFAULT_MODEL
                 and row.get('model_digest') == digest and row.get('dataset_sha256') == data_snapshot.sha256
                 and row.get('prompt_sha256') == request['prompt_sha256']
                 and row.get('request_sha256') == request['request_sha256']
                 and row.get('input_sha256') == request['input_sha256']
                 and row.get('instructions_byte_sha256') == instructions_snapshot.sha256
                 and row.get('instructions_name') == ARM and row.get('instructions_version') == REVISION,
                 'Source row input/request/instruction/model identity differs')
        attempts = row.get('attempts')
        _require(isinstance(attempts, list) and len(attempts) == 1 and isinstance(attempts[0], dict)
                 and type(row.get('selected_attempt')) is int and row['selected_attempt'] == 1
                 and row.get('fallback_used') is False,
                 'Source must preserve exactly one selected attempt without fallback')
        attempt = attempts[0]
        _require(type(attempt.get('number')) is int and attempt['number'] == 1
                 and attempt.get('response_received') is True,
                 'Source requires an actual received first response')
        for key in ('max_tokens', 'temperature', 'reasoning_effort'):
            _equal(attempt.get(key), payload[key], 'Source attempt inference settings differ')
        for key in ('request_sha256', 'input_sha256'):
            _require(attempt.get(key) == request[key], 'Source attempt request/input hash differs')
        _require(isinstance(row.get('answer'), str), 'Source answer must be text')
        for key in ('answer', 'status', 'finish_reason', 'error', 'usage', 'seconds'):
            _equal(row.get(key), attempt.get(key), 'Source selected attempt and row disagree')
        usage = _usage(attempt.get('usage'), 'Source token usage is invalid')
        seconds = attempt.get('seconds')
        _require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0,
                 'Source attempt seconds must be finite and nonnegative')
        _require(row.get('score') is None and row.get('score_status') == REVIEW_REQUIRED,
                 'Source row score must remain unjudged')
        if row.get('status') == 'completed':
            _require(row['answer'].strip() and row.get('finish_reason') == 'stop' and row.get('error') is None,
                     'Completed control requires a nonempty stopped answer without error')
        elif row.get('status') == 'failed':
            _require(not row['answer'].strip() and row.get('finish_reason') == 'length'
                     and usage['completion_tokens'] == 2048,
                     'Failure requires recorded length exhaustion with an empty final answer and 2048 tokens')
            _equal(row.get('error'), {'code': 'empty_answer', 'http_status': None},
                   'Failure requires the actual empty_answer error evidence')
            failures.append({'prompt_id': item['prompt_id'], 'original_status': 'failed',
                             'failure_code': 'empty_answer', 'inference_failure_utility_score': 0,
                             'rubric_labels_not_predicted': True})
        else:
            raise ValueError('Only completed or evidenced empty_answer failed source rows are permitted')
        full_hash = canonical_hash(row)
        for label, arm, original_status, row_hash in (
                ('baseline', 'grounded_call', 'completed', low_entry['full_row_sha256']),
                ('candidate', ARM, row['status'], full_hash)):
            expected_provenance.append({'source': label, 'arm': arm, 'prompt_id': item['prompt_id'],
                                        'original_status': original_status, 'full_row_sha256': row_hash})
        candidate = deepcopy(payload)
        candidate['max_tokens'] = 4096
        changed = sorted(key for key in set(payload) | set(candidate)
                         if canonical_hash(payload.get(key)) != canonical_hash(candidate.get(key)))
        _equal(changed, ['max_tokens'], 'Candidate may change only max_tokens')
        _require(canonical_hash(candidate['messages']) == request['input_sha256'],
                 'Candidate input must equal the original input')
        proofs.append({'prompt_id': item['prompt_id'], 'arm': ARM, 'original_status': row['status'],
                       'full_row_sha256': full_hash, 'prompt_sha256': request['prompt_sha256'],
                       'input_sha256': request['input_sha256'], 'control_request_sha256': request['request_sha256'],
                       'candidate_request_sha256': canonical_hash(candidate), 'changed_fields': changed})
    _equal(comparison_rows, expected_provenance, 'Source full row hashes or original statuses differ from pinned comparison')
    counts = dict(Counter(row['status'] for row in rows))
    _equal(counts, SOURCE_STATUS_COUNTS, 'Original control must retain 17 completed and 3 known failed rows')
    _equal(summary.get('answer_status_counts'), counts, 'Source summary status counts differ')
    for key, expected in {'status': 'partial', 'started_at': source['started_at'],
                          'finished_at': source['finished_at'], 'selected_prompt_ids': ids,
                          'selected_arms': [ARM], 'planned_answers': 20, 'written_answers': 20,
                          'unattempted_answers': 0, 'calls': 20, 'error': None}.items():
        _equal(summary.get(key), expected, 'Source summary finalization/counts/calls disagree')
    _usage(summary.get('usage'), 'Source summary usage is invalid')
    _equal(summary['usage'], sum_usage([row['attempts'][0] for row in rows]), 'Source summary token usage differs')
    _require(type(summary.get('seconds')) in (int, float) and math.isfinite(summary['seconds'])
             and summary['seconds'] >= 0, 'Source summary seconds are invalid')
    output = _path(out)
    private_root = _path(ROOT / 'results')
    _require(output != private_root and output.is_relative_to(private_root),
             'Output must be a dedicated private results directory')
    _require(not output.exists(), 'Output must be fresh; overwrite and resume are forbidden')
    _require(not any(entry.path.is_relative_to(output) for entry in snapshots), 'Output cannot contain sources')
    result = {'schema_version': 1, 'status': 'prepared', 'prepared_at': utc_now(),
              'mode': 'local_prepare_only', 'calls': 0, 'generation_calls': 0, 'model_metadata_calls': 0,
              'split': {'name': 'development', 'limit': 20, 'heldout_included': False},
              'selected_prompt_ids': ids, 'dataset_byte_sha256': data_snapshot.sha256,
              'instructions_byte_sha256': instructions_snapshot.sha256, 'instructions_revision': REVISION,
              'model': {'name': DEFAULT_MODEL, 'digest': digest, 'metadata_source': 'pinned source; no HTTP'},
              'endpoint': DEFAULT_BASE_URL, 'arm': ARM, 'reasoning_effort': 'medium', 'temperature': 0,
              'max_attempts': 1, 'control_max_tokens': 2048, 'candidate_max_tokens': 4096,
              'changed_fields': ['max_tokens'], 'known_failure_count': len(failures), 'source_status_counts': counts,
              'original_manifest_status': source['status'], 'source_finished_at': source['finished_at'],
              'source_byte_hashes': {'comparison_manifest': comparison_snapshot.sha256,
                                     'source_manifest': source_snapshot.sha256, 'source_answers': answers_snapshot.sha256,
                                     'source_summary': summary_snapshot.sha256},
              'external_comparison_sha256_pin': expected_comparison_sha256,
              'external_source_summary_sha256_pin': expected_source_summary_sha256,
              'row_provenance': proofs, 'known_inference_failures': failures,
              'medical_judgments': 'not_performed; recorded inference failures retain utility zero separately',
              'criteria_predicted': False, 'score': None, 'score_status': REVIEW_REQUIRED,
              'execution_support': {'ready': False, 'status': 'NOT_READY',
                                    'reason': 'existing generation runner is fixed at max_tokens=2048'},
              'fixed_low_control_gate': 'unchanged'}
    for entry in snapshots:
        entry.verify()
    return result, snapshots, items, rows, source, comparison


def prepare_budget_preflight(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                             instructions_file: Path, out: Path, *,
                             expected_comparison_sha256: str,
                             expected_source_summary_sha256: str) -> dict:
    """Keep the legacy hashes-only prepare API and fixed-runner NOT_READY proof."""
    result, snapshots, _, _, _, _ = _validate_budget_source(
        data, source_manifest, source_comparison_manifest, instructions_file, out,
        expected_comparison_sha256=expected_comparison_sha256,
        expected_source_summary_sha256=expected_source_summary_sha256)
    output = _path(out)
    _publish_private(output, result, snapshots)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'source-manifest', 'source-comparison-manifest', 'instructions-file', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--expected-comparison-sha256', required=True,
                        help='Externally pinned SHA256 of the original comparison manifest raw bytes')
    parser.add_argument('--expected-source-summary-sha256', required=True,
                        help='Externally pinned SHA256 of the original source summary raw bytes')
    args = parser.parse_args(argv)
    try:
        result = prepare_budget_preflight(args.data, args.source_manifest, args.source_comparison_manifest,
                                          args.instructions_file, args.out,
                                          expected_comparison_sha256=args.expected_comparison_sha256,
                                          expected_source_summary_sha256=args.expected_source_summary_sha256)
    except (ValueError, OSError, UnicodeError) as exc:
        print('Budget preflight rejected: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({'status': result['status'], 'calls': result['calls'],
                      'known_failure_count': result['known_failure_count'],
                      'execution_support': result['execution_support']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
