# 기존 코드 조사: 읽기 전용

조사일: 2026-10-27. 파일·Git 메타데이터를 읽었으며 checkout·수정·push·AWS 호출은 하지 않았습니다. 아래 커밋은 로컬 상태로, GitHub 최신 또는 실제 배포 커밋이라는 뜻이 아닙니다.

| 코드 근거 | 확인한 내용 | 이번 구현과의 관계 |
|---|---|---|
| [UtterAI_Infra](https://github.com/UtterAI-aws13/UtterAI_Infra), `cd92b1b11e0ba48d1c05ae7de6dac0d900bb484c`, `lambda/finops_slack/handler.py` | Slack 원문 서명·±300초 timestamp·base64 처리·Secrets Manager 캐시·슬래시 커맨드. 별도 Lambda를 비동기 Invoke 후 수신 응답 | Slack 수신 자체는 기존 구현에도 있음. 새 기술 성과로 포장하지 않음. 이 경로에는 요청한 SQS consumer/DLQ/지속적인 event dedup 구현이 보이지 않음 |
| 같은 커밋 `lambda/finops_agent/handler.py` | Bedrock invoke_model·도구 루프(최대 10회)·response_url 전송·Cost/Kubecost용 Lambda 호출 코드 | 비용 조회 전용 경로. SQS visibility/모델 checkpoint/불명확 전송 복구와 다름. 실제 비용 조회 도구는 연결하지 않음 |
| [UtterAI_AI](https://github.com/UtterAI-aws13/UtterAI_AI), `456314c4efa2a0165eda4c97e7ba9d383c093746`, `app/pipelines/agentcore_client.py` | `boto3.client('bedrock-agentcore')`와 `invoke_agent_runtime`, runtimeSessionId, SOAP Note 형태 파싱 코드 | 관리형 Bedrock Agents의 `InvokeAgent`와 구분. 함수명은 invoke_agent지만 실제 호출은 AgentCore Runtime. 주석만으로 메모리·검색의 운영 동작을 확정하지 않음 |
| 같은 커밋 `app/workers/report_generation_worker.py` | 20초 polling·visibility heartbeat·report graph·session/patient/therapist/run 기반 작업 처리 | 재사용 가능한 개념은 있으나 의료 도메인 의존성·권한·결과 저장 방식이 달라 복사하지 않음. 코드 존재만 확인했으며 실운영 품질은 별도 Work의 범위 |
| `UtterAI_AI-5agent` 로컬 커밋 `c5744dcaf5808d6da86bffa12e43c38b481c5579` | AgentCore 호출 파일·여러 worker 파일 존재 | 이번 작업에서 수정하지 않음 |

명시적으로 조사한 UtterAI 앱 경로와 Projects의 Python 코드 검색에서 `app_mention` → SQS → Slack thread 응답을 구현한 독립 워커는 찾지 못했습니다. 모든 로컬·원격 저장소를 조사한 것은 아니므로 전사적으로 미구현이라고 단정하지 않습니다. Slack 관련 alert/FinOps 코드는 존재하므로 '기존 Slack 코드가 없다'고 결론 내리지 않습니다.

## 연계 판단

현재는 독립 Bedrock Converse 어댑터가 최소 구성입니다. 외부 모델 호출은 아직 실행하지 않았습니다. 사용자가 연계를 원하면 다음 조건을 확인한 뒤 별도 어댑터를 추가해야 합니다.

1. 대상이 FinOps Lambda, AgentCore Runtime, Bedrock Agents 중 무엇인지 확정.
2. 승인된 테스트 ARN·리전·배포 버전과 응답 스키마 확인.
3. Slack user/team → 서비스 사용자/도메인 데이터의 서버 측 권한 매핑. 입력에 포함된 patient/session ID를 그대로 신뢰하지 않음.
4. 외부 세션 ID에 workspace/user/thread 범위를 포함하고 HMAC 처리해 대화·사용자 간 격리. 기존 의료 대화의 session ID를 재사용하지 않음.
5. agent 내부 도구 루프·토큰·timeout 상한을 확인. 기존 wrapper의 `max_tokens` 인자는 확인한 코드에서 Runtime payload로 전달되지 않으므로 비용 제한으로 간주할 수 없음.
6. 새 어댑터의 계약 테스트, 허용 도구 목록, 장애·권한 음성 테스트, 승인 후 작은 실서비스 smoke test.

연계하면 좋아 보인다는 이유만으로 기존 인프라나 의료 데이터를 접근하지 않습니다. 이번 결과에는 연계 완료·메모리 공유·실배포 확인을 주장하지 않습니다.
