# EKS 고도화 작업 진행 현황

> 최초 작성: 2026-06-24
> 원본 설계 문서: [`docs/shared/eks-advanced-operations.md`](../shared/eks-advanced-operations.md)
> **범례**: ✅ 완료 / 🔄 PR 오픈 / ⬜ 미착수 / 🖱️ UI 수동 작업

---

## 목차

1. [Phase 1 — 즉시 적용 (관찰 가능성 기반)](#1-phase-1--즉시-적용)
2. [Phase 2 — 단기 (ADOT 전환 + VOC 기반 마련)](#2-phase-2--단기)
3. [Phase 3 — 중기 (VOC 완성 + DevOps Agent)](#3-phase-3--중기)
4. [Phase 4 — 장기 (선택적)](#4-phase-4--장기)
5. [관련 PR 목록](#5-관련-pr-목록)
6. [사전 작업 — 수동 설정 항목](#6-사전-작업--수동-설정-항목)

---

## 1. Phase 1 — 즉시 적용

> 인프라 변경 최소, 1~3일 내 적용 목표

### 1-1. 앱 코드 (UtterAI_BE)

| 항목 | 파일 | 상태 | PR |
|---|---|---|---|
| 구조화 JSON 로그 + Trace ID 삽입 | `app/core/logging.py` (신규) | ✅ | [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) |
| `configure_logging()` main.py 적용 | `app/main.py` | ✅ | [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) |
| `log_level` 설정 추가 | `app/core/config.py` | ✅ | [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) |
| 비즈니스 메트릭 5종 추가 | `app/observability/metrics.py` | ✅ | [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) |
| presign URL 타이밍/카운터 계측 | `app/services/audio.py` | ✅ | [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) |
| SQS traceparent 주입 | `app/infrastructure/sqs/client.py` | ✅ | [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) |

### 1-2. 앱 코드 (UtterAI_AI)

| 항목 | 파일 | 상태 | PR |
|---|---|---|---|
| CPU Worker — SpanKind.CONSUMER + session_id/user_id | `app/workers/cpu_worker.py` | ✅ | [AI #61](https://github.com/UtterAI-aws13/UtterAI_AI/pull/61) |
| Report Worker — SpanKind.CONSUMER + session_id/job_id | `app/workers/cpu_worker.py` | ✅ | [AI #61](https://github.com/UtterAI-aws13/UtterAI_AI/pull/61) |
| GPU Worker — SpanKind.CONSUMER + session_id/job_id | `app/workers/ml_gpu_worker.py` | ✅ | [AI #61](https://github.com/UtterAI-aws13/UtterAI_AI/pull/61) |

### 1-3. 인프라 (UtterAI_Infra)

| 항목 | 파일 | 상태 | PR |
|---|---|---|---|
| OTel Collector logs pipeline → Loki 연결 | `k8s/platform/observability/base/otel-collector.yaml` | 🔄 | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| Alertmanager 룰 5종 (PrometheusRule) | `k8s/platform/observability/base/alert-rules-utterai.yaml` | 🔄 | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| Slack 알림 연동 (AlertmanagerConfig + ExternalSecret) | `k8s/platform/observability/base/alertmanager-*.yaml` | 🔄 | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| alertmanagerConfigMatcherStrategy=None | `terraform/modules/eks-addons/main.tf` | 🔄 | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| Grafana Loki Derived Field 설정 (Loki→Tempo 1클릭) | Grafana UI 수동 | 🖱️ | — |

#### Grafana Derived Field 설정 방법 (수동)

```
Grafana → Data Sources → Loki → Derived Fields → Add

Name:    TraceID
Type:    Regex
Regex:   "trace_id":"(\w+)"
URL:     http://tempo.monitoring.svc.cluster.local:3100/api/traces/${__value.raw}
Label:   Tempo에서 열기
```

> Infra PR #306 머지 후 Loki에 로그가 들어오기 시작하면 이 설정을 추가해야
> 로그에서 trace_id 클릭 → Tempo trace로 바로 이동이 가능합니다.

---

## 2. Phase 2 — 단기

> 1~2주, ADOT 전환 + VOC 기반 + ServiceMonitor

### 2-1. ADOT Operator 전환

| 항목 | 파일 | 상태 |
|---|---|---|
| ADOT Operator Helm 설치 | `terraform/modules/eks-addons/main.tf` | ⬜ |
| OpenTelemetryCollector CRD로 이관 | `k8s/platform/observability/base/otel-collector-crd.yaml` (신규) | ⬜ |
| 기존 otel-collector.yaml Deployment 제거 | `k8s/platform/observability/base/otel-collector.yaml` | ⬜ |
| Instrumentation CRD 추가 | `k8s/platform/observability/base/instrumentation-python.yaml` (신규) | ⬜ |
| 앱 ConfigMap OTLP endpoint 주소 변경 | `k8s/apps/*/overlays/prod/patch-configmap.yaml` | ⬜ |

### 2-2. Prometheus 메트릭 수집

| 항목 | 파일 | 상태 |
|---|---|---|
| BE + AI Worker ServiceMonitor 추가 | `k8s/platform/observability/base/service-monitor-utterai.yaml` (신규) | ⬜ |
| Grafana 대시보드 코드화 (4개 Row) | `k8s/platform/observability/base/grafana-dashboard-utterai.yaml` (신규) | ⬜ |

---

## 3. Phase 3 — 중기

> 1~2개월, VOC 완성 + AI 기반 장애 자동 분석

### 3-1. VOC 데이터 흐름 추적 (OpenSearch)

| 항목 | 파일 | 상태 |
|---|---|---|
| AWS OpenSearch 도메인 생성 | `terraform/modules/opensearch/main.tf` (신규 모듈) | ⬜ |
| OpenSearch 보안 그룹 + IAM Policy | `terraform/modules/opensearch/main.tf` | ⬜ |
| Fluent Bit IRSA Role 추가 | `terraform/modules/irsa/main.tf` | ⬜ |
| Fluent Bit DaemonSet 배포 (utterai 앱 로그 → OpenSearch) | `k8s/platform/observability/base/fluent-bit-opensearch.yaml` (신규) | ⬜ |
| prod overlay patch (IRSA ARN, endpoint) | `k8s/platform/observability/overlays/prod/patch-fluent-bit.yaml` (신규) | ⬜ |
| OpenSearch 인덱스 템플릿 + ILM 90일 설정 | OpenSearch Dashboard Dev Tools (1회) | ⬜ |
| OpenSearch Dashboard 3종 뷰 구성 | OpenSearch 콘솔 | ⬜ |

### 3-2. Auto-instrumentation 어노테이션 적용

| 항목 | 파일 | 상태 |
|---|---|---|
| BE Deployment에 instrumentation 어노테이션 추가 | `k8s/apps/backend/base/deployment-*.yaml` | ⬜ |
| AI Worker Deployment에 어노테이션 추가 | `k8s/apps/ai-worker/base/*-deployment.yaml` | ⬜ |

### 3-3. AI 기반 장애 자동 분석 (DevOps Agent Operator)

| 항목 | 파일 | 상태 |
|---|---|---|
| S3 incidents 버킷 추가 (90일 보존) | `terraform/modules/s3/main.tf` | ⬜ |
| DevOps Agent Operator IRSA Role | `terraform/modules/irsa/main.tf` | ⬜ |
| EKS 노드 Role에 SSM 정책 추가 | `terraform/modules/eks/main.tf` | ⬜ |
| Webhook Secret 생성 | AWS Secrets Manager (CLI 1회) | ⬜ |
| Operator Namespace + RBAC + Deployment | `k8s/platform/devops-agent-operator/` (신규 디렉토리) | ⬜ |
| prod overlay (IRSA ARN, cluster name 등) | `k8s/platform/devops-agent-operator/overlays/prod/` | ⬜ |
| DevOps Agent Space + Runbook 3종 등록 | AWS 콘솔 (수동) | ⬜ |

---

## 4. Phase 4 — 장기

> 필요 시 검토, 조건부 적용

| 항목 | 조건 |
|---|---|
| 관리자 페이지 세션 추적 탭 | OpenSearch Dashboard 안정화 후 |
| VOC 티켓 → session_id 자동 연결 | 사용자 신고 시스템 구축 후 |
| X-Ray 전환 (Tempo 대체) | ADOT 전환 완료 후 AWS 통합 트레이싱 필요 시 |
| CloudWatch Logs 전환 (Loki 대체) | Loki S3 운영 비용 > CloudWatch 비용인 경우 |
| DevOps Agent MCP 서버 연동 | 인시던트 패턴 DB화 후 선제 대응 필요 시 |

---

## 5. 관련 PR 목록

| PR | 레포 | 내용 | 상태 |
|---|---|---|---|
| [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) | UtterAI_BE | Phase 1 앱 — 구조화 로그, 비즈니스 메트릭, SQS traceparent | ✅ 머지 (2026-06-24) |
| [AI #61](https://github.com/UtterAI-aws13/UtterAI_AI/pull/61) | UtterAI_AI | Phase 1 앱 — SpanKind.CONSUMER, session/job 속성 | ✅ 머지 (2026-06-24) |
| [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) | UtterAI_Infra | Phase 1 인프라 — OTel→Loki, Alertmanager 룰, Slack 알림 | 🔄 리뷰 중 |

---

## 6. 사전 작업 — 수동 설정 항목

Infra PR #306 **머지 전** 완료 필요:

### Slack Webhook Secret 등록

```bash
aws secretsmanager create-secret \
  --name "utterai-prod/alertmanager-slack" \
  --secret-string '{"webhook_url":"https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK"}' \
  --region ap-northeast-2
```

### PR #306 머지 후 순서

```
1. terraform apply (eks-addons 모듈)
   → kube-prometheus-stack Alertmanager 재시작
   → alertmanagerConfigMatcherStrategy=None 적용

2. ArgoCD sync (utterai-observability app)
   → ExternalSecret 생성 → K8s Secret 생성 확인
     kubectl get secret alertmanager-slack-secret -n monitoring
   → PrometheusRule 적용 확인
     kubectl get prometheusrule -n utterai-observability
   → AlertmanagerConfig 적용 확인
     kubectl get alertmanagerconfig -n monitoring

3. Loki 로그 수신 확인 (Grafana)
   {service_name="backend", environment="prod"} 쿼리

4. Grafana Derived Field 설정 (수동, §1-3 참고)
```
