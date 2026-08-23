#!/usr/bin/env python3
"""엄격 judge — 기준을 만족하는 문장을 직접 인용하게 강제한다.
인용을 못 하면 not met. 도장 찍기(94% met)를 막는 표준 기법."""
import json, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
D={x["qid"]:x for x in json.load(open(ROOT/"conquer-health-lunit-submission/data/hb_val.json"))}

T = """You are a strict grader. Score the assistant's last turn against ONE rubric item.

# Conversation
<<conversation>>

# Rubric item
<<rubric_item>>

# How to grade
Default to criteria_met = false. Only mark true if you can quote the exact sentence(s)
from the assistant's response that satisfy the rubric item. If the rubric item has
multiple parts, EVERY part must be satisfied. If you cannot quote supporting text,
or if you are unsure, the answer is false.

Return only a JSON object:
{"quote": "<verbatim sentence from the response, or empty string>", "criteria_met": <true|false>}"""

def msgs_of(it):
    h=it.get("history") or []
    m=[{"role":x["role"],"content":x["content"]} for x in h if isinstance(x,dict)] \
      if isinstance(h,list) and h and isinstance(h[0],dict) else []
    m.append({"role":"user","content":it["text"]}); return m

def judge(conv, crit):
    p=T.replace("<<conversation>>",conv).replace("<<rubric_item>>",crit)
    for a in range(3):
        try:
            body=json.dumps({"model":"Lunit/L2-preview","temperature":0.0,"max_tokens":400,
                "messages":[{"role":"user","content":p}],
                "chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
            r=urllib.request.Request(URL,data=body,method="POST",headers={
              "Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            t=d["choices"][0]["message"].get("content") or ""
            m=re.search(r"\{.*\}",t,re.S)
            if m:
                o=json.loads(m.group(0))
                met=bool(o.get("criteria_met"))
                # 인용을 못 했으면 met 로 인정하지 않는다
                if met and not str(o.get("quote") or "").strip(): met=False
                return met
        except Exception: time.sleep(1.5*(a+1))
    return None

for name in sys.argv[1:]:
    rows=json.load(open(ROOT/f"answers_{name}.json"))
    jobs=[]
    for r in rows:
        it=D[r["qid"]]
        conv="\n\n".join(f"{m['role']}: {m['content']}" for m in msgs_of(it))+f"\n\nassistant: {r['answer']}"
        for rb in it["official_rubrics"]: jobs.append((r["qid"],conv,rb["criterion"],rb["points"]))
    res=list(ThreadPoolExecutor(max_workers=8).map(lambda j:(j[0],judge(j[1],j[2]),j[3]),jobs))
    per={}
    for qid,met,pts in res:
        per.setdefault(qid,[0.0,0.0,0])
        if met is None: per[qid][2]+=1; continue
        if pts>0: per[qid][1]+=pts
        if met: per[qid][0]+=pts
    sc=[max(0,min(1,g/t)) for g,t,_ in per.values() if t>0]
    got=sum(g for g,_,_ in per.values()); tot=sum(t for _,t,_ in per.values())
    print(f"{name:5s} 로컬 {sum(sc)/len(sc)*100:5.2f}  (met {got/tot*100:4.1f}%, 문항 {len(sc)}, judge실패 {sum(b for _,_,b in per.values())})")
