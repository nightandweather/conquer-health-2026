"""Export supplied, blinded Codex reviews without inference or automatic judging.

All inputs are validated before any output writes. Answers are read as one byte
snapshot while the generation worker may append later. Each output file is
published atomically; unresolved rubric labels remain absent from judgments.
Only explicitly supplied shards are used, never a directory glob.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .healthbench import canonical_hash, load_dataset
from . import healthbench_review as packets
from .healthbench_score import _answer_index, _data_index, _manifest_selection, build_summary


def _json(raw: bytes, label: str) -> dict:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'{label}: duplicate JSON key')
            result[key] = value
        return result
    try:
        value = json.loads(raw, object_pairs_hook=unique_keys)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f'{label}: invalid JSON') from exc
    if not isinstance(value, dict):
        raise ValueError(f'{label}: expected an object')
    return value


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _valid_sha(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(character in '0123456789abcdef' for character in value))


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{path.name}: expected a regular file without symlinks')
    return path.read_bytes()


def _text(value, label: str, *, empty: bool = False, limit: int = 10000) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f'{label}: invalid text')
    return value


def _sources(value, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError(f'{label}: expected a bounded sources list')
    return [_text(source, label, limit=2000) for source in value]


def _validate_candidate(candidate: dict, expected: set[int]) -> dict:
    if (not isinstance(candidate, dict) or set(candidate) != {'judgments', 'flags', 'note'}
            or not isinstance(candidate['judgments'], list)
            or not isinstance(candidate['flags'], list) or len(candidate['flags']) > 30):
        raise ValueError('Candidate review requires judgments, flags and note')
    _text(candidate['note'], 'candidate.note', empty=True)
    seen = set()
    for judgment in candidate['judgments']:
        if (not isinstance(judgment, dict) or not {'rubric_index', 'criteria_met'}.issubset(judgment)
                or set(judgment) - {'rubric_index', 'criteria_met', 'explanation', 'sources'}):
            raise ValueError('Invalid rubric judgment fields')
        index = judgment['rubric_index']
        if isinstance(index, bool) or not isinstance(index, int) or index not in expected or index in seen:
            raise ValueError('Rubric indices must cover the exact packet set once')
        seen.add(index)
        label = judgment['criteria_met']
        if label is not None and not isinstance(label, bool):
            raise ValueError('criteria_met must be a JSON boolean or null')
        _text(judgment.get('explanation', ''), 'judgment.explanation', empty=True)
        _sources(judgment.get('sources', []), 'judgment.sources')
    if seen != expected:
        raise ValueError('Rubric indices must cover the exact packet set once')
    for flag in candidate['flags']:
        if (not isinstance(flag, dict) or not {'category', 'explanation'}.issubset(flag)
                or set(flag) - {'category', 'explanation', 'sources'}):
            raise ValueError('Invalid concrete review flag')
        _text(flag['category'], 'flag.category', limit=100)
        _text(flag['explanation'], 'flag.explanation')
        _sources(flag.get('sources', []), 'flag.sources')
    return candidate


def _publish(outputs: dict[Path, bytes]) -> None:
    """Stage every file first, then atomically replace each completed artifact."""
    staged = []
    try:
        for path, raw in outputs.items():
            path.parent.chmod(0o700)
            descriptor, temporary = tempfile.mkstemp(prefix='.review-results-', dir=path.parent)
            staged.append((temporary, path))
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, path in staged:
            os.replace(temporary, path)
    finally:
        for temporary, _ in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)


def export_reviews(data: Path, answers: Path, manifest: Path, review_dir: Path,
                   shards: list[Path]) -> dict:
    review_dir = packets._output_path(Path(review_dir))
    run_dir = packets._output_path(Path(answers).parent)
    if review_dir.parent != run_dir or Path(manifest).resolve().parent != run_dir:
        raise ValueError('Review directory, answers and manifest must belong to the same run')
    if not isinstance(shards, list) or not shards or any(not isinstance(path, Path) for path in shards):
        raise ValueError('Supply at least one explicit shard path')
    if len({path.resolve() for path in shards}) != len(shards):
        raise ValueError('Duplicate shard path')
    items, dataset = load_dataset(Path(data), limit=0)
    _data_index(items)
    data_rows = {item['prompt_id']: item for item in items}
    manifest_raw = _read(Path(manifest))
    manifest_value = _json(manifest_raw, 'generation manifest')
    private_raw = _read(review_dir / 'private_manifest.json')
    private = _json(private_raw, 'private manifest')
    paired_arms = packets._paired_arms(private.get('paired_arms', packets.PAIRED_ARMS))
    snapshot = _read(Path(answers))  # One immutable snapshot; never reread during export.
    rows = [_json(line.encode('utf-8'), f'answer line {number}')
            for number, line in enumerate(snapshot.decode('utf-8').splitlines(), 1) if line.strip()]
    answer_kinds = _answer_index(rows, data_rows)
    selected, _, arms = _manifest_selection(manifest_value, data_rows, answer_kinds)
    selected = sorted(selected)
    configs = packets._arm_configs(manifest_value, arms, paired_arms)
    generation_data = manifest_value.get('dataset')
    prompt_hashes = {prompt_id: canonical_hash(data_rows[prompt_id]['prompt']) for prompt_id in selected}
    if (not isinstance(generation_data, dict) or generation_data.get('sha256') != dataset['sha256']
            or generation_data.get('selected_prompt_sha256') != prompt_hashes):
        raise ValueError('Generation dataset or prompt hashes disagree')
    if 'selected_prompt_ids' in generation_data:
        ids = generation_data['selected_prompt_ids']
        if (not isinstance(ids, list) or any(not isinstance(value, str) for value in ids)
                or sorted(ids) != selected):
            raise ValueError('Generation dataset selected IDs disagree')
    row_index = {}
    for row in rows:
        if (row.get('dataset_sha256') != dataset['sha256']
                or row.get('prompt_sha256') != prompt_hashes[row['prompt_id']]):
            raise ValueError('Answer dataset or prompt hash changed')
        packets._validate_arm_identity(row, configs)
        row_index[(row['arm'], row['prompt_id'])] = row
    scope = {'dataset_sha256': dataset['sha256'], 'selected_prompt_ids': selected,
             'selected_prompt_sha256': prompt_hashes, 'arms': manifest_value.get('arms')}
    expected_private = {'schema_version': 1, 'review_kind': 'blinded qualitative rubric review; ungraded',
                        'dataset_sha256': dataset['sha256'], 'generation_scope_sha256': canonical_hash(scope),
                        'selected_prompt_ids': selected, 'reviewer_count': packets.REVIEWER_COUNT,
                        'reviewer_assignment': 'sorted original manifest index modulo 4'}
    if any(private.get(key) != value for key, value in expected_private.items()):
        raise ValueError('Private manifest generation identity changed')
    entries = private.get('packets')
    if not isinstance(entries, dict):
        raise ValueError('Private manifest packet mapping is invalid')
    packet_directory = review_dir / 'packets'
    if packet_directory.is_symlink() or not packet_directory.is_dir():
        raise ValueError('Packets must be a regular directory')
    packet_values, packet_bytes = {}, {}
    for packet_id, entry in entries.items():
        if not _valid_sha(packet_id) or not isinstance(entry, dict):
            raise ValueError('Invalid private packet identity')
        prompt_id = entry.get('prompt_id')
        if prompt_id not in selected:
            raise ValueError('Private packet selects an unknown prompt')
        for arm in paired_arms:
            row = row_index.get((arm, prompt_id))
            if (row is None or row['status'] not in packets.REVIEWABLE
                    or not isinstance(row.get('answer'), str) or not row['answer'].strip()):
                raise ValueError('Previously packaged answer is missing or no longer reviewable')
        expected_packet, expected_entry = packets._packet(
            data_rows[prompt_id], row_index, dataset['sha256'], selected.index(prompt_id) % packets.REVIEWER_COUNT,
            paired_arms=paired_arms)
        if packet_id != expected_packet['packet_id'] or any(
                entry.get(key) != value for key, value in expected_entry.items()):
            raise ValueError('Private packet candidate or mapping changed')
        if not _valid_sha(entry.get('first_source_answers_sha256')):
            raise ValueError('Invalid first source snapshot hash')
        if entry.get('entry_sha256') != canonical_hash({key: value for key, value in entry.items()
                                                     if key != 'entry_sha256'}):
            raise ValueError('Private packet metadata hash changed')
        raw = _read(review_dir / 'packets' / expected_entry['packet_filename'])
        packet = _json(raw, 'packet')
        if packet != expected_packet or canonical_hash(packet) != entry['packet_sha256']:
            raise ValueError('Packet content or canonical hash changed')
        packet_values[packet_id], packet_bytes[packet_id] = packet, _sha(raw)
    if {path.name for path in packet_directory.iterdir()} != {entry['packet_filename'] for entry in entries.values()}:
        raise ValueError('Unmapped packet files exist')

    reviewed, shard_sources = {}, []
    for path in shards:
        raw = _read(path)
        shard = _json(raw, 'review shard')
        if shard.get('reviewer') != 'Codex' or shard.get('official') is not False:
            raise ValueError('Shard must declare reviewer Codex and official=false')
        if not isinstance(shard.get('packets'), list):
            raise ValueError('Shard packets must be a list')
        shard_sources.append({'path': str(path.resolve()), 'sha256': _sha(raw)})
        for review in shard['packets']:
            if not isinstance(review, dict) or set(review) != {'packet_id', 'packet_sha256', 'candidates'}:
                raise ValueError('Invalid packet review fields')
            packet_id = review['packet_id']
            if not isinstance(packet_id, str) or packet_id not in packet_values:
                raise ValueError('Shard selects an unknown packet')
            if packet_id in reviewed:
                raise ValueError('Duplicate reviewed packet across supplied shards')
            if review['packet_sha256'] != packet_bytes[packet_id]:
                raise ValueError('Shard packet byte hash changed')
            candidates = review['candidates']
            if not isinstance(candidates, dict) or set(candidates) != {'A', 'B'}:
                raise ValueError('Review candidates must be exactly A and B')
            expected = {rubric['rubric_index'] for rubric in packet_values[packet_id]['rubrics']}
            for candidate in candidates.values():
                _validate_candidate(candidate, expected)
            reviewed[packet_id] = deepcopy(review)

    judgments, assessments, unresolved = [], [], 0
    for packet_id, review in sorted(reviewed.items()):
        entry = entries[packet_id]
        prompt_id = entry['prompt_id']
        for label in ('A', 'B'):
            candidate = review['candidates'][label]
            arm = entry['candidate_map'][label]  # Unblind only after shard validation.
            row = row_index[(arm, prompt_id)]
            answer_sha = canonical_hash(row['answer'])
            for judgment in sorted(candidate['judgments'], key=lambda item: item['rubric_index']):
                if judgment['criteria_met'] is None:
                    unresolved += 1
                    continue
                judgments.append({'arm': arm, 'prompt_id': prompt_id,
                                  'rubric_index': judgment['rubric_index'], 'criteria_met': judgment['criteria_met'],
                                  'explanation': judgment.get('explanation', ''), 'sources': judgment.get('sources', []),
                                  'answer_sha256': answer_sha, 'prompt_sha256': entry['prompt_sha256'],
                                  'packet_id': packet_id, 'packet_byte_sha256': packet_bytes[packet_id],
                                  'reviewer': 'Codex', 'official': False})
            uncertain = any(item['criteria_met'] is None for item in candidate['judgments'])
            # The running viewer permits a numeric score only for reviewed.
            # This status means complete rubric grading, never clinical approval.
            status = 'uncertain' if uncertain else 'reviewed'
            quality_status = 'uncertain' if uncertain else 'needs_review' if candidate['flags'] else 'no_flags_recorded'
            note = candidate['note'] or '; '.join(flag['category'] + ': ' + flag['explanation']
                                                 for flag in candidate['flags'])
            if candidate['flags']:
                note = '추가 검토 필요: ' + note
            assessments.append({'prompt_id': prompt_id, 'arm': arm, 'answer_sha256': answer_sha,
                                'status': status, 'quality_status': quality_status,
                                'score': None, 'note': note[:500]})
    summary = build_summary(items, rows, judgments, manifest_value)
    for assessment in assessments:
        case = next(case for case in summary['arms'][assessment['arm']]['cases']
                    if case['prompt_id'] == assessment['prompt_id'])
        if case['score'] is not None:
            assessment['score'] = min(100.0, max(0.0, case['score'] * 100))
    provenance = {'reviewer': 'Codex', 'official': False, 'dataset_sha256': dataset['sha256'],
                  'answers_snapshot_sha256': _sha(snapshot), 'generation_manifest_byte_sha256': _sha(manifest_raw),
                  'private_manifest_byte_sha256': _sha(private_raw), 'shards': shard_sources}
    summary['review_provenance'] = provenance
    assessment_output = {'schema_version': 1, 'answers': assessments, 'review_provenance': provenance}
    report = {'schema_version': 1, 'status': summary['status'], 'reviewed_packet_count': len(reviewed),
              'assessment_count': len(assessments), 'boolean_judgment_count': len(judgments),
              'unresolved_judgment_count': unresolved, 'selected_count': len(selected),
              'review_provenance': provenance}
    def encoded(value):
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')
    outputs = {review_dir / 'judgments.jsonl': ''.join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                                               allow_nan=False) + '\n' for row in judgments).encode('utf-8'),
               review_dir / 'score_summary.json': encoded(summary),
               review_dir / 'review_export.json': encoded(report),
               run_dir / 'assessments.json': encoded(assessment_output)}
    for path in outputs:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('Result output must be a regular file without symlinks')
    _publish(outputs)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'answers', 'manifest', 'review-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--shards', type=Path, nargs='+', required=True)
    args = parser.parse_args(argv)
    try:
        report = export_reviews(args.data, args.answers, args.manifest, args.review_dir, args.shards)
    except (ValueError, OSError, UnicodeError) as exc:
        parser.error(str(exc))
    print(f"Codex review export: {report['reviewed_packet_count']} packets; "
          f"{report['boolean_judgment_count']} boolean judgments; "
          f"{report['unresolved_judgment_count']} unresolved; aggregate {report['status']}; unofficial")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
