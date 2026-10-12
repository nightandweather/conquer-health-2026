"""One bounded, externally supervised HealthBench-derived conversation step.

This is a new simulated multi-turn task, not an official HealthBench score or
the old single-answer comparison. Only a human/Codex-authored patient turn may
advance a case. Importing this module never prepares or executes an experiment.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from .healthbench import (DEFAULT_BASE_URL, DEFAULT_MODEL, GROUNDED_SUFFIX, ROOT,
                         RequestFailure, canonical_hash, load_dataset, usage_values)
from .healthbench_budget_cycle import _Blocked, _guard
from .healthbench_budget_preflight import _Snapshot, _inode, _path, _require, _sha
from .healthbench_codex_cycle import _source_object
from .healthbench_source_ledger_cycle import _SourceTransport
from .healthbench import Transport, utc_now

CASE_IDS = ('HB-V0000', 'HB-V0004', 'HB-V0015', 'HB-V0016')
DATASET_SHA256 = 'cd3516c601da6f67fc7b6db3439efdf2659a91534c09c6bc1c5d363889ed4033'
GROUNDED_SHA256 = '6a827a355cd215837e6ecfd1904945d7d2cfbf096535eca11933d54afcc5a0e5'
MODEL_DIGEST = 'f38aa0c53da5f8c49d08c43a99df24ff53167fe68e24664a7777288e7656fdfe'
TEMPLATE_SHA256 = 'fa6710a93d78da62641e192361344be7a8c0a1c3737f139cf89f20ce1626b99c'
EXECUTION_PATH = Path(__file__).resolve()
SOURCE_MODULES = (
    'benchmarks/healthbench.py', 'benchmarks/healthbench_budget_cycle.py',
    'benchmarks/healthbench_source_ledger_cycle.py', 'benchmarks/healthbench_codex_cycle.py',
    'benchmarks/healthbench_budget_preflight.py', 'benchmarks/healthbench_source_ledger_preflight.py',
    'benchmarks/healthbench_review.py', 'benchmarks/healthbench_score.py', 'benchmarks/healthbench_refine.py',
)
MAX_CALLS, MAX_DOCTOR_TURNS, TIMEOUT = 20, 5, 90
SCORE_STATUS = 'derived_multiturn_not_officially_scored'
TASK_WRAPPER = (
    'This is a simulated multi-turn clinical conversation derived from a supplied user prompt. '
    'Give one natural-language doctor response to the current conversational turn. '
    'Use patient-specific facts only from original user statements and explicitly sourced later user replies. '
    'Earlier assistant statements are unconfirmed. Missing or unknown patient information remains unknown. '
    'Ask necessary clarifying questions when they meaningfully affect your response and explain appropriate '
    'possibilities and a plan within the request. A follow-up question does not establish a new symptom, '
    'medicine, test result, diagnosis, or completed treatment. Respect a patient reply that information '
    'is unknown; do not convert it to a negative finding. Do not claim an examination or intervention happened.'
)
SETTINGS = {'model': DEFAULT_MODEL, 'base_url': DEFAULT_BASE_URL, 'reasoning_effort': 'medium',
            'max_tokens': 2048, 'temperature': 0, 'timeout': TIMEOUT,
            'maximum_generation_calls': MAX_CALLS, 'maximum_doctor_replies_per_case': MAX_DOCTOR_TURNS,
            'retries': 0}


def _private_path(path: Path) -> Path:
    result = _path(Path(path))
    try:
        result.relative_to((ROOT / 'results').resolve())
    except ValueError as exc:
        raise ValueError('Artifacts require this repository private results directory') from exc
    _require(result != (ROOT / 'results').resolve(), 'A dedicated private run directory is required')
    return result


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _new_json(path: Path, value: dict) -> None:
    raw = _json_bytes(value)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_state(out: Path, state: dict, before: _Snapshot) -> _Snapshot:
    """Atomically publish state only while the captured old state is unchanged."""
    before.verify()
    directory = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identity = _inode(os.fstat(directory))
    temporary = '.state-' + uuid.uuid4().hex + '.json'
    try:
        _require(_inode(out.stat()) == identity, 'Run directory identity changed')
        _new_json(out / temporary, state)
        before.verify()
        os.replace(temporary, 'state.json', src_dir_fd=directory, dst_dir_fd=directory)
        _require(_inode(out.stat()) == identity, 'Run directory identity changed')
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)
    return _Snapshot(out / 'state.json')


def _plan_sources(plan_path: Path) -> tuple[dict, list[_Snapshot], list[dict]]:
    source = _Snapshot(_path(plan_path))
    plan = _source_object(source.raw)
    _require(plan.get('schema_version') == 1 and plan.get('case_ids') == list(CASE_IDS), 'Plan case scope differs')
    expected = {'dataset_sha256': DATASET_SHA256, 'grounded_suffix_sha256': GROUNDED_SHA256,
                'model_digest': MODEL_DIGEST, 'template_sha256': TEMPLATE_SHA256}
    _require(all(plan.get(key) == value for key, value in expected.items()), 'Plan fixed source/model pins differ')
    _require(_sha(GROUNDED_SUFFIX.encode('utf-8')) == GROUNDED_SHA256, 'Frozen grounded instructions changed')
    if 'settings' in plan:
        _require(canonical_hash(plan['settings']) == canonical_hash(SETTINGS), 'Plan fixed inference settings differ')
    modules = plan.get('source_module_sha256')
    _require(isinstance(modules, dict) and set(modules) == set(SOURCE_MODULES), 'Plan source module scope differs')
    execution = _Snapshot(_path(EXECUTION_PATH))
    _require(plan.get('execution_module_sha256') == execution.sha256, 'Execution module pin differs')
    data_path = Path(plan.get('data_path', ''))
    if not data_path.is_absolute():
        data_path = ROOT / data_path
    data = _Snapshot(_path(data_path))
    _require(data.sha256 == DATASET_SHA256, 'Source dataset pin differs')
    snapshots = [source, execution, data]
    if 'case_plan_path' in plan or 'case_plan_sha256' in plan:
        _require(isinstance(plan.get('case_plan_path'), str) and bool(plan['case_plan_path'])
                 and isinstance(plan.get('case_plan_sha256'), str), 'Case source plan requires its path and SHA')
        case_plan_path = Path(plan['case_plan_path'])
        if not case_plan_path.is_absolute():
            case_plan_path = ROOT / case_plan_path
        case_plan = _Snapshot(_private_path(case_plan_path))
        _require(case_plan.sha256 == plan['case_plan_sha256'], 'Case source plan pin differs')
        snapshots.append(case_plan)
    for relative in SOURCE_MODULES:
        captured = _Snapshot(_path(ROOT / relative))
        _require(modules[relative] == captured.sha256, 'Source module pin differs: ' + relative)
        snapshots.append(captured)
    # The loader's 20-case selection is deterministic. No heldout case is
    # selected, adapted, sent, or inspected by this runner.
    dev, dataset = load_dataset(data.path, limit=20)
    _require(dataset['sha256'] == DATASET_SHA256
             and [item['prompt_id'] for item in dev] == [f'HB-V{index:04d}' for index in range(20)],
             'Development source selection differs')
    selected = [next(item for item in dev if item['prompt_id'] == case_id) for case_id in CASE_IDS]
    _require(all(message['role'] in {'user', 'assistant'} for item in selected for message in item['prompt']),
             'Selected source conversations must contain only original user/assistant roles')
    _guard(snapshots)
    return plan, snapshots, selected


def prepare(plan_path: Path, out: Path) -> dict:
    """Validate and pin a new private experiment, with no transport creation."""
    out = _private_path(out)
    _require(not out.exists(), 'Output must be fresh; no overwrite or resumption')
    plan, snapshots, selected = _plan_sources(Path(plan_path))
    manifest = {'schema_version': 1, 'experiment': 'HealthBench-derived supervised multi-turn probe',
                'status': 'prepared', 'prepared_at': utc_now(), 'plan_path': str(snapshots[0].path),
                'plan_byte_sha256': snapshots[0].sha256, 'case_ids': list(CASE_IDS), 'settings': SETTINGS,
                'pins': {key: plan[key] for key in ('dataset_sha256', 'grounded_suffix_sha256', 'model_digest',
                                                  'template_sha256', 'execution_module_sha256', 'source_module_sha256')},
                'source_files': [{'path': str(snapshot.path), 'byte_sha256': snapshot.sha256} for snapshot in snapshots],
                'source_prompt_sha256': {item['prompt_id']: canonical_hash(item['prompt']) for item in selected},
                'task_wrapper_byte_sha256': _sha(TASK_WRAPPER.encode('utf-8')),
                'first_turn_cache_reused': False, 'original_single_answer_comparable': False,
                'source_case_0004_policy': 'Stop after its first doctor reply; no continued simulated treatment',
                'patient_turn_validation': 'Root declares no new facts; exact quotes are checked mechanically. '
                    'This does not independently validate utterance meaning or patient-fact truth.',
                'score': None, 'score_status': SCORE_STATUS}
    state = {'schema_version': 1, 'status': 'prepared', 'calls': 0, 'reserved_calls': 0,
             'model_metadata_calls': 0, 'template_queries': 0, 'used_request_sha256': [], 'pending': None,
             'cases': {case_id: {'status': 'ready', 'doctor_turns': 0, 'steps': []} for case_id in CASE_IDS},
             'score': None, 'score_status': SCORE_STATUS}
    _guard(snapshots)
    out.mkdir(parents=True, mode=0o700)
    out.chmod(0o700)
    _new_json(out / 'manifest.json', manifest)
    _new_json(out / 'state.json', state)
    for case_id in CASE_IDS:
        case_dir = out / 'cases' / case_id
        case_dir.mkdir(parents=True, mode=0o700)
        case_dir.parent.chmod(0o700)
        case_dir.chmod(0o700)
    _guard(snapshots)
    return {'status': 'prepared', 'out': str(out), 'case_count': len(CASE_IDS),
            'maximum_generation_calls': MAX_CALLS, 'calls': 0, 'score': None, 'score_status': SCORE_STATUS}


def _load_run(out: Path) -> tuple[dict, dict, _Snapshot, list[_Snapshot], dict[str, dict]]:
    manifest_snapshot, state_snapshot = _Snapshot(out / 'manifest.json'), _Snapshot(out / 'state.json')
    manifest, state = _source_object(manifest_snapshot.raw), _source_object(state_snapshot.raw)
    _require(manifest.get('case_ids') == list(CASE_IDS) and canonical_hash(manifest.get('settings')) == canonical_hash(SETTINGS)
             and manifest.get('task_wrapper_byte_sha256') == _sha(TASK_WRAPPER.encode('utf-8')),
             'Stored run scope or task wrapper differs')
    try:
        plan, snapshots, selected = _plan_sources(Path(manifest.get('plan_path', '')))
    except (ValueError, OSError, _Blocked, UnicodeError):
        # Persist the global stop even if someone later restores source bytes.
        # This run cannot be resumed after an observed source identity failure.
        state.update(status='blocked', stop_reason='source_pin_changed')
        _replace_state(out, state, state_snapshot)
        raise
    _require(manifest.get('plan_byte_sha256') == snapshots[0].sha256
             and manifest.get('source_files') == [{'path': str(value.path), 'byte_sha256': value.sha256} for value in snapshots]
             and manifest.get('pins') == {key: plan[key] for key in manifest.get('pins', {})},
             'Prepared run source provenance differs')
    items = {item['prompt_id']: item for item in selected}
    _require(manifest.get('source_prompt_sha256') == {key: canonical_hash(item['prompt']) for key, item in items.items()},
             'Prepared original prompt identity differs')
    _require(isinstance(state.get('cases'), dict) and set(state['cases']) == set(CASE_IDS)
             and type(state.get('calls')) is int and type(state.get('reserved_calls')) is int
             and 0 <= state['calls'] <= state['reserved_calls'] <= MAX_CALLS
             and isinstance(state.get('used_request_sha256'), list)
             and len(state['used_request_sha256']) == len(set(state['used_request_sha256'])) == state['reserved_calls'],
             'Run call accounting or request identities differ')
    _require(state.get('pending') is None and state['calls'] == state['reserved_calls'],
             'Unfinalized reserved request cannot be retried or resumed')
    observed, hashes = 0, []
    for case_id, case in state['cases'].items():
        _require(case.get('status') in {'ready', 'awaiting_patient', 'stopped'}
                 and type(case.get('doctor_turns')) is int and 0 <= case['doctor_turns'] <= MAX_DOCTOR_TURNS
                 and isinstance(case.get('steps'), list) and len(case['steps']) == case['doctor_turns'],
                 'Case turn accounting differs')
        _require(case_id != 'HB-V0004' or case['doctor_turns'] <= 1, 'Urgent source case continued after first reply')
        for number, entry in enumerate(case['steps'], 1):
            expected_path = f'cases/{case_id}/turn-{number:02d}.json'
            _require(entry.get('path') == expected_path, 'Case turn order differs')
            captured = _Snapshot(out / expected_path)
            _require(entry.get('byte_sha256') == captured.sha256, 'Stored turn evidence changed')
            expected_post = f'cases/{case_id}/turn-{number:02d}-post.json'
            _require(entry.get('post_path') == expected_post, 'Post-response evidence order differs')
            post_snapshot = _Snapshot(out / expected_post)
            _require(entry.get('post_byte_sha256') == post_snapshot.sha256, 'Post-response evidence changed')
            row = _source_object(captured.raw)
            _require(row.get('case_id') == case_id and row.get('doctor_turn') == number
                     and row.get('request_sha256') == canonical_hash(row.get('request'))
                     and row.get('input_sha256') == canonical_hash(row.get('request', {}).get('messages')),
                     'Stored turn payload identity differs')
            snapshots.append(captured)
            snapshots.append(post_snapshot)
            observed += 1
            hashes.append(row['request_sha256'])
    _require(observed == state['calls'] and set(hashes) == set(state['used_request_sha256']), 'Stored call ledger differs')
    snapshots.append(manifest_snapshot)
    return manifest, state, state_snapshot, snapshots, items


def _patient_turn(path: Path, item: dict) -> tuple[dict, _Snapshot]:
    captured = _Snapshot(_private_path(path))
    value = _source_object(captured.raw)
    _require(set(value).issubset({'utterance', 'source_quotes', 'synthetic_question', 'no_new_facts', 'validated_by'})
             and isinstance(value.get('utterance'), str) and value['utterance'].strip()
             and value.get('no_new_facts') is True and value.get('validated_by') == 'root',
             'Patient turn requires an explicit root no-new-facts declaration')
    quotes = value.get('source_quotes', [])
    synthetic = value.get('synthetic_question', False)
    _require(isinstance(quotes, list) and all(isinstance(quote, str) and quote.strip() for quote in quotes)
             and len(quotes) == len(set(quotes)) and type(synthetic) is bool,
             'Patient source quote fields differ')
    value['utterance'].encode('utf-8')
    user_sources = [message['content'] for message in item['prompt'] if message['role'] == 'user']
    _require(all(any(quote in source for source in user_sources) for quote in quotes),
             'Patient source quotes must be exact substrings of original user text')
    _require((synthetic and not quotes) or (not synthetic and bool(quotes)),
             'Patient turn needs exact user quotes or a root-validated synthetic question')
    audit = {**value, 'source_byte_sha256': captured.sha256, 'source_path': str(captured.path),
             'semantic_validation': 'Root-declared; exact quote audit is not independent semantic verification'}
    return audit, captured


def _messages(out: Path, item: dict, case: dict, patient: dict | None) -> list[dict]:
    messages = [{'role': 'system', 'content': GROUNDED_SUFFIX + '\n\n' + TASK_WRAPPER}, *deepcopy(item['prompt'])]
    for entry in case['steps']:
        row = _source_object((out / entry['path']).read_bytes())
        _require(row.get('status') == 'completed' and isinstance(row.get('answer'), str) and row['answer'].strip(),
                 'An incomplete prior doctor reply cannot be continued')
        messages.append({'role': 'assistant', 'content': row['answer']})
        # The patient turn stored at step N was already in the request before
        # doctor reply N. For the next request, recover it from the later step.
        if row['doctor_turn'] > 1:
            insertion = len(messages) - 1
            messages.insert(insertion, {'role': 'user', 'content': row['patient_turn']['utterance']})
    if patient is not None:
        messages.append({'role': 'user', 'content': patient['utterance']})
    return messages


def _receipt(response, client) -> dict:
    """Keep literal final content and receipt metadata; discard reasoning."""
    choice = None
    if isinstance(response, dict) and isinstance(response.get('choices'), list) and response['choices']:
        choice = response['choices'][0] if isinstance(response['choices'][0], dict) else None
    message = choice.get('message') if isinstance(choice, dict) and isinstance(choice.get('message'), dict) else {}
    content = message.get('content')
    if content is None:
        content = client.received_answer_text
    return {'provider_response_received': client.provider_response_received,
            'content': content if isinstance(content, str) else None,
            'finish_reason': choice.get('finish_reason') if isinstance(choice, dict) else None,
            'usage': deepcopy(response.get('usage')) if isinstance(response, dict) and isinstance(response.get('usage'), dict) else None,
            'reasoning_retained': False}


def step(out: Path, case_id: str, patient_turn: Path | None = None, *, stop: bool = False, transport=None) -> dict:
    """Execute at most one new doctor reply, then return control to root."""
    out = _private_path(out)
    _require(case_id in CASE_IDS, 'Only the four fixed development cases may be advanced')
    directory = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identity = _inode(os.fstat(directory))
    lock = None
    try:
        _require(_inode(out.stat()) == identity, 'Run directory identity changed')
        lock = os.open('.step.lock', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        os.fchmod(lock, 0o600)
        manifest, state, state_snapshot, snapshots, items = _load_run(out)
        _require(state['status'] in {'prepared', 'running'}, 'Run is already stopped or completed')
        case = state['cases'][case_id]
        _require(case['status'] != 'stopped', 'Case is already stopped; duplicate turns are forbidden')
        if stop:
            _require(patient_turn is None, 'A stop action cannot also submit a patient turn')
            case.update(status='stopped', stop_reason='root_marked_stop')
            state['status'] = 'completed' if all(value['status'] == 'stopped' for value in state['cases'].values()) else 'running'
            _guard(snapshots)
            _replace_state(out, state, state_snapshot)
            return {'status': 'stopped', 'case_status': 'stopped', 'run_status': state['status'],
                    'case_id': case_id, 'doctor_turn': case['doctor_turns'], 'calls': state['calls']}
        _require(state['reserved_calls'] < MAX_CALLS and case['doctor_turns'] < MAX_DOCTOR_TURNS, 'Generation budget exhausted')
        _require((case['doctor_turns'] == 0 and patient_turn is None)
                 or (case['doctor_turns'] > 0 and patient_turn is not None),
                 'First reply uses original prompt; later replies require a validated patient turn')
        patient = None
        if patient_turn is not None:
            patient, captured = _patient_turn(Path(patient_turn), items[case_id])
            snapshots.append(captured)
        payload = {'model': DEFAULT_MODEL, 'messages': _messages(out, items[case_id], case, patient),
                   'reasoning_effort': 'medium', 'max_tokens': 2048, 'temperature': 0}
        request_hash = canonical_hash(payload)
        _require(request_hash not in state['used_request_sha256'], 'Duplicate request hash is forbidden')
        client = _SourceTransport(transport if transport is not None else
                                  Transport(DEFAULT_BASE_URL, os.environ.get('HEALTHBENCH_API_KEY', ''), TIMEOUT),
                                  template_pin=TEMPLATE_SHA256)
        try:
            _guard(snapshots, client, MODEL_DIGEST)
            state_snapshot.verify()
        except (ValueError, _Blocked, OSError) as exc:
            state.update(status='blocked', stop_reason=exc.code if isinstance(exc, _Blocked) else 'source_pin_changed')
            state['model_metadata_calls'] += client.metadata_calls
            state['template_queries'] += client.template_queries
            _replace_state(out, state, state_snapshot)
            return {'status': 'blocked', 'case_status': case['status'], 'run_status': 'blocked',
                    'case_id': case_id, 'doctor_turn': case['doctor_turns'], 'calls': state['calls'],
                    'reason': state['stop_reason']}
        number = case['doctor_turns'] + 1
        state['reserved_calls'] += 1
        state['used_request_sha256'].append(request_hash)
        state['pending'] = {'case_id': case_id, 'doctor_turn': number, 'request_sha256': request_hash,
                            'reservation': 'No repeat is allowed after an unfinalized request; dispatch may be unknown'}
        state['status'] = 'running'
        state_snapshot = _replace_state(out, state, state_snapshot)
        client.expected_request_sha256 = request_hash
        started = time.monotonic()
        row = {'case_id': case_id, 'doctor_turn': number, 'call_sequence': state['reserved_calls'],
               'started_at': utc_now(), 'request': payload, 'request_sha256': request_hash,
               'input_sha256': canonical_hash(payload['messages']), 'patient_turn': patient,
               'source_prompt_sha256': manifest['source_prompt_sha256'][case_id],
               'status': 'failed', 'answer': '', 'finish_reason': None, 'error': None,
               'usage': {'prompt_tokens': None, 'completion_tokens': None, 'total_tokens': None},
               'digest_before': client.last_digest, 'digest_after': None,
               'template_before_sha256': client.last_template_sha256, 'template_after_sha256': None,
               'model_identity_verified': False, 'runtime_template_verified': False,
               'attempts': 1, 'retries': 0, 'score': None, 'score_status': SCORE_STATUS}
        response = None
        try:
            response = client.complete(payload)
            choice = response['choices'][0]
            row.update(answer=choice['message'].get('content') or '', finish_reason=choice['finish_reason'],
                       usage=usage_values(response))
            row['status'] = 'failed' if not row['answer'].strip() else ('completed' if row['finish_reason'] == 'stop' else 'truncated')
            if row['status'] == 'failed':
                row['error'] = {'code': 'empty_answer', 'http_status': None}
        except RequestFailure as exc:
            row['status'] = 'failed' if client.provider_response_received else 'blocked'
            row['error'] = {'code': exc.code, 'http_status': exc.http_status}
            row['usage'] = deepcopy(client.provider_usage)
        row.update(raw_receipt=_receipt(response, client), seconds=round(time.monotonic() - started, 3), finished_at=utc_now())
        # Persist the received evidence BEFORE the post-response identity check.
        record_path = out / 'cases' / case_id / f'turn-{number:02d}.json'
        _require(_inode(out.stat()) == identity, 'Run directory identity changed')
        _new_json(record_path, row)
        record_snapshot = _Snapshot(record_path)
        post_error = None
        try:
            _guard([*snapshots, record_snapshot], client, MODEL_DIGEST)
            state_snapshot.verify()
            row.update(digest_after=client.last_digest, template_after_sha256=client.last_template_sha256,
                       model_identity_verified=True, runtime_template_verified=True)
        except (ValueError, _Blocked, OSError, KeyboardInterrupt) as exc:
            post_error = exc.code if isinstance(exc, _Blocked) else ('interrupted' if isinstance(exc, KeyboardInterrupt) else 'source_pin_changed')
            row.update(digest_after=client.last_digest, template_after_sha256=client.last_template_sha256,
                       post_response_stop_reason=post_error)
        # Use a fresh sidecar for post-check evidence; the original receipt file
        # is immutable, including when a source changed after the actual call.
        post_path = record_path.with_name(f'turn-{number:02d}-post.json')
        _new_json(post_path, {'digest_after': row['digest_after'], 'template_after_sha256': row['template_after_sha256'],
                             'model_identity_verified': row['model_identity_verified'],
                             'runtime_template_verified': row['runtime_template_verified'],
                             'stop_reason': post_error})
        state['calls'] += client.calls
        state['model_metadata_calls'] += client.metadata_calls
        state['template_queries'] += client.template_queries
        state['pending'] = None
        case['doctor_turns'] = number
        case['steps'].append({'path': str(record_path.relative_to(out)), 'byte_sha256': record_snapshot.sha256,
                              'post_path': str(post_path.relative_to(out)), 'post_byte_sha256': _sha(post_path.read_bytes())})
        stopped = post_error is not None or row['status'] != 'completed' or case_id == 'HB-V0004' or number == MAX_DOCTOR_TURNS
        case['status'] = 'stopped' if stopped else 'awaiting_patient'
        if stopped:
            case['stop_reason'] = post_error or ('source_urgent_case_first_reply_stop' if case_id == 'HB-V0004'
                                                  else ('reply_limit' if number == MAX_DOCTOR_TURNS else row['status']))
        if post_error is not None or row['status'] == 'blocked' or client.interrupted:
            state.update(status='blocked', stop_reason=post_error or (row.get('error') or {}).get('code', 'interrupted'))
        else:
            state['status'] = 'completed' if all(value['status'] == 'stopped' for value in state['cases'].values()) else 'running'
        _replace_state(out, state, state_snapshot)
        return {'status': 'blocked' if post_error else row['status'], 'case_status': case['status'],
                'run_status': state['status'], 'case_id': case_id, 'doctor_turn': number, 'calls': state['calls'],
                'record_path': str(record_path), 'answer': row['answer'], 'reason': post_error,
                'score': None, 'score_status': SCORE_STATUS}
    finally:
        if lock is not None:
            os.close(lock)
            os.unlink('.step.lock', dir_fd=directory)
        os.close(directory)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    preparation = commands.add_parser('prepare')
    preparation.add_argument('--plan', type=Path, required=True)
    preparation.add_argument('--out', type=Path, required=True)
    advance = commands.add_parser('step')
    advance.add_argument('--out', type=Path, required=True)
    advance.add_argument('--case-id', choices=CASE_IDS, required=True)
    advance.add_argument('--patient-turn', type=Path)
    advance.add_argument('--stop', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = prepare(args.plan, args.out) if args.command == 'prepare' else step(args.out, args.case_id, args.patient_turn, stop=args.stop)
    except (ValueError, OSError, _Blocked, UnicodeError, KeyError, TypeError):
        print('Turn probe rejected or stopped; inspect private evidence; no automatic retry', file=sys.stderr)
        return 2
    print(json.dumps({key: value for key, value in result.items() if key not in {'answer'}}, ensure_ascii=False))
    return 2 if result['status'] == 'blocked' else (1 if result['status'] in {'failed', 'truncated'} else 0)


if __name__ == '__main__':
    sys.exit(main())
