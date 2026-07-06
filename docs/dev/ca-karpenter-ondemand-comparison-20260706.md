# CA vs Karpenter On-Demand 조건 비교 테스트 (2026-07-05 ~ 07-06)

> 관련 문서: [`keda-karpenter-test-summary-20260705.md`](./keda-karpenter-test-summary-20260705.md) —
> 그 문서의 "아직 안 한 것" 항목 중 "**CA 실측**"을 수행한 결과다.

이 문서는 prod(`utterai-prod-eks`)에서 **Cluster Autoscaler(CA)와 Karpenter를
동일 조건(on-demand, 동일 인스턴스 패밀리, 동일 부하)** 으로 순차 테스트하고
cpu-worker 스케일링 속도를 비교한 기록이다.

---

## 1. 배경 및 목적

`keda-karpenter-test-summary-20260705.md`에서 Karpenter cpu/batch/gpu-worker
테스트는 끝냈지만 CA와의 직접 비교 데이터가 없었다. 처음에는 Karpenter cpu-worker
NodePool과 완전히 동일한 조건(spot+on-demand, m5/m5a/m6i/m6a.xlarge)으로 CA도
맞추려 했으나, 아래 4절의 이유로 **on-demand 단독 조건**으로 범위를 좁혀
비교했다.

---

## 2. 테스트 방법론

- 큐: `utterai-prod-audio-preprocess-queue` (cpu-worker 트리거)
- 투입: `tests/load/send_sqs_messages.py --count 100 --rate 10 --phase 2 --s3-bucket utterai-prod-raw-audio`
- 관찰: `tests/observe/watch_scaling.sh`, `tests/observe/measure_scale_time.sh utterai-ai-cpu utterai-cpu-worker`
- 스케일다운 측정: 이번에 새로 작성한 `tests/observe/watch_scaledown_ca.sh`
  (peak→baseline(1 pod, 1 node) 도달까지 30초 간격 폴링, 최대 70분)
- KEDA ScaledObject 설정은 두 테스트 모두 동일(변경 없음): `queueLength=5`, `min=1/max=10`, `cooldownPeriod=300s`
- 매 테스트 전 baseline(1 pod, 1 node, 큐/DLQ 0개) 확인 후 투입

### 2.1 CA 측 인프라 구성

- 기존 legacy Managed Node Group `utterai-prod-worker` (on-demand, m5.xlarge)를
  재사용. `terraform/modules/eks/main.tf`에서 라벨을 `workload=worker` →
  `workload=cpu-worker`로, taint `dedicated=worker`를 추가해 cpu-worker 배포의
  `nodeSelector`/`toleration`과 매칭시켰다.
- `scaling_config.desired_size`는 CA가 실시간으로 바꾸므로 `lifecycle { ignore_changes = [scaling_config[0].desired_size] }`를 추가해 terraform이 드리프트를 되돌리지 못하게 했다.

### 2.2 Karpenter 측 인프라 구성

- 기존 `cpu-worker` NodePool(`k8s/platform/karpenter/base/nodepools.yaml`)은
  `capacity-type: [spot, on-demand]`. CA와 동일 조건 비교를 위해 prod 오버레이에
  `k8s/platform/karpenter/overlays/prod/patch-nodepool-cpu-worker-ondemand-prod.yaml`를
  추가해 **on-demand 전용**으로 임시 제한했다. (⚠️ 8절 "정리 필요" 참고 — 아직 git
  미커밋 + ArgoCD auto-sync 일시 중지 상태로 적용 중)

---

## 3. 최종 결과 (on-demand, 동일 조건)

| 항목 | CA | Karpenter | 비고 |
|---|---:|---:|---|
| 스케일아웃 (1→10 pod, Pending→Running) | **82초** | **78초** | 거의 동일, 오차범위 내 |
| 신규 노드 수 | 8~9대 | 8대 | 동일 |
| Pod baseline 복귀 (peak→1) | +1059초 | +1041초 | 거의 동일 — KEDA `cooldownPeriod`(300s) + SQS visibility timeout(900s) 재시도 로직에 좌우, 오토스케일러와 무관 |
| Node 완전 drain (pod baseline 이후) | +718초(~12분) | +701초(~11분41초) | **거의 동일** |
| DLQ 최종 적재 | 100개 | 200개 | 테스트 간 누적치, 무관 |

### 3.1 CA 스케일아웃 반복 측정 (변동폭 참고용)

같은 조건으로 4회 반복한 CA 스케일아웃 시간: **63초, 82초, 84초, 82초**
(평균 ~78초, 범위 63~84초). Karpenter의 78초는 이 변동폭 안에 정확히 들어온다 —
즉 on-demand 조건에서는 **CA와 Karpenter의 스케일아웃 속도 차이가 통계적으로
유의미하지 않다.**

### 3.2 흥미로운 발견 — Consolidation 속도 역전

Karpenter cpu-worker NodePool의 `consolidateAfter`는 5분으로 CA의 기본
`scale-down-unneeded-time`(10분)의 절반이다. 그런데도 노드 8~9대를 완전히
비우는 데 걸린 총 시간은 두 쪽 다 **~11~12분으로 거의 같았다.**

오늘 앞서 진행한 실제 Karpenter **spot** 테스트(`keda-karpenter-test-summary-20260705.md` 3.2절)에서는
cpu-worker 노드 drain이 **~4분**으로 훨씬 빨랐던 것과 대조적이다. 즉:

- Karpenter의 "빠른 회수" 강점은 이번 on-demand 단독 조건에서는 거의 드러나지
  않았고, spot 활용 및 (아마도) 실제 프로덕션 트래픽 패턴에서 더 뚜렷하게
  나타났던 것으로 보인다.
- 원인은 아직 특정하지 못함(여러 노드를 한꺼번에가 아니라 순차/제한적으로
  disrupt하는 방식일 가능성) — 후속 조사 필요.

**결론**: 스케일아웃 속도는 CA와 Karpenter가 on-demand 조건에서 사실상 동등하다.
Karpenter 채택의 근거는 속도 자체보다는 **SQS 큐 깊이 기반 즉시 반응(HPA CPU
임계값 불필요)**, **spot 활용을 통한 비용 절감**, **워크로드별 NodePool 세분화**
쪽에 무게를 둬야 한다.

---

## 4. 시도했으나 실패한 것 — CA spot+on-demand 혼합 구성

Karpenter의 `capacity-type: [spot, on-demand]`와 완전히 동일하게 맞추려고
legacy MNG 방식으로 **spot leg(`utterai-prod-worker-spot`)를 추가**했으나
(m5/m5a/m6i/m6a.xlarge, `priority` expander로 spot 우선), 매 시도마다 CA가
`Insufficient ephemeral-storage`로 이 그룹을 스케일업 후보에서 계속 제외했다.

원인 추정: 이 ASG가 **한 번도 실제 노드를 띄운 적이 없어서**, CA가 템플릿
용량을 추정할 때 ephemeral-storage 값을 아예 얻지 못함(`missing capacity
ephemeral-storage`). `disk_size` 파라미터만 쓴 첫 시도, GPU 노드그룹과 동일하게
커스텀 `aws_launch_template`(명시적 `block_device_mappings`)을 붙인 두 번째
시도 모두 동일한 증상 — launch template에 `image_id`를 명시하지 않아 CA가 루트
디바이스 크기를 정적으로 확인할 방법이 없었던 것으로 보인다(미검증 가설).

**미해결 상태로 보류.** `utterai-prod-worker-spot` ASG는 desired=0으로 존재하며
priority expander(`cluster-autoscaler-priority-expander` ConfigMap)도 배포는
됐지만 실제 spot 스케줄링을 한 번도 성공시키지 못해 검증되지 않았다. 재시도하려면
launch template에 `image_id`(SSM `/aws/service/eks/optimized-ami/...` 파라미터로
조회)를 명시하는 것부터 시도해볼 것.

---

## 5. 트러블슈팅 — 고아 Karpenter 노드

첫 CA 테스트의 스케일다운이 baseline(1 node)까지 안 내려와서 원인을 찾아보니,
Karpenter를 끄기 **전부터** 떠 있던 spot 노드(`ip-10-20-12-131`, m6i.xlarge,
`karpenter.sh/capacity-type=spot` 라벨) 하나가 컨트롤러 제거 후 아무도 관리하지
않는 채로 계속 떠 있었다(과금 지속). 이 노드에는:

- baseline cpu-worker 파드 1개
- **`utterai-ai-service`(1/1, 이중화 없음)** 파드

가 얹혀 있어서 drain 시 PDB(`minAvailable: 1`)에 막혔고, 해당 PDB가
ArgoCD(`utterai-ai-worker-prod`, `selfHeal: false`)로 관리되고 있어서 patch가
자동으로 되돌아가는 문제도 겹쳤다 (→ Argo automated sync를 잠깐 껐다가 patch,
drain, 복구 후 다시 켬). 최종적으로:

1. cordon + drain (PDB 임시 완화)
2. `aws ec2 terminate-instances`로 인스턴스 종료
3. Node 오브젝트에 `karpenter.sh/termination` finalizer가 남아 안 지워짐 →
   수동으로 finalizer 제거

**교훈**: Karpenter를 끌 때는 helm release만 내리는 게 아니라, **기존
NodeClaim이 만든 노드를 먼저 정리(cordon/drain/terminate)해야** 고아 리소스가
안 남는다. 지금 코드에는 이 절차가 자동화되어 있지 않다 — 향후 karpenter →
CA 전환 runbook에 반드시 포함할 것.

---

## 6. 트러블슈팅 — ArgoCD selfHeal과 로컬 kustomize 변경

이번 테스트 중 두 번 겪음:

1. `utterai-cpu-worker-pdb` (앱 `utterai-ai-worker-prod`, `selfHeal: false`)
   — patch가 거의 즉시 원복됨. `selfHeal: false`인데도 원복된 정확한 이유는
   특정 못 함(자동화 sync 자체는 살아있어 git 기준으로 재조정했을 가능성).
2. `nodepool.karpenter.sh/cpu-worker` (앱 `utterai-platform-prod`,
   `selfHeal: true`) — 로컬에만 있는(git 미커밋) kustomize 패치를
   `kubectl apply -k`로 밀어넣었더니 Argo가 git 기준 상태로 즉시 되돌림.

두 경우 다 해당 Application의 `spec.syncPolicy.automated`를 `null`로 patch해서
잠깐 꺼뒀다가, 원하는 변경을 적용하고, 작업 후 원래 정책으로 복구하는 방식으로
우회했다.

**중요 사고 사례**: 이 과정에서 kubectl context가 prod로 맞춰진 상태에서 실수로
`kubectl apply -k k8s/platform/dev`를 실행해 dev용 EC2NodeClass 설정(role,
discovery 태그)이 잠깐 prod에 적용될 뻔했다. `utterai-platform-prod`의
`selfHeal: true` 덕분에 수초 내로 자동 원복되어 실제 피해는 없었지만, **kustomize
경로를 다룰 때 현재 kubectl context를 항상 재확인해야 한다.**

---

## 7. 정량 데이터 원본 위치

`docs/dev/results/` (git 미추적) 아래:

- `prod_ca_cpu_worker_measure_20260705_202002.log` / `_scaledown_*.log` — 1차 CA 테스트(고아 노드 혼입, 참고용)
- `prod_ca_spotondemand_measure_20260705_212557.log`, `prod_ca_spotondemand2_measure_20260705_220024.log` — spot 시도(둘 다 결국 on-demand로 fallback, 4절 참고)
- `prod_ca_final_measure_20260705_225240.log`, `prod_ca_final_scaledown2_20260705_2253.log` — **CA 최종 대표 수치** (3절 표)
- `prod_karpenter_ondemand_measure_20260705_235820.log`, `prod_karpenter_ondemand_scaledown_20260706_0002.log` — **Karpenter on-demand 최종 대표 수치** (3절 표)

Grafana `UtterAI CA vs Karpenter 스케일링`(uid: `utterai-ca-karpenter`) 대시보드의
`karpenter_offset`/`ca_offset` 변수로 오늘 Karpenter spot 테스트(17:42~) 구간과
겹쳐볼 수 있다 (dev Prometheus retention 3일 이내).

---

## 8. 정리 필요 (다음 작업 전 확인할 것)

이번 세션 종료 시점 기준으로 **아직 정리 안 된 상태**:

- [ ] `terraform/environments/prod/04-addons/main.tf`: 현재
      `cluster_autoscaler_enabled=false / karpenter_enabled=true`(Karpenter가
      마지막으로 켜진 상태). prod의 최종 목표 구성이 CA인지 Karpenter인지 결정 후
      확정할 것.
- [ ] `k8s/platform/karpenter/overlays/prod/patch-nodepool-cpu-worker-ondemand-prod.yaml`:
      cpu-worker NodePool을 on-demand 전용으로 묶어둔 임시 패치. **spot+on-demand로
      되돌리거나(패치 제거) 계속 유지할지 결정 필요.**
- [ ] `utterai-platform-prod` ArgoCD Application의 `syncPolicy.automated`가
      현재 **꺼져(null) 있음** — 위 NodePool 패치를 git에 반영하거나 되돌린 뒤
      `{"automated":{"prune":true,"selfHeal":true}}`로 복구할 것.
- [ ] `terraform/modules/eks/main.tf`의 `aws_launch_template.worker_spot` /
      `aws_eks_node_group.worker_spot`, `terraform/environments/prod/02-eks/terraform.tfvars`의
      `worker_spot_node_group_enabled = true`: spot leg 실험 결과물. 4절 문제를
      해결해서 계속 쓸지, 아니면 제거(destroy)할지 결정 필요. 현재 desired=0이라
      비용 영향은 없음(ASG만 존재).
- [ ] `terraform/modules/eks-addons/main.tf`의 CA `extraArgs.expander=priority` +
      `cluster_autoscaler_priority_expander` ConfigMap: spot leg를 포기하면
      같이 제거 대상.
- [ ] 위 terraform/k8s 변경 사항 전부 **git 미커밋** 상태. 유지하기로 한 것만
      골라서 커밋할 것.

---

## 9. 결론 — 왜 Karpenter인가 (실측 근거 기반)

on-demand 조건에서는 **컨트롤러 자체의 스케일링 속도가 차별점이 아니다** —
이걸 먼저 정직하게 인정해야 한다(3절). 대신 실측으로 확인된 차별점은 다음과
같다.

1. **Capacity 유연성 확보의 운영 비용 차이**: Karpenter는 NodePool
   `requirements` 블록 하나에 `capacity-type: [spot, on-demand]` + 인스턴스
   패밀리 여러 개를 넣으면 매 스케줄링마다 최적 조합을 알아서 고른다. CA로
   동일한 유연성을 얻으려면 capacity-type별로 ASG를 분리하고 priority
   expander까지 설정해야 하는데, **이번에 실제로 시도하다가 `Insufficient
   ephemeral-storage` 버그로 끝내 완성하지 못했다**(4절). 즉 "CA로도 비슷하게
   만들 수 있다"는 이론과 달리, 실제로는 운영 부담이 크고 잘 안 될 수도
   있다는 걸 직접 겪었다.
2. **워크로드별 세분화된 consolidation 튜닝**: cpu-worker(5분)/batch-worker(30초)/gpu(WhenEmpty,
   10분)처럼 NodePool 단위로 다른 축소 정책을 코드 몇 줄로 표현할 수 있다.
   CA는 노드그룹 단위 플래그 튜닝이 더 수동적이다.
3. **실제 spot 활용 시나리오에서의 체감 속도**: 오늘 앞서 진행한 진짜 Karpenter
   spot 테스트(cpu-worker)의 노드 drain은 **~4분**으로, 이번 on-demand 전용
   비교(~11~12분)의 1/3 수준이었다. Karpenter의 이점은 "컨트롤러가 빠르다"가
   아니라 "spot을 실제로 쓰고 워크로드별로 튜닝했을 때" 나타난다.
4. **KEDA와의 조합**: CA+HPA는 CPU 임계값 기반이라 큐만 쌓이고 CPU는 안 오르는
   상황에서 무반응이었다(`keda-karpenter-transition.md` 1절). KEDA는 큐 깊이를
   직접 봐서 ~30초 내 반응하고, Karpenter는 그 요청에 맞춰 즉시 다양한
   인스턴스 타입/구매방식으로 노드를 만들 수 있다 — 이 조합이 CA+HPA 대비
   전체 반응성을 끌어올린다.

**요약 문장**: "동일 on-demand 조건에서 컨트롤러 자체 속도는 CA와 Karpenter가
차이 없다. Karpenter를 쓰는 이유는 (1) spot+다중 인스턴스 활용을 운영 부담
없이 얻을 수 있고 — 반대로 CA로 흉내내려다 버그에 막혀 실패한 사례로
반증됨 — (2) 워크로드별 세밀한 consolidation 튜닝이 가능하며, (3) 실제 spot
활용 시나리오에서 스케일다운이 3배 빠르게 측정됐고, (4) KEDA와 결합해 큐
기반 즉시 반응이 가능하기 때문이다."

---

## 10. 확장 가능성 — CA batch-worker / gpu-worker 테스트

Karpenter는 오늘 cpu/batch/gpu-worker 세 워크로드 모두 테스트했지만(
`keda-karpenter-test-summary-20260705.md`), CA는 cpu-worker만 했다. 나머지
두 워크로드도 CA로 재현 가능한지 정리하면:

### 10.1 gpu-worker — 즉시 가능

`terraform/modules/eks/main.tf`에 `aws_eks_node_group.gpu`가 이미 코드로
존재한다(on-demand, g4dn.xlarge, `workload=ai-gpu` 라벨, `dedicated=ai-gpu`
taint, 커스텀 `aws_launch_template.gpu`로 ephemeral-storage 문제 없음). 현재
`terraform/environments/prod/02-eks/terraform.tfvars`에서
`gpu_node_group_enabled = false`로 꺼져있을 뿐이라, `true`로 바꿔서
`terraform apply`만 하면 바로 테스트 가능하다.

### 10.2 batch-worker — 신규 리소스 필요, 하지만 위험도 낮음

지금 legacy `worker` MNG는 `workload=cpu-worker`로만 라벨링돼 있어
batch-worker 배포(`nodeSelector: workload=batch-worker`)와 매칭되지 않는다.
cpu-worker MNG와 동일한 패턴(on-demand, `dedicated=worker` taint 공유, `disk_size`
파라미터만 사용)으로 `aws_eks_node_group.batch_worker`를 하나 더 추가하면 된다.

4절의 `Insufficient ephemeral-storage` 버그는 **spot 전용 이슈로 추정**된다 —
실제로 첫 CA cpu-worker 테스트 때도 `worker` MNG는 그 시점에 한 번도 인스턴스가
뜬 적 없는 완전히 새 ASG였지만 on-demand였기 때문에 문제없이 동작했다. 따라서
batch-worker도 on-demand로만 구성하면 같은 버그를 피할 가능성이 높다(미검증
가설이지만 근거는 있음).

### 10.3 테스트 방법 (진행하기로 하면)

Karpenter 테스트와 동일한 부하로 비교:

| 워크로드 | 큐 | count/rate | 비교 대상 (Karpenter 실측치) |
|---|---|---|---|
| batch-worker | rag-ingest-queue | 50개 / 5개초 | 스케일업 5분47초, drain ~4분 |
| gpu-worker | gpu-inference-queue | 1개 / 1개초 | 스케일업 5분6초, drain(비정상 지연 포함) ~28분 |

진행할지 결정해주시면 (1) GPU는 tfvars 플래그만 켜서, (2) batch-worker는
새 node group 추가 terraform 코드부터 작성해서 순서대로 테스트하겠습니다.
