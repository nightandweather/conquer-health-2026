#!/usr/bin/env python3
"""시스템 프롬프트 A/B — Docker 없이 L2 직접 호출. Trial 16 파라미터와 동일."""
import json, re, sys, time, urllib.request, statistics as st
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/Users/iganghun/Downloads/lunit-hackathon")
KEY  = (ROOT/".lunitkey").read_text().strip()
URL  = "https://model.hackathon.lunit.io/v1/chat/completions"
DATA = ROOT/"conquer-health-lunit-submission/data/hb_val.json"
N    = int(sys.argv[1]) if len(sys.argv) > 1 else 40

L_EMOJI = "Do not use emoji."
L_CORE  = ("Answer the core question first and directly. Never open with a disclaimer, "
           "a refusal, or a restatement of the question.")
L_TIGHT = ("Keep the answer tight. Do not pad it with background, tangents, or caveats "
           "the user did not ask for.")
L_HEDGE = ("State plainly what is well established. Hedge only where genuine uncertainty "
           "remains, and never substitute 'consult a professional' for an answer.")
L_ASK   = ("If a decisive detail is missing and your answer would change depending on it, "
           "ask exactly one short question. Otherwise do not ask anything.")

VARIANTS = {
  "A_baseline": None,
  "B_emoji":    L_EMOJI,
  "C_core":     L_CORE,
  "D_tight":    L_TIGHT,
  "E_hedge":    L_HEDGE,
  "F_all4":     "\n".join([L_EMOJI, L_CORE, L_TIGHT, L_HEDGE]),
  "G_all5":     "\n".join([L_EMOJI, L_CORE, L_TIGHT, L_HEDGE, L_ASK]),
}

items = json.load(open(DATA))[:N]
def msgs_of(it):
    h = it.get("history") or []
    m = [{"role": x["role"], "content": x["content"]} for x in h
         if isinstance(x, dict)] if isinstance(h, list) and h and isinstance(h[0], dict) else []
    m.append({"role": "user", "content": it["text"]})
    return m

def call(sysmsg, it):
    m = ([{"role": "system", "content": sysmsg}] if sysmsg else []) + msgs_of(it)
    body = json.dumps({"model": "Lunit/L2-preview", "messages": m, "max_tokens": 2048,
                       "chat_template_kwargs": {"enable_thinking": False}}, ensure_ascii=False).encode()
    for a in range(3):
        try:
            r = urllib.request.Request(URL, data=body, method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
            with urllib.request.urlopen(r, timeout=180) as x:
                d = json.loads(x.read().decode())
            c = d["choices"][0]
            return {"qid": it["qid"], "a": c["message"].get("content") or "",
                    "fin": c.get("finish_reason", "")}
        except Exception:
            time.sleep(1.5*(a+1))
    return {"qid": it["qid"], "a": "", "fin": "error"}

EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")
OPEN  = re.compile(r"^[^.\n]{0,140}(I (can'?t|cannot|am not able|am unable)|I'?m not able|As an AI|I am not a (doctor|medical)|It'?s important to note)", re.I)
ASK   = re.compile(r"\?\s*$", re.M)

rub = {x["qid"]: x["official_rubrics"] for x in json.load(open(DATA))}
def bucket(qid):
    for r in rub[qid]:
        c = r["cluster"]
        if "seeks_context" in c:
            if "any-reducible" in c: return "must_ask"
            return "must_not_ask"
    return None

def report(name, rows):
    ok = [r for r in rows if r["a"]]
    L  = sorted(len(r["a"]) for r in ok)
    em = sum(1 for r in ok if EMOJI.search(r["a"]))
    op = sum(1 for r in ok if OPEN.search(r["a"]))
    tr = sum(1 for r in rows if r["fin"] == "length")
    ko = sum(1 for r in ok if re.search(r"[가-힣]", r["a"]))
    ma = [r for r in ok if bucket(r["qid"]) == "must_ask"]
    mn = [r for r in ok if bucket(r["qid"]) == "must_not_ask"]
    ma_hit = sum(1 for r in ma if ASK.search(r["a"]))
    mn_bad = sum(1 for r in mn if ASK.search(r["a"]))
    print(f"{name:12s} n={len(ok):2d} | 이모지 {em:2d} | 오프너 {op:2d} | 잘림 {tr:2d} | ko {ko:2d} "
          f"| 길이 중앙 {L[len(L)//2]:5d} p90 {L[int(len(L)*.9)]:5d} >3000 {sum(1 for x in L if x>3000):2d} "
          f"| 되묻기 필요 {ma_hit}/{len(ma)} 금지위반 {mn_bad}/{len(mn)}")
    return {"name": name, "rows": rows}

out = {}
for name, sysmsg in VARIANTS.items():
    t0 = time.monotonic()
    rows = list(ThreadPoolExecutor(max_workers=8).map(lambda it: call(sysmsg, it), items))
    out[name] = report(name, rows)
    print(f"{'':12s}   ({time.monotonic()-t0:.0f}s)")
json.dump(out, open(ROOT/"ab_results.json","w"), ensure_ascii=False)
