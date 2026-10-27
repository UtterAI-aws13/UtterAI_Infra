# 검증 기록

검증일: 2026-10-27. 환경: macOS, Python 3.12.2, boto3/botocore 1.43.18, pytest 8.4.2. 정확한 의존성은 lock 파일에 기록했습니다.

## 결과

| 검증 | 관찰한 사실 | 증거 |
|---|---|---|
| 테스트 전체 | 58개 통과, 실패 0 | [tests.xml](../evidence/tests.xml) |
| 실제 loopback HTTP 수신 → 정규화 → 모의 큐 → SQLite CAS → 모의 모델 → 모의 Slack | 고유 이벤트 100개·HTTP 전달 200건·큐 적재 200건·모델 호출 100회·답글 100개·DLQ 0 | [http-burst.json](../evidence/http-burst.json) |
| HTTP ACK 측정 | p50 0.274ms, p95 0.403ms, 최대 5.150ms | 위 JSON. 로컬 실측으로 AWS ACK와 다름 |
| 같은 이벤트의 경쟁 | 16개 스레드로 100회 handle 호출, 모델·답글 각각 1회 | `test_same_event_concurrent_fanout` |
| Slack 원격 수신 후 timeout | 모의 원격 답글 1개, AMBIGUOUS 저장, 재전달 시 추가 답글·모델 호출 없음 | `test_slack_accepted_but_timeout_quarantines` |
| Slack 성공 후 SENT 저장 실패 | POSTING이 남고, lease 만료 후 AMBIGUOUS. 자동 재전송 없음 | `test_crash_after_post_before_checkpoint_is_quarantined` |
| 모델 성공 후 429 | Retry-After=42 반영, 결과 재사용, 모델 호출 1회 | `test_429_preserves_result_and_honors_retry_after` |
| 모델 반복 오류 | 8회 전달 시도에도 모델/파이프라인 실행은 최대 2회 | `test_model_failure_has_hard_attempt_cap` |
| heartbeat·fencing | lease/visibility 연장, 만료된 owner의 변경 거부, heartbeat 장애 시 새 전송 중단 | `test_heartbeat_*`, `test_fencing_stops_old_owner` |
| 지속 상태 | 새 SQLite 연결로 재시작해도 완료된 작업의 모델·답글 재실행 없음 | `test_sqlite_persistence_across_restart` |
| AWS SDK 계약 | SQS receive/delete/visibility, DynamoDB typed attribute와 CAS, Bedrock Converse 요청/응답을 botocore Stubber로 확인 | `tests/test_adapters.py`. 실제 AWS 호출 아님 |
| 데이터·권한 경계 | 서명 변조·오래된/미래 요청·비허용 사용자·SSRF URL 차단, slash 비공개, 모델의 멘션 구문 이스케이프 | ingress·adapter 테스트 |
| IaC | JSON 구문·Ref/GetAtt 존재·paid runtime disabled·role 범위·queue 암호화/retention 설정 검사 | `tests/test_infra.py`. AWS 유효성 검사/배포는 아님 |
| Lambda 패키지 | 고정 SDK 포함 2,163개 파일 ZIP 생성, 격리 모드에서 핸들러와 SDK import 성공 | 아래 재현 명령 및 [package.json](../evidence/package.json) |

테스트명과 원본은 `tests/`에 있습니다. JUnit에 XML로 각 케이스의 이름·시간·성공 여부를 보존했습니다. 테스트가 통과했다고 실제 모델 출력의 품질·비용·AWS 권한·서비스 가용성이 검증된 것은 아닙니다.

## 재현 명령

```bash
python -m pytest -q --junitxml=evidence/tests-local.xml
python -m slack_worker.demo --output evidence/http-burst-local.json
python -m pytest -q tests/test_worker.py -k 'timeout or checkpoint or 429 or concurrent'
python tools/package_lambda.py --output dist/lambda.zip
```

현재 컴퓨터에서 HTTP 포트 생성이 기본 실행 환경에 차단돼 있어, 로컬 포트 사용을 허용한 환경에서 HTTP 포함 검증을 실행했습니다. 외부 AWS·Slack에는 연결하지 않았습니다.

## 실험에서 수정한 문제

처음에는 채널 전송 슬롯을 얻지 못한 작업을 SQS에 즉시 반환했습니다. 동일 채널로 100개 요청이 쌓이면 정상 작업도 수신 횟수를 반복 소모해 DLQ로 이동하는 것을 로컬 큐 모의 실험에서 관찰했습니다.

현재는 제한된 동시성 슬롯 안에서 짧게 기다리고 heartbeat를 유지합니다. 수정 후 같은 입력에서 답글 100개·DLQ 0을 확인했습니다. 데모의 처리 시계는 가상 시계이며 198초를 진행했습니다. 전체 시험이 실제 198초의 모델 처리나 처리량을 측정한 것은 아닙니다. 대기열 배출은 단일 소비자 순차 처리이며 실제 다중 consumer 공정성은 검증해야 합니다.

## 해석 제한과 다음 검증

- 200건은 localhost에서 순서대로 HTTP 제출한 뒤 처리했습니다. 100개 동시 HTTP 접속·실제 SQS 성능을 시험하지 않았습니다.
- SQS 중복·visibility·DLQ는 시뮬레이터에서 필요한 동작만 재현했습니다. 서비스의 모든 race·장애·retention·long polling 특성을 재현하지 않습니다.
- DynamoDB의 조건부 요청 형식은 SDK로 검증했고 경쟁 제어는 SQLite CAS로 검증했습니다. 실제 DynamoDB의 경쟁·throttling·네트워크 분할을 검증하지 않았습니다.
- fake model 출력은 결정적 문자열입니다. Bedrock의 실제 응답·tokenizer·모델 품질·단가·리전 지원은 미검증입니다.
- POSTING 후 실제 전송 전에 죽으면 답글이 0개일 수 있습니다. 중복 자동 전송을 피하는 정책의 대가입니다.
- 실제 Slack 앱 설치·scope·콜백 URL·response_url 만료·채널 rate limit·권한은 별도 sandbox 시험이 필요합니다.
- Dockerfile·CI·CloudFormation은 준비했으나 컨테이너 빌드·GitHub CI 실행·AWS 배포는 수행하지 않았습니다.
- 운영 수준의 알림 수신자, 중앙 예산 hard cap, 다중 replica 전역 모델 rate limit, 세션 순서 보장, vector RAG, UtterAI agent 연계는 완료 범위가 아닙니다.
