#!/usr/bin/env python3
"""외부화 2단 CoT — 추론과 생성이 각자 2048 을 온전히 쓴다."""
import json, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."
def fm(msgs,think,mt=2048):
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
def hist(it):
    h=it.get("history") or []
    return [{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
        if isinstance(h,list) and h and isinstance(h[0],dict) else []
PLAN=("Before answering, list what a correct and complete reply to this question must contain: "
      "the key clinical facts, anything that must be flagged, and how certain each point is. "
      "Bullet points only, no prose, under 200 words. Do not write the reply itself.\n\n{q}")
def cot2(it):
    plan,_=fm(hist(it)+[{"role":"user","content":PLAN.format(q=it["text"])}],True,700)
    q=it["text"]
    if plan: q += f"\n\n[Points your answer must cover:\n{plan[:2500]}]"
    q += f"\n\n[{NODUCK}]"
    a,f=fm(hist(it)+[{"role":"user","content":q}],False)
    if not a: a,_=fm(hist(it)+[{"role":"user","content":it['text']+chr(10)+chr(10)+'['+NODUCK+']'}],False)
    return {"qid":it["qid"],"answer":a}
def t25(it):
    m=hist(it)+[{"role":"user","content":it["text"]+"\n\n["+NODUCK+"]"}]
    a,f=fm(m,True)
    if f=="length" or not a: a,_=fm(m,False)
    return {"qid":it["qid"],"answer":a}
for name,fn in (("t25","t25"),("cot2","cot2")):
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(t25 if name=="t25" else cot2, items))
    json.dump(rows,open(ROOT/f"answers_{name}.json","w"),ensure_ascii=False)
    L=sorted(len(r["answer"]) for r in rows if r["answer"])
    print(f"{name:5s} 수집 {sum(1 for r in rows if r['answer'])}/40  길이중앙 {L[len(L)//2]:5d}  ({time.monotonic()-t0:.0f}s)")
