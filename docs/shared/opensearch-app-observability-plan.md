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

### 스팬(Span)이란 무엇인가?

스팬은 **하나의 작업 단위**를 나타내는 JSON 레코드다. "backend가 POST /api/v1/sessions를 처리했다", "cpu-worker가 VAD를 실행했다" 같은 개별 작업이 각각 하나의 스팬이다.

#### 스팬의 핵심 필드

```json
{
  "traceId":    "7cc657da5fc691b8c7a698e41df58edf",  // 하나의 요청 흐름 전체를 묶는 ID
  "spanId":     "cd3c7fb2dec99331",                  // 이 스팬 자신의 고유 ID
  "parentSpanId": "37097c23bb242c39",                // 이 스팬을 만든 부모 스팬의 ID
  "serviceName": "cpu-worker",                       // 어느 서비스에서 만든 스팬인지
  "name":       "worker.cpu.message",                // 작업 이름
  "kind":       "SPAN_KIND_CONSUMER",                // 스팬 종류 (아래 설명)
  "startTime":  "2026-06-25T10:10:31Z",
  "endTime":    "2026-06-25T10:12:45Z",
  "status":     "STATUS_CODE_OK",
  "attributes": { "session_id": "abc", "job.id": "xyz" }
}
```

**traceId**: 하나의 사용자 요청이 여러 서비스를 거치더라도 동일한 traceId를 공유한다. "이 분석 요청이 backend → cpu-worker → ml-gpu-worker를 거쳤다"는 것을 traceId로 묶는다.

**spanId / parentSpanId**: 스팬 간 부모-자식 관계를 만든다. 아래 예시처럼 트리 구조를 형성한다.

```
[backend] POST /api/v1/sessions  spanId=A  parentSpanId=""  (루트 스팬)
    └── [backend] SQS.SendMessage  spanId=B  parentSpanId=A
    └── [backend] SELECT utterai   spanId=C  parentSpanId=A

[cpu-worker] worker.cpu.message   spanId=D  parentSpanId=B  ← backend B의 자식
    └── [cpu-worker] worker.cpu.pipeline  spanId=E  parentSpanId=D
        └── [cpu-worker] worker.cpu.vad   spanId=F  parentSpanId=E
        └── [cpu-worker] worker.cpu.publish_ml_gpu  spanId=G  parentSpanId=E

[ml-gpu-worker] worker.ml_gpu.message  spanId=H  parentSpanId=G  ← cpu-worker G의 자식
    └── [ml-gpu-worker] worker.ml_gpu.asr  spanId=I  parentSpanId=H
```

모든 스팬이 동일한 traceId를 가진다. parentSpanId가 다른 서비스의 spanId를 참조할 때 **서비스 간 연결**이 생긴다.

#### SpanKind: 스팬이 하는 역할

| Kind | 의미 | 예시 |
|------|------|------|
| `SERVER` | 외부에서 온 요청을 처리 | FastAPI HTTP 핸들러 |
| `CLIENT` | 외부로 나가는 동기 요청 | DB 쿼리, HTTP 호출 |
| `PRODUCER` | 비동기 메시지를 발행 | SQS.SendMessage |
| `CONSUMER` | 비동기 메시지를 수신 | SQS 폴링 후 처리 |
| `INTERNAL` | 서비스 내부 로직 | 함수 단위 계측 |

---

### 부모-자식 관계와 서비스 간 trace 전파

같은 서비스 내 부모-자식 관계는 자연스럽다. 문제는 **서비스 경계를 넘을 때**다.

HTTP를 쓰면 요청 헤더에 `traceparent: 00-{traceId}-{spanId}-01` (W3C Trace Context 표준)을 실어 보내면 받는 쪽이 자동으로 읽어서 CONSUMER 스팬의 parentSpanId로 설정한다.

SQS 비동기 구조는 헤더가 없다. 메시지 본문이나 `MessageAttributes`에 직접 traceparent를 심어야 한다.

```
backend (PRODUCER span 시작)
    inject(carrier) → traceparent = "00-{traceId}-{PRODUCER_spanId}-01"
    SQS.SendMessage(MessageAttributes={"traceparent": "00-...-{PRODUCER_spanId}-01"})

cpu-worker (메시지 수신)
    message_context = extract_context_from_message_attributes(raw["MessageAttributes"])
    # → traceparent 파싱 → traceId, parentSpanId = PRODUCER_spanId 추출
    with tracer.start_as_current_span("worker.cpu.message", context=message_context, kind=CONSUMER):
        # 이 스팬의 parentSpanId = backend PRODUCER 스팬의 spanId
        # 이 스팬의 traceId = backend와 동일한 traceId
```

이 흐름이 연결되면 두 서비스의 스팬이 **동일한 traceId 아래** 부모-자식으로 묶인다.

---

### Data Prepper는 왜 필요한가?

앱이 OTel Collector로 보내는 trace 데이터 형식은 **OTLP (OpenTelemetry Protocol)** 이다. OpenSearch는 OTLP를 직접 이해하지 못한다. 둘 사이의 **변환기**가 Data Prepper다.

```
앱 → OTel Collector (OTLP 수집)
         │
         ▼ OTLP gRPC
     Data Prepper
         │ otel_trace_raw 프로세서: OTLP 스팬 → OpenSearch JSON 변환 (180s flush)
         │ service_map 프로세서: 스팬 분석 → 서비스 간 연결 관계 생성
         │
         ▼ REST API
     OpenSearch
         ├── otel-v1-apm-span-*       (스팬 원본)
         └── otel-v1-apm-service-map  (토폴로지)
```

Data Prepper 내부 파이프라인 2개:

**raw-trace-pipeline**: 스팬을 그대로 변환해서 저장. `otel_trace_raw` 프로세서가 동일 traceId의 스팬을 180초 동안 모아서 한꺼번에 flush한다. Trace 상세 뷰에 사용.

**service-map-pipeline**: 스팬들을 분석해서 "A 서비스가 B 서비스를 호출했다"는 관계를 추출. `service_map` 프로세서가 180초 슬라이딩 윈도우 안에 들어오는 스팬들의 부모-자식 관계를 집계한다.

---

### Service Map 토폴로지 생성 조건

`service_map` 프로세서가 서비스 간 엣지(화살표)를 만들려면 **두 가지 조건이 동시에 충족**되어야 한다.

**조건 1**: 180초 윈도우 안에 부모와 자식 스팬이 모두 도착해야 한다.

**조건 2**: 부모-자식 스팬이 서로 다른 서비스에 속하고, 다음 Kind 조합 중 하나여야 한다.

| 부모 스팬 Kind | 자식 스팬 Kind | 의미 |
|--------------|--------------|------|
| `CLIENT` | `SERVER` | 동기 HTTP/RPC 호출 |
| `PRODUCER` | `CONSUMER` | 비동기 메시지 (SQS, Kafka 등) |

`INTERNAL` kind의 부모 스팬은 엣지를 만들지 않는다. 서비스 내부 로직을 나타내는 INTERNAL은 서비스 간 경계를 표현하지 않기 때문이다.

#### 실제 문제 사례 (수정 전)

```
[cpu-worker] worker.cpu.publish_ml_gpu  spanId=G  kind=INTERNAL  ← ❌ 문제
    inject() → traceparent 의 spanId = G

[ml-gpu-worker] worker.ml_gpu.message  parentSpanId=G  kind=CONSUMER
```

service_map 프로세서: "G는 INTERNAL이므로 엣지 생성 건너뜀" → service-map에 연결 없음

#### 수정 후

```
[cpu-worker] worker.cpu.publish_ml_gpu  spanId=G  kind=PRODUCER  ← ✅ 수정
    inject() → traceparent 의 spanId = G

[ml-gpu-worker] worker.ml_gpu.message  parentSpanId=G  kind=CONSUMER
```

service_map 프로세서: "PRODUCER→CONSUMER, 서비스 다름 → cpu-worker → ml-gpu-worker 엣지 생성"

---

### trace는 어디서 오는가?

앱들이 이미 OTel SDK를 통해 OTel Collector로 trace를 보내고 있다. **앱 코드에서 SQS 발행 스팬의 Kind만 수정**하면 Service Map 연결이 생긴다.

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
  (OTel SDK 심어져 있음 — SQS 발행 span Kind 수정됨)
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

`service_map` processor (Data Prepper 2.x)가 180초 윈도우 안에 도착한 스팬들 사이의 PRODUCER→CONSUMER, CLIENT→SERVER 관계를 분석해 서비스 간 토폴로지를 생성한다.

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

### Step 3: SQS trace 전파 수정 — PRODUCER span Kind (앱 PR)

Service Map에 서비스 간 화살표를 생성하려면 SQS를 통한 비동기 흐름에서 발행 시점의 span Kind가 `PRODUCER`여야 한다.

#### 문제 근거

Data Prepper `service_map` 프로세서는 스팬의 부모-자식 관계를 분석할 때 부모 span의 Kind가 `PRODUCER` 또는 `CLIENT`인 경우에만 서비스 간 엣지를 생성한다. `INTERNAL` kind는 서비스 내부 로직으로 간주되어 엣지 생성 대상이 아니다.

#### 실제 확인된 span 체인 (수정 전)

OpenSearch raw 인덱스에서 확인한 연결 구조:

```
traceId: 7cc657da5fc691b8c7a698e41df58edf

[backend]     sqs.publish.analysis_job   kind=INTERNAL → ❌ 엣지 없음
[cpu-worker]  worker.cpu.message         kind=CONSUMER  parentSpanId=backend span

[cpu-worker]  worker.cpu.publish_ml_gpu  kind=INTERNAL → ❌ 엣지 없음
[ml-gpu-worker] worker.ml_gpu.message    kind=CONSUMER  parentSpanId=cpu-worker span
```

#### 수정 내용

**`Utterai_BE/app/infrastructure/sqs/client.py`**

```python
# 수정 전: 별도 span 없이 inject()만 호출
carrier: dict[str, str] = {}
inject(carrier)  # 활성 span = FastAPI SERVER span → PRODUCER 아님

# 수정 후: PRODUCER span 안에서 inject() 호출
with _tracer.start_as_current_span("sqs.publish.analysis_job", kind=trace.SpanKind.PRODUCER):
    inject(carrier)  # 활성 span = PRODUCER span → 워커의 parentSpanId가 이 span을 가리킴
    self._get_client().send_message(...)
```

- `send_analysis_job`: `sqs.publish.analysis_job` (PRODUCER) 추가
- `send_report_job`: `sqs.publish.report_job` (PRODUCER) 추가

**`UtterAI_AI/app/pipelines/analysis_pipeline.py`**

```python
# 수정 전
with tracer.start_as_current_span("worker.cpu.publish_ml_gpu") as span:  # kind=INTERNAL

# 수정 후
with tracer.start_as_current_span("worker.cpu.publish_ml_gpu", kind=trace.SpanKind.PRODUCER) as span:
```

`_send_sqs()` 내부의 `build_message_attributes_from_current_context()`가 `inject()`를 호출하는 시점에 PRODUCER span이 활성화되어 있으므로, ml-gpu-worker의 CONSUMER span이 이 PRODUCER span을 부모로 가리키게 된다.

#### 수정 후 예상 service_map 인덱스

```json
{ "serviceName": "cpu-worker", "destination": { "serviceName": "backend" }, "target": { "serviceName": "cpu-worker" } }
{ "serviceName": "ml-gpu-worker", "destination": { "serviceName": "cpu-worker" }, "target": { "serviceName": "ml-gpu-worker" } }
```

#### SQS trace 전파 전체 흐름 (수정 후)

```
[backend] FastAPI SERVER span  spanId=A
    └── [backend] sqs.publish.analysis_job  spanId=B  kind=PRODUCER  ← 신규
              inject() → traceparent = "00-{traceId}-B-01"
              SQS.SendMessage(MessageAttributes={traceparent: "00-...-B-01"})

[cpu-worker] worker.cpu.message  spanId=C  kind=CONSUMER  parentSpanId=B  ← B가 부모
    └── worker.cpu.pipeline  spanId=D
        └── worker.cpu.publish_ml_gpu  spanId=E  kind=PRODUCER  ← 수정됨
                  inject() → traceparent = "00-{traceId}-E-01"
                  SQS.SendMessage(MessageAttributes={traceparent: "00-...-E-01"})

[ml-gpu-worker] worker.ml_gpu.message  spanId=F  kind=CONSUMER  parentSpanId=E  ← E가 부모
```

service_map 프로세서: B(PRODUCER, backend) → C(CONSUMER, cpu-worker) → 엣지 생성
service_map 프로세서: E(PRODUCER, cpu-worker) → F(CONSUMER, ml-gpu-worker) → 엣지 생성

---

### Step 4: observability-ui 앱 개발 (별도 repo)

- React + TypeScript 프로젝트 초기화
- OpenSearch REST API 연동 (`/otel-v1-apm-span-*`, `otel-v1-apm-service-map` index)
- Service Map → Trace 상세 뷰 드릴다운 구현

### Step 5: observability-ui EKS 배포 (인프라 PR)

파일: `k8s/platform/observability/base/observability-ui.yaml`

- Deployment + ClusterIP Service
- 접근: `kubectl port-forward svc/observability-ui -n utterai-observability 3000:3000`

---

## 검증 계획

| 항목 | 확인 방법 |
|------|----------|
| OpenSearch 기동 | `kubectl get pods -n utterai-observability` — opensearch-0 1/1 Running |
| trace 데이터 수신 | `kubectl exec opensearch-0 -- curl localhost:9200/otel-v1-apm-span-*/_count` — count > 0 |
| Service Map 인덱스 생성 | `otel-v1-apm-service-map` 인덱스 생성 확인 |
| Service Map 연결 확인 | service-map 문서의 `destination` 필드가 null이 아닌 항목 존재 확인 |
| 서비스 간 엣지 검증 | `destination.serviceName`에 backend, cpu-worker가 포함되는지 확인 |
| UI Service Map | 토폴로지 그래프에 backend → cpu-worker → ml-gpu-worker 연결 표시 |
| Trace 드릴다운 | Service Map 노드 클릭 → trace 목록 → trace 상세 타임라인 |
