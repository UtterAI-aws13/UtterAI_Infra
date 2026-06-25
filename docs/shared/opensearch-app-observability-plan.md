# UtterAI 앱 레벨 옵저빌리티 — 설계 · 구현 문서

---

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

### Phase 2 완료

EKS self-hosted OpenSearch + Data Prepper + utterai-voc-ui 구현 완료.

---

## 역할 분리 원칙

| 레벨 | 스택 | 용도 |
|------|------|------|
| 인프라 레벨 | Prometheus + Grafana | pod CPU/메모리, node 상태, 인프라 알람 |
| 앱 레벨 | OpenSearch + utterai-voc-ui | Service Map, Trace 상세, 에러 통계, VOC 대응 |

Tempo / Loki는 인프라 레벨로 병행 유지. OpenSearch Dashboards는 사용하지 않는다.

---

## 핵심 개념

### Trace ID

하나의 **사용자 요청 흐름 전체**를 묶는 128-bit 고유 식별자(32자 hex 문자열)다.

사용자가 음성 파일 분석을 요청하면, 그 요청이 backend → cpu-worker → ml-gpu-worker를 거쳐 완료될 때까지 발생하는 **모든 스팬이 동일한 traceId를 공유**한다. traceId는 최초 진입점(backend HTTP 핸들러)에서 OTel SDK가 자동 생성하고, 이후 서비스 간 호출마다 전파된다.

```
traceId = "7cc657da5fc691b8c7a698e41df58edf"  (32자 hex)

[backend]       traceId=7cc657...  ← 최초 생성
[cpu-worker]    traceId=7cc657...  ← SQS 메시지 속성에서 추출
[ml-gpu-worker] traceId=7cc657...  ← SQS 메시지 속성에서 추출
```

같은 traceId를 가진 스팬들을 OpenSearch에서 조회하면 하나의 요청이 서비스를 넘나들며 어디서 얼마나 걸렸는지 전체 그림이 나온다.

**traceId가 생성되는 시점**: `tracer.start_as_current_span()`이 root span(parentSpanId가 없는 스팬)을 만들 때 OTel SDK가 자동 생성. HTTP 요청이면 헤더에 기존 traceId가 있으면 그것을 이어받고, 없으면 새로 생성한다.

---

### 스팬(Span)

스팬은 **하나의 작업 단위**를 나타내는 JSON 레코드다. "backend가 POST /api/v1/analysis-jobs를 처리했다", "cpu-worker가 전처리를 실행했다" 같은 개별 작업이 각각 하나의 스팬이다.

#### 스팬의 전체 필드

```json
{
  "traceId":      "7cc657da5fc691b8c7a698e41df58edf",
  "spanId":       "cd3c7fb2dec99331",
  "parentSpanId": "37097c23bb242c39",
  "serviceName":  "cpu-worker",
  "name":         "worker.cpu.message",
  "kind":         "SPAN_KIND_CONSUMER",
  "startTime":    "2026-06-25T10:10:31.123456789Z",
  "durationInNanos": 134500000000,
  "status":       "STATUS_CODE_OK",
  "attributes": {
    "session_id":  "abc-123",
    "job.id":      "xyz-456",
    "sqs.queue":   "audio-preprocess-queue"
  }
}
```

**traceId**: 어느 요청 흐름에 속하는지. 서비스를 넘어도 동일한 값.

**spanId**: 이 스팬 자신의 고유 ID (16자 hex). 다른 스팬이 `parentSpanId`로 이 값을 참조하면 부모-자식 관계가 형성된다.

**parentSpanId**: 나를 만든 부모 스팬의 spanId. 없으면 루트 스팬(trace의 시작점). **서비스 간 경계를 넘는 연결도 이 필드 하나로 표현**한다. cpu-worker의 CONSUMER 스팬이 backend의 PRODUCER 스팬을 `parentSpanId`로 가리키면, 두 스팬이 다른 프로세스에 있어도 하나의 tree로 묶인다.

**serviceName**: 스팬을 만든 서비스 이름. k8s Deployment의 `OTEL_SERVICE_NAME` 환경변수로 결정된다.
- backend → `"backend"`
- cpu-worker → `"cpu-worker"`
- ml-gpu-worker → `"ml-gpu-worker"`

**name**: 작업 이름. 개발자가 `start_as_current_span("이름")` 으로 지정한다.

**kind**: 스팬의 역할 (아래 별도 설명).

**durationInNanos**: 작업 소요 시간. 나노초 단위. UI에서 `/ 1_000_000` 하면 밀리초.

**status**: `STATUS_CODE_OK` 또는 `STATUS_CODE_ERROR`. 예외가 발생하면 SDK가 자동으로 ERROR로 기록한다.

**attributes**: 개발자가 붙인 추가 메타데이터. `span.set_attribute("key", "value")`로 추가.

#### SpanKind: 스팬의 역할 분류

| Kind | 의미 | 우리 코드 예시 |
|------|------|---------------|
| `SPAN_KIND_SERVER` | 외부에서 온 요청을 처리 | FastAPI HTTP 핸들러 (`POST /api/v1/analysis-jobs`) |
| `SPAN_KIND_CLIENT` | 외부로 나가는 동기 요청 | DB 쿼리, 외부 HTTP 호출 |
| `SPAN_KIND_PRODUCER` | 비동기 메시지를 발행 | `sqs.publish.analysis_job`, `worker.cpu.publish_ml_gpu` |
| `SPAN_KIND_CONSUMER` | 비동기 메시지를 수신해서 처리 | `worker.cpu.message`, `worker.ml_gpu.message` |
| `SPAN_KIND_INTERNAL` | 서비스 내부 로직 | 함수 단위 계측 (`run_preprocess`, `run_vad`) |

**PRODUCER / CONSUMER가 중요한 이유**: Data Prepper의 `service_map` 프로세서는 `PRODUCER → CONSUMER` 쌍(부모-자식)에서만 서비스 간 엣지를 생성한다. `INTERNAL` kind의 스팬이 부모면 엣지가 만들어지지 않는다. 이것이 Phase 2 초기에 service map에 연결이 없었던 원인이었다.

#### parentSpanId가 만드는 트리 구조

같은 traceId 안에서 `parentSpanId` → `spanId` 연결이 트리를 형성한다.

```
[traceId: 7cc657...]

[backend]       POST /api/v1/analysis-jobs   spanId=A  parentSpanId=""    (root)
                    │
                    └── [backend]   sqs.publish.analysis_job   spanId=B  parentSpanId=A  (PRODUCER)
                                          │
                                          └── [cpu-worker]   worker.cpu.message   spanId=C  parentSpanId=B  (CONSUMER)
                                                      │
                                                      ├── [cpu-worker]   run_preprocess   spanId=D  parentSpanId=C  (INTERNAL)
                                                      │
                                                      └── [cpu-worker]   worker.cpu.publish_ml_gpu   spanId=E  parentSpanId=C  (PRODUCER)
                                                                                │
                                                                                └── [ml-gpu-worker]   worker.ml_gpu.message   spanId=F  parentSpanId=E  (CONSUMER)
                                                                                            │
                                                                                            ├── [ml-gpu-worker]   run_diarization   spanId=G  parentSpanId=F
                                                                                            └── [ml-gpu-worker]   run_asr           spanId=H  parentSpanId=F
```

모든 스팬이 동일한 traceId를 가진다. B(backend, PRODUCER) → C(cpu-worker, CONSUMER)처럼 `parentSpanId`가 **다른 서비스의 spanId를 가리키는 순간** 서비스 간 연결이 형성되고, Data Prepper가 이 관계를 service-map 인덱스에 기록한다.

---

### SQS를 넘는 Trace Context 전파

같은 서비스 내 부모-자식 관계는 메모리 안에서 자연스럽게 이어진다. **문제는 서비스 경계를 SQS 메시지로 넘을 때**다. 프로세스가 다르기 때문에 context를 명시적으로 메시지에 실어 보내야 한다.

#### W3C Trace Context 표준 형식

```
traceparent: 00-{traceId}-{spanId}-{flags}
traceparent: 00-7cc657da5fc691b8c7a698e41df58edf-cd3c7fb2dec99331-01
             ↑  ←────────── traceId ──────────→  ←── spanId ───→  ↑
             버전                                                   샘플링 여부
```

#### 실제 전파 흐름

```python
# backend — PRODUCER 스팬 안에서 inject()
with tracer.start_as_current_span("sqs.publish.analysis_job", kind=SpanKind.PRODUCER) as span:
    carrier = {}
    inject(carrier)
    # carrier = {"traceparent": "00-7cc657...-spanId_B-01"}
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(payload),
        MessageAttributes={
            "traceparent": {"DataType": "String", "StringValue": carrier["traceparent"]}
        }
    )

# cpu-worker — 메시지 수신 시 extract()
raw_attrs = sqs_message["MessageAttributes"]
carrier = {"traceparent": raw_attrs["traceparent"]["StringValue"]}
ctx = extract(carrier)
# ctx 안에 traceId=7cc657..., parentSpanId=spanId_B 가 들어있음

with tracer.start_as_current_span("worker.cpu.message", context=ctx, kind=SpanKind.CONSUMER):
    # 이 스팬의 traceId = backend와 동일한 7cc657...
    # 이 스팬의 parentSpanId = backend PRODUCER 스팬의 spanId_B
```

이 흐름이 연결되면 서로 다른 프로세스의 스팬이 **동일한 traceId 아래 부모-자식**으로 묶인다.

---

### SQS 큐 대기 시간

PRODUCER 스팬 종료 ~ CONSUMER 스팬 시작 사이의 시간 간격이다.

```
[backend PRODUCER]  ───── 1ms ─────┤
                                    ← 이 구간 = SQS에서 메시지가 대기한 시간
                                    ├── [cpu-worker CONSUMER]  ─── 800ms ───
```

```
큐 대기 시간 = CONSUMER.startTime - (PRODUCER.startTime + PRODUCER.durationInNanos / 1_000_000)
```

이 값이 크면 cpu-worker 인스턴스가 메시지를 즉시 처리하지 못하고 있다는 뜻이다. 처리 자체가 느린 건지 워커가 부족한 건지 구분하는 기준이 된다.

---

## 데이터 수집 흐름

### OTEL SDK → OpenSearch 전체 경로

```
[ 앱 ]
  backend / cpu-worker / ml-gpu-worker
  (Python OTel SDK: opentelemetry-sdk)
  각 서비스의 Deployment에 OTEL_EXPORTER_OTLP_ENDPOINT 환경변수 설정
       │ OTLP HTTP (port 4318)
       ▼
[ OTel Collector ]  (utterai-observability namespace)
       │
       ├── OTLP gRPC → Tempo          (Grafana 드릴다운, 기존 유지)
       ├── spanmetrics → Prometheus   (에러율/지연, 기존 유지)
       ├── logs → Loki               (기존 유지)
       │
       └── OTLP gRPC (port 21890) → Data Prepper  ← Phase 2 신규 추가
                                           │
                                    ┌──────┴───────┐
                                    ▼              ▼
                            raw-trace-pipeline  service-map-pipeline
                            (otel_trace_raw)    (service_map processor)
                            180s flush          180s sliding window
                                    │              │
                                    └──────┬───────┘
                                           ▼
                                    [ OpenSearch ]  (utterai-observability namespace)
                                    otel-v1-apm-span-YYYY.MM.DD  ← 스팬 원본
                                    otel-v1-apm-service-map       ← 서비스 토폴로지
                                           │ REST API (port 9200)
                                           ▼
                                    [ utterai-voc-ui ]
                                    nginx /opensearch/ → OpenSearch ClusterIP
```

### OTel Collector에서의 분기

OTel Collector config에 exporter를 추가하는 것만으로 동일한 trace 데이터가 Tempo(기존)와 Data Prepper(신규) 두 곳에 동시 전달된다. 앱 코드 변경 없음.

```yaml
exporters:
  otlp/tempo:
    endpoint: tempo.utterai-observability.svc.cluster.local:4317
  otlp/data-prepper:
    endpoint: data-prepper.utterai-observability.svc.cluster.local:21890
    tls:
      insecure: true

service:
  pipelines:
    traces:
      exporters: [otlp/tempo, spanmetrics, otlp/data-prepper]
```

### Data Prepper 파이프라인 상세

Data Prepper는 OTLP 형식과 OpenSearch JSON 사이의 변환기다. OpenSearch는 OTLP를 직접 이해하지 못하기 때문에 중간에 필요하다.

**raw-trace-pipeline** (`otel_trace_raw` 프로세서)
- OTLP 스팬 → OpenSearch JSON 변환
- 동일 traceId의 스팬을 **180초 동안 버퍼에 모았다가** 한꺼번에 flush
- 저장 위치: `otel-v1-apm-span-YYYY.MM.DD` (일별 인덱스)

**service-map-pipeline** (`service_map` 프로세서)
- 스팬의 부모-자식 관계를 분석해서 서비스 간 연결 추출
- **PRODUCER→CONSUMER, CLIENT→SERVER 쌍만** 엣지로 인정
- **180초 슬라이딩 윈도우** 안에 부모와 자식 스팬이 모두 도착해야 엣지 생성
- 저장 위치: `otel-v1-apm-service-map`

#### 180초 flush가 의미하는 것

스팬이 OpenSearch에 보이기까지 최대 180초가 걸린다. ml-gpu-worker처럼 처리 시간이 긴 스팬은 완료 후에도 Data Prepper 버퍼에서 기다리다가 flush된다. Service Map은 이 윈도우 안에 부모·자식이 모두 들어와야 엣지가 생기므로, 처음 배포 후에는 3분 이상 기다려야 연결이 나타난다.

### OpenSearch 인덱스 구조

```
OpenSearch
├── otel-v1-apm-span-2026.06.25   (일별 인덱스, 자동 생성)
│   └── 문서 1개 = 스팬 1개
│       traceId, spanId, parentSpanId, serviceName, name, kind, startTime, durationInNanos, status, attributes
│
└── otel-v1-apm-service-map
    └── 문서 1개 = 서비스 간 연결 1개
        serviceName, destination.domain, traceGroupName
```

`traceId` 필드는 OpenSearch에서 `keyword` 타입으로 매핑된다 (`traceId.keyword` 서브필드 없음). `term` 쿼리로 직접 조회 가능.

---

## 파이프라인 토폴로지

### 실제 배포된 서비스와 흐름

```
[사용자/FE]
    │
    │ POST /api/v1/analysis-jobs
    ▼
[backend]  ──(SQS: audio-preprocess-queue PRODUCER)──▶  [cpu-worker: preprocess loop]
                                                                  │
                                                      (SQS: gpu-inference-queue PRODUCER)
                                                                  │
                                                                  ▼
                                                         [ml-gpu-worker]
                                                    STT + 화자분리 → transcript 저장
                                                    status = ANALYSIS_COMPLETED
                                                    (SQS 발행 없음, 여기서 종료)

[사용자/FE]
    │
    │ POST /api/v1/transcripts/{id}/finalize   ← 사용자가 수동으로 트리거
    ▼
[backend: TranscriptService.finalize()]
    │ S3에 final transcript 저장
    │ SQSClient().send_report_job()
    │
    ▼
[backend]  ──(SQS: report-analysis-queue PRODUCER)──▶  [cpu-worker: report loop]
                                                                  │
                                                         run_bedrock_report_stage()
```

### Service Map 엣지 (3개)

| 엣지 | 발생 시점 | SQS 큐 |
|------|----------|--------|
| `backend → cpu-worker` | 분석 job 생성 API 호출 | audio-preprocess-queue |
| `cpu-worker → ml-gpu-worker` | cpu preprocess 완료 후 | gpu-inference-queue |
| `backend → cpu-worker` | 사용자가 finalize API 호출 | report-analysis-queue |

### 주요 확인 사항

**ml-gpu-worker → cpu-worker 엣지가 없는 이유**: ml-gpu-worker는 작업 완료 후 SQS에 아무것도 발행하지 않는다(`analysis_pipeline.py: run_ml_gpu_stage` 참고). report 루프는 ml-gpu-worker가 직접 트리거하지 않고 사용자/FE가 `/finalize` API를 호출해야 시작된다.

**llm_gpu_worker.py**: 코드에 존재하나 k8s Deployment가 없어서 배포되지 않은 상태. service map에 나타나지 않는 것이 정상.

**cpu-worker의 두 루프**: 하나의 pod 안에서 `preprocess loop`(audio-preprocess-queue 소비)와 `report loop`(report-analysis-queue 소비)가 별도 스레드로 동작한다. `OTEL_SERVICE_NAME=cpu-worker`로 동일하므로 service map에는 하나의 노드로 나타난다.

---

## utterai-voc-ui 구현

### 리포지터리

`utterai-voc-ui` (별도 repo) — React + TypeScript + Vite

### 기술 스택

| 항목 | 선택 |
|------|------|
| 프레임워크 | React + TypeScript (Vite) |
| 서비스 토폴로지 그래프 | Cytoscape.js (`breadthfirst` directed layout) |
| 스타일 | CSS Modules |
| 배포 | Dockerfile (node:20-alpine build → nginx:alpine serve) |
| OpenSearch 프록시 | nginx `/opensearch/` → OpenSearch ClusterIP:9200 |
| 로컬 개발 프록시 | Vite dev server `/opensearch` → localhost:9200 |

### 화면 구성

**Service Map 탭** (3단 레이아웃, 각 패널 드래그로 크기 조절 가능)

```
┌─────────────────────┬───┬──────────────┬───┬─────────────────────┐
│                     │   │              │   │                     │
│   Service Map       │ ◀ │  Trace 목록  │ ◀ │  Trace Gantt        │
│   (Cytoscape.js)    │   │  (최근 20건) │   │  (스팬 트리 뷰)     │
│                     │   │  에러 하이라 │   │  + SQS 대기 시간    │
│   노드 클릭 →       │   │  이트 포함   │   │                     │
│   오른쪽 패널       │   │              │   │                     │
└─────────────────────┴───┴──────────────┴───┴─────────────────────┘
```

**에러 통계 탭**: 서비스별 총 요청 수, 에러 수, 에러율(%), p50/p99 지연

**헤더 traceId 검색**: traceId 붙여넣기 후 Enter → 해당 Trace Gantt 바로 열림

### OpenSearch 쿼리 패턴

**Service Map 데이터 조회** (`otel-v1-apm-service-map`)
```json
POST /otel-v1-apm-service-map/_search
{
  "size": 200,
  "query": { "match_all": {} },
  "_source": ["serviceName", "destination", "traceGroupName"]
}
```

**서비스별 최근 트레이스 목록** (`otel-v1-apm-span-*`)
```json
POST /otel-v1-apm-span-*/_search
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        { "term": { "serviceName": "backend" } },
        { "bool": {
          "should": [
            { "term": { "kind": "SPAN_KIND_SERVER" } },
            { "term": { "kind": "SPAN_KIND_CONSUMER" } }
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "sort": [{ "startTime": { "order": "desc" } }],
  "_source": ["traceId", "name", "startTime", "durationInNanos", "status"]
}
```

SERVER + CONSUMER 진입점 스팬만 필터링하는 이유: 하나의 trace에는 수십 개의 INTERNAL 스팬이 있는데, 사용자에게 의미 있는 "이 요청이 언제 들어왔고 얼마나 걸렸는가"는 진입점 스팬(SERVER: HTTP 요청, CONSUMER: SQS 수신) 하나로 표현된다.

**Trace 전체 스팬 조회** (`otel-v1-apm-span-*`)
```json
POST /otel-v1-apm-span-*/_search
{
  "size": 500,
  "query": { "term": { "traceId": "7cc657da5fc691b8c7a698e41df58edf" } },
  "_source": ["traceId", "spanId", "parentSpanId", "serviceName", "name", "kind",
              "status", "startTime", "durationInNanos", "attributes"],
  "sort": [{ "startTime": { "order": "asc" } }]
}
```

`traceId`는 keyword 타입이므로 `.keyword` 서브필드 없이 `term` 쿼리 직접 사용.

**서비스별 에러 통계** (`otel-v1-apm-span-*`)
```json
POST /otel-v1-apm-span-*/_search
{
  "size": 0,
  "query": {
    "bool": {
      "must": [
        { "bool": { "should": [
          { "term": { "kind": "SPAN_KIND_SERVER" } },
          { "term": { "kind": "SPAN_KIND_CONSUMER" } }
        ], "minimum_should_match": 1 }},
        { "range": { "startTime": { "gte": "now-60m" } } }
      ]
    }
  },
  "aggs": {
    "by_service": {
      "terms": { "field": "serviceName.keyword", "size": 20 },
      "aggs": {
        "error_count": { "filter": { "term": { "status": "STATUS_CODE_ERROR" } } },
        "p50": { "percentiles": { "field": "durationInNanos", "percents": [50] } },
        "p99": { "percentiles": { "field": "durationInNanos", "percents": [99] } }
      }
    }
  }
}
```

### Trace Gantt — 스팬 트리 뷰

`fetchTraceSpans()`로 traceId에 해당하는 모든 스팬을 가져온 뒤, `parentSpanId → spanId` 연결로 트리를 구성하고 DFS 순서로 펼쳐서 렌더링한다.

```
buildTree(spans):
  1. spanId → SpanNode 맵 생성
  2. 각 노드를 parentSpanId가 가리키는 부모의 children에 추가
  3. DFS로 펼치며 depth 부여
  4. 결과: [root, child1, grandchild1, child2, ...]  (DFS 순)

렌더링:
  depth=0: 들여쓰기 없음
  depth=1: └─ 또는 ├─  (isLastChild 여부에 따라)
  depth=2:   └─ 또는   ├─  (14px * (depth-1) padding)
```

### SQS 큐 대기 시간 표시

```
큐 대기 = CONSUMER.startTime_ms - (PRODUCER.startTime_ms + PRODUCER.durationInNanos / 1_000_000)
```

CONSUMER 스팬의 `parentSpanId`가 가리키는 부모가 `SPAN_KIND_PRODUCER`이면 계산해서 해당 CONSUMER 행 위에 `⏳ SQS 대기 +Xms` 주황색 행으로 표시한다.

---

## 인프라 구성

### 배포 컴포넌트

| 컴포넌트 | 이미지 | 네임스페이스 | 리소스 |
|----------|--------|-------------|--------|
| OpenSearch | `opensearchproject/opensearch:2.13.0` | utterai-observability | 2GB RAM, 20Gi PVC (gp2) |
| Data Prepper | `opensearchproject/data-prepper:2.8.0` | utterai-observability | 512MB RAM |
| utterai-voc-ui | 커스텀 빌드 (nginx:alpine) | utterai-observability | 256MB RAM |

OpenSearch는 single-node, `DISABLE_SECURITY_PLUGIN=true` (내부 전용). OpenSearch Dashboards는 배포하지 않는다.

### 주요 파일 위치

```
k8s/platform/observability/base/
├── opensearch.yaml          StatefulSet + PVC + ClusterIP
├── data-prepper.yaml        Deployment + ConfigMap (파이프라인 설정)
├── otel-collector.yaml      ConfigMap (data-prepper exporter 추가)
└── observability-ui.yaml    Deployment + ClusterIP
```

### nginx 프록시 설정 (utterai-voc-ui)

```nginx
location /opensearch/ {
  proxy_pass http://opensearch.utterai-observability.svc.cluster.local:9200/;
  proxy_set_header Host $host;
}
```

UI는 `/opensearch/인덱스명/_search`로 요청하면 nginx가 OpenSearch ClusterIP로 프록시한다. OpenSearch를 외부에 노출하지 않아도 UI에서 쿼리 가능.

---

## 구현 이력

### Step 1 — OpenSearch + Data Prepper 배포

- OpenSearch StatefulSet, `vm.max_map_count=262144` initContainer
- Data Prepper pipeline: entry → raw-trace + service-map

### Step 2 — OTel Collector 분기 추가

- `otlp/data-prepper` exporter 추가
- 기존 Tempo/Prometheus/Loki 파이프라인 영향 없음

### Step 3 — PRODUCER span Kind 수정 (앱 PR)

**문제**: SQS 발행 스팬이 `SPAN_KIND_INTERNAL`이어서 service_map 프로세서가 엣지를 생성하지 않음.

**수정**:
- `Utterai_BE/app/infrastructure/sqs/client.py`: `send_analysis_job`, `send_report_job`에 `SPAN_KIND_PRODUCER` 스팬 추가
- `UtterAI_AI/app/pipelines/analysis_pipeline.py`: `worker.cpu.publish_ml_gpu` 스팬 kind를 `INTERNAL → PRODUCER`로 변경

**결과**: `backend → cpu-worker`, `cpu-worker → ml-gpu-worker` 엣지 생성 확인

### Step 4 — utterai-voc-ui 개발 및 배포

- 3단 레이아웃 (Service Map + Trace 목록 + Trace Gantt), 각 패널 드래그 리사이저
- traceId 헤더 직접 검색
- Trace Gantt: 스팬 트리 뷰 (DFS 순, 들여쓰기 + ├/└ 연결선), PRODUCER/CONSUMER 배지
- SQS 큐 대기 시간 계산 및 표시
- 에러 통계 탭 (서비스별 에러율, p50/p99)
- nginx 리버스 프록시로 OpenSearch 내부 접근

---

## 검증

| 항목 | 확인 방법 |
|------|----------|
| OpenSearch 기동 | `kubectl get pods -n utterai-observability` — opensearch-0 1/1 Running |
| trace 수신 확인 | `kubectl exec opensearch-0 -- curl localhost:9200/otel-v1-apm-span-*/_count` |
| service-map 엣지 존재 | `otel-v1-apm-service-map` 인덱스 문서의 `destination.domain` 필드 확인 |
| UI 접근 | `kubectl port-forward svc/observability-ui -n utterai-observability 3000:3000` |
| Service Map 표시 | backend → cpu-worker → ml-gpu-worker 3개 노드, 엣지 확인 |
| Trace 드릴다운 | 노드 클릭 → trace 목록 → trace 클릭 → Gantt 스팬 트리 |
| SQS 대기 시간 | CONSUMER 행 위 ⏳ 주황색 행 표시 확인 |
