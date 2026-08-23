#!/usr/bin/env python3
"""제출 컨테이너의 언어 일치율·길이·fallback 을 잰다.

사용: python3 lang_audit.py <포트> [문항수]
  예: docker run -d --name x -p 18098:8000 <이미지>
      python3 lang_audit.py 18098 40

문항 원문은 출력하지 않는다(HealthBench 공개 금지 조항).
"""
import json, re, sys, time, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

PORT = sys.argv[1] if len(sys.argv) > 1 else "18098"
N    = int(sys.argv[2]) if len(sys.argv) > 2 else 40
EP   = f"http://127.0.0.1:{PORT}/v1/chat/completions"
DATA = "/Users/iganghun/Downloads/lunit-hackathon/conquer-health-lunit-submission/data/hb_val.json"
FALLBACK = ("죄송합니다. 일시적인 오류", "현재 의료 답변 생성 서비스에 일시적인 문제")

def lang(t):
    for pat, name in ((r"[가-힣]","ko"),(r"[一-鿿]","zh"),(r"[぀-ヿ]","ja"),
                      (r"[Ѐ-ӿ]","ru"),(r"[؀-ۿ]","ar"),(r"[ऀ-ॿ]","hi")):
        if re.search(pat, t): return name
    return "latin"

def ask(item):
    h = item.get("history") or []
    msgs = [{"role": m["role"], "content": m["content"]} for m in h if isinstance(m, dict)] \
           if h and isinstance(h[0], dict) else []
    msgs.append({"role": "user", "content": item["text"]})
    body = json.dumps({"model": "medai", "messages": msgs}, ensure_ascii=False).encode()
    t0 = time.monotonic()
    try:
        r = urllib.request.Request(EP, data=body, headers={"Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(r, timeout=300) as x: d = json.loads(x.read().decode())
        a = d["choices"][0]["message"]["content"] or ""
        st = "FALLBACK" if a.startswith(FALLBACK) else ("EMPTY" if not a.strip() else "OK")
    except Exception as e:
        a, st = "", f"EXC:{type(e).__name__}"
    return {"qid": item["qid"], "status": st, "sec": round(time.monotonic()-t0,1),
            "chars": len(a), "a_lang": lang(a) if a else "-"}

rows = list(ThreadPoolExecutor(max_workers=4).map(ask, json.load(open(DATA))[:N]))
ok  = [r for r in rows if r["status"] == "OK"]
bad = [r for r in ok if r["a_lang"] != "latin"]          # 질문은 전부 비한국어(라틴/기타)
cs, ss = sorted(r["chars"] for r in ok), sorted(r["sec"] for r in ok)
print(f"n={len(rows)}  OK={len(ok)}  fallback/err={len(rows)-len(ok)}")
print(f"언어 불일치: {len(bad)}건 ({len(bad)/len(rows)*100:.0f}%)  {dict(Counter(r['a_lang'] for r in bad))}")
print(f"길이 중앙 {cs[len(cs)//2]}  ({cs[0]}~{cs[-1]})")
print(f"지연 중앙 {ss[len(ss)//2]}s  p90 {ss[int(len(ss)*0.9)]}s  최대 {ss[-1]}s")
json.dump(rows, open(f"lang_audit_{PORT}.json","w"), ensure_ascii=False, indent=1)
