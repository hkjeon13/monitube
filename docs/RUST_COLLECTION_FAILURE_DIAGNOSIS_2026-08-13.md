# Rust 전환 후 수집 실패 진단

## 문서 범위

- 확인 시각: 2026-08-13 09:11 KST
- 원격 환경: `ssh ai-assistant`, `/data/psyche/Projects/monitube`
- 배포 리비전: `925d0b6e1fbc0cf9fefdb6f52bf2607715cb1a67`
- Rust 수집 워커 시작 시각: 2026-08-13 02:23 KST
- 대상: Rust 전환 뒤 발생한 수집 실패 중 시스템 자체 결함
- 제외: 영상 비공개·삭제, 댓글 비활성화, YouTube quota, 일반적인 provider 403
- 이 문서에는 수정안, 배포안, 실패 Job 재시도 절차를 포함하지 않는다.

## 결론

확정된 시스템 문제는 다음 3개다.

1. Rust 수집기가 댓글 문자열의 PostgreSQL 비허용 `NUL(0x00)` 문자를 제거하지 않는다.
2. SearchAPI가 HTTP 200이지만 계약에 맞지 않는 응답을 반환하면 Rust 워커가 일시 오류로 재시도하지 않고 즉시 영구 실패 처리한다.
3. Rust 실패 저장 계약이 원인·provider·오류 코드·재시도 가능 여부를 잃어 UI에 일반화된 문구만 노출한다.

## 문제 1. 댓글의 `NUL(0x00)` 미정규화로 페이지 전체 저장 실패

### 판정

시스템 결함으로 확정했다. 외부 댓글 내용에 PostgreSQL text가 허용하지 않는 `NUL` 문자가 포함될 수 있는데, Rust 저장 경로가 이를 그대로 SQL 파라미터로 전달한다.

### 근거

- Rust 수집기는 YouTube 응답의 `textDisplay` 또는 `textOriginal`을 그대로 `CommentInput.text`에 넣는다: `apps/collection-worker-rust/src/collector.rs:1913`.
- 저장 시 이 값을 별도 정규화 없이 `comments.text_display`, `comments.text_original`의 `$7` 파라미터로 바인딩한다: `apps/collection-worker-rust/src/collector.rs:368`.
- 전환 전 Python 저장 경로에는 댓글 ID, 영상 ID, parent/thread ID, `text_display`에서 `\x00`을 제거하는 방어 로직이 있었다: `apps/api/monitube_api/infrastructure/postgres_writes.py:260` 및 `apps/api/monitube_api/infrastructure/postgres_support.py:6`.
- Rust 전환 뒤 실제 PostgreSQL 업무 오류 10건은 모두 동일했다.

```text
ERROR: invalid byte sequence for encoding "UTF8": 0x00
CONTEXT: unnamed portal parameter $7
STATEMENT: INSERT INTO comments (... text_display, text_original ...)
```

- PostgreSQL 오류 시각과 실패 child Job의 `updated_at`이 10건 모두 일치한다.
- 한 댓글에서 오류가 나면 현재 페이지의 트랜잭션 전체가 rollback되고 child Job은 `collection database operation failed`로 영구 실패한다.

### 확인된 영향 범위

| 부모 Job | 대상 | 실패 child 수 | 영상 ID | 2026-08-13 09:11 KST 상태 |
|---|---|---:|---|---|
| `c89be3bc-ab4e-4894-8059-db0942356901` | 키워드 `키작남` | 1 | `OGUo7KZm0Wk` | `failed` |
| `d280b678-2f0b-41e0-bb54-6f8222c0586a` | 채널 `@moviethink` | 3 | `8q5DJlAWygo`, `UXgaSofmUkE`, `ttZh6kryYg8` | `failed` |
| `c15ac738-4e58-4d97-a98f-ae4515f5b24e` | 채널 `@보다BODA` | 1 | `Z02pC4Ar14k` | 자식 대기 중 |
| `de06d3d2-0886-4d6c-9d55-f4e2964d0a23` | 채널 `@SGBG` | 3 | `_jFddgWBcIU`, `bPZGr1AAZUE`, `zHcT4kM2QJo` | 자식 대기 중 |
| `8a141589-1f05-4b9e-85d8-f2c063783e0c` | 채널 `@globelab` | 2 | `oXl6WmhfJrA`, `uqz_FmqBymo` | 자식 대기 중 |

사용자가 제시한 `키작남` 1건과 `@moviethink` 3건은 이 결함으로 직접 설명된다. 같은 결함은 제시 목록 밖의 6개 child Job에도 이미 발생했다.

## 문제 2. 정상 HTTP 상태의 비정상 SearchAPI 응답을 영구 실패 처리

### 판정

SearchAPI가 일시적으로 계약에 맞지 않는 응답을 반환한 것 자체의 provider 원인은 원본 body가 보존되지 않아 확정할 수 없다. 그러나 이 응답을 재시도 가능한 provider 오류로 분류하지 않고 즉시 영구 실패시킨 동작은 시스템 결함으로 확정했다.

### 근거

- 두 실패 Job 모두 `provider_request_logs`에는 `provider=searchapi`, `operation=youtube_channel`, `status_code=200` 한 건만 존재한다.
- Rust SearchAPI client는 JSON decode 실패도 `Value::Null`로 바꾸고, HTTP가 2xx이면 payload 구조를 검증하지 않은 채 `Ok(payload)`로 반환한다: `apps/collection-worker-rust/src/searchapi.rs:142`.
- 채널 수집기는 그 뒤 `channel` object 또는 `channel.id`가 없으면 `CollectorError::InvalidPayload`를 반환한다: `apps/collection-worker-rust/src/collector.rs:748`.
- `InvalidPayload`는 provider와 무관하게 `YouTube response is invalid`라는 고정 문구를 사용한다: `apps/collection-worker-rust/src/collector.rs:1961`.
- worker main은 `YouTubeError`와 `SearchApiError`만 대기 재시도로 보내고, `InvalidPayload`는 일반 오류 분기에서 즉시 `failed`로 종료한다: `apps/collection-worker-rust/src/main.rs:150`.

### 확인된 영향 범위

| 실패 Job | 채널 | 실패 시 provider 기록 | 후속 동일 대상 Job의 확인 결과 |
|---|---|---|---|
| `295d03b5-8bbd-47fc-9258-43f464131430` | `@stylist_unnie` | `youtube_channel`, HTTP 200, 이후 단계 없음 | 후속 Job `582a6084-...`에서 profile 200 후 video page 31회 진행 |
| `5617e7e8-6c9f-4b05-96d2-c349f8f48572` | `@뽀구미` | `youtube_channel`, HTTP 200, 이후 단계 없음 | 후속 Job `424dfc0f-...`에서 profile 200 후 video page 26회 진행 |

두 채널 모두 나중 실행에서는 동일 수집 경로가 채널 profile을 통과하고 영상 목록 단계까지 진행했다. 따라서 영구적인 채널 비공개·삭제·접근 불가로 볼 근거가 없고, 일시적인 malformed/empty 2xx 응답을 terminal failure로 확정한 것이 내부 문제다.

## 문제 3. 실패 원인과 재시도 정보를 잃는 Rust 오류 계약

### 판정

시스템 관측성 결함으로 확정했다. UI의 `재시도 정보 없음`, `코드 없음`은 실제 정보가 없어서가 아니라 Rust worker와 실패 API 사이에서 구조화된 오류 정보가 저장되지 않기 때문에 발생한다.

### 근거

- `CollectorError::Database`의 표시 문자열은 원래 `sqlx::Error`와 관계없이 항상 `collection database operation failed`다.
- worker는 일반 오류를 `error.to_string()` 하나로 `pause_reason`에 저장하며 DB 오류의 source chain, SQLSTATE, 단계, 파라미터 종류를 로그나 Job 필드에 남기지 않는다.
- SearchAPI 채널 응답 오류도 `YouTube response is invalid`로 기록되어 실제 provider가 잘못 표시된다.
- 최근 실패 API는 `pause_reason`만 발견하면 `(reason, None, None)`을 반환한다: `apps/api-rust/src/jobs.rs:417`. 따라서 `errorCode`와 `retryable`은 `null`이 된다.
- 하위 Job의 DB 원인은 UI/API/worker 로그만으로 확인되지 않았고 PostgreSQL 원본 로그를 대조해야만 `0x00`, `$7`, `INSERT INTO comments`를 특정할 수 있었다.

### 영향

- 내부 데이터 결함과 외부 provider 장애를 UI에서 구분할 수 없다.
- 재시도 가능한 malformed provider 응답도 `재시도 정보 없음`으로 보인다.
- 부모 Job에는 `N child collection jobs failed`만 남고 대표 child의 실제 DB 원인이 소실된다.
- 동일 문구가 서로 다른 parsing 단계에서 재사용되어 실패 지점을 식별할 수 없다.

## 제외한 항목

다음은 이번 시스템 문제 목록에 포함하지 않았다.

- 사용자가 제시한 2026-08-11~12의 `YouTube commentThreads request failed with HTTP 403 (forbidden)` 항목은 Rust 수집 워커가 2026-08-13 02:23 KST에 시작되기 전 기록이다.
- Rust 전환 뒤 확인된 `quotaExceeded` 대기 작업은 외부 quota 상태이므로 제외했다. 확인 시점에는 `commentThreads` 7,684건, `videos` 108건, `comments` 28건이 `waiting_quota`였다.
- `commentsDisabled`로 완료된 영상은 영상별 댓글 비활성화 상태로 정상 partial warning 처리되므로 제외했다.
- 영상 비공개·삭제를 직접 가리키는 `videoNotFound` 등은 이번 확정 시스템 오류 집합에서 발견되지 않았다.

## 확인 경계

- 원격 Job/child Job 상태, provider request log, PostgreSQL error log, 배포 리비전과 실행 중 컨테이너를 읽기 전용으로 확인했다.
- SearchAPI 응답 원문은 저장되어 있지 않아 두 HTTP 200 응답에서 실제로 빠진 필드 외의 body 내용은 확인할 수 없다.
- 코드 수정, Job 재시도, 데이터 변경, 서비스 재시작은 수행하지 않았다.
