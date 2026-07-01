# Terraform CI Provider Cache Troubleshooting

## 상황

GitHub Actions의 Terraform CI에서 다음과 같은 오류가 발생할 수 있다.

```text
Terraform init: ./terraform/modules/secrets
Initializing provider plugins found in the configuration...
- Finding latest version of hashicorp/aws...
- Installing hashicorp/aws v6.53.0...

Error: Failed to install provider

Error while installing hashicorp/aws v6.53.0:
write .terraform/providers/registry.terraform.io/hashicorp/aws/6.53.0/linux_amd64/terraform-provider-aws_v6.53.0_x5:
no space left on device
```

겉으로는 GitHub Actions runner의 디스크 부족 문제처럼 보이지만, 실제로는 Terraform provider 해석 방식과 CI 검증 방식이 같이 만든 문제다.

## 원인

현재 Terraform CI는 `*.tf` 파일이 있는 모든 디렉터리를 찾아서 각 디렉터리에서 `terraform init -backend=false`와 `terraform validate`를 실행한다.

즉, 실제 배포 단위인 environment 디렉터리뿐 아니라 다음과 같은 재사용 module 디렉터리도 독립적으로 init된다.

```text
terraform/modules/secrets
terraform/modules/vpc
terraform/modules/eks
terraform/modules/rds
```

이때 module 내부에 `required_providers` 제약이 없으면 Terraform은 해당 provider의 최신 버전을 찾는다.

예를 들어 `terraform/modules/secrets`에 provider 제약이 없으면 다음처럼 동작한다.

```text
Finding latest version of hashicorp/aws...
Installing hashicorp/aws v6.53.0...
```

반면 root environment들은 보통 다음처럼 AWS provider를 `~> 5.0`으로 고정하고 있다.

```hcl
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

따라서 module을 단독 init할 때 root environment와 다른 provider major version을 받을 수 있고, 여러 디렉터리에서 provider를 반복 설치하면서 GitHub-hosted runner의 디스크를 빠르게 소모할 수 있다.

## 수정 방향

### 1. Module에도 provider version constraint를 둔다

재사용 module도 CI에서 독립적으로 init될 수 있으므로, module 내부에 최소한의 `versions.tf`를 둔다.

예시:

```hcl
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
```

여러 provider를 사용하는 module은 실제 사용하는 provider를 함께 명시한다.

예시:

```hcl
terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
```

이렇게 하면 module을 단독으로 init해도 root environment와 같은 provider major version을 사용한다.

### 2. CI에서 Terraform plugin cache를 사용한다

Terraform은 기본적으로 각 working directory의 `.terraform/providers` 아래에 provider plugin을 설치한다.

CI가 여러 Terraform 디렉터리를 순회하면 같은 provider를 반복 다운로드하거나 압축 해제할 수 있다. 이를 줄이기 위해 `TF_PLUGIN_CACHE_DIR`를 설정한다.

예시:

```yaml
jobs:
  terraform-check:
    runs-on: ubuntu-latest
    env:
      TF_PLUGIN_CACHE_DIR: /tmp/terraform-plugin-cache

    steps:
      - name: Prepare Terraform plugin cache
        run: mkdir -p "$TF_PLUGIN_CACHE_DIR"
```

주의할 점:

```yaml
env:
  TF_PLUGIN_CACHE_DIR: ${{ runner.temp }}/terraform-plugin-cache
```

위 설정은 job-level `env`에서 actionlint 오류가 날 수 있다.

`runner` context는 모든 위치에서 사용할 수 있는 context가 아니므로, job-level env에서는 `/tmp/terraform-plugin-cache`처럼 고정 경로를 사용하는 편이 안전하다.

## 재현 및 확인 방법

문제가 발생한 module에서 init를 실행해 provider 버전이 의도대로 잡히는지 확인한다.

```bash
terraform -chdir=terraform/modules/secrets init -backend=false
```

정상적인 경우:

```text
Finding hashicorp/aws versions matching "~> 5.0"...
Installing hashicorp/aws v5.x.x...
Terraform has been successfully initialized!
```

validate도 함께 확인한다.

```bash
terraform -chdir=terraform/modules/secrets validate
```

전체 Terraform formatting 확인:

```bash
terraform fmt -check -recursive
```

## CI/CD 팀 체크리스트

- Terraform CI가 environment만 검증하는지, module까지 단독 검증하는지 확인한다.
- module까지 단독 검증한다면 module에도 `required_providers`를 명시한다.
- root environment와 module의 provider major version이 다르지 않게 맞춘다.
- GitHub Actions에서 여러 Terraform 디렉터리를 순회한다면 `TF_PLUGIN_CACHE_DIR`를 설정한다.
- job-level `env`에서 사용할 수 있는 GitHub Actions context인지 actionlint로 확인한다.
- provider cache는 CI 실행 중 재사용 목적이며, Terraform state나 lock을 대체하지 않는다.

## 이번 케이스의 결론

이번 오류는 단순히 GitHub Actions runner 용량이 작아서만 발생한 것이 아니다.

핵심은 `terraform/modules/secrets`가 provider 버전을 고정하지 않아 Terraform이 최신 AWS provider `v6.53.0`을 설치하려 했고, CI가 여러 Terraform 디렉터리를 순회하면서 provider 설치 비용이 커졌다는 점이다.

따라서 다음 두 가지를 함께 적용했다.

1. Terraform modules에 `versions.tf`를 추가해 provider version constraint를 명시한다.
2. Terraform CI에 `TF_PLUGIN_CACHE_DIR`를 추가해 provider plugin 설치를 캐시한다.

