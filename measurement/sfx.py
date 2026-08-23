#!/usr/bin/env python3
"""user 턴 suffix 로 지시를 하나씩 — 어느 것이 무엇을 바꾸는가."""
import json, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
D=json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))
items=D[:40]
rub={x["qid"]:x["official_rubrics"] for x in D}
V={
 "0_none":      None,
 "1_emoji":     "Do not use emoji.",
 "2_corefirst": "Answer the core question first and directly, without a preamble.",
 "3_tight":     "Keep it tight — no tangents the user did not ask for.",
 "4_noduck":    "Do not substitute 'consult a professional' for an answer; answer as far as you can.",
 "5_ask":       "If one decisive detail is missing and your answer would change because of it, ask exactly one short question at the end. Otherwise do not ask anything.",
 "6_emoji_core":"Do not use emoji. Answer the core question first and directly, without a preamble.",
}
def build(instr,it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    q=it["text"] if not instr else it["text"]+"\n\n["+instr+"]"
    return m+[{"role":"user","content":q}]
def call(instr,it):
    body=json.dumps({"model":"Lunit/L2-preview","messages":build(instr,it),"max_tokens":2048,"temperature":0.0,
        "chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
    for a in range(3):
        try:
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            return d["choices"][0]["message"].get("content") or ""
        except Exception: time.sleep(2*(a+1))
    return ""
EMOJI=re.compile("[\U0001F300-\U0001FAFF☀-➿️]"); ASK=re.compile(r"\?\s*$",re.M)
DUCK=re.compile(r"(consult|speak|talk|see)[^.\n]{0,50}(doctor|clinician|physician|healthcare|professional)",re.I)
def buck(q):
    for r in rub[q]:
        if "seeks_context" in r["cluster"]:
            return "must_ask" if "any-reducible" in r["cluster"] else "must_not"
    return None
for name,instr in V.items():
    t0=time.monotonic()
    outs=list(ThreadPoolExecutor(max_workers=8).map(lambda it: call(instr,it), items))
    ok=[(it,a) for it,a in zip(items,outs) if a]; L=sorted(len(a) for _,a in ok)
    ma=[(i,a) for i,a in ok if buck(i["qid"])=="must_ask"]; mn=[(i,a) for i,a in ok if buck(i["qid"])=="must_not"]
    print(f"{name:13s} 이모지{sum(1 for _,a in ok if EMOJI.search(a)):3d} 진료권유{sum(1 for _,a in ok if DUCK.search(a)):3d} "
          f"길이중앙{L[len(L)//2]:5d} p90{L[int(len(L)*.9)]:5d} >3000 {sum(1 for x in L if x>3000):2d} "
          f"되묻기 {sum(1 for _,a in ma if ASK.search(a))}/{len(ma)} 위반{sum(1 for _,a in mn if ASK.search(a))}/{len(mn)} ({time.monotonic()-t0:.0f}s)")
