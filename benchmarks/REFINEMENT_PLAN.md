# gpt-oss 답변·대화 개선 실험

**Codex가 gpt-oss의 답변과 대화를 독립적으로 검증하고, 개발 실패를 보고 일반 지침을
수정한 뒤 새 답변을 재검증합니다.** 주요 개선 절차와 첫 후보는
[Codex 검증 과정](CODEX_VERIFIER.md)에 있습니다.
답변의 정확성과 필요한 내용의 충족을 먼저 평가하고, 그 품질을 얻는 데 든 전체 시간과
토큰을 비교합니다. gpt-oss 자기 검토는 보조 실험이며 통과했다는 사실은 정답 판정이 아닙니다.
현재 200문항의 기본·개선 비교는 설정을 고정한 채 진행하며, 아래 변경은 후속 개발 실험입니다.

## 문서와 현재 실행에서 확인한 설정

2026-10-11 UTC에 생성 호출 없이 `/api/show`, `/api/ps`, `/api/version`을 확인했습니다.
[메타데이터 기록](ollama_runtime_snapshot.json)은 이 시점의 값이며 일반 기본값과 구분합니다.

| 설정 | 확인한 지원 범위 | 후속 실험에서의 사용 |
|---|---|---|
| `reasoning_effort` | gpt-oss의 `low`, `medium`, `high`; 모델 기본 `medium` | 같은 지침에서 low와 medium부터 비교 |
| `temperature` | Ollama 호환 API 지원; 현재 모델에 저장된 값은 **1** | 0을 명시한 뒤 0.2를 별도로 비교; 낮다고 정확성이 보장되지는 않음 |
| `top_p`, `seed` | 호환 API 지원 | 초기 실험에서는 top_p를 동시에 바꾸지 않음; seed가 모든 환경에서 동일 출력까지 보장하지는 않음 |
| `max_tokens` | 호환 API의 출력 예산 | 잘림·빈 답변·요청 누락을 함께 기록; 검토·수정까지 합산 |
| `num_ctx` | native API options 또는 Modelfile; 호환 API 요청 목록에는 없음 | 모델 최대 131,072와 현재 할당 32,768을 구분; 이력 길이 측정 후 변경 |
| `top_k`, `min_p`, `repeat_penalty` | Modelfile 등 런타임 설정 | 초기 실험에서는 유지; NOVA 원격 endpoint 지원 여부를 별도 확인 |
| `response_format` | Ollama 호환 API JSON mode 지원 | 짧은 검토 결과를 JSON으로 받고 실제 스키마도 코드에서 검증 |
| `stream` | 호환 API 지원 | 읽기 화면에는 최종 답변만 표시; 완료까지의 시간은 별도 측정 |

지원 필드: [Ollama OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility).
추론 수준: [OpenAI Harmony](https://developers.openai.com/cookbook/articles/openai-harmony),
[Ollama Thinking](https://docs.ollama.com/capabilities/thinking).
런타임 설정: [Modelfile](https://docs.ollama.com/modelfile),
[Context length](https://docs.ollama.com/context-length).

현재 로컬 템플릿은 API의 `system` 내용을 Harmony의 developer 지침으로 렌더합니다.
별도 API `developer` 역할의 지원은 확인하지 않았으므로 로컬 실험은 `system` 지침을 사용합니다.
Harmony 특수 토큰은 프롬프트에 직접 붙이지 않습니다. Ollama가 템플릿을 처리합니다.
이 지원 범위가 NOVA 원격 서버에서도 동일하다는 의미는 아닙니다.

현재 받은 공식 `nova_sdk.py`의 `Client.chat()`은 `messages`, `max_tokens`, `request_id`만
받습니다. 로컬 개발 키트도 Doctor 호출에 메시지와 토큰 한도만 전달합니다.
따라서 이 SDK 경로에서 temperature·reasoning_effort를 바로 지정할 수 있다고 가정하지 않습니다.
서버 지원과 허용 경로를 확인하기 전에는 메시지 지침·토큰 예산·호출 흐름의 개선부터 적용합니다.
[NOVA 개발 가이드](https://nova.snubhai.org/docs/)와 저장소에 보관한 공식 SDK를 함께 확인했습니다.

## 보조 실험: 답변 하나 안에서의 자기 검토

1. 원래 역할과 발화 순서를 보존한 입력으로 초안을 작성합니다.
2. 같은 원본 입력과 초안을 읽고 구체적인 오류·누락만 짧게 검토합니다.
3. 수정할 결함이 있을 때 한 번 수정합니다. 결함이 없으면 초안을 유지합니다.

최대 3호출·수정 1회로 시작합니다. 무한 재귀나 임의의 합격 점수를 사용하지 않습니다.
초안 1,536 + 검토 512 + 수정 2,048 = 출력 한도 합 4,096토큰이며,
실제 입력·출력 토큰과 세 호출의 시간을 모두 비용에 포함합니다.
이 한도는 최적값이 아니라 검증할 시작값입니다. 추론이 출력 예산을 소모하여 검토 JSON이
잘리거나 최종 답변이 비는 경우도 성공으로 숨기지 않습니다.

검토의 범주는 요청 누락, 근거 없는 환자 정보, 이력 모순, 위험한 표현, 불확실성 처리,
가독성입니다. 새 연구·용량·효과를 추측해 보충하지 않도록 지시합니다.
검토 내용은 모델이 만든 참고 자료이며 환자의 새 발화나 검증된 사실이 아닙니다.
검토가 잘못되거나 수정이 잘리면 완성된 초안을 보존하고 해당 상태를 기록합니다.
초안과 수정본의 품질은 별도 Codex 평가로 비교합니다.

같은 지침·설정의 단일 호출을 대조군으로 남깁니다. 기존 grounded-v2와 새로운 반복 흐름의
차이는 지침 위치·추론·예산도 포함할 수 있으므로 반복 검토만의 효과라고 해석하지 않습니다.
검토 JSON은 숨겨진 analysis/CoT를 노출하도록 요청하는 방식이 아닙니다.

지식의 근거가 부족한 문제에는 자기 검토만 반복하지 않습니다. NOVA는 서버의
`NOVA_GUIDELINES_DIR`에 진료지침 자료를 제공하므로, 관련 원문을 찾아 출처와 함께
입력하는 별도 실험이 가능합니다. 검색 내용은 환자 사실과 구분하고 문서의 연도·조건을
확인합니다. 이 실험에서는 아직 검색을 연결하지 않았습니다.
[NOVA 진료지침 자료실 안내](https://nova.snubhai.org/docs/#%EC%A7%84%EB%A3%8C%EC%A7%80%EC%B9%A8-%EC%9E%90%EB%A3%8C%EC%8B%A4).

## 프롬프트를 고치는 바깥 반복

| 단계 | 변경 | 판정 |
|---|---|---|
| 0 | 현재 200문항 비교 완료 및 기준별 검토 | 개발 20 / 평가 180을 따로 집계 |
| 1 | Codex가 개발 실패를 보고 일반 지침 한 가지 수정 | 버전·변경 이유·개발 범위 기록 |
| 2 | 같은 생성 조건으로 개발 20을 다시 생성하고 Codex 재검토 | 해결한 오류와 새로 만든 오류, 전체 시간 |
| 3 | 필요하면 low·medium 또는 자기 검토를 보조 비교 | Codex가 정확도·누락·잘림 판정 |
| 4 | 후보 확정 후 별도 평가 | 품질 차이를 먼저 보고 시간·토큰 비교 |

개발용 20문항의 채점 기준은 Codex 평가에만 사용하며 생성 모델에는 전달하지 않습니다.
평가용 180문항을 보고 지침을 바꾸면 이 자료는 이후의 독립 평가셋으로 재사용하지 못합니다.
현재 비교의 평가 답변은 채점할 수 있지만 프롬프트 수정 근거로 사용하지 않습니다.
대화 이력 압축 방식이나 temperature도 한 번에 하나씩 바꿔야 기여도를 구분할 수 있습니다.

문서상 평가 권고는 기준별 pass/fail, A/B 비교, 순서·길이 편향 통제, 사람 평가와의 대조입니다.
여기서는 Codex가 기준별 판단을 하며 공식 HealthBench 점수나 임상 검증으로 표시하지 않습니다.
[OpenAI Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices),
[Model optimization](https://developers.openai.com/api/docs/guides/model-optimization).

## 여러 턴에서 보존할 정보

실제 `user → assistant(final) → user` 순서를 유지하고, 원문을 출처로 보관합니다.
지난 assistant의 진단 추정·치료 제안은 사용자 확인 없이 환자 사실로 승격하지 않습니다.
사용자가 말한 내용도 객관적으로 검증된 검사 결과와 구분해야 합니다.

| 이력 항목 | 함께 남길 정보 |
|---|---|
| 사용자가 보고한 증상·병력 | 원문, 발화 번호, `user_reported` |
| 실제 검사·도구 결과 | 원문, 결과를 얻은 행동과 턴, `observed` |
| 모델의 가설·제안 | 원문 또는 짧은 요약, `proposed`; 확진·기존 처방으로 바꾸지 않음 |
| 미확인 또는 서로 다른 정보 | 두 출처, `unknown` 또는 `conflicting`; 최신 발화만으로 임의 삭제하지 않음 |
| 이미 한 질문·검사 | 요청과 실제 응답, 결과 유무; 동일 행동의 불필요한 반복 방지 |

짧은 HealthBench 이력은 먼저 원문 그대로 사용합니다. 긴 NOVA 대화에서만 출처가 있는 요약과
최근 원문을 함께 전달하는 방식을 추가 실험합니다. 자료 복원은 대화 생성이나 새로운 환자
응답 시뮬레이션이 아닙니다. 후속 질문의 적절성·모순·반복·최종 기록의 사실 보존은
NOVA 공개 샘플의 실제 환자 응답으로 별도로 검증해야 합니다.

gpt-oss는 final 완료 뒤 다음 턴에 이전 analysis/CoT를 제거하도록 권고합니다.
같은 답변의 도구 호출이 진행 중이면 final 전의 필요한 analysis는 유지하는 예외가 있습니다.
현재 HealthBench 코드는 final 내용만 보관하며, 과거 내부 추론을 이력에 넣지 않습니다.
[OpenAI raw CoT handling](https://developers.openai.com/cookbook/articles/gpt-oss/handle-raw-cot).

## 업무 지침 초안

> 질문에 필요한 답을 먼저 제공하세요. 사용자에게서 보고된 정보와 실제 도구 결과만
> 출처와 함께 환자 정보로 사용하세요. 없는 검사·수치·증상·치료를 만들어 넣지 마세요.
> 연구·효과·용량이 불확실하면 단정하지 마세요. 이전 답변의 추측을 확인된 사실로 사용하지
> 마세요. 결론을 실질적으로 바꿀 정보만 추가로 질문하고, 현재 정보로 답할 수 있는 내용은
> 답하세요. 필요한 내용과 안전상 조건을 빠뜨리지 말고 Markdown으로 읽기 쉽게 작성하세요.

이 지침과 반복 흐름은 학습 가중치를 바꾸지 않는 추론 실험입니다.

## 보조 실험의 준비 및 수동 실행

아래 코드는 자기 검토 흐름을 보관하기 위한 것입니다. 현재 자동 대기 큐는 생성 전에
종료했으며 주요 후속 실험은 Codex 검토에 따른 일반 지침 변경입니다.

개발용 20문항의 요청 해시와 조건만 먼저 검증하려면 다음 명령을 사용합니다.
준비 모드는 실행 중인 기준선도 받을 수 있으며 모델을 호출하지 않습니다.

```sh
python3 -m benchmarks.healthbench_refine \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --baseline-manifest results/BASELINE_RUN/manifest.json \
  --out results/refinement-dev20-prepared --prepare-only
```

필요하면 전수 비교가 끝난 뒤 low·medium 초안의 두 반복 검토 실험을 차례로 실행할 수 있습니다.
이 명령은 한 번의 연속 계산이며, 기준선이 실패·중단되거나 조건이 바뀌면 중단합니다.
원본 자료 해시와 개발용 ID, 모델 식별 정보도 기준선과 일치해야 합니다.

```sh
python3 scripts/run_refinement_after_baseline.py \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --baseline-manifest results/BASELINE_RUN/manifest.json \
  --out results/refinement-dev20-sequential
```

각 실험의 `answers.jsonl`에는 초안·검토·수정의 최종 내용과 비용을 보관합니다.
`draft_controls.jsonl`은 그 안에서 실제 관찰한 초안만 추출한 대조 자료이며,
별도의 단일 호출 실험을 실행한 것으로 표시하지 않습니다.
검토·수정의 비용은 원래 반복 실험에 전부 포함합니다. 실제 품질 판정은 이후 Codex 검토가 필요합니다.
네트워크 소켓 timeout은 시간 초과 응답을 채택하지 않도록 처리하지만,
프로세스의 절대 실행 시간을 강제로 끊는 장치는 아닙니다.
