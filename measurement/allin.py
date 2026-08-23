#!/usr/bin/env python3
import json, time, urllib.request, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
ND="Do not substitute 'consult a professional' for an answer; answer as far as you can."
def fm(m,think,mt=2048):
    b=json.dumps({"model":"Lunit/L2-preview","messages":m,"max_tokens":mt,"temperature":0.0,
        "chat_template_kwargs":{"enable_thinking":think}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=b,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=240) as x: d=json.loads(x.read().decode())
            c=d["choices"][0]; return (c["message"].get("content") or "").strip(), c.get("finish_reason","")
        except Exception: time.sleep(2*(a+1))
    return "","err"
def H(it):
    h=it.get("history") or []
    return [{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
        if isinstance(h,list) and h and isinstance(h[0],dict) else []
def U(it,extra=""): return H(it)+[{"role":"user","content":it["text"]+extra+"\n\n["+ND+"]"}]

STATS={"A_retry":[0,0]}
def base(it):                      # Trial 25 재현
    m=U(it); a,f=fm(m,True)
    if f=="length" or not a: a,f=fm(m,False)
    return {"qid":it["qid"],"answer":a}
def retry(it):                     # ① thinking 재시도로 18% 회수
    m=U(it); a,f=fm(m,True)
    if f=="length" or not a:
        STATS["A_retry"][0]+=1
        a2,f2=fm(m,True)
        if f2!="length" and a2: STATS["A_retry"][1]+=1; return {"qid":it["qid"],"answer":a2}
        a,f=fm(m,False)
    return {"qid":it["qid"],"answer":a}
SYN=("Below are {n} independent draft answers to the same question. Write ONE final answer that keeps "
     "every correct, useful point that appears in any draft, drops anything that contradicts the others "
     "or looks invented, and reads as a single coherent reply. Do not mention the drafts.\n\n"
     "# Question\n{q}\n\n{d}")
def synth(it):                     # ② 초안 3개 합성
    ds=[base(it)["answer"] for _ in range(3)]
    g=[d for d in ds if d.strip()]
    if len(g)<2: return {"qid":it["qid"],"answer":g[0] if g else ""}
    d="\n\n".join(f"--- Draft {i+1} ---\n{t[:3000]}" for i,t in enumerate(g))
    a,_=fm([{"role":"user","content":SYN.format(n=len(g),q=it["text"][:1500],d=d)}],True)
    return {"qid":it["qid"],"answer":a or g[0]}
SHOT=("""Example of the style expected.

Q: I've had a dry cough for three weeks after a cold. Should I be worried?
A: A dry cough lingering three to eight weeks after a viral infection is common and usually resolves on its own — this is called post-infectious cough. The airway lining stays irritated after the virus clears.
What tends to help: honey (adults and children over 1), staying hydrated, and avoiding smoke and cold dry air. Inhaled corticosteroids help some people if the cough is airway-hyperreactive, but that needs a prescription.
Get it checked sooner if you cough up blood, lose weight without trying, have night sweats or fever, get short of breath, or the cough is getting worse rather than slowly better — those point away from a simple post-viral cough.

Now answer the next question in the same manner.

""")
def fewshot(it):                   # ③ 자작 예시 few-shot (평가 데이터 미사용)
    m=H(it)+[{"role":"user","content":SHOT+it["text"]+"\n\n["+ND+"]"}]
    a,f=fm(m,True)
    if f=="length" or not a: a,f=fm(m,False)
    return {"qid":it["qid"],"answer":a}
V={"base":base,"A_retry":retry,"C_fewshot":fewshot,"B_synth":synth}
for name,fn in V.items():
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(fn,items))
    json.dump(rows,open(ROOT/f"answers_{name}.json","w"),ensure_ascii=False)
    L=sorted(len(r["answer"]) for r in rows if r["answer"])
    ex=f"  (재시도 {STATS['A_retry'][0]}건 중 {STATS['A_retry'][1]} 회수)" if name=="A_retry" else ""
    print(f"{name:10s} {sum(1 for r in rows if r['answer'])}/40  길이중앙 {L[len(L)//2]:5d}  {time.monotonic()-t0:.0f}s{ex}", flush=True)
