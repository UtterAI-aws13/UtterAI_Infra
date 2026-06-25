# 앱 레벨 옵저빌리티 구현 계획

## 배경

### 목적

VOC 발생 시 인프라 로그를 직접 뒤지지 않고, 커스텀 UI에서 어느 서비스/단계에서 문제가 생겼는지 빠르게 파악한다.

### 현재 문제

- Grafana Service Graph: BE → SQS → Workers 비동기 구조에서 구조적으로 동작하지 않음 (CLIENT↔SERVER 스팬 TTL 매칭 불가)
- 인프라 레벨(pod CPU/메모리)과 앱 레벨(서비스 간 흐름, trace) 시각화가 분리되어 있지 않음

### Phase 1 완료 (현재 운영 중)

spanmetrics connector + Grafana "UtterAI 파이프라인 모니터" 대시보드

- 서비스별 에러율 / 처리 지연 (p50/p99) — Prometheus 기반
- Span 단위 에러 Top 10 / 느린 Span Top 10
- Loki 에러 로그, Tempo 에러 트레이스 목록

서비스별 에러율은 확인 가능하나, 서비스 간 흐름을 토폴로지로 시각화하는 Service Map이 없고 VOC 대응에 특화된 UI가 없다.

---

## Phase 2 목표

VOC 전용 커스텀 UI를 새로 개발한다. OpenSearch를 데이터 저장소로 사용하고, React + TypeScript로 목적에 맞게 설계된 UI를 제공한다.

---

## 역할 분리 원칙

| 레벨 | 스택 | 용도 |
|------|------|------|
| 인프라 레벨 | Prometheus + Grafana | pod CPU/메모리, node 상태, 인프라 알람 |
| 앱 레벨 | OpenSearch + 커스텀 UI | Service Map, Trace 상세, 에러 통계 (VOC 대응) |

Tempo / Loki는 인프라 레벨로 병행 유지. OpenSearch Dashboards는 사용하지 않는다.

---

## 아키텍처

```
앱 (backend / cpu-worker / gpu-worker)
  │
  ▼ OTLP
OTel Collector
  ├── traces → Tempo           (기존 유지)
  ├── traces → Data Prepper → OpenSearch   (신규)
  ├── logs   → Loki            (기존 유지)
  └── metrics → Prometheus     (기존 유지)

OpenSearch (데이터 저장소)
  └── REST API ← Observability UI (React + TypeScript)
```

---

## 커스텀 UI 구성

### 기술 스택

| 항목 | 선택 | 이유 |
|------|------|------|
| 프레임워크 | React + TypeScript | |
| 그래프 시각화 | Cytoscape.js 또는 D3.js | Service Map 토폴로지 렌더링 |
| 데이터 소스 | OpenSearch REST API 직접 쿼리 | |
| 배포 | EKS Deployment + ClusterIP | 내부 전용, port-forward로 접근 |

### 1차 구현 범위 (VOC 대응 핵심)

| 화면 | 내용 |
|------|------|
| **Service Map** | backend → cpu-worker → gpu-worker 토폴로지 그래프. 노드 색상으로 에러율 표시. 클릭 시 해당 서비스의 최근 에러 trace 목록 |
| **Trace 상세 뷰** | 특정 trace_id의 end-to-end 스팬 타임라인 (Gantt 형식). 어느 단계에서 얼마나 걸렸는지 시각화 |
| **에러 통계** | 서비스별 에러율, p99 지연 시간. 시간 범위 필터 |

### 2차 확장 (추후)

- 로그 검색 (trace_id로 연관 로그 조회)
- 알람 히스토리
- 특정 사용자/세션 추적

---

## 인프라 구성 컴포넌트

| 컴포넌트 | 이미지 | 리소스 | 비고 |
|----------|--------|--------|------|
| OpenSearch | `opensearchproject/opensearch:2.13.0` | 2GB memory, 20Gi PVC (gp2) | single-node, security 비활성화 |
| Data Prepper | `opensearchproject/data-prepper:2.8.0` | 512MB memory | OTel trace → OpenSearch 변환 |
| observability-ui | 별도 빌드 이미지 | 256MB memory | React 앱, 내부 접근 전용 |

OpenSearch Dashboards는 배포하지 않는다.

---

## 구현 순서

### Step 1: OpenSearch + Data Prepper 배포 (인프라 PR)

파일: `k8s/platform/observability/base/opensearch.yaml`

- OpenSearch StatefulSet (single-node, `DISABLE_SECURITY_PLUGIN: true`)
- PVC: gp2 20Gi
- initContainer: `sysctl -w vm.max_map_count=262144` (OpenSearch 필수 커널 파라미터)
- ClusterIP Service

파일: `k8s/platform/observability/base/data-prepper.yaml`

Data Prepper 파이프라인:
```
entry-pipeline (otel_trace_source:21890)
  ├── raw-trace-pipeline   → opensearch (index_type: trace-analytics-raw)
  └── service-map-pipeline → opensearch (index_type: trace-analytics-service-map)
```

`service_map_stateful` processor가 SQS 비동기 흐름을 포함한 서비스 간 토폴로지를 생성한다.

### Step 2: OTel Collector 파이프라인 추가 (인프라 PR, Step 1과 동일 PR 가능)

파일: `k8s/platform/observability/base/otel-collector.yaml`

```yaml
exporters:
  otlp/data-prepper:
    endpoint: data-prepper.utterai-observability.svc.cluster.local:21890
    tls:
      insecure: true
service:
  pipelines:
    traces:
      exporters: [otlp/tempo, servicegraph, spanmetrics, otlp/data-prepper]
```

### Step 3: observability-ui 앱 개발 (별도 repo)

- React + TypeScript 프로젝트 초기화
- OpenSearch REST API 연동 (`/otel-v1-apm-span-*`, `otel-v1-apm-service-map` index)
- Service Map → Trace 상세 뷰 드릴다운 구현

### Step 4: observability-ui EKS 배포 (인프라 PR)

파일: `k8s/platform/observability/base/observability-ui.yaml`

- Deployment + ClusterIP Service
- 접근: `kubectl port-forward svc/observability-ui -n utterai-observability 3000:80`

---

## 검증 계획

| 항목 | 확인 방법 |
|------|----------|
| OpenSearch 기동 | `kubectl get pods -n utterai-observability` — opensearch-0 Running |
| trace 데이터 수신 | `curl http://localhost:9200/otel-v1-apm-span-*/_count` — count > 0 |
| Service Map 인덱스 | `otel-v1-apm-service-map` 인덱스 생성 확인 |
| UI Service Map | 토폴로지 그래프에 backend / cpu-worker / gpu-worker 노드 표시 |
| Trace 드릴다운 | Service Map 노드 클릭 → trace 목록 → trace 상세 타임라인 |
