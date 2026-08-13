# Rust 수집 실패 개선 계획

## 1. 목적

이 계획은 `docs/RUST_COLLECTION_FAILURE_DIAGNOSIS_2026-08-13.md`에서 확정한 다음 문제를 개선한다.

1. provider 문자열의 `NUL(0x00)` 때문에 PostgreSQL 댓글 저장이 실패하는 문제
2. SearchAPI의 malformed/empty HTTP 2xx 응답을 즉시 영구 실패 처리하는 문제
3. 실패 원인, provider, operation, 오류 코드, 재시도 가능 여부가 Job/API/UI에서 소실되는 문제

최종 목표는 신규 실패를 차단하고, 일시 장애는 제한된 횟수만 자동 재시도하며, 실제 시스템 오류와 외부 리소스·quota 상태를 운영 화면에서 구분할 수 있게 만드는 것이다.

## 2. 변경하지 않는 경계

- 현재 외부 URL과 API 응답의 기존 필드 계약은 유지한다.
- SearchAPI/YouTube provider 선택 및 역할은 이번 작업에서 변경하지 않는다.
- `commentsDisabled`, `videoNotFound`, 비공개·삭제 등 리소스 상태는 시스템 오류로 승격하지 않는다.
- `quotaExceeded`는 기존 `waiting_quota` 경로를 유지하며 일반 오류 재시도 횟수와 섞지 않는다.
- 원문 댓글, API key, Bearer header, request URL, page token, provider 원문 body는 로그와 공개 오류 응답에 남기지 않는다.
- 기존 댓글·영상·분석 결과를 삭제하거나 초기화하지 않는다.
- Python과 Rust collection worker가 같은 Job queue를 동시에 claim하지 않게 한다.
- 실패 Job 복구는 코드 배포 및 검증이 끝난 뒤 별도 승인 가능한 단계로 수행한다.

## 3. 목표 상태

### 3.1 PostgreSQL 안전 문자열 경계

모든 provider 유래 문자열은 DB bind 직전에 공용 sanitizer를 통과한다.

- content 문자열은 `\u0000`만 제거하고 다른 Unicode 내용은 보존한다.
- ID·parent ID·thread ID도 기존 Python `_strip_nul` 동작과 동일하게 처리한다.
- sanitizer 적용 후 필수 ID가 비어 있으면 저장하지 않고 구조화된 payload 오류로 분류한다.
- 댓글뿐 아니라 같은 collector가 저장하는 채널, 영상, transcript 문자열에도 동일한 DB 안전 경계를 적용한다.
- 페이지 저장과 checkpoint 전진의 기존 transaction 원자성은 유지한다.

### 3.2 구조화된 수집 오류

Rust 내부에 provider와 저장 계층이 함께 사용할 단일 오류 표현을 둔다.

```text
CollectionFailure
  code
  provider
  operation
  stage
  retryable
  safe_message
  http_status
```

`safe_message`는 사용자에게 노출 가능한 고정 문구만 사용한다. 내부 로그에는 `sqlx` source chain을 추가로 기록하되 public comment, provider body, credential은 기록하지 않는다.

### 3.3 오류 분류 정책

| 오류 유형 | 상태 전이 | 자동 재시도 | 대표 코드 |
|---|---|---:|---|
| transport timeout/network | `waiting_retry` | 예 | `provider_transport_error` |
| SearchAPI HTTP 429/5xx | `waiting_retry` | 예 | provider별 safe code |
| YouTube quota reason/429 | `waiting_quota` | quota reset 정책 | 기존 quota code |
| JSON decode 실패 | `waiting_retry` | 예 | `provider_invalid_json` |
| 필수 필드가 없는 HTTP 2xx | `waiting_retry` | 예 | `provider_invalid_payload` |
| DB pool timeout, serialization, deadlock, lock unavailable | `waiting_retry` | 예 | `database_temporarily_unavailable` |
| DB programming/constraint 오류 | `failed` | 아니요 | `database_operation_failed` |
| `commentsDisabled`, `videoNotFound` | partial warning/완료 | 아니요 | 기존 resource code |
| 잘못된 source 설정 | `failed` | 아니요 | `invalid_source_config` |

일반 retry는 동일 stage에서 `60초 → 120초 → 300초 → 600초`로 최대 4회만 수행한다. checkpoint가 정상 전진하면 연속 retry 횟수를 0으로 초기화한다. 최대 횟수 초과 시 마지막 구조화 오류를 보존한 채 terminal failure로 전환한다. quota 대기는 이 횟수에 포함하지 않는다.

## 4. 구현 작업

### Phase A. 신규 NUL 저장 실패 차단 — P0

대상 파일:

- `apps/collection-worker-rust/src/collector.rs`
- 신규 `apps/collection-worker-rust/src/sanitize.rs`
- 필요 시 `apps/collection-worker-rust/src/main.rs`

작업:

1. Python의 `_strip_nul`과 의미가 같은 Rust sanitizer를 작성한다.
2. `CommentInput` 생성 또는 `persist_comments()`의 DB bind 경계에서 다음 필드를 정규화한다.
   - comment/thread/parent ID
   - author channel ID, author name
   - `textDisplay`, `textOriginal`
3. `persist_video_items()`, `persist_searchapi_channel()`, transcript 저장 경로의 provider 문자열에도 같은 sanitizer를 적용한다.
4. 필수 ID가 정규화 뒤 비어 있으면 `provider_invalid_payload`로 분류하고 DB transaction을 시작하지 않는다.
5. 정규화된 실제 저장값으로 content hash와 downstream NLP 입력이 계산되는지 확인한다.

완료 기준:

- top-level comment와 reply에 `\u0000`이 있어도 PostgreSQL 저장과 checkpoint commit이 완료된다.
- 동일 페이지 재처리 시 comment 수와 rollup이 증가하지 않는다.
- 기존 Python fixture와 Rust 저장 결과가 일치한다.
- 신규 `invalid byte sequence for encoding "UTF8": 0x00` 로그가 0건이다.

### Phase B. malformed 2xx 및 DB 오류의 typed retry — P0

대상 파일:

- `apps/collection-worker-rust/src/searchapi.rs`
- `apps/collection-worker-rust/src/youtube.rs`
- `apps/collection-worker-rust/src/collector.rs`
- `apps/collection-worker-rust/src/main.rs`
- `crates/monitube-collection-store/src/lib.rs`
- 신규 `apps/collection-worker-rust/src/failure.rs`

작업:

1. `serde_json::from_slice(...).unwrap_or(Value::Null)`을 제거하고 JSON decode 실패를 별도 `SearchApiError`로 반환한다.
2. operation별 최소 응답 schema를 검증한다.
   - `youtube_channel`: `channel.id` 필수
   - `youtube_channel_videos`: 허용된 empty 결과와 malformed 결과 구분
   - `youtube`, transcript, YouTube Data API 응답도 각 operation의 최소 필드 검증
3. 기존의 provider 불문 `InvalidPayload`를 provider·operation이 포함된 typed failure로 교체한다.
4. `sqlx::Error`는 API Rust의 기존 DB 분류와 같은 기준으로 transient/terminal을 나눈다.
5. `waiting_retry` 전환 시 retry 횟수와 다음 `resume_at`을 하나의 fenced update로 기록한다.
6. lease owner가 바뀌거나 lease가 만료된 worker는 retry/terminal 상태를 기록할 수 없게 기존 fencing을 유지한다.
7. 최대 retry 횟수 초과 뒤에만 terminal failure를 기록한다.

완료 기준:

- HTTP 200 + invalid JSON, `null`, `{}`, `channel.id` 누락 fixture가 즉시 `failed`가 아니라 `waiting_retry`가 된다.
- 4회 실패 후에만 terminal 상태가 되고 마지막 오류 코드와 attempt가 남는다.
- 정상 후속 응답을 받으면 동일 Job이 checkpoint에서 이어서 진행한다.
- quota/resource 오류의 기존 상태 전이가 바뀌지 않는다.

### Phase C. durable 실패 메타데이터와 API/UI 표시 — P1

대상 파일:

- 신규 `database/migrations/024_collection_failure_metadata.sql`
- `crates/monitube-collection-store/src/lib.rs`
- `apps/api-rust/src/jobs.rs`
- `apps/api-rust/src/sources.rs`
- `apps/web/app/lib/api/types.ts`
- `apps/web/app/features/collection/workbench-pages.tsx`
- `apps/web/app/features/collection/workbench-model.ts`

expand-only migration 후보:

```text
sync_jobs.retry_count             integer not null default 0
sync_jobs.last_error_code         text null
sync_jobs.last_error_provider     text null
sync_jobs.last_error_operation    text null
sync_jobs.last_error_retryable    boolean null
sync_jobs.last_error_http_status  integer null
sync_jobs.last_error_at           timestamptz null
```

작업:

1. nullable/default 기반 additive migration으로 Python/이전 Rust image가 새 schema를 무시할 수 있게 한다.
2. 상태 전이와 오류 메타데이터를 같은 SQL update에서 기록한다.
3. `pause_reason`은 사용자용 안전 메시지로 유지하고, 분류에는 구조화 필드를 사용한다.
4. parent 최근 실패 조회는 대표 child의 구조화 오류를 `pause_reason`보다 우선한다.
5. 기존 `errorCode`, `retryable` API 필드는 이름을 바꾸지 않고 값만 채운다.
6. UI에는 다음을 구분해 표시한다.
   - `자동 재시도 예정 · N회차 · 시각`
   - `재시도 불가 · 오류 코드`
   - `quota 대기`
   - `영상/댓글 리소스 경고`
7. provider와 operation은 allowlist된 이름만 표시하며 원문 URL은 표시하지 않는다.

완료 기준:

- 재현 fixture의 최근 실패 API에 `errorCode`와 `retryable`이 null이 아니다.
- child DB 실패가 parent 화면에서도 실제 대표 코드로 표시된다.
- 기존 API consumer가 추가 필드 없이도 계속 동작한다.
- UI에서 system failure, quota wait, resource warning을 혼동하지 않는다.

### Phase D. 한정 복구 도구 — P1

대상 파일:

- `apps/maintenance-rust/src/main.rs`
- `scripts/runbooks/deployment-rollback.md` 또는 신규 collection recovery runbook

작업:

1. `collection-retry` maintenance command를 추가한다.
2. 기본 동작은 `--dry-run`이며 다음을 출력한다.
   - 정확한 Job ID
   - parent/source/target/video ID
   - 현재 state와 last error
   - 변경 예정 state와 예상 건수
3. 실제 적용은 명시적인 Job ID allowlist와 expected count가 모두 맞을 때만 허용한다.
4. failed child는 기존 checkpoint와 idempotency key를 유지한 채 `queued`로 되돌리고 lease/resume 필드만 안전하게 초기화한다.
5. 이미 `failed`인 parent는 모든 대상 child가 검증됐을 때만 `waiting_retry`로 전환한다. 아직 자식을 기다리는 parent는 불필요하게 수정하지 않는다.
6. 복구 전 대상 row를 release state directory의 mode-0600 JSONL과 DB dump에 보존한다.
7. `last_error_*`는 복구 후에도 감사 근거로 유지하고, 활성 `pause_reason`만 해제한다.
8. 같은 명령을 다시 실행해도 완료·진행 중 Job을 중복 재처리하지 않게 한다.

초기 복구 allowlist는 진단 당시 확인된 다음 10개 child Job으로 제한한다.

```text
2eb1fae0-be46-4506-847b-9c9e8c831f21
c08525b5-c339-4c0a-a2dd-3b259452516c
ac6e5592-b312-44e1-b3f3-25d93571dcae
d900e8a5-f2a6-43e7-b9b2-a2b007830422
7fd1cec2-1e3a-44a3-9e86-0c962c935139
7cf68629-5c1f-4226-a4ce-b7a7c6819024
191f3bf2-6091-4482-a421-a8e5d315597d
cf36a304-cf20-4c4c-af6f-e86d89a53647
cf8cdf41-9c60-4a17-9ce4-97bf82743b9c
02974198-a20c-4840-9ea9-905baf30b0fe
```

`quotaExceeded`, `commentsDisabled`, 일반 403 Job은 이 명령의 자동 선택 조건에 포함하지 않는다.

## 5. 테스트 계획

### 5.1 Rust unit/fixture 테스트

- 모든 provider 유래 문자열 필드의 NUL 제거
- NUL 제거 후 빈 필수 ID 거부
- top-level/reply parent 연결 보존
- SearchAPI invalid JSON, `null`, `{}`, 필수 필드 누락
- 유효한 empty collection 응답과 malformed 응답 구분
- timeout/429/5xx/malformed 2xx/DB transient/DB terminal 분류
- retry 1~4회 backoff 및 최대 횟수 초과
- quota/resource 오류가 일반 retry counter를 증가시키지 않음
- 오류 메시지에 API key, bearer token, URL, page token, comment text가 포함되지 않음

### 5.2 PostgreSQL 통합 테스트

- 실제 PostgreSQL에 `\u0000` 포함 댓글 페이지 저장
- page transaction rollback/checkpoint 원자성
- 동일 페이지 replay와 rollup idempotency
- 상태, retry count, `resume_at`, last error의 단일 transaction 전이
- lease lost 상태에서 stale worker update 차단
- parent가 child의 structured failure를 정확히 집계
- migration 전후 기존 row/default/null 호환
- recovery command dry-run/apply/re-run 멱등성

### 5.3 전체 gate

```text
make check-rust
make check
docker compose 기반 migration verification
collection worker container build
격리 PostgreSQL snapshot에서 targeted collection smoke
graceful shutdown 및 lease recovery smoke
```

## 6. 배포 계획

### 6.1 배포 전

1. 원격 revision, dirty worktree, migration current 상태를 다시 확인한다.
2. PostgreSQL dump와 release state directory를 생성한다.
3. source/video/comment 수, active/waiting/failed Job 수를 기록한다.
4. `0x00`, invalid payload, quota/resource별 baseline을 별도 집계한다.
5. 격리 DB에서 Python fixture와 Rust 결과를 비교한다.

### 6.2 적용 순서

1. additive migration 적용
2. Rust API가 nullable 신규 column을 읽는지 검증
3. 모든 collection consumer를 중지하고 lease 상태 확인
4. 새 Rust collection worker 1개에 queue ownership 이전
5. 짧은 smoke/soak 동안 readiness, restart, lease, DB error 확인
6. 통과하면 기존 2개 replica로 복원
7. web/API failure contract 확인
8. 최소 안정화 구간 통과 후에만 Phase D 복구 수행

같은 Job을 Python/Rust 또는 구/신 Rust worker가 동시에 실행하는 방식의 shadow/canary는 사용하지 않는다.

### 6.3 즉시 rollback 조건

- 신규 `0x00` PostgreSQL 오류 1건 이상
- 정상 provider 응답이 invalid payload로 분류됨
- retry가 최대 횟수나 backoff를 무시함
- Job state/lease/checkpoint 불일치
- comment/rollup 중복 또는 기존 count 감소
- DB pool timeout/CPU/lock wait가 baseline을 유의하게 초과
- worker restart 발생 또는 readiness 실패
- API/UI contract mismatch

rollback은 이전 immutable Rust image로 애플리케이션만 되돌리고 additive migration과 기존 데이터는 유지한다. PostgreSQL volume 복구는 데이터 손실 위험 때문에 자동 수행하지 않는다.

## 7. 기존 실패 Job 복구 순서

1. `collection-retry --dry-run` 결과가 정확히 10개 child Job인지 확인한다.
2. 각 Job의 원인이 `database_operation_failed`/기존 `collection database operation failed`이며 대상 video ID가 진단 목록과 일치하는지 확인한다.
3. quota/resource 오류가 0건 포함됐는지 확인한다.
4. parent 단위로 순차 적용한다. 한 parent의 child가 terminal이 될 때까지 다음 parent 복구를 시작하지 않는다.
5. 저장 댓글 수, rollup, checkpoint, parent state를 확인한다.
6. 실패한 항목은 그대로 보존하고 자동으로 다음 parent까지 확대하지 않는다.
7. `@stylist_unnie`, `@뽀구미`의 과거 malformed 2xx terminal Job은 후속 수집이 이미 진행됐으므로 데이터 결손을 먼저 비교한 뒤 필요한 경우에만 별도 재요청한다.
8. quota 대기 Job 7천여 건을 bulk release하지 않는다.

## 8. 운영 검증과 완료 기준

### 즉시 검증

- `/health`, `/ready`, web-to-API proxy 정상
- migration current
- worker replica/restart/lease 정상
- synthetic NUL comment 저장 성공
- malformed 2xx fixture가 `waiting_retry`와 code/retry time을 반환
- 최근 실패 UI에 `코드 없음`, `재시도 정보 없음`이 신규 system failure에서 나타나지 않음

### 24시간 관찰

- 신규 PostgreSQL `invalid byte sequence ... 0x00`: 0건
- 신규 generic `YouTube response is invalid`: 0건
- `provider_invalid_payload`의 retry 횟수가 상한 이내
- retry 후 성공률과 terminal 전환 건수 집계 가능
- quota/resource 오류가 system failure 지표에 섞이지 않음
- Job lease orphan, checkpoint 역행, duplicate child: 0건
- comment/video/source row count 감소: 0건
- restart count: 0

### 최종 완료 조건

- 10개 NUL 실패 child Job이 성공 또는 개별 근거가 남은 terminal 상태로 정리됨
- `키작남`, `@moviethink`, `@보다BODA`, `@SGBG`, `@globelab` parent 상태가 자식 결과와 일치
- `@stylist_unnie`, `@뽀구미`의 malformed 응답이 재발해도 bounded retry와 구조화 코드가 남음
- API와 UI가 reason/code/retryability/child count를 일관되게 표시
- rollback rehearsal와 운영 runbook 갱신 완료

## 9. 권장 실행 일정

| 순서 | 작업 | 예상 구간 |
|---|---|---|
| 1 | Phase A NUL sanitizer + 회귀 테스트 | 0.5일 |
| 2 | Phase B typed error + bounded retry | 0.5~1일 |
| 3 | Phase C migration/API/UI failure contract | 0.5~1일 |
| 4 | Phase D dry-run 복구 도구 + 통합 테스트 | 0.5일 |
| 5 | 격리 DB 검증, 배포, 1-replica soak | 0.5일 |
| 6 | 10개 child Job 순차 복구 | 배포 안정화 후 0.5일 |
| 7 | production 관찰 | 최소 24시간 |

기능 수정과 기존 실패 복구를 같은 트랜잭션이나 같은 즉시 배포 단계로 묶지 않는다. 신규 실패 차단이 확인된 뒤 복구 범위를 명시적으로 승인하고 진행한다.
