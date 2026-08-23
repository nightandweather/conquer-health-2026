#!/usr/bin/env python3
"""Best-of-3 — 후보 3개를 뽑고 rubric 없이 L2 가 고른다(실전과 동일 조건)."""
import json, re, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."

def fm(messages, max_tokens=2048, temp=0.0):
    body=json.dumps({"model":"Lunit/L2-preview","messages":messages,"max_tokens":max_tokens,
        "temperature":temp,"chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception: time.sleep(2*(a+1))
    return ""

def gen(it,_k):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    m.append({"role":"user","content":it["text"]+"\n\n["+NODUCK+"]"})
    return fm(m)

SEL="""You are choosing the single best reply to give a person asking a medical question.

# Question
{q}

# Candidates
{c}

# Choose by, in order of importance
1. Factual accuracy — no incorrect medical statements.
2. It directly answers what was actually asked, early.
3. It is calibrated: definite where evidence is settled, uncertain only where it truly is.
4. It does not deflect to "see a professional" instead of answering.
5. No unnecessary bulk.

Return only JSON: {{"best": <1|2|3>}}"""

def pick(it,cands):
    good=[c for c in cands if c.strip()]
    if len(good)<2: return good[0] if good else ""
    c="\n\n".join(f"--- Candidate {i+1} ---\n{t[:3500]}" for i,t in enumerate(good))
    out=fm([{"role":"user","content":SEL.format(q=it["text"][:1200],c=c)}],max_tokens=120)
    m=re.search(r'"best"\s*:\s*([123])',out or "")
    i=int(m.group(1))-1 if m else 0
    return good[i] if 0<=i<len(good) else good[0]

t0=time.monotonic()
jobs=[(it,k) for it in items for k in range(3)]
outs=list(ThreadPoolExecutor(max_workers=10).map(lambda j: gen(*j), jobs))
cands={it["qid"]:outs[i*3:i*3+3] for i,it in enumerate(items)}
print(f"후보 3개 생성 완료 ({time.monotonic()-t0:.0f}s)")
t1=time.monotonic()
sel=list(ThreadPoolExecutor(max_workers=8).map(lambda it: {"qid":it["qid"],"answer":pick(it,cands[it["qid"]])}, items))
json.dump(sel,open(ROOT/"answers_bo3.json","w"),ensure_ascii=False)
json.dump([{"qid":q,"answer":c[0]} for q,c in cands.items()],open(ROOT/"answers_first.json","w"),ensure_ascii=False)
L=sorted(len(r["answer"]) for r in sel if r["answer"])
print(f"선택 완료 ({time.monotonic()-t1:.0f}s)  길이중앙 {L[len(L)//2]}  총 {time.monotonic()-t0:.0f}s")
