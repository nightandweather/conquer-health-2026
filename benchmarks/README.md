# HealthBench × gpt-oss-20b 비교 실험

루닛 Conquer Health에서 사용한 호출 설정을 로컬 Ollama `gpt-oss:20b`에 적용해,
같은 의료 대화에 대한 기본 호출과 비교하는 실험입니다. Python 3.12와 표준 라이브러리만 사용합니다.

최초 2026-10-10 로컬 동작 검증에서 **3문항 × 2설정의 답변 6개를 생성**했습니다.
총 264.897초, 재호출 포함 9번의 요청이었으며, HealthBench 기준별 채점은 수행하지 않았습니다.

| 설정 | stop으로 완료 | 출력 한도에 걸림 | 호출 수 | 호출 시간 합 | 재호출 |
|---|---:|---:|---:|---:|---:|
| 기본 호출 | 2/3 | 1/3 | 3 | 53.857초 | 0 |
| 루닛 호출 설정 | 3/3 | 0/3 | 6 | 211.039초 | 3 |

루닛 설정의 high 첫 호출은 세 문항 모두 출력 한도에 걸렸습니다. 한 문항은 잘린 답변,
두 문항은 빈 답변이어서 low로 다시 호출했고, 세 답변 모두 stop으로 끝났습니다.
완성 여부와 호출 비용을 확인한 결과이며, 임상 답변의 질이나 점수 향상을 입증하지 않습니다.
서버가 보고한 completion_tokens는 기본 4,835개, 루닛 설정 21,643개입니다.
사고 토큰과 최종 답변 토큰은 별도로 분리해 저장하지 않았습니다.
세부 설정·모델 digest·토큰·시간은 [검증 기록](validation.json)에 있습니다.

## 개선 설정과 대화 화면

최근 3문항 × 3설정 실행에서는 9개 답변이 모두 `stop`으로 끝났습니다.
기본·루닛·개선 설정의 호출 시간 합은 각각 46.519초, 222.363초, 6.116초였습니다.
정성 검토에서는 개선 설정도 미확인 치료 계획과 근거 설명 문제가 남았습니다.
루닛 설정의 임상시험 부재 단정은 발표된
[무작위 예비시험](https://pubmed.ncbi.nlm.nih.gov/36416492/)과 맞지 않았습니다.
이 시간 차이를 정확도 향상으로 해석하지 않습니다. 이 기록은 변경 전 개선 지침이며,
200문항 실행에는 치료 계획과 이전 모델 답변의 사실화를 막는 `grounded-v2`를 고정합니다.

`grounded_call`은 제공된 환자 사실만 사용하고, 누락된 진단·진찰·수치·약·계획은
빈칸으로 표시하도록 요구하는 별도 실험입니다. 불확실한 임상시험·약물 용량을
추측하지 말고, 요청 범위 안에서 간결하게 답하도록 합니다. temperature 0,
reasoning_effort low, 2,048토큰, 한 번의 호출을 사용합니다.
학습이나 파인튜닝을 수행하지 않으며, 기존 기본·루닛 조건은 보존합니다.

답변 화면은 Markdown 읽기 모드가 기본이며 원문으로 전환할 수 있습니다.
제목·목록·표·코드·링크를 로컬에서 렌더링하고 모델 출력의 실행 가능한 HTML과
이미지는 표시하지 않습니다. 200문항에서는 선택·검색·이전/다음으로 탐색합니다.
검토가 끝나기 전에는 점수를 표시하지 않습니다.

```sh
python3 -m benchmarks.healthbench \
  --data private_data/healthbench/legacy_consensus_val.json \
  --arms raw_default,lunit_call,grounded_call \
  --limit 3 --out results/healthbench-three-arms

python3 -m local_tools.dialogue_server --port 8766
```

[로컬 대화 화면 사용법](../local_tools/README.md)에 실행법이 있습니다.
`http://127.0.0.1:8766`에서 세 답변을 비교하고 새 답변이 생성되는 동안 볼 수 있습니다.
`--arms`를 생략한 CLI 기본값은 기존 두 설정이며, 채점 manifest에는 실제 실행한
설정만 넣습니다. 표시된 과거 기록과 새 기록은 각 열의 실행 이름으로 구분합니다.

## 기존 비교 조건

| 항목 | `raw_default` | `lunit_call` |
|---|---|---|
| 입력 | 저장된 대화 그대로 | 마지막 user 턴에 당시 답변 지시 추가 |
| temperature | 생략, 서버 기본값 | 0 |
| 출력 한도 | 2,048 토큰 | 6,144 토큰 |
| reasoning_effort | 생략, 서버 기본값 | high |
| 빈 답변·잘린 답변 | 상태를 기록 | low로 1회 재호출 후 상태 기록 |

여기서 기본 호출은 HealthBench 자유 답변의 비교 기준입니다. NOVA의 `run.py`가 수행하는
문진·진찰 에이전트와는 다른 과제입니다. NOVA의 SAY/EXAM·30자 제한·SOAP 지시를
HealthBench 입력에 적용하지 않습니다. 로컬 양자화 모델과 NOVA 서버의 모델도 구분합니다.

이식한 것은 호출 설정입니다. 루닛 제출물의 선택적 MCP·출력 review·시간 배분을 전부
옮긴 재현은 아닙니다. 당시 L2의 thinking on/off를 gpt-oss에서는 high/low로 대응시킨
새 가설이며, 효과를 측정해야 합니다. API 차이는 [Ollama 문서](https://docs.ollama.com/api/openai-compatibility)에서 확인할 수 있습니다.

같은 문항에서 두 설정을 실행하고 문항마다 호출 순서를 번갈아 배치합니다.
3문항 실행은 동작 확인용입니다. 성능 비교에는 더 큰 평가셋과 반복 평가가 필요합니다.
이번 비교는 여러 설정을 한꺼번에 변경하므로 개별 설정의 기여도를 분리해 추정하지 않습니다.

## 데이터 상태

두 자료를 구분합니다.

- **복구된 Consensus 검증 자료:** 당시 도구가 변환한 200문항, 기준 438개.
  81문항의 이전 대화는 텍스트로 저장되어 있습니다. 기존 변환은 이 텍스트를 보존합니다.
  `--restore-roles`는 명확한 역할 표식·교대 순서·원문 재조립을 검증해 336개 발화를 복원합니다.
  새 자료는 `legacy_consensus_val_roles_200`이며 이전 파일을 덮어쓰지 않습니다.
  원본 prompt ID가 없어 두 자료 모두 공식 Consensus 전체 평가와 직접 비교하지 않습니다.
- **리더보드 재현용 Main 부분집합:** CoEval의 301개 prompt ID 목록은 확인했습니다.
  Main JSONL 원본은 이 Mac에서 찾지 못했습니다. 원본을 제공하면 대화 구조와 ID를
  그대로 유지해 301문항을 구성합니다. 복구된 Consensus 자료와 교집합을 만들지 않습니다.

복구된 원본 파일 SHA-256:
`1a6c39c254391679bf1ceeb9c49bc2470fbdae19e0923d26dca533082eed67ae`.
CoEval 301개 ID 목록 SHA-256:
`e4047a7adb607cda11270d3d76b80270e75bff8081bdb64f20f78f77f963c8b9`.

실제 문항·루브릭·카나리·답변·키는 공개 저장소에 넣지 않습니다.
공개 코드는 합성 데이터로 검사하며 실제 데이터와 실행 결과는 `private_data/`와 `results/`에 보관합니다.
[HealthBench 공개 안내](https://openai.com/index/healthbench/)와
[라이선스](LICENSE-HealthBench.txt)를 참고하세요.

## 실행

Ollama에 `gpt-oss:20b`가 설치되어 있고 서버가 실행 중이어야 합니다.
NOVA 대회 키를 사용하지 않습니다. 기본 주소는 `http://127.0.0.1:11434/v1`입니다.

복구한 당시 파일이 있다면:

```sh
python3 -m benchmarks.prepare_healthbench \
  --legacy-file /path/to/hb_val.json \
  --out private_data/healthbench/legacy_consensus_val.json

python3 -m benchmarks.healthbench \
  --data private_data/healthbench/legacy_consensus_val.json \
  --limit 3 --dry-run --out results/healthbench-dry-run

python3 -m benchmarks.healthbench \
  --data private_data/healthbench/legacy_consensus_val.json \
  --limit 3 --out results/healthbench-smoke
```

NOVA 로컬 저장소에서는 `python3` 대신 `.venv/bin/python`을 사용할 수 있습니다.
기존 실행 폴더를 덮어쓰지 않도록 매 실행마다 새 `--out` 경로를 지정합니다.
`--limit 0`은 제공한 자료 전체를 선택합니다. 기본 선택은 prompt ID 정렬순 첫 3문항입니다.
공식 전체 평가의 무작위 표집과 구분되는 동작 확인용 선택입니다.

200문항의 동일 이력으로 기본·개선 400개 답변을 비교하려면:

```sh
python3 -m benchmarks.prepare_healthbench \
  --legacy-file /path/to/hb_val.json --restore-roles \
  --out private_data/healthbench/legacy_consensus_val_roles.json

python3 -m benchmarks.healthbench \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --arms raw_default,grounded_call --limit 0 \
  --out results/healthbench-full-200
```

2026-10-11 04:59:38 UTC에 실제 200문항·400개 답변 생성을 시작했습니다.
진행 상태는 [검증 기록](validation.json)의 확인 시점 기준이며, 최종 품질 점수는 아직 없습니다.
개발 20문항과 평가 180문항을 구분하는 [검토 계획](EVALUATION_PLAN.md)을 따릅니다.
역할 복원 자료 SHA-256:
`cd3516c601da6f67fc7b6db3439efdf2659a91534c09c6bc1c5d363889ed4033`.

**gpt-oss는 답변을 생성하고 Codex가 검증·개선·재검증을 담당합니다.** 개발 20문항의 실패를
보고 일반 지침을 바꾼 뒤 같은 생성 조건으로 다시 비교하는 절차와 후보는
[Codex 검증 과정](CODEX_VERIFIER.md)에 있습니다. 자기 검토는 보조 실험입니다.
공식 문서에서 확인한 gpt-oss·Ollama 설정은 [후속 개선 실험](REFINEMENT_PLAN.md)에 있습니다.
현재 전수 실행의 지침이나 설정은 바꾸지 않습니다.

Main 원본과 CoEval 목록이 준비되면:

```sh
python3 -m benchmarks.prepare_healthbench \
  --main-file private_data/healthbench/main.jsonl \
  --ids-file private_data/healthbench/conquer_val_ids.json \
  --out private_data/healthbench/conquer_val.json

python3 -m benchmarks.healthbench \
  --data private_data/healthbench/conquer_val.json \
  --limit 0 --out results/healthbench-main-301
```

원본 다운로드 위치는 [기존 데이터 재구성 설명](https://github.com/nightandweather/conquer-health-2026/blob/2fc54f136852026b7005218b4508469a832bfa88/measurement/README.md)에 있습니다.
준비 스크립트는 네트워크를 사용하지 않고 입력 파일의 해시·문항 수·누락을 확인합니다.

생성 파일은 `manifest.json`, `answers.jsonl`, `summary.json`입니다. 문항 ID·입력 및 데이터 해시,
모델 digest, 설정, 종료 사유, 각 호출 시간·토큰, 재호출 사용 여부를 기록합니다.
루브릭과 정답은 답변 생성 요청에 전달하지 않습니다. `blocked`는 실행 차단이며 0점으로 대체하지 않습니다.

## 채점

답변 생성만으로 HealthBench 점수가 나오지는 않습니다. 별도 채점기가 각 기준에 대한
`criteria_met`을 판정해야 합니다. 이 코드는 준비된 판정 JSONL을 집계하며 채점 모델을 호출하지 않습니다.
공식 채점기와 로컬 자체 채점은 결과에 구분해서 명시해야 합니다.

Codex 검토용 A/B 패킷을 만들 때는 설정 이름·시간·토큰 수를 제외합니다.
부분 실행에서도 생성된 쌍만 추가하며, 기존 후보 답변이 바뀌면 덮어쓰기를 거부합니다.

```sh
python3 -m benchmarks.healthbench_review \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --answers results/healthbench-full-200/answers.jsonl \
  --manifest results/healthbench-full-200/manifest.json \
  --out results/healthbench-full-200/review
```

판단 보류 항목을 성공·실패로 자동 변환하지 않습니다. 점수와 검토 완료 비율,
사실 오류·정보 누락·환자 정보 임의 생성에 대한 메모를 함께 기록합니다.

판정 파일의 한 행 형식은 다음과 같습니다. 이 예시는 합성 ID입니다.

```json
{"arm":"raw_default","prompt_id":"synthetic-001","rubric_index":0,"criteria_met":true}
```

```sh
python3 -m benchmarks.healthbench_score \
  --data private_data/healthbench/legacy_consensus_val.json \
  --answers results/healthbench-smoke/answers.jsonl \
  --manifest results/healthbench-smoke/manifest.json \
  --judgments private_data/healthbench/judgments.jsonl \
  --out results/healthbench-smoke/score.json
```

문항 점수는 충족한 기준의 점수 합을 전체 양수 배점 합으로 나눕니다. 음수 기준도
충족하면 감점하고, **문항 평균을 구한 뒤** 0–1로 제한합니다. 판정 누락으로 분모를 줄이지 않으며,
누락이 있으면 점수는 `null`입니다. 문항별 승·패·동률과 원점수 차이를 함께 기록합니다.
점수식 출처는 [공식 HealthBench 구현](https://github.com/openai/simple-evals/blob/main/healthbench_eval.py)입니다.

## 확인 및 출처

```sh
python3 -m unittest discover -s tests -p '*healthbench*.py' -v
```

[검증 기록](validation.json)에 합성 검사와 실제 실행 상태를 기록합니다.
공개 검증 기록에는 문항·답변 원문이 없습니다.

호출 방식의 기준 코드는
[루닛 제출 app.py](https://github.com/nightandweather/conquer-health-2026/blob/2fc54f136852026b7005218b4508469a832bfa88/submission/app.py),
실험 흐름은 [ARCHITECTURE.md](https://github.com/nightandweather/conquer-health-2026/blob/2fc54f136852026b7005218b4508469a832bfa88/ARCHITECTURE.md)에 있습니다.
이 저장소에 기록된 당시 L2 점수는 새로운 gpt-oss 실험 결과가 아닙니다.

[같은 medium의 출력 한도 개발 비교](OUTPUT_BUDGET_DEV20.md)는 별도 경로로 20개를 한 번씩 생성했습니다. 2,048→4,096토큰에서 정상 17→18개·빈 답변 실패 3→2개이며 기존 정상 답변 17개는 원문이 같습니다. 요청 시간과 토큰이 늘었으며 독립 검토 40기록의 판정은 70개 확정·6개 보류입니다. 두 설정의 가능한 기준 범위는 같고 새 중대한 의학적 오류가 있어 채택하지 않았습니다. 공식 점수는 없습니다.

[사용자 원문 출처 색인 실험](SOURCE_LEDGER_DEV20.md)은 호출 없는 준비 뒤 별도 고정 실행기로 20문항을 한 번씩 생성했고 모두 비어 있지 않은 완료 답변을 받았습니다. 원래 대조군의 정상 18·빈 실패 1·잘림 1과 partial 상태를 보존합니다. 새 독립 검토 40기록은 기준 판정 78개·보류 6개, 실패의 2기준 생략이며 기준 점수 34기록·별도 실패 0점 1기록·점수 보류 5기록으로 구분합니다. 가능한 범위는 대조군 49.17–57.50·후보 50.83–55.00으로 겹치고 새 중대한 약물 정체성 오류와 전체 품질 향상 미확인으로 미채택했습니다. 이전 판정은 유지하고 같은 후보를 다시 호출하지 않습니다. 요청 시간 3.93%·총 토큰 4.38% 증가는 단일 실행 관측입니다. 새 모델 정보·템플릿 조회는 각각 43회이며 생성과 별도입니다. private 전체 372개·공개 지정 11개 모듈 249개·새 실행기 합성 25개 검사와 독립 코드 감사가 통과했습니다. 사용자 발화의 출처 표시를 환자 사실의 진실성이나 의학적 정확성으로 인증하지 않습니다.
