"""Package the explicit offline-guideline candidate and run an isolated smoke check.

No upload or model call. Raw documents and the derived database are excluded.
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
SOURCE = ROOT / 'submission_candidates/20261012-review-guidelines-sdk101'
MEMBERS = ('run.py', 'nova_sdk.py', 'requirements.txt', 'nova/__init__.py',
           'nova/guidelines.py', 'nova/guideline_prepare.py',
           'nova/guideline_context.py', 'nova/guideline_runtime.py')
SDK_SHA = 'fdd65302e82791ed862c22fe5a014ef443ab331d8986ec7c72b3923add6bc871'
REQUIREMENTS_SHA = 'd2b2ed1aed141db4390d0e782a7fc25b97328de2828b70958d99c145ea1d0843'
sha = lambda raw: hashlib.sha256(raw).hexdigest()

OFFLINE_CHECK = r'''
import contextlib,csv,io,json,os,socket,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
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
class Fake:
 timeout=240
 def __init__(self):self.calls=[];self.outputs=[{'action':'DONE'},{'action':'DONE'},{'diagnosis':'synthetic'}]
 def session(self):return {'initial':{'opening':'aspirin','vital_signs':'synthetic'},'turns_remaining':30,'deadline':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
 def chat(self,messages,**kwargs):
  self.calls.append((messages,kwargs))
  return {'choices':[{'message':{'role':'assistant','content':json.dumps(self.outputs.pop(0))},'finish_reason':'stop'}]}
 def diagnose(self,**kwargs):return {'status':'synthetic_diagnosed'}
c=Fake()
with contextlib.redirect_stderr(io.StringIO()):result=run.consult(c,state)
assert result['status']=='synthetic_diagnosed' and len(c.calls)==3
assert c.calls[0][0][1:3]==c.calls[1][0][1:3]
refs=json.loads(c.calls[0][0][2]['content'])['references']
assert len(refs)==1 and refs[0]['patient_observation'] is False
assert 'NOVA_GUIDELINE_REFERENCES' not in json.dumps(c.calls[2])
assert [x[1]['max_tokens'] for x in c.calls]==[2048,2048,4096]
assert all(x[1]['reasoning_effort']=='medium' for x in c.calls)
assert all('temperature' not in x[1] and 'model' not in x[1] for x in c.calls)
print(json.dumps({'isolated_import_prepare_consult':True,'network_denied':True,'synthetic_chat_calls':3,'real_model_calls':0,'real_http_requests':0,'reference_count':len(refs),'server_validated':False,'clinical_quality_verified':False}))
'''


def build(output):
    if SOURCE.is_symlink():
        raise ValueError('Candidate source must not be a symlink')
    captured = {}
    for name in MEMBERS:
        path = SOURCE / name
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Runtime file must be regular: '+name)
        captured[name] = path.read_bytes()
        captured[name].decode('utf-8')
        if name.startswith('nova/') and name != 'nova/__init__.py':
            if captured[name] != (ROOT/name).read_bytes():
                raise ValueError('Candidate/root module drift: '+name)
    if sha(captured['nova_sdk.py']) != SDK_SHA or sha(captured['requirements.txt']) != REQUIREMENTS_SHA:
        raise ValueError('Frozen SDK or requirements changed')
    stream = io.BytesIO()
    with ZipFile(stream,'w',compression=ZIP_DEFLATED) as archive:
        for name in MEMBERS:
            entry=ZipInfo(name,date_time=(2026,10,12,0,0,0))
            entry.create_system=3;entry.external_attr=0o100644<<16;entry.compress_type=ZIP_DEFLATED
            archive.writestr(entry,captured[name])
    raw=stream.getvalue()
    if len(raw)>50_000_000 or sum(map(len,captured.values()))>500_000_000:
        raise ValueError('NOVA archive size exceeded')
    with tempfile.TemporaryDirectory() as folder:
        unpacked=Path(folder).resolve()
        with ZipFile(io.BytesIO(raw)) as archive:
            assert archive.testzip() is None and set(archive.namelist()) == set(MEMBERS)
            for name in MEMBERS:
                assert archive.read(name)==captured[name]
                target=unpacked/name;target.parent.mkdir(exist_ok=True)
                target.write_bytes(archive.read(name))
        checked=subprocess.run([sys.executable,'-I','-S','-c',OFFLINE_CHECK,str(unpacked)],
                               cwd=unpacked,env={'PATH':os.defpath},capture_output=True,text=True,timeout=60)
        if checked.returncode:
            raise ValueError('Isolated offline check failed: '+checked.stderr)
        receipt=json.loads(checked.stdout)
    if any((SOURCE/name).read_bytes()!=body for name,body in captured.items()):
        raise ValueError('Runtime source changed during check')
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    filename='nova-review-guidelines-sdk101.zip'
    manifest={'status':'offline_synthetic_verified_not_submitted','zip_file':filename,
              'zip_bytes':len(raw),'zip_sha256':sha(raw),'members':list(MEMBERS),
              'source_sha256':{n:sha(b) for n,b in captured.items()},
              'unpacked_bytes':sum(map(len,captured.values())), 'sdk':'1.0.1',
              'offline_checks':receipt,'sample_submissions':0,'official_submissions':0,
              'clinical_quality_verified':False}
    (output/filename).write_bytes(raw)
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return manifest

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.output),indent=2))
