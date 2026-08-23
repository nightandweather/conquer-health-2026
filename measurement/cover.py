#!/usr/bin/env python3
import json, time, urllib.request, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."
COVER=("Address every distinct thing the user asked about, and state the warning signs that would "
       "require prompt medical attention.")
NOFAB=("Do not name a specific guideline document, society, or publication year unless you are certain "
       "it exists; state the recommendation without attributing it rather than attributing it wrongly.")
PROSE="Write in plain prose, not bullet lists or headings."
DATA=json.load(open(ROOT/"conquer_val.json"))[:80]
def fm(msgs,think,mt=6144):
    b=json.dumps({"model":"Lunit/L2-preview","messages":msgs,"max_tokens":mt,"temperature":0.0,
        "chat_template_kwargs":{"enable_thinking":think}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=b,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=240) as x: d=json.loads(x.read().decode())
            c=d["choices"][0]
            return (c["message"].get("content") or "").strip(), c.get("finish_reason","")
        except Exception: time.sleep(2*(a+1))
    return "",""
def gen(ex,instr):
    m=[dict(t) for t in ex["prompt"]]
    if m and m[-1]["role"]=="user": m[-1]["content"]+=f"\n\n[{instr}]"
    a,f=fm(m,True)
    if f=="length" or not a: a,f=fm(m,False)
    return a
V={"cv_cover": NODUCK+" "+COVER,
   "cv_nofab": NODUCK+" "+NOFAB,
   "cv_both":  NODUCK+" "+COVER+" "+NOFAB,
   "cv_prose": NODUCK+" "+COVER+" "+NOFAB+" "+PROSE}
for name,instr in V.items():
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=10).map(lambda ex:{"qid":ex["prompt_id"],"answer":gen(ex,instr)},DATA))
    json.dump(rows,open(ROOT/f"answers_{name}.json","w"),ensure_ascii=False)
    L=sorted(len(r["answer"]) for r in rows if r["answer"])
    print(f"{name}: {sum(1 for r in rows if r['answer'])}/80 길이중앙 {L[len(L)//2]} ({time.monotonic()-t0:.0f}s)",flush=True)
