#!/usr/bin/env python3
"""assistant prefill 로 '핵심 먼저' 를 지시가 아니라 구조로 강제한다."""
import json, re, time, urllib.request, statistics as st
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:60]
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."

def base_msgs(it, instr=True):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    q=it["text"]+("\n\n["+NODUCK+"]" if instr else "")
    m.append({"role":"user","content":q}); return m

def call(it, prefill):
    m=base_msgs(it)
    p={"model":"Lunit/L2-preview","max_tokens":2048,"temperature":0.0,
       "chat_template_kwargs":{"enable_thinking":False}}
    if prefill:
        m=m+[{"role":"assistant","content":prefill}]
        p.update({"add_generation_prompt":False,"continue_final_message":True})
    p["messages"]=m
    body=json.dumps(p,ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            c=(d["choices"][0]["message"].get("content") or "")
            return {"qid":it["qid"],"answer":(prefill+c) if prefill else c,"fin":d["choices"][0].get("finish_reason","")}
        except Exception: time.sleep(2*(a+1))
    return {"qid":it["qid"],"answer":"","fin":"err"}

V={"champ(현재)":None,
   "prefill_direct":"**Direct answer:** ",
   "prefill_bare":"Directly: "}
DUCK=re.compile(r"(consult|speak (with|to)|talk to|see|check with)[^.\n]{0,70}(doctor|physician|clinician|healthcare (provider|professional)|medical professional)",re.I)
PREAMBLE=re.compile(r"^[^.\n]{0,160}(it'?s important|it is important|there are (several|many|a few)|before (I|we)|thanks for|great question|I understand|that'?s a)",re.I)
for name,pf in V.items():
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(lambda it: call(it,pf), items))
    ok=[r for r in rows if r["answer"]]; L=sorted(len(r["answer"]) for r in ok)
    json.dump(rows,open(ROOT/f"answers_{name.split('(')[0]}.json","w"),ensure_ascii=False)
    print(f"{name:16s} n={len(ok):2d} 서론형오프너 {sum(1 for r in ok if PREAMBLE.search(r['answer'].replace(pf or '','',1))):2d} "
          f"진료권유 {sum(1 for r in ok if DUCK.search(r['answer'])):2d} 길이중앙 {L[len(L)//2]:5d} "
          f"잘림 {sum(1 for r in rows if r['fin']=='length'):2d} ({time.monotonic()-t0:.0f}s)")
