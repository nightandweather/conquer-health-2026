#!/usr/bin/env python3
"""thinking on/off A/B — Trial 16 파라미터 그대로, 컨테이너 없이."""
import json, re, sys, time, urllib.request, statistics as st
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:int(sys.argv[1] if len(sys.argv)>1 else 40)]

def msgs_of(it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    m.append({"role":"user","content":it["text"]}); return m

def call(think,it):
    body=json.dumps({"model":"Lunit/L2-preview","messages":msgs_of(it),"max_tokens":2048,
        "chat_template_kwargs":{"enable_thinking":think}},ensure_ascii=False).encode()
    t0=time.monotonic()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=240) as x: d=json.loads(x.read().decode())
            c=d["choices"][0]; m=c["message"]
            return {"qid":it["qid"],"a":m.get("content") or "","fin":c.get("finish_reason",""),
                    "reason":len(m.get("reasoning") or m.get("reasoning_content") or ""),
                    "sec":round(time.monotonic()-t0,1),
                    "usage":(d.get("usage") or {}).get("completion_tokens",0)}
        except Exception as e:
            if a==2: return {"qid":it["qid"],"a":"","fin":"error","reason":0,
                             "sec":round(time.monotonic()-t0,1),"usage":0}
            time.sleep(2*(a+1))

for label,think in (("thinking OFF",False),("thinking ON",True)):
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(lambda it: call(think,it), items))
    ok=[r for r in rows if r["a"]]
    L=sorted(len(r["a"]) for r in ok)
    print(f"{label:13s} 응답 {len(ok)}/{len(rows)} | 빈content {sum(1 for r in rows if not r['a'] and r['fin']!='error'):2d}"
          f" | 잘림(length) {sum(1 for r in rows if r['fin']=='length'):2d}"
          f" | reasoning평균 {int(st.mean(r['reason'] for r in rows)):5d}자"
          f" | 길이중앙 {L[len(L)//2] if L else 0:5d} p90 {L[int(len(L)*.9)] if L else 0:5d}"
          f" | 지연중앙 {st.median(r['sec'] for r in rows):5.1f}s | 총 {time.monotonic()-t0:.0f}s")
    json.dump(rows,open(ROOT/f"think_{'on' if think else 'off'}.json","w"),ensure_ascii=False)
