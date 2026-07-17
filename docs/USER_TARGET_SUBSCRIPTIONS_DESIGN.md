# 사용자별 수집 대상과 공유 데이터 분리 설계

> 상태: 제안 (구현 전)  
> 작성일: 2026-07-17  
> 대상: 로그인 사용자, Sources/Channels/Explore, 수집 작업 및 PostgreSQL 모델

## 1. 결정 요약

YouTube의 공개 채널·영상·댓글 데이터와 실제 수집 작업은 **서비스 전체에서 한 번만** 유지한다. 반면 사용자가 Sources에서 보는 항목, 삭제·알림·자동 갱신의 선택은 **사용자별 구독(subscription)** 으로 분리한다.

이를 위해 현재의 사용자 소유 `collection_sources`를 그대로 다중 사용자 UI 항목으로 재사용하지 않는다. 대신 다음의 세 계층을 명확히 나눈다.

```text
app_users
  └─ collection_subscriptions       사용자별 "내 Sources" 항목
       └─ collection_targets         정규화된 공용 수집 대상과 coverage
            ├─ collection_target_videos
            ├─ sync_jobs             공용 수집/재시도 작업
            ├─ channels / videos / comments
            └─ collection_target_pins (대상 갱신 정책)
```

예: A가 B 채널을 수집한 뒤 C가 같은 B 채널을 추가하면, C에게는 별도 Sources 항목이 생기지만 B의 공개 영상·댓글과 수집 이력은 복제하지 않는다. 이미 coverage가 충분하면 수집 작업을 만들지 않고 즉시 공유 결과를 제공한다. 작업 중이면 같은 target 작업에 합류한다.

## 2. 해결하려는 문제

현재 구조에는 `collection_targets`와 `sync_jobs`가 canonical target 기준으로 공유되는 장점이 있다. 하지만 `collection_sources.owner_id`가 사용자별 목록 역할과 worker의 대표 source 역할을 동시에 맡고 있다.

그 결과 두 번째 사용자가 동일 대상을 요청할 때 다음 문제가 생길 수 있다.

1. 기존 target·job은 재사용되지만, 두 번째 사용자의 `collection_sources` 행이 생기지 않는다.
2. 첫 사용자 소유 source를 반환한 뒤 owner 할당이 no-op이 되어, 두 번째 사용자의 Sources 목록에 대상이 보이지 않는다.
3. `Explore`와 검색이 현재 전역 public-data 테이블을 넓게 읽을 수 있어, 사용자별 접근 범위가 명확하지 않다.
4. target에 붙은 `owner_id`는 public-data 공유 모델과 충돌한다. target은 특정 사용자의 소유물이 아니라 공용 엔티티여야 한다.

## 3. 용어와 책임

| 용어 | 저장 단위 | 책임 | 사용자에게 보임 |
| --- | --- | --- | --- |
| 공용 콘텐츠 | `channels`, `videos`, `comments` | YouTube 공개 데이터의 단일 원본 | 구독 범위 안에서만 |
| 공용 수집 대상 | `collection_targets` | 입력 정규화, coverage, 결과 membership, 작업 coalescing | 간접적으로만 |
| 사용자 구독 | `collection_subscriptions` | 사용자의 Sources 목록, 대상 접근 권한, 개인 설정 | 예 |
| 수집 작업 | `sync_jobs` | target 단위 API 호출, checkpoint, quota, 재시도 | 상태만 구독자에게 |
| 수집 요청 | `collection_requests` | 한 번의 사용자 요청과 공유 job의 감사 기록 | 필요 시 요청 이력 |

UI에는 “별칭” 대신 **수집 대상** 또는 **구독 중인 대상**이라고 표시한다. 기술적으로 subscription은 공용 target으로 향하는 사용자별 연결이다.

## 4. 목표와 비목표

### 목표

- 같은 canonical channel/keyword/video target은 사용자 수와 무관하게 하나만 둔다.
- 같은 target의 영상·댓글·통계는 한 번만 저장한다.
- 각 사용자는 자신이 추가한 target만 Sources, Channels, Explore, 검색에서 본다.
- 동일 target의 기존 coverage가 요청 범위를 만족하면 API 호출 없이 즉시 완료한다.
- 동일 target의 작업이 실행 중이면 새 요청은 해당 job에 `joined`로 연결한다.
- 더 넓은 coverage 요청이 들어오면 target의 요구 범위를 병합하고 하나의 후속/진행 job으로 처리한다.
- 한 사용자가 Sources에서 제거해도 다른 사용자의 구독, 공용 콘텐츠, 진행 중 작업을 삭제하지 않는다.

### 비목표

- 사용자별로 공개 YouTube 영상·댓글을 복제하거나 별도 캐시하지 않는다.
- 사용자별 API key·OAuth credential·quota를 받거나 저장하지 않는다.
- 서로 다른 keyword query를 하나의 target으로 병합하지 않는다. 정규화된 query+필터가 동일한 경우만 공유한다.
- 공개 콘텐츠 자체의 서비스 전체 보존·삭제 정책을 이 설계에서 변경하지 않는다.

## 5. 데이터 모델

### 5.1 신규 테이블: `collection_subscriptions`

```sql
CREATE TABLE collection_subscriptions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
  target_id UUID NOT NULL REFERENCES collection_targets(id) ON DELETE CASCADE,
  display_config JSONB NOT NULL DEFAULT '{}',
  enabled BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, target_id)
);

CREATE INDEX collection_subscriptions_user_created_idx
  ON collection_subscriptions (user_id, created_at);
CREATE INDEX collection_subscriptions_target_idx
  ON collection_subscriptions (target_id);
```

`display_config`에는 사용자에게 보이는 입력 표현(예: `@handle`)과 향후 개인 UI 설정만 둔다. 실제 수집 범위·coverage 판단에 쓰는 config는 `collection_targets.config`와 요청 이력에 둔다.

### 5.2 기존 테이블의 역할 조정

| 테이블 | 변경 후 역할 |
| --- | --- |
| `collection_targets` | 전역 canonical target. `owner_id` 사용을 중단하고 이후 migration에서 제거한다. |
| `collection_sources` | worker/호환성·감사 레코드. 사용자 목록 또는 권한의 source of truth가 아니다. |
| `collection_requests` | 요청자와 subscription을 기록하고, shared job과 disposition을 연결한다. |
| `collection_target_pins` | target 단위 스케줄 정책. 적어도 하나의 enabled subscription이 있을 때만 활성화한다. |
| `collection_target_videos` | target에서 발견한 공유 비디오 membership. 유지한다. |
| `channels`, `videos`, `comments` | 공용 공개 콘텐츠. 유지한다. |

### 5.3 요청 감사 확장

`collection_requests`에 다음 nullable 컬럼을 추가한다.

```sql
ALTER TABLE collection_requests
  ADD COLUMN user_id UUID REFERENCES app_users(id) ON DELETE SET NULL,
  ADD COLUMN subscription_id UUID REFERENCES collection_subscriptions(id) ON DELETE SET NULL;

CREATE INDEX collection_requests_user_created_idx
  ON collection_requests (user_id, created_at DESC)
  WHERE user_id IS NOT NULL;
```

기존 `source_id`는 레거시 worker 연결을 위해 보존한다. 새 코드에서 사용자 식별은 `subscription_id`를 우선한다.

## 6. 쓰기 흐름

### 6.1 `POST /v1/collection-requests`

요청을 인증 사용자 ID와 함께 service/repository에 전달한다. 현재처럼 요청 완료 뒤 `assign_source_owner`를 호출하지 않는다. owner 결정과 subscription 생성은 target lock을 잡은 **동일 transaction** 안에서 수행한다.

```text
1. 입력을 canonical key + aliases로 정규화한다.
2. collection_targets(type, canonical_key)를 FOR UPDATE로 찾거나 생성한다.
3. INSERT ... ON CONFLICT (user_id, target_id) DO UPDATE로 subscription을 확보한다.
4. target coverage와 새 요청 coverage를 비교한다.
5. 충분함: request 상태 completed, job 없음.
6. 적합한 active job 존재: request 상태 joined, 그 job 연결.
7. 부족함: target-level 새 job을 만들거나 queued job의 scope를 확장한다.
8. request(user_id, subscription_id, target_id, job_id, disposition)를 기록한다.
9. subscription과 target 상태를 한 응답으로 반환한다.
```

### 6.2 config 병합 규칙

target의 수집 config는 public-data coverage에 영향을 주는 범위만 단조롭게 확장한다.

| 종류 | 공용으로 병합 가능한 값 |
| --- | --- |
| channel | `includeComments=true` 우선, 전체 업로드/댓글 범위 확장 |
| video | `includeComments=true` 우선, 댓글 범위 확장 |
| keyword | canonical query·기간·언어·지역·정렬이 같을 때만 같은 target. 댓글 수집 여부는 확장 가능 |

새 요청이 더 좁은 범위여도 기존 공용 coverage를 줄이지 않는다. 좁은 요청은 기존의 넓은 결과를 재사용한다.

### 6.3 갱신 pin

현재 channel pin은 target에 붙어 있다. 유지하되 활성 조건을 다음처럼 바꾼다.

- enabled subscription이 하나 이상 있으면 target-level pin을 활성화할 수 있다.
- 마지막 enabled subscription이 제거/비활성화되면 pin을 비활성화한다.
- 한 사용자의 “수집 일시정지”는 기본적으로 자신의 subscription만 비활성화한다. 공용 pin을 끄는 별도 관리 기능은 운영자 권한으로 제한한다.

이 정책은 A가 채널을 잠시 숨겨도 C의 자동 갱신이 중단되지 않게 한다.

## 7. 읽기·권한 모델

### 7.1 Sources와 결과

| API | 읽기 조건 |
| --- | --- |
| `GET /v1/sources` | `collection_subscriptions.user_id = current_user.id` |
| `GET /v1/sources/{id}` | 해당 source DTO가 가리키는 subscription의 사용자와 일치 |
| `GET /v1/sources/{id}/results` | 위와 동일. 결과는 subscription → target → target videos로 조인 |
| `GET /v1/jobs/{id}` | request/subscription을 통해 현 사용자가 해당 target에 구독 중일 때 |

Sources API는 기존 URL을 유지해 프런트 호환성을 확보한다. 응답의 `id`는 subscription ID가 되며, `targetId`는 공용 target ID로 계속 제공한다.

### 7.2 Explore·통합 검색

공용 `videos`/`comments` 테이블을 직접 전역 조회하지 않는다. 반드시 사용자의 subscription에서 도달 가능한 target으로 scope를 제한한다.

```sql
-- 개념 예시: 현재 사용자가 구독한 target에서 발견한 영상만 Explore에 노출
SELECT v.*
FROM collection_subscriptions s
JOIN collection_target_videos tv ON tv.target_id = s.target_id
JOIN videos v ON v.id = tv.video_id
WHERE s.user_id = :current_user_id
  AND s.enabled;
```

댓글 검색도 같은 video/target membership으로 제한한다. 동일 영상이 여러 구독 target에 속하면 video ID 기준으로 dedupe한다.

동일 작성자의 다른 댓글 상세는 현재 사용자가 접근 가능한 target들의 콘텐츠에서만 조회한다. “전체 DB”는 **그 사용자가 구독한 전체 데이터**라는 뜻으로 정의한다.

## 8. 삭제와 보존

| 사용자 동작 | 처리 |
| --- | --- |
| Sources에서 제거 | 해당 사용자의 subscription만 삭제/비활성화 |
| 마지막 subscription 제거 | target pin 비활성화. target·공용 콘텐츠는 retention 정책에 따라 유지 |
| 계정 삭제 | 사용자의 subscriptions/세션/request-user link 삭제. 다른 사용자와 공용 콘텐츠 유지 |
| 운영 retention 만료 | target membership 및 public content를 정책에 따라 정리. 이 설계와 별도 lifecycle job에서 처리 |

`collection_targets`, `videos`, `comments`에 `ON DELETE CASCADE`로 즉시 연결된 공용 데이터를 지우지 않는다.

## 9. 마이그레이션 및 롤아웃

### Phase 1 — Expand

1. 신규 `collection_subscriptions`, request 컬럼·index를 추가한다.
2. `collection_targets.owner_id`는 제거하지 않고 deprecated로 표시한다.
3. 기존 `collection_sources(owner_id, target_id)`에서 subscription을 backfill한다.
4. 소유자 없는 legacy source/target은 현재 `psyche` 계정에 한 번만 귀속한다. 대상 사용자가 없으면 migration을 중단하지 않고 unclaimed 상태로 남긴다.

### Phase 2 — Dual read / single write

1. 새 요청은 subscription을 생성하고 request에 사용자/구독을 기록한다.
2. Sources API는 subscription 기반으로 읽되, backfill되지 않은 행은 legacy source를 fallback으로 읽는다.
3. target-level job coalescing과 coverage 재사용은 기존 동작을 유지한다.
4. Explore/Search에 subscription scope 필터를 추가한다.

### Phase 3 — Cutover

1. 모든 활성 legacy source에 subscription이 있는지 검증한다.
2. fallback read와 `assign_source_owner` 호출을 제거한다.
3. `collection_targets.owner_id`와 owner 중심 target authorization을 제거한다.
4. `collection_sources`를 worker compatibility/audit용으로 축소하거나 후속 migration에서 `target_worker_sources`로 명확히 분리한다.

### 안전 장치

- migration은 expand-only로 시작하며 기존 public 콘텐츠나 job을 삭제하지 않는다.
- `(user_id, target_id)` unique 제약으로 중복 클릭·동시 요청에도 사용자별 항목은 하나만 만든다.
- target 행 lock과 현재의 active-job unique/coalescing 로직을 그대로 사용한다.
- rollout 중 모든 API 응답에 `targetId`를 유지해 프런트 캐시/URL 호환성을 보장한다.
- migration 전후에 사용자별 subscription 수, target 수, job 수, target video membership 수를 비교한다.

## 10. API/UI 변경안

### API

| 기존 | 변경 |
| --- | --- |
| `POST /v1/collection-requests` | 인증 user를 내부로 전달하고 subscription DTO를 반환 |
| `GET /v1/sources` | 현재 사용자 subscription 목록 반환 |
| `DELETE /v1/sources/{id}` | subscription 해제; 공용 target 삭제 금지 |
| `PUT /v1/collection-targets/{id}/pin` | 사용자 설정 endpoint로 대체하거나 운영자 전용으로 제한 |
| `GET /v1/explore`, `GET /v1/search` | subscription scope를 강제 |

### UI

- Sources: “내 수집 대상”만 표시한다.
- 같은 target을 추가하면 새 행을 중복 생성하지 않고 “이미 추가된 대상입니다”를 보여 준다.
- 다른 사용자가 이미 수집한 target이면 “공유 데이터 사용 가능” 또는 “수집 완료 데이터 연결됨”을 노출할 수 있다. 다른 사용자의 ID·이름·요청 내역은 노출하지 않는다.
- 완료/진행 중 상태는 공용 target job을 읽되, 항목 관리·숨김·개인 자동 갱신은 subscription 기준으로 처리한다.

## 11. 검증 시나리오

1. A가 B 채널을 전체 댓글 포함으로 요청한다. target/job/content가 생성된다.
2. B 수집 완료 후 C가 같은 채널을 요청한다. C subscription만 생성되고 YouTube API 호출은 0회다.
3. B 수집 중 C가 같은 채널을 요청한다. C request는 `joined`이며 같은 job ID를 받는다.
4. A가 댓글 미포함으로, C가 댓글 포함으로 요청한다. target coverage가 확장되고 하나의 후속/진행 job만 존재한다.
5. A가 자신의 Sources에서 B를 삭제한다. C의 Sources와 pin, 공유 결과는 유지된다.
6. C가 로그인한 상태에서 Explore/Search/댓글 상세를 호출한다. C가 구독한 target에서 발견한 콘텐츠만 반환된다.
7. concurrent POST 2개가 같은 `(user, target)`으로 도착해도 subscription은 하나만 생긴다.
8. migration 전 legacy `psyche` source와 이후 생성 계정의 source가 모두 정상적으로 보이고 기존 job/result URL이 유지된다.

## 12. 구현 순서와 완료 기준

1. migration과 repository protocol을 추가한다.
2. PostgreSQL/InMemory repository의 target+subscription transaction을 구현한다.
3. service/main authorization과 response DTO를 subscription 기준으로 바꾼다.
4. Sources/Explore/Search/댓글 상세 query에 scope join을 적용한다.
5. pin·삭제·자동 갱신의 subscription semantics를 적용한다.
6. API, repository, worker coalescing, migration backfill 통합 테스트를 추가한다.
7. staging에서 A/C 동시 요청과 삭제 시나리오를 검증한 뒤 production migration을 실행한다.

완료 기준은 서로 다른 두 계정이 동일 target을 추가했을 때, 각자 Sources에 독립 항목이 나타나고 한 번만 수집되며 서로의 사용자 정보나 구독하지 않은 콘텐츠는 보이지 않는 것이다.
