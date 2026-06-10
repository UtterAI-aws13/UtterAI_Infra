# UtterAI Infra

AWS EKS 기반 AI 음성 분석 플랫폼의 인프라 구성을 관리하는 저장소입니다.

## Repository Structure

```text
UtterAI_Infra/
├── terraform/          # IaC — VPC, EKS, RDS, Redis, S3, SQS, IRSA 등
│   ├── modules/        # 재사용 가능한 공통 모듈
│   └── environments/   # 환경별 변수 조합 (dev / prod)
│
├── k8s/                # Kubernetes 매니페스트 (Namespace, RBAC, Workload, Ingress)
├── k8s-demo/           # Kustomize 구조 (base + overlays) — ArgoCD 배포 대상
│
├── docs/               # 인프라 설계 및 운영 문서
│   ├── dev/            # Dev 환경 가이드, 보안, 부하 테스트 시나리오
│   └── prod/           # Prod 환경 가이드
│
├── observability/      # 로컬 관측성 스택 (Docker Compose)
├── tests/              # 부하 테스트 스크립트 (CA+HPA / Karpenter+KEDA)
├── scripts/            # 배포 자동화 및 마이그레이션 스크립트
└── deploy/             # 배포 관련 설정
```

## Documents

| 문서 | 용도 |
|------|------|
| [`docs/README.md`](docs/README.md) | 인프라 환경 개요 (Dev vs Prod 비교, AWS 계정 구조) |
| [`docs/dev/README.md`](docs/dev/README.md) | Dev 환경 배포 가이드 (Terraform 적용 순서, K8s 배포) |
| [`docs/dev/security-overview.md`](docs/dev/security-overview.md) | Dev 환경 전체 보안 구현 현황 |
| [`docs/dev/security-hardening.md`](docs/dev/security-hardening.md) | 보안 수정 이력 및 잔여 항목 |
| [`docs/dev/load-test-scenarios.md`](docs/dev/load-test-scenarios.md) | CA+HPA / Karpenter+KEDA 부하 테스트 시나리오 |
| [`observability/README.md`](observability/README.md) | 로컬 observability 스택 실행 방법 |

## Local Observability

```bash
cd observability
docker compose up -d
```

| 서비스 | URL |
|--------|-----|
| Grafana | http://localhost:3000 (admin / admin) |
| Prometheus | http://localhost:9090 |
| Tempo | http://localhost:3200 |
| OTLP gRPC | localhost:4317 |
| OTLP HTTP | http://localhost:4318 |

## Branch Strategy

| 브랜치 | 용도 |
|--------|------|
| `main` | 프로덕션 기준 브랜치. ArgoCD가 트래킹하여 자동 sync. |
| `dev` | 기본 개발 브랜치. PR 시 GitHub Actions가 terraform plan / lint 자동 실행. |
| `feature/*` | 새 인프라 리소스 추가 |
| `fix/*` | 버그 수정 |
| `docs/*` | 문서 작업 |
| `ci/*` | GitHub Actions 워크플로우 변경 |
| `security/*` | 보안 관련 변경 |

상세 규칙은 [`CONTRIBUTING.md`](CONTRIBUTING.md)를 따른다.
