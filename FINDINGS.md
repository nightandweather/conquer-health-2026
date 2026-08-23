# Conquer Health — 지금까지 무엇이 통했고 무엇이 안 통했나

**작성 시각**: 8/21 저녁 · 제출 마감 8/22 10:30
**현재 최고점**: 50 대 (1위 51.1)
**평가 대상 브랜치**: `lunit/hackathon-submission` — 이 브랜치 HEAD 의 40자리 SHA 를 대시보드에 넣는다

---

## 0. 한 문단 요약

**하네스를 만들수록 점수가 떨어졌고, 걷어낼수록 올랐다.** 지금 제출물은 사실상
`L2 를 한 번 부르는 코드 20줄`이다. 점수를 올린 건 전부 **호출 방식**이었지
파이프라인이 아니었다. RAG · 분류 · 라우팅 · 재순위화는 **전부 마이너스**였다.

---

## 1. 점수 이력 — 무엇이 얼마를 벌었나

| Trial | 변경 | 점수 | 델타 |
|---|---|---:|---:|
| 7 | `src/medai` 풀 파이프라인 (분류·검색·DUR·응급게이트) | 23.57 | — |
| 8 | flat 라우팅 하네스 | 29.13 | +5.6 |
| 16 | **하네스 전부 제거. L2 raw 패스스루** | 32.44 | +3.3 |
| 19 | **`temperature: 0`** | 39.26 | **+6.8** |
| 22 | **user 턴 끝에 지시 한 줄** | 43.07 | **+3.8** |
| 25 | **thinking ON + 손상 시 fallback** | 49.72 | **+6.7** |

**핵심 코드는 이게 전부다** (`app.py` 의 `generate_reply`):

```python
forwarded = [dict(m) for m in messages]
if forwarded and forwarded[-1].get("role") == "user":
    forwarded[-1]["content"] += f"\n\n[{ANSWER_INSTRUCTION}]"

data = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": True}})
choice = data["choices"][0]
content = (choice["message"].get("content") or "").strip()
if choice.get("finish_reason") == "length" or not content:      # thinking 이 2048 을 다 먹은 경우
    data = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": False}})
    content = (data["choices"][0]["message"].get("content") or "").strip()
return content
```

```python
ANSWER_INSTRUCTION = "Do not substitute 'consult a professional' for an answer; answer as far as you can."
# temperature=0 은 call_fm 의 payload 에 있음
```

---

## 2. 측정으로 확인된 것 — 이 모델의 성질

### ① L2 는 `system` 메시지를 사실상 무시한다. `user` 턴은 따른다

같은 지시(`"Do not use emoji"`)를 위치만 바꿔 40문항씩:

```
지시 없음      이모지 10/40
system 메시지  이모지 11/40   ← 효과 없음
user 턴 앞     이모지  2/40
user 턴 뒤     이모지  0/40   ← 완전히 따름
```

**Trial 7·8·13 이 실패한 진짜 이유가 이것이다.** 프롬프트가 나빴던 게 아니라
전부 `system` 에 넣어서 아무 일도 일어나지 않았다. Trial 22 가 같은 지시를
user 턴으로 옮겨 **+3.8** 을 냈다.

> 지시를 추가할 일이 있으면 **반드시 마지막 user 메시지 뒤에 `[...]` 로** 붙일 것.

### ② temperature 를 안 보내면 서버 기본값(≈1.0)으로 채점받는다

```
temperature 미지정  샘플 유사도 0.04   → 32.44
temperature 0.9     0.05
temperature 0.2     0.12
temperature 0.0     0.20              → 39.26
```

rubric 438개 중 **143개(33%)가 "사실 오류가 없을 것"** 이라 고온이 직격이다.
`"temperature": 0` 을 payload 에 명시할 것. (0.3 은 38 로 약간 낮았다.)

### ③ thinking 은 이득이 있지만 2048 예산을 최종 답변과 나눠 쓴다

```
thinking OFF   잘림  1/40   빈 응답 0/40   지연 중앙  6.4s
thinking ON    잘림 10/40   빈 응답 2/40   지연 중앙 19.6s   reasoning 평균 2,728자
```

**끄면 안 된다. 손상된 것만 건져내면 된다.** 그게 Trial 25 이고 +6.7 이었다.

### ④ `temperature=0` 인데 결정론이 아니다 ← 매우 중요

동일 설정으로 40문항을 3번 돌린 결과:

```
run1  진료권유 10   길이중앙 1946
run2  진료권유 12   길이중앙 2042
run3  진료권유  8   길이중앙 1891

run 간 답변 유사도 0.34   (1.00 이어야 결정론)
```

`seed` 파라미터도 **무시된다**(같은 seed 3회 유사도 0.79 / 0.23).

> **로컬 n=40 채점의 노이즈는 ±9점이다.** 그보다 작은 차이는 판정 불가.
> 대시보드 점수에도 run-to-run 변동이 있으니 **+3 미만 차이로 채택/기각하지 말 것.**

### ⑤ 직렬 분해는 지고, 병렬 다양성은 이긴다

```
외부화 2단 CoT (계획 호출 → 생성 호출)   로컬 65.42  vs  기준선 73.33   ← 짐
초안 3개 → 선택(Best-of-3)               로컬 61.25  vs  62.92          ← 무효
초안 3개 → 합성(union)                   로컬 66.67  vs  54.17          ← +12.5
```

HealthBench rubric 은 문항당 **개별 내용 항목 2.19개**를 채점한다. 그래서
"셋 중 고르기"는 안 되고 **"셋의 합집합"** 이 통한다.

---

## 3. 측정으로 기각된 것 — 다시 하지 말 것

| 시도 | 결과 | 근거 |
|---|---|---|
| **MCP / RAG 검색** | ❌ | 인용을 요구하는 consensus 기준 **37개 중 0개**. hb_val 질문은 **96% 영어 일반 의학**, MCP 도구는 전부 한국 특화(급여·심평원·법령·KCD·식약처). 검색 있는 Trial 은 전부 낮았다 |
| `system` 프롬프트 | ❌ | ①번. 행동이 안 바뀐다 |
| 지시 여러 줄 추가 (hedge / emergency / tailor) | ❌ | 로컬에서 전부 노이즈 대역 안 |
| 이모지 금지 지시 | ❌ | temperature 0 이 이미 해결 (11/40 → 1/40) |
| 되묻기 지시 | ❌ | 1/2 → 1/2. 게다가 최대 이득이 전체의 3.7% 뿐 |
| assistant prefill | ❌ | 작동은 하나 고칠 문제가 없음(서론형 오프너 0/60) |
| kNN few-shot (자작 예시) | ❌ | 로컬 64.17 vs 65.42 |
| 출력 후처리 (이모지 제거·진료권유 삭제 등) | ❌ | 현 챔피언의 기계적 결함이 이미 거의 0 — 절단 3/60, 언어 불일치 0/60, 반복 붕괴 0/60, 이모지 1/60. 진료권유 11/60 은 전부 1,200자 넘는 충실한 답변 안이라 rubric 위반이 아님 |

> **RAG 는 프론티어 상(임상의 블라인드, 한국어 3턴)에서는 정반대로 유리하다.**
> 벤치마크 상위 10 컷을 통과한 뒤에 켜는 것이 순서.

---

## 4. 로컬에서 실험하는 법 (Docker 불필요)

현 제출물이 `L2 한 번 호출`이라 **컨테이너 없이 엔드포인트를 직접 때리면 동일**하다.

```bash
cd /Users/iganghun/Downloads/lunit-hackathon
echo "<LUNIT_FM_API_KEY>" > .lunitkey && chmod 600 .lunitkey
```

| 스크립트 | 용도 |
|---|---|
| `lang_audit.py <포트> [n]` | 도커로 띄운 이미지의 언어 일치율·길이·지연·fallback |
| `ab.py`, `sfx.py`, `where.py` | 지시 위치·내용 A/B (행동 지표) |
| `control.py` | **노이즈 바닥 측정.** 동일 설정 3회 |
| `strict_score.py <이름...>` | 로컬 rubric 채점 (`answers_<이름>.json` 필요) |
| `allin.py`, `synth.py`, `cot2.py` | 재시도 / 합성 / 2단 CoT 변형 |

```bash
python3 allin.py            # 변형별 답변 생성 → answers_*.json
python3 strict_score.py t25b retry fewshot
```

### ⚠ 로컬 채점기의 한계 — 반드시 알고 쓸 것

`strict_score.py` 는 official rubric 을 L2 에게 읽히고 **근거 문장 인용을 강제**해
도장 찍기를 막은 것이다. t7 < t8 < t16 순서는 재현했지만 **길이 편향이 있다** —
긴 답변일수록 인용거리가 많아 높게 나온다. 실제로 t16(1934자) 이 temp0(1437자)보다
로컬에서 높지만 **대시보드에서는 반대**였다.

> **길이가 비슷한 변형끼리만 비교할 것. 최종 판단은 대시보드 Trial.**

---

## 5. 지금 살아 있는 후보

| 순위 | 변경 | 근거 | 비용 |
|---|---|---|---|
| **1** | **thinking 재시도** — 손상 시 곧바로 non-thinking 으로 가지 말고 thinking 으로 한 번 더 | 비결정성 덕에 **손상 10건 중 4건 회수**. 로컬 74.17 vs 65.42 | 코드 3줄 |
| 2 | **초안 2~3개 → 합성** | 로컬 +12.5 (노이즈 넘은 유일한 신호) | 호출 3~4배. Trial 25 가 22분이었으므로 **시간 위험** |
| 3 | 같은 SHA 재제출 | 노이즈 폭 실측. 격차가 1.4 라 이것만으로 순위가 바뀔 수 있음 | Trial 1회 |

**1번 구현**:

```python
a, f = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": True}})
if damaged(a, f):
    a, f = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": True}})   # 추가
if damaged(a, f):
    a, f = await call_fm(forwarded, MAX_TOKENS, {"chat_template_kwargs": {"enable_thinking": False}})
```

---

## 6. 제출 시 지킬 것

- 브랜치 **`lunit/hackathon-submission`**, 포트 **8000**, Dockerfile 루트, 빌드 5분 이내
- 제출은 **그 브랜치 HEAD 의 40자리 전체 SHA**. `sha_not_branch_head` 로 두 번 튕겼다
- **대시보드의 마지막 제출이 최종 제출이다.** 최고점 SHA 를 팀에 공유해 두고,
  마감 직전에 낮은 커밋으로 덮이지 않게 할 것
- API key 는 운영진 지침에 따라 이미지 안에서 참조 가능해야 한다(자동 주입 없음).
  대회 종료 후 폐기·히스토리 정리 필요
- HealthBench 문항 원문을 문서·슬랙·저장소에 붙여넣지 말 것(공식 공개 금지)
- 평가 데이터 분포를 근거로 튜닝하지 말 것 — **일반화 가능한 상담 행동**만.
  커밋 메시지에도 분포 통계를 근거로 쓰지 않는다
