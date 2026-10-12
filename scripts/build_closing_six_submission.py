"""Freeze E05's closing-hint threshold experiment and check both ZIP runtimes.

No network/model calls or upload. Exactly one byte changes against pinned E03:
next_step closing-hint threshold 2 to 6. The hint does not force a SAY action.
Synthetic outputs test plumbing, not the Doctor's compliance or clinical quality.
"""
import argparse
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
SOURCE = ROOT / 'submission_candidates/20261012-closing-six-sdk101'
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
CANDIDATE_RUN_SHA = '5c0d765955c77475f3634fa5edb3da73d49ed82671fb0c9ae05a8b354f60c816'
OLD_CONDITION = b'    if turns_left <= 2:\n'
NEW_CONDITION = b'    if turns_left <= 6:\n'
sha = lambda raw: hashlib.sha256(raw).hexdigest()


def validate_captured(base, candidate):
    if set(base) != set(MEMBERS) or set(candidate) != set(MEMBERS):
        raise ValueError('Runtime member set drift')
    for name in MEMBERS:
        if sha(base[name]) != BASE_PINS[name]:
            raise ValueError('Frozen E03 source drift: ' + name)
        candidate[name].decode('utf-8')
        if name != 'run.py' and candidate[name] != base[name]:
            raise ValueError('Other runtime file drift: ' + name)
    before, after = base['run.py'], candidate['run.py']
    if before.count(OLD_CONDITION) != 1:
        raise ValueError('Expected one frozen closing condition')
    expected = before.replace(OLD_CONDITION, NEW_CONDITION)
    changes = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    if after != expected or len(before) != len(after) or len(changes) != 1:
        raise ValueError('Expected exactly the closing threshold byte change 2 to 6')
    if sha(after) != CANDIDATE_RUN_SHA:
        raise ValueError('Candidate run pin drift')
    return {'only_closing_hint_threshold_changed': True, 'changed_run_bytes': 1,
            'changed_byte_offset': changes[0], 'baseline_byte': '2', 'candidate_byte': '6',
            'other_runtime_files_identical': 9, 'record_prompt_is_frozen_E03': True}


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


# Compare pinned E03 with extracted E05 ZIP in separate isolated interpreters.
# All seven boundaries execute real prepare/consult code with fake SDK clients.
OFFLINE_CHECK = r'''
import contextlib,copy,csv,hashlib,io,json,os,socket,sys
from pathlib import Path
root=Path(sys.argv[1]).resolve(); variant=sys.argv[2]
assert variant in ('baseline','candidate')
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
SAY='\n이제 환자에게 가능성 높은 진단과 다음 계획을 쉬운 말로 설명하세요(SAY).'
EXAM='\n문진을 마무리하고 필요한 신체 진찰(EXAM)을 하세요.'
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
class Fake:
 timeout=240
 def __init__(self,turns):
  self.turns=turns;self.calls=[];self.actions=[];self.diagnoses=[]
  self.outputs=[{'action':'SAY','content':'합성 초안'},{'action':'EXAM','content':'합성 실제 진찰'},{'O':{'exam':'합성 실제 소견'},'diagnosis':'합성 주진단'}]
 def session(self):return {'initial':{'opening':'aspirin','vital_signs':'합성 활력'},'turns_remaining':self.turns,'deadline':'2100-01-01T00:00:00+00:00'}
 def chat(self,messages,**kwargs):
  self.calls.append(copy.deepcopy([messages,kwargs]))
  return {'choices':[{'message':{'role':'assistant','content':json.dumps(self.outputs.pop(0))},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':5,'total_tokens':25}}
 def say(self,content):raise AssertionError('Unreviewed draft was dispatched')
 def exam(self,content):
  self.actions.append(['EXAM',content]);return {'result':'합성 실제 소견','turns_remaining':0}
 def diagnose(self,**kwargs):self.diagnoses.append(kwargs);return {'status':'synthetic_diagnosed'}
boundaries=[]
for remaining in (7,6,5,4,3,2,1):
 state=run.prepare();c=Fake(remaining);stderr=io.StringIO()
 with contextlib.redirect_stderr(stderr):result=run.consult(c,state)
 assert result['status']=='synthetic_diagnosed' and len(c.calls)==3
 assert c.actions==[['EXAM','합성 실제 진찰']]
 expected=SAY if remaining <= (6 if variant=='candidate' else 2) else EXAM
 session=c.session()
 expected_request=run.situation(session,[])+f'\n\n남은 턴: {remaining}'+expected+'\n다음 행동:'
 assert c.calls[0][0][-1]['content']==expected_request
 assert c.calls[0][0][0]=={'role':'system','content':run.STEP_PROMPT}
 assert c.calls[1][0][0]=={'role':'system','content':run.REVIEW_PROMPT}
 assert c.calls[0][0][1:3]==c.calls[1][0][1:3]
 refs=json.loads(c.calls[0][0][2]['content'])['references']
 assert len(refs)==1 and refs[0]['patient_observation'] is False
 record=c.calls[-1][0]
 assert record[0]=={'role':'system','content':run.RECORD_PROMPT}
 assert len(record)==2 and 'NOVA_GUIDELINE_REFERENCES' not in json.dumps(record)
 assert record[1]['content']==run.situation(session,['진찰(합성 실제 진찰): 합성 실제 소견'])
 assert '합성 초안' not in record[1]['content']
 assert all(x[1]=={'max_tokens':(4096 if i==2 else 2048),'reasoning_effort':'medium'} for i,x in enumerate(c.calls))
 assert c.diagnoses[0]['note']['O']['exam']=='합성 실제 소견'
 stats=json.loads(stderr.getvalue())
 assert stats['all_chat']['attempted']==3 and stats['review_applied']==1
 assert [stats['chat_phases'][p]['attempted'] for p in ('draft','review','record')]==[1,1,1]
 assert '합성 실제 소견' not in stderr.getvalue()
 normalized=copy.deepcopy(c.calls)
 if 3 <= remaining <= 6:
  normalized[0][0][-1]['content']=run.situation(session,[])+f'\n\n남은 턴: {remaining}'+'\nVARIABLE_CLOSING_HINT\n다음 행동:'
 boundaries.append({'remaining':remaining,'actual_hint':'SAY' if expected==SAY else 'EXAM',
  'actual_calls_sha256':digest(c.calls),'normalized_calls_sha256':digest(normalized),
  'review_and_record_calls_sha256':digest(c.calls[1:]),'actions_sha256':digest(c.actions),
  'diagnose_payload_sha256':digest(c.diagnoses)})
print(json.dumps({'isolated_import_prepare_consult':True,'network_denied':True,'synthetic_sessions':7,'synthetic_chat_calls':21,'real_model_calls':0,'real_http_requests':0,'reference_count_per_session':1,'record_no_retrieval':True,'review_can_keep_exam_despite_say_hint':True,'prompt_sha256':{name:hashlib.sha256(getattr(run,name).encode()).hexdigest() for name in ('STEP_PROMPT','REVIEW_PROMPT','RECORD_PROMPT')},'limits':[run.MAX_TURNS,run.TIME_MARGIN,run.MAX_CHARS,run.MAX_CHAT_ATTEMPTS,run.MAX_COMPLETION_BUDGET,run.MAX_REPORTED_PROMPT_TOKENS,run.DRAFT_CHAT_RESERVE,run.RECORD_CHAT_RESERVE],'boundaries':boundaries,'server_validated':False,'clinical_quality_verified':False}))
'''


def isolated_check(files, directory, variant):
    directory.mkdir()
    for name, body in files.items():
        target = directory / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
    checked = subprocess.run([sys.executable, '-I', '-S', '-c', OFFLINE_CHECK,
                              str(directory), variant], cwd=directory,
                             env={'PATH': os.defpath}, capture_output=True,
                             text=True, timeout=60)
    if checked.returncode:
        raise ValueError('Isolated offline check failed: ' + checked.stderr)
    return json.loads(checked.stdout)


def validate_receipts(before, after):
    if {k: v for k, v in before.items() if k != 'boundaries'} != {
            k: v for k, v in after.items() if k != 'boundaries'}:
        raise ValueError('Synthetic settings or prompt drift')
    rows = before.get('boundaries'), after.get('boundaries')
    if any([row.get('remaining') for row in group] != [7, 6, 5, 4, 3, 2, 1] for group in rows):
        raise ValueError('Synthetic boundary set drift')
    for old, new in zip(*rows):
        remaining = old['remaining']
        if old['actual_hint'] != ('SAY' if remaining <= 2 else 'EXAM') or new['actual_hint'] != ('SAY' if remaining <= 6 else 'EXAM'):
            raise ValueError('Wrong actual closing hint boundary')
        for field in ('normalized_calls_sha256', 'review_and_record_calls_sha256',
                      'actions_sha256', 'diagnose_payload_sha256'):
            if old[field] != new[field]:
                raise ValueError('Synthetic execution drift outside hint: ' + field)
        same_calls = old['actual_calls_sha256'] == new['actual_calls_sha256']
        if same_calls != (remaining not in (6, 5, 4, 3)):
            raise ValueError('Unexpected actual request difference')


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
        unpacked = {name: archive.read(name) for name in MEMBERS}
        if unpacked != candidate:
            raise ValueError('ZIP content mismatch')
    with tempfile.TemporaryDirectory() as folder:
        before = isolated_check(baseline, Path(folder) / 'baseline', 'baseline')
        after = isolated_check(unpacked, Path(folder) / 'candidate', 'candidate')
    validate_receipts(before, after)
    if capture(BASE) != baseline or capture(SOURCE) != candidate:
        raise ValueError('Runtime source changed during check')
    filename = 'nova-closing-six-sdk101.zip'
    manifest = {'status': 'offline_synthetic_verified_not_submitted', 'experiment': 'E05',
                'factor': 'closing_hint_threshold_2_to_6', 'zip_file': filename,
                'zip_bytes': len(raw), 'zip_sha256': sha(raw), 'members': list(MEMBERS),
                'source_sha256': {name: sha(body) for name, body in candidate.items()},
                'baseline_source_sha256': BASE_PINS, 'change_proof': proof,
                'unpacked_bytes': sum(map(len, candidate.values())), 'sdk': '1.0.1',
                'offline_checks': after, 'baseline_offline_checks': before,
                'sample_submissions': 0, 'official_submissions': 0,
                'clinical_quality_verified': False,
                'limits': 'Synthetic dispatch checks do not verify clinical behavior, quality, equal tokens or runtime, or server preparation. The hint does not force SAY. Same-UID concurrent changes are not fully excluded.'}
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


