# 운영·비용·배포 전 검토

## 준비된 것과 실행하지 않은 것

실행 준비물: Lambda 핸들러, 장기 실행 SQS consumer, Bedrock Converse와 Slack 어댑터, Dockerfile, 고정 의존성, Slack manifest, Terraform 모듈(`infra/terraform/modules/slack_worker`)과 환경 두 개(`envs/local`: LocalStack 적용, `envs/aws`: 검증만).

실행하지 않은 것: AWS 자격증명 조회, 외부 Slack/Bedrock 요청, 리소스 생성·변경, 이미지 빌드·업로드, GitHub 저장소 생성·push. `envs/aws`는 `terraform validate`만 실행했고 plan·apply는 하지 않았습니다. 같은 모듈을 LocalStack에 적용해 동작을 확인했지만 LocalStack은 IAM과 리소스 정책을 강제하지 않습니다.

## 권한과 비밀정보

별도 Secrets Manager secret 둘을 준비합니다. 수신용은 `{"signing_secret":"..."}`, 워커용은 `{"bot_token":"xoxb-..."}` 형식입니다. Terraform 모듈에는 비밀값을 넣지 않고 ARN만 전달합니다. worker token은 시작 시 읽으므로 회전 후 워커를 재시작해야 합니다. 수신 secret은 5분 캐시가 있어 회전 직후 재시도가 필요할 수 있습니다. 구·신 secret 동시 검증은 구현하지 않았습니다.

| 역할 | 필요한 권한 | 제공하지 않는 권한 |
|---|---|---|
| IngressRole | 해당 queue SendMessage, signing secret GetSecretValue, 해당 로그 쓰기 | 큐 소비·모델·bot token·테이블 접근 |
| WorkerRole | 해당 queue Receive/Delete/ChangeVisibility, 해당 테이블 Get/Put, bot secret Get, 승인 model/profile ARN InvokeModel | 임의 AWS 도구 호출·Signing secret·큐 재적재·DLQ redrive |
| ExecutionRole | 해당 ECR 이미지 pull·로그 쓰기, ECR 토큰 요청 | 애플리케이션 데이터 접근 |
| 운영자 별도 | DLQ 조회/정상 상태 대조/승인된 복구 | 워커 task role에 부여하지 않음 |

`ecr:GetAuthorizationToken`만 AWS 특성상 Resource `*`입니다. Function URL은 Slack이 SigV4를 보내지 않으므로 AuthType NONE과 공개 호출 정책이 필요하고, 앱 내부 Slack 서명을 필수로 확인합니다. 2025년 10월 이후 새 URL에 필요한 두 invoke 권한을 `aws_lambda_permission`으로 포함했습니다(Terraform 이전 과정에서 빠졌다가 복구, [사례](aws-deployment.md#로컬-에뮬레이션이-숨긴-문제를-찾은-사례)). [AWS URL 인증](https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html), [Terraform aws_lambda_permission](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lambda_permission).

초기 reserved concurrency=2는 부하와 일부 비용을 제한하지만 공개 endpoint 남용 비용을 완전히 막지는 않습니다. API Gateway HTTP API는 스테이지 throttling 등 운영 제어가 필요할 때 대안이며 비용과 추가 구성이 생깁니다. WAF 연계까지 필요하면 지원되는 API/edge 구성을 별도 확인해야 합니다.

SQS는 관리형 SSE, DynamoDB는 SSE·온디맨드·TTL을 설정합니다. 고객 관리 KMS 키를 쓰려면 필요한 키 정책/Decrypt 권한을 별도 추가해야 합니다. 큐에 사용자 질문과 slash `response_url`이 있으므로 큐/DLQ 접근 권한은 비밀정보 접근으로 취급합니다. response_url은 HTTPS의 정확한 `hooks.slack.com/commands/` 경로만 허용하고 redirect를 따르지 않습니다. GovSlack은 이번 범위에서 지원하지 않습니다.

원문 질문·response_url·토큰·SDK 오류 전문을 로그로 출력하지 않습니다. 로그에는 결과 분류와 receive count만 남깁니다. 원문 질문은 DynamoDB에 복제하지 않지만 생성 결과는 저장합니다. 로그 7일, source queue 4일, DLQ 14일, dedup state TTL 35일입니다. TTL 삭제는 즉시 보장되지 않습니다. 수동 개인정보 삭제 요구가 있으면 큐·DLQ·state·Slack에 걸친 삭제 절차를 운영자가 수행해야 합니다.

## 리소스와 실행 순서: 승인 후에만

Terraform 모듈은 SQS 2개, DynamoDB 1개, Lambda와 공개 URL·호출 권한 2개, IAM 역할 3개, 로그 그룹 2개, CloudWatch alarm 2개, ECS TaskDefinition을 만듭니다. 외부 알림 발송 대상은 연결하지 않습니다. **ECS Service·VPC·NAT·secret·ECR·S3 bucket은 생성하지 않습니다.**

1. 독립 sandbox 계정/리전과 테스트 Slack workspace/channel/user를 확정. 실제 bot user ID를 포함해 allowlist 입력.
2. 승인된 model ID와 필요한 모든 정확한 foundation-model / inference-profile ARN을 확정. cross-region profile의 대상 리전도 승인 필요.
3. secret을 비공개로 저장. sandbox의 버전 관리 S3 bucket에 Lambda 패키지 업로드. object version으로 고정. 패키지 생성은 `python tools/package_lambda.py`로 로컬에서 수행할 수 있음.
4. `terraform plan`으로 IAM·공개 URL·삭제 범위를 검토한 뒤 적용. 이번 작업에서는 plan도 실행하지 않음.
5. Slack manifest의 URL을 실제 URL로 변경하고 설치. bot을 허용 채널에 초대. url_verification, 서명 음성 테스트와 큐 적재 smoke test부터 진행.
6. Docker 이미지 빌드·검사·ECR 업로드 후 digest로 고정. 베이스 이미지 태그는 현재 Dockerfile에서 고정 digest가 아니므로 배포 전에 실제 digest를 기록.
7. worker task는 기본 `ENABLE_PAID_MODEL=disabled`. 승인 후 해당 실행의 환경 변수 override로 `approved`를 지정. 단일 Fargate task 또는 승인된 sandbox 머신에서 실행. task definition만 등록해도 소비자가 자동 시작되지 않음.
8. Fargate 사용 시 **기존의 승인된** cluster/subnet/보안 그룹·HTTPS 외부 연결을 선택. private subnet의 NAT 또는 외부 프록시가 Slack에 도달해야 함. SQS/DynamoDB/Bedrock 엔드포인트만으로 Slack에 연결되지 않음. 신규 NAT/EKS 클러스터는 이 최소 구성에 필요하지 않음.
9. 컨테이너는 non-root·읽기 전용 root filesystem·수신 포트 없음. ECS StopTimeout 120초, SIGTERM 이후 새 receive를 중단하고 수신 중이던 작업을 drain. 실제 종료·네트워크 장애 실험은 승인 후 필요.

Lambda 패키지에는 고정 SDK를 포함할 수 있습니다. 런타임에 내장된 SDK 버전에 의존하는 배포와 구분하세요. `tools/package_lambda.py`는 현재 Python 환경의 lock 버전 일치를 확인하며 외부에서 패키지를 다운로드하지 않습니다.

## 실서비스 시험 계획과 승인 항목

초기 제안 범위는 **최대 60분, 워커 1개, 동시성 2, 테스트 질문 최대 10개, 작업당 최대 2회 모델 시도, 출력 최대 700 tokens**입니다. 실데이터·UtterAI 연계·외부 agent 도구는 제외합니다. 이 범위는 아직 승인된 실행 계획이 아닙니다.

모델 ID/리전/네트워크 경로를 정하지 않았으므로 USD 합계나 절감률을 꾸며내지 않습니다. 승인 전에 선택값에 맞는 총 예상 비용과 상한을 계산해야 합니다. AWS Budgets 알림은 지출을 즉시 차단하는 hard cap이 아니며, 현재 코드에도 일일 USD hard cap은 없습니다. 운영자가 60분 후 종료하거나 별도 자동 종료를 승인해야 합니다.

| 시험 | 합격 기준 | 실패 시 조치 |
|---|---|---|
| 실제 수신 10회·cold/warm | 모든 허용 요청 ACK <3초, 큐 확인 | Lambda 중지·timeout/cold start 분석. 미달이면 API 옵션·provisioned concurrency 비용 별도 검토 |
| 이벤트 중복 및 정상 재시도 | 동일 키의 모델/답글 자동 실행 1회 | 워커 중지·state 대조 |
| 모델 지연·워커 중단 | visibility 연장, 이전 owner의 새 전송 차단 | heartbeat/lease 검사 |
| Slack 429 | 결과 재사용, Retry-After 준수 | task 수·전송 제한 축소 |
| Slack 수신 후 연결 단절 / SENT 저장 실패 | 자동 중복 답글 없음, AMBIGUOUS 확인 | 수동 대조 후 복구 |
| 잘못된 큐 메시지 | 수신 8회 이후 DLQ 관찰 | IAM/redrive/retention 검사 |
| 권한 음성 시험 | 비허용 사용자 모델 호출 0, role 범위 외 API 거부 | 공개 URL 비활성화·권한 수정 |
| 모델 답변 | 사람이 10개 질문의 정확성·문서 근거·안전성 검토 | 프롬프트/검색 수정 후 재검증 |

실제 장애 유발을 운영 Slack이나 기존 UtterAI에서 수행하지 않습니다. 기간 초과, 승인 비용 예상 초과, 3초 ACK 실패, 중복 답글, 권한 누수, 예상 외 도구 실행이 보이면 중단합니다.

실행 승인에 필요한 값: 계정·리전, 모델과 ARN, 테스트 Slack workspace/channel/user·설치 권한, 기존 네트워크 경로, 로그/데이터 보존 동의, 총 비용 상한, 종료 시각·정리 범위. 자격증명은 채팅으로 전달하지 말고 승인된 로컬/클라우드 secret 경로를 사용합니다.

## 비용 계산

`월비용 = Lambda 요청·실행 + SQS API + DynamoDB 요청·저장 + worker vCPU·GB시간 + model token + secrets + logs·alarms + image/storage + 네트워크`

- FastAPI 신규 서버를 추가하면 그 서버·로드밸런서·네트워크의 유휴 비용이 증가할 수 있습니다. 기존 서버를 함께 쓰면 추가 비용은 다릅니다.
- 최소한의 비교에서는 기존 EKS 유휴 용량을 0원으로 단정하지 않습니다. 신규 EKS는 이 워커 하나에 과합니다. Fargate도 상시 유지하면 유휴 비용이 있습니다.
- long polling 20초인 유휴 슬롯 2개는 이론상 약 `2 × 86,400/20 = 8,640` ReceiveMessage/일입니다. 실제 조기 응답·재시도·복제 수에 따라 달라집니다.
- 모델 1회 비용은 `(inputTokens × inputPrice + outputTokens × outputPrice) / 1,000,000`. 실패·중단으로 최대 2회까지 발생할 수 있고, 결과 저장 이후 전송 재시도는 모델 비용을 늘리지 않습니다.
- 4,000자/6,000자는 token 수와 같지 않습니다. 고른 모델의 tokenizer 또는 usage 실측을 사용해야 합니다. 저장하는 `usage`는 SDK가 반환한 값이며 timeout으로 응답이 없으면 실제 과금을 알 수 없습니다.
- private NAT 시간 요금·전송·공인 IPv4·VPC endpoints가 작은 워커의 처리 비용보다 클 수 있어 기존 경로부터 확인합니다.

단가는 승인 직전 선택 리전에서 확인: [Fargate](https://aws.amazon.com/fargate/pricing/), [Bedrock](https://aws.amazon.com/bedrock/pricing/), [VPC/NAT](https://aws.amazon.com/vpc/pricing/). 이 작업에서 실측 비용 또는 % 절감은 주장하지 않습니다.

## 알림·장애·정리

Terraform 모듈은 DLQ visible >0, oldest queue age >180초 alarm을 만듭니다. 알림 수신자는 아직 없으므로 console alarm만으로 운영 감시가 완료됐다고 주장할 수 없습니다. 실행 로그의 `job_outcome`·`job_retry`를 수집하되 오류 세부 원인을 안전하게 분류하는 지표·ACK p95·model usage 합계·AMBIGUOUS 집계·workspace 429·실행시간 대시보드는 추가 운영 작업입니다.

AMBIGUOUS: 워커를 멈추고 마지막 전송 시점/Slack thread를 확인합니다. 기존 요청 완료 가능성이 사라질 때까지 기다린 후, 답글이 있으면 운영자가 SENT로 확정하고 없으면 명시적으로 재실행을 승인합니다. 자동 초기화·자동 redrive로 이 상태를 지우지 않습니다. FAILED도 원인·권한·비용 상한을 확인한 뒤 새 요청으로 복구합니다. 이 구현에는 수동 상태 수정 CLI를 제공하지 않으므로 오조작하기 쉬운 일괄 복구를 자동화하지 않았습니다.

종료: Slack request URL/커맨드를 비활성화 → 소비자 task 중단 → in-flight·DLQ 증거 저장 여부 결정 → 공개 URL 삭제 또는 Lambda reserved concurrency 0 → `terraform destroy` → S3 버전·ECR 이미지·secret을 정리합니다. 모듈은 데이터 보존(`prevent_destroy`)을 설정하지 않으므로 destroy 시 queue/state/log도 사라집니다. 기존 cluster/VPC·UtterAI 리소스는 이 Terraform 상태에 없으므로 삭제 대상이 아닙니다.
