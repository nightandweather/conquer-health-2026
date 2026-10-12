# 옆 패널의 HealthBench 대화 비교

로컬 Ollama gpt-oss-20b의 기본·루닛·개선 호출을 같은 화면에서 비교합니다.
기존 `results/healthbench-*/answers.jsonl` 기록을 읽고, 실행 버튼을 누르면 최종 답변을
생성하는 동안 화면에 표시합니다. 사고 과정은 표시하거나 별도로 저장하지 않습니다.

```sh
python3 -m local_tools.dialogue_server \
  --data private_data/healthbench/legacy_consensus_val.json --port 8766
```

브라우저에서 `http://127.0.0.1:8766`을 엽니다. 기본은 정렬순 첫 3문항입니다.
`세 설정 모두 비교`는 동일한 새 실행에서 9개 후보 답변을 생성하며, 루닛 설정은
빈 답변 또는 출력 한도에 걸렸을 때 재호출할 수 있습니다.
`기본·개선 다시 비교`는 두 설정의 6개 후보 답변을 생성합니다.
각 열의 실행 이름을 통해 과거 기록과 새 실행을 구분할 수 있습니다.

개선 설정은 학습된 가중치를 바꾸지 않습니다. 미확인 환자 사실을 추측하지 않도록
지시하고 low reasoning과 2,048토큰을 사용합니다. 동일 모델에서 여러 설정을 바꾸는
실험이므로 각 설정의 기여도를 분리하거나 임상적 개선을 입증하지 않습니다.
답변 생성과 HealthBench 기준별 채점은 별도입니다.

서버는 127.0.0.1에만 연결합니다. 루브릭·카나리·API 키·원본 파일 경로는 화면 API로
제공하지 않으며, 데이터와 답변은 Git에서 제외된 로컬 폴더에 보관합니다.
외부 서비스·CDN 없이 작동합니다. 스트리밍 API는
[Ollama OpenAI 호환 API](https://docs.ollama.com/api/openai-compatibility)를 사용합니다.
이 도구는 대회 제출 ZIP과 NOVA Docker 빌드에서 제외됩니다.

## 읽기 모드와 200문항

답변은 기본적으로 Markdown 읽기 모드로 표시됩니다. 제목·목록·표·코드·링크를
읽기 쉽게 정리하며 `원문 보기`로 전환할 수 있습니다. 모델이 출력한 HTML이나
이미지를 실행하거나 자동으로 가져오지 않습니다.

역할 표식으로 복원한 자료로 200문항을 표시하고 기본·개선 400개 답변을 생성하려면:

```sh
python3 -m local_tools.dialogue_server \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --limit 200 --port 8767 --start-arms raw_default,grounded_call
```

위 예시는 기존 8766 화면과 다른 포트를 사용합니다. 같은 포트에 본인이 실행한 서버가 있으면
그 서버를 종료하거나 다른 포트를 지정하세요. `--start-arms`를 생략하면
과거 기록만 표시하고 생성 버튼을 눌렀을 때 시작합니다. 문항 선택·검색·이전/다음으로
이동합니다. 과거 루닛 답변은 동일한 입력 해시인 경우에만 해당 문항에 표시합니다.
각 답변의 실행 이름을 확인하세요.

Codex 검토 결과는 실행 폴더의 `assessments.json`에서 읽습니다. 원문 답변 해시가
일치하는 검토만 표시하고, 판정이 없는 답변에는 점수를 만들어 넣지 않습니다.
표시 필드는 `prompt_id`, `arm`, `answer_sha256`(canonical JSON hash), `status`,
`score`(0–100 또는 null), `note`입니다. 채점 기준의 데이터 필드와 키는 화면에 전달하지 않습니다. 검토 메모에는 기준의 쟁점에 관한 설명이 포함될 수 있습니다.
검토 절차는 [200문항 평가 계획](../benchmarks/EVALUATION_PLAN.md)에 있습니다.

`reviewed`는 기준별 채점이 끝났다는 뜻이며 임상적으로 안전하다는 판정이 아닙니다.
오류·누락 또는 채점 기준 문제가 있으면 점수와 함께 `추가 검토 필요` 메모를 표시합니다.
판정 보류가 있는 답변은 `uncertain`이며 숫자 점수를 표시하지 않습니다.

## Codex 개선 루프의 저장된 비교

후보 생성이 끝나 `comparison/manifest.json`과 `comparison/answers.jsonl`이 만들어지면,
중첩된 비교 폴더를 지정해 대조군과 새 후보를 읽기 모드로 표시할 수 있습니다.

```sh
python3 -m local_tools.dialogue_server \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --limit 20 --port 8768 \
  --comparison-dir results/codex-loop-dev20-v1/round-01/execution-approved-20261012/comparison
```

`--comparison-dir`에는 이 저장소의 `results/` 안에 있는 실제 비교 폴더를 지정합니다.
표시 문항 범위인 `--limit`은 비교 manifest의 문항을 모두 포함해야 합니다.
화면은 manifest의 실제 설정 이름(`grounded_call`, `codex_guided_v3` 등), 추론 강도,
temperature, 출력 한도와 답변별 시간·호출 수·토큰을 표시합니다. 다른 실행의 최신 답변을
섞지 않으며, 기존 기본·루닛 비교 화면과 별도 포트에서 열 수 있습니다.

저장된 비교에서는 생성 버튼을 숨깁니다. 다음 후보 생성은 개선 루프 실행기로 진행하며,
`--start-arms`와 `--comparison-dir`를 함께 사용할 수 없습니다. 원본 대조군 실행이
`partial`이면 화면에도 부분 완료로 표시합니다. 선택한 개발 문항의 대조군 답변이 완료된
경우에도 원본 전체 실행이 완료됐다고 표시하지 않습니다. 후보 답변의 잘림·실패도 유지합니다.

이 비교 폴더의 `assessments.json`이 생기거나 갱신되면 일치하는 답변 해시의 Codex 검토가
자동으로 표시됩니다. 검토가 없거나 판단 보류이면 숫자 점수 대신 검토 대기 상태를 표시합니다.

## 같은 지침의 low·medium 추론 비교

```sh
python3 -m local_tools.dialogue_server \
  --data private_data/healthbench/legacy_consensus_val_roles.json \
  --limit 200 --port 8771 \
  --comparison-dir results/codex-reasoning-dev20-v1/review-scope
```

현재 [추론 설정 비교 화면](http://127.0.0.1:8771/)은 개발 20문항의 대조군 low와 후보 medium을 읽기 모드로 표시합니다. 같은 지침·이력·출력 한도이며 원본 실행의 부분 완료와 후보 실패 3건을 유지합니다. 파생 검토 폴더는 실패를 포함한 독립 검토를 위해 답변 바이트를 그대로 복사한 것이며 새 호출이나 원본 상태 변경은 없습니다. [실행과 판정 기록](../benchmarks/REASONING_EFFORT_DEV20.md)을 함께 확인하세요.
