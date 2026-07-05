# KEDA + Karpenter 스케일링 테스트 종합 정리 (2026-07-05)

이 문서는 2026-07-05 하루 동안 prod(`utterai-prod-eks`)에서 수행한 KEDA+Karpenter
스케일링 테스트 전체(cpu-worker 4차 재현 + batch/gpu-worker 신규 테스트)를 한
문서로 정리한 것이다. 개별 실측 로그는 `docs/dev/results/`(git 미추적)에 있고,
이 문서는 방법론·대시보드 보는 법·정량 결과를 재구성한 요약본이다.

## 1. 테스트 방법론

### 1.1 공통 스크립트

```bash
python tests/load/send_sqs_messages.py \
  --queue-url "<SQS 큐 URL>" \
  --count <N> --rate <R> --phase 2 \
  --s3-bucket <버킷명>
```

- `--phase 2`: KEDA가 큐 깊이를 직접 폴링해 ~30초 내 반응하는 시나리오
- `--s3-key`를 지정하지 않으면 **존재하지 않는 랜덤 키**를 메시지에 담는다
  (스크립트 docstring에 명시된 의도된 동작). 워커가 해당 파일을 다운로드하려다
  실패 → visibility timeout(900s) 이후 재시도 소진 → **DLQ 전량 적재**가
  정상적으로 반복 관찰되는 이유이며, 서비스 결함이 아니다.

### 1.2 워크로드별 트리거 설정

| 워크로드 | 큐 | KEDA queueLength | min/max replica | Karpenter NodePool | 인스턴스 패밀리 |
|---|---|---:|---|---|---|
| cpu-worker | audio-preprocess-queue | 5 | 1 / 10 | cpu-worker | m5/m5a/m6i/m6a.xlarge, spot |
| batch-worker | rag-ingest-queue | 3 | 0 / 5 | batch-worker | c5/c6i/c6a/m5/m6i.large·xlarge, spot |
| gpu-worker | gpu-inference-queue | 1 | 0 / 4 | gpu | g4dn/g5.xlarge·2xlarge, spot |

cpu-worker는 baseline이 1이라 "스케일업"이고, batch/gpu-worker는 baseline이 0이라
**"0에서부터 콜드 스타트"**라는 점이 질적으로 다르다.

## 2. Grafana 대시보드 보는 법

두 개의 대시보드가 관련되어 있다.

### 2.1 `UtterAI CA vs Karpenter 스케일링` (uid: `utterai-ca-karpenter`)

오늘 대부분의 정밀 지표를 여기에 추가했다.

| 패널 | 보는 것 | 비고 |
|---|---|---|
| Pending Pods / Unschedulable Pods | 스케줄 대기 중인 파드 수 | 전체 네임스페이스 합산 |
| Ready Nodes | 클러스터 전체 Ready 노드 수 | 워크로드 구분 없음 |
| Node CPU/Memory Usage, Cluster Requested Resource Ratio | 인프라 레벨 리소스 | 스케일링과 별개로 상시 참고용 |
| Pod Phase, Node Inventory | 파드/노드 목록 타임라인 | 노드 교체 시각 확인용 |
| Cluster Autoscaler Activity, CA Errors 15m | CA 관련 | **CA를 아직 켜지 않아 현재 데이터 없음** |
| Karpenter Activity (`karpenter_nodes_created/terminated_total`) | Karpenter 노드 생성/종료 총량 | 정상 동작 확인(오늘 cpu-worker 9회 등 반영됨) |
| **노드 수 스케일링 비교 (Karpenter vs CA)** | cpu-worker NodePool 노드 수 vs CA legacy MNG 노드 수, `karpenter_offset`/`ca_offset` 변수로 서로 다른 시각에 측정한 두 테스트를 같은 시간대에 겹쳐보기 | CA 데이터는 아직 없음(터폼 준비만 됨) |
| **Karpenter 스케일다운(Consolidation) 소요시간**(신규) | NodeClaim 종료 소요시간 + 노드 lifetime, `nodepool`별 분리 | 오늘부터 데이터 생성 |
| **Karpenter Voluntary Disruption 결정 추이**(신규) | consolidation 판단이 언제·왜(reason) 내려졌는지 | `$__range` 사용 — 대시보드 상단 시간범위를 조회 구간으로 맞추면 됨 |
| **Pod 프로비저닝 Latency (Karpenter, p95)**(신규) | Pod startup/bound p95 | **워크로드(nodepool)별 분리 불가** — cpu/batch/gpu 지연시간이 합산된 값 |
| **Karpenter NodePool 한도 사용률(%)**(신규) | NodePool `limits.cpu/memory` 대비 실사용률, 80%/95% 임계 경고 색상 | nodepool별로 정확히 분리됨(확인 완료) |

`karpenter_offset`/`ca_offset` 변수는 기본값 `0`이며, 단위 붙은 `0s`/`0m` 등을
넣으면 Prometheus가 파싱 에러(`bad_data`)를 낸다 — 반드시 단위 없는 정수(예: `0`,
`3600`) 또는 실제 duration(`3h20m`)만 사용.

### 2.2 `UtterAI SQS / KEDA / Karpenter` (uid: `utterai-sqs-keda-karpenter`)

| 패널 | 상태 |
|---|---|
| SQS Visible Messages / In-flight·Oldest Message | **데이터 없음** — `aws_sqs_*` 메트릭을 Prometheus로 넣어주는 CloudWatch exporter가 없음 |
| KEDA HPA Current vs Desired, Worker Deployment Replicas, Worker Pending Pod | 정상 동작(cpu/gpu/batch 네임스페이스 전체 커버) |
| Karpenter Node Activity, Karpenter Metric Series | 정상 동작 |
| Ready Node 수 | 정상 동작 |
| Worker SQS Receive/Publish (`utterai_ai_sqs_*`) | 앱 자체 OTel 메트릭 — 별도 확인 필요(오늘 검증 안 함) |
| KEDA Operator 로그 / Karpenter 로그 | Loki 기반 로그 패널, 정상 |

**주의**: 이 대시보드는 KEDA queueLength 계산 근거가 되는 실제 SQS 백로그를 못 보여준다.
큐 깊이 확인은 `aws sqs get-queue-attributes` CLI로 대체해야 한다.

## 3. 정량 결과

### 3.1 스케일업 (baseline → peak)

| 워크로드 | replica | 실행시각(KST) | 소요시간 | 신규 노드 | 재시작 |
|---|---|---|---:|---|---:|
| cpu-worker | 1→10 | 17:42:37 투입 → 17:46:09 | **3분 32초** | 9대 (m5/m6i.xlarge, spot) | 0 |
| batch-worker | 0→5 | 18:21:48 투입 → 18:27:35 | **5분 47초** | 4대 (m5.large/xlarge, c6i.large, spot) | 0 |
| gpu-worker | 0→4 | 18:31:03 투입 → 18:36:09 | **5분 6초** | 4대 (g4dn.xlarge, spot) | 0 |

batch/gpu가 cpu보다 느린 것은 1차적으로 **0→N 콜드스타트**(NodeClaim을 아예 없는
상태에서 생성) 특성 때문이며, batch-worker는 테스트 도중 겹친 Spot 중단(3.3절)의
영향도 일부 있을 수 있다(분리 측정 안 됨).

### 3.2 스케일다운 (peak → baseline)

| 워크로드 | Pod 0(또는 1)까지 | 노드 소진까지 | NodeClaim 평균 종료 소요시간 |
|---|---|---|---:|
| cpu-worker | 10→1, 18:04:19 | 10대→1대, 18:09:56~18:13:42(**약 4분**) | 58.6초 |
| batch-worker | 5→0, ~18:41:47 활성 종료 기준 | 4대→0대, ~18:46~18:50(**약 4분**) | 50.4초 |
| gpu-worker | 4→0, ~18:36:23 활성 종료 기준 | 4대→0대, **cooldown 종료(18:46:23) 후 실제 disrupt 시작까지 약 10분 지연**, 이후 종료 완료까지 총 **약 28분**(19:04 확인) | **308.5초(약 5분 8초)** — 다른 풀 대비 6배 이상 |

### 3.3 예상 밖 관찰 사항

1. **Spot 중단 자연 발생(batch-worker)** — 테스트 도중 `m5.large` 노드 1대가
   실제 AWS Spot 중단을 받음(`karpenter_nodeclaims_disrupted_total{reason="spot_interrupted"}=1`).
   Deployment는 5/5 유지, 재시작 0회로 무중단 흡수했으나, AWS의 2분 통지 윈도우
   안에 그레이스풀 드레인은 **완료하지 못함**(`FailedDraining` 2회) — 상세는
   `docs/dev/results/prod_keda_karpenter_batch_worker_20260705.md`.
2. **GPU NodePool consolidation 지연** — 설정된 `consolidateAfter: 30s`와 무관하게
   실제 disruption 시작까지 약 10분, 노드 4대 전체 소진까지 약 28분 소요.
   NodeClaim 종료 소요시간 자체도 308.5초로 cpu/batch(50~59초)의 6배 이상.
   batch-worker 축소 처리(동시간대 진행)와 경합했을 가능성이 있으나 원인 특정은
   안 됨 — **후속 조사 필요**.
3. **DLQ 100% 적재는 설계된 정상 동작** — cpu(100개)·batch(20개, 사전 DLQ
   baseline 미확인이라 최종 28개엔 이전 잔량 포함 가능)·gpu(5개) 전량 DLQ 적재.
   `--s3-key` 미지정 시 존재하지 않는 파일을 참조하는 스크립트 설계 때문이며
   서비스 버그가 아님.

## 4. 아직 안 한 것

- **CA(cluster-autoscaler) 실측** — terraform 코드는 준비됨(`02-eks` worker MNG +
  `04-addons` cluster_autoscaler_enabled), apply만 하면 됨. 대시보드의 "CA vs
  Karpenter" 비교 패널이 의미를 가지려면 이게 선행되어야 함.
- **동시다발 멀티큐 버스트** — cpu/batch/gpu를 동시에 부하 줘서 Karpenter
  컨트롤러 하나가 여러 NodePool을 동시 처리할 때 병목 여부 확인.
- **지속/반복 부하(flapping 여부)** — 지금까진 전부 단발성 버스트만 테스트.
- **GPU consolidation 지연 원인 규명** — 3.3절 2번 항목.
- **DLQ 실제 처리 성공 케이스 검증** — 실제 존재하는 S3 파일로 1건이라도
  end-to-end 성공 처리를 확인해본 적은 없음(전부 의도적 실패 케이스만 테스트).
