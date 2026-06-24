# EKS 고도화 — 관찰 가능성 & AI 기반 장애 자동 분석

> 참고 블로그
> - [AWS DevOps Agent + K8s Operator를 통한 EKS 운영 자동화](https://aws.amazon.com/ko/blogs/tech/aws-devops-agent-k8s-operator/)
> - [Amazon EKS에서 Spring Boot 애플리케이션 관찰 가능성 구성](https://aws.amazon.com/ko/blogs/tech/springboot-application-observability-using-amazon-eks/)

---

## 목차

1. [현재 UtterAI 관찰 가능성 스택 — 현황 및 갭](#1-현재-utterai-관찰-가능성-스택--현황-및-갭)
2. [고도화 방향 1 — 관찰 가능성 완성](#2-고도화-방향-1--관찰-가능성-완성)
3. [고도화 방향 2 — ADOT 전환 및 Auto-instrumentation](#3-고도화-방향-2--adot-전환-및-auto-instrumentation)
4. [고도화 방향 3 — 데이터 흐름 모니터링 & VOC](#4-고도화-방향-3--데이터-흐름-모니터링--voc)
5. [고도화 방향 4 — AI 기반 장애 자동 분석](#5-고도화-방향-4--ai-기반-장애-자동-분석)
6. [적용 우선순위 및 로드맵](#6-적용-우선순위-및-로드맵)

---

## 1. 현재 UtterAI 관찰 가능성 스택 — 현황 및 갭

### 1-1. 현재 전체 데이터 흐름

```
┌─────────────────────────────────────────────────────────────┐
│  앱 Pod (FastAPI BE / CPU Worker / GPU Worker)              │
│                                                             │
│  Python logging → stdout/stderr ──────────────────────────┐ │
│  OTel SDK → OTLP HTTP ────────────────────────────────────┤ │
└───────────────────────────────────────────────────────────┼─┘
                                                            │
              ┌─────────────────────────────────────────────┤
              │                                             │
              ▼ (OTLP HTTP :4318)                          ▼ (stdout)
   ┌──────────────────────┐                    ┌──────────────────┐
   │   otel-collector     │                    │    Promtail      │
   │ (utterai-observability)                   │  (DaemonSet)     │
   │                      │                    └────────┬─────────┘
   │  traces pipeline ────┼──→ Tempo                   │
   │  metrics pipeline ───┼──→ Prometheus (exporter)   │
   │  logs pipeline ──────┼──→ debug only ⚠️           │
   └──────────────────────┘                            │
                                                       ▼
                                            ┌──────────────────┐
                                            │  Loki            │
                                            │  (S3 backend:    │
                                            │  utterai-{env}   │
                                            │  -loki)          │
                                            └────────┬─────────┘
                                                     │
                          ┌──────────────────────────┘
                          ▼
               ┌──────────────────┐
               │    Grafana       │  ← Tempo / Prometheus / Loki 모두 연결
               └──────────────────┘
```

### 1-2. 컴포넌트별 현황 상세

#### OTel Collector (`k8s/platform/observability/base/otel-collector.yaml`)

현재 파이프라인:

| 파이프라인 | Receiver | Processor | Exporter | 상태 |
|---|---|---|---|---|
| traces | otlp | memory_limiter, batch | otlp/tempo | ✅ 정상 |
| metrics | otlp | memory_limiter, batch | prometheus(exporter :8889) | ✅ 정상 |
| logs | otlp | memory_limiter, batch | **debug only** | ⚠️ 유실 |

**logs pipeline 문제**: 앱이 `logger.info("presigned URL 생성 완료", extra={"bucket": "..."})`로 찍는 구조화 JSON 로그가 OTel로 전송되지만 `debug` exporter는 collector 콘솔에만 출력하고 저장하지 않음. Loki에 도달 안 함.

#### Promtail (stdout 수집)

Pod의 `stdout/stderr`만 수집. 앱이 Python `logging` 모듈로 터미널에 출력하는 텍스트 로그는 여기서 Loki에 들어감. 하지만:
- 구조화 JSON 필드(trace_id, user_id 등)가 string으로 통째로 저장됨 → Loki에서 필드별 필터링 불가
- Trace ID가 로그에 없으면 Tempo trace와 연결 불가

#### Tempo (traces)

OTel Collector → Tempo 전송은 정상. 단, Loki 로그와 연결(Derived Field)이 미설정이라 Grafana에서 trace → log 이동 불가.

#### Prometheus (metrics)

OTel Collector prometheus exporter(:8889)로 앱 메트릭 노출 중. 현재 FastAPI 기본 HTTP 메트릭만 있고, 비즈니스 메트릭(업로드 성공률, SQS 처리 지연 등) 미계측.

### 1-3. 갭 요약

| 영역 | 갭 | 영향 |
|---|---|---|
| **구조화 로그** | OTel logs → Loki 미연결 | 앱 JSON 로그 유실, 필드 검색 불가 |
| **Trace-Log 연결** | Trace ID가 로그에 없음 | 장애 시 trace → log 드릴다운 불가 |
| **비즈니스 메트릭** | 커스텀 메트릭 미계측 | 업로드 실패율, SQS 지연 등 파악 불가 |
| **Alertmanager 룰** | 룰 미정의 | 장애 발생해도 Slack 알림 없음 |
| **장애 데이터 보존** | Pod 삭제 시 로그 소멸 | 사후 분석 불가 |
| **야간 장애 대응** | 수동 감시 | KEDA scale-to-zero 후 무인 상태 |

---

## 2. 고도화 방향 1 — 관찰 가능성 완성

### 2-1. OTel Collector logs pipeline → Loki 연결

**파일**: `k8s/platform/observability/base/otel-collector.yaml`

현재 config.yaml의 exporters와 service 섹션을 수정:

```yaml
# otel-collector-config ConfigMap 내부 config.yaml

exporters:
  debug:
    verbosity: basic
  otlp/tempo:
    endpoint: tempo.monitoring.svc.cluster.local:4317
    tls:
      insecure: true
  prometheus:
    endpoint: 0.0.0.0:8889

  # 추가: Loki exporter
  loki:
    endpoint: http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push
    default_labels_enabled:
      exporter: false
      job: true
    # OTel 리소스 속성을 Loki 레이블로 매핑
    labels:
      resource:
        service.name: "service_name"
        deployment.environment: "environment"
        k8s.namespace.name: "namespace"
        k8s.pod.name: "pod"

service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [otlp/tempo]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheus, debug]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [loki]            # debug → loki 교체
```

적용 후 Loki에서 UtterAI 앱 로그 쿼리 예시:
```logql
# BE API 에러 로그만 조회
{service_name="backend", environment="prod"} |= "ERROR"

# presigned URL 관련 로그
{service_name="backend"} |~ "presigned|S3_BUCKET|RAW_AUDIO"

# GPU worker 추론 완료 로그
{service_name="ml-gpu-worker"} |= "inference_complete"
```

### 2-2. BE 앱 구조화 로그 + Trace ID 삽입

`Utterai_BE` 앱이 OTel SDK로 계측되어 있으므로, 로그에 trace_id만 붙이면 Tempo ↔ Loki 연결이 완성됩니다.

**파일**: `Utterai_BE/app/core/logging.py` (신규 생성)

```python
import logging
import json
from opentelemetry import trace


class OtelJsonFormatter(logging.Formatter):
    """
    Python 로그를 OTel 친화적인 구조화 JSON으로 출력.
    현재 span context에서 trace_id / span_id를 자동 주입.
    """

    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span()
        ctx = span.get_span_context()

        log_record = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            # OTel trace context
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else "",
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else "",
        }

        # extra 필드 병합 (예: logger.info("msg", extra={"user_id": "..."}))
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno",
                "pathname", "filename", "module", "exc_info",
                "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread",
                "threadName", "processName", "process", "message",
            ):
                log_record[key] = val

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_record, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(OtelJsonFormatter())

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(handler)
```

**파일**: `Utterai_BE/app/main.py` — 앱 시작 시 적용

```python
from app.core.logging import configure_logging
from app.core.config import get_settings

settings = get_settings()

# OTel SDK 초기화보다 먼저 실행되어야 span context가 로그에 잡힘
configure_logging(level=settings.log_level)
```

**사용 예시** (기존 코드 변경 없이 동작):

```python
# app/services/audio.py
import logging
logger = logging.getLogger(__name__)

def create_presigned_upload(self, request, user):
    logger.info(
        "presigned URL 생성",
        extra={
            "bucket": settings.raw_audio_bucket,
            "user_id": str(user.id),
            "session_id": str(request.session_id),
        }
    )
```

Loki에서 이 로그:
```json
{
  "timestamp": "2026-06-24T12:30:00.123Z",
  "level": "INFO",
  "logger": "app.services.audio",
  "message": "presigned URL 생성",
  "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
  "span_id": "00f067aa0ba902b7",
  "bucket": "utterai-prod-raw-audio",
  "user_id": "cbc3e0fd-61cc-...",
  "session_id": "ee607124-e7c3-..."
}
```

### 2-3. Grafana Trace ↔ Log 연결 설정

Grafana UI에서 다음 두 가지 설정:

**① Loki 데이터 소스 → Derived Fields**

```
Data Sources → Loki → Derived Fields → Add

Name:       TraceID
Type:       Regex
Regex:      "trace_id":"(\w+)"
URL:        http://tempo.monitoring.svc.cluster.local:3100/api/traces/${__value.raw}
URL Label:  Tempo에서 열기
```

설정 후: Loki 로그 한 줄에서 trace_id 클릭 → Tempo의 해당 trace 자동 이동

**② Tempo 데이터 소스 → Loki 연결**

```
Data Sources → Tempo → Trace to logs

Data source:    Loki
Tags:           service.name
Mapped tags:    service.name → service_name
Filter by trace ID: ON
```

설정 후: Tempo trace span에서 "Logs for this span" 클릭 → 해당 시간대 Loki 로그 자동 조회

### 2-4. 비즈니스 메트릭 계측 (`Utterai_BE`)

현재 FastAPI 기본 HTTP 메트릭(요청 수, 지연시간)만 있고 비즈니스 레벨 메트릭이 없습니다. Prometheus에서 UtterAI 고유 지표를 조회하려면 아래를 추가해야 합니다.

**파일**: `Utterai_BE/app/core/metrics.py` (신규 생성)

```python
from opentelemetry import metrics

_meter = metrics.get_meter("utterai.backend", version="1.0.0")

# ── 음성 업로드 ────────────────────────────────────────────
audio_upload_counter = _meter.create_counter(
    name="audio_upload_total",
    description="음성 파일 presigned URL 발급 요청 수",
    unit="1",
)

audio_upload_duration = _meter.create_histogram(
    name="audio_upload_presign_duration_seconds",
    description="presigned URL 생성 소요 시간",
    unit="s",
)

# ── SQS 디스패치 ───────────────────────────────────────────
sqs_dispatch_counter = _meter.create_counter(
    name="sqs_dispatch_total",
    description="SQS 메시지 발송 결과 (labels: queue, status)",
    unit="1",
)

# ── 세션 상태 전이 ─────────────────────────────────────────
session_status_counter = _meter.create_counter(
    name="session_status_transition_total",
    description="세션 상태 변경 횟수 (labels: from_status, to_status)",
    unit="1",
)

# ── GPU 추론 대기 시간 ─────────────────────────────────────
gpu_inference_wait = _meter.create_histogram(
    name="gpu_inference_queue_wait_seconds",
    description="SQS 메시지 enqueue → GPU worker 처리 시작까지 대기 시간",
    unit="s",
)
```

**사용 예시**:

```python
# app/services/audio.py
import time
from app.core.metrics import audio_upload_counter, audio_upload_duration

def create_presigned_upload(self, request, user):
    start = time.monotonic()
    try:
        url = self.s3_client.generate_upload_url(...)
        audio_upload_counter.add(1, {"status": "success"})
        return url
    except Exception:
        audio_upload_counter.add(1, {"status": "error"})
        raise
    finally:
        audio_upload_duration.record(
            time.monotonic() - start,
            {"bucket": settings.raw_audio_bucket}
        )
```

### 2-5. Prometheus 스크레이프 설정 — UtterAI 앱 대상 추가

현재 OTel Collector가 prometheus exporter(:8889)를 노출하지만, ServiceMonitor나 scrape config가 UtterAI 앱 pod를 직접 타겟으로 잡고 있지 않습니다.

**파일**: `k8s/platform/observability/base/service-monitor-utterai.yaml` (신규)

```yaml
# BE API 메트릭 수집
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: utterai-backend
  namespace: utterai-observability
spec:
  namespaceSelector:
    matchNames:
      - utterai-prod-api
  selector:
    matchLabels:
      app.kubernetes.io/name: utterai-api
  endpoints:
    - port: http
      path: /metrics            # FastAPI + prometheus-fastapi-instrumentator
      interval: 30s
---
# GPU/CPU Worker 메트릭 수집
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: utterai-ai-workers
  namespace: utterai-observability
spec:
  namespaceSelector:
    matchNames:
      - utterai-ai-gpu
      - utterai-ai-cpu
  selector:
    matchLabels:
      app.kubernetes.io/part-of: utterai
  endpoints:
    - port: metrics
      path: /metrics
      interval: 30s
```

> **참고**: FastAPI에서 `/metrics` 엔드포인트를 노출하려면 `prometheus-fastapi-instrumentator` 패키지 추가 후 `Instrumentator().instrument(app).expose(app)` 한 줄이면 됩니다.

### 2-6. Alertmanager 룰 정의

현재 Alertmanager는 구성됐지만 실제 알림 룰이 없습니다. UtterAI 서비스에 맞는 PrometheusRule을 추가합니다.

**파일**: `k8s/platform/observability/base/alert-rules-utterai.yaml` (신규)

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: utterai-alerts
  namespace: utterai-observability
  labels:
    app: kube-prometheus-stack
    release: kube-prometheus-stack
spec:
  groups:
    # ── Pod 상태 이상 ─────────────────────────────────────────────────
    - name: utterai.pod
      rules:
        - alert: UtterAIPodCrashLoopBackOff
          expr: |
            kube_pod_container_status_waiting_reason{
              reason="CrashLoopBackOff",
              namespace=~"utterai-.*"
            } == 1
          for: 2m
          labels:
            severity: critical
            team: utterai
          annotations:
            summary: "Pod CrashLoopBackOff: {{ $labels.pod }}"
            description: |
              {{ $labels.namespace }}/{{ $labels.pod }} 컨테이너 {{ $labels.container }}가
              CrashLoopBackOff 상태입니다. kubectl logs {{ $labels.pod }} -n {{ $labels.namespace }} --previous 로 확인하세요.

        - alert: UtterAIPodPendingTooLong
          expr: |
            kube_pod_status_phase{
              phase="Pending",
              namespace=~"utterai-.*"
            } == 1
          for: 5m
          labels:
            severity: warning
            team: utterai
          annotations:
            summary: "Pod Pending 5분 초과: {{ $labels.pod }}"
            description: |
              {{ $labels.namespace }}/{{ $labels.pod }} 이 5분 이상 Pending 상태입니다.
              Karpenter 노드 프로비저닝 실패 또는 리소스 부족일 수 있습니다.
              kubectl describe pod {{ $labels.pod }} -n {{ $labels.namespace }} 로 Events 확인하세요.

        - alert: UtterAIGPUWorkerOOMKilled
          expr: |
            kube_pod_container_status_last_terminated_reason{
              reason="OOMKilled",
              namespace="utterai-ai-gpu"
            } == 1
          for: 0m
          labels:
            severity: critical
            team: utterai
          annotations:
            summary: "GPU Worker OOMKilled: {{ $labels.pod }}"
            description: |
              {{ $labels.pod }} 컨테이너가 메모리 한계(14Gi)를 초과해 강제 종료됐습니다.
              ml-gpu-worker-deployment.yaml의 memory limit 조정이 필요할 수 있습니다.

    # ── API 서비스 품질 ────────────────────────────────────────────────
    - name: utterai.api
      rules:
        - alert: UtterAIAPIHighErrorRate
          expr: |
            (
              sum(rate(http_requests_total{
                namespace="utterai-prod-api",
                status=~"5.."
              }[5m]))
              /
              sum(rate(http_requests_total{
                namespace="utterai-prod-api"
              }[5m]))
            ) > 0.05
          for: 3m
          labels:
            severity: critical
            team: utterai
          annotations:
            summary: "BE API 5xx 에러율 5% 초과"
            description: "최근 5분간 5xx 에러율이 {{ $value | humanizePercentage }}입니다."

        - alert: UtterAIAPIHighLatency
          expr: |
            histogram_quantile(0.95,
              sum(rate(http_request_duration_seconds_bucket{
                namespace="utterai-prod-api"
              }[5m])) by (le)
            ) > 3
          for: 5m
          labels:
            severity: warning
            team: utterai
          annotations:
            summary: "BE API p95 응답시간 3초 초과"
            description: "p95 응답시간이 {{ $value }}초입니다."

    # ── SQS / KEDA ────────────────────────────────────────────────────
    - name: utterai.sqs
      rules:
        - alert: UtterAIGPUQueueDepthHigh
          expr: |
            aws_sqs_approximate_number_of_messages_visible_maximum{
              queue_name="utterai-prod-gpu-inference-queue"
            } > 20
          for: 10m
          labels:
            severity: warning
            team: utterai
          annotations:
            summary: "GPU 추론 큐 적체 ({{ $value }}개)"
            description: |
              GPU inference SQS 큐에 {{ $value }}개 메시지가 10분 이상 적체되어 있습니다.
              GPU worker 스케일링 또는 SCP/쿼터 문제를 확인하세요.

        - alert: UtterAIAudioUploadFailureRateHigh
          expr: |
            (
              sum(increase(audio_upload_total{status="error"}[10m]))
              /
              sum(increase(audio_upload_total[10m]))
            ) > 0.1
          for: 5m
          labels:
            severity: critical
            team: utterai
          annotations:
            summary: "음성 업로드 실패율 10% 초과"
            description: |
              presigned URL 생성 실패율이 {{ $value | humanizePercentage }}입니다.
              S3 버킷 이름 환경변수 또는 IRSA 권한을 확인하세요.
```

Alertmanager Slack 수신자 설정 (`k8s/platform/observability/base/alertmanager-config.yaml`):

```yaml
apiVersion: monitoring.coreos.com/v1alpha1
kind: AlertmanagerConfig
metadata:
  name: utterai-slack
  namespace: utterai-observability
spec:
  route:
    receiver: slack-utterai
    matchers:
      - name: team
        value: utterai
    groupBy: ["alertname", "namespace"]
    groupWait: 30s
    groupInterval: 5m
    repeatInterval: 4h
  receivers:
    - name: slack-utterai
      slackConfigs:
        - apiURL:
            name: alertmanager-slack-secret
            key: webhook-url
          channel: "#alerts-utterai"
          title: "[{{ .Status | toUpper }}] {{ .GroupLabels.alertname }}"
          text: |
            {{ range .Alerts }}
            *요약*: {{ .Annotations.summary }}
            *내용*: {{ .Annotations.description }}
            *심각도*: {{ .Labels.severity }}
            {{ end }}
          sendResolved: true
```

### 2-7. Grafana 대시보드 구성

Grafana ConfigMap으로 대시보드를 코드화하여 관리합니다.

**파일**: `k8s/platform/observability/base/grafana-dashboard-utterai.yaml` (신규)

핵심 패널 구성:

```
UtterAI Service Overview 대시보드
├── Row: API Health
│   ├── 요청 TPS (rate by status code)
│   ├── p50/p95/p99 응답시간
│   ├── 5xx 에러율 (%)
│   └── Active Pod 수 (blue/green 각각)
│
├── Row: Audio Upload Pipeline
│   ├── Presigned URL 생성 성공/실패 (counter)
│   ├── S3 업로드 완료 → SQS 디스패치 지연
│   ├── CPU Worker SQS 큐 depth
│   └── GPU Worker SQS 큐 depth
│
├── Row: GPU Inference
│   ├── GPU Worker Pod 수 (KEDA scale 현황)
│   ├── 추론 완료 처리량 (msg/min)
│   ├── GPU 메모리 사용률 (nvidia_smi 필요 시)
│   └── OOMKilled 발생 횟수
│
└── Row: Infrastructure
    ├── Karpenter NodeClaim 상태
    ├── 노드별 CPU/메모리 사용률
    └── SQS DLQ 메시지 수
```

---

## 3. 고도화 방향 2 — ADOT 전환 및 Auto-instrumentation

### 3-1. ADOT이란 — 현재 vanilla OTel Collector와의 관계

ADOT(AWS Distro for OpenTelemetry)은 vanilla OTel Collector의 **대체재**이지 별도 추가 컴포넌트가 아닙니다.

```
vanilla OTel Collector (현재)          ADOT (전환 후)
─────────────────────────────          ────────────────────────────────────
otel/opentelemetry-collector-contrib   public.ecr.aws/aws-observability/
  이미지를 직접 Deployment으로 운영       aws-otel-collector 이미지
                                       → EKS 애드온으로 AWS가 수명주기 관리
                                          OR ADOT Operator Helm으로 CRD 방식 관리

config.yaml → ConfigMap                config.yaml → OpenTelemetryCollector CRD
(동일한 receivers/processors/exporters 문법 그대로 사용 가능)

자동 계측 없음 (앱 코드에 OTel SDK 직접 추가 필요)
                                       Instrumentation CRD → Pod 어노테이션
                                         하나로 SDK 자동 주입 (코드 수정 없음)
```

**현재 `otel-collector.yaml`은 변경 없이 CRD spec으로 이관됩니다.** 백엔드(Tempo/Prometheus/Loki)도 그대로 유지됩니다.

ADOT 전환으로 추가로 쓸 수 있는 것:

| 기능 | vanilla OTel | ADOT |
|---|---|---|
| Tempo/Prometheus/Loki로 전송 | ✅ | ✅ (동일) |
| X-Ray, CloudWatch로 전송 | ⚠️ 가능하나 미지원 컴포넌트 | ✅ AWS 공식 지원 |
| Auto-instrumentation (코드 수정 없음) | ❌ | ✅ Instrumentation CRD |
| AWS 관리형 수명주기 (EKS 애드온) | ❌ | ✅ |
| Container Insights 통합 | ❌ | ✅ |

### 3-2. 전환 방식 — ADOT Operator (권장)

EKS 애드온 방식도 있지만, 현재 Terraform Helm 기반 스택과 일관성을 유지하는 **ADOT Operator Helm 방식**이 더 적합합니다.

```
현재                                   전환 후
────────────────────────────────────   ──────────────────────────────────────
k8s/platform/observability/base/       k8s/platform/observability/base/
  otel-collector.yaml                    otel-collector-crd.yaml
  (Deployment + ConfigMap + Service)     (OpenTelemetryCollector CRD)
                                         instrumentation-python.yaml
                                         (Instrumentation CRD)

terraform/modules/eks-addons/main.tf   terraform/modules/eks-addons/main.tf
  (없음)                                  + helm_release "adot_operator"
```

#### A. Terraform — ADOT Operator 설치

**파일**: `terraform/modules/eks-addons/main.tf`에 추가

```hcl
# ── ADOT Operator ─────────────────────────────────────────────────────────────
resource "helm_release" "adot_operator" {
  name             = "opentelemetry-operator"
  repository       = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart            = "opentelemetry-operator"
  version          = "0.68.0"
  namespace        = "utterai-observability"
  create_namespace = false   # utterai-observability namespace는 기존에 존재
  cleanup_on_fail  = true
  wait             = true
  wait_for_jobs    = true

  depends_on = [helm_release.kube_prometheus_stack]

  values = [
    yamlencode({
      manager = {
        resources = {
          requests = { cpu = "100m", memory = "128Mi" }
          limits   = { cpu = "500m", memory = "256Mi" }
        }
      }
      # cert-manager 없이 동작하도록 (현재 cert-manager 미사용)
      admissionWebhooks = {
        certManager = {
          enabled = false
        }
        autoGenerateCert = {
          enabled = true
        }
      }
    })
  ]
}
```

#### B. OpenTelemetryCollector CRD — 기존 config 이관

**파일**: `k8s/platform/observability/base/otel-collector-crd.yaml` (신규, 기존 otel-collector.yaml 대체)

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: OpenTelemetryCollector
metadata:
  name: utterai
  namespace: utterai-observability
spec:
  # sidecar / daemonset / statefulset / deployment 중 선택
  # deployment: 현재 방식과 동일 (ClusterIP 서비스 자동 생성됨)
  mode: deployment
  replicas: 1

  resources:
    requests:
      cpu: "100m"
      memory: "256Mi"
    limits:
      cpu: "500m"
      memory: "512Mi"

  # 기존 otel-collector.yaml의 config.yaml 내용을 그대로 이관
  config: |
    receivers:
      otlp:
        protocols:
          grpc:
            endpoint: 0.0.0.0:4317
          http:
            endpoint: 0.0.0.0:4318

    processors:
      batch: {}
      memory_limiter:
        check_interval: 1s
        limit_mib: 256
        spike_limit_mib: 64

    extensions:
      health_check:
        endpoint: 0.0.0.0:13133

    exporters:
      otlp/tempo:
        endpoint: tempo.monitoring.svc.cluster.local:4317
        tls:
          insecure: true
      prometheus:
        endpoint: 0.0.0.0:8889
      loki:
        endpoint: http://loki-gateway.monitoring.svc.cluster.local/loki/api/v1/push
        labels:
          resource:
            service.name: "service_name"
            deployment.environment: "environment"
            k8s.namespace.name: "namespace"
            k8s.pod.name: "pod"

    service:
      extensions: [health_check]
      pipelines:
        traces:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [otlp/tempo]
        metrics:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [prometheus]
        logs:
          receivers: [otlp]
          processors: [memory_limiter, batch]
          exporters: [loki]    # 기존 debug → loki로 변경
```

CRD를 적용하면 ADOT Operator가 자동으로:
- `utterai-collector` Deployment 생성
- `utterai-collector` ClusterIP Service 생성 (포트 4317, 4318)
- ServiceMonitor 생성 (prometheus exporter 자동 스크레이프)

**기존 `otel-collector.yaml`에서 수동 관리하던 Deployment/Service/ServiceMonitor/ConfigMap은 삭제합니다.**

앱의 `OTEL_EXPORTER_OTLP_ENDPOINT`는 Service 이름만 바뀌므로 ConfigMap에서 수정:

```yaml
# k8s/apps/backend/overlays/prod/patch-configmap.yaml
OTEL_EXPORTER_OTLP_ENDPOINT: "http://utterai-collector.utterai-observability.svc.cluster.local:4318"
```

### 3-3. Auto-instrumentation — 핵심 기능

ADOT의 가장 큰 차별점입니다. 현재 UtterAI BE는 `app/main.py`에서 OTel SDK를 직접 초기화하고 있는데, 이를 Operator가 자동 주입하도록 위임할 수 있습니다.

**파일**: `k8s/platform/observability/base/instrumentation-python.yaml` (신규)

```yaml
apiVersion: opentelemetry.io/v1alpha1
kind: Instrumentation
metadata:
  name: utterai-python
  namespace: utterai-observability
spec:
  # 모든 앱이 사용할 기본 OTel 엔드포인트
  exporter:
    endpoint: http://utterai-collector.utterai-observability.svc.cluster.local:4318

  propagators:
    - tracecontext
    - baggage
    - b3

  sampler:
    type: parentbased_traceidratio
    argument: "1.0"    # 100% 샘플링 (prod에서는 0.1~0.2 권장)

  python:
    env:
      - name: OTEL_LOGS_EXPORTER
        value: otlp
      - name: OTEL_METRICS_EXPORTER
        value: otlp
      - name: OTEL_PYTHON_LOG_CORRELATION
        value: "true"    # 로그에 trace_id 자동 삽입
      - name: OTEL_PYTHON_LOG_FORMAT
        value: "%(asctime)s %(levelname)s [%(name)s] [%(filename)s:%(lineno)d] [trace_id=%(otelTraceID)s span_id=%(otelSpanID)s] - %(message)s"
```

**Pod에 어노테이션 추가** (코드 수정 없이 자동 계측 활성화):

```yaml
# k8s/apps/backend/base/deployment-green.yaml
spec:
  template:
    metadata:
      annotations:
        instrumentation.opentelemetry.io/inject-python: "utterai-observability/utterai-python"
```

어노테이션을 추가하면 Operator가 Pod 시작 시 Init Container를 자동 삽입하여:
1. `opentelemetry-distro`, `opentelemetry-exporter-otlp` 패키지 자동 설치
2. `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT` 등 환경변수 자동 주입
3. FastAPI 자동 계측 활성화 (`opentelemetry-instrumentation-fastapi` 포함)

**결과**: 앱 `main.py`에서 직접 호출하던 OTel SDK 초기화 코드(`configure_opentelemetry()` 등)를 제거할 수 있습니다.

### 3-4. 전환 절차 요약

```
1. Terraform: ADOT Operator Helm 추가 → terraform apply
   (기존 앱/관찰 가능성 스택에 영향 없음, Operator만 설치됨)

2. k8s: OpenTelemetryCollector CRD 적용 (ArgoCD sync)
   → utterai-collector Deployment/Service 자동 생성

3. 앱 ConfigMap의 OTEL_EXPORTER_OTLP_ENDPOINT 수정
   (otel-collector → utterai-collector)

4. k8s: 기존 otel-collector.yaml Deployment/Service/ConfigMap 삭제
   (kustomization.yaml에서 제거)

5. Instrumentation CRD 적용

6. 앱 Deployment에 어노테이션 추가
   → 다음 rollout restart 시 자동 계측 활성화
```

> **주의**: 3번에서 엔드포인트가 바뀌므로 ArgoCD sync + rollout restart가 함께 필요합니다.

---

## 4. 고도화 방향 3 — 데이터 흐름 모니터링 & VOC

### 4-1. UtterAI 구조와 MSA 로그 산발 문제

UtterAI는 완전한 MSA(Microservices Architecture)는 아니지만 **Worker별 독립 배포 구조**이기 때문에 MSA가 겪는 동일한 문제를 가집니다.

**현재 실제 데이터 흐름:**

```
[사용자]
  │ 음성 파일 업로드 요청
  ▼
[utterai-api]  utterai-prod-api 네임스페이스
  │ presigned URL 반환 → 사용자가 S3에 직접 업로드
  │ S3 업로드 완료 콜백 수신
  │ → SQS audio-preprocess-queue 메시지 발송
  ▼                      ↑ 여기서 trace 끊김 (비동기 경계)
[utterai-cpu-worker]  utterai-ai-cpu 네임스페이스
  │ SQS audio-preprocess-queue 소비
  │ 음성 전처리 (노이즈 제거, 구간 분할 등)
  │ → SQS gpu-inference-queue 메시지 발송
  ▼                      ↑ 여기서 trace 또 끊김
[utterai-ml-gpu-worker]  utterai-ai-gpu 네임스페이스
  │ SQS gpu-inference-queue 소비
  │ ML 추론 (STT, 분석)
  │ → SQS report-analysis-queue 메시지 발송
  │ → S3 결과 저장
  ▼
[DB/결과 반환]

[utterai-batch-worker]  utterai-batch 네임스페이스 (별도 흐름)
  RAG 문서 수집 → SQS rag-ingest-queue → 벡터 DB 적재
```

**MSA가 아니더라도 동일한 문제가 발생하는 이유:**

| 특성 | 완전한 MSA | UtterAI 현재 |
|---|---|---|
| 서비스 간 통신 방식 | HTTP API 또는 메시지 큐 | **SQS** (비동기 큐) |
| 배포 단위 분리 | 서비스별 독립 배포 | **Worker별 독립 배포** |
| 네임스페이스 분리 | 서비스별 분리 | **utterai-ai-cpu / utterai-ai-gpu / utterai-batch** 분리 |
| 로그 위치 | 서비스별 분산 | **각 Worker Pod에 분산** |
| Trace 연속성 | 동기 호출 시 자동 전파 | **SQS 경계에서 자동 끊김** |

**결론: 완전한 MSA와 동일한 문제 발생, 오히려 SQS 특성상 더 명시적인 처리 필요**

---

현재 VOC(사용자 불만) 대응 시 실제 발생하는 상황:

```
"제 녹음 분석이 안 나왔어요"
  ↓
kubectl logs -n utterai-prod-api <pod> | grep "user_id" → 해당 session 찾기 시도
  ↓
kubectl logs -n utterai-ai-cpu <pod> | grep "session_id" → 연결 안 됨 (다른 trace)
  ↓
kubectl logs -n utterai-ai-gpu <pod> | grep "session_id" → 연결 안 됨
  ↓
SQS 콘솔에서 DLQ 확인 → 메시지 이미 삭제됨 (가시성 타임아웃 후 소멸)
  ↓
원인 파악 불가
```

### 4-2. 해결 아키텍처 — session_id 기반 데이터 흐름 추적

**핵심 원칙**: `session_id` + `user_id`를 모든 SQS 메시지 body와 모든 로그에 포함시키면 Elasticsearch에서 `session_id` 하나로 전체 흐름 조회 가능.

```
┌───────────────────────────────────────────────────────────────────┐
│  앱 레이어 변경 (상관 ID 전파)                                      │
│                                                                   │
│  API → SQS 메시지에 session_id + user_id + traceparent 포함       │
│  CPU Worker → SQS 메시지 수신 시 이 값들을 추출하여 로그에 삽입     │
│  GPU Worker → 동일                                                 │
└───────────────────────────────────────────────────────────────────┘
                    │ 구조화 JSON 로그 (stdout)
                    ▼
┌───────────────────────────────────────────────────────────────────┐
│  로그 수집 레이어                                                   │
│                                                                   │
│  Fluent Bit DaemonSet                                             │
│  ├── /var/log/containers/utterai-*.log 수집                       │
│  ├── JSON 파싱 → session_id, user_id 필드 추출                    │
│  ├── → Loki (기존, 인프라 모니터링용)                              │
│  └── → AWS OpenSearch (신규, 데이터 흐름/VOC용)                   │
└───────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌───────────────────────────────────────────────────────────────────┐
│  AWS OpenSearch                                                    │
│                                                                   │
│  인덱스: utterai-sessions-{YYYY.MM}                               │
│  ├── session_id: "xyz789"                                         │
│  ├── user_id: "abc123"                                            │
│  ├── service: "backend" / "cpu-worker" / "ml-gpu-worker"          │
│  ├── event: "audio_uploaded" / "preprocess_started" / ...         │
│  ├── status: "success" / "error" / "pending"                      │
│  └── duration_ms, error_message, trace_id                         │
│                                                                   │
│  OpenSearch Dashboard                                             │
│  ├── 세션 타임라인 뷰 (session_id 검색)                            │
│  ├── 사용자 이력 뷰 (user_id 검색)                                 │
│  ├── 에러 패턴 뷰 (어느 Worker에서 실패가 많은가)                   │
│  └── 처리 시간 분포 (각 단계별 소요시간)                            │
└───────────────────────────────────────────────────────────────────┘
```

### 4-3. 앱 코드 변경 — SQS 상관 ID 전파

이 섹션은 **앱 레포(UtterAI_BE, UtterAI_AI)에서 수행할 변경사항**입니다. 인프라 변경보다 선행되어야 합니다.

#### A. SQS 메시지 발송 시 상관 ID 포함 (BE API)

```python
# Utterai_BE/app/services/audio.py

from opentelemetry import trace
from opentelemetry.propagate import inject

def dispatch_to_preprocess(self, session_id: str, user_id: str, s3_key: str) -> None:
    # 현재 span의 trace context를 carrier로 추출 (W3C traceparent 형식)
    carrier: dict[str, str] = {}
    inject(carrier)

    message_body = {
        "session_id": session_id,
        "user_id": user_id,
        "s3_key": s3_key,
        "enqueued_at": datetime.utcnow().isoformat(),
        # OTel trace context — CPU Worker에서 span을 이어받을 때 사용
        "traceparent": carrier.get("traceparent", ""),
    }

    self.sqs_client.send_message(
        QueueUrl=settings.sqs_audio_preprocess_queue_url,
        MessageBody=json.dumps(message_body),
        # MessageAttributes에도 추가 (OTel auto-instrumentation 호환)
        MessageAttributes={
            "traceparent": {
                "StringValue": carrier.get("traceparent", ""),
                "DataType": "String",
            }
        },
    )
    logger.info(
        "SQS 디스패치 완료",
        extra={"session_id": session_id, "user_id": user_id, "queue": "audio-preprocess"},
    )
```

#### B. SQS 메시지 수신 시 trace 이어받기 (CPU Worker)

```python
# UtterAI_AI/app/workers/cpu_worker.py

from opentelemetry import trace
from opentelemetry.propagate import extract

tracer = trace.get_tracer("cpu-worker")

def handle_message(message: dict) -> None:
    body = json.loads(message["Body"])
    session_id = body["session_id"]
    user_id    = body["user_id"]
    s3_key     = body["s3_key"]

    # API에서 전달받은 trace context 복원
    carrier = {"traceparent": body.get("traceparent", "")}
    parent_ctx = extract(carrier)

    with tracer.start_as_current_span(
        "cpu_worker.preprocess",
        context=parent_ctx,          # API trace의 child span으로 시작
        kind=trace.SpanKind.CONSUMER,
    ) as span:
        span.set_attribute("session_id", session_id)
        span.set_attribute("user_id", user_id)

        logger.info(
            "전처리 시작",
            extra={"session_id": session_id, "user_id": user_id, "s3_key": s3_key},
        )

        try:
            result = preprocess_audio(s3_key)

            # GPU Worker로 연결 시 다시 trace context 주입
            dispatch_to_gpu_inference(session_id, user_id, result)

            logger.info(
                "전처리 완료 → GPU 큐 디스패치",
                extra={"session_id": session_id, "user_id": user_id, "duration_ms": ...},
            )
        except Exception as e:
            span.record_exception(e)
            logger.error(
                "전처리 실패",
                extra={"session_id": session_id, "user_id": user_id, "error": str(e)},
            )
            raise
```

GPU Worker도 동일한 패턴으로 CPU Worker의 traceparent를 이어받아 단일 trace로 연결됩니다.

**변경 후 Tempo에서 보이는 trace 구조:**

```
utterai-api (span: handle_upload)
  └── utterai-cpu-worker (span: cpu_worker.preprocess)   ← SQS 경계 넘어 연결됨
        └── utterai-ml-gpu-worker (span: gpu_worker.inference)  ← 하나의 trace로 통합
```

### 4-4. Terraform — AWS OpenSearch 도메인

**파일**: `terraform/modules/opensearch/main.tf` (신규 모듈)

```hcl
locals {
  prefix = "${var.project_name}-${var.environment}"
}

resource "aws_opensearch_domain" "utterai" {
  domain_name    = "${local.prefix}-opensearch"
  engine_version = "OpenSearch_2.11"

  cluster_config {
    instance_type  = "t3.medium.search"
    instance_count = 1
    # 운영 안정성 필요 시: dedicated_master_enabled = true, instance_count = 3
  }

  ebs_options {
    ebs_enabled = true
    volume_size = 50     # 세션 로그 90일 보존 기준 (트래픽에 따라 조정)
    volume_type = "gp3"
    throughput  = 125
  }

  vpc_options {
    subnet_ids         = [var.private_app_subnet_ids[0]]
    security_group_ids = [aws_security_group.opensearch.id]
  }

  encrypt_at_rest {
    enabled = true
  }

  node_to_node_encryption {
    enabled = true
  }

  domain_endpoint_options {
    enforce_https       = true
    tls_security_policy = "Policy-Min-TLS-1-2-2019-07"
  }

  advanced_security_options {
    enabled                        = true
    anonymous_auth_enabled         = false
    internal_user_database_enabled = false  # IAM 인증만 사용
    master_user_options {
      master_user_arn = var.opensearch_admin_role_arn  # 관리자 IAM Role
    }
  }

  log_publishing_options {
    log_type                 = "INDEX_SLOW_LOGS"
    cloudwatch_log_group_arn = aws_cloudwatch_log_group.opensearch.arn
  }

  tags = {
    Environment = var.environment
    Project     = var.project_name
    Purpose     = "voc-data-flow-monitoring"
  }
}

resource "aws_opensearch_domain_policy" "utterai" {
  domain_name = aws_opensearch_domain.utterai.domain_name

  access_policies = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { AWS = var.fluent_bit_irsa_role_arn }
        Action    = ["es:ESHttpPost", "es:ESHttpPut"]
        Resource  = "${aws_opensearch_domain.utterai.arn}/*"
      },
      {
        Effect    = "Allow"
        Principal = { AWS = var.opensearch_admin_role_arn }
        Action    = "es:*"
        Resource  = "${aws_opensearch_domain.utterai.arn}/*"
      }
    ]
  })
}

resource "aws_security_group" "opensearch" {
  name        = "${local.prefix}-opensearch-sg"
  description = "OpenSearch domain security group"
  vpc_id      = var.vpc_id

  ingress {
    description     = "HTTPS from EKS nodes"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [var.node_security_group_id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_cloudwatch_log_group" "opensearch" {
  name              = "/aws/opensearch/${local.prefix}"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_resource_policy" "opensearch" {
  policy_name = "${local.prefix}-opensearch-logs"

  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "es.amazonaws.com" }
      Action    = ["logs:PutLogEvents", "logs:CreateLogStream"]
      Resource  = "${aws_cloudwatch_log_group.opensearch.arn}:*"
    }]
  })
}
```

**파일**: `terraform/modules/opensearch/outputs.tf`

```hcl
output "opensearch_endpoint" {
  value = aws_opensearch_domain.utterai.endpoint
}

output "opensearch_domain_arn" {
  value = aws_opensearch_domain.utterai.arn
}
```

**파일**: `terraform/environments/prod/03-services/main.tf`에 모듈 추가

```hcl
module "opensearch" {
  source = "../../../modules/opensearch"

  project_name             = var.project_name
  environment              = var.environment
  vpc_id                   = data.terraform_remote_state.network.outputs.vpc_id
  private_app_subnet_ids   = data.terraform_remote_state.network.outputs.private_app_subnet_ids
  node_security_group_id   = data.terraform_remote_state.eks.outputs.node_security_group_id
  fluent_bit_irsa_role_arn = module.irsa.fluent_bit_role_arn
  opensearch_admin_role_arn = "arn:aws:iam::032886669461:role/utterai-prod-admin"
}
```

### 4-5. Terraform — Fluent Bit IRSA Role

**파일**: `terraform/modules/irsa/main.tf`에 추가

```hcl
# ── Fluent Bit IRSA (OpenSearch 로그 전송) ────────────────────────────────────

resource "aws_iam_role" "fluent_bit" {
  name = "${local.prefix}-fluent-bit-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:utterai-observability:fluent-bit"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "fluent_bit" {
  name = "${local.prefix}-fluent-bit-policy"
  role = aws_iam_role.fluent_bit.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "OpenSearchWrite"
        Effect   = "Allow"
        Action   = ["es:ESHttpPost", "es:ESHttpPut", "es:ESHttpGet"]
        Resource = "${var.opensearch_domain_arn}/*"
      }
    ]
  })
}

output "fluent_bit_role_arn" {
  value = aws_iam_role.fluent_bit.arn
}
```

### 4-6. K8s — Fluent Bit DaemonSet (OpenSearch 전송)

현재 Promtail(Loki용)은 그대로 유지하고, Fluent Bit을 **OpenSearch 전용**으로 추가합니다. 두 컴포넌트는 독립적으로 동작합니다.

**파일**: `k8s/platform/observability/base/fluent-bit-opensearch.yaml` (신규)

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: fluent-bit
  namespace: utterai-observability
  annotations:
    eks.amazonaws.com/role-arn: PLACEHOLDER   # overlay에서 실제 ARN으로 교체
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: fluent-bit-opensearch-config
  namespace: utterai-observability
data:
  fluent-bit.conf: |
    [SERVICE]
        Flush         1
        Log_Level     info
        Parsers_File  parsers.conf

    # utterai 앱 Pod 로그만 수집
    [INPUT]
        Name              tail
        Tag               utterai.*
        Path              /var/log/containers/utterai-*.log
        Multiline.Parser  docker, cri
        DB                /var/log/flb_utterai.db
        Mem_Buf_Limit     10MB
        Skip_Long_Lines   On
        Refresh_Interval  5

    # K8s 메타데이터 보강 (namespace, pod_name, container_name)
    [FILTER]
        Name                kubernetes
        Match               utterai.*
        Kube_URL            https://kubernetes.default.svc:443
        Kube_CA_File        /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
        Kube_Token_File     /var/run/secrets/kubernetes.io/serviceaccount/token
        Merge_Log           On        # JSON 로그를 필드로 파싱
        Keep_Log            Off
        K8S-Logging.Parser  On
        K8S-Logging.Exclude On
        Labels              Off
        Annotations         Off

    # session_id가 있는 로그만 OpenSearch로 (데이터 흐름 로그만 선별)
    [FILTER]
        Name    grep
        Match   utterai.*
        Regex   session_id .+

    # OpenSearch로 전송 (AWS SigV4 인증)
    [OUTPUT]
        Name              opensearch
        Match             utterai.*
        Host              ${OPENSEARCH_ENDPOINT}
        Port              443
        TLS               On
        AWS_Auth          On
        AWS_Region        ap-northeast-2
        AWS_Service       es
        Index             utterai-sessions
        Logstash_Format   On
        Logstash_Prefix   utterai-sessions
        Logstash_DateFormat %Y.%m
        Time_Key          timestamp
        Time_Key_Format   %Y-%m-%dT%H:%M:%S.%L
        Replace_Dots      On
        Retry_Limit       3
        Buffer_Size       5MB

  parsers.conf: |
    [PARSER]
        Name        json
        Format      json
        Time_Key    time
        Time_Format %Y-%m-%dT%H:%M:%S.%L
---
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluent-bit-opensearch
  namespace: utterai-observability
spec:
  selector:
    matchLabels:
      app: fluent-bit-opensearch
  template:
    metadata:
      labels:
        app: fluent-bit-opensearch
    spec:
      serviceAccountName: fluent-bit
      tolerations:
        - operator: Exists    # 모든 노드에 스케줄 (GPU 노드 포함)
      containers:
        - name: fluent-bit
          image: public.ecr.aws/aws-observability/aws-for-fluent-bit:stable
          env:
            - name: OPENSEARCH_ENDPOINT
              value: PLACEHOLDER    # overlay에서 실제 엔드포인트로 교체
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 256Mi
          volumeMounts:
            - name: varlog
              mountPath: /var/log
            - name: fluent-bit-config
              mountPath: /fluent-bit/etc/
      volumes:
        - name: varlog
          hostPath:
            path: /var/log
        - name: fluent-bit-config
          configMap:
            name: fluent-bit-opensearch-config
```

**파일**: `k8s/platform/observability/overlays/prod/patch-fluent-bit.yaml` (신규)

```yaml
- op: replace
  path: /spec/template/spec/serviceAccountName
  value: fluent-bit
- op: replace
  path: /spec/template/spec/containers/0/env/0/value
  value: "search-utterai-prod-opensearch-xxxxxxx.ap-northeast-2.es.amazonaws.com"
```

### 4-7. OpenSearch 인덱스 설계 & 대시보드

#### 인덱스 매핑

OpenSearch에 인덱스 템플릿을 등록합니다 (최초 1회, curl 또는 OpenSearch Dashboard Dev Tools):

```json
PUT _index_template/utterai-sessions
{
  "index_patterns": ["utterai-sessions-*"],
  "template": {
    "settings": {
      "number_of_shards": 1,
      "number_of_replicas": 0,
      "index.lifecycle.name": "utterai-sessions-ilm"
    },
    "mappings": {
      "properties": {
        "timestamp":     { "type": "date" },
        "session_id":    { "type": "keyword" },
        "user_id":       { "type": "keyword" },
        "trace_id":      { "type": "keyword" },
        "service":       { "type": "keyword" },
        "event":         { "type": "keyword" },
        "status":        { "type": "keyword" },
        "level":         { "type": "keyword" },
        "message":       { "type": "text", "analyzer": "standard" },
        "duration_ms":   { "type": "long" },
        "error_message": { "type": "text" },
        "s3_key":        { "type": "keyword" },
        "queue":         { "type": "keyword" },
        "kubernetes": {
          "properties": {
            "namespace_name": { "type": "keyword" },
            "pod_name":       { "type": "keyword" }
          }
        }
      }
    }
  }
}
```

ILM 정책 (90일 보존):

```json
PUT _ilm/policy/utterai-sessions-ilm
{
  "policy": {
    "phases": {
      "hot":    { "actions": { "rollover": { "max_age": "30d", "max_size": "10gb" } } },
      "delete": { "min_age": "90d", "actions": { "delete": {} } }
    }
  }
}
```

#### OpenSearch Dashboard — 핵심 뷰 3종

**① 세션 타임라인 뷰** (VOC 대응 시 가장 많이 사용)

```
검색: session_id: "xyz789"

결과:
timestamp            service          event                   status    duration_ms
─────────────────────────────────────────────────────────────────────────────────
2026-06-24 10:00:01  backend          audio_upload_requested  success   12ms
2026-06-24 10:00:02  backend          sqs_dispatched          success   5ms
2026-06-24 10:00:03  cpu-worker       preprocess_started      success   -
2026-06-24 10:00:45  cpu-worker       preprocess_complete     success   42,000ms
2026-06-24 10:00:46  cpu-worker       gpu_sqs_dispatched      success   8ms
2026-06-24 10:00:47  ml-gpu-worker    inference_started       success   -
2026-06-24 10:02:15  ml-gpu-worker    inference_complete      success   88,000ms
2026-06-24 10:02:16  ml-gpu-worker    result_saved            success   120ms
```

**② 에러 패턴 분석 뷰**

```
집계: status: error, 기간: 최근 7일
그룹: service 별 에러 수 + 대표 error_message
→ 어느 Worker에서 실패가 집중되는지 파악
```

**③ 처리 시간 분포 뷰**

```
집계: event: inference_complete, duration_ms 분포
P50 / P95 / P99 → GPU 추론 지연 분포 파악
→ SLA 기준 설정 및 이상 감지
```

### 4-8. VOC 관리 단계별 로드맵

| 단계 | 내용 | 구현 위치 |
|---|---|---|
| **즉시** | SQS 메시지에 session_id/user_id 추가 | UtterAI_BE + UtterAI_AI 앱 코드 |
| **즉시** | Worker 로그에 session_id/user_id 항상 포함 | UtterAI_AI 앱 코드 |
| **단기** | AWS OpenSearch 도메인 + Fluent Bit 파이프라인 | Terraform + K8s |
| **단기** | SQS 경계 trace 연결 (traceparent 전파) | UtterAI_BE + UtterAI_AI 앱 코드 |
| **중기** | OpenSearch Dashboard 3종 뷰 구성 | OpenSearch 콘솔 |
| **중기** | 관리자 페이지 "세션 추적" 탭 | UtterAI_FE + UtterAI_BE (OpenSearch API 쿼리) |
| **장기** | VOC 티켓 수신 → session_id 자동 연결 | 어드민 시스템 |
| **장기** | 담당자 자동 배정 (에러 발생 Worker 기준) | 어드민 시스템 |

---

## 5. 고도화 방향 4 — AI 기반 장애 자동 분석

### 4-1. 왜 필요한가 — UtterAI 현황

현재 장애 감지 → 대응 흐름:

```
사용자 신고 or Slack 수동 알림 (지연 발생)
  ↓
kubectl get pods -A
  ↓
kubectl describe pod <name>    ← K8s Events는 1시간 후 자동 삭제
  ↓
kubectl logs <name> --previous ← 직전 1회만 보존, 2회 이전은 소멸
  ↓
수동 원인 분석 (30분~수 시간)
```

**오늘 실제 발생한 케이스로 보는 문제점**:

| 장애 | 현재 대응 시간 | Operator 있었다면 |
|---|---|---|
| ConfigMap PLACEHOLDER 버킷명 | 수동 로그 확인 → 변수 추적 → PR → 머지 → rollout (약 2시간) | 즉시 "RAW_AUDIO_BUCKET=PLACEHOLDER" 로그 수집 + 최근 커밋 분석 → 원인 1분 내 도출 |
| GPU SCP deny | Karpenter 로그 수동 확인 | NodeClaim Unknown 즉시 감지 → EC2 에러 메시지 자동 수집 → SCP ARN까지 알림 |
| GPU OOMKilled | dmesg 접근 불가 | SSM으로 노드 레벨 수집 → 모델 메모리 사용량 + 배치 사이즈 변경 커밋 자동 연결 |

### 4-2. DevOps Agent Operator 전체 아키텍처

```
┌─────────────────────────────────────────────────────────────────┐
│  EKS Cluster                                                    │
│                                                                 │
│  ┌──────────────────┐   Pod 상태 변경 Watch                     │
│  │  devops-agent-   │◄──────────────────────────────────────┐  │
│  │  operator Pod    │   (Informer: 모든 utterai-* namespace) │  │
│  │  (system ns)     │                                        │  │
│  └────────┬─────────┘      ┌──────────────────────────────┐  │  │
│           │                │  utterai-prod-api            │  │  │
│           │ 수집            │  utterai-ai-gpu              │──┘  │
│           │                │  utterai-ai-cpu              │     │
│           │                │  utterai-batch               │     │
│           │                └──────────────────────────────┘     │
│           │                                                      │
│           ▼ AWS SSM SendCommand                                  │
│  ┌──────────────────┐                                           │
│  │  EKS Worker Node │                                           │
│  │  (장애 Pod가      │                                           │
│  │   있던 노드)      │                                           │
│  └──────────────────┘                                           │
└─────────┬───────────────────────────────────────────────────────┘
          │ 수집 데이터
          ├── Pod logs (current + previous containers)
          ├── K8s Events (만료 전 즉시 저장)
          ├── Pod manifest / ConfigMap 현재 값
          ├── kubelet 로그 (SSM)
          ├── dmesg (OOM killer 기록, SSM)
          └── 메모리/CPU/GPU 사용량 (SSM)
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│  AWS                                                            │
│                                                                 │
│  S3: utterai-prod-incidents/                                    │
│    incidents/{timestamp}/{namespace}/{pod}/collected-data.json  │
│                                                                 │
│  CloudWatch Logs: /aws/eks/utterai-prod/incidents               │
│                                                                 │
│  AWS DevOps Agent ◄── Webhook (HMAC-SHA256 서명)                │
│    ├── Agent Space: UtterAI Production                          │
│    ├── Pipeline: UtterAI_BE, UtterAI_Infra, UtterAI_AI (GitHub)│
│    ├── Runbook: OOMKilled / CrashLoopBackOff / Karpenter 실패   │
│    └── Communication: Slack #alerts-utterai                     │
│               ↓                                                 │
│    AI 자율 분석 → 근본 원인 + Mitigation Plan                    │
│               ↓                                                 │
│    Slack 알림 (분석 완료, 원인, 조치 방안)                        │
└─────────────────────────────────────────────────────────────────┘
```

### 4-3. Terraform 변경사항

#### A. S3 incidents 버킷 추가

**파일**: `terraform/modules/s3/main.tf`

```hcl
locals {
  base_buckets = {
    frontend    = "${local.prefix}-frontend"
    raw_audio   = "${local.prefix}-raw-audio"
    template    = "${local.prefix}-template"
    rag_ingest  = "${local.prefix}-rag-ingest"
    reports     = "${local.prefix}-reports"
    transcripts = "${local.prefix}-transcripts"
    kubecost    = "${local.prefix}-kubecost"
    loki        = "${local.prefix}-loki"
    incidents   = "${local.prefix}-incidents"  # 추가
  }
}

# incidents 버킷 라이프사이클 (90일 보존)
resource "aws_s3_bucket_lifecycle_configuration" "incidents" {
  bucket = aws_s3_bucket.buckets["incidents"].id

  rule {
    id     = "expire-incidents"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
  }
}
```

**파일**: `terraform/modules/s3/outputs.tf`

```hcl
output "incidents_bucket_arn" {
  value = aws_s3_bucket.buckets["incidents"].arn
}

output "incidents_bucket_name" {
  value = aws_s3_bucket.buckets["incidents"].id
}
```

#### B. IRSA — DevOps Agent Operator Role 추가

**파일**: `terraform/modules/irsa/main.tf`에 추가

```hcl
# ── DevOps Agent Operator IRSA ───────────────────────────────────────────────

resource "aws_iam_role" "devops_agent_operator" {
  name = "${local.prefix}-devops-agent-operator-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_aud}" = "sts.amazonaws.com"
          "${local.oidc_sub}" = "system:serviceaccount:devops-agent-operator-system:devops-agent-operator"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "devops_agent_operator" {
  name = "${local.prefix}-devops-agent-operator-policy"
  role = aws_iam_role.devops_agent_operator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # SSM: 노드 레벨 로그 수집
      {
        Sid    = "SSMCollect"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
        ]
        Resource = [
          "arn:aws:ec2:${var.aws_region}:${var.aws_account_id}:instance/*",
          "arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript",
        ]
      },
      # S3: 수집 데이터 저장
      {
        Sid    = "S3IncidentWrite"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject"]
        Resource = "${var.incidents_bucket_arn}/*"
      },
      {
        Sid    = "S3IncidentList"
        Effect = "Allow"
        Action = ["s3:ListBucket"]
        Resource = var.incidents_bucket_arn
      },
      # CloudWatch Logs: 인시던트 로그 저장
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams",
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/eks/*"
      },
      # EC2: 인스턴스 정보 조회 (SSM 타겟 확인)
      {
        Sid    = "EC2Describe"
        Effect = "Allow"
        Action = ["ec2:DescribeInstances"]
        Resource = "*"
      },
    ]
  })
}
```

**파일**: `terraform/modules/irsa/variables.tf`에 추가

```hcl
variable "incidents_bucket_arn" {
  description = "ARN of S3 bucket for incident data storage"
  type        = string
}
```

**파일**: `terraform/modules/irsa/outputs.tf`에 추가

```hcl
output "devops_agent_operator_role_arn" {
  value = aws_iam_role.devops_agent_operator.arn
}
```

**파일**: `terraform/environments/prod/03-services/main.tf` — irsa 모듈 호출에 추가

```hcl
module "irsa" {
  source = "../../../modules/irsa"
  # ... 기존 인자들 ...
  incidents_bucket_arn = module.s3.incidents_bucket_arn
}
```

#### C. SSM 노드 접근을 위한 Node Role 정책 추가

Operator가 SSM으로 EKS 워커 노드에 명령을 보내려면 노드 IAM Role에 SSM 정책이 필요합니다.

**파일**: `terraform/modules/eks/main.tf` — node role에 추가

```hcl
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
```

> **왜 필요한가**: SSM Agent가 EKS 노드에 기본 설치되어 있지만 SSM 서비스에 등록되려면 `AmazonSSMManagedInstanceCore` 정책이 노드 role에 붙어있어야 합니다. 이 정책 없이는 `ssm:SendCommand`가 "No instances found" 오류를 반환합니다.

### 4-4. Secrets Manager — Webhook Secret 추가

Operator가 DevOps Agent로 Webhook을 보낼 때 HMAC-SHA256 서명에 사용하는 secret을 Secrets Manager에 저장합니다.

**AWS CLI로 생성**:

```bash
aws secretsmanager create-secret \
  --name "utterai-prod/devops-agent-webhook" \
  --secret-string '{"webhook_url":"https://devops-agent.amazonaws.com/webhook/xxx","hmac_secret":"YOUR_HMAC_SECRET"}' \
  --region ap-northeast-2
```

**파일**: `k8s/platform/devops-agent-operator/external-secret.yaml`

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: devops-agent-webhook-secret
  namespace: devops-agent-operator-system
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secretsmanager
    kind: ClusterSecretStore
  target:
    name: devops-agent-webhook-secret
  data:
    - secretKey: webhook-url
      remoteRef:
        key: utterai-prod/devops-agent-webhook
        property: webhook_url
    - secretKey: webhook-hmac-secret
      remoteRef:
        key: utterai-prod/devops-agent-webhook
        property: hmac_secret
```

### 4-5. K8s 매니페스트 전체 구성

**디렉토리 구조**:

```
k8s/platform/devops-agent-operator/
├── kustomization.yaml
├── namespace.yaml
├── serviceaccount.yaml
├── rbac.yaml
├── deployment.yaml
├── external-secret.yaml
└── overlays/
    └── prod/
        └── kustomization.yaml
```

**파일**: `k8s/platform/devops-agent-operator/namespace.yaml`

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: devops-agent-operator-system
```

**파일**: `k8s/platform/devops-agent-operator/serviceaccount.yaml`

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: devops-agent-operator
  namespace: devops-agent-operator-system
  annotations:
    eks.amazonaws.com/role-arn: PLACEHOLDER   # prod overlay에서 실제 ARN으로 교체
```

**파일**: `k8s/platform/devops-agent-operator/rbac.yaml`

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: devops-agent-operator
rules:
  # Pod 조회 및 로그 수집
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
  # K8s Events 조회 (1시간 내 만료 전 수집)
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
  # 노드 정보 (SSM 타겟 인스턴스 ID 조회)
  - apiGroups: [""]
    resources: ["nodes"]
    verbs: ["get", "list", "watch"]
  # ConfigMap 현재 값 수집 (환경변수 오설정 진단)
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list"]
  # Deployment / ReplicaSet (최근 롤아웃 이력)
  - apiGroups: ["apps"]
    resources: ["deployments", "replicasets"]
    verbs: ["get", "list", "watch"]
  # Karpenter NodeClaim 상태
  - apiGroups: ["karpenter.sh"]
    resources: ["nodeclaims", "nodepools"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: devops-agent-operator
subjects:
  - kind: ServiceAccount
    name: devops-agent-operator
    namespace: devops-agent-operator-system
roleRef:
  kind: ClusterRole
  name: devops-agent-operator
  apiGroup: rbac.authorization.k8s.io
```

**파일**: `k8s/platform/devops-agent-operator/deployment.yaml`

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: devops-agent-operator
  namespace: devops-agent-operator-system
spec:
  replicas: 1
  selector:
    matchLabels:
      app: devops-agent-operator
  template:
    metadata:
      labels:
        app: devops-agent-operator
    spec:
      serviceAccountName: devops-agent-operator
      containers:
        - name: operator
          image: ghcr.io/aws-samples/devops-agent-operator:latest
          env:
            - name: DEVOPS_AGENT_WEBHOOK_URL
              valueFrom:
                secretKeyRef:
                  name: devops-agent-webhook-secret
                  key: webhook-url
            - name: WEBHOOK_HMAC_SECRET
              valueFrom:
                secretKeyRef:
                  name: devops-agent-webhook-secret
                  key: webhook-hmac-secret
            - name: EKS_CLUSTER_NAME
              value: PLACEHOLDER
            - name: AWS_REGION
              value: ap-northeast-2
            - name: ENABLE_SSM_COLLECTION
              value: "true"
            - name: CLOUDWATCH_LOG_GROUP
              value: PLACEHOLDER
            - name: S3_BUCKET
              value: PLACEHOLDER
            # 감시할 namespace (콤마 구분)
            - name: WATCH_NAMESPACES
              value: "utterai-prod-api,utterai-ai-gpu,utterai-ai-cpu,utterai-batch"
            # Pending 상태도 감지 (기본은 Failed만)
            - name: WATCH_PENDING_THRESHOLD_SECONDS
              value: "300"    # 5분 이상 Pending 시 수집
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
```

**파일**: `k8s/platform/devops-agent-operator/overlays/prod/kustomization.yaml`

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../base
patches:
  - target:
      kind: ServiceAccount
      name: devops-agent-operator
    patch: |
      - op: replace
        path: /metadata/annotations/eks.amazonaws.com~1role-arn
        value: "arn:aws:iam::032886669461:role/utterai-prod-devops-agent-operator-role"
  - target:
      kind: Deployment
      name: devops-agent-operator
    patch: |
      - op: replace
        path: /spec/template/spec/containers/0/env/2/value
        value: "utterai-prod-eks"
      - op: replace
        path: /spec/template/spec/containers/0/env/6/value
        value: "/aws/eks/utterai-prod/incidents"
      - op: replace
        path: /spec/template/spec/containers/0/env/7/value
        value: "utterai-prod-incidents"
```

### 4-6. Runbook 작성 (DevOps Agent에 등록)

Agent Space → Runbooks에 마크다운으로 등록합니다.

**Runbook 1: OOMKilled (GPU Worker)**

```markdown
# GPU Worker OOMKilled 분석 Runbook

## 트리거 조건
- utterai-ai-gpu 네임스페이스의 Pod가 OOMKilled로 종료

## 수집 데이터 확인 순서

### 1. 메모리 limit 설정 확인
- Pod manifest의 `resources.limits.memory` 값 확인 (현재: 14Gi)
- 실제 사용량이 limit 대비 얼마였는지 확인

### 2. CloudWatch Logs 메모리 추이 조회
- 로그 그룹: /aws/eks/utterai-prod/incidents
- 최근 7일 메모리 사용 패턴 조회
- 특정 시점에 급증했는지 vs 점진적 증가인지 구분

### 3. dmesg OOM killer 로그 확인
- S3 수집 데이터의 dmesg 출력에서 `oom-kill-process` 라인 확인
- 어떤 프로세스가 어느 정도 메모리를 점유했는지 확인

### 4. 최근 배포 이력 확인 (GitHub)
- UtterAI_AI 레포의 최근 커밋 확인
- 모델 파일 크기 변경 (`*.pt`, `*.bin`, `*.safetensors`)
- 배치 사이즈 변경 (`batch_size`, `max_batch`)
- 새 라이브러리 추가 (메모리 증가 가능)

### 5. HuggingFace 모델 로딩 메모리 확인
- 수집된 `nvidia-smi` 결과에서 GPU VRAM 사용량 확인
- CPU 메모리 (14Gi) vs GPU VRAM (별도) 혼동 여부 확인

## 근본 원인 판단

| 패턴 | 원인 | 조치 |
|------|------|------|
| 특정 커밋 이후 지속 증가 | 코드 메모리 누수 | 해당 커밋 롤백 |
| 배치 사이즈 증가 커밋 후 발생 | 배치 크기 과다 | batch_size 축소 |
| 점진적 증가 (리크 패턴) | 메모리 미해제 | 프로파일링 필요 |
| 모델 파일 크기 증가 | 모델 자체 용량 | limit 증가 or 모델 경량화 |

## 즉각 조치 (Mitigation)
1. 단기: ml-gpu-worker memory limit 14Gi → 20Gi 증가
   - `k8s/apps/ai-worker/base/ml-gpu-worker-deployment.yaml` 수정
2. 장기: 모델 추론 배치 사이즈 최적화, GPU VRAM 활용 확인
```

**Runbook 2: CrashLoopBackOff (API)**

```markdown
# API Pod CrashLoopBackOff 분석 Runbook

## 트리거 조건
- utterai-prod-api 네임스페이스 Pod CrashLoopBackOff

## 수집 데이터 확인 순서

### 1. 시작 실패 로그 확인
- `--previous` 로그에서 시작 직전 에러 메시지 확인
- FastAPI 앱 시작 실패 vs DB 연결 실패 vs 설정값 오류 구분

### 2. 환경변수/ConfigMap 값 확인
- 수집된 ConfigMap 값에서 PLACEHOLDER 여부 확인
- 특히: RAW_AUDIO_BUCKET, DB_HOST, REDIS_HOST, JWT_SECRET_KEY

### 3. 최근 Infra 변경 이력 (UtterAI_Infra GitHub)
- k8s/apps/backend/overlays/prod/ 최근 커밋 확인
- patch-configmap.yaml 변경 이력

### 4. DB/Redis 연결 확인
- DB_HOST가 올바른지 (utterai-prod-rds.c76womumyurf.ap-northeast-2.rds.amazonaws.com)
- alembic migration init container 실패 여부

## 즉각 조치
1. 환경변수 오설정: ConfigMap 수정 후 `kubectl rollout restart deployment utterai-api-green -n utterai-prod-api`
2. DB 연결 실패: RDS Security Group 및 private subnet routing 확인
3. Migration 실패: `kubectl logs <pod> -n utterai-prod-api -c db-migrate`로 상세 확인
```

**Runbook 3: Karpenter 노드 프로비저닝 실패**

```markdown
# Karpenter 노드 프로비저닝 실패 Runbook

## 트리거 조건
- Pod 5분 이상 Pending 유지
- Karpenter NodeClaim Unknown 상태

## 수집 데이터 확인 순서

### 1. K8s FailedScheduling Events 확인
- 수집된 Events에서 FailedScheduling 원인 파악
  - `node affinity/selector`: nodeSelector 불일치
  - `untolerated taint`: toleration 미설정
  - `insufficient cpu/memory`: 리소스 부족

### 2. Karpenter 에러 메시지 확인
- NodeClaim 상태 및 에러 메시지
- EC2 RunInstances 에러 코드 확인
  - `UnauthorizedOperation`: SCP 차단 → Organizations 관리자 요청
  - `InsufficientInstanceCapacity`: 해당 AZ/타입 용량 부족 → 다른 인스턴스 타입 추가
  - `RequestExpired`: IAM IRSA 토큰 문제

### 3. Service Quota 확인
- G 인스턴스: On-Demand (L-DB2E81BA), Spot (L-3819A6DF)
- `aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA`

### 4. SCP 차단 여부 확인
- 에러 메시지에 `service control policy` 포함 시 → SCP 해제 요청 필요
- Organizations 관리 계정 담당자에게 정책 ARN 전달

## 즉각 조치
- SCP 차단: Organizations 관리자에게 `p-jr23q446` 정책 예외 요청
- 용량 부족: nodepools.yaml에 대체 인스턴스 타입 추가
- 쿼터 부족: AWS 콘솔에서 증설 요청 (승인 수 시간~수일 소요)
```

---

## 6. 적용 우선순위 및 로드맵

### Phase 1 — 즉시 적용 (1~3일, 인프라 변경 최소)

| 항목 | 파일 | 예상 공수 | 효과 |
|---|---|---|---|
| OTel logs → Loki 연결 | `k8s/platform/observability/base/otel-collector.yaml` | 1h | 앱 구조화 로그 Loki 저장 시작 |
| BE 앱 Trace ID 로그 삽입 | `Utterai_BE/app/core/logging.py` | 2h | Trace ↔ Log 연결 기반 마련 |
| Grafana Derived Field 설정 | Grafana UI | 30m | 로그→트레이스 1클릭 이동 |
| Alertmanager 룰 추가 | `k8s/platform/observability/base/alert-rules-utterai.yaml` | 2h | CrashLoopBackOff, OOMKilled 즉시 알림 |

### Phase 2 — 단기 (1~2주)

| 항목 | 파일 | 예상 공수 | 효과 |
|---|---|---|---|
| 비즈니스 메트릭 계측 | `Utterai_BE/app/core/metrics.py` | 4h | 업로드 실패율, SQS 처리량 가시성 |
| ServiceMonitor 추가 | `k8s/platform/observability/base/` | 2h | 앱 메트릭 Prometheus 자동 수집 |
| Grafana 대시보드 코드화 | `k8s/platform/observability/base/grafana-dashboard-utterai.yaml` | 4h | 전체 서비스 상태 한눈에 파악 |
| **ADOT Operator 설치** | `terraform/modules/eks-addons/main.tf` | 1h | OpenTelemetryCollector CRD 기반 마련 |
| **OTel Collector → CRD 방식 이관** | `k8s/platform/observability/base/otel-collector-crd.yaml` | 2h | AWS 관리형 수명주기 |
| **Instrumentation CRD 추가** | `k8s/platform/observability/base/instrumentation-python.yaml` | 1h | Auto-instrumentation 준비 |
| **SQS 메시지에 session_id/user_id 추가** | `UtterAI_BE`, `UtterAI_AI` 앱 코드 | 4h | VOC 전체 흐름 추적의 기반 |
| **Worker 로그 상관 ID 포함** | `UtterAI_AI` 앱 코드 | 2h | Elasticsearch 검색 가능 상태 |

### Phase 3 — 중기 (1~2개월)

| 항목 | 파일/작업 | 예상 공수 | 효과 |
|---|---|---|---|
| **Auto-instrumentation 어노테이션 적용** | `k8s/apps/backend/base/`, `k8s/apps/ai-worker/base/` 배포 파일 | 2h | 앱 코드 OTel 초기화 제거, SDK 자동 주입 |
| **SQS trace context 전파 (traceparent)** | `UtterAI_BE` + `UtterAI_AI` 앱 코드 | 4h | Tempo에서 API→CPU→GPU 하나의 trace로 연결 |
| **AWS OpenSearch 도메인 생성** | `terraform/modules/opensearch/main.tf` (신규 모듈) | 3h | VOC 데이터 저장소 |
| **Fluent Bit IRSA 추가** | `terraform/modules/irsa/main.tf` | 1h | Fluent Bit → OpenSearch 인증 |
| **Fluent Bit DaemonSet 배포** | `k8s/platform/observability/base/fluent-bit-opensearch.yaml` | 2h | utterai 앱 로그 → OpenSearch 전송 시작 |
| **OpenSearch 인덱스 템플릿 + ILM 설정** | OpenSearch Dashboard Dev Tools | 1h | session_id 필드 인덱싱, 90일 보존 |
| **OpenSearch Dashboard 3종 뷰** | OpenSearch 콘솔 | 3h | 세션 타임라인 / 에러 패턴 / 처리시간 분포 |
| S3 incidents 버킷 추가 | `terraform/modules/s3/main.tf` | 1h | DevOps Agent 장애 데이터 90일 보존 |
| IRSA DevOps Agent Operator Role | `terraform/modules/irsa/main.tf` | 2h | Operator AWS 권한 |
| EKS Node SSM 정책 추가 | `terraform/modules/eks/main.tf` | 30m | 노드 레벨 로그 수집 |
| Webhook Secret 생성 | AWS Secrets Manager | 30m | DevOps Agent 연결 |
| Operator K8s 매니페스트 | `k8s/platform/devops-agent-operator/` | 4h | 장애 자동 감지 시작 |
| DevOps Agent Space + Runbook 3종 | AWS 콘솔 | 5h | AI 자동 분석 완성 |

### Phase 4 — 장기 (필요 시 검토)

| 항목 | 조건 |
|---|---|
| 관리자 페이지 세션 추적 탭 | OpenSearch Dashboard 운영 안정화 후, admin UI 직접 구축 필요 시 |
| VOC 티켓 → session_id 자동 연결 | 사용자 신고 시스템 구축 후 |
| X-Ray 전환 (Tempo 대체) | ADOT 전환 완료 후, AWS 통합 트레이싱 필요 시 |
| CloudWatch Logs 전환 (Loki 대체) | Loki S3 운영 비용 > CloudWatch 비용인 경우 |
| DevOps Agent MCP 서버 연동 | 인시던트 패턴 DB화 후 선제 대응 필요 시 |

---

## 참고 — 현재 스택 vs 목표 스택

| 역할 | 현재 | Phase 1 | Phase 2 (ADOT + VOC 기반) | Phase 3 (VOC 완성 + DevOps Agent) |
|---|---|---|---|---|
| OTel 수집기 | vanilla OTel Collector | 동일 | ADOT Operator + CRD | 동일 |
| 로그 저장 (인프라) | stdout → Loki (구조화 미흡) | OTel → Loki (구조화 JSON) | 동일 | 동일 |
| 로그 저장 (데이터 흐름) | 없음 | 없음 | Fluent Bit → OpenSearch | 동일 + 인시던트 S3 보존 |
| 트레이스 | Tempo (SQS 경계에서 끊김) | Tempo + Log drill-down | traceparent 전파 준비 | API→CPU→GPU 하나의 trace |
| VOC 데이터 흐름 추적 | 불가 (수동 grep) | 불가 | session_id 로그 포함 | OpenSearch 세션 타임라인 |
| 메트릭 | 기본 HTTP 메트릭만 | 비즈니스 메트릭 추가 | 동일 | 동일 |
| 자동 계측 | 앱 코드 직접 작성 | 동일 | Instrumentation CRD 준비 | 어노테이션 자동 주입 |
| 알림 | 없음 | CrashLoopBackOff/OOMKilled | 동일 | 동일 |
| 장애 분석 | 수동 30분~수 시간 | 수동 (데이터 풍부) | 동일 | AI 자동 분석 1~5분 |
| 장애 데이터 보존 | Pod 삭제 시 소멸 | Loki 저장됨 | 동일 | S3 90일 보존 |
