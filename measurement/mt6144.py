#!/usr/bin/env python3
import json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
def fm(msgs,think,mt):
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
    return "","err"
STAT={}
def run(mt,it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    m.append({"role":"user","content":it["text"]+"\n\n["+NODUCK+"]"})
    a,f=fm(m,True,mt); fell=False
    if f=="length" or not a:
        STAT[mt]=STAT.get(mt,0)+1; a,f=fm(m,False,mt); fell=True
    return {"qid":it["qid"],"answer":a,"fell":fell}
for mt in (2048,6144):
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(lambda it: run(mt,it), items))
    json.dump(rows,open(ROOT/f"answers_mt{mt}.json","w"),ensure_ascii=False)
    L=sorted(len(r["answer"]) for r in rows if r["answer"])
    print(f"max_tokens={mt:5d}  fallback 발동 {STAT.get(mt,0):2d}/40  길이중앙 {L[len(L)//2]:5d}  p90 {L[int(len(L)*.9)]:5d}  ({time.monotonic()-t0:.0f}s)")
