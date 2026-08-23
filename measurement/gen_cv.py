#!/usr/bin/env python3
import json, time, urllib.request, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
SCR=Path("/private/tmp/claude-501/-Users-iganghun-Downloads-lunit-hackathon/23dce4e5-dbc4-4ef8-b957-24d951d725b5/scratchpad")
URL="https://model.hackathon.lunit.io/v1/chat/completions"
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."
DATA=json.load(open(ROOT/"conquer_val.json"))[:int(sys.argv[2]) if len(sys.argv)>2 else 80]
MT=int(sys.argv[3]) if len(sys.argv)>3 else 6144
def fm(msgs,think,mt):
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
    return "",""
def run(ex):
    m=[dict(t) for t in ex["prompt"]]
    if m and m[-1]["role"]=="user": m[-1]["content"]+=f"\n\n[{NODUCK}]"
    a,f=fm(m,True,MT)
    if f=="length" or not a: a,f=fm(m,False,MT)
    return {"qid":ex["prompt_id"],"answer":a}
t0=time.monotonic()
rows=list(ThreadPoolExecutor(max_workers=8).map(run,DATA))
json.dump(rows,open(ROOT/f"answers_{sys.argv[1]}.json","w"),ensure_ascii=False)
L=sorted(len(r["answer"]) for r in rows if r["answer"])
print(f"{sys.argv[1]}: {sum(1 for r in rows if r['answer'])}/{len(rows)}  길이중앙 {L[len(L)//2]}  ({time.monotonic()-t0:.0f}s)")
