#!/usr/bin/env python3
"""샘플 다양성 — Best-of-N 이 가능한지 판정. 같은 질문을 3번 뽑아 차이를 잰다."""
import json, difflib, time, urllib.request, statistics as st, itertools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:8]

def call(it, temp):
    p={"model":"Lunit/L2-preview","messages":[{"role":"user","content":it["text"]}],
       "max_tokens":2048,"chat_template_kwargs":{"enable_thinking":False}}
    if temp is not None: p["temperature"]=temp
    body=json.dumps(p,ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception:
            time.sleep(2*(a+1))
    return ""

for label,temp in (("temperature=0.0",0.0),("temperature=0.2",0.2)):
    jobs=[(it,k) for it in items for k in range(3)]
    outs=list(ThreadPoolExecutor(max_workers=8).map(lambda j: call(j[0],temp), jobs))
    sims=[]
    for i,it in enumerate(items):
        trio=outs[i*3:i*3+3]
        if all(trio):
            sims += [difflib.SequenceMatcher(None,a,b).ratio() for a,b in itertools.combinations(trio,2)]
    print(f"{label:26s} 샘플쌍 {len(sims):2d} | 유사도 중앙 {st.median(sims):.2f} "
          f"(1.00=완전동일) | 최소 {min(sims):.2f} 최대 {max(sims):.2f}")
