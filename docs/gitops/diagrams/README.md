# UtterAI GitOps 포트폴리오 다이어그램

참고 이미지의 흰 배경, 얇은 회색 그룹 박스, 직각 연결선, 파랑·주황 강조색을 따른 2000×1120 다이어그램입니다. 포트폴리오 본문에는 통합본 `03-gitops-unified-portfolio`를 사용하고, 기존 두 장은 상세 설명에 활용합니다.

| 파일 | 용도 |
| --- | --- |
| `03-gitops-unified-portfolio.png` / `.svg` | 본문 통합본: Root/Child·replica 제어와 롤백 호환성 검증 |
| `01-gitops-control-boundaries.png` / `.svg` | 환경별 Root/Child 구성과 Git·Argo CD / KEDA·HPA의 필드 관리 경계 |
| `02-gitops-rollback-cache-validation.png` / `.svg` | Git revert 이후 메시지 호환성 및 외부 base 변경에 따른 정합성 검증 |

PNG는 포트폴리오 삽입용이며 SVG는 벡터 원본입니다. SVG는 아이콘을 내장하며 외부 파일을 불러오지 않습니다. 한글 글꼴은 Apple SD Gothic Neo, Noto Sans CJK KR, Noto Sans KR 순서로 사용합니다.

## 검증 범위

- kind의 실제 Argo CD·KEDA·HPA와 모의 워크로드로 검증한 결과를 표현합니다.
- 수량 비교는 Deployment `spec.replicas` 기준이며 Ready Pod 수나 EKS 운영 성능을 뜻하지 않습니다.
- 롤백은 구·신 스키마 각 5개로 구성한 단일 모의 작업 집합을 사용했습니다. 호환성 gate는 prototype입니다.
- 캐시 비교는 실제 Live ConfigMap과 같은 Application revision을 새 checkout에서 렌더링한 ConfigMap을 비교합니다. Argo CD 내부 캐시를 직접 추출하는 도구가 아닙니다. 외부 base는 격리된 실험용 구성입니다.
- 자세한 실험 근거는 상위 디렉터리의 `operations-validation.md`, `deep-design.md` 및 `evidence/`를 참고합니다.

## 재생성

Python 3와 librsvg의 `rsvg-convert`, 한글 글꼴이 필요합니다.

```sh
python3 build_diagrams.py
rsvg-convert -w 2000 -h 1120 03-gitops-unified-portfolio.svg -o 03-gitops-unified-portfolio.png
rsvg-convert -w 2000 -h 1120 01-gitops-control-boundaries.svg -o 01-gitops-control-boundaries.png
rsvg-convert -w 2000 -h 1120 02-gitops-rollback-cache-validation.svg -o 02-gitops-rollback-cache-validation.png
```

## 아이콘 출처

- Argo: [CNCF artwork](https://github.com/cncf/artwork/blob/master/projects/argo/icon/color/argo-icon-color.svg)
- KEDA: [CNCF artwork](https://github.com/cncf/artwork/blob/master/projects/keda/icon/color/keda-icon-color.svg)
- Deployment, HPA, Pod: [Kubernetes community icons](https://github.com/kubernetes/community/tree/master/icons/svg/resources/labeled)
- Git, Queue, Application 문서 심벌은 이 다이어그램용으로 직접 그렸습니다.
