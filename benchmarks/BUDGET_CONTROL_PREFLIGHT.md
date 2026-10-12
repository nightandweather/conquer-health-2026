# medium 출력 한도 비교 준비

2026-10-12의 후속 작업입니다. 직전 추론 비교의 보류 4개를 설정·시간·토큰을 가린 새 검증자가 다시 확인했으며 **해소 0개, 보류 유지 4개**입니다. 판정 방향·적용 조건·근거 정의에 대한 추가 확인이 필요합니다. 원래 답변과 기준을 고쳐 보류를 없애지 않았으며 점수와 미채택 판정도 유지합니다. 동일 자료의 재검토는 새 근거가 있을 때만 다시 진행합니다.

다음 가설은 같은 medium 추론에서 출력 한도만 2,048에서 4,096토큰으로 늘리면 빈 최종 답변이 줄어드는지 확인하는 것입니다. 기존 medium 20개 기록(정상 17개·실패 3개)을 그대로 대조군으로 보존하고 첫 20개 개발 입력만 사용합니다. low/2,048과 medium/4,096을 한 변수 비교로 해석하지 않습니다.

| 조건 | 보존할 대조군 | 계획한 후보 |
|---|---|---|
| 추론 | medium | medium |
| 출력 한도 | 2,048 | 4,096 |
| 지침·이력·temperature·모델 digest | 기존 원문과 동일 | 동일해야 함 |
| 새 생성 호출 | 0 (기존 20개) | 이번 후속 작업에서는 0 |
| 실패 기록 | 빈 최종 답변 3건 포함 | 아직 결과 없음 |

`healthbench_budget_preflight`를 구현하고 개발용 20개의 실제 저장 기록에서 준비 검증을 완료했습니다. 답변 생성과 모델 조회 호출은 모두 0회입니다. 이 도구는 네트워크·모델 조회·답변 생성 없이 비교 준비를 검증합니다. 외부에서 고정한 원본 비교 파일과 전체 source summary의 SHA에 manifest·답변·집계·설정·행·실패 증거를 묶고, 20개 입력과 요청을 재구축하여 `max_tokens`만 달라지는지 검사합니다. 확인 전에는 준비 결과를 쓰지 않고, 새 비공개 출력 폴더만 허용합니다. 실제 질문·답변·기준은 준비 출력에 포함하지 않습니다.

신규 합성 테스트 41개, private 전체 회귀 229개, 공개 패키지의 신규 준비·기존 runner/queue 대상 87개가 통과했습니다. 공개 저장소 전체 테스트를 실행한 것은 아닙니다. 독립 감사에서 재현한 출력 폴더·파일 교체 문제를 보완하고 다른 작성자의 파일이 보존되는지 확인했습니다.

동일 계정의 다른 프로세스가 `mkdir`과 최초 inode 확인 사이에 비어 있는 폴더를 교체하는 극단적 경합은 일반 POSIX API로 완전히 배제하지 못합니다. 모든 동시 변경을 차단한다고 주장하지 않습니다. 고유한 전용 출력 폴더와 한 작성자만 사용하는 범위에서 실행하며, 더 강한 보장이 필요하면 프로세스 분리나 플랫폼별 원자적 게시가 별도로 필요합니다. 실제 답변 생성에는 이 도구가 아직 연결되지 않았습니다.

아래 예도 준비 전용이며 네트워크·생성은 없습니다. 두 외부 해시는 승인한 원본 준비 계획에서 고정한 값입니다. 변경된 source 파일의 해시를 새로 넣어 검사를 우회하지 않습니다. 출력 폴더는 새 회차마다 고유하게 지정합니다.

```sh
python3 -m benchmarks.healthbench_budget_preflight \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --source-manifest results/codex-reasoning-dev20-v1/execution-20261012-0246/candidate/manifest.json \
  --source-comparison-manifest results/codex-reasoning-dev20-v1/execution-20261012-0246/comparison/manifest.json \
  --expected-comparison-sha256 0f6890df90f5802fc7cfa4ba6900c61ca7bca2408042e5cc855ec2a852663960 \
  --expected-source-summary-sha256 80053f86a211704e1263ea064b42670f4050d2d19d769b6afb94e4ba73ac777c \
  --instructions-file benchmarks/prompts/grounded_v2_frozen.txt \
  --out results/UNIQUE_BUDGET_PREFLIGHT
```

기존 원본 데이터 파일의 해시와 구조는 유지합니다. 전체 파일의 구조·해시 확인과 평가용 180문항의 개별 임상 내용·검토를 설정 수정에 사용하는 것은 구분하며, 이번 후보 요청 재구축은 개발용 첫 20개에 한정합니다. 대조군 답변 검증에는 기존 medium 후보의 20개 행만 사용합니다.

현재 실제 생성기와 큐는 출력 2,048토큰·기존 low 대조군 정책을 유지합니다. 이 정책을 완화하지 않습니다. 준비 도구를 통과해도 4,096토큰 실행 지원이나 품질 개선이 확인된 것은 아닙니다. 실제 생성 지원·모델 digest 확인·동일 요청 전송·출처 재검사와 독립 검토 경로를 검증한 뒤에만 별도 회차를 시작할 수 있습니다.

Ollama 호환 API는 `max_tokens`와 `reasoning_effort`를 지원하며, gpt-oss는 low·medium·high 추론 수준을 지원합니다. 이 문서 지원이 로컬의 4,096토큰 결과나 빈 답변 감소를 보장하지는 않습니다. [Ollama 공식 문서](https://docs.ollama.com/api/openai-compatibility), [OpenAI 모델 설명](https://developers.openai.com/api/docs/models/gpt-oss-20b).

[직전 low·medium 실험](REASONING_EFFORT_DEV20.md), [진행 집계](validation.json), [검증 절차](CODEX_VERIFIER.md)에 연결됩니다. 실제 여러 턴 NOVA 동작이나 제출 에이전트에는 적용하지 않았습니다. 공식 점수와 임상 안전 인증은 없습니다.
