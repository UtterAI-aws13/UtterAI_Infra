# Monitoring Runbook

> 팀원이 EKS 모니터링 화면을 로컬에서 확인할 때 쓰는 실행 가이드.  
> 현재 명령어 예시는 dev 기준이며, prod에서도 흐름은 동일하지만 클러스터 이름, AWS 계정/profile, 저장소 정책은 환경에 맞게 바꿔야 한다.
> 장애 원인 분석과 재발 방지 기록은 [Troubleshooting](./troubleshooting.md)에 정리한다.

## 환경별 차이

모니터링 확인 방식 자체는 dev/prod 공통이다.

```text
kubeconfig 설정
  -> kubectl로 EKS 접근 확인
  -> Grafana port-forward
  -> Prometheus Metrics 확인
  -> Loki Logs 확인
```

환경별로 달라지는 값:

| 항목 | Dev 예시 | Prod 예시 |
|---|---|---|
| EKS Cluster | `utterai-dev-eks` | `utterai-prod-eks` |
| AWS Account/Profile | dev 계정/profile | prod 계정/profile |
| Region | `ap-northeast-2` | 운영 배포 Region |
| Grafana Service | `kube-prometheus-stack-grafana` | 배포 이름에 따라 다를 수 있음 |
| Loki 저장 방식 | 임시 저장소 가능 | S3 backend 또는 영속 저장소 권장 |
| 접근 방식 | 로컬 `port-forward` | 운영 정책에 따라 VPN/SSO/Ingress/port-forward |

## 현재 구성

```text
Kubernetes Metrics/Exporters
  -> Prometheus
  -> Grafana Dashboard

Pod stdout/stderr logs
  -> Promtail
  -> Loki
  -> Grafana Explore

AWS Managed Services
  -> CloudWatch

Prometheus Alerts
  -> Alertmanager
  -> Slack

Kubernetes Cost Metrics
  -> Kubecost
  -> existing Prometheus
```

주요 네임스페이스:

| Namespace | 역할 |
|---|---|
| `monitoring` | Grafana, Prometheus, kube-state-metrics, node-exporter, Loki, Promtail |
| `kubecost` | Kubecost cost-analyzer |
| `utterai-observability` | OpenTelemetry Collector |
| `utterai-api`, `utterai-ai-*`, `utterai-batch` | 애플리케이션 워크로드 |

## 사전 조건

로컬 터미널에서 아래가 가능해야 한다.

```bash
aws sts get-caller-identity
kubectl get nodes
kubectl get pods -n monitoring
```

`kubectl get nodes`가 안 되면 먼저 kubeconfig와 EKS access entry를 확인한다.

dev 예시:

```bash
aws eks update-kubeconfig \
  --region ap-northeast-2 \
  --name utterai-dev-eks
```

prod 예시:

```bash
aws eks update-kubeconfig \
  --region ap-northeast-2 \
  --name utterai-prod-eks
```

## Grafana 접속

Grafana는 외부에 직접 노출하지 않고, 로컬에서 `port-forward`로 접속한다.

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

브라우저에서 접속:

```text
http://localhost:3000
```

기본 계정:

```text
Username: admin
Password: Kubernetes Secret 기준으로 확인
```

비밀번호 확인:

```bash
kubectl get secret -n monitoring grafana-admin-credentials \
  -o jsonpath="{.data.admin-password}" | base64 -d; echo
```

접속이 끝나면 `port-forward`를 실행한 터미널에서 `Ctrl+C`로 종료한다.

## Grafana admin credential 관리

Grafana admin credential은 Git이나 Terraform 변수에 평문으로 두지 않는다. Dev 기준 운영 흐름은 Alertmanager Slack webhook과 동일하게 AWS Secrets Manager에서 시작한다.

현재 흐름:

```text
AWS Secrets Manager
  -> External Secrets Operator
  -> Kubernetes Secret: monitoring/grafana-admin-credentials
  -> kube-prometheus-stack Grafana admin.existingSecret
```

### Secrets Manager 값 주입

먼저 Terraform이 Secrets Manager secret을 만들었는지 확인한다.

```bash
terraform -chdir=terraform/environments/dev/04-addons output \
  grafana_admin_secret_manager_name
```

Secrets Manager에는 JSON 형태로 값을 넣는다.

```bash
aws secretsmanager put-secret-value \
  --secret-id utterai-dev/grafana-admin-credentials \
  --secret-string '{"admin_user":"admin","admin_password":"<GRAFANA_ADMIN_PASSWORD>"}'
```

비밀번호는 충분히 긴 랜덤 값을 사용한다. 이 값은 Git, PR, Slack 일반 채널에 기록하지 않는다.

### Terraform 적용

Secret 값이 주입된 뒤 Grafana가 해당 Secret을 사용하도록 `04-addons`를 적용한다.

```bash
cd ~/utter-ai/UtterAI_Infra
terraform -chdir=terraform/environments/dev/04-addons plan \
  -var='alertmanager_slack_enabled=true' \
  -var='grafana_admin_credentials_enabled=true'
terraform -chdir=terraform/environments/dev/04-addons apply \
  -var='alertmanager_slack_enabled=true' \
  -var='grafana_admin_credentials_enabled=true'
```

반복 적용 시 로컬 전용 `terraform.tfvars`를 둘 수 있다. 이 파일은 Git에 커밋하지 않는다.

```bash
cp terraform/environments/dev/04-addons/terraform.tfvars.example \
  terraform/environments/dev/04-addons/terraform.tfvars
```

### 동작 확인

ExternalSecret과 Kubernetes Secret 동기화를 확인한다.

```bash
kubectl get externalsecret -n monitoring grafana-admin-credentials
kubectl get secret -n monitoring grafana-admin-credentials
```

Grafana Pod가 정상인지 확인한다.

```bash
kubectl get pods -n monitoring | grep grafana
```

Grafana admin password는 아래 Secret에서 확인한다.

```bash
kubectl get secret -n monitoring grafana-admin-credentials \
  -o jsonpath="{.data.admin-password}" | base64 -d; echo
```

### Bootstrap 주의사항

새 클러스터에서 처음 `04-addons`를 적용할 때 `monitoring` namespace가 아직 없으면 Grafana admin ExternalSecret을 먼저 만들 수 없다. 이 경우 아래 순서로 나눠서 진행한다.

```text
1. grafana_admin_credentials_enabled=false 상태로 04-addons 1차 적용
2. Secrets Manager에 Grafana admin JSON 값 주입
3. grafana_admin_credentials_enabled=true 상태로 04-addons 2차 적용
```

이미 dev처럼 `monitoring` namespace와 External Secrets Operator가 설치된 환경에서는 2차 적용부터 진행하면 된다.

## Prometheus / Metrics 확인

Grafana에서 대시보드를 확인한다.

추천 확인 항목:

| Panel | 확인 내용 |
|---|---|
| `Ready Nodes` | 현재 Ready 상태 노드 수 |
| `Pending Pods` | 스케줄링 대기 중인 Pod 수 |
| `Unschedulable Pods` | 노드 부족/taint/resource 부족 등으로 스케줄 불가 Pod |
| `Pod Restarts 15m` | 최근 재시작 여부 |
| `Node CPU / Memory Usage` | 노드별 사용률 |
| `Cluster Requested Resource Ratio` | 요청 리소스 대비 allocatable 비율 |
| `Cluster Autoscaler Activity` | CA scale-up/down 흐름 |

Prometheus 쿼리 예시:

```promql
up
kube_node_status_condition{condition="Ready",status="true"}
kube_pod_status_phase{phase="Pending"}
kube_pod_container_status_restarts_total
```

OpenTelemetry Collector scrape 여부:

```promql
up{job=~".*otel.*"}
```

## Kubecost / Cost 확인

Kubecost는 별도 Prometheus/Grafana를 띄우지 않고, `monitoring` namespace의 기존 `kube-prometheus-stack` Prometheus를 조회한다.

현재 dev 기본값:

| 항목 | 값 | 이유 |
|---|---|---|
| Chart | `cost-analyzer` `2.8.6` | 2026-06-17 기준 chart repo 최신 안정 항목 (2.9.x는 3.0 마이그레이션 전용 버전) |
| Namespace | `kubecost` | 모니터링 시스템과 비용 UI 분리 |
| Prometheus | `utterai-monitoring-prometheus.monitoring.svc.cluster.local:9090` | 기존 Prometheus 재사용 |
| Bundled Prometheus/Grafana | disabled | 중복 리소스와 비용 방지 |
| PersistentVolume | dev disabled, prod enabled | dev는 EBS 비용 최소화, prod는 재시작 후 데이터 보존 |
| Network costs / Forecasting / Diagnostics | disabled | v1 비용 관측을 가볍게 시작 |

Terraform 적용:

```bash
cd ~/utter-ai/UtterAI_Infra
terraform -chdir=terraform/environments/dev/04-addons plan \
  -var='kubecost_enabled=true'
terraform -chdir=terraform/environments/dev/04-addons apply \
  -var='kubecost_enabled=true'
```

반복 적용 시 로컬 전용 `terraform.tfvars`에 유지할 수 있다.

```hcl
kubecost_enabled = true
kubecost_persistent_volume_enabled = false
```

Pod와 Service 확인:

```bash
kubectl get pods -n kubecost
kubectl get svc -n kubecost
kubectl get servicemonitor -n kubecost
```

Kubecost UI는 외부에 직접 노출하지 않고 로컬 `port-forward`로 접속한다.

```bash
kubectl port-forward -n kubecost svc/kubecost 9090:9090
```

브라우저에서 접속:

```text
http://localhost:9090
```

Prometheus scrape 확인:

```promql
up{namespace="kubecost"}
kubecost_cluster_info
node_total_hourly_cost
```

`node_total_hourly_cost`가 바로 보이지 않으면 Kubecost가 초기 ETL을 끝낼 때까지 몇 분 기다린 뒤 다시 확인한다.

### Kubecost 비용 주의사항

Kubecost 자체도 Pod 리소스를 사용한다. dev에서는 아래 설정으로 시작한다.

```text
kubecost_persistent_volume_enabled = false
networkCosts.enabled = false
forecasting.enabled = false
diagnostics.enabled = false
```

더 정확한 네트워크 비용, 장기 비용 추이, 재시작 후 비용 데이터 보존이 필요해지면 순서대로 켠다.

| 단계 | 켜는 값 | 비용/영향 |
|---|---|---|
| 비용 데이터 보존 | `kubecost_persistent_volume_enabled=true` | EBS PVC 비용 추가 |
| 네트워크 비용 | `networkCosts.enabled=true` | DaemonSet 리소스 추가 |
| 예측 | `forecasting.enabled=true` | 모델링 Pod 리소스 추가 |

## Alertmanager / Slack 알림 확인

Alertmanager는 Prometheus alert를 받아 Slack으로 전달한다. Dev 기준 Slack webhook URL은 Git에 기록하지 않고 AWS Secrets Manager에만 저장한다.

현재 흐름:

```text
AWS Secrets Manager
  -> External Secrets Operator
  -> Kubernetes Secret: monitoring/alertmanager-slack-webhook
  -> Alertmanager Secret mount
  -> Slack receiver
```

### Terraform 적용

Slack 알림을 켠 상태로 `04-addons`를 적용한다.

```bash
cd ~/utter-ai/UtterAI_Infra
terraform -chdir=terraform/environments/dev/04-addons plan \
  -var='alertmanager_slack_enabled=true'
terraform -chdir=terraform/environments/dev/04-addons apply \
  -var='alertmanager_slack_enabled=true'
```

반복 적용 시 `-var`를 빼먹지 않도록 로컬에 `terraform.tfvars`를 둘 수 있다. 이 파일은 Git에 커밋하지 않는다.

```bash
cp terraform/environments/dev/04-addons/terraform.tfvars.example \
  terraform/environments/dev/04-addons/terraform.tfvars
```

### Slack webhook Secret 주입

`terraform output`으로 Secrets Manager 이름을 확인한다.

```bash
terraform -chdir=terraform/environments/dev/04-addons output \
  alertmanager_slack_secret_manager_name
```

Slack webhook URL은 Secrets Manager에만 넣는다.

```bash
aws secretsmanager put-secret-value \
  --secret-id utterai-dev/alertmanager-slack-webhook \
  --secret-string '<SLACK_WEBHOOK_URL>'
```

### 동작 확인

ExternalSecret과 Kubernetes Secret 동기화를 확인한다.

```bash
kubectl get externalsecret -n monitoring alertmanager-slack-webhook
kubectl get secret -n monitoring alertmanager-slack-webhook
```

Alertmanager reconciliation 상태를 확인한다.

```bash
kubectl get alertmanager -n monitoring
```

정상 기준:

```text
READY       1
RECONCILED  True
AVAILABLE   True
```

실제 Alertmanager 라우팅 설정을 확인한다.

```bash
kubectl get secret -n monitoring alertmanager-utterai-monitoring-alertmanager \
  -o go-template='{{index .data "alertmanager.yaml" | base64decode}}'
```

정상 설정에는 아래 내용이 보여야 한다.

```text
receiver: slack
receivers:
- name: slack
- name: "null"
routes:
- receiver: "null"
  matchers:
  - alertname = "Watchdog"
```

### EKS control plane 알림 정책

EKS는 `kube-scheduler`, `kube-controller-manager`, `etcd`를 AWS managed control plane에서 운영한다. 클러스터 내부 Prometheus가 해당 컴포넌트를 직접 scrape할 수 없으므로 아래 알림은 기본값에서 제외한다.

| Alert | 처리 |
|---|---|
| `KubeSchedulerDown` | 비활성화 |
| `KubeControllerManagerDown` | 비활성화 |
| etcd 관련 기본 rule | 비활성화 |

대신 유지하는 알림:

| 영역 | 이유 |
|---|---|
| kube-apiserver | EKS에서도 Prometheus target으로 확인 가능 |
| kubelet / cAdvisor | 노드와 Pod 리소스 확인에 필요 |
| kube-state-metrics | Kubernetes object 상태 확인에 필요 |
| node-exporter | 노드 리소스/파일시스템 확인에 필요 |

## Loki / Logs 확인

Loki와 Promtail이 적용된 상태라면 다음 리소스가 보여야 한다.

```bash
kubectl get pods -n monitoring | grep -E "loki|promtail"
kubectl get svc -n monitoring | grep loki
```

Grafana에서 확인:

1. 왼쪽 메뉴에서 `Explore` 진입
2. datasource를 `Loki`로 선택
3. 아래 LogQL 쿼리 실행

전체 로그 라벨 확인:

```logql
{namespace=~".+"}
```

특정 네임스페이스 로그:

```logql
{namespace="utterai-api"}
{namespace="utterai-ai-api"}
{namespace="utterai-ai-cpu"}
{namespace="utterai-ai-gpu"}
{namespace="utterai-batch"}
```

특정 Pod 로그:

```logql
{pod=~"utterai-api-.*"}
```

에러성 로그 검색:

```logql
{namespace=~"utterai-.*"} |= "ERROR"
```

## 현재 Loki 저장 방식

현재 dev 검증용 Loki는 임시 저장소 모드다.

```text
singleBinary.persistence.enabled = false
```

의미:

| 항목 | 설명 |
|---|---|
| 장점 | EBS PVC를 만들지 않으므로 비용/설정 부담이 작다 |
| 단점 | Loki Pod가 재시작되면 저장된 로그가 사라질 수 있다 |
| 목적 | Grafana에서 로그 수집 흐름이 되는지 확인하는 dev 검증용 |

prod 또는 장기 보관 단계에서는 S3 backend 또는 EBS PVC 기반 저장소로 전환한다.

## 자주 보는 kubectl 명령

모니터링 Pod 상태:

```bash
kubectl get pods -n monitoring
```

Grafana Service 확인:

```bash
kubectl get svc -n monitoring kube-prometheus-stack-grafana -o wide
```

Prometheus/Grafana/Loki 이벤트 확인:

```bash
kubectl get events -n monitoring --sort-by=.lastTimestamp
```

Pod 상세 확인:

```bash
kubectl describe pod -n monitoring <pod-name>
```

Pod 로그 확인:

```bash
kubectl logs -n monitoring <pod-name>
```

## 문제 해결

### Grafana port-forward가 끊기는 경우

먼저 Grafana Pod와 Service를 확인한다.

```bash
kubectl get pods -n monitoring | grep grafana
kubectl get svc -n monitoring kube-prometheus-stack-grafana -o wide
```

Service port가 `80/TCP`이면 아래처럼 연결한다.

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
```

로컬 `3000` 포트가 이미 사용 중이면 다른 포트를 사용한다.

```bash
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3001:80
```

접속 URL:

```text
http://localhost:3001
```

### Grafana 로그인이 안 되는 경우

Secret의 admin password를 다시 확인한다.

```bash
kubectl get secret -n monitoring grafana-admin-credentials \
  -o jsonpath="{.data.admin-password}" | base64 -d; echo
```

그래도 안 되면 Grafana DB 안에서 비밀번호가 별도로 변경됐을 수 있다. 이 경우 팀 내에서 현재 admin password를 공유받거나, 운영자 기준으로 reset 절차를 진행한다.

### Loki datasource가 안 보이는 경우

Terraform `04-addons`가 최신으로 적용됐는지 확인한다.

```bash
cd ~/utter-ai/UtterAI_Infra
terraform -chdir=terraform/environments/dev/04-addons plan
```

Grafana datasource 설정은 `kube-prometheus-stack` Helm values에 들어 있다.

### Loki 로그가 안 나오는 경우

Promtail이 떠 있는지 먼저 본다.

```bash
kubectl get pods -n monitoring | grep promtail
```

Promtail 로그에서 Loki push 에러가 있는지 확인한다.

```bash
kubectl logs -n monitoring <promtail-pod-name> --tail=100
```

### Alertmanager `RECONCILED=False`인 경우

Alertmanager CR 상태 메시지를 먼저 확인한다.

```bash
kubectl get alertmanager -n monitoring utterai-monitoring-alertmanager -o yaml
```

자주 보는 원인:

| 증상 | 의미 | 조치 |
|---|---|---|
| `notification config name "null" is not unique` | receiver 이름 중복 | Terraform Alertmanager config 확인 후 `04-addons` 재적용 |
| `alertmanager-slack-webhook` Secret 없음 | ESO 동기화 실패 또는 Slack Secret 미주입 | ExternalSecret, Secrets Manager 값 확인 |
| Slack 메시지가 안 옴 | receiver가 `null`이거나 webhook 값 문제 | `alertmanager_slack_enabled=true` 적용 여부와 Secret 값 확인 |

확인 명령:

```bash
kubectl get externalsecret -n monitoring alertmanager-slack-webhook
kubectl get secret -n monitoring alertmanager-slack-webhook
kubectl get alertmanager -n monitoring
```

## 종료/주의사항

`port-forward`는 로컬 터미널 프로세스라서 `Ctrl+C`로 끄면 된다.  
Grafana/Prometheus/Loki 자체는 EKS 안에서 계속 실행된다.

현재 Loki는 임시 저장소라 PVC/EBS 비용은 만들지 않는다. 다만 Pod 리소스는 기존 노드에서 사용한다.
