# Kubecost / FinOps 데이터 실사 (2026-07-05)

GitOps(ArgoCD) 점검 도중 "Kubecost에 지금 어떤 데이터가 쌓여있고, 발표에 뭘 근거로
써야 하는가"를 확인하기 위해 Kubecost API·CUR/Athena·AWS 공식 가격 데이터를 직접
대조한 결과를 정리한다.

## 1. 배경

- 오늘(2026-07-05) `finops-query` Lambda에 CUR 2.0 + Athena 기반 `get_spot_savings`를
  새로 배포했다(상세는 `docs/prod/architecture.md` §9.1.6).
- 발표/문서에 FinOps 숫자가 당장 필요한데, CUR 데이터는 아직 안 들어온 상태라
  "지금 뭘 근거로 써야 하는가"를 확인할 필요가 있었음.
- 확인 과정에서 **Kubecost 자체의 데이터 신뢰성 문제**를 하나 발견해서 같이 기록한다.

## 2. GitOps(ArgoCD) 점검 요약 (참고용, 조치는 보류)

- ArgoCD Application 4개(`utterai-ai-service-prod`, `utterai-ai-worker-prod`,
  `utterai-backend-prod`, `utterai-platform-prod`) 전부 `Synced/Healthy`.
- 자동화 수준이 앱마다 다름: `platform-prod`만 `prune+selfHeal` 전체 자동화,
  `ai-worker-prod`는 automated이지만 prune/selfHeal 둘 다 off, `ai-service-prod`·
  `backend-prod`는 automated 설정 자체가 없음(수동 sync 필요). 의도된 정책 차이인지
  추가 확인 필요.
- ArgoCD·Kubecost 컴포넌트가 전부 `platform` NodePool에서 도는데 PDB가 없어서,
  Prometheus/Grafana(PR #418/#419로 이미 `system` 노드+PDB 고정) 때와 같은 방식으로
  노드 교체마다 재기동됨을 실측 확인(dex-server/redis/server, kubecost 3파드 전부
  27~33분 전 재기동 이력).
- **다만 확인 결과 이건 데이터 유실 문제가 아님**:
  - Kubecost `cost-model`은 자체 Prometheus가 없고 이미 보호된 공유
    `utterai-monitoring-prometheus`(`http://utterai-monitoring-prometheus.monitoring.svc.cluster.local:9090`)를
    그대로 쿼리한다 → 원본 메트릭은 안전.
  - `kubecost-aggregator`(StatefulSet)의 로컬 집계 DB는 PVC(`aggregator-db-storage`,
    20Gi, gp2)에 저장되고, 파드 재스케줄 시 같은 PVC가 재부착됨(볼륨 ID 동일 확인) →
    데이터 유실 없음.
  - 실제 사고는 "데이터 유실"이 아니라, 당시 신규 도입된 `kubecost-aggregator`가
    cpu/batch/gpu-worker 부하테스트로 클러스터가 꽉 찬 시점과 겹쳐 **스케줄링이
    약 2분 지연**된 것(`FailedScheduling: Insufficient memory / Too many pods /
    volume node affinity conflict`).
- **결론**: PDB 미비는 사실이지만 심각도는 낮음(UI/재기동 다운타임 몇 분 수준). 발표
  시급도가 아니라 **조치는 보류**하기로 함(사용자 결정).

## 3. Kubecost 실측 데이터

### 3.1 네임스페이스별 비용 (`/model/allocation?window=7d&aggregate=namespace`)

| 네임스페이스 | 7일 totalCost($) |
|---|---:|
| `__idle__`(유휴 용량) | 54.351 |
| utterai-ai-cpu | 12.513 |
| kube-system | 5.833 |
| utterai-ai-service | 3.878 |
| utterai-observability | 3.410 |
| monitoring | 2.709 |
| utterai-ai-gpu | 2.356 |
| utterai-prod-api | 1.550 |
| karpenter | 0.659 |
| keda | 0.585 |
| utterai-batch | 0.336 |
| kubecost | 0.359 |

idle 비용($54.35/7일)이 `utterai-ai-cpu`보다도 크다는 점은, KEDA+Karpenter의
"min=0 스케일투제로"가 왜 필요한지를 보여주는 근거로 쓸 수 있다(CA+HPA처럼 항상
최소 1대 이상 떠있는 구조였다면 이 idle 비용이 구조적으로 더 컸을 것).

### 3.2 노드 asset 예시 (`/model/assets?filterTypes=Node`)

```json
{
  "properties": { "name": "ip-10-20-12-131...", "providerID": "i-07d5fb57235fe1436" },
  "nodeType": "m6i.xlarge",
  "preemptible": 1,
  "discount": 0,
  "cpuCost": 0.155872,
  "ramCost": 0.080125,
  "totalCost": 0.235997
}
```

`preemptible: 1`(=spot)인데 `discount: 0`이고 `totalCost`가 아래 3.3의 on-demand
공식가와 사실상 동일 — 이게 4절의 핵심 발견으로 이어짐.

## 4. 핵심 발견: Kubecost가 Spot 할인을 반영하지 않고 있음

| 항목 | 값 |
|---|---:|
| Kubecost가 리포트한 m6i.xlarge spot 노드 실비용 | **$0.235997/hr** |
| AWS Pricing API로 조회한 m6i.xlarge on-demand 공식가(ap-northeast-2) | **$0.236/hr** |
| 차이 | 0.001% (오차 범위 내 = 사실상 동일) |

`/model/savings` 엔드포인트도 확인했으나 `abandonedWorkload`/`clusterSizing`/
`nodeGroupSizing`(향후 절감 추천)만 있고 `reservedInstances`는 `"state":"unsupported"` —
"실제 spot 대비 on-demand였다면 얼마"라는 과거 실측 비교 자체가 이 배포에 없다.

**결론**: Kubecost의 절대 비용 수치는 "어디에 얼마를 쓰는지"(귀속) 파악에는 쓸 수
있지만, **"Spot 덕분에 얼마를 아꼈다"는 주장의 근거로는 지금 쓸 수 없다.** 원인은
미조사 상태(Spot Data Feed/Cluster Controller 연동 누락 추정, 8절 후속 과제 참고).

## 5. 실제 Spot 절감률 (AWS 공식 데이터 기준)

CUR/Athena(`get_spot_savings`)를 직접 호출한 결과:

```json
{"status": "data_unavailable", "reason": "No Spot CUR line items are available for this period yet"}
```

CUR export가 아직 도착하지 않아(오늘 막 배포, AWS CUR는 통상 24시간+ 지연) 사용
불가. 대신 **AWS Pricing API(on-demand) + EC2 Spot Price History API(실제 spot가)를
직접 대조**해 아래 수치를 확보했다 — 이번 발표에서 CUR/Athena를 대신할 근거.

| 인스턴스 타입 | On-Demand($/hr) | 실제 Spot가($/hr, 오늘 사용 AZ 기준) | 절감률 |
|---|---:|---:|---:|
| m5.xlarge | 0.236 | 0.0692 (ap-northeast-2c) | **70.7%** |
| m6i.xlarge | 0.236 | 0.0789 (ap-northeast-2c, cpu-worker 실제 AZ) | **66.6%** |
| g4dn.xlarge | 0.647 | 0.2438 (ap-northeast-2a, gpu-worker 테스트 AZ) | **62.3%** |

이 방법론(공개 온디맨드가 대비 실비용 차이)은 CUR/Athena `get_spot_savings`가
자동화하려는 계산과 동일하다 — CUR 데이터가 도착하면 이 수동 계산과 대조해서
Lambda 구현이 맞게 동작하는지 검증할 수 있다.

## 6. 발표 자료 구성 권장안

세 데이터 소스의 역할을 명확히 분리해서 설명한다.

1. **비용 귀속(어디에 얼마 쓰는지) → Kubecost**
   `/model/allocation` 네임스페이스별 표, idle 비용($54.35/7일)을 근거로 사용.
2. **Spot 절감률(실제로 얼마 아꼈는지) → AWS 공식 가격 데이터**
   5절의 62~71% 절감률 표를 근거로 사용. Kubecost 숫자는 이 주장에 쓰지 않는다.
3. **자동화 방향성 → CUR 2.0 + Athena(오늘 배포, 데이터 도착 대기 중)**
   "지금은 수동으로 대조했지만, 앞으로는 Slack 챗봇이 이 계산을 자동으로
   해준다"는 로드맵으로 설명.

핵심 메시지: **"KEDA+Karpenter가 Spot으로 62~71% 절감 + min=0 스케일투제로로
유휴 시간엔 비용 0"** — Kubecost는 이 결과가 어느 워크로드에 반영되는지 보여주는
관측 도구, CUR/Athena는 매번 수동 계산 없이 검증을 자동화하려는 인프라라고
설명하면 앞뒤가 맞는다.

## 7. 재확인 체크리스트 (CUR 데이터 도착 후)

- [ ] `get_spot_savings` 재호출해 `data_unavailable`이 해소됐는지 확인
- [ ] CUR 기반 절감률이 5절의 수동 계산(62~71%)과 방향성이 일치하는지 대조
- [ ] Kubecost `discount` 필드가 여전히 0인지, 아니면 자체적으로 고쳐졌는지 재확인

## 8. 후속 과제

- **Kubecost Spot 가격 미반영 원인 조사** — Spot Data Feed 구독 또는 Kubecost
  Cluster Controller(실시간 spot 가격 수집 컴포넌트) 연동 누락 추정, 미확인.
  급하지 않아 보류.
- **ArgoCD/Kubecost PDB 미비** — 2절 참고, 조치는 보류(사용자 결정).
- **ArgoCD 앱별 automated sync 정책 비일관성** — 의도된 것인지 확인 필요.
