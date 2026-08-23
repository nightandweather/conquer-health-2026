# 팀 AIM — 제출물 구조

**Conquer Health 해커톤 · 벤치마크 상 1위 · HealthBench Consensus 52.88**
Lunit L2 (Gravity 기반 30B / 활성 5B MoE) · 파인튜닝 없음 · 외부 모델 없음

---

## 1. 요청 흐름

```
CoEval 평가기
   │  POST /v1/chat/completions  (대화 전체, OpenAI 호환)
   ▼
app.py  ──  Deadline 시작 (요청 단위 예산)
   │
   ├─ 초안  _draft_raw            ← PIPELINE=raw (기본값, 채점된 경로)
   │        └ 마지막 user 턴에 답변 지시 부착
   │        └ L2 호출: temperature=0, max_tokens=6144, thinking=on
   │        └ 잘림·빈 응답이면 thinking=off 로 재생성
   │
   ├─ 초안  _draft_harness        ← PIPELINE=harness (선택, MCP 경로)
   │        └ router.classify → retrieval → generation
   │
   ├─ 검증  review.review          ← REVIEW_MODE=suspect (기본값)
   │        └ 규칙에 걸린 요청만 FM 재호출. 대부분은 통과
   │
   └─ 응답  OpenAI 호환 JSON
```

**빈 응답은 0점입니다.** CoEval 설정이 `score_inference_failures_as_zero: true` 이므로,
어떤 실패 경로에서도 문장을 반환하도록 이중 폴백을 둡니다.

---

## 2. 파일 지도

| 파일 | 줄 | 역할 | 채점 경로 |
|---|---:|---|:---:|
| `app.py` | 753 | HTTP 서버 · FM 호출 · 초안 라우팅 · 폴백 | ✅ |
| `budget.py` | 73 | 요청 단위 데드라인, 단계별 남은 시간 배분 | ✅ |
| `review.py` | 426 | 출력 검증층. 규칙에 걸린 것만 재작성 | ✅ |
| `router.py` | 426 | 질문 유형·언어·페르소나 분류 | harness |
| `retrieval.py` | 566 | MCP 도구 선택·호출·중복 차단 | harness |
| `generation.py` | 294 | 근거 결합 생성 | harness |
| `mcp_client.py` | 196 | MCP Streamable HTTP 클라이언트 | harness |
| `toolspec.py` | 231 | MCP 도구 정의 → OpenAI function 변환 | harness |
| `digest.py` / `compress.py` | 368 / 279 | 검색 결과 압축 (Evidence Card) | harness |
| `serve.py` | 243 | 로컬 개발용 stdlib 서버 (Makefile 전용) | — |

`PIPELINE` 환경변수 하나로 두 경로를 전환합니다. 최종 제출은 `raw`.

---

## 3. 점수를 만든 네 가지 결정

### ① `temperature: 0`  (+6.8)

호출 payload 에 temperature 를 명시하지 않으면 서버 기본값(≈1.0)으로 채점받습니다.
HealthBench 루브릭 3,410개 중 **정확성 축이 1,382점**이고, 고온 샘플링이 그걸 직격합니다.

```python
payload = {"model": FM_MODEL, "messages": msgs,
           "temperature": 0.0,          # 명시하지 않으면 서버 기본 ≈1.0
           "max_tokens": MAX_TOKENS,
           "chat_template_kwargs": {"enable_thinking": ENABLE_THINKING}}
```

### ② 지시를 `user` 턴에  (+3.8)

L2 는 `system` 메시지를 사실상 무시하고 마지막 `user` 턴을 따릅니다.
같은 문장(`"Do not use emoji."`)을 위치만 바꿔 40문항씩 측정한 결과 —

| 위치 | 이모지 잔존 |
|---|---|
| 지시 없음 | 10 / 40 |
| `system` 메시지 | 11 / 40 |
| `user` 턴 앞 | 2 / 40 |
| `user` 턴 뒤 | **0 / 40** |

```python
forwarded = [dict(m) for m in messages]
if forwarded and forwarded[-1].get("role") == "user":
    forwarded[-1]["content"] += f"\n\n[{ANSWER_INSTRUCTION}]"
```

### ③ thinking ON + 손상 시 폴백  (+6.7)

L2 는 추론을 별도 채널로 생성하지만 **최종 답변과 같은 출력 예산을 씁니다.**
켜면 정확도가 오르고, 예산이 모자라면 답변이 잘리거나 비어서 나옵니다.
끄는 대신 **손상된 것만 되살립니다.**

```python
data = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": True}})
choice = data["choices"][0]
content = (choice["message"].get("content") or "").strip()
if choice.get("finish_reason") == "length" or not content:
    data = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": False}})
    content = (data["choices"][0]["message"].get("content") or "").strip()
```

### ④ `max_tokens: 6144`  (+1.5)

2048 상한은 팀이 스스로 건 제한이었습니다. 엔드포인트는 8192 까지 받습니다.

| max_tokens | finish_reason | 폴백 발동 |
|---|---|---|
| 2048 | length | 9 / 40 |
| 6144 | stop | **0 / 40** |

지연은 동일했고(160s vs 165s / 40문항), 22% 의 답변이 추론을 버리던 문제가 사라졌습니다.

---

## 4. 측정 인프라

대시보드 제출 1회가 **25~35분**이라 그 속도로는 A/B 가 불가능했습니다.
채점을 로컬로 복원했습니다.

```
CoEval 공개 저장소의 conquer_val prompt-id 목록  (301개)
  +  공개 HealthBench Main 데이터셋
  =  리더보드와 동일한 301문항 · 기준 3,410개

공식 점수식 구현:  Σ(met × points) / Σ(max(0, points))
                  문항 평균 후 [0, 1] clip · 감점 기준 30.5% 포함
```

**캘리브레이션**: 로컬 45.0 vs 리더보드 47.8 — 3점 이내.
축별로 분해해 어디서 잃는지 문항 단위로 추적했습니다.

| 축 | 획득률 | 배점 |
|---|---:|---:|
| 완전성 | 45.2% | 1,450 |
| 정확성 | 44.0% | 1,382 |
| 맥락인지 | 35.6% | 711 |
| 의사소통 | 63.3% | 245 |
| 지시순응 | 70.6% | 209 |

---

## 5. 노이즈 관리

동일 SHA 를 두 번 제출한 결과가 **51.61 / 52.88** — 1.27점 차이.
CoEval 문서도 `n=500` 에서 `sd 0.011`, 약 3점 이내는 통계적 동률이라고 명시합니다.

그래서 채택 기준을 바꿨습니다.

- 평균이 아니라 **문항별 승·패·동률**
- **극단값 제거 후** 재계산 (문항 점수는 감점 때문에 하한이 없습니다 — 실제로 −242% 사례 발생)
- 동일 설정 3회로 **노이즈 바닥** 측정
- 3점 미만 차이는 채택하지 않음

이 규율이 실제로 걸러낸 사례가 있습니다. 한 프롬프트 변경이 평균 +2.5점으로 보였는데,
문항별로 보니 한 문항이 만든 착시였고 그 문항을 빼면 평균이 음수였습니다.
원인은 **문서 작성 과제**였습니다 — 상담용 지시가 H&P 노트에 관리계획을 덧붙이게 만들어
`Provides a management plan (−8)` 과 `Includes details not linked to the user's input (−9)` 를
동시에 밟았습니다.

---

## 6. 데이터로 기각한 것들

| 시도 | 결과 |
|---|---|
| MCP / RAG 검색 | 인용 요구 기준 37개 중 0개 · 검증 문항 96% 영어 · 도구는 한국 특화 |
| `system` 프롬프트 | 이모지 11/40 → 11/40. 행동 변화 없음 |
| 외부화 2단 CoT (계획→생성) | 73.3 → 65.4 |
| Best-of-N 선택 | 61.3 vs 62.9. 무효 |
| 초안 3개 합성 | 6승 7패 27동률 · 재현 시 8승 9패 |
| thinking 재시도 | 회수율 45%, 그러나 0승 2패 18동률 |
| kNN few-shot | 64.2 vs 65.4 |
| 학회·지침 인용 금지 | 정확성 44.0 → 35.4 (모호해지며 가점까지 상실) |

초기 풀 파이프라인(분류·검색·DUR·응급게이트)이 23.57점,
그걸 전부 걷어낸 raw 패스스루가 32.44점이었습니다.

---

## 7. 재현

```bash
docker build -t aim-submission .
docker run --rm -p 8000:8000 -e LUNIT_FM_API_KEY=... aim-submission

curl -s localhost:8000/v1/models
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"What causes morning headaches?"}]}'
```

| 환경변수 | 기본값 | 용도 |
|---|---|---|
| `PIPELINE` | `raw` | `harness` 로 MCP 경로 전환 |
| `REVIEW_MODE` | `suspect` | `off` 로 검증층 비활성화 |
| `FM_MAX_TOKENS` | 6144 | 출력 예산 |
| `FM_THINKING` | on | 추론 채널 |
| `REQUEST_BUDGET_S` | — | 요청 단위 데드라인 |

제출 규약: 브랜치 `lunit/hackathon-submission` · 포트 `0.0.0.0:8000` ·
루트 `Dockerfile` · 빌드 5분 이내 · `GET /v1/models`, `POST /v1/chat/completions`
