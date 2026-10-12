# 제공 지침 기반 오프라인 검색 프로토타입

2026-10-12 제공 파일 6개를 수령·검증하고, 지침 검색과 Doctor 입력 초안을 만드는 기능을 구현했습니다. 원문·색인·근거 패킷은 비공개 로컬에 유지하며, 공개에는 코드·합성 검사·집계만 저장합니다. 현재 `run.py`의 진료 행동에 적용하지 않았고 새 임상 성능 점수는 없습니다.

## 현재 작동하는 흐름

`NOVA_GUIDELINES_DIR`의 제공 Markdown → 원문 바이트·목록·출처·라이선스 확인 → SQLite FTS5/BM25 색인 → 원문 구간·문서 ID·출처·바이트 위치 반환 → 환자 대화와 구분한 Doctor 입력 초안.

최종 실제 색인은 15,904문서, 389,730구간입니다. 이 Mac에서 19.48초, 최대 메모리 약165MiB, DB 1,266,274,304바이트(약1.18GiB)로 만들었습니다. SQLite 3.53.1/FTS5 환경의 한 번 관측이며 서버 성능을 보증하지 않습니다. 일반 질의 4개에서 5·5·5·0개의 결과를 받았고, 15개 원문 구간의 바이트 위치·전체/구간 해시·출처·라이선스를 독립 확인했습니다. 이는 조회 정합성 검사이며 임상 관련도·검색 재현율 검사가 아닙니다.

`nova/guideline_context.py`는 원래 system/user/assistant 메시지의 내용·역할·순서를 보존하며 공통 출처 지침과 별도의 참고 JSON을 삽입합니다. 대조군에는 같은 위치의 빈 참고 목록을 넣고, 후보에는 검증된 원문 구간을 넣습니다. 일반 합성 이력을 사용한 실제 3구간 입력 준비를 확인했습니다. 모델 호출·네트워크 호출은 0회입니다. 원문이 지시문처럼 보여도 참고 데이터로만 전달하며, 이 구분이 모델의 지시문 혼동을 완전히 막는다고 주장하지 않습니다. 참고 문자 예산과 실제 모델의 전체 입력 토큰 예산은 별도로 검증해야 합니다.

## 로컬 실행

Python 표준 라이브러리와 FTS5가 있는 SQLite를 사용합니다. 원문 경로는 실제 다운로드한 `guidelines` 폴더 또는 서버의 `/opt/nova/guidelines`입니다. 출력 폴더는 본인 소유·0700으로 미리 만들고 원문 폴더 밖에 두세요. 기존 파일을 덮어쓰지 않습니다.

```sh
export NOVA_GUIDELINES_DIR=/path/to/extracted/guidelines
mkdir -m 700 results/local-guidelines
python -m nova.guidelines build --db results/local-guidelines/guidelines.sqlite
python -m nova.guidelines status --db results/local-guidelines/guidelines.sqlite
python -m nova.guidelines search --db results/local-guidelines/guidelines.sqlite --query hypertension --limit 3
python -m nova.guidelines search --db results/local-guidelines/guidelines.sqlite --query 인플루엔자 --language ko --limit 3
```

입력 초안은 본인이 준비한 원래 텍스트 메시지 JSON으로 만듭니다. 이 명령은 모델을 호출하지 않으며 출력에는 원문 구간이 포함되므로 비공개로 유지해야 합니다.

```sh
python -m nova.guideline_context --messages-file private_data/messages.json --query hypertension --db results/local-guidelines/guidelines.sqlite --output results/local-guidelines/request-draft.json
```

## 자료 품질과 남은 조건

자료 15,904개가 전부 최신 진료지침인 것은 아닙니다. 목록상 질환 개요9,656개·진료지침2,645개와 권고문·논문·요약·일반인 자료 등이 섞여 있습니다. 영어는90.53%이고 연도는1971–2026, 공란5개,2016년 이전899개입니다. 같은 출처URL의 중복은 문서 내용의 중복을 뜻하지 않습니다. 검색 결과의 문서 종류·대상·발행 시점·적용 조건을 확인해야 하고, 연도만으로 임상적 유효성을 판정하지 않습니다.

검색은 단어 기반입니다. 한국어 형태소·영한 번역·동의어·의미 검색·의학적 정확성·새 답변 품질은 미검증입니다. 구간을 자르는 과정에서 앞뒤 문맥이 충분한지도 별도로 확인해야 합니다. 검색 내용은 일반 의학 참고이며 환자의 증상·검사값·병력을 인증하지 않습니다. 벤치마크 질문·정답·평가 기준은 색인하지 않았습니다.

DB는 50MB 코드 ZIP에 넣을 수 없습니다. 대회에서는 읽기 전용으로 탑재된 원문을 사용하고, 준비10분·CPU2코어·RAM8GB 조건의 색인 준비 또는 제한된 검색 방식을 별도로 검증해야 합니다. 현재 Mac의 빠른 빌드 결과를 서버 준비 통과로 해석하지 않습니다. 같은 UID의 악의적인 동시 경로 변경을 모든 시점에서 배제하지 못하는 한계도 유지합니다.

외부 API 호출은 제출 실행에서 금지됩니다. 제공 원문의 출처·라이선스를 보존하며 ND 등 변경본 배포 제한 자료의 색인/번역/요약본을 공개 저장소에 올리지 않습니다. [공식 개발 가이드](https://nova.snubhai.org/docs/), [참가 규칙](https://nova.snubhai.org/rules/)

제공 벤치마크의 전체 구조·중복·분할 품질은 [별도 분석](NOVA_BENCHMARK_QUALITY.md)에 있습니다. [전제·출처·미확인 구분 지침 초안](../prompts/doctor-evidence-first-v1.txt)은 함정 대응을 검토하기 위한 미시험 후보이며 이번 검색 구현이나 오늘의 제출 기준선에 자동 적용하지 않았습니다.
