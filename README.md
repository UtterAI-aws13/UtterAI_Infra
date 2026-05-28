# UtterAI_Infra

UtterAI Infra repository.

현재 저장소는 인프라 구성을 위한 코드를 관리한다.

## repository structure

```
infra-repo/
├── .github/
│   ├── pull_request_template.md
│   └── ISSUE_TEMPLATE/
│       ├── infra-change.yml
│       ├── infra-bug.yml
│       └── config.yml
├── envs/
│   ├── dev/
│   └── prod/
├── modules/
│   ├── network/
│   ├── eks/
│   ├── karpenter/
│   ├── security/
│   ├── database/
│   └── observability/
├── helm/
├── scripts/
└── README.md
```

## Main Documents

- 

## Implementation Reference

구현 시 다음 아키텍처를 기준으로 사용한다.

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
