# ChatOps 확장 검증 (4단계)

검증일: 2026-10-27. 원시 결과: [evidence/chatops-e2e.json](../evidence/chatops-e2e.json)

## 무엇을 더했나

질의응답 봇에 "채팅에서 운영 작업을 실행하고, 그 실행을 통제하는" 기능을 붙였다.

| 기능 | 동작 |
|---|---|
| 알림 연동 | Alertmanager webhook → 알림 채널에 게시. 버튼: [상태 보기], (LLM이 재시작을 제안하면) [재시작 요청] |
| 조회 | `/ops status <대상>`: Deployment Ready 수, Pod 상태, 재시작 횟수. LLM을 거치지 않는 결정적 조회 |
| 파이프라인 한눈에 보기 | `/ops pipeline`: 음성 처리 단계(stt → analysis → report)마다 큐 대기 수, 처리 중 수, DLQ, Ready Pod, 재시작 횟수를 한 메시지로. 알림 메시지에도 같은 요약을 붙임 |
| 복구 확인 | 파이프라인 단계의 워커를 재시작하면, Pod Ready가 아니라 **그 단계의 큐가 줄어드는지**를 확인해 스레드에 "처리 재개 확인" 또는 "처리 재개 안 됨"을 남김 |
| 실행 | `/ops restart <대상>` 또는 [재시작 요청] → 요청 메시지에 [승인] [거절] → **다른 운영자**가 승인하면 rollout restart |
| LLM의 역할 | 알림 요약과 런북 근거의 조치 제안까지. 실행 권한 없음. 제안은 허용된 두 값(`restart`, `none`) 외에는 버림 |

```text
Alertmanager ─(Bearer)─┐
Slack /ops, 버튼 ─(서명)─┴→ Lambda Function URL → SQS → 워커 ─→ Kubernetes API (ServiceAccount 토큰, RBAC)
                                                        │  └→ LiteLLM(ops-advisor 키) → 런북 근거 제안
                                                        └→ DynamoDB: 작업 기록 + 실행 기록(action)
```

입력 경로(Lambda, SQS)와 중복 제거 저장소(DynamoDB)는 질의응답 봇과 같다. 새 입력 세 가지는 같은 Lambda가 받는다.

## 실행 통제 설계

| 위험 | 대응 |
|---|---|
| LLM이 잘못된 조치를 실행 | LLM은 버튼을 띄울 수만 있다. 실행은 사람이 누른 [승인]으로만 |
| 요청자 혼자 실행 | 요청자 본인의 승인은 거부. 운영자 목록(OPS_OPERATORS)에 없는 사용자도 거부 |
| 오래된 요청을 뒤늦게 승인 | 승인 가능 시간(기본 10분, 시험 20초)이 지나면 EXPIRED |
| 버튼 연타·Slack 재전송으로 두 번 실행 | 실행 기록(action)을 조건부 쓰기로 OPEN → EXECUTING으로 한 번만 전이 |
| 실행 도중 워커 종료 | 재시작 시각을 승인 순간에 고정해 저장. 대신 넘겨받은 워커가 같은 값으로 다시 보내면 Pod 템플릿이 그대로라 두 번째 롤아웃이 생기지 않음 |
| 앱 설정 실수로 허용 대상이 넓어짐 | 워커 토큰의 Role이 `api` 하나만 patch 가능. 앱 허용 목록과 별개로 Kubernetes가 막음 |
| Alertmanager 위조 요청 | Slack 서명을 쓸 수 없어 별도 Bearer 토큰(Secrets Manager) 확인 |
| 같은 알림 반복(repeat_interval, webhook 재시도) | groupKey + 상태 + (fingerprint, startsAt)로 키를 만들어 한 번만 게시. 다시 발생한 알림은 startsAt이 달라 새로 게시 |
| 모델 장애로 알림이 늦어짐 | 알림 게시는 LLM을 기다리지 않음. 모델 실패 시 요약 없이 바로 게시 |
| Pod는 살아났는데 처리가 멈춰 있음 | 재시작 뒤 같은 SQS 메시지를 일정 간격으로 다시 받아 큐 대기 수를 확인. 별도 스케줄러 없이 SQS 재전달로 구현 |

**답글 게시와 실행의 차이:** 질의응답 봇은 Slack 전송 결과를 모르면 중복 답글을 피하려고 자동 재전송을 멈춘다(AMBIGUOUS). 재시작은 같은 값으로 다시 보내도 결과가 같도록(멱등) 만들었기 때문에, 결과를 모를 때 다시 실행하는 쪽을 택한다. 메시지 갱신(`chat.update`)도 같은 내용을 덮어쓰므로 재시도한다.

## 결과

kind 로컬 클러스터의 실제 Kubernetes API 서버에 대해 실행했다. Slack은 loopback 모의 서버다.

| 시나리오 | 결과 |
|---|---|
| `/ops status api` | 0.36초 후 채널에 Ready 2/2 게시. 클러스터 변경 없음 |
| 승인 흐름 | 요청자 본인 승인 → 거부, 운영자가 아닌 사용자 승인 → 거부(generation 5 유지). 다른 운영자 승인 → generation 6, 기존 Pod 전부 교체, 요청 메시지가 "재시작 완료"로 갱신 |
| 승인 버튼 6회 동시 클릭 | generation 증가 1, 메시지 갱신 1회, 나머지 5건은 "이미 처리됨" 안내 |
| 거절 / 승인 시간 만료 | 거절 후 승인 시도, 20초 경과 후 승인 시도 모두 실행 안 됨(generation 증가 0) |
| RBAC 차단 | 앱 허용 목록에 있는 `db` 재시작을 승인 → Kubernetes가 403, "재시작 실패: k8s_forbidden", db generation 증가 0 |
| 재시작 직후 워커 강제 종료 | 패치 직후 종료(exit 1) → 대체 워커가 같은 값으로 다시 적용 → generation 7 → 8 → 8, 재시작 시각 동일, 92.7초 후 메시지 갱신 1회 |
| 알림 | 잘못된 토큰 401. 같은 알림 3회 전달 → 게시 1건(2.2초). [상태 보기] → 알림 스레드에 상태 답글 |
| 알림 → 재시작 | 재시작 제안 알림에서 [재시작 요청] → 알림 스레드에 승인 요청 → 다른 운영자 승인 → generation +1, 알림 수신부터 3.5초 |
| 모델 중단 중 알림 | 요약 없이 0.3초 만에 게시, [상태 보기] 버튼 유지 |

### 파이프라인 가시성과 복구 확인

단계별 큐는 LocalStack SQS, 단계별 워커는 kind의 Deployment다. **큐 소비는 테스트 하네스의 시뮬레이터가 대신한다.** 시뮬레이터는 "재시작하면 회복하는 멈춘 소비자"와 "재시작해도 계속 멈춘 소비자"를 흉내 낸다. 확인 간격 10초, 최대 5회로 시험했다(기본값 60초, 5회).

| 시나리오 | 결과 |
|---|---|
| analysis 단계 소비 멈춤 + `/ops pipeline` | 4.96초 후 게시. `[!] analysis: 대기 300, 처리 중 0, DLQ 0, Pod 1/1 (소비 멈춤 의심)`, 나머지 두 단계는 `[ok]` |
| 재시작하면 회복하는 소비자 | 승인 11.1초 후 "처리 재개 확인: analysis 대기 300 → 10, 롤아웃 완료" |
| 재시작해도 멈춘 소비자 | Pod는 정상으로 롤아웃됐지만 대기 300 → 300. 54초 관찰 후 "처리 재개 안 됨, 담당자 확인 필요" |
| 알림 → 복구 확인까지 | QueueConsumerStalled 알림에 파이프라인 상태가 붙고 analysis가 표시됨 → LLM이 런북에 따라 재시작 제안 → 승인 → 알림 수신 18.3초 후 같은 스레드에 "처리 재개 확인" |

두 번째와 세 번째 시나리오는 Pod 상태만 보면 똑같이 "재시작 성공"이다. 큐 대기 수를 기준으로 판단해야 둘을 구분할 수 있다.

(알림 → 재시작 흐름은 첫 실행에서 모델이 재시작을 제안하지 않아 진행되지 않았다. 해당 시나리오만 다시 실행했고, 두 번째 알림에서 재시작 제안이 나왔다. evidence의 `rerun` 항목에 기록했다.)

### LLM 제안이 런북을 따르는가

조언 모델: `quality` 별칭(qwen2.5:3b, Ollama), temperature 0. 알림 문구를 바꿔 13건 측정했다.

| 경우 | 런북의 지시 | 런북과 일치 |
|---|---|---|
| 최근 배포 없는 크래시 루프 | 재시작 요청 | 5건 중 4건 |
| 큐 적체 | 재시작하지 말 것 | 5건 중 5건 |
| **배포 직후 크래시 루프** | **재시작하지 말고 롤백 검토** | **3건 중 0건(3건 모두 재시작 제안)** |

- 재시작이 오히려 해로운 경우(배포 직후)에 모델이 일관되게 재시작을 제안했다. **LLM에게 실행 권한을 주지 않고, 제안을 사람이 승인하게 한 이유가 측정으로 확인됐다.**
- 프롬프트를 고치기 전에는 더 나빴다. llama3.2:1b는 JSON 형식을 지키지 못했다. qwen2.5:3b는 질의응답용 시스템 프롬프트("외부 도구 실행 권한은 없습니다" 포함) 아래에서 크래시 루프에 4번 중 3번 `none`을 답했다. 조언 전용 시스템 프롬프트로 바꾸자 이번에는 큐 적체에도 3번 모두 `restart`를 답했다. 런북 단계를 먼저 인용한 뒤 조치를 고르도록 형식을 바꾸고 나서 위 결과가 나왔다.
- 13건은 경향을 보는 수준의 표본이다. 정확도 수치로 일반화하면 안 된다.

## 해석할 때 주의할 점

- **EKS가 아니다.** kind의 Kubernetes API와 RBAC은 실제 구현이지만, EKS의 인증(IAM → access entry → Kubernetes 그룹)은 검증하지 않았다. 워커는 kind ServiceAccount 토큰을 파일로 받았다.
- **Slack 버튼은 모의 서버다.** 버튼 클릭 요청은 Slack과 같은 서명 방식으로 직접 만들어 보냈다. 실제 Slack 화면, Block Kit 렌더링, 상호작용 재시도 동작은 확인하지 않았다.
- **승인 만료는 클릭할 때 판정한다.** 만료된 요청 메시지는 누군가 버튼을 누를 때까지 버튼이 남아 있다. 별도 정리 작업은 만들지 않았다.
- **복구 판단 기준은 단순하다.** 큐 대기 수가 승인 시점의 절반 이하 또는 0이 되면 "재개"로 본다. 유입이 많으면 소비가 재개돼도 절반 아래로 내려가지 않을 수 있다. AWS에서는 CloudWatch의 `NumberOfMessagesDeleted`와 `ApproximateAgeOfOldestMessage`를 함께 보는 편이 정확하다.
- **확인 횟수는 SQS 수신 횟수를 쓴다.** 확인 1회가 수신 1회라, 확인 횟수(최대 6)와 처음 처리를 합쳐 DLQ 기준(maxReceiveCount 8)보다 작게 제한했다.
- **큐 소비는 시뮬레이션이다.** 실제 음성 처리 워커가 멈추고 회복하는 원인을 재현하지는 않았다. 재시작 대상과 큐 조회는 실제 Kubernetes API와 LocalStack SQS다.
- **재시작 대상이 아닌 Deployment는 복구 확인을 하지 않는다.** 파이프라인 단계로 등록한 워커만 확인한다.
- **모델 결과는 로컬 3B 모델 기준이다.** Bedrock의 큰 모델이라면 결과가 다를 수 있다. 그래도 실행 권한을 주지 않는 설계는 유지한다.

## 재현

```bash
scripts/local_up.sh --burst 5 --duplicates 2 --observe 5   # LocalStack + Terraform
scripts/kind_up.sh                                          # kind 클러스터, RBAC, 워커 토큰(별도 kubeconfig)
docker compose -f gateway/compose.yaml --profile scale up -d
.venv/bin/python gateway/setup_keys.py                      # slack-bot, batch, ops-advisor 키
.venv/bin/python e2e/run_chatops.py                         # 약 9분
.venv/bin/python e2e/run_chatops.py --only alert_flow       # 시나리오 하나만 다시 실행
kind delete cluster --name slack-ops                        # 정리
```
