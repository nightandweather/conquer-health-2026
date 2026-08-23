#!/usr/bin/env python3
"""temp 0 에서 샘플링 파라미터 스윕. (temp0 은 greedy 라 top_p/top_k 는 무효여야 정상)"""
import json, re, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
V={
 "base(t0)":        {},
 "rep_pen 1.05":    {"repetition_penalty":1.05},
 "rep_pen 1.10":    {"repetition_penalty":1.10},
 "freq_pen 0.3":    {"frequency_penalty":0.3},
 "pres_pen 0.3":    {"presence_penalty":0.3},
 "top_p 0.9(sanity)":{"top_p":0.9},
}
def call(extra,it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    m.append({"role":"user","content":it["text"]})
    p={"model":"Lunit/L2-preview","messages":m,"max_tokens":2048,"temperature":0.0,
       "chat_template_kwargs":{"enable_thinking":False}, **extra}
    body=json.dumps(p,ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            c=d["choices"][0]
            return {"qid":it["qid"],"a":c["message"].get("content") or "","fin":c.get("finish_reason","")}
        except Exception as e:
            if a==2: return {"qid":it["qid"],"a":"","fin":f"err:{type(e).__name__}"}
            time.sleep(2*(a+1))
EMOJI=re.compile("[\U0001F300-\U0001FAFF☀-➿️]")
DUCK=re.compile(r"(consult|speak (with|to)|talk to|see|check with)[^.\n]{0,70}(doctor|physician|clinician|healthcare (provider|professional)|medical professional)",re.I)
def rep_ratio(t):   # 3-gram 중복률 — 붕괴/반복 탐지
    w=t.split()
    if len(w)<20: return 0.0
    g=[" ".join(w[i:i+3]) for i in range(len(w)-2)]
    return 1-len(set(g))/len(g)
base=None
for name,extra in V.items():
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(lambda it: call(extra,it), items))
    ok=[r for r in rows if r["a"]]; L=sorted(len(r["a"]) for r in ok)
    if base is None: base={r["qid"]:r["a"] for r in ok}
    import difflib
    sim=sum(difflib.SequenceMatcher(None,base.get(r["qid"],""),r["a"]).ratio() for r in ok)/max(len(ok),1)
    print(f"{name:19s} n={len(ok):2d} 이모지{sum(1 for r in ok if EMOJI.search(r['a'])):3d} 진료권유{sum(1 for r in ok if DUCK.search(r['a'])):3d} "
          f"길이중앙{L[len(L)//2]:5d} p90{L[int(len(L)*.9)]:5d} 반복률{sum(rep_ratio(r['a']) for r in ok)/len(ok):.3f} "
          f"잘림{sum(1 for r in rows if r['fin']=='length'):2d} base유사{sim:.2f} ({time.monotonic()-t0:.0f}s)")
    json.dump(rows,open(ROOT/f"samp_{name.split('(')[0].strip().replace(' ','_')}.json","w"),ensure_ascii=False)
