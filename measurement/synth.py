#!/usr/bin/env python3
"""Draft→Synthesize: rubric 은 개별 '내용 항목' 을 채점하므로 초안 3개의 합집합이 유리한가."""
import json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."
def fm(msgs,mt=2048):
    b=json.dumps({"model":"Lunit/L2-preview","messages":msgs,"max_tokens":mt,"temperature":0.0,
        "chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=b,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=200) as x: d=json.loads(x.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception: time.sleep(2*(a+1))
    return ""
def hist(it):
    h=it.get("history") or []
    return [{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
        if isinstance(h,list) and h and isinstance(h[0],dict) else []
def draft(j):
    it=j[0]
    return fm(hist(it)+[{"role":"user","content":it["text"]+"\n\n["+NODUCK+"]"}])
SYN=("Below are {n} independent draft answers to the same question. Write ONE final answer "
     "that keeps every correct, useful point that appears in any draft, drops anything that "
     "contradicts the others or looks invented, and reads as a single coherent reply. "
     "Do not mention the drafts.\n\n# Question\n{q}\n\n{d}")
def synth(it,ds):
    good=[d for d in ds if d.strip()]
    if len(good)<2: return good[0] if good else ""
    d="\n\n".join(f"--- Draft {i+1} ---\n{t[:3000]}" for i,t in enumerate(good))
    return fm([{"role":"user","content":SYN.format(n=len(good),q=it["text"][:1500],d=d)}])
t0=time.monotonic()
jobs=[(it,k) for it in items for k in range(3)]
outs=list(ThreadPoolExecutor(max_workers=10).map(draft,jobs))
C={it["qid"]:outs[i*3:i*3+3] for i,it in enumerate(items)}
print(f"초안 3개 생성 ({time.monotonic()-t0:.0f}s)")
t1=time.monotonic()
sy=list(ThreadPoolExecutor(max_workers=8).map(lambda it:{"qid":it["qid"],"answer":synth(it,C[it["qid"]])},items))
json.dump(sy,open(ROOT/"answers_synth.json","w"),ensure_ascii=False)
json.dump([{"qid":q,"answer":c[0]} for q,c in C.items()],open(ROOT/"answers_d1.json","w"),ensure_ascii=False)
L=sorted(len(r["answer"]) for r in sy if r["answer"])
print(f"합성 완료 ({time.monotonic()-t1:.0f}s) 길이중앙 {L[len(L)//2]}  총 {time.monotonic()-t0:.0f}s")
