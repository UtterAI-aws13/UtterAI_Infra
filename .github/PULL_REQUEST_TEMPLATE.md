## Summary

이 PR이 무엇을 바꾸는지 간단히 적는다.

## Changes

- 
- 

## Why

변경 이유를 적는다.

## Impact

- Terraform: (추가/변경/삭제될 AWS 리소스)
- ArgoCD: (영향받는 Application, 네임스페이스, 환경)
- GitHub Actions: (영향받는 워크플로우, 트리거 조건)
- IAM / Security Group: (권한 범위 변경)
- Cost: (비용 증감 예상)

## Terraform Plan

`terraform plan` 결과를 GitHub Actions 로그에서 복사하거나 요약한다.

```
(plan output 또는 요약)
```

## Test

- [ ] `terraform plan` 결과 확인
- [ ] `terraform validate` / lint 통과
- [ ] ArgoCD sync 영향 범위 확인
- [ ] GitHub Actions 워크플로우 실행 확인
- [ ] 수동 검증 수행 또는 미실행 사유 기재

## Checklist

- [ ] base branch가 올바르다 (Infra repo는 `feature/*` → `main`)
- [ ] 관련 이슈를 연결했다
- [ ] tfstate, kubeconfig, access key, secret 값이 포함되지 않았다
- [ ] IAM / Security Group 설정이 최소 권한 원칙을 따른다
- [ ] 불필요한 destroy가 없음을 `terraform plan`으로 확인했다
- [ ] 문서가 필요하면 함께 수정했다

## References

관련 이슈, 아키텍처 문서, AWS 콘솔 링크, 스크린샷, 로그를 적는다.
