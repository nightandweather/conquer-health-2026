#!/usr/bin/env python3
"""문서 과제 11건에서 의미 기반 자기-게이트가 실제로 작동하는가 (점수 아닌 기전 확인)."""
import json, re, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
NODUCK="Do not substitute 'consult a professional' for an answer; answer as far as you can."
CTX=("Address every distinct thing the user asked about, and state the warning signs that would "
     "require prompt medical attention. If the user offers information you would need — lab or imaging "
     "results, a medication list, measurements — ask them for it. If one decisive detail is missing and "
     "your answer would change because of it, ask for that one thing, while still answering as far as "
     "you can without it.")
DOCISO=("If the user asks you to draft, rewrite, summarize, or continue a clinical document, use only "
        "the information provided. Do not add a management plan, recommendations, warning signs, or new "
        "clinical facts unless the user explicitly asks for them.")
D=json.load(open(ROOT/"conquer_val.json"))[:80]
def th(e): return [t.split(':',1)[1] for t in (e.get('example_tags') or []) if t.startswith('theme:')]
DOC=[e for e in D if 'health_data_tasks' in th(e)]
def fm(msgs,think,mt=6144):
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
def gen(ex,instr):
    m=[dict(t) for t in ex["prompt"]]
    if m and m[-1]["role"]=="user": m[-1]["content"]+=f"\n\n[{instr}]"
    a,f=fm(m,True)
    if f=="length" or not a: a,f=fm(m,False)
    return a
PLAN=re.compile(r"\b(management plan|treatment plan|plan of care|admission diagnosis|disposition|"
    r"recommend(ation)?s?:|escalat|monitor(ing)? plan|follow[- ]up plan|next steps?:|assessment and plan|A/P\b)",re.I)
WARN=re.compile(r"\b(warning signs?|red flags?|seek (immediate|urgent|emergency)|call 911|go to the (ER|emergency)|"
    r"when to (seek|call)|escalate to)\b",re.I)
V={"champ":NODUCK, "ctx":NODUCK+" "+CTX, "ctx+iso":NODUCK+" "+CTX+" "+DOCISO}
out={}
for name,instr in V.items():
    t0=time.monotonic()
    rows=list(ThreadPoolExecutor(max_workers=8).map(lambda e:(e["prompt_id"],gen(e,instr)),DOC))
    out[name]=dict(rows)
    L=sorted(len(a) for _,a in rows if a)
    print(f"{name:9s} n={len(rows)} 길이중앙 {L[len(L)//2]:5d}  관리계획 {sum(1 for _,a in rows if PLAN.search(a)):2d}/{len(rows)}  경고징후 {sum(1 for _,a in rows if WARN.search(a)):2d}/{len(rows)}  ({time.monotonic()-t0:.0f}s)",flush=True)
json.dump(out,open(ROOT/"docgate.json","w"),ensure_ascii=False)
