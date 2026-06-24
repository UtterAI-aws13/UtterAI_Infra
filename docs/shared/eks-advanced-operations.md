# EKS 고도화 — 관찰 가능성 & AI 기반 장애 자동 분석

> 참고 블로그
> - [AWS DevOps Agent + K8s Operator를 통한 EKS 운영 자동화](https://aws.amazon.com/ko/blogs/tech/aws-devops-agent-k8s-operator/)
> - [Amazon EKS에서 Spring Boot 애플리케이션 관찰 가능성 구성](https://aws.amazon.com/ko/blogs/tech/springboot-application-observability-using-amazon-eks/)

---

## 목차

1. [현재 UtterAI 관찰 가능성 스택 — 현황 및 갭](#1-현재-utterai-관찰-가능성-스택--현황-및-갭)
2. [고도화 방향 1 — 관찰 가능성 완성](#2-고도화-방향-1--관찰-가능성-완성)
3. [고도화 방향 2 — AI 기반 장애 자동 분석](#3-고도화-방향-2--ai-기반-장애-자동-분석)
4. [적용 우선순위 및 로드맵](#4-적용-우선순위-및-로드맵)

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

## 3. 고도화 방향 2 — AI 기반 장애 자동 분석

### 3-1. 왜 필요한가 — UtterAI 현황

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

### 3-2. DevOps Agent Operator 전체 아키텍처

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

### 3-3. Terraform 변경사항

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

### 3-4. Secrets Manager — Webhook Secret 추가

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

### 3-5. K8s 매니페스트 전체 구성

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

### 3-6. Runbook 작성 (DevOps Agent에 등록)

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

## 4. 적용 우선순위 및 로드맵

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

### Phase 3 — 중기 (1~2개월)

| 항목 | 파일/작업 | 예상 공수 | 효과 |
|---|---|---|---|
| S3 incidents 버킷 추가 | `terraform/modules/s3/main.tf` | 1h | 장애 데이터 90일 보존 |
| IRSA Operator Role 추가 | `terraform/modules/irsa/main.tf` | 2h | Operator AWS 권한 |
| EKS Node SSM 정책 추가 | `terraform/modules/eks/main.tf` | 30m | 노드 레벨 로그 수집 |
| Webhook Secret 생성 | AWS Secrets Manager | 30m | DevOps Agent 연결 |
| Operator K8s 매니페스트 | `k8s/platform/devops-agent-operator/` | 4h | 장애 자동 감지 시작 |
| DevOps Agent Space 구성 | AWS 콘솔 | 2h | AI 분석 파이프라인 완성 |
| Runbook 3종 등록 | DevOps Agent 콘솔 | 3h | 장애 유형별 자동 분석 |

### Phase 4 — 장기 (필요 시 검토)

| 항목 | 조건 |
|---|---|
| AWS 관리형 스택 전환 (AMP/AMG/X-Ray) | Loki/Tempo 운영 부담 증가 시 |
| DevOps Agent MCP 서버 연동 | 과거 인시던트 패턴 DB화 후 선제 대응 |
| Proactive Prevention | 인시던트 누적 데이터 기반 예방 알림 |

---

## 참고 — 현재 스택 vs 목표 스택

| 역할 | 현재 | Phase 1~2 완료 후 | Phase 3 완료 후 |
|---|---|---|---|
| 로그 저장 | stdout → Loki (구조화 미흡) | OTel → Loki (구조화 JSON) | 동일 + 인시던트 S3 보존 |
| 트레이스 | Tempo (Log 연결 없음) | Tempo + Log drill-down | 동일 |
| 메트릭 | 기본 HTTP 메트릭만 | 비즈니스 메트릭 추가 | 동일 |
| 알림 | 없음 | CrashLoopBackOff/OOMKilled 즉시 | 동일 |
| 장애 분석 | 수동 30분~수 시간 | 수동 (단, 데이터 풍부) | AI 자동 분석 1~5분 |
| 장애 데이터 보존 | Pod 삭제 시 소멸 | 소멸 (단, Loki 저장됨) | S3 90일 보존 |
