"""Prepare a fixed development20 literal-source index without inference.

The completed placement cache is a new, independently pinned source profile;
it is never passed to or substituted into the old 17+3 budget/low gates. The
only proposal is one JSON user message after the unchanged leading instruction
block. It repeats literal user text with original roles/turn numbers and does
not certify facts or repeat assistant text. Requests/indexes stay in memory;
the private proof contains hashes and scalar provenance only. There is no
transport, generation switch, retry, digest lookup or configurable source pin.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import json
import math
from pathlib import Path
import sys

from . import healthbench_budget_preflight as gate
from .healthbench import DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, ROOT, canonical_hash, sum_usage, utc_now
from .healthbench_codex_cycle import REVIEW_REQUIRED, _source_object, make_payload

SOURCE_ARM = 'grounded_medium_system2048_v1'
UPSTREAM_ARM = 'grounded_medium_v1'
REVISION = 'grounded-v2'
MODEL_DIGEST = 'f38aa0c53da5f8c49d08c43a99df24ff53167fe68e24664a7777288e7656fdfe'
TEMPLATE_SHA256 = 'fa6710a93d78da62641e192361344be7a8c0a1c3737f139cf89f20ce1626b99c'
IDS = [f'HB-V{index:04d}' for index in range(20)]
STATUS_COUNTS = {'completed': 18, 'failed': 1, 'truncated': 1}
SOURCE_USAGE = {'prompt_tokens': 7613, 'completion_tokens': 17338, 'total_tokens': 24951}
_SOURCE_BYTE_SHA256 = {
    'data': 'cd3516c601da6f67fc7b6db3439efdf2659a91534c09c6bc1c5d363889ed4033',
    'instructions': '6a827a355cd215837e6ecfd1904945d7d2cfbf096535eca11933d54afcc5a0e5',
    'source_manifest': '9881ea40911145f599c1a30dbb9733c4a26c0e7c3cbb6d84b554c2261fdd8b69',
    'source_answers': '904b71c460cf02b2d70b9103ab694dfb4c885972fe63656452fc94cdf130c88f',
    'source_summary': '66041b258e2b8797fce09cb46c01ea485404dee786cd0d7edb01a5be565c2a0e',
    'comparison_manifest': 'd4ea682e44af093fc4086c930c0ed3d12f484610e138770a440e56cd369cfe5d',
    'comparison_answers': 'ffee0e18425b2c4016a24413f4eacf53ca63a319957c0ee26744de0d2a5ea066',
    'producer_verification': '9876addd69622eb8b79a4b626128404a34a1676a0ba0e48c5b2beed312da53b1',
    'data_sidecar': '0b9747ec0e7c25af1ada00e16583ffee0254a72359e24db4fcc142cd0fbf9aa5',
}
_UPSTREAM_BYTE_SHA256 = {
    'source_manifest': 'a6bb83ea5ef8dc2eea1cb53ef65ca780e74e1ca0f0fa7652321e2de40c41f441',
    'source_answers': '285956446d8c15f22fcf1c49a86af49d200fc2f2d600118a51c70649e2367662',
    'source_summary': '80053f86a211704e1263ea064b42670f4050d2d19d769b6afb94e4ba73ac777c',
    'comparison_manifest': '0f6890df90f5802fc7cfa4ba6900c61ca7bca2408042e5cc855ec2a852663960',
}
_PRODUCER_CODE_SHA256 = {
    'benchmarks/healthbench_placement_cycle.py': '40c8527ed4fd05b226f3d235498a76e14acad3d5389eb62c5c291b11a8471682',
    'benchmarks/healthbench_budget_cycle.py': 'e9d6396469f26f5917d09900b7361ef53a77767d93bb2ade7f88ae2b316218d6',
    'benchmarks/healthbench_codex_cycle.py': '716ef7ea42bcac3c1ee53d3a94b2d0167ea5fbd1c35c73fbfe26d01978ef9b84',
}


def _index(original: list[dict]) -> dict:
    """Literal role/turn index, with no factual certification or assistant text."""
    gate._require(isinstance(original, list) and original, 'Index requires original messages')
    entries = []
    for turn, message in enumerate(original, 1):
        gate._require(isinstance(message, dict) and set(message) == {'role', 'content'}
                      and isinstance(message['role'], str) and message['role'] in {'user', 'assistant'}
                      and isinstance(message['content'], str) and message['content'].strip(),
                      'Index requires unchanged user/assistant source messages')
        message['content'].encode('utf-8')  # No Unicode replacement or normalization.
        entry = {'turn': turn, 'role': message['role']}
        if message['role'] == 'user':
            entry['user_text'] = message['content']
        entries.append(entry)
    gate._require(original[-1]['role'] == 'user', 'Index source must end in user role')
    return {'schema_version': 1, 'source_messages': entries}


def _ledger_requests(item: dict, frozen_text: str) -> tuple[dict, dict, dict]:
    """Remove exactly the inserted JSON message to recover the entire control."""
    gate._require(frozen_text.encode('utf-8') == GROUNDED_SUFFIX.encode('utf-8'),
                  'Source-index instructions must remain the exact frozen bytes')
    index = _index(item['prompt'])
    encoded = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    encoded.encode('utf-8')
    decoded = _source_object(encoded.encode('utf-8'))
    gate._equal(decoded, index, 'Source-index JSON must round trip exactly')
    for message, entry in zip(item['prompt'], decoded['source_messages']):
        if message['role'] == 'user':
            gate._require(entry['user_text'].encode('utf-8') == message['content'].encode('utf-8'),
                          'Source-index user bytes must round trip unchanged')
        else:
            gate._equal(sorted(entry), ['role', 'turn'], 'Source index must not promote assistant text')
    control = make_payload(item, frozen_text, DEFAULT_MODEL, reasoning_effort='medium',
                           max_tokens=2048, instruction_placement='system_prefix')
    block = '\n\n[' + frozen_text + ']'
    gate._equal(control['messages'], [{'role': 'system', 'content': block}, *item['prompt']],
                'Control must contain the unchanged frozen block and original messages')
    candidate = deepcopy(control)
    candidate['messages'].insert(1, {'role': 'user', 'content': encoded})
    gate._equal(candidate['messages'][0], control['messages'][0], 'Instruction block must remain unchanged')
    gate._equal(candidate['messages'][2:], item['prompt'], 'Original message bytes, roles and order must remain unchanged')
    changed = sorted(key for key in set(control) | set(candidate)
                     if canonical_hash(control.get(key)) != canonical_hash(candidate.get(key)))
    gate._equal(changed, ['messages'], 'Only one inserted source-index message may change the request')
    inverse = deepcopy(candidate)
    del inverse['messages'][1]
    gate._equal(inverse, control, 'Removing the index must recover the complete cached control request')
    return control, candidate, index


def _rows(snapshot: gate._Snapshot, label: str) -> list[dict]:
    lines = snapshot.raw.decode('utf-8').splitlines()
    gate._require(lines and all(line.strip() for line in lines), label + ' must not contain blank rows')
    return [_source_object(line.encode('utf-8')) for line in lines]


def _finalized(value: dict, label: str) -> None:
    gate._require(value.get('status') == 'partial' and value.get('error') is None,
                  label + ' must preserve its finalized partial state')
    gate._require(value.get('score') is None and value.get('score_status') == REVIEW_REQUIRED,
                  label + ' must preserve its original unjudged score')
    gate._time(value.get('finished_at'), label + ' requires a finished_at')


def _attempt(row: dict, item: dict, payload: dict, data_sha: str, *, placement: bool) -> None:
    arm = SOURCE_ARM if placement else UPSTREAM_ARM
    gate._require(row.get('arm') == arm and row.get('model') == DEFAULT_MODEL
                  and row.get('model_digest') == MODEL_DIGEST
                  and row.get('dataset_sha256') == data_sha
                  and row.get('prompt_sha256') == canonical_hash(item['prompt'])
                  and row.get('instructions_byte_sha256') == _SOURCE_BYTE_SHA256['instructions']
                  and row.get('instructions_name') == arm and row.get('instructions_version') == REVISION,
                  'Source row model/input/instruction identity differs')
    for key, expected in {'request_sha256': canonical_hash(payload),
                          'input_sha256': canonical_hash(payload['messages'])}.items():
        gate._equal(row.get(key), expected, 'Source row request/input hash differs')
    attempts = row.get('attempts')
    gate._require(isinstance(attempts, list) and len(attempts) == 1 and isinstance(attempts[0], dict),
                  'Source must retain exactly one attempt')
    gate._equal(row.get('selected_attempt'), 1, 'Source selected attempt differs')
    gate._equal(row.get('fallback_used'), False, 'Source must not use fallback')
    attempt = attempts[0]
    gate._equal(attempt.get('number'), 1, 'Source attempt number differs')
    gate._equal(attempt.get('response_received'), True, 'Source must retain the actual received response')
    for key in ('request_sha256', 'input_sha256'):
        gate._equal(attempt.get(key), row[key], 'Attempt request/input hash differs')
    for key in ('max_tokens', 'temperature', 'reasoning_effort'):
        gate._equal(attempt.get(key), payload[key], 'Attempt settings differ')
    gate._require(isinstance(row.get('answer'), str), 'Source answer must be text')
    row['answer'].encode('utf-8')
    for key in ('answer', 'status', 'finish_reason', 'error', 'usage', 'seconds'):
        gate._equal(row.get(key), attempt.get(key), 'Selected attempt and row disagree')
    usage = gate._usage(attempt.get('usage'), 'Source usage is invalid')
    seconds = attempt.get('seconds')
    gate._require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0,
                  'Source seconds must be finite and nonnegative')
    gate._require(row.get('score') is None and row.get('score_status') == REVIEW_REQUIRED,
                  'Source scores must remain unjudged')
    if row.get('status') == 'completed':
        gate._require(row['answer'].strip() and row.get('finish_reason') == 'stop' and row.get('error') is None,
                      'Completed source requires a nonempty stopped response')
    elif row.get('status') == 'failed':
        gate._require(not row['answer'].strip() and row.get('finish_reason') == 'length'
                      and usage['completion_tokens'] == 2048,
                      'Failed source requires an empty final response with 2048-token length exhaustion')
        gate._equal(row.get('error'), {'code': 'empty_answer', 'http_status': None},
                    'Failed source requires actual empty_answer evidence')
    elif placement and row.get('status') == 'truncated':
        gate._require(row['answer'].strip() and row.get('finish_reason') == 'length'
                      and usage['completion_tokens'] == 2048 and row.get('error') is None,
                      'Truncated source requires a nonempty 2048-token length response')
    else:
        raise ValueError('Source row status is outside the fixed cache profile')
    if placement:
        for value in (row, attempt):
            gate._equal(value.get('instruction_placement'), 'system_prefix', 'Source placement identity differs')
        for key in ('digest_before', 'digest_after'):
            gate._equal(row.get(key), MODEL_DIGEST, 'Source before/after model digest differs')
        for key in ('template_before_sha256', 'template_after_sha256'):
            gate._equal(row.get(key), TEMPLATE_SHA256, 'Source before/after template differs')
        for key in ('model_identity_verified', 'runtime_template_verified'):
            gate._equal(row.get(key), True, 'Source runtime identity verification is missing')
        gate._equal(row.get('model_digest_status'), 'expected_source_pin', 'Source digest status differs')


def prepare_source_ledger(data: Path, source_manifest: Path, source_comparison_manifest: Path,
                          instructions_file: Path, producer_verification: Path, out: Path) -> dict:
    """Prepare hashes only against the fixed 2026-10-12 placement cache.

    Use a dedicated fresh private directory and one writer. Publication reuses
    pinned directory FDs/source snapshots; the documented portable POSIX
    mkdir -> first inode capture same-UID empty-folder replacement limit remains.
    Fixed byte/code pins are not caller parameters or refreshable CLI options.
    """
    paths = {'data': data, 'instructions': instructions_file, 'source_manifest': source_manifest,
             'source_answers': source_manifest.parent / 'answers.jsonl',
             'source_summary': source_manifest.parent / 'summary.json',
             'comparison_manifest': source_comparison_manifest,
             'comparison_answers': source_comparison_manifest.parent / 'answers.jsonl',
             'producer_verification': producer_verification,
             'data_sidecar': data.with_suffix('.manifest.json')}
    snapshots = []
    named = {}
    def pinned(name, path, sha):
        snapshot = gate._Snapshot(path)
        gate._require(snapshot.sha256 == sha, 'Fixed ' + name + ' raw byte pin differs')
        named[name] = snapshot
        snapshots.append(snapshot)
        return snapshot
    for name, path in paths.items():
        pinned(name, path, _SOURCE_BYTE_SHA256[name])
    # The active checkout is fixed. A pristine alternate tree must not hide
    # changes to the producer whose payload helper is actually imported here.
    code_root = gate._path(ROOT)
    for relative, expected in _PRODUCER_CODE_SHA256.items():
        pinned('code:' + relative, code_root / relative, expected)
    source = _source_object(named['source_manifest'].raw)
    summary = _source_object(named['source_summary'].raw)
    comparison = _source_object(named['comparison_manifest'].raw)
    verification = _source_object(named['producer_verification'].raw)
    gate._equal(verification.get('code_sha256'), _PRODUCER_CODE_SHA256, 'Producer verification code pins differ')
    gate._equal(verification.get('status'), 'actual_runner_prepared_no_calls', 'Producer preparation status differs')
    for key in ('calls', 'model_metadata_queries', 'template_queries'):
        gate._equal(verification.get(key), 0, 'Producer prepare-only queries differ')
    gate._equal(verification.get('role_runner_source_factor'), ['messages'], 'Producer preparation factor differs')
    gate._equal(verification.get('source_status_counts'), {'completed': 17, 'failed': 3}, 'Producer source profile differs')
    for name, value in [('Source', source), ('Summary', summary), ('Comparison', comparison)]:
        _finalized(value, name)
    for value in (source, comparison):
        gate._equal(value.get('schema_version'), 1, 'Source/comparison schema differs')
        gate._equal(value.get('source_byte_hashes'), _UPSTREAM_BYTE_SHA256, 'Original source byte provenance differs')
    gate._require(gate._time(source.get('finished_at'), 'Source finish time differs') >=
                  gate._time(source.get('started_at'), 'Source start time differs'), 'Source finishes before it starts')
    gate._equal(comparison.get('finished_at'), source['finished_at'], 'Comparison finalization time differs')
    items, all_ids = gate._dataset(named['data'])
    gate._equal(all_ids, [f'HB-V{index:04d}' for index in range(200)], 'Dataset must retain the original 200 IDs')
    ids = [item['prompt_id'] for item in items]
    gate._equal(ids, IDS, 'Only the fixed development20 IDs may be selected')
    metadata = source.get('dataset')
    gate._require(isinstance(metadata, dict), 'Source dataset provenance is missing')
    gate._equal(metadata.get('path'), str(named['data'].path), 'Dataset path differs')
    gate._equal(metadata.get('sha256'), named['data'].sha256, 'Dataset byte provenance differs')
    gate._equal(metadata.get('total_records'), 200, 'Dataset count differs')
    gate._equal(metadata.get('selected_prompt_ids'), ids, 'Dataset selection differs')
    gate._equal(metadata.get('selected_prompt_sha256'),
                {item['prompt_id']: canonical_hash(item['prompt']) for item in items}, 'Dataset original prompt hashes differ')
    provenance = metadata.get('provenance')
    gate._require(isinstance(provenance, dict) and provenance.get('status') == 'validated',
                  'Dataset sidecar provenance must be validated')
    gate._equal(provenance.get('sidecar'), str(named['data_sidecar'].path), 'Sidecar path differs')
    gate._equal(provenance.get('sidecar_sha256'), named['data_sidecar'].sha256, 'Sidecar byte provenance differs')
    sidecar = _source_object(named['data_sidecar'].raw)
    gate._equal(sidecar.get('converted_sha256'), named['data'].sha256, 'Sidecar data pin differs')
    for key, value in sidecar.items():
        if key in provenance:
            gate._equal(provenance[key], value, 'Sidecar provenance field differs')
    gate._require(named['instructions'].raw == GROUNDED_SUFFIX.encode('utf-8'), 'Frozen instruction bytes differ')
    gate._equal(source.get('instructions'), {'name': SOURCE_ARM, 'version': REVISION,
                 'byte_sha256': named['instructions'].sha256}, 'Source instruction metadata differs')
    expected_arm = {'max_tokens': 2048, 'temperature': 0, 'reasoning_effort': 'medium', 'max_attempts': 1,
                    'suffix': GROUNDED_SUFFIX, 'prompt_revision': REVISION, 'instruction_placement': 'system_prefix'}
    gate._equal(source.get('arms'), {SOURCE_ARM: expected_arm}, 'Source arm settings differ')
    upstream_arm = {key: value for key, value in expected_arm.items() if key != 'instruction_placement'}
    gate._equal(comparison.get('arms'), {UPSTREAM_ARM: upstream_arm, SOURCE_ARM: expected_arm}, 'Comparison arm settings differ')
    gate._equal(source.get('config'), {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'timeout': 300,
                  'reasoning_effort': 'medium', 'temperature': 0, 'max_tokens': 2048,
                  'maximum_calls_per_case': 1, 'resume': False, 'prepare_only': False,
                  'instruction_placement': 'system_prefix'}, 'Source request/call policy differs')
    gate._equal(source.get('model'), {'name': DEFAULT_MODEL, 'digest': MODEL_DIGEST,
                 'metadata_status': 'available', 'digest_status': 'expected_source_pin'}, 'Source model metadata differs')
    expected_runtime_model = {'observed_digest': MODEL_DIGEST, 'metadata_status': 'available', 'metadata_calls': 43}
    expected_runtime_template = {'expected_byte_sha256': TEMPLATE_SHA256, 'observed_byte_sha256': TEMPLATE_SHA256,
                                'metadata_status': 'available', 'template_queries': 43, 'verified': True,
                                'query': 'POST /api/show; exact template UTF-8 bytes; metadata only'}
    for value in (source, summary, comparison):
        gate._equal(value.get('current_runtime_model'), expected_runtime_model, 'Source recorded runtime model queries differ')
        gate._equal(value.get('current_runtime_template'), expected_runtime_template, 'Source recorded runtime template queries differ')
        gate._equal(value.get('cached_source_runtime_template'), {
            'byte_sha256': TEMPLATE_SHA256, 'metadata_source': 'cached source-time runtime snapshot; no new control query',
            'bound_source_model_digest': MODEL_DIGEST, 'bound_source_manifest_byte_sha256': _UPSTREAM_BYTE_SHA256['source_manifest'],
            'plain_api_system_mapping': 'supported by this fixed pinned runtime template'}, 'Cached source-time template provenance differs')
    gate._equal(source.get('split'), {'name': 'development', 'limit': 20,
                'selection': 'first 20 frozen baseline IDs; heldout excluded'}, 'Fixed development split differs')
    gate._equal(comparison.get('dataset'), metadata, 'Comparison dataset differs')
    for value in (source, summary, comparison):
        gate._equal(value.get('selected_prompt_ids'), ids, 'Source selected IDs differ')
    for value in (source, summary):
        gate._equal(value.get('selected_arms'), [SOURCE_ARM], 'Source selected arm differs')
    gate._equal(comparison.get('selected_arms'), [UPSTREAM_ARM, SOURCE_ARM], 'Comparison selected arms differ')
    sources = comparison.get('sources')
    gate._require(isinstance(sources, dict) and set(sources) == {'baseline', 'candidate'}, 'Comparison source profiles differ')
    candidate_source = sources['candidate']
    gate._equal(candidate_source, {'manifest_path': str(named['source_manifest'].path),
                'answers_path': str(named['source_answers'].path),
                'manifest_byte_sha256': named['source_manifest'].sha256, 'answers_byte_sha256': named['source_answers'].sha256,
                'expected_manifest_byte_sha256': named['source_manifest'].sha256,
                'expected_answers_byte_sha256': named['source_answers'].sha256,
                'original_manifest_status': 'partial', 'recorded_run_status': 'partial', 'integrity_status': 'verified'},
                'Comparison candidate source pins differ')
    baseline = sources['baseline']
    gate._require(isinstance(baseline, dict) and isinstance(baseline.get('manifest_path'), str), 'Original source paths missing')
    upstream_manifest = Path(baseline['manifest_path'])
    upstream_paths = {'source_manifest': upstream_manifest, 'source_answers': upstream_manifest.parent / 'answers.jsonl',
                      'source_summary': upstream_manifest.parent / 'summary.json',
                      'comparison_manifest': upstream_manifest.parent.parent / 'comparison' / 'manifest.json'}
    for key, path in upstream_paths.items():
        pinned('upstream:' + key, path, _UPSTREAM_BYTE_SHA256[key])
    gate._equal(baseline, {'manifest_path': str(named['upstream:source_manifest'].path),
                'answers_path': str(named['upstream:source_answers'].path),
                'manifest_byte_sha256': _UPSTREAM_BYTE_SHA256['source_manifest'],
                'answers_byte_sha256': _UPSTREAM_BYTE_SHA256['source_answers'],
                'original_manifest_status': 'partial', 'provenance_kind': 'externally pinned validated original snapshot',
                'source_changed_after_validation': False, 'current_source_matching': 'verified_at_last_check'},
                'Original comparison source pins differ')
    gate._require(len({entry.stamp[:2] for entry in snapshots}) == len(snapshots), 'Sources/code files must be distinct unaliased files')
    rows = _rows(named['source_answers'], 'Source answers')
    upstream_rows = _rows(named['upstream:source_answers'], 'Original answers')
    comparison_rows = _rows(named['comparison_answers'], 'Comparison answers')
    gate._equal([row.get('prompt_id') for row in rows], ids, 'Source must retain every development20 row once in original order')
    gate._equal([row.get('prompt_id') for row in upstream_rows], ids, 'Original source development rows differ')
    gate._require(len(comparison_rows) == 40, 'Comparison must retain all 40 rows')
    payloads, proofs, prepared, expected_provenance = [], [], [], []
    role_counts = Counter()
    history_count = 0
    for item, row, upstream in zip(items, rows, upstream_rows):
        control, candidate, index = _ledger_requests(item, GROUNDED_SUFFIX)
        _attempt(row, item, control, named['data'].sha256, placement=True)
        original_payload = make_payload(item, GROUNDED_SUFFIX, DEFAULT_MODEL, reasoning_effort='medium', max_tokens=2048)
        _attempt(upstream, item, original_payload, named['data'].sha256, placement=False)
        for label, actual in [('baseline', upstream), ('candidate', row)]:
            expected_provenance.append({'source': label, 'prompt_id': item['prompt_id'], 'arm': actual['arm'],
                'original_status': actual['status'], 'full_row_sha256': canonical_hash(actual),
                'input_sha256': actual['input_sha256'], 'request_sha256': actual['request_sha256']})
        prepared.append({'prompt_id': item['prompt_id'], 'prompt_sha256': canonical_hash(item['prompt']),
                         'input_sha256': canonical_hash(control['messages']), 'request_sha256': canonical_hash(control),
                         'dataset_sha256': named['data'].sha256, 'model_digest': MODEL_DIGEST,
                         'instructions_byte_sha256': named['instructions'].sha256, 'instructions_name': SOURCE_ARM,
                         'instructions_version': REVISION, 'instruction_placement': 'system_prefix'})
        inverse = deepcopy(candidate)
        del inverse['messages'][1]
        role_counts.update(message['role'] for message in item['prompt'])
        history_count += len(item['prompt']) > 1
        payloads.append(control)
        proofs.append({'prompt_id': item['prompt_id'], 'source_arm': SOURCE_ARM, 'original_status': row['status'],
                       'full_row_sha256': canonical_hash(row), 'prompt_sha256': canonical_hash(item['prompt']),
                       'original_messages_sha256': canonical_hash(candidate['messages'][2:]),
                       'control_input_sha256': canonical_hash(control['messages']),
                       'candidate_input_sha256': canonical_hash(candidate['messages']),
                       'control_request_sha256': canonical_hash(control), 'candidate_request_sha256': canonical_hash(candidate),
                       'inverse_control_request_sha256': canonical_hash(inverse),
                       'source_index_sha256': canonical_hash(index), 'source_index_message_byte_sha256': gate._sha(candidate['messages'][1]['content'].encode('utf-8')),
                       'original_message_count': len(item['prompt']),
                       'indexed_user_text_count': sum(message['role'] == 'user' for message in item['prompt']),
                       'assistant_content_copies': 0, 'changed_fields': ['messages']})
    gate._equal(source.get('prepared_requests'), prepared, 'Source prepared input/request/model provenance differs')
    gate._equal(comparison.get('row_provenance'), expected_provenance, 'Comparison full-row provenance differs')
    gate._equal(comparison_rows, [row for pair in zip(upstream_rows, rows) for row in pair], 'Comparison answer rows or row bytes differ')
    gate._equal(dict(role_counts), {'user': 31, 'assistant': 11}, 'Original development role counts differ')
    gate._equal(history_count, 5, 'Original multi-turn development count differs')
    counts = dict(Counter(row['status'] for row in rows))
    gate._equal(counts, STATUS_COUNTS, 'Latest source must retain 18 completed, 1 failed and 1 truncated rows')
    gate._equal(dict(Counter(row['status'] for row in upstream_rows)), {'completed': 17, 'failed': 3}, 'Original cache status counts differ')
    for key, value in {'started_at': source['started_at'], 'finished_at': source['finished_at'],
                       'planned_answers': 20, 'written_answers': 20, 'unattempted_answers': 0,
                       'calls': 20, 'model_metadata_calls': 43, 'template_queries': 43,
                       'answer_status_counts': STATUS_COUNTS, 'finish_reason_counts': {'stop': 18, 'length': 2},
                       'empty_final_answer_count': 1, 'response_not_received_count': 0, 'usage': SOURCE_USAGE,
                       'source_status_counts': {'completed': 17, 'failed': 3}, 'source_known_failure_count': 3,
                       'source_recorded_calls': 20, 'comparison_status': 'partial'}.items():
        gate._equal(summary.get(key), value, 'Source summary counts/status/settings differ')
    gate._equal(sum_usage([row['attempts'][0] for row in rows]), SOURCE_USAGE, 'Source row token usage differs')
    upstream_usage = sum_usage([row['attempts'][0] for row in upstream_rows])
    gate._equal(summary.get('source_usage'), upstream_usage, 'Original source summary usage differs')
    for key, value in {'planned_answers': 40, 'written_answers': 40, 'original_control_manifest_status': 'partial',
                       'control_status_counts': {'completed': 17, 'failed': 3}, 'control_recorded_calls': 20,
                       'control_usage': upstream_usage, 'candidate_status': 'partial'}.items():
        gate._equal(comparison.get(key), value, 'Comparison counts/status differ')
    seconds = summary.get('seconds')
    gate._require(type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0, 'Source summary seconds invalid')
    output = gate._path(out)
    private_root = gate._path(ROOT / 'results')
    gate._require(output != private_root and output.is_relative_to(private_root), 'Output must be a dedicated private results directory')
    gate._require(not output.exists(), 'Output must be fresh; overwrite and resume are forbidden')
    gate._require(not any(entry.path.is_relative_to(output) for entry in snapshots), 'Output cannot contain sources')
    result = {'schema_version': 1, 'status': 'prepared', 'prepared_at': utc_now(), 'mode': 'local_prepare_only',
              'calls': 0, 'generation_calls': 0, 'model_metadata_calls': 0, 'template_queries': 0,
              'network_calls': 0, 'transports_created': 0,
              'source_profile': 'immutable_placement_medium2048_20261012_051006',
              'selected_prompt_ids': ids, 'split': {'name': 'development', 'limit': 20, 'heldout_included': False},
              'source_arm': SOURCE_ARM, 'original_manifest_status': 'partial', 'source_status_counts': counts,
              'known_failure_count': 1, 'known_truncation_count': 1, 'source_recorded_calls': 20,
              'source_usage': SOURCE_USAGE, 'source_finished_at': source['finished_at'],
              'dataset_byte_sha256': named['data'].sha256, 'instructions_byte_sha256': named['instructions'].sha256,
              'frozen_block_byte_sha256': gate._sha(('\n\n[' + GROUNDED_SUFFIX + ']').encode('utf-8')),
              'model': {'name': DEFAULT_MODEL, 'digest': MODEL_DIGEST, 'metadata_source': 'fixed source; no new runtime queries'},
              'runtime_template_byte_sha256': TEMPLATE_SHA256, 'endpoint': DEFAULT_BASE_URL,
              'request_settings': {key: value for key, value in payloads[0].items() if key != 'messages'},
              'reasoning_effort': 'medium', 'temperature': 0, 'max_tokens': 2048, 'max_attempts': 1,
              'changed_fields': ['messages'], 'original_role_counts': dict(role_counts), 'original_multiturn_case_count': history_count,
              'indexed_user_text_count': 31, 'assistant_content_copies': 0,
              'experiment': {'factor': 'additional_literal_source_index_user_message', 'instruction_block_unchanged': True,
                             'original_messages_unchanged': True, 'includes_current_user_message': True,
                             'source_text_is_verified_patient_fact': False, 'summaries_or_fact_extraction': False,
                             'assistant_text_promoted': False, 'quality_improvement_proven': False},
              'source_byte_hashes': dict(_SOURCE_BYTE_SHA256), 'upstream_byte_hashes': dict(_UPSTREAM_BYTE_SHA256),
              'producer_code_sha256': dict(_PRODUCER_CODE_SHA256),
              'source_snapshots': [{'path': str(entry.path), 'byte_sha256': entry.sha256} for entry in snapshots],
              'row_provenance': proofs, 'score': None, 'score_status': REVIEW_REQUIRED, 'criteria_predicted': False,
              'medical_judgments': 'not_performed; preparation is not generation or a quality result',
              'execution_support': {'ready': False, 'status': 'NOT_READY', 'reason': 'no source-index generation or paired-review runner is implemented'},
              'fixed_low_control_gate': 'unchanged', 'old_medium17_plus3_source_gate': 'unchanged and not called',
              'default_payload_builder': 'unchanged',
              'publication_limit': 'portable POSIX mkdir to first inode capture cannot exclude same-UID empty-directory replacement'}
    for entry in snapshots:
        entry.verify()
    gate._publish_private(output, result, snapshots)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data', 'source-manifest', 'source-comparison-manifest', 'instructions-file',
                 'producer-verification', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare_source_ledger(args.data, args.source_manifest, args.source_comparison_manifest,
                                      args.instructions_file, args.producer_verification, args.out)
    except (ValueError, OSError, UnicodeError) as exc:
        print('Source-index preflight rejected: ' + str(exc), file=sys.stderr)
        return 2
    print(json.dumps({'status': result['status'], 'generation_calls': result['generation_calls'],
                      'source_status_counts': result['source_status_counts'], 'execution_support': result['execution_support']},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
