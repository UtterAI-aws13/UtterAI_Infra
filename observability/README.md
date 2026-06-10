# UtterAI Observability

이 디렉토리는 UtterAI 로컬 개발 환경에서 관측성 스택을 실행하기 위한 구성입니다.

현재 범위는 FE/BE/AI 앱 전체 실행 환경이 아니라, 앱과 독립적으로 실행 가능한 로컬 인프라 및 관측성 환경입니다. Metrics는 Prometheus/Grafana, traces는 OpenTelemetry Collector/Tempo로 검증합니다.

## 포함된 서비스

```text
Nginx
PostgreSQL + pgvector
postgres_exporter
cAdvisor
nginx_exporter
Prometheus
OpenTelemetry Collector
Tempo
Grafana
```

## 실행 방법

```bash
cd UtterAI_Infra/observability
docker compose up -d
docker compose ps
```

정상 실행 시 다음 서비스가 `Up` 상태여야 합니다.

```text
nginx
postgres
postgres_exporter
cadvisor
nginx_exporter
prometheus
otel-collector
tempo
grafana
```

## 접속 URL

```text
Nginx entry:          http://localhost:8080
Prometheus UI:        http://localhost:9090/graph
Prometheus targets:   http://localhost:9090/targets
Prometheus via Nginx: http://localhost:8080/prometheus/
Grafana:              http://localhost:3000
Tempo API:            http://localhost:3200
OTLP gRPC:            localhost:4317
OTLP HTTP:            http://localhost:4318
OTel metrics:         http://localhost:8889/metrics
cAdvisor:             http://localhost:8081
Nginx status:         http://localhost:8080/nginx_status
postgres_exporter:    http://localhost:9187/metrics
nginx_exporter:       http://localhost:9113/metrics
```

Grafana 로그인:

```text
admin / admin
```

Grafana 대시보드:

```text
Dashboards -> UtterAI -> UtterAI Observability Overview
```

## 요청 흐름

Nginx는 로컬 단일 진입점 역할을 합니다.

```text
Browser
  -> http://localhost:8080
      -> /                 -> FE host port 5173
      -> /api/v1/*         -> BE host port 8000
      -> /backend-health   -> BE /health
      -> /ai/*             -> AI host port 8001
      -> /ai-health/live   -> AI /health/live
      -> /ai-health/ready  -> AI /health/ready
      -> /prometheus/*     -> Prometheus
      -> /grafana/*        -> Grafana direct URL로 redirect
```

FE/BE/AI 앱은 현재 Docker Compose에 포함되어 있지 않습니다. 각 repo에서 별도로 실행해야 합니다.

## OpenTelemetry

OpenTelemetry Collector는 host에서 실행하는 앱의 OTLP metrics/traces를 받습니다.

```text
OTLP gRPC endpoint: http://localhost:4317
OTLP HTTP endpoint: http://localhost:4318
```

Collector pipeline:

```text
metrics -> Prometheus exporter(:8889) -> Prometheus scrape
traces  -> tail sampling -> Tempo(:4317) -> Grafana Explore
```

trace sampling 정책:

| 정책 | 기준 |
| --- | --- |
| `errors` | ERROR status trace 보존 |
| `slow` | 3000ms 이상 trace 보존 |
| `normal-sampled` | 일반 trace 5% 샘플링 |

Prometheus는 `otel-collector` job으로 Collector의 Prometheus exporter를 scrape합니다.

## 데이터베이스

PostgreSQL 컨테이너는 로컬 개발용 DB를 생성합니다.

```text
utterai    -> BE용 DB
utterai_ai -> AI/RAG용 DB
```

두 DB 모두 `vector` extension을 활성화합니다.

Host에서 실행하는 서비스의 DB 연결 문자열:

```text
BE: postgresql+psycopg://utterai:utterai@localhost:5432/utterai
AI: postgresql+psycopg://utterai:utterai@localhost:5432/utterai_ai
```

## 수집하는 지표

현재 대시보드는 앱 내부 지표가 아니라 인프라 핵심 지표를 먼저 수집합니다.

| 지표 | 수집 대상 | 의미 |
| --- | --- | --- |
| `up` | Prometheus targets | scrape 대상이 살아있는지 확인합니다. |
| `pg_up` | postgres_exporter | PostgreSQL exporter가 DB에 접근 가능한지 확인합니다. |
| `pg_stat_database_numbackends` | PostgreSQL | DB connection 증가를 확인합니다. |
| `pg_database_size_bytes` | PostgreSQL | DB 용량 증가를 확인합니다. |
| `container_cpu_usage_seconds_total` | cAdvisor | 컨테이너 CPU 사용량을 확인합니다. |
| `container_memory_working_set_bytes` | cAdvisor | 컨테이너 메모리 사용량을 확인합니다. |
| `nginx_up` | nginx_exporter | Nginx exporter 상태를 확인합니다. |
| `nginx_connections_active` | Nginx | Nginx active connection 수를 확인합니다. |
| 앱 OTLP metrics | OpenTelemetry Collector | BE/AI/FE가 내보낸 앱 지표를 Prometheus에서 확인합니다. |

## 검증 명령어

Prometheus target 확인:

```bash
curl http://localhost:9090/api/v1/targets
```

PostgreSQL 상태 확인:

```bash
curl "http://localhost:9090/api/v1/query?query=pg_up"
```

cAdvisor 상태 확인:

```bash
curl "http://localhost:9090/api/v1/query?query=up%7Bjob%3D%22cadvisor%22%7D"
```

Nginx 상태 확인:

```bash
curl http://localhost:8080/nginx_status
curl "http://localhost:9090/api/v1/query?query=nginx_up"
```

OpenTelemetry Collector metrics scrape 확인:

```bash
curl "http://localhost:9090/api/v1/query?query=up%7Bjob%3D%22otel-collector%22%7D"
curl http://localhost:8889/metrics
```

## 그래프 변화 테스트

PostgreSQL connection 그래프가 실제로 변하는지 확인하려면 다음 명령을 실행합니다.

```bash
cd UtterAI_Infra/observability

docker compose exec -T postgres sh -c 'for i in $(seq 1 10); do psql -U utterai -d utterai -c "select pg_sleep(120);" & done; wait'
```

명령이 실행되는 동안 다른 터미널에서 현재 connection 수를 확인합니다.

```bash
cd UtterAI_Infra/observability

docker compose exec -T postgres psql -U utterai -d utterai -c "select datname, state, count(*) from pg_stat_activity where datname in ('utterai','postgres') group by datname, state order by datname, state;"
```

Prometheus에서도 같은 값을 확인할 수 있습니다.

```bash
curl "http://localhost:9090/api/v1/query?query=sum(pg_stat_database_numbackends%7Bdatname!~%22template.*%22%7D)"
```

Grafana에서는 우측 상단을 다음처럼 설정하면 변화가 잘 보입니다.

```text
Time range: Last 5 minutes
Refresh: 5s 또는 10s
```

정상 동작하면 `PostgreSQL Connections` 그래프가 증가했다가 120초 후 다시 감소합니다.

## 종료 방법

잠깐 중지:

```bash
docker compose stop
```

다시 실행:

```bash
docker compose up -d
```

컨테이너 제거:

```bash
docker compose down
```

볼륨 데이터까지 초기화:

```bash
docker compose down -v
```

`down -v`는 PostgreSQL, Grafana, Prometheus, Tempo 데이터를 삭제합니다. 시연 중에는 사용하지 않는 것이 좋습니다.

## AWS 확장 방향

이 구성은 AWS 운영 환경 자체가 아니라, Prometheus/Grafana/Tempo 기반 관측 흐름을 로컬에서 검증하기 위한 기준 환경입니다.

AWS/EKS 환경에서는 Docker Compose를 그대로 사용하는 것이 아니라 Terraform, Helm, Kubernetes manifest 등을 통해 별도 인프라 구성으로 확장하는 것이 적절합니다.

비용을 줄이기 위해 운영 환경에서는 다음 원칙을 우선합니다.

```text
핵심 metrics부터 수집
너무 짧은 scrape interval 지양
dev 환경 retention 짧게 유지
logs/traces는 전량 수집보다 filtering 또는 sampling 적용
```

## 참고

환경변수 예시는 `env/` 디렉토리에 있습니다.

```text
env/frontend.env.example
env/backend.env.example
env/ai.env.example
```

FE/BE/AI를 host에서 실행할 때 local-dev 인프라에 연결하기 위한 참고용입니다.
