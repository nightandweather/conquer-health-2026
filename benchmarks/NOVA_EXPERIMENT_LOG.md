# NOVA 실험 기록 찾기

현재까지의 원래 실행·실패·검토 기록을 보존하고 새 관측을 별도 파일로 추가한다. 점수가 없으면 `null`이며 0점으로 대신하지 않는다. 준비만 완료한 기록과 실제 생성·서버 평가 결과를 구분한다.

## 서버 샘플

숫자와 처리 상태의 현재 집계는 [NOVA_SAMPLE_EXPERIMENTS.json](NOVA_SAMPLE_EXPERIMENTS.json), 시간순 해설은 [NOVA_DAY0_SUBMISSION.md](NOVA_DAY0_SUBMISSION.md)에 있다.

| ID | 실험 | 마지막 확인 상태 | private 결과 폴더 |
|---|---|---|---|
| E00 | 공식 SDK1.0.1 기본선 | 완료 42.179 | `results/codex-day0-submission-v1/` |
| E01 | 초안 뒤 같은 Doctor의 검토1회 | 서버 장애 복구 대기, 점수 없음 | `results/codex-review-once-sdk101-v1/` |
| E02 | 검토 + 오프라인 지침 검색 | 디스크 부족으로 준비 실패, 점수 없음 | `results/codex-guideline-agent-v1/` |
| E03 | 지침 색인을 RAM 전용으로 수정 | 완료 42.483; 전체 품질 개선 미확인 | `results/codex-memory-guideline-agent-v1/` |
| E04 | E03의 최종 기록 지시 묶음만 변경 | 완료41.079; 3건 정상, E03 대비-1.404점·미채택 | `results/codex-record-contract-agent-v1/` |
| E05 | E03의 종료 설명 힌트 2→6 | 20:59KST 한 번 접수·평가 대기·점수 없음 | `results/codex-closing-six-agent-v1/` |

각 회차에서 계획, 코드·ZIP 해시, 검사 결과, 독립 감사, 제출 링크·서버 해시, 관측 시각, 점수·6영역·증례별 턴·시간·오류, 비교와 채택 여부, 게시 검증을 남긴다. 해당 자료가 아직 없으면 미확인/미완료로 유지한다. 첫 접수와 후속 관측의 원래 파일을 덮어쓰지 않는다. `phase.json`과 공개 집계는 최신 상태를 가리키는 변경 가능한 색인이다.

E03는 E00보다 +0.304점이지만 검토와 검색이 함께 달랐고 여러 영역이 하락했다. E04의 대조군은 E03이며 총점은1.404점 하락했다. E04도 검증된 개선으로 미채택이다. 같은 공개3샘플 재사용, 서버 모델 digest와 호출·토큰 미공개, 실제 대화·기록의 독립 검토 부재를 유지한다. 공식 점수나 임상 안전 인증으로 해석하지 않는다.

## 로컬 비교·개발 기록

| 작업 | 공개 설명 | 원래 private 결과 위치 |
|---|---|---|
| HealthBench 원래200문항 및 검토 | [비교 안내](README.md), [검증자 원칙](CODEX_VERIFIER.md) | `results/healthbench-live-20261011-045938-5bd962/`, `results/healthbench-final-review-v1/` |
| dev20 지침 v3/v4 | [개선 계획](REFINEMENT_PLAN.md) | `results/codex-guided-dev20-prepared-v3/`, `results/codex-guided-v3-queue/`, `results/codex-loop-dev20-v1/` |
| 추론 강도 | [REASONING_EFFORT_DEV20.md](REASONING_EFFORT_DEV20.md) | `results/codex-reasoning-dev20-v1/` |
| 출력 예산 | [OUTPUT_BUDGET_DEV20.md](OUTPUT_BUDGET_DEV20.md) | `results/codex-budget-dev20-v1/` |
| 지침 전달 위치 | [INSTRUCTION_PLACEMENT_DEV20.md](INSTRUCTION_PLACEMENT_DEV20.md) | `results/codex-placement-preflight-dev20-v1/` |
| 사용자 원문 출처 색인 | [SOURCE_LEDGER_DEV20.md](SOURCE_LEDGER_DEV20.md) | `results/codex-source-ledger-dev20-v1/` |
| 제공 지침 검색·준비 | [NOVA_OFFLINE_KNOWLEDGE.md](NOVA_OFFLINE_KNOWLEDGE.md), [프로토타입](NOVA_GUIDELINE_PROTOTYPE.md) | `results/codex-guidelines-build-v1/`, `results/codex-provided-integration-v1/` |
| 제공 벤치 구조·노출 감사 | [NOVA_BENCHMARK_QUALITY.md](NOVA_BENCHMARK_QUALITY.md) | `results/codex-provided-benchmark-quality-v1/` |
| 환자 대화 네 축 분포 | 기존 집계와 phase 기록 | `results/codex-dialogue-distribution-v1/` |
| 공식 웹 조사 | [NOVA_WEB_RESEARCH_20261012.md](NOVA_WEB_RESEARCH_20261012.md) | `results/codex-web-research-20261012-v1/` |
| 외래·응급/방문 목적별 흐름 | [미실행 설계](NOVA_ENCOUNTER_FLOW_PLAN.md) | `results/codex-experiment-record-index-v1/` |

HealthBench 원래200문항은 partial이고 400기록 검토에도 보류 기준이 남아 있다. 이전 dev20 후보는 모두 검증된 개선으로 미채택이며 동일 후보를 재호출하지 않는다. dev20과 holdout180 경계를 유지한다. 준비·구조·원문 조회의 정합성은 임상 생성 성능과 별개다.

private 전체 `results/`의 파일 경로·크기·수정시각 색인은 `results/codex-experiment-record-index-v1/record-inventory-v1.json`에 저장했다. 이 색인은 기존 파일의 존재 목록이며 모든 파일 내용의 검증이나 별도 백업 완료 증명은 아니다. 원래 원문·DB는 복제하거나 공개하지 않았다. 코드·합성 검사·일반 문서·집계만 명시적으로 선택해 Git에 보존한다.

E04 완료 후 실제 E03/E04 서버 원문 제공 경로를 확인했지만 조사한 화면·가이드·결과 폴더에서는 찾지 못했다. 없는 대화를 추정하지 않고 [E05 종료 힌트 실험](NOVA_CLOSING_SIX_PROTOTYPE.md)을 별도 한 변수 후보로 검사·독립 감사 후20:59KST에 한 번 접수했다. 원래 10월10일 로컬 대화 검토는 이 서버 실행의 증거로 사용하지 않는다.
