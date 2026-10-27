# LocalStack 통합 검증 (1단계)

검증일: 2026-10-27. 원시 결과: [evidence/localstack-e2e.json](../evidence/localstack-e2e.json)

## 무엇을 바꿨나

이전 검증은 SQS를 Python 모의 큐로, DynamoDB를 SQLite로, 모델과 Slack을 테스트 대역으로 대신했다. 이번에는 다음 구간을 실제 프로세스와 AWS 호환 API로 연결했다.

```text
서명한 HTTP 요청 → LocalStack Lambda Function URL → LocalStack SQS
→ 워커 프로세스(boto3) ⇄ LocalStack DynamoDB(조건부 쓰기)
→ Ollama(llama3.2:1b, OpenAI 호환 API) → loopback 모의 Slack(chat.postMessage)
```

- 인프라: `infra/terraform`의 공통 모듈을 `envs/local`로 LocalStack에 적용(리소스 21개). `envs/aws`는 같은 모듈을 쓰지만 `validate`만 하고 적용하지 않았다.
- 로컬 설정이 실제 AWS에 적용되지 않도록 `envs/local` provider에 `allowed_account_ids = ["000000000000"]`(LocalStack 고정 계정)을 두었다. 엔드포인트나 자격증명이 잘못 잡히면 plan 단계에서 실패한다.
- 워커 핵심 로직(`worker.py`)은 바꾸지 않았다. 모델 공급자 선택, Slack API 주소, 수신 visibility만 환경 변수로 분리했다.

## 결과

| 시나리오 | 관찰 결과 |
|---|---|
| 고유 이벤트 100개, 클라이언트 동시성 10 | HTTP 200 100건, 답글 100개, 모델 호출 100회, DynamoDB SENT 100건, 이벤트당 최대 답글 1개 |
| 같은 이벤트를 3번 전달(원본 1 + `X-Slack-Retry-Num` 1·2) × 20개 | 60건 모두 큐 적재, 답글 20개, 모델 호출 20회 |
| 모의 Slack이 첫 전송에 429(Retry-After 2) 반환 | 거절 1회 후 답글 1개, 모델 호출 1회(결과 재사용), 최종 SENT |
| 모의 Slack이 답글을 받은 뒤 응답 지연(클라이언트 8초 timeout 초과) | AMBIGUOUS로 격리, 100초 관찰 동안 답글 1개 유지, 모델 호출 1회 |
| 워커 SIGTERM | 종료 코드 0 |
| 파싱 불가능한 메시지 3개 직접 적재 | 3개 모두 DLQ 이동(23.4초, maxReceiveCount 8, 이 시나리오만 수신 visibility 2초) |

ACK 시간(클라이언트 측, LocalStack Function URL 왕복):

| 구간 | p50 | p95 | 최대 |
|---|---:|---:|---:|
| 측정 구간 100건 | 0.284초 | 1.046초 | 1.119초 |

## 해석할 때 주의할 점

- **ACK 시간은 LocalStack 기준이다.** 실제 Lambda의 cold start, 네트워크, Secrets Manager 지연이 들어 있지 않다. 같은 날 LocalStack을 새로 띄운 뒤 첫 동시 요청은 컨테이너 기동 때문에 최대 8초가 걸렸다(소규모 시험). 본 실행의 "first wave"는 앞선 시험으로 컨테이너가 이미 떠 있던 상태라 cold start 수치가 아니다.
- **IAM은 검증 대상이 아니다.** LocalStack은 기본 설정에서 IAM 정책을 강제하지 않는다. 역할과 정책은 생성만 확인했다.
- **Slack 재시도는 헤더를 붙여 흉내 냈다.** 실제 Slack 서버의 재전송 간격과 조건은 재현하지 않았다.
- **모델은 Ollama 소형 모델이다.** 답변 품질, Bedrock 지연, 실제 비용은 측정하지 않았다. 모델 호출 수는 워커가 남기는 `model_call` 로그로 셌다.
- **AMBIGUOUS 관찰은 100초 동안 재전송이 없었다는 의미다.** 이 상태의 메시지는 수신 횟수를 모두 소진한 뒤 DLQ로 이동하며, 사람이 확인해야 한다.
- **파싱은 되지만 스키마가 잘못된 메시지**(예: `{"key": 1}`)는 워커의 90초 재시도 경로를 타서 DLQ까지 약 12분이 걸린다. 배포 중 버전이 다른 작업을 바로 버리지 않으려는 설계다.

## 재현

```bash
# LocalStack 로그인(lstk login)과 Ollama 실행이 필요하다. AWS 계정은 필요 없다.
scripts/local_up.sh                       # 기본: 100개 burst, 20개 중복, 100초 관찰
scripts/local_up.sh --burst 5 --duplicates 2 --observe 5   # 빠른 확인
```
