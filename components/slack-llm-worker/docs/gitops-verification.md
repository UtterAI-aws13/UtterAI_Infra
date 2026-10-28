# GitOps 되돌리기 검증 (5단계)

검증일: 2026-10-28. 원시 결과: [evidence/gitops-e2e.json](../evidence/gitops-e2e.json)

## 왜 바꿨나

4단계 ChatOps는 봇이 `kubectl patch`로 Deployment를 직접 재시작했다. 실제 UtterAI 알림 기록과 맞춰 보니 맞지 않았다.

- 6/26~7/6 Discord 알림 기록의 실제 장애 6~7건 중 **재시작으로 해결된 것은 없었다.** 6/30 batch-worker CrashLoopBackOff(2시간 5분)는 직전에 배포된 이미지 문제였고 다음 이미지 배포로 해결됐다. cpu-worker Pending은 Karpenter NodePool 설정 수정 커밋으로 해결됐다.
- UtterAI는 ArgoCD로 배포한다. 클러스터를 직접 바꾸면 Git과 어긋나고, 실제 해결책도 모두 Git 변경이었다.

그래서 봇의 쓰기 경로를 **배포 저장소의 Pull Request 하나**로 바꿨다. 봇은 클러스터를 읽기만 한다.

## 흐름

```text
CI 커밋(이미지 태그 변경) → Argo CD 동기화 → Pod CrashLoopBackOff
→ Alertmanager 형식 알림 → Lambda(/alertmanager) → SQS → 워커
   근거 수집(결정적 조회): Pod 상태·종료 코드, Warning 이벤트, 배포 저장소의 최근 태그 변경, Argo CD 상태
   규칙: 크래시 알림 + 알림 2시간 이내 태그 변경 + 실패 Pod가 그 태그 → [되돌리기 PR 만들기] 버튼
→ 운영자 클릭 → 직전 재확인 → 봇이 rollback 브랜치·커밋·PR 생성, 필수 상태 검사 success
→ 다른 사람이 리뷰·승인·merge (봇은 merge 불가)
→ Gitea webhook → Lambda(/git, HMAC 서명) → SQS → 워커
→ Argo CD Synced/Healthy, 새 이미지 Pod Ready, 재시작 0회를 확인해 Slack 스레드에 결과
```

| 구성 | 로컬 | 실무 대응 |
|---|---|---|
| 배포 저장소 | Gitea 1.22 (main 보호: 승인 1, 필수 상태 검사, 최신 브랜치 요구) | GitHub(같은 API 형태, `X-Hub-Signature-256` 서명) |
| 배포 | kind의 Argo CD v2.14 core, 자동 동기화, 15초 폴링 | 기존 Argo CD. webhook으로 즉시 동기화 가능 |
| 봇 권한 | 클러스터: 읽기 전용 ClusterRole. 저장소: 쓰기 토큰이지만 main 직접 push 불가, 본인 PR 승인 불가 | EKS access entry(읽기) + GitHub App(브랜치·PR·상태만) |

## 실행 통제

| 위험 | 대응 |
|---|---|
| LLM이 잘못된 조치를 제안 | 되돌리기 제안 여부는 규칙으로 결정. LLM은 수집한 근거를 요약만 함 |
| 봇이 혼자 배포를 바꿈 | 봇은 PR만 연다. main 보호 규칙상 다른 사람의 승인 없이 merge할 수 없음 |
| 버튼 연타로 PR이 여러 개 | 진단한 커밋별 실행 기록과 브랜치 하나. Git 작업은 모두 다시 해도 같은 결과(이미 있으면 재사용) |
| 알림 뒤 누가 이미 고침 | 클릭 직전에 배포 브랜치를 다시 읽어, 진단한 커밋이 최신이 아니면 PR을 만들지 않음 |
| PR 대기 중 새 배포 | push webhook을 받으면 필수 상태 검사를 failure로 바꿔 merge를 막고 Slack에 보류 알림 |
| 늦게 처리된 클릭이 다른 장애에 작용 | 버튼 값에 진단한 커밋을 넣어, 그 커밋에 대해서만 동작 |
| merge됐지만 복구 안 됨 | Pod Ready만이 아니라 Argo CD 상태, 새 이미지 Pod 비율, 재시작 횟수까지 확인. 실패하면 "복구 안 됨"으로 알림 |

## 결과

kind 클러스터, Argo CD, Gitea, LocalStack, 모의 Slack에서 실행했다. 장애는 6/30 batch-worker 사례를 본떴다. 정상 이미지 `prod-aa314fc`를 시작하자마자 종료하는 이미지 `prod-6e134f1`로 바꾼다.

| 시나리오 | 결과 |
|---|---|
| 배포 원인 크래시 | 알림 메시지의 근거: "17:12 이미지 prod-aa314fc → prod-6e134f1 (ci 커밋), 알림 0분 전", "CrashLoopBackOff, 마지막 종료 exit 1(Error)", "BackOff 이벤트", "Argo CD Synced/Progressing". 되돌리기 버튼 표시 |
| 버튼 3회 동시 클릭(운영자 2명) | PR 1개, 스레드 공지 1개, 나머지 2건은 본인에게만 "이미 처리됨" 안내 |
| PR 내용 | 작성자 봇, 태그를 `prod-aa314fc`로 되돌리는 커밋 하나, 필수 상태 검사 success |
| 봇 토큰으로 merge 시도 | **HTTP 405 거부**(승인 없음) |
| 다른 사람이 승인 후 merge | 성공. 30초 뒤 스레드에 "복구 확인: Argo CD Synced/Healthy, Pod 2/2 Ready, 이미지 prod-aa314fc, 새 Pod 재시작 0회" |
| 알림과 클릭 사이에 hotfix 배포 | PR을 만들지 않음. "진단 이후 배포 브랜치가 바뀌었습니다" 안내 |
| PR 대기 중 새 배포 | 1.4초 만에 필수 상태 검사 failure, Slack 스레드에 보류 알림, 승인 후 merge 시도도 405로 차단 |
| 노드 부족 Pending(cpu-worker 사례) | 근거만 표시: "FailedScheduling: 0/1 nodes are available: 1 Insufficient cpu". 되돌리기 제안 없음 |

### 검증하다 찾은 문제

1. **늦게 처리된 클릭이 다른 장애에 PR을 열었다.** 동시 클릭 중 충돌로 재시도된 클릭이 다음 시나리오가 새로 만든 장애 상황에서 처리됐고, 그 시점 배포 브랜치 기준으로 "진단이 유효하다"고 판단했다. 버튼이 진단한 커밋에 묶여 있지 않은 것이 원인이었다. 버튼 값에 커밋을 넣어 고쳤다.
2. **복구됐는데 "복구 안 됨"으로 판정했다.** 같은 빌드에 붙은 두 태그 중 Kubernetes가 상태(`status.image`)에 다른 태그 이름을 보고했다. Pod spec의 이미지로 판정하도록 고쳤다.
3. **Rolling update 중에는 기존 Pod가 Ready로 남는다.** Deployment의 readyReplicas(2/2)는 정상처럼 보였다. 근거에는 실제 Pod 수 기준(2/3 Ready)으로 표시하고, 되돌리기 규칙도 "실패 중인 Pod가 새 태그인가"로 바꿨다.
4. **봇 토큰에 PR 댓글 권한이 없어** 보류 처리 중간에 멈췄다. 권한을 추가하고, 댓글이 실패해도 merge 차단과 Slack 알림은 진행되게 했다.

### LLM 요약의 한계

요약은 대체로 맞았지만 근거에 없는 원인 후보를 섞었다. 크래시 건에는 "Pod의 리소스 배분 문제"를, Pending 건에는 "Pod이 재시작되었습니다"를 넣었다(실제로는 재시작 0회). **판단을 규칙에 두고 LLM을 요약에만 쓴 이유**가 여기서도 확인된다. 메시지에는 "검증 필요"를 붙였다.

## 해석할 때 주의할 점

- **EKS와 GitHub가 아니다.** Argo CD와 Kubernetes RBAC은 실제 구현이지만 EKS 인증, GitHub App 권한, GitHub branch protection은 검증하지 않았다.
- **알림은 테스트가 보낸다.** Prometheus 규칙 대신, 같은 라벨과 주석을 가진 Alertmanager 형식 요청을 크래시를 확인한 뒤 보냈다.
- **수치는 랩 기준이다.** Argo CD 폴링을 15초로 줄였고 사람의 리뷰 시간이 없다. 응답 시간을 성능 지표로 쓰지 않는다.
- **장애 원인 단서는 배포 이력 하나에 기대고 있다.** 설정·인프라 변경이 원인이면 근거만 보여 주고 되돌리기는 제안하지 않는다.

## 재현

```bash
scripts/local_up.sh --burst 5 --duplicates 2 --observe 5
scripts/kind_up.sh
scripts/gitops_up.sh          # Argo CD core, Gitea, 이미지 3개, 배포 저장소, webhook
docker compose -f gateway/compose.yaml --profile scale up -d && .venv/bin/python gateway/setup_keys.py
.venv/bin/python e2e/run_gitops.py                     # 약 12분
.venv/bin/python e2e/run_gitops.py --only stale_after_pr
```
