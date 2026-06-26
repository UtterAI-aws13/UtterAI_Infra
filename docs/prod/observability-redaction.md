# UtterAI Prod Observability Redaction Guide

> 작성일: 2026-06-25  
> 범위: Prod 관측성 데이터 중 로그, trace, metric label의 민감정보 노출 방지 정책  
> 적용 위치: Promtail, OpenTelemetry Collector, 운영 문서 규칙

---

## 1. 목적

이 문서는 Prod 환경에서 관측성 데이터에 민감정보가 남지 않도록 하기 위한 redaction 구조를 설명한다.

관측성 데이터는 장애 분석에 필요하지만, 동시에 다음 정보가 실수로 남을 수 있다.

```text
- Authorization header
- Cookie / Set-Cookie
- 내부 호출 토큰
- password / token / secret / API key 계열 값
- HuggingFace token
- DB password
- Redis auth token
- AWS presigned URL
- S3 object key
- audio object key
- RAG key
- queue URL/name attribute
```

따라서 로그와 trace는 저장 또는 export 전에 민감정보 후보를 제거한다.

---

## 2. 전체 구조

Prod 관측성 데이터 흐름은 다음처럼 나뉜다.

```text
애플리케이션 stdout/stderr logs
  -> Promtail
  -> Loki
  -> S3 backend
  -> Grafana Loki Explore

애플리케이션 OTLP traces
  -> OpenTelemetry Collector
  -> Tempo
  -> S3 backend
  -> Grafana Tempo

애플리케이션 OTLP metrics
  -> OpenTelemetry Collector
  -> Prometheus exporter
  -> Prometheus scrape
  -> Grafana Prometheus datasource
```

각 경로의 redaction 책임은 다음과 같다.

| 데이터 | 경로 | Redaction 위치 | 방식 |
|---|---|---|---|
| 로그 | Pod stdout/stderr -> Promtail -> Loki | Promtail `pipelineStages` | regex replace |
| Trace | App -> OTel Collector -> Tempo | OTel Collector `attributes/redact` processor | attribute delete |
| OTel logs | App -> OTel Collector debug exporter | OTel Collector `attributes/redact` processor | attribute delete |
| Metrics | App -> OTel Collector -> Prometheus | 코드/운영 계약 | 민감 label 금지 |

---

## 3. Promtail 로그 Redaction

### 3.1 적용 위치

Terraform Helm values에서 Promtail pipeline stage를 설정한다.

```text
terraform/modules/eks-addons/main.tf
```

Promtail은 모든 노드에서 Pod 로그를 수집하는 DaemonSet이다. 로그가 Loki에 저장되기 전에 `replace` stage를 거쳐 민감정보 후보를 치환한다.

### 3.2 현재 적용된 패턴

현재 Promtail은 다음 값을 redaction한다.

| 대상 | 치환 값 |
|---|---|
| `Authorization: Bearer ...` | `[REDACTED_AUTHORIZATION]` |
| 독립적인 `Bearer ...` 토큰 | `Bearer [REDACTED]` |
| `Cookie`, `Set-Cookie` | `[REDACTED_SENSITIVE_FIELD]` |
| `X-Internal-Token` | `[REDACTED_SENSITIVE_FIELD]` |
| password/passwd/token/secret/API key 계열 필드 | `[REDACTED_SENSITIVE_FIELD]` |
| `HF_TOKEN`, `DB_PASSWORD`, `REDIS_AUTH_TOKEN` | `[REDACTED_SENSITIVE_FIELD]` |
| AWS presigned URL | `[REDACTED_PRESIGNED_URL]` |

### 3.3 예시

원본 로그:

```text
Authorization: Bearer dummy.jwt.token
DB_PASSWORD=dummy-password
https://example-bucket.s3.amazonaws.com/test.wav?X-Amz-Signature=dummy-signature
```

Loki 저장 로그:

```text
[REDACTED_AUTHORIZATION]
[REDACTED_SENSITIVE_FIELD]
[REDACTED_PRESIGNED_URL]
```

### 3.4 주의사항

Promtail redaction은 Loki에 저장되기 전에 적용된다.

이미 Loki에 저장된 과거 로그는 자동으로 다시 redaction되지 않는다. 검증할 때는 기존 Pod 이름을 재사용하지 말고 새 테스트 Pod 이름을 사용한다.

---

## 4. OpenTelemetry Collector Attribute Redaction

### 4.1 적용 위치

Kubernetes manifest의 OTel Collector ConfigMap에서 processor를 설정한다.

```text
k8s/platform/observability/base/otel-collector.yaml
```

### 4.2 적용 방식

`attributes/redact` processor를 추가하고, traces/logs pipeline에 연결한다.

```yaml
processors:
  attributes/redact:
    actions:
      - key: http.request.header.authorization
        action: delete
      - key: http.request.header.cookie
        action: delete
      - key: http.response.header.set_cookie
        action: delete
      - key: http.url
        action: delete
      - key: url.full
        action: delete
      - key: aws.s3.key
        action: delete
      - key: audio.object_key
        action: delete
      - key: audio.key
        action: delete
      - key: rag.key
        action: delete
      - key: queue.name
        action: delete
```

Pipeline 연결:

```yaml
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, attributes/redact, batch]
      exporters: [otlp/tempo]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, attributes/redact, batch]
      exporters: [debug]
```

### 4.3 왜 trace도 redaction해야 하는가

Trace는 로그가 아니지만 Grafana Tempo에서 검색 가능한 운영 데이터다. 따라서 아래 값이 attribute로 남으면 민감정보가 될 수 있다.

```text
- full URL
- auth/cookie header
- S3 object key
- audio object key
- RAG key
- queue URL/name
```

Promtail은 trace를 처리하지 않는다. Trace는 OTel Collector에서 별도로 attribute를 삭제해야 한다.

---

## 5. Metric Label Guardrail

Metrics pipeline에는 redaction processor를 연결하지 않는다.

대신 metric label에 민감정보를 넣지 않는 것을 운영 계약으로 둔다. Metric label은 cardinality가 커지면 Prometheus 성능에도 영향을 주므로, 보안과 성능 양쪽에서 제한이 필요하다.

### 5.1 금지 label

Metric label에는 다음 값을 넣지 않는다.

```text
- email
- user_id
- session_id
- job_id
- child_id / parent_id
- Authorization token
- refresh token
- internal callback token
- S3 key
- audio key
- presigned URL
- request body
- response body
- prompt
- transcript 원문
```

### 5.2 허용 label

Metric label은 낮은 cardinality 운영 값만 허용한다.

```text
- status
- worker.type
- stage
- route template
- environment
- service.name
```

예시:

```text
좋음: utterai_ai_stage_duration_seconds{worker_type="cpu-worker", stage="vad"}
나쁨: utterai_ai_stage_duration_seconds{job_id="...", audio_key="..."}
```

---

## 6. 배포 경로

Promtail과 OTel Collector는 적용 경로가 다르다.

| 변경 | 파일 | 적용 방식 |
|---|---|---|
| Promtail redaction | `terraform/modules/eks-addons/main.tf` | Terraform `04-addons` apply |
| OTel Collector redaction | `k8s/platform/observability/base/otel-collector.yaml` | ArgoCD sync 또는 Kustomize apply |
| 문서 | `docs/prod/*` | PR merge |

### 6.1 Promtail 적용

```bash
terraform -chdir=terraform/environments/prod/04-addons plan
terraform -chdir=terraform/environments/prod/04-addons apply
```

### 6.2 OTel Collector 적용

PR merge 후 ArgoCD에서 observability app을 sync한다.

```bash
argocd app list | grep -i observability
argocd app sync <observability-app-name>
```

ConfigMap 변경 후 Collector Pod가 자동 재시작되지 않을 수 있으므로 rollout restart를 수행한다.

```bash
kubectl rollout restart deployment/otel-collector -n utterai-observability
kubectl rollout status deployment/otel-collector -n utterai-observability
```

---

## 7. 검증 절차

### 7.1 Promtail 배포 상태 확인

```bash
kubectl rollout status daemonset/promtail -n monitoring
kubectl get pods -n monitoring -l app.kubernetes.io/name=promtail -o wide
helm status promtail -n monitoring
```

정상 기준:

```text
- DaemonSet successfully rolled out
- 모든 Promtail Pod 1/1 Running
- Helm status: deployed
```

### 7.2 Promtail live config 확인

```bash
kubectl get secret promtail -n monitoring \
  -o jsonpath='{.data.promtail\.yaml}' \
  | base64 -d \
  | sed -n '/pipeline_stages:/,/kubernetes_sd_configs:/p'
```

`replace` stage와 `[REDACTED_*]` 치환 값이 보여야 한다.

### 7.3 로그 redaction 동작 검증

실제 secret을 사용하지 않는다. 반드시 dummy 값만 사용한다.

```bash
kubectl run redaction-test-v2 \
  -n monitoring \
  --image=busybox:1.36 \
  --restart=Never \
  --command -- sh -c 'echo "Authorization: Bearer dummy.jwt.token"; echo "DB_PASSWORD=dummy-password"; echo "https://example-bucket.s3.amazonaws.com/test.wav?X-Amz-Signature=dummy-signature"; sleep 30'
```

Grafana Loki Explore에서 조회:

```logql
{namespace="monitoring", pod="redaction-test-v2"}
```

기대 결과:

```text
[REDACTED_AUTHORIZATION]
[REDACTED_SENSITIVE_FIELD]
[REDACTED_PRESIGNED_URL]
```

보이면 안 되는 값:

```text
dummy.jwt.token
dummy-password
X-Amz-Signature=dummy-signature
```

정리:

```bash
kubectl delete pod redaction-test-v2 -n monitoring
```

### 7.4 OTel Collector live config 확인

```bash
kubectl get configmap otel-collector-config \
  -n utterai-observability \
  -o yaml | grep -E "attributes/redact|http.url|aws.s3.key|audio.object_key|rag.key|queue.name"
```

### 7.5 OTel Collector 로그 확인

```bash
kubectl logs -n utterai-observability deploy/otel-collector --tail=100
```

정상 기준:

```text
- unknown processor 없음
- failed to build pipelines 없음
- cannot unmarshal 없음
- error reading config 없음
```

### 7.6 Trace 확인

API 요청을 발생시킨 뒤 Grafana Tempo에서 최근 trace를 확인한다.

```bash
curl https://api.utterai.org/health
```

`/health`가 tracing 제외 또는 sampling으로 보이지 않을 수 있다. 그 경우 실제 API 요청으로 확인한다.

확인할 것:

```text
- trace가 계속 들어오는가
- http.url attribute가 남지 않는가
- aws.s3.key / audio.object_key / rag.key / queue.name 이 남지 않는가
```

---

## 8. 운영 중 문제 해결

### 8.1 Terraform apply가 Promtail에서 timeout 되는 경우

증상:

```text
module.eks_addons.helm_release.promtail: Still modifying...
Error: context deadline exceeded
```

확인:

```bash
kubectl get ds promtail -n monitoring -o wide
kubectl get pods -n monitoring -l app.kubernetes.io/name=promtail -o wide
kubectl describe pod -n monitoring <pending-promtail-pod>
```

`Too many pods`가 원인이면 Promtail 설정 문제가 아니라 노드 pod capacity 문제다. 해당 노드의 pod slot을 하나 비운 뒤 다시 apply한다.

```bash
kubectl get pods -A --field-selector spec.nodeName=<node-name> -o wide
```

복제본이 있는 addon Pod 하나를 재배치한 뒤 Promtail rollout을 확인한다.

```bash
kubectl rollout status daemonset/promtail -n monitoring
terraform -chdir=terraform/environments/prod/04-addons plan
```

### 8.2 Loki에 과거 dummy token이 계속 보이는 경우

Loki는 이미 저장된 로그를 다시 redaction하지 않는다. Promtail 수정 후에는 새 Pod 이름으로 테스트하고, Grafana 시간 범위를 수정 이후로 좁힌다.

---

## 9. 한계와 후속 과제

이 redaction은 관측성 파이프라인의 안전망이다. 가장 좋은 보안은 애플리케이션 코드에서 민감정보를 애초에 로그로 찍지 않는 것이다.

후속으로 고려할 수 있는 작업:

```text
- BE/AI 공통 logger redaction filter 추가
- request/response body 전체 로그 금지 정책 명문화
- trace attribute allowlist 방식 검토
- metric label lint/test 추가
- 로그 샘플 기반 redaction 회귀 테스트 자동화
```

---

## 10. 관련 파일

| 파일 | 역할 |
|---|---|
| `terraform/modules/eks-addons/main.tf` | Promtail Helm values 및 log redaction stage |
| `k8s/platform/observability/base/otel-collector.yaml` | OTel Collector attribute redaction processor |
| `docs/prod/README.md` | Prod 모니터링 구성 요약 |
| `docs/prod/security.md` | Prod 보안 현황 |
| `docs/shared/monitoring-runbook.md` | Grafana/Loki/Prometheus 운영 확인 절차 |
