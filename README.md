# UtterAI Infra

UtterAI 인프라 구성과 로컬 관측성 환경을 관리하는 저장소입니다.

현재 주요 범위는 EKS 인프라 구성과 로컬 observability Docker Compose입니다.

## Repository Structure

```text
UtterAI_Infra/
├── docs/
│   ├── dev/
│   │   ├── README.md
│   │   └── security-hardening.md
│   ├── prod/
│   │   └── README.md
│   ├── eks-architecture-flow.md
│   └── README.md
├── observability/
│   ├── docker-compose.yml
│   ├── grafana/
│   ├── nginx/
│   ├── otel-collector/
│   ├── postgres/
│   ├── prometheus/
│   ├── tempo/
│   └── README.md
└── README.md
```

## Main Documents

| 문서 | 용도 |
| --- | --- |
| `observability/README.md` | 로컬 observability stack 실행 방법 |
| `docs/README.md` | EKS 인프라 구성 진입점 |
| `docs/dev/README.md` | Dev 환경 배포 가이드 |
| `docs/dev/security-overview.md` | Dev 환경 전체 보안 구현 현황 |
| `docs/dev/security-hardening.md` | 보안 수정 이력 |

## Local Observability

```bash
cd observability
docker compose up -d
docker compose ps
```

주요 접속 URL:

```text
Nginx entry:    http://localhost:8080
Prometheus:     http://localhost:9090/graph
Grafana:        http://localhost:3000
Tempo API:      http://localhost:3200
OTLP gRPC:      localhost:4317
OTLP HTTP:      http://localhost:4318
```

Grafana 기본 로그인은 `admin / admin`입니다.

## Branch Strategy

| 브랜치 | 용도 |
|--------|------|
| `main` | 프로덕션 기준 브랜치. ArgoCD가 이 브랜치를 트래킹하여 자동 sync한다. |
| `dev` | 기본 개발 브랜치. PR 생성 시 GitHub Actions가 `terraform plan` / lint를 자동 실행한다. |
| `feature/*` | 새 인프라 리소스 추가 (Terraform 모듈, ArgoCD Application, GitHub Actions 워크플로우) |
| `fix/*` | 인프라 버그 수정 |
| `docs/*` | 문서 작업 |
| `ci/*` | GitHub Actions 워크플로우 변경 |

상세 규칙은 `CONTRIBUTING.md`를 따른다.
