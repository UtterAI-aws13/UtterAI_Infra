# Grafana 핵심 지표 가이드

이 문서는 Grafana에서 운영자가 우선 확인해야 하는 핵심 지표와 각 지표에서 얻을 수 있는 인사이트를 정리한다.

Grafana는 데이터를 직접 수집하지 않는다. Prometheus, Loki, Tempo 같은 datasource에 저장된 데이터를 화면으로 보여준다.

## Datasource 구분

| Datasource | 보는 것 | 예시 |
|---|---|---|
| Prometheus | 숫자 지표 | Pod 상태, 재시작 횟수, CPU/Memory, 앱 처리량 |
| Loki | 로그 | API 로그, worker 로그, ERROR 로그 |
| Tempo | Trace | 요청 흐름, 서비스 간 호출 추적 |

## 기본 해석 규칙

| 화면 상태 | 의미 |
|---|---|
| 값이 0 | 문제가 없거나 이벤트가 없음 |
| 값이 1 이상 | 해당 상태나 이벤트가 존재함 |
| No data | 문제가 없어서 데이터가 없을 수도 있고, 수집 자체가 안 될 수도 있음 |
| 갑자기 증가 | 장애, 배포, 트래픽 변화, 스케일링 이벤트 가능성 |
| 지속적으로 증가 | 누적 장애 또는 반복 실패 가능성 |

`Running` Pod라고 해서 항상 정상은 아니다. `CrashLoopBackOff` Pod도 Pod phase상 `Running`에 포함될 수 있다. 그래서 `Pod 상태 요약`만 보지 말고 `CrashLoopBackOff Pod`, `재시작 많은 Pod`를 같이 확인해야 한다.

## 클러스터 / Pod 상태

Kubernetes 전체 상태를 확인하는 대시보드다. 앱 문제인지, 클러스터 공통 문제인지 먼저 분리하는 데 사용한다.

### Pod 상태 요약

```promql
sum by (phase) (kube_pod_status_phase)
```

| 항목 | 내용 |
|---|---|
| Visualization | Bar gauge |
| 무엇을 보는가 | 전체 Pod가 `Running`, `Pending`, `Failed`, `Succeeded`, `Unknown` 중 어디에 있는지 본다. |
| 정상 기준 | 대부분 `Running`, `Pending`/`Failed`/`Unknown`은 0에 가까워야 한다. |
| 이상 신호 | `Pending`, `Failed`, `Unknown`이 1 이상이면 확인이 필요하다. |
| 인사이트 | 클러스터 전체에 문제가 생겼는지 가장 빠르게 감지할 수 있다. 단, `CrashLoopBackOff`는 `Running`에 포함될 수 있으므로 이 지표만으로 정상 판단하면 안 된다. |

### 문제 Pod 목록

```promql
sum by (namespace, phase) (
  kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}
) > 0
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | `Pending`, `Failed`, `Unknown` 상태인 Pod가 어느 namespace에 있는지 본다. |
| 정상 기준 | No data 또는 결과 없음. |
| 이상 신호 | 특정 namespace와 phase가 표시된다. |
| 인사이트 | 문제가 앱 namespace에 있는지, monitoring/infra namespace에 있는지 빠르게 구분할 수 있다. 예를 들어 `monitoring`의 `Pending`은 로그 수집기나 관측 스택 문제일 수 있고, 앱 namespace의 `Pending`은 스케줄링이나 리소스 부족 문제일 수 있다. |

### CrashLoopBackOff Pod

```promql
sum by (namespace, pod, container, reason) (
  kube_pod_container_status_waiting_reason{reason="CrashLoopBackOff"}
) > 0
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | 컨테이너가 실행 후 실패하고 Kubernetes가 재시작을 반복하는 Pod를 본다. |
| 정상 기준 | No data 또는 결과 없음. |
| 이상 신호 | Pod, container, reason이 표시된다. |
| 인사이트 | 스케줄링 문제는 이미 지나갔고, 컨테이너 내부 실행 실패일 가능성이 높다. 이 경우 다음 단계는 Loki에서 해당 Pod 로그를 확인하는 것이다. |

### 재시작 많은 Pod

```promql
topk(10, sum by (namespace, pod) (
  increase(kube_pod_container_status_restarts_total[1h])
))
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | 최근 1시간 동안 재시작이 많은 Pod 상위 10개를 본다. |
| 정상 기준 | 대부분 0 또는 낮은 값. |
| 이상 신호 | 특정 Pod의 재시작 횟수가 반복적으로 증가한다. |
| 인사이트 | `CrashLoopBackOff`, OOM, 앱 예외, 설정 오류를 빠르게 찾을 수 있다. 배포 직후 값이 증가하면 새 이미지나 설정 변경을 의심한다. |

### Ready Node 수

```promql
sum(kube_node_status_condition{condition="Ready", status="true"})
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | 현재 사용 가능한 Node 수를 본다. |
| 정상 기준 | 기대하는 Node 수와 일치한다. |
| 이상 신호 | 평소보다 Ready Node 수가 줄어든다. |
| 인사이트 | 앱 Pod 문제가 아니라 Node 장애나 Karpenter/스케일링 문제인지 구분하는 출발점이다. |

## UtterAI 워크로드

UtterAI 서비스 namespace만 따로 보는 대시보드다.

대상 namespace:

```text
utterai-prod-api
utterai-ai-cpu
utterai-ai-gpu
```

### UtterAI Pod 상태

```promql
sum by (namespace, phase) (
  kube_pod_status_phase{namespace=~"utterai-prod-api|utterai-ai-cpu|utterai-ai-gpu"}
)
```

| 항목 | 내용 |
|---|---|
| Visualization | Bar gauge |
| 무엇을 보는가 | UtterAI 서비스 Pod의 phase를 namespace별로 본다. |
| 정상 기준 | 필요한 API/worker Pod가 `Running` 상태다. |
| 이상 신호 | `Pending`, `Failed`, `Unknown`이 보인다. |
| 인사이트 | 전체 클러스터가 아니라 우리 서비스만 따로 볼 수 있다. 장애 대응 시 첫 화면으로 적합하다. |

### UtterAI 문제 Pod

```promql
sum by (namespace, phase) (
  kube_pod_status_phase{
    namespace=~"utterai-prod-api|utterai-ai-cpu|utterai-ai-gpu",
    phase=~"Pending|Failed|Unknown"
  }
) > 0
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | UtterAI namespace 중 문제 phase만 본다. |
| 정상 기준 | No data 또는 결과 없음. |
| 이상 신호 | `utterai-prod-api`, `utterai-ai-cpu`, `utterai-ai-gpu` 중 하나가 표시된다. |
| 인사이트 | API 문제인지, CPU worker 문제인지, GPU worker 문제인지 바로 나눌 수 있다. |

### UtterAI CrashLoopBackOff Pod

```promql
sum by (namespace, pod, container, reason) (
  kube_pod_container_status_waiting_reason{
    namespace=~"utterai-prod-api|utterai-ai-cpu|utterai-ai-gpu",
    reason="CrashLoopBackOff"
  }
) > 0
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | UtterAI 앱 컨테이너 중 반복 실패하는 Pod를 본다. |
| 정상 기준 | No data 또는 결과 없음. |
| 이상 신호 | Pod와 container가 표시된다. |
| 인사이트 | Pod가 뜨지 못하는 문제가 아니라 앱 프로세스가 시작 후 죽는 문제다. 이미지, 환경변수, Secret, GPU 의존성, 앱 예외를 확인해야 한다. |

### UtterAI 재시작 많은 Pod

```promql
topk(10, sum by (namespace, pod) (
  increase(kube_pod_container_status_restarts_total{
    namespace=~"utterai-prod-api|utterai-ai-cpu|utterai-ai-gpu"
  }[1h])
))
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | UtterAI Pod 중 최근 1시간 재시작이 많은 Pod를 본다. |
| 정상 기준 | 0 또는 낮은 값. |
| 이상 신호 | 특정 Pod의 재시작 횟수가 증가한다. |
| 인사이트 | API/worker 중 어떤 컴포넌트가 불안정한지 바로 찾을 수 있다. `CrashLoopBackOff` 패널과 같이 보면 원인 추적 속도가 빨라진다. |

### GPU Worker Pending 수

```promql
sum(kube_pod_status_phase{namespace="utterai-ai-gpu", phase="Pending"})
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | GPU worker가 스케줄링 대기 중인지 본다. |
| 정상 기준 | 0 |
| 이상 신호 | 1 이상 |
| 인사이트 | GPU 노드 부족, nodeSelector/affinity 불일치, taint/toleration 문제, Karpenter provisioning 지연을 의심할 수 있다. |

## 앱 처리 지표

Pod가 떠 있는지만 보는 것이 아니라, 앱이 실제로 일을 처리하는지 확인하는 대시보드다.

앱 지표는 애플리케이션에서 OpenTelemetry Collector로 전달되고, Prometheus가 Collector의 Prometheus exporter endpoint를 scrape해서 Grafana에서 조회한다.

### 최근 5분 분석 Job 생성 수

```promql
increase(utterai_analysis_jobs_created_total[5m])
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | 최근 5분 동안 생성된 분석 Job 수를 본다. |
| 정상 기준 | 사용자가 분석 요청을 하면 값이 증가한다. |
| 이상 신호 | 요청이 있는데 값이 0이거나 급감한다. |
| 인사이트 | API가 분석 요청을 정상적으로 받고 있는지 확인할 수 있다. 서비스 사용량의 가장 앞단 지표다. |

### 분석 Job 전달 상태

```promql
sum by (status) (
  increase(utterai_analysis_job_dispatch_total[5m])
)
```

| 항목 | 내용 |
|---|---|
| Visualization | Bar gauge |
| 무엇을 보는가 | 분석 Job이 worker로 전달되는 결과를 status별로 본다. |
| 정상 기준 | 성공 status가 증가한다. |
| 이상 신호 | 실패 status가 증가하거나 성공이 0이다. |
| 인사이트 | API가 Job을 만들었지만 worker 큐로 넘기지 못하는 문제를 분리할 수 있다. |

### Worker별 SQS 수신 수

```promql
sum by (worker) (
  increase(utterai_ai_sqs_receive_total[5m])
)
```

| 항목 | 내용 |
|---|---|
| Visualization | Bar gauge |
| 무엇을 보는가 | worker가 SQS에서 메시지를 가져가는지 본다. |
| 정상 기준 | 작업이 있을 때 관련 worker 값이 증가한다. |
| 이상 신호 | Job은 생성되는데 worker 수신이 0이다. |
| 인사이트 | 큐에 작업이 쌓이는지, worker가 실제로 일을 가져가는지 분리해서 볼 수 있다. |

### AI 단계 처리시간 p95

```promql
histogram_quantile(
  0.95,
  sum by (le, worker, stage) (
    rate(utterai_ai_stage_duration_seconds_bucket[5m])
  )
)
```

| 항목 | 내용 |
|---|---|
| Visualization | Time series |
| 무엇을 보는가 | AI pipeline stage별 p95 처리시간을 본다. |
| 정상 기준 | 평소 처리시간 범위를 유지한다. |
| 이상 신호 | 특정 worker나 stage의 p95가 급증한다. |
| 인사이트 | 전체가 느린지, 특정 단계만 병목인지 알 수 있다. 예를 들어 download, preprocess, inference, publish 중 어느 구간이 느린지 좁힐 수 있다. |

### 앱 Metric 수집 상태

```promql
up{job="otel-collector"}
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | OTel Collector가 Prometheus에 의해 정상 scrape되는지 본다. |
| 정상 기준 | 1 |
| 이상 신호 | 0 또는 No data |
| 인사이트 | 앱 metric이 안 보일 때 앱이 안 보내는 문제인지, Collector/Prometheus 수집 경로 문제인지 먼저 분리할 수 있다. |

## 관측 스택 상태

Grafana에 보이는 데이터 자체를 믿을 수 있는지 확인하는 대시보드다.

### 수집 실패 대상

```promql
up == 0
```

| 항목 | 내용 |
|---|---|
| Visualization | Table |
| 무엇을 보는가 | Prometheus가 scrape하지 못하는 target을 본다. |
| 정상 기준 | No data 또는 결과 없음. |
| 이상 신호 | job, instance가 표시된다. |
| 인사이트 | Grafana에서 지표가 안 보일 때 앱 문제인지 수집 문제인지 먼저 구분할 수 있다. |

### OTel Collector 상태

```promql
up{job="otel-collector"}
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | 앱 metrics/traces 수집 경로 상태를 본다. |
| 정상 기준 | 1 |
| 이상 신호 | 0 또는 No data |
| 인사이트 | 앱 지표가 사라졌을 때 Collector부터 확인해야 하는지 판단할 수 있다. |

### Prometheus 상태

```promql
up{job="utterai-monitoring-prometheus"}
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | Prometheus 자체 scrape 상태를 본다. |
| 정상 기준 | 1 |
| 이상 신호 | 0 또는 No data |
| 인사이트 | metrics 저장/조회 경로의 핵심 컴포넌트가 살아있는지 확인한다. |

### Tempo 상태

```promql
up{job="tempo"}
```

| 항목 | 내용 |
|---|---|
| Visualization | Stat |
| 무엇을 보는가 | trace backend 상태를 본다. |
| 정상 기준 | 1 |
| 이상 신호 | 0 또는 No data |
| 인사이트 | trace 조회가 안 될 때 backend 자체 문제인지 먼저 판단할 수 있다. |

### Kubecost 상태

```promql
up{job=~"kubecost|kubecost-aggregator"}
```

| 항목 | 내용 |
|---|---|
| Visualization | Bar gauge |
| 무엇을 보는가 | Kubecost와 aggregator scrape 상태를 본다. |
| 정상 기준 | 각 target이 1 |
| 이상 신호 | 0 또는 No data |
| 인사이트 | 비용 데이터가 최신인지, Kubecost 화면을 신뢰할 수 있는지 판단할 수 있다. |

## Loki로 확인하는 로그

상태, 개수, CPU, 메모리, 재시작은 Prometheus에서 본다. 실제 에러 메시지와 stack trace는 Loki에서 본다.

### UtterAI ERROR 로그

```logql
{namespace=~"utterai-prod-api|utterai-ai-cpu|utterai-ai-gpu"} |= "ERROR"
```

| 항목 | 내용 |
|---|---|
| Datasource | Loki |
| Visualization | Logs |
| 인사이트 | 앱 에러가 실제로 발생했는지 확인한다. `CrashLoopBackOff`나 재시작 많은 Pod가 있을 때 같이 본다. |

### GPU Worker 로그

```logql
{namespace="utterai-ai-gpu", pod=~"utterai-ml-gpu-worker.*"}
```

| 항목 | 내용 |
|---|---|
| Datasource | Loki |
| Visualization | Logs |
| 인사이트 | GPU worker가 왜 실패하는지 확인한다. 이미지, CUDA, 모델 로딩, 환경변수, Secret 문제를 로그에서 찾는다. |

### OTel Collector 로그

```logql
{namespace="utterai-observability", pod=~"otel-collector.*"}
```

| 항목 | 내용 |
|---|---|
| Datasource | Loki |
| Visualization | Logs |
| 인사이트 | 앱 metric/trace가 안 들어올 때 Collector 수신/전송 오류를 확인한다. |

## Grafana 패널 구성 원칙

| 목적 | 추천 Visualization |
|---|---|
| 현재 상태 숫자 | Stat |
| 상태별/namespace별 개수 | Bar gauge |
| 문제 목록 | Table |
| 시간에 따른 변화 | Time series |
| 로그 원문 | Logs |

권장 배치:

```text
1행: 요약 상태
2행: 문제 목록
3행: 처리량/지연시간 추세
4행: 필요 시 로그
```

운영 대시보드는 많은 정보를 한 화면에 넣는 것보다, 장애가 났을 때 바로 판단할 수 있는 최소 지표만 두는 것이 좋다.
