# 응답 관측과 기존 파서의 합성 결합 검증

2026-10-13 KST. [고정한 계약](RESPONSE_RECEIPT_PARSER_CONTRACT.md)에 따라 새 test-only 파일 하나에서 기존 `_SourceTransport`와 구조 저장을 결합해 확인했다. **고유 검사 6개가 private/public 각각 지정 모듈 1개에서 통과했다.** 기존 helper·오프라인 어댑터·HBT 실행기·NOVA 런타임은 바꾸지 않았으며 실제 모델·제공자 HTTP·metadata/template·임상 재검토·NOVA 접수는 모두 0이다.

정상 UTF-8·total 누락, 빈 final+length, 유효 final+잘못된 finish의 세 fixture를 직접/관측 경로로 비교했다. 각 분기 안의 객체 동일성과 요청 불변, 분기 간 fingerprint와 기존 결과의 일치를 확인했다. total의 원래 누락 상태와 기존 코드가 계산한 합계를 구분했다. 빈 답의 envelope가 반환되어도 완전 답변으로 판정하지 않았다.

저장 실패 전에 객체를 관측한 경우, 별도 저장된 실패 영수증은 관측 true를 보존하면서 기존 수신 플래그는 false/transport_failure일 수 있었다. 구조와 실패 영수증의 저장이 모두 실패하면 읽히는 파일과 뒤의 완료 기록이 있어도 관측은 unknown/null로 남았다. 구조 저장 직후 반환 전 중단에서는 구조는 보존되고 기존 결과는 interrupted/false였다. 이는 앞으로 관측을 연결할 때 구분해야 할 동작이며, 과거 HBT02/HBT03 빈 답변의 upstream 원인을 규명한 것은 아니다.

| 최종 지정 모듈 | 고유 검사 | fake / helper | outer 진입 / observer 정상 반환 | 네트워크·실제 Transport 시도 |
|---|---:|---:|---:|---:|
| private | 6 통과 | 10 / 6 | 10 / 3 | 0 |
| public 같은 사본 | 6 통과 | 10 / 6 | 10 / 3 | 0 |

열 개 분기는 direct 3·observed 7이다. outer 진입은 독립 의미 파서 진입 횟수가 아니며, 기존 wrapper의 정상 반환 4·오류 6은 임상 성공/실패 집계가 아니다. 복구 시 순수 메타데이터 비교는 pipeline helper 횟수에 더하지 않았다. 최종 private/public 모듈 시간은 0.084886/0.089666초이며 전체 작업 시간이 아니다.

개발 실행은 세 번, root의 비공개 검증은 수정 전후 두 번, 공개 검증은 한 번이었다. 각각 fake 10회다. 첫 개발 실행은 테스트가 예상한 provenance 이름이 기존 helper와 달라 1개 실패했으며 기대값을 수정했다. 독립 감사에서는 첫 재진입의 전역 차단이 이후 검사 이유를 가리는 공백을 찾았다. 새 harness의 소비된 예약/다른 출력 이름 거절을 분리해 열 개 분기 모두 정확한 거절 이유와 추가 fake/outer 0을 확인했다. 소스·요청·무결성 실패는 계속 영구 차단한다. 수정 전 결과도 보존했다.

독립 검토는 전체 행렬을 반복하지 않고 두 가지 좁은 확인에 fake 1·helper 1·outer 1·observer 반환 0을 사용했다. 저장 실패 후 null 보존과 재진입 차단, 소스 불일치 뒤 원래 상태로 돌아와도 차단 유지가 확인됐다. **이번에 기록된 개발·검증·독립 확인의 합성 fake 합은 61회**다. 실제 모델 호출이 아니며, 재실행과 독립 확인을 고유 검사 6개에 더하지 않는다.

차단을 설치한 상태로 모듈을 import하고 실제 Transport 클래스의 생성자와 메서드를 막았다. 최종 두 사본은 13개 소스 파일과 fixture registry digest 1개를 같은 값으로 확인했다. 지정한 기존 128파일의 전후 해시도 일치했다. 전체 백업·운영체제 전체 네트워크 격리·모든 장치의 전원 차단 내구성 검증은 아니다.

영수증에는 구조 정보와 고정 코드만 남고 답변/추론 본문·추론 해시·예외 원문·traceback·임의 경로는 복사되지 않았다. content 해시는 익명화가 아니다. 실제 과거 응답을 다시 처리하지 않았고, Transport가 반환 전에 거절한 envelope나 실제 HTTP의 관측 범위도 확대하지 않았다.

이 결과는 실제 런타임 연결이나 임상 개선의 증거가 아니다. HBT02/HBT03의 소진·임상 비교 null·미채택·원인 미확정 및 네 완전 답변 조건을 유지한다. 다음 작업을 자동으로 새 모델 호출이나 NOVA 제출로 전환하지 않는다.

[합성 검사 코드](../tests/test_response_receipt_parser_contract.py), [일반 집계 JSON](RESPONSE_RECEIPT_PARSER_CHECK.json). 비공개 실행·개발 실패·독립 감사·게시 영수증은 `results/codex-response-receipt-parser-check-v1/`에 보존한다.
