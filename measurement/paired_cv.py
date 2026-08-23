#!/usr/bin/env python3
"""conquer_val 공식 루브릭으로 문항별 쌍대 비교 + outlier 민감도."""
import json, re, sys, time, urllib.request, statistics as st
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
URL="https://model.hackathon.lunit.io/v1/chat/completions"
DATA={r["prompt_id"]:r for r in json.load(open(ROOT/"conquer_val.json"))}
G=open(ROOT/"coeval_local.py").read().split('GRADER = """')[1].split('""".strip()')[0].strip()
def judge(conv,crit):
    p=G.replace("<<conversation>>",conv).replace("<<rubric_item>>",crit)
    for a in range(3):
        try:
            b=json.dumps({"model":"Lunit/L2-preview","messages":[{"role":"user","content":p}],
              "max_tokens":2048,"temperature":1.0,"chat_template_kwargs":{"enable_thinking":False}},ensure_ascii=False).encode()
            r=urllib.request.Request(URL,data=b,method="POST",headers={"Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
            with urllib.request.urlopen(r,timeout=180) as x: d=json.loads(x.read().decode())
            t=re.sub(r"^```json\s*|\s*```$","",(d["choices"][0]["message"].get("content") or "").strip())
            m=re.search(r"\{.*\}",t,re.S)
            if m:
                o=json.loads(m.group(0))
                if isinstance(o.get("criteria_met"),bool): return o["criteria_met"]
        except Exception: time.sleep(1.5*(a+1))
    return None
def per_q(name):
    A={r["qid"]:r["answer"] for r in json.load(open(ROOT/f"answers_{name}.json"))}
    jobs=[]
    for q,a in A.items():
        ex=DATA[q]
        conv="\n\n".join(f"{m['role']}: {m['content']}" for m in ex["prompt"])+f"\n\nassistant: {a}"
        for c in ex["rubrics"]: jobs.append((q,conv,c["criterion"],c["points"]))
    res=list(ThreadPoolExecutor(max_workers=10).map(lambda j:(j[0],judge(j[1],j[2]),j[3]),jobs))
    per=defaultdict(lambda:[0.0,0.0])
    for q,met,p in res:
        if met is None: continue
        if p>0: per[q][1]+=p
        if met: per[q][0]+=p
    return {q:(g/t) for q,(g,t) in per.items() if t>0}
a,b=sys.argv[1],sys.argv[2]
A,B=per_q(a),per_q(b)
qs=sorted(set(A)&set(B))
d=[(B[q]-A[q],q) for q in qs]
w=sum(1 for x,_ in d if x>0.001); l=sum(1 for x,_ in d if x<-0.001); t=len(d)-w-l
print(f"{b} vs {a}   n={len(qs)}")
print(f"  평균  {st.mean(A[q] for q in qs)*100:.2f} → {st.mean(B[q] for q in qs)*100:.2f}   ({st.mean(x for x,_ in d)*100:+.2f})")
print(f"  중앙  {st.median(A[q] for q in qs)*100:.2f} → {st.median(B[q] for q in qs)*100:.2f}   ({st.median(x for x,_ in d)*100:+.2f})")
print(f"  문항별 {w}승 {l}패 {t}동률")
d.sort(reverse=True)
print(f"  최대 상승 {d[0][0]*100:+.1f} ({d[0][1][:8]})   최대 하락 {d[-1][0]*100:+.1f} ({d[-1][1][:8]})")
rest=[x for x,_ in d[1:]]
print(f"  최대 상승 1건 제외 평균 {st.mean(rest)*100:+.2f}")
rest2=[x for x,_ in d[3:-3]]
print(f"  상하위 3건씩 제외 평균 {st.mean(rest2)*100:+.2f}")
