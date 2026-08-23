# 측정 도구

전부 표준 라이브러리만 씁니다. `LUNIT_FM_API_KEY` 를 `.lunitkey` 에 넣고 실행합니다.

## 데이터셋 재구성

문항은 이 저장소에 없습니다. 아래로 만듭니다.

```bash
git clone https://github.com/lunit-io/CoEval
curl -sL -o hb_main.jsonl \
  https://openaipublic.blob.core.windows.net/simple-evals/healthbench/2025-05-07-06-14-12_oss_eval.jsonl

python3 - <<'PY'
import json
ids = set(json.load(open("CoEval/src/coeval/data/conquer_val_ids.json"))["prompt_ids"])
rows = [json.loads(l) for l in open("hb_main.jsonl", encoding="utf-8")]
rows = sorted((r for r in rows if r.get("prompt_id") in ids), key=lambda r: r["prompt_id"])
json.dump(rows, open("conquer_val.json", "w"), ensure_ascii=False)
print(len(rows), "문항")   # 301
PY
```

⚠️ `conquer_val.json` 과 `hb_main.jsonl` 은 카나리를 포함합니다. 커밋하지 마십시오.

## 주요 스크립트

| | |
|---|---|
| `coeval_local.py` | conquer_val 301문항 공식 루브릭 채점 + 축별 분해 |
| `paired_cv.py` | 문항별 승·패·동률, 극단값 민감도 |
| `control.py` | **노이즈 바닥.** 동일 설정 반복 측정 |
| `lang_audit.py` | 컨테이너의 언어 일치율·길이·지연·fallback |
| `ab.py` `sfx.py` `where.py` | 지시 위치·내용 A/B |
| `gen_cv.py` `cover.py` `ctx.py` `cond.py` | 프롬프트 변형 생성 |
| `think_ab.py` `sampling.py` `div.py` | thinking, 샘플링 파라미터, 출력 다양성 |
| `bo3.py` `synth.py` `cot2.py` | Best-of-N, 합성, 외부화 CoT — 전부 기각됨 |
| `mcpbench.py` `mcpaudit.py` | MCP 지연 분해, 도구 21개 응답 검증 |

## 읽는 법

**평균만 보지 마십시오.** 문항 점수는 감점 기준(전체의 30.5%) 때문에 하한이 없어
한 문항이 −242% 까지 갑니다. `paired_cv.py` 가 승패와 절사평균을 함께 냅니다.

**노이즈를 먼저 재십시오.** 같은 답변을 다시 채점하면 ±3, 대시보드도 같은 SHA 로
51.61 과 52.88 을 냈습니다. 그보다 작은 차이는 채택 근거가 되지 않습니다.
