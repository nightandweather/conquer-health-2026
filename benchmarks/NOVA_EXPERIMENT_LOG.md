# NOVA 실험 기록과 제출 방법

현재까지의 원래 실행·실패·검토 기록을 보존하고 새 관측을 별도 파일로 추가한다. 점수가 없으면 `null`이며 0점으로 대신하지 않는다. 준비만 완료한 기록과 실제 생성·서버 평가 결과를 구분한다.

2026-10-13 08:35 KST 정리. 기존 결과 파일과 고정 ZIP 6개의 크기·SHA-256을 확인하고 공식 홈·규칙·개발 가이드·평가 안내를 읽었다. 이번 문서 정리에서 새 모델 호출·합성 검사·업로드·평가 요청은 모두 0회다. 아래 점수는 저장된 완료 관측이며 이번에 서버 결과를 새로 조회한 것이 아니다.

## 현재 판단과 다음 작업

샘플에서 가장 높은 표시 총점은 E03의 **42.483**이며 E00 기본선보다 **+0.304**다. 전체 임상 품질 개선을 입증하지 못해 E03·E04·E05 모두 검증된 개선으로 미채택이다. 새로 수정·검증한 NOVA 실행 후보는 없고, 뒤에 만든 응답 관측 helper·adapter·파서 검사는 실제 런타임에 연결하지 않았다.

E01의 마지막 상세 관측은 **2026-10-13 05:04 KST**이며 서비스 장애·복구 대기(`EXECUTION_INFRASTRUCTURE`), 점수 `null`이었다. 정기 재조회 필요성은 **09:00 KST 이후** 판단한다. 자동화 루프는 사용자의 요청으로 **20분 주기**로 바뀌었지만, 매 회차마다 모델 실험이나 제출을 하는 뜻은 아니다.

공식 접수·잔여의 마지막 로그인 화면 관측은 **2026-10-13 00:08–00:09 KST**였다. 공식 선택지는 접수 기간이 아니라며 비활성화됐고 공식 1/1·샘플 10회가 남아 있었다. 이는 당시 기록이며 현재 접수·잔여를 재확인한 값이 아니다. 자정 전 제출 요청은 미접수 기록으로 종료했고 나중 제출로 자동 이월하지 않는다.

## 서버 샘플

숫자와 처리 상태의 현재 집계는 [NOVA_SAMPLE_EXPERIMENTS.json](NOVA_SAMPLE_EXPERIMENTS.json), 시간순 해설은 [NOVA_DAY0_SUBMISSION.md](NOVA_DAY0_SUBMISSION.md)에 있다.

| ID | 실험 | 마지막 확인 상태 | private 결과 폴더 |
|---|---|---|---|
| E00 | 공식 SDK1.0.1 기본선 | 완료 42.179 | `results/codex-day0-submission-v1/` |
| E01 | 초안 뒤 같은 Doctor의 검토1회 | 서버 장애 복구 대기, 점수 없음 | `results/codex-review-once-sdk101-v1/` |
| E02 | 검토 + 오프라인 지침 검색 | 디스크 부족으로 준비 실패, 점수 없음 | `results/codex-guideline-agent-v1/` |
| E03 | 지침 색인을 RAM 전용으로 수정 | 완료 42.483; 전체 품질 개선 미확인 | `results/codex-memory-guideline-agent-v1/` |
| E04 | E03의 최종 기록 지시 묶음만 변경 | 완료41.079; 3건 정상, E03 대비-1.404점·미채택 | `results/codex-record-contract-agent-v1/` |
| E05 | E03의 종료 설명 힌트 2→6 | 완료39.932; 3건 정상, E03 대비-2.551점·미채택 | `results/codex-closing-six-agent-v1/` |

각 회차에서 계획, 코드·ZIP 해시, 검사 결과, 독립 감사, 제출 링크·서버 해시, 관측 시각, 점수·6영역·증례별 턴·시간·오류, 비교와 채택 여부, 게시 검증을 남긴다. 해당 자료가 아직 없으면 미확인/미완료로 유지한다. 첫 접수와 후속 관측의 원래 파일을 덮어쓰지 않는다. `phase.json`과 공개 집계는 최신 상태를 가리키는 변경 가능한 색인이다.

E03는 E00보다 +0.304점이지만 검토와 검색이 함께 달랐고 여러 영역이 하락했다. E04의 대조군은 E03이며 총점은1.404점 하락했다. E04도 검증된 개선으로 미채택이다. 같은 공개3샘플 재사용, 서버 모델 digest와 호출·토큰 미공개, 실제 대화·기록의 독립 검토 부재를 유지한다. 공식 점수나 임상 안전 인증으로 해석하지 않는다.

E00–E05는 각각 샘플 검증 한 번으로 접수한 서로 다른 코드 버전이다. 이 6종에서 공식 평가 요청은 0회다. HBT 실험의 로컬 모델 호출 수를 NOVA 서버 호출 수로 대신하지 않는다.

| 완료 샘플 | 정보 /25 | 추론 /15 | 검사·추적 /10 | 안전 /15 | 소통 /15 | 효율 /20 | 총점 /100 | 표시 증례 시간 합 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| E00 | 5.21 | 7.22 | 0.94 | 6.25 | 2.56 | 20.00 | 42.179 | 1,620초 |
| E03 | 5.75 | 9.17 | 0.69 | 6.25 | 2.00 | 18.62 | 42.483 | 2,757초 |
| E04 | 6.55 | 6.39 | 0.00 | 7.50 | 1.83 | 18.80 | 41.079 | 1,975초 |
| E05 | 5.20 | 8.89 | 0.22 | 3.75 | 3.39 | 18.48 | 39.932 | 3,172초 |

영역은 표시 소수점 2자리, 총점은 3자리라 합이 정확히 일치하지 않을 수 있다. 시간 합은 증례별 표시 시간의 합이며 빌드·대기·채점을 포함한 전체 작업 경과가 아니다. E01·E02는 점수가 없어 이 표에 0점으로 넣지 않았다.

## 실제 로컬 턴 실험과 합성 검사의 구분

| 실험 | 확인하려던 변화 | 실제 Doctor 생성 요청 | 완전 / 실패 | 생성 요청 시간 합 | 기록된 입력 / 출력 / total 토큰 | 판정 |
|---|---|---:|---|---:|---|---|
| [HBT01](HEALTHBENCH_TURN_PROBE.md) | 기존 dev 입력 4개를 여러 턴 대화로 사용 | 12 | 12 / 0 | 106.411초 | 13,121 / 9,737 / 22,858 | 독립 검토에서 정확성·반복·진료 시점 우려가 남아 개선 미채택 |
| [HBT02](HEALTHBENCH_CARE_TIMING_PROBE.md) | 고정 system 지시에 진료 시점 일관성 추가 | 4 | 3 / 1 | 66.619초 | 2,790 / 5,418 / 8,208 | 4개 완전 답변 전제 실패, 새 임상 비교 `null` |
| [HBT03](HEALTHBENCH_QUESTION_REUSE_PROBE.md) | 첫 요청에 이미 제공한 답 재사용 지시 추가 | 4 | 3 / 1 | 49.014초 | 1,879 / 4,363 / 6,242 | 4개 완전 답변 전제 실패, 새 임상 비교 `null` |

이 세 실험만 합하면 실제 생성 요청 **20회**, 완전 답변 **18개**, 실패 **2개**다. 프로젝트 전체 누적 호출 수를 뜻하지 않는다. HBT02·HBT03의 각 실패는 같은 한 기록의 빈 최종 답변과 `finish_reason=length`이며 두 실패로 중복 계산하지 않는다. 재시도·실패 건 재호출은 없다. 요청 시간은 개발·검토·게시 전체 시간이 아니며 모델 metadata/template 조회도 생성 요청과 별도다.

사용량의 입력·출력은 기존 제공자 유효 보고값이다. 특히 HBT02·HBT03는 원래 total 필드의 직접 보고/누락 후 파생 출처를 저장하지 않아 위 total은 **기록된 합계**로 표시한다. 출력 사용량의 추론 비중과 빈 답변의 정확한 upstream 원인은 미확인이다. 성공한 3개만 골라 임상 비교하거나 예산·gate·출력 폴더를 바꾸어 소진 후보를 구제하지 않는다.

| 뒤에 완료한 작업 | 고유 합성 검사 | 실제 모델 생성 | 의미 |
|---|---:|---:|---|
| [빈 응답 오프라인 진단](HEALTHBENCH_COMPLETION_DIAGNOSTICS.md) | 28개 응답 경로 확인 | 0 | 기존 8개 영수증과 frozen 실행기 점검, 원인 미확정 |
| [응답 메타데이터 helper](RESPONSE_RECEIPT_HELPER.md) | 34개 | 0 | 구조·usage 출처 추출, 런타임 미연결 |
| [오프라인 adapter](RESPONSE_RECEIPT_OFFLINE_ADAPTER.md) | 20개 | 0 | 등록된 fake fixture의 저장·중단 계약, 런타임 미연결 |
| [기존 파서 결합 검사](RESPONSE_RECEIPT_PARSER_CHECK.md) | 6개 | 0 | 구조 저장과 frozen parser의 결합, 런타임 미연결 |

private/public 사본에서 같은 모듈을 다시 확인한 횟수와 fake 호출 수는 고유 검사 수나 실제 모델 호출 수에 더하지 않는다. 각 모듈 검사 통과를 프로젝트 전체 검사 통과 또는 임상 개선으로 표현하지 않는다.

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
| HealthBench 파생 여러 턴 HBT01 | [12회 실행·독립 검토 결과](HEALTHBENCH_TURN_PROBE.md) | `results/codex-healthbench-turn-probe-v1/` |
| HBT02 진료 시점 일관성 | [실행 4회: 완전 3·빈 답변 한도 종료 1, 비교 전제 실패·미채택](HEALTHBENCH_CARE_TIMING_PROBE.md), [사전 설계](HEALTHBENCH_CARE_TIMING_DESIGN.md) | `results/codex-healthbench-care-timing-probe-v1/`, `results/codex-healthbench-care-timing-design-v1/` |
| HBT03 이미 제공한 답 재사용 | [실행 4회: 완전 3·빈 답변 한도 종료 1, 비교 전제 실패·미채택](HEALTHBENCH_QUESTION_REUSE_PROBE.md), [사전 설계](HEALTHBENCH_QUESTION_REUSE_DESIGN.md) | `results/codex-healthbench-question-reuse-probe-v1/`, `results/codex-healthbench-question-reuse-design-v1/` |
| HBT02/HBT03 빈 응답 오프라인 진단 | [기존 8기록 구조 확인·28합성 응답 검사·원인 미확정](HEALTHBENCH_COMPLETION_DIAGNOSTICS.md) | `results/codex-healthbench-completion-diagnostics-v1/` |
| 응답 구조·사용량 출처 보조 함수 | [별도 순수 함수·34합성 검사, 런타임 미연결](RESPONSE_RECEIPT_HELPER.md) | `results/codex-response-receipt-helper-v1/` |
| 응답 관측 연결 계약 | [저장 순서·중단 처리의 사전 설계 snapshot](RESPONSE_RECEIPT_INTEGRATION_DESIGN.md) | `results/codex-response-receipt-integration-design-v1/` |
| 별도 오프라인 응답 어댑터 | [20고유 합성 검사·실제 모델 0·런타임 미연결](RESPONSE_RECEIPT_OFFLINE_ADAPTER.md) | `results/codex-response-receipt-offline-adapter-v1/` |
| 응답 관측과 기존 파서의 결합 | [6계획군·fake 최대10회 사전 계약 snapshot](RESPONSE_RECEIPT_PARSER_CONTRACT.md) | `results/codex-response-receipt-parser-contract-v1/` |
| 응답 관측·기존 파서 합성 결합 검사 | [6고유 검사·기록된 fake61·실제 모델0](RESPONSE_RECEIPT_PARSER_CHECK.md) | `results/codex-response-receipt-parser-check-v1/` |
| 환자 대화 네 축 분포 | 기존 집계와 phase 기록 | `results/codex-dialogue-distribution-v1/` |
| 공식 웹 조사 | [NOVA_WEB_RESEARCH_20261012.md](NOVA_WEB_RESEARCH_20261012.md) | `results/codex-web-research-20261012-v1/` |
| 외래·응급/방문 목적별 흐름 | [미실행 설계](NOVA_ENCOUNTER_FLOW_PLAN.md) | `results/codex-experiment-record-index-v1/` |

HealthBench 원래200문항은 partial이고 400기록 검토에도 보류 기준이 남아 있다. 이전 dev20 후보는 모두 검증된 개선으로 미채택이며 동일 후보를 재호출하지 않는다. dev20과 holdout180 경계를 유지한다. 준비·구조·원문 조회의 정합성은 임상 생성 성능과 별개다.

private 전체 `results/`의 파일 경로·크기·수정시각 색인은 `results/codex-experiment-record-index-v1/record-inventory-v1.json`에 저장했다. 이 색인은 기존 파일의 존재 목록이며 모든 파일 내용의 검증이나 별도 백업 완료 증명은 아니다. 원래 원문·DB는 복제하거나 공개하지 않았다. 코드·합성 검사·일반 문서·집계만 명시적으로 선택해 Git에 보존한다.

E04 완료 후 실제 E03/E04 서버 원문 제공 경로를 확인했지만 조사한 화면·가이드·결과 폴더에서는 찾지 못했다. 없는 대화를 추정하지 않고 [E05 종료 힌트 실험](NOVA_CLOSING_SIX_PROTOTYPE.md)을 별도 한 변수 후보로 검사·독립 감사 후20:59KST에 한 번 접수했다. 원래 10월10일 로컬 대화 검토는 이 서버 실행의 증거로 사용하지 않는다.

21:05KST E05 진행 관측은 `results/codex-closing-six-agent-v1/sample-in-progress-v1.json`에 별도 보존했다. 지침 검색과 실제 계획 제시의 차이는 [E05 문서](NOVA_CLOSING_SIX_PROTOTYPE.md)에 기록했다. E04 검사·추적 0점의 원인은 실제 대화·기록 미확보로 미확인이다. 이번 후속 확인에서 새 코드·모델·샘플은 없다.

21:59–22:00KST E05 완료 관측: 총점39.932, 정보5.20/추론8.89/검사추적0.22/안전3.75/소통3.39/효율18.48. 모두30턴, 표시 시간1,021·1,096·1,055초(합3,172초). E03 대비-2.551점·+415초(+15.05%), 개선 미채택. `sample-completed-v1.json`, `aggregate-comparison-v1.json`, `adoption-decision-v1.json`에 원래 관측과 별도로 보존했다. 임상 오류 원인은 실제 원문 검토 전 미확인이다.

## NOVA에 올리는 절차

이 절차는 다음에 명시적으로 제출할 때 사용하는 안내다. 이 문서 정리 요청으로 업로드·샘플 검증·공식 평가·최종 선택을 실행하지 않는다. 기존 6종의 샘플 접수는 이미 완료했으므로 같은 후보의 샘플 반복 제출용 안내가 아니다.

1. [NOVA](https://nova.snubhai.org/)에 참가 등록 이메일로 로그인하고 소속 팀을 확인한다. [예선 제출 화면](https://nova.snubhai.org/submissions/?phase=preliminary)에서 업로드 양식을 연다. 현재 화면의 접수 상태와 남은 횟수를 확인한다.
2. 올릴 **실험 ID·변경 요인·고정 ZIP**을 먼저 정한다. 제목에는 ID와 버전을, 설명에는 실제 바꾼 부분·고정한 조건·검증 범위를 적는다. 아래 ZIP 목록과 manifest의 크기·SHA-256을 확인한다. 저장소 전체나 `results/` 전체를 압축해 올리지 않는다.
3. 평가 유형을 고른다. **샘플 검증**은 공개 샘플 실행과 팀 전용 참고 점수이며 공식 1회를 사용하지 않는다. **리더보드 제출**은 공식 평가이며 KST 기준 팀·단계별 하루 1회다. 참가자 코드 오류·시간 초과 등으로 공식 실행이 실패해도 해당 횟수가 사용된다. [공식 규칙](https://nova.snubhai.org/rules/)
4. 파일 선택에서 고정 ZIP을 지정하고 제목·설명·평가 유형·파일을 다시 확인한 뒤 최종 제출을 **한 번** 누른다. 공식 선택지가 비활성화돼 있으면 공식 접수를 진행하지 않는다. 서비스 상태의 이용 가능 표시만으로 접수가 열렸다고 판단하지 않는다. 샘플 승인을 공식 평가 승인으로 확대하지 않는다.
5. 등록 후 이동한 상세 화면의 **제출 ID·URL·서버 ZIP SHA**를 기록하고 로컬 SHA와 대조한다. 결과를 보기 전 다시 업로드하거나 추가 평가 버튼을 누르지 않는다. 접수 여부가 불명확하면 기존 목록·상세와 등록 기록부터 확인한다.
6. 빌드/파일 검증과 평가 상태를 확인한다. 완료되면 총점·6영역·증례별 정상/오류·턴·시간을 저장한다. 보이지 않는 모델 호출·토큰·digest는 `null`로 남긴다. 서비스 장애 복구 대기는 같은 제출의 상태만 확인하고 임의 재업로드로 대체하지 않는다.
7. **최종 선택**은 공식 평가 결과가 있는 제출 중 단계 최종 평가에 사용할 하나를 고르는 별도 동작이다. 샘플 점수 확인이나 코드 업로드만으로 최종 선택까지 끝난 것은 아니다. [플랫폼 이용 절차](https://nova.snubhai.org/)

샘플 채점과 공식 채점의 모델이 달라 참고 점수를 공식 점수로 환산하지 않는다. [개발 가이드](https://nova.snubhai.org/docs/)

### ZIP에 들어갈 파일

예선 코드 ZIP은 **50,000,000바이트 이하**이며 압축 루트에 `run.py`와 `requirements.txt`가 있어야 한다. `prepare()`와 `consult(client, state)`를 구현하고 최종 기록은 `client.diagnose()`로 제출한다. 현재 후보들은 `nova_sdk.py` 및 실제 import하는 모듈도 함께 넣었다. 의존성은 PyPI의 `name==version` 형식이며 실행 중 외부 인터넷은 차단된다. [제출 규칙](https://nova.snubhai.org/rules/) · [실행 계약](https://nova.snubhai.org/docs/)

현재 고정 ZIP에는 API 키·실제 질문/답변·개별 검토·공개 샘플 대본·원문 지침/DB·모델 가중치를 넣지 않았다. 제공 지침은 서버의 읽기 전용 `/opt/nova/guidelines`를 `NOVA_GUIDELINES_DIR`로 읽으며 ZIP에 다시 넣을 필요가 없다. [지침 제공 경로](https://nova.snubhai.org/docs/)

새 후보를 만들 때는 필요한 모듈을 명시적으로 포함하고 네트워크·키 없이 ZIP만 푼 상태의 import/prepare와 해당 변경의 합성 경로를 확인한다. 기존 `dist/submission.zip`은 별도 오래된 루트 빌드일 수 있으므로 아래 고정 SDK1.0.1 ZIP과 혼동하지 않는다. 합성 검사 통과와 실제 서버·임상 품질 검증은 구분한다.

### 이미 보존한 고정 ZIP

아래 경로는 **private 저장소 루트 기준**이며 공개 저장소에는 ZIP·실제 결과 영수증을 배포하지 않는다. 2026-10-13 08:35 KST 확인에서 6개 파일 모두 원래 build manifest의 크기·SHA와 일치했다. 재생성하거나 원래 ZIP을 덮어쓰지 않았다.

| ID | 로컬 ZIP 경로 | 바이트 | SHA-256 |
|---|---|---:|---|
| E00 | `results/codex-day0-submission-v1/build-20261012-sdk101-v1/day0-official-sdk101.zip` | 7,960 | `a02388291d2c2e93329b95fb07ee5852c9375498ac4d971b8b808a97e2d61ab1` |
| E01 | `results/codex-review-once-sdk101-v1/build-v1/nova-review-once-sdk101.zip` | 12,066 | `a54c6349062f02b986c9cf6a55dbdf17b9ae4f8d9676b244643b7051d4eba92c` |
| E02 | `results/codex-guideline-agent-v1/build-v1/nova-review-guidelines-sdk101.zip` | 29,694 | `7f8bcf85a799e2aa5317cac68538452a345b4a599589df7d40947d1924db6afa` |
| E03 | `results/codex-memory-guideline-agent-v1/build-v1/nova-review-memory-guidelines-sdk101.zip` | 36,090 | `fcad1c128bb9a3b790220f0c556a1782a0aa039003ae151ba6a2725a7ff46ac4` |
| E04 | `results/codex-record-contract-agent-v1/build-v1/nova-record-contract-sdk101.zip` | 36,741 | `761c1f7a4c63651e957b440ba291f933be0965203d3cbbb44ac18b1c0a5c485b` |
| E05 | `results/codex-closing-six-agent-v1/build-v1/nova-closing-six-sdk101.zip` | 36,089 | `a92020b5889fe550c3dfeb9a5ecd9b48ae396079571c43002778d93923c61a2d` |

기존 ZIP 확인은 빌드를 다시 돌릴 필요가 없다. 예를 들어 E03는 private 저장소 루트에서 다음처럼 읽기만 한다.

```sh
shasum -a 256 results/codex-memory-guideline-agent-v1/build-v1/nova-review-memory-guidelines-sdk101.zip
unzip -l results/codex-memory-guideline-agent-v1/build-v1/nova-review-memory-guidelines-sdk101.zip
```

별도 재현 목적이 생긴 경우에만 빌더에 **존재하지 않는 새 폴더**를 지정한다. 빌더는 네트워크 없는 합성 준비를 수행하므로 모델 실행이나 업로드 명령은 아니다. 아래는 실행법을 적은 것이며 이번에 실행하지 않았다.

```sh
# E00 원문 재현: 원래 build 폴더는 유지한다.
.venv/bin/python scripts/build_day0_submission.py --output-dir results/manual-e00-rebuild-v1

# E03 고정 소스 재현: --output은 ZIP 파일명이 아니라 새 결과 폴더다.
.venv/bin/python scripts/build_memory_guideline_submission.py --output results/manual-e03-rebuild-v1
```

이 빌더는 원래 소스 핀을 확인하므로 새로운 변경 후보를 만들려면 별도 소스·빌드 계약이 필요하다. 존재하는 폴더를 삭제하거나 핀만 바꿔 통과시키지 않는다. `.venv`는 private 저장소의 기존 Python 환경이며 공개 사본에서는 자체 환경을 먼저 준비해야 한다.

## 기록을 읽는 순서와 공개 범위

실험별 private 결과 폴더의 `phase.json`에서 최신 결과 파일을 찾고 `sample-completed-v1.json` 또는 `completed-aggregate-v1.json`, 비교·채택 기록, 게시 검증을 읽는다. `build-v1/manifest.json`의 미제출/0회 표현은 **빌드 당시 snapshot**이다. 이후 실제 접수·완료 상태를 지우거나 현재 미실행으로 오독하지 않는다. 시간순 원래 관측도 그대로 보존한다.

새 관측은 새 영수증에 관측 시각·제출 ID/URL·로컬/서버 ZIP SHA·표시 상태·오류·점수·영역·증례 결과·호출/토큰의 확인 여부·추가 요청 수를 남긴다. 점수 없는 장애를 0점으로 바꾸지 않고 빈 답변/한도 종료의 중복 집계를 피한다.

공개 사본은 코드·합성 검사·일반 문서·집계만 포함한다. 실제 입력·답변·기준·환자/도구 원문·개별 검토·키·원문 DB와 원래 영수증은 private에 보존한다. 전체 `results/`를 공개하거나 문서 정리를 전체 백업 완료라고 표현하지 않는다. 과거 200문항·dev20의 상세 위치는 위 기록 색인을 따른다.
