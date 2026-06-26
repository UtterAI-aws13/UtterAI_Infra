# ADR: app.utterai.org CloudFront WAF v1 규칙 선택

> 상태: 결정 및 구현 완료 (Implemented) - [edge-cloudfront-waf-acm.md](../../../UtterAI_Docs/adr/network/edge-cloudfront-waf-acm.md) 참고
> 대상: `app.utterai.org` CloudFront distribution
> 범위: Route53 -> CloudFront + WAF + ACM 통합 구조 구현 완료. ALB/API WAF는 CF WAF로 통합 차단.

---

## 1. 한 줄 결론

`app.utterai.org`에는 CloudFront용 AWS WAF Web ACL을 붙이고, v1 규칙은 다음 두 개로 시작한다.

```text
1. AWSManagedRulesCommonRuleSet
2. RateLimitByIP custom rule
```

처음에는 둘 다 `Count` mode로 운영한다. 며칠간 CloudWatch metric과 sampled request를 보고 정상 트래픽 오탐이 적다고 판단되면 `Block` mode로 전환한다.

---

## 2. 사전지식

### 2.1 CloudFront

CloudFront는 사용자가 `app.utterai.org`에 접속할 때 가장 먼저 만나는 AWS edge entrypoint다.

쉽게 말하면 학교 정문 같은 역할이다. 사용자는 바로 S3 버킷이나 서버에 들어가지 않고, 먼저 CloudFront를 통과한다.

```text
사용자 -> app.utterai.org -> CloudFront -> S3 frontend origin
```

현재 목표는 이 정문 앞에 보안 검색대를 세우는 것이다.

### 2.2 WAF

WAF(Web Application Firewall)는 HTTP 요청을 검사하는 필터다.

예를 들어 요청 안에 이런 위험한 패턴이 있는지 본다.

```text
- 이상한 SQL 조각
- script 태그 같은 XSS 시도
- 너무 큰 요청 body
- 같은 IP의 과도한 반복 요청
```

방화벽이라고 해서 모든 해킹을 막는 만능 도구는 아니다. 하지만 인터넷에 공개된 웹 서비스 앞에서 흔한 공격과 과도한 요청을 줄이는 첫 방어선이 된다.

### 2.3 Web ACL

Web ACL은 WAF 규칙 묶음이다.

```text
Web ACL
  rule 1
  rule 2
  rule 3
```

CloudFront에 WAF를 붙인다는 말은 실제로는 CloudFront distribution에 Web ACL 하나를 연결한다는 뜻이다.

### 2.4 Managed Rule과 Custom Rule

Managed Rule은 AWS가 미리 만들어 관리하는 규칙이다. 우리가 SQLi, XSS 패턴을 하나하나 직접 쓰지 않아도 된다.

Custom Rule은 우리가 직접 만드는 규칙이다. 예를 들어 "같은 IP가 5분 동안 2000번 넘게 요청하면 잡는다" 같은 정책은 서비스 상황에 맞춰 직접 정한다.

### 2.5 Count mode와 Block mode

WAF rule은 보통 다음 동작 중 하나를 한다.

| Mode | 의미 | 언제 사용 |
|---|---|---|
| `Count` | 잡히는지만 기록하고 요청은 통과시킴 | 처음 도입, 오탐 확인 |
| `Block` | 잡힌 요청을 차단함 | 튜닝 후 운영 차단 |
| `Allow` | 요청을 허용함 | 예외 허용 |

처음부터 `Block`으로 켜면 정상 사용자의 요청도 실수로 막을 수 있다. 그래서 v1은 `Count`로 시작한다.

---

## 3. 결정 배경

### 3.1 이번 범위

이번 작업은 `app.utterai.org` CloudFront 보호만 다룬다.

```text
사용자
  -> app.utterai.org
  -> CloudFront
  -> WAF
  -> S3 frontend
```

ALB에 WAF를 붙이거나 `api.utterai.org` 직접 진입 경로를 막는 것은 별도 결정이다.

### 3.2 왜 ALB WAF가 아닌가

현재 목표가 프론트엔드 CloudFront 보호이기 때문이다.

ALB WAF는 다음 상황에서 필요하다.

```text
- api.utterai.org가 ALB로 직접 들어오는 구조를 보호해야 할 때
- API 요청 body, auth endpoint, backend abuse를 ALB 앞에서 검사해야 할 때
- CloudFront를 통하지 않는 backend 진입 경로까지 보호해야 할 때
```

하지만 이번 v1은 `app.utterai.org` CloudFront만 보호한다. 그래서 ALB WAF를 같이 붙이면 범위와 비용, 디버깅 지점이 늘어난다.

---

## 4. 선택한 규칙

### 4.1 AWSManagedRulesCommonRuleSet

`AWSManagedRulesCommonRuleSet`은 AWS Managed Rules의 기본 방어 세트다.

왜 필요한가?

```text
- 흔한 웹 공격 패턴을 넓게 잡아준다.
- SQL injection, XSS, bad input 계열을 직접 구현하지 않아도 된다.
- WAF v1의 baseline으로 가장 설명하기 쉽다.
```

고등학생 비유로 보면, 정문 보안요원이 "위험한 물건 목록"을 이미 갖고 있는 것이다. 우리가 위험한 물건 이름을 처음부터 전부 외워서 적지 않아도, AWS가 기본 목록을 관리한다.

`RateLimitByIP`만 쓰면 요청 내용이 수상한지는 보지 못한다. 요청이 적으면 악성 payload도 통과할 수 있다. 그래서 CommonRuleSet이 먼저 필요하다.

### 4.2 RateLimitByIP custom rule

`RateLimitByIP`는 우리가 직접 만드는 custom rate-based rule이다.

예시 정책:

```text
같은 IP가 5분 동안 2000회 넘게 요청하면 Count 또는 Block
```

왜 필요한가?

```text
- 같은 IP의 과도한 반복 요청을 볼 수 있다.
- 단순 flood, scraping, refresh loop, 비정상 자동화 요청을 조기에 발견할 수 있다.
- CommonRuleSet이 "요청 내용"을 본다면, RateLimitByIP는 "요청량"을 본다.
```

둘의 역할은 다르다.

| 규칙 | 보는 것 | 예시 |
|---|---|---|
| CommonRuleSet | 요청 내용이 위험한가 | SQLi, XSS, 이상한 payload |
| RateLimitByIP | 요청량이 비정상적으로 많은가 | 같은 IP의 과도한 반복 요청 |

따라서 v1에서는 둘을 같이 두는 것이 균형이 좋다.

---

## 5. 최소 비용 관점

AWS WAF 비용은 크게 다음 요소로 결정된다.

```text
- Web ACL 개수
- Web ACL에 추가한 rule 또는 managed rule group 개수
- Web ACL이 검사한 요청 수
- Bot Control, Fraud Control, CAPTCHA, Challenge 같은 추가 기능 사용 여부
- WAF logging 저장량
```

최소 비용만 보면 가장 싼 시작점은 다음이다.

```text
Web ACL 1개
AWSManagedRulesCommonRuleSet 1개
```

하지만 운영 관점에서는 rate limit 하나가 있으면 과도한 반복 요청을 볼 수 있다. 그래서 현실적인 v1은 다음을 추천한다.

```text
Web ACL 1개
AWSManagedRulesCommonRuleSet 1개
RateLimitByIP custom rule 1개
```

비용을 크게 올릴 수 있는 기능은 v1에서 제외한다.

```text
- Bot Control
- Fraud Control
- CAPTCHA
- Challenge
- Marketplace managed rule
- 너무 많은 custom rule
- 전체 요청 body를 크게 검사하는 고비용 설정
```

---

## 6. 다른 흔한 규칙들은 왜 있는가

### 6.1 AWSManagedRulesKnownBadInputsRuleSet

잘 알려진 악성 입력 패턴을 잡는 rule group이다.

왜 있는가?

```text
- 이미 널리 알려진 공격 payload를 빠르게 차단하기 위해
- CommonRuleSet보다 bad input 성격에 더 초점을 맞추기 위해
```

왜 v1에서 보류하는가?

```text
- rule group이 하나 늘어나면 비용과 오탐 확인 범위가 늘어난다.
- CommonRuleSet으로 baseline을 먼저 만들고, metric을 본 뒤 추가해도 늦지 않다.
```

### 6.2 AWSManagedRulesAmazonIpReputationList

AWS가 관리하는 평판 낮은 IP 목록 기반 rule group이다.

왜 있는가?

```text
- 알려진 악성 IP, 스캐너, 공격 출처를 빠르게 줄이기 위해
```

왜 v1에서 보류하는가?

```text
- 프론트엔드 정적 사이트 보호 v1에서는 CommonRuleSet + rate limit이 우선이다.
- 실제 공격성 트래픽이 확인되면 추가하는 편이 설명과 튜닝이 쉽다.
```

### 6.3 AWSManagedRulesAnonymousIpList

VPN, proxy, Tor 같은 익명화 네트워크를 탐지하는 rule group이다.

왜 있는가?

```text
- 익명 proxy 기반 abuse를 줄이기 위해
- 로그인, 결제, 관리자 화면 같은 민감한 기능에서 유용할 수 있음
```

왜 v1에서 보류하는가?

```text
- 일반 사용자 중 VPN을 쓰는 정상 사용자도 있다.
- 정적 프론트엔드 전체에 바로 Block하면 오탐 UX 문제가 생길 수 있다.
```

### 6.4 AWSManagedRulesSQLiRuleSet

SQL injection 탐지에 더 초점을 둔 rule group이다.

왜 있는가?

```text
- API, 검색, 폼 입력처럼 DB와 연결된 요청에서 SQLi를 더 강하게 보기 위해
```

왜 v1에서 보류하는가?

```text
- 이번 범위는 app CloudFront의 frontend 보호다.
- API 전용 WAF를 설계할 때 `/api/*` 또는 `api.utterai.org`에 맞춰 다시 검토하는 것이 더 자연스럽다.
```

### 6.5 Bot Control

AWS가 제공하는 bot 탐지용 managed rule group이다.

왜 있는가?

```text
- 크롤러, 자동화 bot, scraping 트래픽을 더 세밀하게 분류하기 위해
```

왜 v1에서 보류하는가?

```text
- 일반 WAF보다 비용과 튜닝 부담이 커질 수 있다.
- 지금은 rate limit으로 기본적인 반복 요청 관찰부터 시작한다.
```

### 6.6 CAPTCHA / Challenge

사용자가 사람인지 확인하거나 브라우저 challenge를 수행하게 하는 기능이다.

왜 있는가?

```text
- 로그인/회원가입 brute force나 bot 요청을 줄이기 위해
```

왜 v1에서 보류하는가?

```text
- 사용자 경험에 직접 영향을 준다.
- 추가 비용이 발생할 수 있다.
- 정적 프론트엔드 전체에 먼저 적용할 기능은 아니다.
```

### 6.7 Geo match rule

국가 기준으로 요청을 허용하거나 차단하는 custom rule이다.

왜 있는가?

```text
- 서비스 대상 국가가 명확할 때 불필요한 해외 트래픽을 줄이기 위해
```

왜 v1에서 보류하는가?

```text
- 실제 사용자 위치 정책이 먼저 확정되어야 한다.
- 해외 출장, VPN, 글로벌 사용자 가능성이 있으면 정상 사용자를 막을 수 있다.
```

---

## 7. Count mode 운영 기준

v1 적용 후 바로 Block으로 바꾸지 않는다.

먼저 확인할 것:

```text
- CommonRuleSet에서 어떤 rule이 자주 match되는가
- 정상 사용자 요청이 match되는가
- RateLimitByIP에 걸리는 IP가 실제 비정상 트래픽인가
- FE asset 요청이 rate limit에 과하게 걸리지 않는가
- 배포 직후 403 증가가 없는가
```

Block 전환 기준:

```text
- 며칠간 Count metric 확인
- 오탐 rule이 있으면 예외 또는 Count 유지
- 위험도가 명확한 rule부터 Block 전환
- rate limit 값은 실제 요청량 기준으로 조정
```

---

## 8. Terraform 구현 방향

CloudFront distribution은 이미 Terraform module로 관리된다. 따라서 WAF도 Terraform으로 관리한다.

```text
terraform/environments/prod/04-addons
  aws_wafv2_web_acl.frontend_edge
  module.cloudfront.web_acl_id = aws_wafv2_web_acl.frontend_edge.arn

terraform/modules/cloudfront
  variable "web_acl_id"
  aws_cloudfront_distribution.frontend.web_acl_id = var.web_acl_id
```

CloudFront용 WAF는 반드시 다음 조건을 지킨다.

```text
provider = aws.us_east_1
scope    = "CLOUDFRONT"
```

---

## 9. 결정

v1에서는 다음을 적용한다.

```text
대상:
  app.utterai.org CloudFront distribution

포함:
  AWSManagedRulesCommonRuleSet
  RateLimitByIP custom rule

초기 action:
  Count

보류:
  ALB WAF
  API 전용 CloudFront WAF
  Bot Control
  Fraud Control
  CAPTCHA / Challenge
  Marketplace managed rule
  Geo block
  SQLi 전용 rule group
  IP reputation rule group
```

이 결정의 목적은 "가장 강한 WAF를 한 번에 붙이기"가 아니다. `app.utterai.org`에 대해 비용과 운영 위험을 낮게 유지하면서, 관찰 가능한 첫 방어선을 만드는 것이다.

---

## 10. 참고 문서

- AWS WAF Pricing: https://aws.amazon.com/waf/pricing/
- AWS Managed Rules rule groups list: https://docs.aws.amazon.com/waf/latest/developerguide/aws-managed-rule-groups-list.html
- AWS WAF rule actions: https://docs.aws.amazon.com/waf/latest/developerguide/waf-rule-action.html
- AWS WAF rule action overrides: https://docs.aws.amazon.com/waf/latest/developerguide/web-acl-rule-group-override-options.html
