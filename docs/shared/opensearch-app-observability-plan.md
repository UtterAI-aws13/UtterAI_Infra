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

## 컴포넌트 상세 설명

### OpenSearch란?

Elasticsearch를 fork한 오픈소스 검색/분석 엔진이다. 핵심은 **인덱스(Index)** 단위로 JSON 문서를 저장하고 빠르게 검색/집계할 수 있다는 점이다.

우리가 쓰는 방식은 검색엔진 기능보다 **시계열 데이터 저장소**로서의 역할이다. trace 데이터(스팬들)를 JSON으로 저장해두고, 나중에 UI가 "이 서비스의 최근 1시간 에러 trace 목록 줘" 같은 쿼리를 날리면 빠르게 응답하는 구조다.

```
OpenSearch 내부 인덱스 구조 (Data Prepper가 자동 생성)
├── otel-v1-apm-span-YYYY.MM.DD    ← 개별 스팬 (어떤 작업을 했는지)
└── otel-v1-apm-service-map        ← 서비스 간 연결 관계 (Service Map용)
```

Prometheus는 숫자 메트릭만 저장하지만, OpenSearch는 스팬 전체를 JSON으로 저장해서 "이 trace_id의 모든 스팬", "backend 서비스에서 발생한 에러 스팬" 같은 쿼리가 가능하다.

---

### Data Prepper는 왜 필요한가?

앱이 OTel Collector로 보내는 trace 데이터 형식은 **OTLP (OpenTelemetry Protocol)** 이다. OpenSearch는 OTLP를 직접 이해하지 못한다. 둘 사이의 **변환기**가 Data Prepper다.

```
앱 → OTel Collector (OTLP 수집)
         │
         ▼ OTLP gRPC
     Data Prepper
         │ otel_traces_raw 프로세서: OTLP 스팬 → OpenSearch JSON 변환
         │ service_map_stateful 프로세서: 스팬 분석 → 서비스 간 연결 관계 생성
         │
         ▼ REST API
     OpenSearch
         ├── otel-v1-apm-span-*       (스팬 원본)
         └── otel-v1-apm-service-map  (토폴로지)
```

Data Prepper 내부 파이프라인 2개:

**raw-trace-pipeline**: 스팬을 그대로 변환해서 저장. Trace 상세 뷰에 사용.

**service-map-pipeline**: 스팬들을 분석해서 "A 서비스가 B 서비스를 호출했다"는 관계를 추출. `service_map_stateful` 프로세서가 180초 윈도우 안에 들어오는 스팬들을 보면서 부모-자식 관계를 집계한다. SQS 비동기 구조에서도 trace propagation이 연결돼 있으면 관계를 잡아낼 수 있다. Grafana servicegraph와의 차이: Grafana는 실시간 매칭(TTL 내)이지만 Data Prepper는 저장된 데이터를 집계해서 만든다.

---

### trace는 어디서 오는가?

앱들이 이미 OTel SDK를 통해 OTel Collector로 trace를 보내고 있다. **앱 코드 변경 없이** OTel Collector에서 Data Prepper로 분기하는 것만 추가한다.

```
현재 흐름:
backend / cpu-worker / gpu-worker
  └─ OTLP HTTP → otel-collector:4318
                      ├── traces  → Tempo (Grafana 드릴다운)
                      ├── metrics → Prometheus (spanmetrics)
                      └── logs    → Loki

추가 후 흐름:
backend / cpu-worker / gpu-worker
  └─ OTLP HTTP → otel-collector:4318
                      ├── traces  → Tempo          (기존 유지)
                      ├── traces  → Data Prepper   (신규 분기)
                      ├── metrics → Prometheus     (기존 유지)
                      └── logs    → Loki           (기존 유지)
```

OTel Collector config에 exporter 하나 추가하는 것만으로 동일한 trace 데이터가 두 곳에 동시에 전달된다.

---

### 전체 흐름 요약

```
[ 앱 ]
  backend / cpu-worker / gpu-worker
  (OTel SDK 심어져 있음 — 코드 변경 없음)
       │ OTLP HTTP (:4318)
       ▼
[ OTel Collector ]
       │
       ├─────────────────────────────────────┐
       │ OTLP gRPC (:21890)                  │ OTLP gRPC (:4317)
       ▼                                     ▼
[ Data Prepper ]                         [ Tempo ]
  raw-trace-pipeline                     Grafana 드릴다운
  service-map-pipeline
       │ REST API
       ▼
[ OpenSearch ]
  otel-v1-apm-span-*        ← 스팬 원본
  otel-v1-apm-service-map   ← 서비스 토폴로지
       │ REST API
       ▼
[ Observability UI ]  ← Step 3~4 구현 예정
  Service Map / Trace 상세 / 에러 통계
```

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
