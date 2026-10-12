"""Build the pinned, unmodified SDK 1.0.1 starter as an offline fallback.

This builder does not submit, make HTTP requests, or run a model. Its isolated
check uses synthetic replies and denies socket access before importing the ZIP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "submission_candidates/20261012-official-baseline-sdk101"
DEFAULT_OUTPUT = ROOT / "results/codex-day0-submission-v1/build-20261012-sdk101-v1"
SOURCE_URL = "https://nova.snubhai.org/docs/starter.zip"
SOURCE_ARCHIVE_SHA256 = "29d62dd2b0509cf02ed0bbb83191bb1ec3f57da72a0eb58cc6cbadcca8513aaf"
EXPECTED = {
    "nova_sdk.py": "fdd65302e82791ed862c22fe5a014ef443ab331d8986ec7c72b3923add6bc871",
    "requirements.txt": "d2b2ed1aed141db4390d0e782a7fc25b97328de2828b70958d99c145ea1d0843",
    "run.py": "93f281f959f84b2a020e7923d72fe7d7852730d7bdb480d17e85268df0416263",
}
MAX_ZIP_BYTES = 50_000_000
MAX_UNPACKED_BYTES = 500_000_000
MAX_FILES = 5_000


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_sources(source: Path) -> dict[str, bytes]:
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be a regular directory")
    captured = {}
    for name, expected in EXPECTED.items():
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError(f"Source is not a regular file: {name}")
        raw = path.read_bytes()
        if sha256(raw) != expected:
            raise ValueError(f"Official source SHA mismatch: {name}")
        raw.decode("utf-8", errors="strict")
        captured[name] = raw
    if len(captured) > MAX_FILES or sum(map(len, captured.values())) > MAX_UNPACKED_BYTES:
        raise ValueError("Source exceeds file or unpacked-size limits")
    # The pinned official requirements file has comments only. No dependency
    # download or environment-specific package is necessary for this fallback.
    if any(line.split("#", 1)[0].strip()
           for line in captured["requirements.txt"].decode().splitlines()):
        raise ValueError("Fallback must not require external packages")
    return captured


def archive_bytes(captured: dict[str, bytes]) -> bytes:
    if set(captured) != set(EXPECTED):
        raise ValueError("Only the three pinned runtime files may be packaged")
    for name, raw in captured.items():
        if sha256(raw) != EXPECTED[name]:
            raise ValueError(f"Official source SHA mismatch: {name}")
    import io
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in sorted(captured):
            entry = ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = ZIP_DEFLATED
            bundle.writestr(entry, captured[name], compresslevel=9)
    raw = stream.getvalue()
    if len(raw) > MAX_ZIP_BYTES:
        raise ValueError("ZIP exceeds 50,000,000 bytes")
    return raw


def validate_archive(archive: Path) -> dict[str, bytes]:
    """Validate everything before extracting any member."""
    if archive.is_symlink() or not stat.S_ISREG(archive.lstat().st_mode):
        raise ValueError("ZIP must be a regular file")
    if archive.stat().st_size > MAX_ZIP_BYTES:
        raise ValueError("ZIP exceeds 50,000,000 bytes")
    try:
        with ZipFile(archive) as bundle:
            members = bundle.infolist()
            names = [item.filename for item in members]
            if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_UNPACKED_BYTES:
                raise ValueError("ZIP exceeds file or unpacked-size limits")
            if len(names) != len(set(names)):
                raise ValueError("Duplicate ZIP member")
            if set(names) != set(EXPECTED):
                raise ValueError("ZIP must contain only the three root runtime files")
            for item in members:
                if item.flag_bits & 1 or item.is_dir():
                    raise ValueError("Encrypted or directory ZIP member")
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError("ZIP member is not a regular file")
            if bundle.testzip() is not None:
                raise ValueError("ZIP CRC mismatch")
            captured = {item.filename: bundle.read(item) for item in members}
    except (BadZipFile, RuntimeError) as exc:
        raise ValueError("Invalid ZIP") from exc
    for name, raw in captured.items():
        if sha256(raw) != EXPECTED[name]:
            raise ValueError(f"ZIP source SHA mismatch: {name}")
    return captured


OFFLINE_CHECK = r'''
import io, json, os, socket, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.clear()
def deny_network(*args, **kwargs):
    raise RuntimeError('OFFLINE_NETWORK_DENIED')
socket.create_connection = deny_network
socket.getaddrinfo = deny_network
socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.socket.sendto = deny_network
try:
    socket.create_connection(('example.invalid', 443))
except RuntimeError as exc:
    assert str(exc) == 'OFFLINE_NETWORK_DENIED'
else:
    raise AssertionError('Network guard did not run')

folder = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(folder))
import run
import nova_sdk
assert Path(run.__file__).resolve().parent == folder
assert Path(nova_sdk.__file__).resolve().parent == folder
assert nova_sdk.__version__ == '1.0.1'
assert nova_sdk.USER_AGENT == 'nova-sdk/1.0.1'
assert not os.environ
assert run.prepare() is None
assert run.REASONING_EFFORT == 'medium'

# Check actual SDK request construction, replacing its transport with an
# in-memory response before Client.chat is invoked. No HTTP request is sent.
wire = []
sdk_client = nova_sdk.Client(api_base='https://example.invalid',
                             token='SYNTHETIC_OFFLINE_TOKEN', session_id='synthetic')
def fake_open(request):
    wire.append((request, json.loads(request.data)))
    response = io.BytesIO(json.dumps({'choices': [{'message': {'content': '{}'}}]}).encode())
    response.status = 200
    return response
sdk_client._open = fake_open
sdk_client.chat([{'role': 'user', 'content': 'synthetic'}],
                max_tokens=2048, reasoning_effort='medium')
assert len(wire) == 1
assert wire[0][0].get_header('User-agent') == 'nova-sdk/1.0.1'
assert wire[0][1]['reasoning_effort'] == 'medium'
assert wire[0][1]['max_tokens'] == 2048
assert 'model' not in wire[0][1] and 'temperature' not in wire[0][1]

note = nova_sdk.empty_record()
note['S']['cc'] = '합성 연습 주호소'
note['S']['hpi'] = '합성 자료로 구성한 연습 기록'
note['O']['exam'] = '합성 진찰 결과'
note['A']['evidence'] = '수집한 합성 자료'
record_response = dict(note, diagnosis='합성 연습 진단')
say = {'action': 'SAY', 'content': '언제부터 증상이 있었나요?'}
exam = {'action': 'EXAM', 'content': '흉부 청진하겠습니다'}
done = {'action': 'DONE'}

class FakeClient:
    def __init__(self, steps, *, deadline_seconds=3600, invalid_exam=False,
                 patient_transient=False):
        self.responses = list(steps) + [record_response]
        self.chat_calls = []
        self.actions = []
        self.remaining = 30
        self.deadline_seconds = deadline_seconds
        self.invalid_exam = invalid_exam
        self.patient_transient = patient_transient
        self.diagnosis_calls = []
        self.say_attempts = 0
    def session(self):
        return {'initial': {'opening': '합성 환자 시작 진술',
                            'vital_signs': '합성 활력징후'},
                'turns_remaining': self.remaining,
                'deadline': (datetime.now(timezone.utc) +
                             timedelta(seconds=self.deadline_seconds)).isoformat()}
    def chat(self, messages, **kwargs):
        self.chat_calls.append((messages, kwargs))
        assert kwargs['reasoning_effort'] == 'medium'
        assert kwargs['max_tokens'] in (2048, 4096)
        response = self.responses.pop(0)
        return {'choices': [{'message': {'content':
                    json.dumps(response, ensure_ascii=False)}}]}
    def say(self, content):
        self.say_attempts += 1
        assert 0 < len(run.unicodedata.normalize('NFC', content)) <= 30
        if self.patient_transient and self.say_attempts == 1:
            raise nova_sdk.NovaError('PATIENT_RESPONSE_INVALID')
        self.actions.append(('SAY', content))
        self.remaining -= 1
        return {'content': '합성 환자 답변', 'turns_remaining': self.remaining}
    def exam(self, content):
        assert 0 < len(run.unicodedata.normalize('NFC', content)) <= 30
        self.actions.append(('EXAM', content))
        self.remaining -= 1
        if self.invalid_exam:
            self.invalid_exam = False
            raise nova_sdk.NovaError('EXAM_NOT_RECOGNIZED')
        return {'result': '합성 진찰 소견', 'turns_remaining': self.remaining}
    def diagnose(self, *, note, diagnosis):
        assert set(note) == set(nova_sdk.RECORD_FIELDS)
        for section, fields in nova_sdk.RECORD_FIELDS.items():
            assert set(note[section]) == set(fields)
            assert all(isinstance(value, str) for value in note[section].values())
        assert diagnosis == '합성 연습 진단'
        self.diagnosis_calls.append((note, diagnosis))
        return {'status': 'synthetic_diagnosed'}

scenarios = []
def finish(name, client, expected_actions, expected_chats):
    result = run.consult(client, run.prepare())
    assert result['status'] == 'synthetic_diagnosed'
    assert [action for action, content in client.actions] == expected_actions
    assert len(client.chat_calls) == expected_chats >= 1
    assert len(client.diagnosis_calls) == 1
    assert not client.responses
    assert client.chat_calls[-1][1]['max_tokens'] == 4096
    scenarios.append({'name': name, 'synthetic_chat_calls': expected_chats,
                      'synthetic_action_attempts': len(client.actions),
                      'diagnose_calls': 1, 'passed': True})
    return client

finish('say_exam_done_record', FakeClient([say, exam, done]), ['SAY', 'EXAM'], 4)
finish('deadline_margin_record_without_actions',
       FakeClient([], deadline_seconds=60), [], 1)
c = finish('rejected_exam_consumes_turn',
           FakeClient([exam, say, done], invalid_exam=True), ['EXAM', 'SAY'], 4)
assert '남은 턴: 29' in c.chat_calls[1][0][-1]['content']
assert '1턴 사용' in c.chat_calls[1][0][-1]['content']
assert '남은 턴: 28' in c.chat_calls[2][0][-1]['content']
long_say = {'action': 'SAY', 'content': '가' * 31}
c = finish('too_long_output_not_sent', FakeClient([long_say, say, done]), ['SAY'], 4)
assert '직전 출력은 형식에 맞지 않았습니다' in c.chat_calls[1][0][-1]['content']
sleeps = []
run.time.sleep = lambda seconds: sleeps.append(seconds)
c = finish('patient_provider_error_retry_without_turn_cost',
           FakeClient([say, exam, done], patient_transient=True), ['SAY', 'EXAM'], 4)
assert c.say_attempts == 2 and sleeps == [2]
assert c.remaining == 28

print(json.dumps({'status': 'offline_synthetic_checks_passed',
                  'sdk_version': nova_sdk.__version__, 'credentials_present': False,
                  'socket_access_denied': True, 'isolated_import_prepare': True,
                  'sdk_request_transport': 'in_memory_stub',
                  'sdk_wire_user_agent_verified': True,
                  'scenarios': scenarios, 'actual_http_requests': 0,
                  'actual_model_calls': 0, 'official_evaluation_requests': 0,
                  'server_validation_passed': None,
                  'clinical_quality_improvement': None}, ensure_ascii=False))
'''


def check_offline(archive: Path) -> dict:
    captured = validate_archive(archive)
    with tempfile.TemporaryDirectory(prefix="nova-day0-offline-") as folder:
        extracted = Path(folder)
        for name, raw in captured.items():
            (extracted / name).write_bytes(raw)
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", OFFLINE_CHECK, str(extracted)],
            cwd=extracted, env={"PATH": os.defpath}, capture_output=True,
            text=True, timeout=60,
        )
    if result.returncode:
        raise ValueError("Offline synthetic runtime check failed: " + result.stderr.strip())
    receipt = json.loads(result.stdout)
    if receipt.get("status") != "offline_synthetic_checks_passed":
        raise ValueError("Unexpected offline-check result")
    return receipt


def exclusive_write(path: Path, raw: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)


def build(source: Path = DEFAULT_SOURCE, output_dir: Path = DEFAULT_OUTPUT) -> dict:
    source, output_dir = Path(source), Path(output_dir)
    captured = read_sources(source)
    raw = archive_bytes(captured)
    # An existing result directory is never resumed or overwritten.
    output_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_dir.mkdir(mode=0o700)
    archive = output_dir / "day0-official-sdk101.zip"
    exclusive_write(archive, raw)
    offline = check_offline(archive)
    if read_sources(source) != captured:
        raise ValueError("Source changed during offline validation")
    manifest = {
        "schema_version": 1,
        "status": "prepared_pinned_official_fallback_offline_checked",
        "origin": {"url": SOURCE_URL, "archive_sha256": SOURCE_ARCHIVE_SHA256,
                   "receipt_date_kst": "2026-10-12"},
        "sdk_version": "1.0.1",
        "agent_recipe": "unmodified_official_starter_medium",
        "source_files": {name: {"bytes": len(captured[name]), "sha256": EXPECTED[name]}
                         for name in sorted(captured)},
        "zip_file": archive.name,
        "zip_bytes": len(raw), "zip_sha256": sha256(raw),
        "zip_members": sorted(captured), "unpacked_bytes": sum(map(len, captured.values())),
        "file_count": len(captured),
        "limit_checks": {"zip_bytes_max": MAX_ZIP_BYTES,
                         "unpacked_bytes_max": MAX_UNPACKED_BYTES,
                         "files_max": MAX_FILES, "passed": True},
        "deterministic_zip_metadata": True,
        "source_unchanged_before_after_offline_check": True,
        "builder_sha256": sha256(Path(__file__).read_bytes()),
        "offline_checks": offline,
        "server_sample_validation": "not_run",
        "official_evaluation": "not_requested",
        "clinical_quality": "not_measured",
        "guideline_or_healthbench_prompt_changes": False,
        "existing_root_agent_sdk_and_submission_zip_modified": False,
        "limitations": ["Synthetic replies do not establish model or clinical quality.",
                        "Actual server login, team quota, model response, and ZIP validation remain unchecked.",
                        "SDK 1.0.1 and this official baseline are paired; do not mix the new run.py with SDK 1.0.0."],
    }
    exclusive_write(output_dir / "manifest.json",
                    (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
    exclusive_write(output_dir / "offline-checks.json",
                    (json.dumps(offline, ensure_ascii=False, indent=2) + "\n").encode())
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT,
                        help="Fresh result directory; an existing directory is refused")
    args = parser.parse_args()
    try:
        manifest = build(output_dir=args.output_dir)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"Day-0 packaging failed: {exc}\n")
    print(json.dumps({"output_dir": str(args.output_dir),
                      "zip_file": manifest["zip_file"],
                      "zip_bytes": manifest["zip_bytes"],
                      "zip_sha256": manifest["zip_sha256"],
                      "status": manifest["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
