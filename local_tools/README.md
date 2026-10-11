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
