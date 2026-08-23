#!/usr/bin/env python3
"""지시를 system 에 두느냐 user 턴에 두느냐 — L2 가 어느 쪽을 따르는가."""
import json, re, sys, time, urllib.request, statistics as st
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
items=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))[:int(sys.argv[1] if len(sys.argv)>1 else 40)]
INSTR=("Do not use emoji. Answer the core question first and directly. "
       "Keep it tight — no tangents the user did not ask for.")

def base(it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    return m, it["text"]

def build(mode,it):
    m,q=base(it)
    if mode=="none":   return m+[{"role":"user","content":q}]
    if mode=="system": return [{"role":"system","content":INSTR}]+m+[{"role":"user","content":q}]
    if mode=="prefix": return m+[{"role":"user","content":INSTR+"\n\n"+q}]
    if mode=="suffix": return m+[{"role":"user","content":q+"\n\n["+INSTR+"]"}]

def call(mode,it):
    body=json.dumps({"model":"Lunit/L2-preview","messages":build(mode,it),"max_tokens":2048,
        "chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            c=d["choices"][0]
            return {"a":c["message"].get("content") or "","fin":c.get("finish_reason","")}
        except Exception:
            time.sleep(2*(a+1))
    return {"a":"","fin":"error"}

EMOJI=re.compile("[\U0001F300-\U0001FAFF☀-➿️]")
for mode in ("none","system","prefix","suffix"):
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(lambda it: call(mode,it), items))
    ok=[r for r in rows if r["a"]]; L=sorted(len(r["a"]) for r in ok)
    print(f"{mode:7s} n={len(ok):2d} | 이모지 {sum(1 for r in ok if EMOJI.search(r['a'])):2d}/{len(ok)} "
          f"| 길이중앙 {L[len(L)//2]:5d} p90 {L[int(len(L)*.9)]:5d} >3000 {sum(1 for x in L if x>3000):2d} "
          f"| 잘림 {sum(1 for r in rows if r['fin']=='length'):2d} | ({time.monotonic()-t0:.0f}s)")
