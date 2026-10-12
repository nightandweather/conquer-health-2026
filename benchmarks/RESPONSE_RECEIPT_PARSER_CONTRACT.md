# 응답 관측과 기존 파서의 합성 결합 계약 — 미실행 설계

2026-10-13 KST. [완료한 오프라인 어댑터](RESPONSE_RECEIPT_OFFLINE_ADAPTER.md)는 가짜 parser stub을 사용한다. 앞선 28회 응답 재현은 기존 실행기의 해석을 확인했지만 새 구조 저장과 결합하지 않았다. 따라서 후속 범위는 **기존 `_SourceTransport`의 inner client 반환 앞에 구조 저장을 넣었을 때의 합성 결합 동작**으로 한정한다. 이번에는 코드·합성 실행·실제 모델 호출·NOVA 제출을 추가하지 않았다.

## 확인할 경계

기존 wrapper는 inner client를 부르기 전에 `calls`를 올리고 `provider_response_received=False`로 초기화한다. inner client가 정상 반환한 다음에야 수신 플래그가 true가 된다. 그러므로 가짜 객체를 받은 뒤 저장 오류가 발생하면 기존 결과는 `transport_failure`와 false일 수 있다. 이때 별도 영수증이 기록한 “객체 관측됨”을 지우거나 제공자 미응답으로 바꾸면 안 된다. 이것은 앞으로 연결할 때 구분해야 할 상태이며, 과거 HBT02/HBT03 실패의 원인을 찾았다는 뜻이 아니다.

별도 test-only harness가 등록된 합성 객체를 받고, 고정 helper 메타데이터를 저장한 뒤 **같은 원래 객체**를 그대로 반환하도록 계획한다. 기존 helper·오프라인 어댑터·HBT 실행기와 NOVA 런타임은 수정하지 않는다. 정상 반환과 잘못된 종료 사유의 거절을 기존 wrapper의 직접 경로와 비교한다. 빈 문자열이나 None도 이 envelope wrapper에서는 반환될 수 있으므로 정상 반환을 완전 답변이나 임상 성공으로 판정하지 않는다.

`_SourceTransport`에는 독립 parser 진입 hook가 없다. outer wrapper 진입, fake 호출, helper 진입, observer 정상 반환을 각각 세고 이를 실제 의미 검사 진입 횟수로 이름 붙이지 않는다. 각 관측 분기 안에서는 fake→helper→외부 반환의 객체 동일성과 JSON 불변을 확인한다. 직접/관측 분기는 별개 fixture 객체의 fingerprint와 기존 필드를 비교하며, 거절 경로에는 외부 반환 객체가 없다는 한계를 기록한다.

## 여섯 계획 검사군

| 계획군 | 입력·상황 | 확인할 차이 |
|---|---|---|
| C01 | UTF-8 정상 final, stop, total 누락 | 같은 객체·기존 결과 유지; raw total 누락과 파생 합계를 별도 보존 |
| C02 | 빈 final, length, reasoning 구조 있음 | envelope 반환을 완전 답변으로 승격하지 않음; 추론으로 대체하지 않음 |
| C03 | 유효 final 문자열, 잘못된 finish | 구조 저장 뒤에도 기존 invalid_provider_response와 수신 플래그 유지 |
| C04 | fake가 객체 반환 전에 예외 | helper 0, 관측 증거 없음과 복구 unknown 구분 |
| C05 | 객체 반환 뒤 구조 저장 실패 | 실패 영수증 저장 가능/불가 두 분기; 기존 false 플래그와 관측 증거 분리 |
| C06 | 구조 저장 완료 뒤 반환 전 중단 | 구조는 보존, 기존 interrupted/false 유지, 재전송 차단 |

C01–C03은 직접/관측 경로 각 한 번, 나머지는 관측 경로만 사용한다. **단일 계획 행렬은 direct 3 + observed 7 = fake 최대 10회**이며 각 사전 등록된 분기는 예약당 최대 한 번이다. 두 저장 실패 분기는 별개의 합성 scope다. 소스·요청 불일치의 호출 전 차단과 같은 owner/다른 출력 이름 재진입은 추가 fake 0이어야 한다. 여섯 군과 열 번은 계획 수치이며 통과 검사나 실제 실행량이 아니다. 개발 재실행이 생기면 별도로 센다.

기존 배타 쓰기·flush/fsync·snapshot 및 terminal 이후 commit 순서를 재사용하고, 완료한 42개 장애 행렬을 다시 구현하지 않는다. 저장 실패면 observer 반환을 막는다. 읽히는 구조 파일만으로 fsync 성공을 주장하지 않는다. 실패 영수증도 저장할 수 없으면 이전에 저장 완료를 입증한 디스크 증거만 사용하고, 뒤의 결과 기록으로 메모리 속 관측을 복구 사실로 다시 넣지 않는다. 입증하지 못하는 관측은 unknown이다. 예약·부분 파일·남은 잠금을 지우거나 다른 출력 이름으로 재전송하지 않는다. 최종 기록 자신이 자신의 저장 완료를 증명하지 않도록 기존 저장 장벽 계약을 유지한다.

## 범위와 다음 단계

제안 파일은 `tests/test_response_receipt_parser_contract.py` 하나이며 아직 없다. 먼저 socket/urllib 진입점을 차단한 상태로 healthbench를 import하고, 얻은 실제 Transport 클래스의 생성자와 호출 메서드를 차단한 뒤 `_SourceTransport`를 import한다. 이 차단은 정리 종료까지 유지하며 `_SourceTransport` 자체 코드를 monkeypatch하지 않는다. 실제 Transport 생성·호출·키 읽기·endpoint 입력·metadata/template 조회는 배제한다. 등록 fixture와 소스/요청 핀만 사용한다. 실제 모델 신원은 이번에 다시 조회하지 않았다.

구조 영수증은 기존 helper의 허용 필드와 고정 오류 코드만 저장한다. 답변/추론 본문, 추론 해시, 예외 원문·traceback·임의 경로를 복사하지 않는다. content 해시는 익명화가 아니다. Transport가 반환 전에 거절한 envelope나 실제 HTTP 전체 관측은 이 검사로 검증되지 않는다.

이 계약을 구현하더라도 실제 런타임 연결·임상 정확성·원인 규명·새 후보 채택이 되지 않는다. HBT02/HBT03의 소진 상태, 원인 미확정, 임상 비교 null·미채택과 네 완전 답변 조건을 유지한다. 이전 후보 재호출이나 NOVA 자동 제출은 포함하지 않는다.

[기계 판독 설계](RESPONSE_RECEIPT_PARSER_CONTRACT.json). 비공개 원래 설계·감사·게시 검증은 `results/codex-response-receipt-parser-contract-v1/`에 보존한다.
