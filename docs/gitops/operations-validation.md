# GitOps 운영 정책: kind 검증

## 범위와 선택

EKS를 기동하지 않고 실제 Argo CD 3.4.3, KEDA 2.16.1, Kubernetes HPA와 로컬 ElasticMQ 1.6.9로 제어 동작을 검증한다. 애플리케이션은 nginx 모의 워크로드이며 실제 AWS IAM, EC2 공급, ECR 이미지와 서비스 트래픽은 측정하지 않는다. 각 실험은 별도 Git 저장소·AppProject·namespace를 사용한다. 기본 kubeconfig나 다른 kind 클러스터는 변경하지 않는다.

이 문서는 App of Apps 편입 검증 이후의 정책 실험이다. 첫 단계의 원본 Child spec 보존 결과는 당시 설정의 결과이며, 여기서 검증한 후속 replica 정책 개선과 구분한다.

## A. Parent 상태와 실제 Child 상태

잘못된 이미지 태그로 Child Deployment의 progress deadline을 초과시켰다. 기본 설정에서는 Child가 Synced/Degraded인데 Parent는 Synced/Healthy였다. Child의 Application 리소스 정의가 Git과 일치하는 것과 워크로드 가용성을 구분해야 한다.

`application-health.lua`는 참여 Child에 `gitops.utterai.io/propagate-health=true` 라벨을 지정한 경우만 상태를 전파한다. 아직 상태가 없거나 Synced가 아니면 Progressing으로 두고, 40자리 고정 revision이면 status.sync.revision이 요청 revision과 같은지도 확인한다. Lua 표준 라이브러리를 열지 않아도 동작하도록 작성했다. 이 검사도 엔드포인트 가용성이나 비즈니스 요청 성공을 검사하는 것은 아니다.

상태 복구 후 Parent가 늦게 갱신되는 현상이 있었다. 복구 절차에는 Child 상태와 실제 Deployment 확인 후 Parent normal refresh를 포함한다. 즉, 이번 결과를 무지연 상태 전파 보장으로 해석하지 않는다.

운영용 Root에는 이 Health customization을 일괄 적용하지 않는다. 상태 집계가 필요한지와 의존성 차단이 필요한지는 별도 결정이다. 실험 스크립트는 기존 argocd-cm 설정을 저장하고 종료 시 복구한다.

## B. Sync Wave와 실패 전파

모의 Add-on Deployment와 후속 워크로드를 별도 Child로 구성하여 비교했다. Add-on은 의도적으로 기동 실패시켰다. 이 실험은 **Application 간 워크로드 readiness 제어**를 검증한다. 실제 KEDA Helm 업그레이드 실패나 CRD 버전 마이그레이션을 검증한 것은 아니다.

| 조건 | 선행 Child 장애 중 후속 배포 |
| --- | --- |
| Wave 0/1, Application Health customization 없음 | 후속 Child 생성·Healthy 확인 |
| Wave 0/1, opt-in Health customization 있음 | 10초 이상의 관측 구간 동안 후속 Child 미생성·Parent operation Running 확인 |
| 같은 Wave, opt-in Health customization 있음 | 장애 Child와 별개로 다른 Child가 생성되어 Healthy 확인 |

Wave 비교의 공통 제어 조건으로 resource.ignoreResourceUpdatesEnabled=false를 사용했고 종료 시 원래 값을 복구했다. 이 설정의 성능 영향은 측정하지 않았으며 운영 기본값으로 채택하지 않는다.

막힌 operation은 이전 Root commit에 고정되어 있었다. 복구는 기존 operation 종료 → 정상 이미지 commit → Child revision 갱신 → 새 Root revision sync → Child/Parent 상태 확인 순서로 수행했다. Git 변경만으로 이미 실행 중인 operation이 새 revision으로 바뀐다고 가정하지 않는다.

결론적으로 API와 SQS Worker처럼 독립적으로 배포 가능한 경계에 일괄 Wave를 추가하지 않는다. 실제 선행 조건이 필요한 경계에서만 Health 평가와 함께 사용한다. Parent를 거치지 않는 직접 Child sync까지 Wave가 차단하는 것은 아니므로, 엄격한 배포 통제가 필요하면 권한과 실행 경로도 함께 설계해야 한다.

## C. Argo CD와 KEDA/HPA의 replica 소유권

Git replicas=1인 Deployment를 생성한 뒤, 로컬 SQS 메시지 4개와 queueLength=1로 HPA가 replicas=4를 만들게 한다. 서로 다른 두 nginx 태그는 같은 이미지 bytes를 가리키며, 이미지 필드 변경으로 rollout을 발생시킨다. 처리량·성능 비교는 하지 않는다.

세 조건을 각각 3회 비교한다: 제외 없음, ignoreDifferences만 설정, ignoreDifferences와 RespectIgnoreDifferences=true 조합. 각 trial은 Deployment watch로 resourceVersion, generation, spec.replicas, readyReplicas와 image 변경을 수집한다. 마지막 HPA 복원 이벤트까지 수집하지 못한 시도는 최종 결과로 사용하지 않는다.

첫 생성 때는 RespectIgnoreDifferences를 사용해도 Git의 replicas=1이 적용된다. 기존 리소스의 sync와 최초 생성을 구분한다. runtime replicas만 제외하고 Git이 관리하는 image를 고의로 변경하는 self-heal 대조 실험도 수행한다.

결과와 반복 횟수는 아래 증거 파일에서 확인한다. 이 결과를 서비스 무중단이나 처리량 개선 수치로 일반화하지 않는다.

## D. 경로 기반 manifest 생성 최적화

여러 Kustomize Application이 같은 Git 저장소와 공통 base를 참조하도록 구성한다. 별도 namespace의 ConfigMap 두 개만 관리해 애플리케이션 실행 부하와 구분한다.

- 변경 종류: 관련 없는 파일 / 특정 Application overlay / 공통 base.
- 조건: annotation 없음 / manifest-generate-paths에 해당 overlay와 공통 base를 모두 포함.
- 각 변경을 3회 반복하고 normal refresh를 요청한다. 측정 중 hard refresh는 사용하지 않는다.
- repo-server의 `kustomize build` 실행 로그를 저장소의 고유 경로로 필터링하여 실제 생성 횟수를 센다.
- GenerateManifest RPC와 Git 요청은 별도 수집한다. RPC 호출 수를 Kustomize 실행 횟수로 간주하지 않는다.
- 공통 base 변경 뒤 모든 실제 ConfigMap에 변경이 반영됐는지 확인한다.

사용 버전에서 명시적 repository type이 없는 fixture는 annotation을 추가해도 생성 감소가 관측되지 않았다. 공식 v3.4.3 소스의 UpdateRevisionForPaths는 repository type=git인 경우 Git 변경 파일 비교를 수행한다. 따라서 성공 조건에는 type: git으로 등록한 테스트 repository Secret을 포함한다. 운영 저장소도 type과 clone depth를 함께 확인해야 한다.

실험은 warm cache와 명시적 normal refresh 조건이다. poll/webhook 감지 시간이나 cold start를 측정하지 않는다. 같은 로컬 클러스터에 다른 검증 워크로드가 있으므로 elapsed time, process CPU 및 전체 RPC 증감은 진단 자료다. 처리량 향상률이나 EKS 운영 성능 수치로 제시하지 않는다.

## 실행 결과 원본

정적·정책 검증 19개 통과. [최종 검증 범위와 코드 해시](evidence/phase2-verification.json).

- [KEDA/HPA replica 정책 원본](evidence/keda-ownership.json)
- [Health/Wave 9개 실험](evidence/health-waves.json)
- [4개 Application 경로 캐시 비교 18개 측정](evidence/manifest-cache-4.json)

replica 정책의 3회 반복 결과는 다음과 같다. 여기서 수량은 Deployment spec.replicas다. rollout 중 readyReplicas는 별도로 변동했으므로 모든 Pod가 항상 Ready였다고 주장하지 않는다.

| 조건 | sync 전 | sync 중 최소 spec.replicas | 최종 | 반복 |
| --- | --- | --- | --- | --- |
| 제외 없음 | 4 | 1 | 4 | 3/3 |
| ignoreDifferences만 | 4 | 1 | 4 | 3/3 |
| ignoreDifferences + RespectIgnoreDifferences | 4 | 4 | 4 | 3/3 |

respect 조건에서도 첫 생성은 Git의 1이 적용됐고, Git이 관리하는 이미지의 drift는 self-heal로 복구됐다. 각 조건 검증 후 큐를 비웠으므로 실험 종료 후 live replicas는 minReplicaCount=1로 돌아갈 수 있다.

Kustomize 실행 횟수는 3회 반복에서 모두 아래와 같았다.

| 변경 | annotation 없음 | overlay와 공통 base 포함 |
| --- | --- | --- |
| 관련 없는 파일 | 4 | 0 |
| 특정 overlay | 4 | 1 |
| 공통 base | 4 | 4 |

공통 base 변경 후 실제 ConfigMap 4개가 모두 변경된 값으로 동기화된 것을 확인했다. GenerateManifest RPC와 Git ls-remote 요청 자체가 0이 된 결과는 아니다. 30개 이상의 합성 규모나 다른 규모로 일반화하지 않는다.

## 실행

전용 kind 및 로컬 Gitea 구성은 [App of Apps 전환 문서](app-of-apps.md)를 따른다. Kubernetes 1.31.9 서버에는 같은 버전의 kubectl 사용을 권장하며 `GITOPS_KUBECTL`로 경로를 지정할 수 있다. 새 실험은 기존 3311 포트의 테스트 Gitea에 독립 repository를 만든다.

```bash
python3 -m pip install -r scripts/gitops/lab-requirements.txt
helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version 2.16.1 \
  --kubeconfig /private/tmp/utterai-gitops.kubeconfig --kube-context kind-utterai-gitops -n keda --create-namespace
kind load docker-image softwaremill/elasticmq:1.6.9 --name utterai-gitops
docker tag nginx:alpine nginx:gitops-v2
kind load docker-image nginx:gitops-v2 --name utterai-gitops
```

Health/Wave는 argocd-cm을 임시 변경하므로 다른 실험과 동시에 실행하지 않는다.

```bash
python3 tests/gitops/advanced.py --kubeconfig /private/tmp/utterai-gitops.kubeconfig --output /private/tmp/health-waves.json
python3 tests/gitops/keda_ownership.py --kubeconfig /private/tmp/utterai-gitops.kubeconfig --trials 3 --output /private/tmp/keda-ownership.json
python3 tests/gitops/manifest_cache.py --kubeconfig /private/tmp/utterai-gitops.kubeconfig --applications 4 --trials 3 --output /private/tmp/manifest-cache-4.json
```

KEDA/HPA는 kind 안에서 동작하고 ElasticMQ는 별도 로컬 Docker 컨테이너다. queue URL과 테스트용 자격증명은 로컬 전용이다. AWS SDK는 로컬 endpoint와 명시적 테스트 자격증명을 사용한다. KEDA 실험은 port-forward를 사용하지 않는다.

실험마다 Git repository, AppProject, Application과 namespace가 생성된다. 결과 JSON에 repository/작업 디렉터리를 기록한다. 리소스를 리뷰한 뒤 해당 experiment 라벨의 Application만 비연쇄 삭제하고 해당 실험 namespace·프로젝트·repository Secret을 정리한다. KEDA 결과의 queueFixture.container도 함께 정리한다. 기존 utterai-dev/prod fixture와 다른 kind 클러스터는 삭제 대상이 아니다.

## 검증 코드에서 수정한 사항

Argo CD Core에는 API server가 없어서 실험 harness는 Application의 operation을 직접 생성한다. 일반 sync API가 Application의 syncOptions를 복사한다는 점을 놓치면 RespectIgnoreDifferences가 실제 operation에 들어가지 않는다. 이 차이를 공식 v3.4.3 server/application/application.go와 controller/sync.go로 확인했고, harness를 수정하고 회귀 테스트를 추가했다. 최종 replica 결과는 실제 operationSyncOptions를 확인한 시도만 사용한다.

초기 port-forward가 서비스 준비 전에 실패하거나 idle connection reset으로 종료한 시도, Deployment watch가 만료되어 최종 복원 이벤트를 놓친 시도, repo-server 연결 실패로 중단된 시도는 최종 반복 결과에 합산하지 않았다. SQS fixture를 로컬 Docker endpoint로 변경하고 watch timeout을 제거했다. 이들은 실제 EKS 장애 사례로 소개하지 않는다.

## 근거

- [Application Health](https://argo-cd.readthedocs.io/en/release-3.4/operator-manual/health/)
- [Sync Waves](https://argo-cd.readthedocs.io/en/release-3.4/user-guide/sync-waves/)
- [RespectIgnoreDifferences](https://argo-cd.readthedocs.io/en/release-3.4/user-guide/sync-options/#respect-ignore-differences-configs)
- [Argo CD v3.4.3 controller/state.go](https://github.com/argoproj/argo-cd/blob/v3.4.3/controller/state.go)
- [Argo CD v3.4.3 repository.go](https://github.com/argoproj/argo-cd/blob/v3.4.3/reposerver/repository/repository.go)
- [KEDA 2.16 AWS SQS scaler](https://keda.sh/docs/2.16/scalers/aws-sqs/)

- [Argo CD v3.4.3 sync API](https://github.com/argoproj/argo-cd/blob/v3.4.3/server/application/application.go)
- [Argo CD v3.4.3 sync controller](https://github.com/argoproj/argo-cd/blob/v3.4.3/controller/sync.go)

## 이번 검증에서 적용할 소유권

| 관리 대상 | 선언/설치 주체 | 런타임 제어 주체 |
| --- | --- | --- |
| Argo CD·KEDA 등 기존 Add-on 설치 | 기존 Terraform/Helm 유지 | 각 Add-on controller |
| Application 정의 | 환경별 Root | Argo CD application-controller |
| Worker 이미지·환경변수·ScaledObject 정책 | Git/Argo CD | Deployment 및 KEDA controller |
| 오토스케일 대상 Deployment의 replicas | 최초 생성은 Git 값, 이후 sync는 live 수량 존중 | KEDA/HPA |
| NodePool·EC2NodeClass 정책 | 기존 platform Application의 Git 선언 | Karpenter |
| NodeClaim·Node 및 실제 EC2 공급 | Karpenter의 런타임 상태 | Karpenter; AWS 동작은 이번 kind 검증 밖 |

Terraform이 설치하는 Add-on을 Root가 중복 설치하도록 바꾸지 않았다. Root의 Wave는 Terraform 작업 완료를 감시하는 기능이 아니므로 Add-on 설치·업그레이드의 준비 상태 확인은 별도 운영 절차다.

이번 후속 정책 변경을 기존 EKS에 반영할 때는 Child spec diff가 발생한다. 최초 편입용 bootstrap preflight는 이를 정상적으로 거부한다. 편입 단계와 정책 변경을 나누거나, 승인된 Child 정책을 먼저 반영하고 일치 상태를 확인한 후 Root에 편입한다. drift 검사를 생략하는 옵션을 추가하지 않는다.


## 저장소에 반영한 정책

- dev/prod ai-worker와 ai-service Application에 RespectIgnoreDifferences=true를 추가했다.
- Deployment 전체에 적용하던 replica 제외를 실제 ScaledObject/HPA target의 name/namespace로 제한했다. 정적 Deployment의 replica drift까지 숨기지 않는다.
- Child 8개와 Root 2개에 manifest-generate-paths를 지정했다. 앱 overlay와 공통 base를 모두 포함하며, platform은 전체 k8s/platform을 감시해 하위 공통 리소스 의존성을 보존한다.
- 대상 저장소가 type: git으로 등록됐는지, shallow clone 설정이 아닌지 확인해야 캐시 최적화 효과를 기대할 수 있다. 기존 repository credential 설정을 변경하거나 중복 repository Secret을 자동 생성하지 않는다.
- 실제 local Kustomize 의존성 그래프와 annotation 경로의 포함 관계, scaler target과 replica ignore의 일치를 CI에서 확인한다.
- API와 Worker 사이에 새 Wave를 추가하지 않았고 Terraform 소유 Add-on을 Argo CD로 옮기지 않았다.

EKS 기동은 이 검증의 완료 조건이 아니다. 이번 결과는 kind에서 확인한 제어 동작이며 저장소 변경은 로컬 작업 트리에 있다.

후속 심화 결과는 [롤백 호환성과 캐시 정합성](deep-design.md), 포트폴리오 본문은 [초안](portfolio-draft.md)에 정리했다.
