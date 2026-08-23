#!/usr/bin/env python3
"""MCP 지연을 성분별로 분해한다."""
import json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path("/Users/iganghun/Downloads/lunit-hackathon"); KEY=(ROOT/".lunitkey").read_text().strip()
MCP="https://mcp.hackathon.lunit.io/mcp"
_id=[0]
def rpc(method,params,tag=""):
    _id[0]+=1
    payload={"jsonrpc":"2.0","id":_id[0],"method":method,"params":params}
    h={"Accept":"application/json, text/event-stream","Content-Type":"application/json",
       "Authorization":f"Bearer {KEY}","MCP-Protocol-Version":"2025-06-18","Mcp-Method":method}
    if tag: h["Mcp-Name"]=tag
    t0=time.monotonic()
    try:
        r=urllib.request.Request(MCP,data=json.dumps(payload,ensure_ascii=False).encode(),headers=h,method="POST")
        with urllib.request.urlopen(r,timeout=120) as x:
            raw=x.read().decode("utf-8","replace"); ct=x.headers.get("Content-Type","")
        if "event-stream" in ct:
            msgs=[]
            for blk in raw.replace("\r\n","\n").split("\n\n"):
                dl=[l[5:].lstrip() for l in blk.splitlines() if l.startswith("data:")]
                if dl:
                    try: msgs.append(json.loads("\n".join(dl)))
                    except: pass
            v=msgs[-1] if msgs else {}
        else: v=json.loads(raw)
        return round(time.monotonic()-t0,2), v, len(raw)
    except urllib.error.HTTPError as e:
        return round(time.monotonic()-t0,2), {"http":e.code,"body":e.read().decode()[:150]}, 0
    except Exception as e:
        return round(time.monotonic()-t0,2), {"err":type(e).__name__}, 0

print("① tools/list")
t,v,n=rpc("tools/list",{})
tools=[x["name"] for x in (v.get("result",{}).get("tools") or []) if isinstance(x,dict)]
print(f"   {t}s  도구 {len(tools)}개  응답 {n}바이트")
print(f"   {tools[:12]}")
if not tools: print("   →", json.dumps(v)[:250]); raise SystemExit

print("\n② tools/list 재호출 (캐시 효과 확인)")
t2,_,_=rpc("tools/list",{}); print(f"   {t2}s")

cand=[x for x in tools if any(k in x for k in ("relevant","search","query","keyword"))][:4] or tools[:4]
print(f"\n③ 개별 tools/call 순차")
seq=0
for name in cand:
    t,v,n=rpc("tools/call",{"name":name,"arguments":{"query":"고혈압 환자의 급여 기준"}},name)
    seq+=t
    ok = "result" in v
    print(f"   {name:36s} {t:5.2f}s  {n:7d}바이트  {'ok' if ok else json.dumps(v)[:70]}")
print(f"   순차 합계 {seq:.2f}s")

print(f"\n④ 같은 호출 병렬 (concurrent)")
t0=time.monotonic()
res=list(ThreadPoolExecutor(max_workers=len(cand)).map(
    lambda n: rpc("tools/call",{"name":n,"arguments":{"query":"고혈압 환자의 급여 기준"}},n), cand))
print(f"   병렬 합계 {time.monotonic()-t0:.2f}s   (순차 {seq:.2f}s 대비 {seq/(time.monotonic()-t0):.1f}배)")
