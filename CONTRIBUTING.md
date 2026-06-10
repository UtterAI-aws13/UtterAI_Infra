# Contributing

## Branch Rules

- `main`은 Infra repo의 단일 기준 브랜치다.
- dev/prod 환경 분리는 브랜치가 아니라 Kustomize overlay path로 관리한다.
- 새 작업은 `main`에서 분기하고, PR base도 `main`으로 둔다.
- `main`으로의 PR 생성 시 GitHub Actions가 Terraform/Kustomize 검증을 실행한다.
- Argo CD는 같은 `main` 브랜치에서 `overlays/dev`, `overlays/prod` 경로를 각각 트래킹한다.
- 브랜치 네이밍 규칙:
  - `feature/<issue-number>-<short-name>` — 신규 리소스/모듈/앱 추가
  - `fix/<issue-number>-<short-name>` — 인프라 버그 수정
  - `docs/<issue-number>-<short-name>` — 문서 변경
  - `chore/<issue-number>-<short-name>` — 설정, 의존성 등 비기능 변경
  - `ci/<issue-number>-<short-name>` — GitHub Actions 워크플로우 변경

예시:

- `feature/12-eks-nodegroup`
- `feature/18-karpenter-gpu-nodepool`
- `fix/21-private-subnet-route`
- `ci/07-terraform-plan-workflow`
- `docs/03-infra-architecture-update`

## Commit Rules

커밋 메시지는 아래 형식을 따른다:

```
<type>(<scope>): <description>
```

- 커밋 메시지는 짧고 목적이 분명해야 한다.
- 커밋은 가능하면 하나의 인프라 변경 단위만 담는다.

### Type

| type | 설명 |
|------|------|
| `feat` | 신규 리소스/모듈/기능 추가 |
| `fix` | 인프라 버그 수정 |
| `docs` | 문서 변경 |
| `refactor` | 기능 변경 없이 구조 개선 |
| `test` | 테스트 추가 또는 수정 |
| `chore` | 설정, 의존성 등 비기능 변경 |
| `ci` | GitHub Actions 워크플로우 변경 |

### Scope

변경 대상 인프라 영역을 명시한다. 생략 가능하나, 범위가 명확할 때는 적는 것을 권장한다.

| scope | 설명 |
|-------|------|
| `eks` | EKS 클러스터 및 노드그룹 |
| `karpenter` | Karpenter 노드풀/프로비저너 |
| `network` | VPC, 서브넷, 라우팅 |
| `iam` | IAM 역할/정책 |
| `rds` | RDS 인스턴스/설정 |
| `s3` | S3 버킷/정책 |
| `argocd` | ArgoCD Application/설정 |
| `cicd` | CI/CD 파이프라인 |
| `kms` | KMS 키/정책 |
| `sg` | Security Group |

### 예시

- `feat(eks): add system nodegroup`
- `feat(karpenter): add gpu nodepool`
- `fix(network): update private subnet route table`
- `ci(cicd): add terraform plan workflow`
- `docs: update infra architecture diagram`
- `chore(iam): rotate service account role`

## Issue Rules

- 작업 시작 전 이슈를 먼저 만든다.
- 하나의 이슈는 하나의 목적에 집중한다.
- 이슈에는 배경, 목표, 완료 조건을 반드시 적는다.
- 인프라 이슈는 가능하면 대상 환경과 영향 범위를 명시한다.
  - 예: `dev`, `prod`, `eks`, `karpenter`, `rds`, `s3`, `iam`, `network`, `cicd`

## Pull Request Rules

- `feature/*`, `fix/*`, `ci/*`, `docs/*`, `chore/*` 브랜치의 PR base는 `main`이다.
- dev/prod 배포 차이는 PR base가 아니라 변경되는 Kustomize overlay path와 GitHub Environment approval로 구분한다.
- PR 하나에는 하나의 논리적 변경만 담는다.
- 초안 상태에서는 `Draft PR`을 사용한다.
- PR 본문에는 변경 내용, 검증 결과, 영향 범위, 리뷰 포인트를 적는다.
- Terraform 변경이 있으면 GitHub Actions가 자동 실행한 `terraform plan` 결과를 본문에 붙인다.
- ArgoCD Application 변경이 있으면 sync 영향 범위(앱, 네임스페이스, 환경)를 명시한다.
- GitHub Actions 워크플로우 변경이 있으면 트리거 조건과 영향받는 파이프라인을 명시한다.
- 보안, 비용, 네트워크, 데이터 계층 영향이 있으면 PR에 명시한다.
- 머지 전 최소 1회 이상 셀프 리뷰를 수행한다.

## Review Checklist

- 요구사항과 실제 변경 범위가 일치하는가
- `terraform plan` 결과가 의도한 변경만 포함하는가
- 불필요한 destroy가 포함되어 있지 않은가
- IAM, Security Group, KMS, Secret 설정이 최소 권한 원칙을 따르는가
- ArgoCD sync 시 의도치 않은 리소스 변경이 발생하지 않는가
- GitHub Actions 워크플로우에 시크릿 노출 위험이 없는가
- 비용이 크게 증가하는 리소스가 추가되지 않았는가
- tfstate, kubeconfig, AWS access key, secret 값이 커밋되지 않았는가
- 롤백 또는 복구 방법이 명확한가
- 테스트 또는 수동 검증 결과가 있는가
