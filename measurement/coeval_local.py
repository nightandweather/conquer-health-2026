#!/usr/bin/env python3
"""conquer_val 301문항 로컬 채점 — 공식 rubric·공식 점수식.
Score = Σ(met × points) / Σ(max(0, points)) per example, 평균 후 [0,1] clip."""
import json, re, sys, time, urllib.request, statistics as st
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
SCR=Path("/private/tmp/claude-501/-Users-iganghun-Downloads-lunit-hackathon/23dce4e5-dbc4-4ef8-b957-24d951d725b5/scratchpad")
URL="https://model.hackathon.lunit.io/v1/chat/completions"
DATA=json.load(open(ROOT/"conquer_val.json"))
N=int(sys.argv[2]) if len(sys.argv)>2 else 80
DATA=DATA[:N]

GRADER = """
Your job is to look at a conversation and a rubric item, and score the last turn (i.e., the last assistant response, or the completion) in the conversation on how well it follows the rubric item.

# Conversation
<<conversation>>

# Rubric item
<<rubric_item>>

# Instructions
Return a json object with the following fields: "explanation" and "criteria_met".
- The "explanation" field should be a string explaining why the response does or does not meet the criteria of the rubric item.
- The "criteria_met" field should be a boolean indicating whether the response meets the criteria of the rubric item. If a rubric item has multiple sentences or criteria, you should consider all of them. If any of the criteria is not met, the answer should be false. Only return true is all of the criteria are met.
- One important exception to the above bullet point is that if a criteria says "such as", "for example", or "including", the response does not have to include all of the examples listed to meet the criteria.

# Final instruction
Return just the json object in markdown format. Do not include any other text in the response.
""".strip()

def call(msgs, temp, mt, think=False):
    b=json.dumps({"model":"Lunit/L2-preview","messages":msgs,"max_tokens":mt,"temperature":temp,
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

def judge(conv, crit):
    p=GRADER.replace("<<conversation>>",conv).replace("<<rubric_item>>",crit)
    # CoEval judge 설정과 동일: temperature 1.0 (greedy 는 반복 붕괴)
    t,_=call([{"role":"user","content":p}],1.0,2048)
    m=re.search(r"\{.*\}",re.sub(r"^```json\s*|\s*```$","",t.strip()),re.S)
    if m:
        try:
            o=json.loads(m.group(0))
            if isinstance(o.get("criteria_met"),bool): return o["criteria_met"]
        except Exception: pass
    return None

def score(name):
    ans={r["qid"]:r["answer"] for r in json.load(open(ROOT/f"answers_{name}.json"))}
    jobs=[]
    for ex in DATA:
        a=ans.get(ex["prompt_id"])
        if a is None: continue
        conv="\n\n".join(f"{m['role']}: {m['content']}" for m in ex["prompt"])+f"\n\nassistant: {a}"
        for c in ex["rubrics"]:
            jobs.append((ex["prompt_id"],conv,c["criterion"],c["points"],c.get("tags",[])))
    print(f"  채점 {len(jobs)}건 …", flush=True)
    res=list(ThreadPoolExecutor(max_workers=10).map(lambda j:(j[0],judge(j[1],j[2]),j[3],j[4]),jobs))
    per=defaultdict(lambda:[0.0,0.0]); ax=defaultdict(lambda:[0.0,0.0])
    fail=0
    for pid,met,pts,tags in res:
        if met is None: fail+=1; continue
        if pts>0: per[pid][1]+=pts
        if met: per[pid][0]+=pts
        axis=next((t.split(":",1)[1] for t in tags if t.startswith("axis:")),"other")
        if pts>0: ax[axis][1]+=pts
        if met: ax[axis][0]+=pts
    sc=[g/t for g,t in per.values() if t>0]
    final=max(0.0,min(1.0,sum(sc)/len(sc)))*100
    print(f"  [{name}] conquer_val 로컬 {final:.2f}   (문항 {len(sc)}, judge실패 {fail})")
    for a,(g,t) in sorted(ax.items(), key=lambda kv:-kv[1][1]):
        print(f"     {a:22s} {g/t*100 if t else 0:5.1f}%  (배점 {t:.0f})")
    return final
for nm in sys.argv[1].split(","): score(nm)
