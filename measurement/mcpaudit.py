#!/usr/bin/env python3
"""MCP 21개 도구 실검증 — 응답하는가 / 실제 값이 나오는가 / 기준일이 언제인가."""
import json, re, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
KEY=open("/Users/iganghun/Downloads/lunit-hackathon/.lunitkey").read().strip()
MCP="https://mcp.hackathon.lunit.io/mcp"
_id=[0]
def rpc(method,params,tag=""):
    _id[0]+=1
    h={"Accept":"application/json, text/event-stream","Content-Type":"application/json",
       "Authorization":f"Bearer {KEY}","MCP-Protocol-Version":"2025-06-18","Mcp-Method":method}
    if tag: h["Mcp-Name"]=tag
    t0=time.monotonic()
    try:
        r=urllib.request.Request(MCP,data=json.dumps({"jsonrpc":"2.0","id":_id[0],"method":method,"params":params},ensure_ascii=False).encode(),headers=h,method="POST")
        with urllib.request.urlopen(r,timeout=90) as x:
            raw=x.read().decode("utf-8","replace"); ct=x.headers.get("Content-Type","")
        if "event-stream" in ct:
            ms=[]
            for blk in raw.replace("\r\n","\n").split("\n\n"):
                dl=[l[5:].lstrip() for l in blk.splitlines() if l.startswith("data:")]
                if dl:
                    try: ms.append(json.loads("\n".join(dl)))
                    except: pass
            v=ms[-1] if ms else {}
        else: v=json.loads(raw)
        return round(time.monotonic()-t0,2), v, len(raw)
    except urllib.error.HTTPError as e:
        return round(time.monotonic()-t0,2), {"_http":e.code,"_body":e.read().decode()[:200]}, 0
    except Exception as e:
        return round(time.monotonic()-t0,2), {"_err":type(e).__name__}, 0

_,v,_=rpc("tools/list",{})
tools={t["name"]:t for t in v.get("result",{}).get("tools",[])}
print(f"도구 {len(tools)}개\n")
PROBES={
 "kcd_get_name":{"code":"E11.9"}, "kcd_search_codes":{"query":"제2형 당뇨병"},
 "openapi_hira_disease_check_code":{"code":"E11.9"},
 "openapi_hira_get_drug_price":{"query":"타이레놀"},
 "hira_updates_search":{"query":"오시머티닙 급여기준"},
 "openapi_mfds_check_drug_permission":{"query":"키트루다"},
 "openapi_mfds_get_drug_indication":{"query":"키트루다"},
 "adr_retrieve_drug_info":{"query":"warfarin"},
 "openapi_law_search":{"query":"국민건강보험법"},
 "openapi_law_list_articles":{"query":"국민건강보험법"},
 "rag_vector_query":{"query":"metformin lactic acidosis"},
 "rag_sql_query":{"query":"warfarin bleeding"},
 "rag_get_all_data_sources":{}, "rag_get_data_source_detail":{"query":"pubmed"},
 "index_list_documents":{}, "index_get_relevant_nodes":{"query":"고혈압 진료지침"},
 "index_keyword_search":{"query":"고혈압"}, "index_get_page_content":{"query":"고혈압"},
 "index_get_document_structure":{"query":"고혈압"},
}
DATE=re.compile(r"20[12]\d[-./년]\s?\d{1,2}")
def probe(name):
    args=PROBES.get(name,{"query":"고혈압"})
    t,v,n=rpc("tools/call",{"name":name,"arguments":args},name)
    if "_http" in v: return name,t,n,"HTTP "+str(v["_http"]),""
    if v.get("error"): return name,t,n,"error: "+str(v["error"])[:60],""
    blob=json.dumps(v.get("result",{}),ensure_ascii=False)
    empty = len(blob)<120 or '"content":[]' in blob.replace(" ","")
    dates=sorted(set(DATE.findall(blob)))[-3:]
    return name,t,n,("빈결과" if empty else "ok"),(",".join(dates))
res=list(ThreadPoolExecutor(max_workers=6).map(probe,sorted(tools)))
print(f"{'도구':36s} {'초':>5s} {'바이트':>8s}  상태      최신 날짜")
for n,t,b,s,d in res:
    print(f"{n:36s} {t:5.2f} {b:8d}  {s:10s} {d}")
