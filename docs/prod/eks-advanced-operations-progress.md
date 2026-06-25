# EKS 고도화 작업 진행 현황

> 최초 작성: 2026-06-24
> 원본 설계 문서: [`docs/shared/eks-advanced-operations.md`](../shared/eks-advanced-operations.md)
> **범례**: ✅ 완료 / 🔄 PR 오픈 / ⬜ 미착수 / 🖱️ UI 수동 작업 / ➖ 미사용

---

## 방향성 요약

| 레이어 | 담당 | 구성 |
|---|---|---|
| **인프라 모니터링** | 옵저빌리티/인프라팀 | OTel Collector, Loki, Tempo, Prometheus, Alertmanager 파이프라인 운영 |
| **애플리케이션 트레이싱** | 앱팀 | span attribute 설계, Grafana 대시보드/서비스맵 구성 |

VoC 발생 시: Grafana → Service Graph에서 문제 서비스 확인 → Tempo trace → Loki 로그. kubectl 진입 없이 원인 파악.

> **ADOT 전환 없음**: 현재 vanilla OTel Collector contrib이 모든 기능(servicegraph, Loki, Tempo)을 지원. 운영 중인 prod 파이프라인 교체 리스크 대비 기능적 이득 없음.

---

## 목차

1. [Phase 1 — 즉시 적용 (관찰 가능성 기반)](#1-phase-1--즉시-적용)
2. [Phase 2 — 단기 (Service Map + 앱 메트릭 수집)](#2-phase-2--단기)
3. [Phase 3 — 중기 (VOC 완성 + DevOps Agent)](#3-phase-3--중기)
4. [Phase 4 — 장기 (선택적)](#4-phase-4--장기)
5. [관련 PR 목록](#5-관련-pr-목록)
6. [수동 설정 항목](#6-수동-설정-항목)

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
| OTel Collector logs pipeline → Loki 연결 | `k8s/platform/observability/base/otel-collector.yaml` | ✅ | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| Alertmanager 룰 5종 (PrometheusRule) | `k8s/platform/observability/base/alert-rules-utterai.yaml` | ✅ | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| alertmanagerConfigMatcherStrategy=None | `terraform/modules/eks-addons/main.tf` | ✅ | [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) |
| Slack 알림 연동 | — | ➖ | Slack 미사용 — kustomization에서 비활성화 |
| Grafana Loki Derived Field 설정 (Loki→Tempo 1클릭) | Grafana UI 수동 | 🖱️ | — |

#### Grafana Derived Field 설정 방법 (수동)

```
Grafana → Data Sources → Loki → Derived Fields → Add

Name:    TraceID
Type:    Regex
Regex:   "trace_id":"(\w+)"
Internal Link: ON → Tempo 데이터소스 선택
Query:   ${__value.raw}
```

---

## 2. Phase 2 — 단기

> 1~2주. 현재 OTel Collector 그대로 유지하며 Service Map + 앱 메트릭 수집 추가.

### 2-1. OTel Collector — servicegraph connector 추가

Service Graph (Node Graph) 활성화. BE→CPU→GPU 서비스 간 호출 관계, 지연, 에러율을 Grafana에서 시각화.

| 항목 | 파일 | 상태 |
|---|---|---|
| servicegraph connector + metrics/servicegraph 파이프라인 추가 | `k8s/platform/observability/base/otel-collector.yaml` | ✅ |

```
Grafana → Explore → Tempo → Service Graph 탭
→ utterai-be → utterai-cpu-worker → utterai-gpu-worker 노드 그래프
```

| 항목 | 파일 | 상태 |
|---|---|---|
| Tempo 데이터소스 serviceMap + nodeGraph + tracesToLogs 설정 | `terraform/modules/eks-addons/main.tf` | ✅ |

> Terraform 변경 후 `terraform apply` 필요 (eks-addons 모듈).

### 2-2. ServiceMonitor — 앱 메트릭 수집

`serviceMonitorSelectorNilUsesHelmValues = false` + `serviceMonitorNamespaceSelector = {}` 설정으로 Prometheus가 전 네임스페이스 ServiceMonitor를 자동 수집 중.
OTel Collector ServiceMonitor(`utterai-observability`)가 이미 활성이므로 BE/Worker OTLP 메트릭은 Prometheus에서 조회 가능.
Worker는 HTTP /metrics 미노출 → per-pod ServiceMonitor 실효 없음.

| 항목 | 파일 | 상태 |
|---|---|---|
| OTel Collector ServiceMonitor (기존) | `k8s/platform/observability/base/otel-collector.yaml` | ✅ |
| BE /metrics 노출 (prometheus-fastapi-instrumentator) | `UtterAI_BE` 앱 코드 | ⬜ 앱팀 작업 |

### 2-3. Grafana 대시보드 코드화

ConfigMap으로 대시보드를 코드화하여 ArgoCD 관리. 재현 가능한 구성.

| 항목 | 파일 | 상태 |
|---|---|---|
| UtterAI Service Overview 대시보드 (4개 Row) | `k8s/platform/observability/base/grafana-dashboard-utterai.yaml` (신규) | ✅ |

```
Row 1: API Health       — TPS, p50/p95/p99 응답시간, 5xx 에러율, Pod 수
Row 2: Audio Pipeline   — presign 성공/실패, CPU/GPU SQS 큐 depth
Row 3: GPU Inference    — Worker Pod 수 (KEDA 현황), 처리량, OOMKilled 횟수
Row 4: Infrastructure   — Karpenter NodeClaim 상태, 노드 CPU/메모리
```

---

## 3. Phase 3 — 중기

> 1~2개월. VoC 완성 + AI 기반 장애 자동 분석.

### 3-1. VOC 데이터 흐름 추적 (OpenSearch)

session_id 기반으로 BE → CPU Worker → GPU Worker 전체 처리 흐름을 타임라인으로 조회.

| 항목 | 파일 | 상태 |
|---|---|---|
| AWS OpenSearch 도메인 생성 | `terraform/modules/opensearch/main.tf` (신규 모듈) | ⬜ |
| OpenSearch 보안 그룹 + IAM Policy | `terraform/modules/opensearch/main.tf` | ⬜ |
| Fluent Bit IRSA Role 추가 | `terraform/modules/irsa/main.tf` | ⬜ |
| Fluent Bit DaemonSet (utterai 앱 로그 → OpenSearch) | `k8s/platform/observability/base/fluent-bit-opensearch.yaml` (신규) | ⬜ |
| prod overlay patch (IRSA ARN, endpoint) | `k8s/platform/observability/overlays/prod/patch-fluent-bit.yaml` (신규) | ⬜ |
| OpenSearch 인덱스 템플릿 + ILM 90일 설정 | OpenSearch Dashboard Dev Tools (1회) | ⬜ |
| OpenSearch Dashboard 3종 뷰 구성 | OpenSearch 콘솔 | ⬜ |

**VoC 대응 흐름 (완성 후):**
```
고객 불만 접수 → session_id 확인
→ OpenSearch: session_id 검색 → 전체 처리 타임라인 (어느 단계에서 실패했는지)
→ Grafana Tempo: trace_id 검색 → 해당 span waterfall (정확한 지연 위치)
→ Grafana Loki: 해당 시간대 로그 → 에러 메시지 확인
```

### 3-2. AI 기반 장애 자동 분석 (DevOps Agent Operator)

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

## 4. Phase 4 — 장기 (선택적)

| 항목 | 조건 |
|---|---|
| ADOT Operator 전환 | AWS 관리형 수명주기 필요 시, 현재 vanilla OTel Collector로 충분 |
| X-Ray 전환 (Tempo 대체) | ADOT 전환 후 AWS 통합 트레이싱 필요 시 |
| CloudWatch Logs 전환 (Loki 대체) | Loki S3 운영 비용 > CloudWatch 비용인 경우 |
| 관리자 페이지 세션 추적 탭 | OpenSearch Dashboard 안정화 후 |
| VOC 티켓 → session_id 자동 연결 | 사용자 신고 시스템 구축 후 |
| DevOps Agent MCP 서버 연동 | 인시던트 패턴 DB화 후 선제 대응 필요 시 |

---

## 5. 관련 PR 목록

| PR | 레포 | 내용 | 상태 |
|---|---|---|---|
| [BE #80](https://github.com/UtterAI-aws13/UtterAI_BE/pull/80) | UtterAI_BE | Phase 1 앱 — 구조화 로그, 비즈니스 메트릭, SQS traceparent | ✅ 머지 (2026-06-24) |
| [AI #61](https://github.com/UtterAI-aws13/UtterAI_AI/pull/61) | UtterAI_AI | Phase 1 앱 — SpanKind.CONSUMER, session/job 속성 | ✅ 머지 (2026-06-24) |
| [Infra #306](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/306) | UtterAI_Infra | Phase 1 인프라 — OTel→Loki, Alertmanager 룰 | ✅ 머지 (2026-06-24) |
| [Infra #308](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/308) | UtterAI_Infra | Phase 2 방향 수정 — ADOT 제거, Slack 비활성화 | ✅ 머지 (2026-06-24) |
| [Infra #309](https://github.com/UtterAI-aws13/UtterAI_Infra/pull/309) | UtterAI_Infra | Phase 2 — servicegraph connector, Grafana 대시보드, Tempo 연결 | ✅ 머지 (2026-06-25) |

---

## 6. 수동 설정 항목

### terraform apply 필요 (Phase 1 완료 후)

```bash
# eks-addons 모듈 — alertmanagerConfigMatcherStrategy=None 적용
cd terraform/environments/prod/02-eks-addons
terraform apply
```

### Grafana Loki Derived Field (Phase 1 완료 후)

```
Grafana → Data Sources → Loki → Derived Fields
Name: TraceID / Regex: "trace_id":"(\w+)" / Internal Link: Tempo
```

### Grafana Tempo → Loki 연결 설정

```
Grafana → Data Sources → Tempo → Trace to logs
Data source: Loki / Tags: service.name / Mapped tags: service.name → service_name
Filter by trace ID: ON
```
