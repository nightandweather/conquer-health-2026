"""Freeze E04's final-record instruction experiment and check both ZIP runtimes.

No network/model calls or upload. Exactly RECORD_PROMPT changes against pinned
E03; the instruction bundle is one factor, not an effect estimate for each rule.
Synthetic outputs test plumbing, not the Doctor's compliance or clinical quality.
"""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'submission_candidates/20261012-review-memory-guidelines-sdk101'
SOURCE = ROOT / 'submission_candidates/20261012-record-contract-sdk101'
BASE_PINS = {
    'run.py': '6bb27da593be842f993ab819bf1519af28d226f02eb102b1990d72f4de0d1b1e',
    'nova_sdk.py': 'fdd65302e82791ed862c22fe5a014ef443ab331d8986ec7c72b3923add6bc871',
    'requirements.txt': 'd2b2ed1aed141db4390d0e782a7fc25b97328de2828b70958d99c145ea1d0843',
    'nova/__init__.py': 'f2a0592bd8716ed166974e4f04b76a8e92463239b95dabdb904e4d1a686db9cd',
    'nova/guidelines.py': '4e039d55b65d08737bf8a1a45e5eae53b031c97435f5bf3053b4297a2c18d8b9',
    'nova/guideline_prepare.py': 'f2e9d9cd8ccc5be00412dc9af9aaa76b1062926a1ad48b24a83e1fb5f3606093',
    'nova/guideline_context.py': 'd844b54ee020ce15802d3d99f839d9499ec4b3be6931fc13de68a1224632d53e',
    'nova/guideline_runtime.py': '1a953363c1e8b92df5f9556dd23ef76017a3fb549406a083fe0d797bf92fd69b',
    'nova/guideline_memory.py': '24d10e3731edb56f3b7ea92c76fb5e54cdae18b263fc3702462ea9cc6ef77949',
    'nova/guideline_memory_runtime.py': '6a7b49b7a2ddf790e2deb77ef31f0d2b97f7e16de97518c029ac3a7834682ad4',
}
MEMBERS = tuple(BASE_PINS)
CANDIDATE_RUN_SHA = '82f2077cb719bd5681f52fefcae6a9fd957a1dca8037eb12c8ba762bd10b0a32'
PROMPT_SHA = 'dd07452117dd2218732153999d70464321fd5a5867c24c3cbe17abdc76545008'
sha = lambda raw: hashlib.sha256(raw).hexdigest()


def prompt_parts(raw):
    """Use AST UTF-8 byte offsets; compare exact bytes outside the assignment."""
    tree = ast.parse(raw)
    matches = [n for n in tree.body if isinstance(n, ast.Assign) and
               any(isinstance(t, ast.Name) and t.id == 'RECORD_PROMPT' for t in n.targets)]
    if len(matches) != 1:
        raise ValueError('Expected one RECORD_PROMPT assignment')
    node = matches[0]
    if (len(node.targets) != 1 or not isinstance(node.value, ast.Constant)
            or type(node.value.value) is not str):
        raise ValueError('RECORD_PROMPT must be one literal string')
    lines = raw.splitlines(keepends=True)
    start = sum(map(len, lines[:node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[:node.end_lineno - 1])) + node.end_col_offset
    rest = ast.Module(body=[n for n in tree.body if n is not node], type_ignores=[])
    return raw[:start], raw[end:], node.value.value, ast.dump(rest, include_attributes=False)


def validate_captured(base, candidate):
    if set(base) != set(MEMBERS) or set(candidate) != set(MEMBERS):
        raise ValueError('Runtime member set drift')
    for name in MEMBERS:
        if sha(base[name]) != BASE_PINS[name]:
            raise ValueError('Frozen E03 source drift: ' + name)
        candidate[name].decode('utf-8')
        if name != 'run.py' and candidate[name] != base[name]:
            raise ValueError('Non-prompt runtime drift: ' + name)
    before, after = prompt_parts(base['run.py']), prompt_parts(candidate['run.py'])
    if before[:2] != after[:2] or before[3] != after[3]:
        raise ValueError('Change outside RECORD_PROMPT assignment')
    if before[2] == after[2] or sha(after[2].encode()) != PROMPT_SHA:
        raise ValueError('Unapproved RECORD_PROMPT')
    if sha(candidate['run.py']) != CANDIDATE_RUN_SHA:
        raise ValueError('Candidate run pin drift')
    return {'only_record_prompt_assignment_changed': True,
            'outside_assignment_bytes_identical': True, 'other_runtime_files_identical': 9,
            'baseline_record_prompt_sha256': sha(before[2].encode()),
            'candidate_record_prompt_sha256': sha(after[2].encode()),
            'baseline_record_prompt_utf8_bytes': len(before[2].encode()),
            'candidate_record_prompt_utf8_bytes': len(after[2].encode())}


def capture(directory):
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Source directory must be a real directory')
    result = {}
    for name in MEMBERS:
        path = directory / name
        for parent in path.parents:
            if parent == directory:
                break
            if parent.is_symlink():
                raise ValueError('Runtime parent must not be a symlink: ' + name)
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Runtime file must be regular: ' + name)
        result[name] = path.read_bytes()
    return result


# Run twice in separate isolated Python interpreters: E03 then actual E04 ZIP.
# Hash all synthetic request payloads so only RECORD system text can differ.
OFFLINE_CHECK = r'''
import contextlib,copy,csv,hashlib,io,json,os,socket,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve()
os.environ.clear()
def deny(*a,**kw): raise AssertionError('Network denied')
socket.socket.connect=deny
socket.socket.connect_ex=deny
socket.socket.sendto=deny
socket.create_connection=deny
socket.getaddrinfo=deny
sys.path.insert(0,str(root))
import run,nova_sdk
from nova import guidelines,guideline_runtime
assert Path(run.__file__).parent==root
assert Path(guidelines.__file__).parent==root/'nova'
assert Path(guideline_runtime.__file__).parent==root/'nova'
assert nova_sdk.__version__=='1.0.1'
source=root/'synthetic-corpus';source.mkdir();(source/'EN').mkdir()
output=root/'synthetic-output';output.mkdir(mode=0o700)
(source/'EN/a.md').write_text('---\ntitle: "Synthetic aspirin"\norganization: "Synthetic"\nyear: "2026"\nlanguage: "en"\nsource_url: "https://example.invalid"\nlicense: "synthetic"\n---\naspirin synthetic reference.\n')
row=dict(zip(guidelines.CATALOG_COLUMNS,['']*len(guidelines.CATALOG_COLUMNS)))
row.update({'파일명':'EN/a.md','제목':'Synthetic aspirin','발행기관':'Synthetic','연도':'2026','언어':'영어','문서 유형':'진료지침','형식':'Markdown','라이선스':'synthetic','출처 URL':'https://example.invalid'})
with (source/'catalog.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=guidelines.CATALOG_COLUMNS);w.writeheader();w.writerow(row)
os.environ.update(NOVA_GUIDELINES_DIR=str(source),NOVA_OUTPUT_DIR=str(output))
state=run.prepare()
record={'S':{'hpi':'합성 실제 환자 응답','pmh':'약 ( )'},'O':{'exam':'합성 진찰 결과','tests':''},'P':{'tests':'합성 추가 확인 계획','follow_up':'정보 부족: 추가 확인 필요','education':'실제 설명 없음; 교육 계획은 추가 확인 후 결정'},'diagnosis':'합성 주진단'}
class Fake:
 timeout=240
 def __init__(self):
  self.calls=[];self.actions=[];self.diagnoses=[]
  self.outputs=[{'action':'SAY','content':'합성 초안 질문'},{'action':'SAY','content':'합성 실제 질문'}, {'action':'EXAM','content':'합성 진찰'},{'action':'EXAM','content':'합성 진찰'},record]
 def session(self):return {'initial':{'opening':'aspirin','vital_signs':'합성 활력'},'turns_remaining':2,'deadline':'2100-01-01T00:00:00+00:00'}
 def chat(self,messages,**kwargs):
  self.calls.append(copy.deepcopy([messages,kwargs]))
  return {'choices':[{'message':{'role':'assistant','content':json.dumps(self.outputs.pop(0))},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'total_tokens':25}}
 def say(self,content):
  self.actions.append(['SAY',content]);return {'content':'합성 실제 환자 응답: 약 이름은 모릅니다','turns_remaining':1}
 def exam(self,content):
  self.actions.append(['EXAM',content]);return {'result':'합성 진찰 결과','turns_remaining':0}
 def diagnose(self,**kwargs):self.diagnoses.append(kwargs);return {'status':'synthetic_diagnosed'}
c=Fake();stderr=io.StringIO()
with contextlib.redirect_stderr(stderr):result=run.consult(c,state)
assert result['status']=='synthetic_diagnosed' and len(c.calls)==5
assert c.actions==[['SAY','합성 실제 질문'],['EXAM','합성 진찰']]
for i in (0,2):
 assert c.calls[i][0][1:3]==c.calls[i+1][0][1:3]
 refs=json.loads(c.calls[i][0][2]['content'])['references']
 assert len(refs)==1 and refs[0]['patient_observation'] is False
actual=c.calls[-1][0]
assert actual[0]=={'role':'system','content':run.RECORD_PROMPT}
assert len(actual)==2 and 'NOVA_GUIDELINE_REFERENCES' not in json.dumps(actual)
assert '합성 실제 환자 응답: 약 이름은 모릅니다' in actual[1]['content']
assert '합성 초안 질문' not in actual[1]['content']
assert '진찰(합성 진찰): 합성 진찰 결과' in actual[1]['content']
assert '턴' not in actual[1]['content']
assert [x[1]['max_tokens'] for x in c.calls]==[2048,2048,2048,2048,4096]
assert all(x[1]=={'max_tokens':(4096 if i==4 else 2048),'reasoning_effort':'medium'} for i,x in enumerate(c.calls))
assert c.diagnoses[0]['note']['O']['tests']=='' and c.diagnoses[0]['note']['P']=={k:record['P'].get(k,'') for k in nova_sdk.RECORD_FIELDS['P']}
stats=json.loads(stderr.getvalue())
assert stats['all_chat']['attempted']==5 and stats['chat_phases']['record']['attempted']==1
assert '합성 실제 환자 응답' not in stderr.getvalue()
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
normalized=copy.deepcopy(c.calls);normalized[-1][0][0]['content']='RECORD_PROMPT_VARIABLE'
print(json.dumps({'isolated_import_prepare_consult':True,'network_denied':True,'synthetic_chat_calls':5,'real_model_calls':0,'real_http_requests':0,'reference_count':len(refs),'record_no_retrieval':True,'record_system_sha256':hashlib.sha256(run.RECORD_PROMPT.encode()).hexdigest(),'normalized_actual_calls_sha256':digest(normalized),'actions_sha256':digest(c.actions),'diagnose_payload_sha256':digest(c.diagnoses),'limits':[run.MAX_TURNS,run.TIME_MARGIN,run.MAX_CHARS,run.MAX_CHAT_ATTEMPTS,run.MAX_COMPLETION_BUDGET,run.MAX_REPORTED_PROMPT_TOKENS,run.DRAFT_CHAT_RESERVE,run.RECORD_CHAT_RESERVE],'server_validated':False,'clinical_quality_verified':False}))
'''


def isolated_check(files, directory):
    directory.mkdir()
    for name, body in files.items():
        target = directory / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
    checked = subprocess.run([sys.executable, '-I', '-S', '-c', OFFLINE_CHECK, str(directory)],
                             cwd=directory, env={'PATH': os.defpath}, capture_output=True,
                             text=True, timeout=60)
    if checked.returncode:
        raise ValueError('Isolated offline check failed: ' + checked.stderr)
    return json.loads(checked.stdout)


def build(output):
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Immutable output already exists')
    baseline, candidate = capture(BASE), capture(SOURCE)
    proof = validate_captured(baseline, candidate)
    stream = io.BytesIO()
    with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
        for name in MEMBERS:
            entry = ZipInfo(name, date_time=(2026, 10, 12, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, candidate[name])
    raw = stream.getvalue()
    if len(raw) > 50_000_000 or sum(map(len, candidate.values())) > 500_000_000:
        raise ValueError('NOVA archive size exceeded')
    with ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() is not None or archive.namelist() != list(MEMBERS):
            raise ValueError('ZIP member or CRC failure')
        unpacked = {n: archive.read(n) for n in MEMBERS}
        if unpacked != candidate:
            raise ValueError('ZIP content mismatch')
    with tempfile.TemporaryDirectory() as folder:
        before = isolated_check(baseline, Path(folder) / 'baseline')
        after = isolated_check(unpacked, Path(folder) / 'candidate')
    if before['record_system_sha256'] != proof['baseline_record_prompt_sha256'] or after['record_system_sha256'] != PROMPT_SHA:
        raise ValueError('Actual RECORD payload does not match frozen prompt')
    if {k: v for k, v in before.items() if k != 'record_system_sha256'} != {
            k: v for k, v in after.items() if k != 'record_system_sha256'}:
        raise ValueError('Synthetic execution differs outside RECORD payload')
    if capture(BASE) != baseline or capture(SOURCE) != candidate:
        raise ValueError('Runtime source changed during check')
    filename = 'nova-record-contract-sdk101.zip'
    manifest = {'status': 'offline_synthetic_verified_not_submitted', 'experiment': 'E04',
                'factor': 'final_record_instruction_bundle', 'zip_file': filename,
                'zip_bytes': len(raw), 'zip_sha256': sha(raw), 'members': list(MEMBERS),
                'source_sha256': {n: sha(b) for n, b in candidate.items()},
                'baseline_source_sha256': BASE_PINS, 'change_proof': proof,
                'unpacked_bytes': sum(map(len, candidate.values())), 'sdk': '1.0.1',
                'offline_checks': after, 'baseline_offline_checks': before,
                'sample_submissions': 0, 'official_submissions': 0,
                'clinical_quality_verified': False,
                'limits': 'Synthetic dispatch checks do not verify semantic compliance, clinical quality, token cost, or server preparation. Same-UID concurrent changes are not fully excluded.'}
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with (output / filename).open('xb') as f:
        f.write(raw)
    with (output / 'manifest.json').open('x') as f:
        f.write(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.output), indent=2))
