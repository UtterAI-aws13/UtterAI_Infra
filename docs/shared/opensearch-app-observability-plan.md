# OpenSearch 앱 레벨 옵저빌리티 구현 계획

## 배경

### 현재 문제

VOC 발생 시 인프라 로그를 직접 뒤지지 않고, 어느 서비스/단계에서 문제가 생겼는지 시각적으로 빠르게 파악하는 구조가 없다.

현재 Grafana Service Graph가 동작하지 않는 근본 원인: UtterAI는 BE → SQS → Workers 비동기 구조라 CLIENT 스팬과 SERVER 스팬 간격이 수십 초~수 분 이상이며, servicegraph connector는 동기 HTTP 스팬 쌍을 TTL 내에 매칭해야 하므로 비동기 아키텍처에서는 구조적으로 동작하지 않는다.

### Phase 1 완료 (현재 운영 중)

spanmetrics connector + Grafana "UtterAI 파이프라인 모니터" 대시보드

- 서비스별 에러율 / 처리 지연 (p50/p99)
- Span 단위 에러 Top 10 / 느린 Span Top 10
- Loki 에러 로그, Tempo 에러 트레이스 목록

Phase 1으로 VOC 발생 시 어느 서비스에서 에러가 났는지 확인할 수 있으나, 서비스 간 흐름을 토폴로지로 시각화하는 Service Map은 없다.

---

## 목표 (Phase 2)

OpenSearch Dashboards의 Trace Analytics로 앱 레벨 Service Map 제공.

```
backend → SQS → cpu-worker → gpu-worker
```

위 비동기 파이프라인 흐름을 저장된 trace 데이터 기반으로 Service Map에서 시각화.

---

## 역할 분리 원칙

| 레벨 | 스택 | 용도 |
|------|------|------|
| 인프라 레벨 | Prometheus + Grafana | pod CPU/메모리, node 상태, 인프라 알람 |
| 앱 레벨 | OpenSearch Dashboards | Service Map, Trace Analytics, 앱 로그 검색 |

Tempo / Loki는 인프라 레벨로 병행 유지.

---

## 배포 방식 결정

### 비교

| 방식 | 비용 | 운영 부담 | 비고 |
|------|------|----------|------|
| AWS OpenSearch Serverless | 월 $700+ (최소 OCU 고정과금) | 없음 | 트래픽 0이어도 과금 |
| AWS OpenSearch Service | 월 ~$50 (t3.medium.search) | 낮음 | AWS 관리형 |
| **EKS self-hosted** | **기존 노드 공유, 추가 거의 없음** | **중간** | **선택** |

### EKS self-hosted 선택 이유

- 앱 전용이라 데이터 볼륨 작음 → 기존 platform nodepool 노드 공유로 추가 비용 없음
- 기존 ArgoCD + Karpenter GitOps 방식 그대로 관리 가능
- 트래픽/서비스 수 증가 시 AWS OpenSearch Service로 마이그레이션 고려

---

## 아키텍처

```
앱 (backend / cpu-worker / gpu-worker)
  │
  ▼ OTLP
OTel Collector
  ├── traces → Tempo                    (기존 유지, Grafana 드릴다운)
  ├── traces → Data Prepper → OpenSearch (신규, Service Map / Trace Analytics)
  ├── logs   → Loki                     (기존 유지, 인프라 로그)
  └── metrics → Prometheus              (기존 유지, 파이프라인 모니터)
```

---

## 구성 컴포넌트

| 컴포넌트 | 이미지 | 리소스 | 비고 |
|----------|--------|--------|------|
| OpenSearch | `opensearchproject/opensearch:2.13.0` | 2GB memory, 20Gi PVC (gp2) | single-node, security 비활성화 |
| OpenSearch Dashboards | `opensearchproject/opensearch-dashboards:2.13.0` | 512MB memory | Ingress 또는 포트포워딩 |
| Data Prepper | `opensearchproject/data-prepper:2.8.0` | 512MB memory | OTel trace → OpenSearch 변환 |

- 네임스페이스: `utterai-observability` (기존과 동일)
- 노드: `platform` nodepool (기존 otel-collector와 동일)

---

## 구현 순서

### Step 1: OpenSearch + OpenSearch Dashboards 배포

파일: `k8s/platform/observability/base/opensearch.yaml`

- OpenSearch StatefulSet (single-node, `DISABLE_SECURITY_PLUGIN: true`)
- PVC: gp2 20Gi
- initContainer: `sysctl -w vm.max_map_count=262144` (OpenSearch 필수)
- OpenSearch Dashboards Deployment
- 두 컴포넌트 모두 ClusterIP Service

### Step 2: Data Prepper 배포

파일: `k8s/platform/observability/base/data-prepper.yaml`

파이프라인 구성:
```
entry-pipeline (otel_trace_source:21890)
  └── processor: otel_traces_raw
  └── sink → raw-trace-pipeline      → OpenSearch (index_type: trace-analytics-raw)
  └── sink → service-map-pipeline    → OpenSearch (index_type: trace-analytics-service-map)
```

`service_map_stateful` processor가 SQS 비동기 흐름을 포함한 서비스 간 토폴로지를 생성한다.

### Step 3: OTel Collector 파이프라인 추가

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

### Step 4: kustomization.yaml 업데이트

`opensearch.yaml`, `data-prepper.yaml` resources 추가.

### Step 5: OpenSearch Dashboards 접근

초기에는 포트포워딩으로 접근:
```bash
kubectl port-forward svc/opensearch-dashboards -n utterai-observability 5601:5601
```

필요 시 기존 ALB Ingress에 subdomain 추가 (`opensearch.internal.utterai.org` 등).

---

## 검증 계획

| 항목 | 확인 방법 |
|------|----------|
| OpenSearch 기동 | `kubectl get pods -n utterai-observability` — opensearch-0 Running |
| Data Prepper 기동 | data-prepper pod Running, `/list` health endpoint 응답 |
| OTel → Data Prepper 연결 | OTel Collector 로그에서 `otlp/data-prepper` exporter 에러 없음 |
| trace 데이터 수신 | OpenSearch index `otel-v1-apm-span-*` 생성 확인 |
| Service Map 표시 | OpenSearch Dashboards → Observability → Trace Analytics → Service Map |

---

## 향후 확장 고려

- **앱 로그 → OpenSearch 추가**: Data Prepper에 `otel_logs_source` 파이프라인 추가, Loki 대체 검토
- **Ingress 추가**: 내부 subdomain으로 Dashboards 상시 접근
- **AWS OpenSearch Service 마이그레이션**: 트래픽 증가 또는 운영 안정성 요건 강화 시
