# GitOps 심화: 롤백의 호환성 범위와 캐시 정합성

## 검증 범위

이전 App of Apps·KEDA/HPA·캐시 실행 횟수 검증에 두 가지 설계 질문을 추가했다. 실제 Argo CD 3.4.3과 로컬 SQS 호환 ElasticMQ를 사용하며, Worker는 정수 값을 두 배로 만드는 모의 프로그램이다. UtterAI API/Worker 애플리케이션 코드를 변경한 것은 아니다.

- [Git revert와 잔존 메시지 실험](evidence/rollback-contract.json)
- [독립 렌더링 감사 실험](evidence/cache-audit.json)
- [실행 코드·정책 검증 해시](evidence/phase3-verification.json)

## 1. 독립 배포와 독립 롤백의 조건은 다르다

### 재현

v1 reader는 schema 1의 value 필드만 읽고, v2 reader는 schema 1과 schema 2의 input.value를 읽는다. v2 배포 commit은 reader version과 소비 일시 중지 설정만 바꾼다. 소비를 잠시 중지해 구·신 메시지 5개씩 총 10개를 큐에 남긴 뒤, 그 v2 commit을 실제 Git revert하여 v1으로 복귀했다.

Argo CD는 Synced/Healthy였고, 유효한 구형 메시지 5개는 정상 처리됐다. 그러나 신형 메시지 5개는 각각 3번째 수신에서 테스트용 격리 큐로 이동했다. Worker readiness는 프로세스 시작과 입력 큐 연결을 확인한다. 실제 신형 메시지 처리 호환성은 readiness와 별개의 계약이다.

v1 동작을 유지하면서 두 메시지 형식을 읽는 v1-bridge compatibility reader로 전환하고, 격리된 5개를 입력 큐에 재발행했다. 알려진 10개 job_id의 결과가 모두 기대값과 일치했고, 이 코호트에서는 출력 중복이 관측되지 않았다.

### 설계 결론

- Git revert는 큐의 메시지나 이미 발생한 외부 효과를 되돌리지 않는다.
- 롤백 허용 여부를 reader가 지원하는 schema와 잔존할 수 있는 schema의 포함 관계로 정의했다.
- 구형 reader를 호환 release로 먼저 확장한 뒤 새 형식의 writer를 활성화하는 순서를 검토한다.
- 신형 writer를 먼저 되돌리더라도 신형 메시지가 남아 있으면 reader를 즉시 예전 버전으로 되돌릴 수는 없다.
- 계약 제거 조건에는 visible뿐 아니라 in-flight·delayed·격리 큐·재처리 가능 기간을 포함해야 한다. Approximate queue depth=0만으로 계약 제거를 허용하지 않는다.

`contract_policy.py`는 이 포함 관계를 판정하는 prototype이며 production Argo CD PreSync gate로 연결하지 않았다. 실험의 입력 schema 집합은 알려진 모의 코호트에서 주어졌다. 실제 운영에서 이를 자동 판단하려면 producer 버전과 잔존 작업 계약을 추적하는 근거가 필요하다.

### 한계와 트레이드오프

한 코호트의 10개 모의 작업을 검증한 결과다. 실제 음성 처리, DB schema 변경, ML 모델 기동, 처리량 또는 exactly-once를 검증하지 않는다. output 발행과 input 삭제 사이의 crash에 대한 원자성도 보장하지 않는다. Deployment는 Recreate로 설정해 제어된 롤백을 재현했으며, 여러 reader 버전의 동시 실행은 unit compatibility matrix로만 확인했다.

호환 reader를 오래 유지하면 배포 독립성이 높아지는 대신, 지원할 계약과 테스트 조합이 늘어난다. 따라서 모든 버전을 무기한 지원하는 대신 계약 지원 기간과 제거 조건을 명시하는 방향이다.

## 2. 캐시 최적화의 정합성을 별도 계산으로 확인한다

### 재현과 결과

| 조건 | Argo CD 관측 | 독립 렌더링과 실제 ConfigMap 비교 |
| --- | --- | --- |
| 공통 base 감시 경로 누락, 변경 3회 | 매회 Synced/Healthy, 실제 Kustomize 생성 0회 | 3회 모두 불일치 탐지 |
| 감시 경로 수정 및 명시적 hard refresh·sync | Synced/Healthy | 3회 모두 일치 |
| 외부 base가 main을 추적하고 외부 Git만 변경 | 애플리케이션 Git SHA 동일, Synced/Healthy | 불일치 탐지 |
| 외부 base를 commit SHA로 고정 후 외부 main 변경 | 고정된 입력 유지 | 일치 확인 |

독립 렌더링은 repo-server와 같은 Kustomize 바이너리를 사용하되, 별도의 임시 checkout에서 정확한 application revision을 렌더링한다. manifest cache와 기존 Git checkout을 재사용하지 않는다. 외부 base가 mutable이면 application revision만 고정해도 입력 전체가 고정되지 않는다는 점을 별도 fixture로 확인했다.

비교 대상은 cache-based sync 이후의 **Live ConfigMap**과 독립 렌더링 결과다. Redis에 저장된 raw desired manifest를 직접 추출하는 구현은 아니다. fixture에는 Live ConfigMap을 변경하는 다른 controller가 없고, Application이 Synced임을 확인한 뒤 비교했다. 감사 전후 Live ConfigMap fingerprint가 같은지도 검사해 읽기 전용 동작을 확인했다. 감사가 자동으로 운영 리소스를 수정하거나 production 승격을 중단하는 것은 아니다.

### 설계 결론

- 경로 기반 CI 검사에 더해 캐시와 다른 계산 경로로 정합성을 확인할 수 있다.
- 원격 입력도 immutable revision으로 고정해야 application Git SHA만 보고 재현성을 판단하는 오류를 줄일 수 있다.
- 공통 의존성 변경은 우선 검증하고, 나머지는 주기 또는 표본 검사를 적용하는 방안을 검토한다.
- 모든 변경을 재렌더링하면 캐시 이득이 줄어든다. 표본 검사만 하면 탐지 지연이 남으므로 검사 범위와 주기를 비용·위험에 맞춰 결정해야 한다.

현재 감사 함수는 ConfigMap의 identity/data/binaryData/immutable 비교용 prototype이다. 일반 Kubernetes 리소스의 semantic diff 엔진이나 전체 운영 모니터링으로 소개하지 않는다. 외부 base는 설계 한계를 검증하기 위한 별도 로컬 fixture이고, 기존 UtterAI manifest에 새로 도입하지 않았다.

## 실행

App of Apps 문서의 kind와 로컬 Gitea가 준비되어 있어야 한다. 기존 실험과 전역 설정을 동시에 변경하지 않는다.

```bash
python3 -m pip install -r scripts/gitops/lab-requirements.txt
docker build -t utterai/gitops-contract-reader:lab tests/gitops/contract-image
kind load docker-image utterai/gitops-contract-reader:lab --name utterai-gitops
python3 tests/gitops/rollback_contract.py --kubeconfig /private/tmp/utterai-gitops.kubeconfig --output /private/tmp/rollback-contract.json
python3 tests/gitops/cache_audit.py --kubeconfig /private/tmp/utterai-gitops.kubeconfig --output /private/tmp/cache-audit.json
python3 -m unittest discover -s tests/gitops -p 'test_*.py' -v
```

필요하면 GITOPS_KUBECTL로 서버 버전에 맞는 kubectl 경로를 지정한다. 새 실험은 독립 Git repository·AppProject·namespace와 SQS fixture를 생성한다. 결과의 repository/workDirectory/queueFixture로 리소스를 식별할 수 있다.

이번 실행 중 Docker VM 공간 고갈과 로컬 API/SQS timeout이 있었다. 사용하지 않는 SDK 이미지 캐시와 완료된 자체 SQS 컨테이너를 정리하고 작은 Worker 이미지를 사용한 뒤 새 fixture에서 완료했다. 중단된 시도는 성공 사례로 집계하지 않았다. 다른 프로젝트 컨테이너와 EKS는 변경하지 않았다.

## 근거

- [AWS: 구·신 버전 공존과 rollback 호환성](https://docs.aws.amazon.com/wellarchitected/latest/devops-guidance/dl.ads.5-ensure-backwards-compatibility-for-data-store-and-schema-changes.html)
- [Argo CD 3.4: 캐시와 외부 의존성](https://argo-cd.readthedocs.io/en/release-3.4/operator-manual/high_availability/)
