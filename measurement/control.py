#!/usr/bin/env python3
"""통제 실험 — 완전히 동일한 설정을 3번 돌린다. 이게 노이즈 바닥이다."""
import json, re, time, urllib.request, difflib, statistics as st
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:40]
def call(it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    m.append({"role":"user","content":it["text"]})
    body=json.dumps({"model":"Lunit/L2-preview","messages":m,"max_tokens":2048,"temperature":0.0,
        "chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception: time.sleep(2*(a+1))
    return ""
EMOJI=re.compile("[\U0001F300-\U0001FAFF☀-➿️]")
DUCK=re.compile(r"(consult|speak (with|to)|talk to|see|check with)[^.\n]{0,70}(doctor|physician|clinician|healthcare (provider|professional)|medical professional)",re.I)
runs=[]
for i in range(3):
    t0=time.monotonic()
    outs=list(ThreadPoolExecutor(max_workers=8).map(call, items))
    runs.append(outs)
    L=sorted(len(a) for a in outs if a)
    print(f"run{i+1}  이모지 {sum(1 for a in outs if EMOJI.search(a)):2d}  진료권유 {sum(1 for a in outs if DUCK.search(a)):2d}  "
          f"길이중앙 {L[len(L)//2]:5d}  p90 {L[int(len(L)*.9)]:5d}  ({time.monotonic()-t0:.0f}s)")
sims=[st.mean(difflib.SequenceMatcher(None,runs[i][k],runs[j][k]).ratio() for k in range(40))
      for i,j in ((0,1),(0,2),(1,2))]
print(f"\n동일 설정 run 간 답변 유사도: {st.mean(sims):.2f}  (1.00 = 완전 결정론)")
print(f"→ temperature=0 인데 재현되지 않음" if st.mean(sims)<0.9 else "→ 결정론적")
