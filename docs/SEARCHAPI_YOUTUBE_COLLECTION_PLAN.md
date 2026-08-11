# SearchAPI.io 기반 YouTube 발견·대본 수집 전환 계획

> 상태: 2026-08-11 구현 완료. 원격 배포 및 운영 canary 결과는 배포 검증 기록으로 별도 확인한다.

구현 범위에는 SearchAPI client, 채널·키워드 discovery 전환, 공식 `videos.list` 보강, 신규 영상 대본의 한국어→영어 fallback, provider별 저장/audit, source별 `지금 재수집`, 6시간 자동 주기 재계산, migration·환경·테스트가 포함된다. 댓글 수집 코드는 공식 YouTube API 경로를 유지한다.

## 1. 결정 요약

공식 YouTube Data API quota를 많이 사용하는 **키워드 검색**과 **채널 영상 목록 조회**를 SearchAPI.io로 전환한다. 댓글은 SearchAPI.io로 전환하지 않고 현재 공식 YouTube Data API 수집 방식을 유지한다. 채널·키워드는 등록 후 기본적으로 6시간마다 자동 수집하며, 사용자가 원할 때 `지금 재수집`으로 다음 예약 시각을 기다리지 않고 한 번 즉시 실행할 수 있다.

| 기능 | 사용할 provider | 비고 |
| --- | --- | --- |
| 키워드별 영상 발견 | SearchAPI.io `engine=youtube` | 공식 `search.list` 대체 |
| 채널 ID/handle 확인과 채널 정보 | SearchAPI.io `engine=youtube_channel` | 채널 고유 ID를 먼저 확정 |
| 채널 내 영상 목록 | SearchAPI.io `engine=youtube_channel_videos` | 기본 최신순 pagination 사용 |
| 발견 영상 상세 보강 | 공식 YouTube Data API `videos.list` | 50개씩 batch; 정확한 게시 시각·통계·댓글 수 확보 |
| 직접 영상 등록 | 기존 공식 YouTube Data API 경로 유지 | 이번 provider 전환 범위 밖 |
| 댓글과 답글 | 기존 공식 `commentThreads.list` + `comments.list` | 변경하지 않음 |
| 영상 대본 | SearchAPI.io `engine=youtube_transcripts` | 한국어 우선, 없으면 영어 |
| 채널·키워드 즉시 재수집 | source별 즉시 job | 기본 6시간 자동 수집은 유지하고 클릭 시 한 번 앞당겨 실행 |

핵심 원칙은 다음과 같다.

- SearchAPI.io는 영상 ID를 **발견**하는 provider다.
- 공식 `videos.list`는 발견된 영상의 canonical metadata를 **보강**하는 provider다.
- 댓글은 정확한 `publishedAt`, `updatedAt`, parent/thread 관계를 제공하는 현재 공식 API 경로를 그대로 쓴다.
- SearchAPI.io 장애 때 공식 `search.list`나 playlist 순회로 자동 fallback하지 않는다. 운영자가 명시적으로 provider를 되돌릴 때만 공식 discovery를 사용한다.
- 기존 저장 영상·댓글은 일괄 재수집하거나 덮어쓰지 않는다.
- 채널·키워드 source의 수동 재수집은 예약과 같은 증분 수집 경로를 즉시 실행하며, 중복 active job은 만들지 않는다.

## 2. 공식 문서 기준

- [YouTube Search API](https://www.searchapi.io/docs/youtube): `engine=youtube`, `q`, `sp`, `pagination.next_page_token`
- [YouTube Channel API](https://www.searchapi.io/docs/youtube-channel): `engine=youtube_channel`, `channel_id`
- [YouTube Channel Videos API](https://www.searchapi.io/docs/youtube-channel-videos-api): `engine=youtube_channel_videos`, `channel_id`, `next_page_token`
- [YouTube Transcripts API](https://www.searchapi.io/docs/youtube-transcripts): `engine=youtube_transcripts`, `video_id`, `lang`, `transcript_type`

공통 요청은 `https://www.searchapi.io/api/v1/search`로 보내고 API key는 query string이 아니라 `Authorization: Bearer` header로 전달한다. key, 전체 요청 URL, pagination token, 대본 원문은 로그에 남기지 않는다.

`zero_retention=true`는 문서상 Enterprise 전용이다. 실제 요금제가 이를 지원하지 않으면 SearchAPI.io의 요청·HTML·JSON 보존 정책과 기간을 운영 전에 확인한다.

## 3. 현재 Monitube 동작과 유지할 부분

현재 repository 기준으로 다음 동작은 유지한다.

- 키워드와 채널 pin의 기본 재조회 간격은 모두 `360분`, 즉 6시간이다.
- 신규 등록·재활성화 시 `next_run_at=now()`로 두어 worker의 다음 polling에서 첫 수집을 시작한다.
- worker 기본 polling 간격은 3초다.
- 다음 예약 시각은 job 완료 시각이 아니라 dispatch 시각 + 6시간이다.
- 같은 target의 active job이 있으면 중복 job을 만들지 않고 다음 예약 시각만 진행한다.
- backend에는 이미 `POST /v1/sources/{sourceId}/jobs` 즉시 job 생성과 target별 active job 재사용 경계가 있다. 다만 채널·키워드별 “지금 재수집” 제품 흐름과 예약 시각 처리, 명확한 disposition 응답은 아직 별도로 정리돼 있지 않다.
- 댓글 page와 checkpoint의 transaction, reply 전체 순회, comment ID upsert는 유지한다.
- 공식 YouTube quota ledger와 SearchAPI.io 요청량은 서로 섞지 않는다.

변경 대상은 `apps/worker/monitube_worker/collection/discovery.py`의 키워드 검색 및 채널 영상 ID 발견 경로다. 댓글 수집 모듈은 provider 변경 대상이 아니다.

## 4. 목표 흐름

```text
채널 등록
  -> SearchAPI youtube_channel로 canonical channel ID와 채널 정보 확인
  -> SearchAPI youtube_channel_videos로 최신 영상 ID 수집
  -> 공식 videos.list로 최대 50개씩 canonical metadata 보강
  -> 신규/갱신 대상 영상에 기존 공식 댓글 수집
  -> 신규 영상이면 SearchAPI transcript 수집

키워드 등록
  -> SearchAPI youtube로 영상/Shorts ID 수집
  -> 광고·채널·playlist·post 등 비영상 결과 제외
  -> 공식 videos.list로 최대 50개씩 canonical metadata와 정확한 게시 시각 보강
  -> 설정된 날짜 조건을 exact publishedAt으로 후처리
  -> 신규/갱신 대상 영상에 기존 공식 댓글 수집
  -> 신규 영상이면 SearchAPI transcript 수집
```

공식 API에서 줄어드는 주된 사용량은 고비용 `search.list`와 채널 uploads playlist pagination이다. `videos.list`와 댓글 endpoint는 정확성 경계를 위해 유지한다.

## 5. 채널 조회 계획

### 5.1 채널 식별

`youtube_channel`은 channel ID와 `@handle`을 입력으로 받는다.

- channel ID 입력: 그대로 `channel_id`에 전달
- `@handle` 또는 handle URL: 정규화 후 `@handle` 전달
- 자유 텍스트 채널명: SearchAPI `youtube` 검색 결과 중 channel 결과로 후보를 찾은 뒤, 단일 후보가 확실할 때만 `youtube_channel`로 확인
- legacy username URL: 실제 지원 여부를 Phase 0 contract probe에서 확인; 직접 지원되지 않으면 SearchAPI 검색으로 후보를 제시하고 임의 자동 선택은 하지 않음

채널 등록의 canonical key는 반드시 응답 `channel.id`로 확정한다. handle은 변경 가능하므로 target의 고유키로 쓰지 않는다.

### 5.2 SearchAPI 채널 전용 정보

`youtube_channel`은 현재 Monitube가 저장하지 않는 다음 정보를 제공한다.

- `keywords`, `tags`
- `available_countries`
- `badges`, `is_verified`, `is_family_safe`
- `banner`, `avatar`
- `first_link`, `about.links`
- `about.joined_date`

이 값들은 공식 댓글 데이터와 혼합하지 않고 channel provider profile로 분리한다. additive schema 후보는 다음과 같다.

`channel_provider_profiles`

- `channel_id`, `provider=searchapi_youtube_channel`
- `keywords`, `tags JSONB`, `available_countries JSONB`
- `badges JSONB`, `is_verified`, `is_family_safe`
- `banner_url`, `avatar_url`, `external_links JSONB`, `joined_date`
- `source_fetched_at`, `raw_schema_version`

subscriber/view/video count는 기존 `channel_snapshots`에 저장하되 `source_attribution=searchapi_youtube_channel`로 구분한다. 기존 공식 snapshot은 유지한다.

### 5.3 채널 영상 목록

`youtube_channel_videos`의 기본 결과는 최신 영상 목록이며 다음 page는 `pagination.next_page_token`으로 진행한다.

- 첫 page: `engine=youtube_channel_videos`, `channel_id`
- 다음 page: 같은 channel ID와 이전 `next_page_token`
- `Popular`, `Oldest` filter token은 일반 최신 갱신에 사용하지 않는다.
- token이 길어질 수 있으므로 문서 권고대로 길이가 임계값을 넘으면 JSON body의 POST로 전환한다. checkpoint와 로그에 token 원문을 노출하지 않는다.
- pagination token은 provider와 schema version을 함께 checkpoint에 저장한다. provider가 바뀐 token은 재사용하지 않는다.

예약 갱신은 첫 page부터 시작해 모든 영상 ID가 이미 알려진 page를 만났을 때 중단할 수 있다. 단, 이 최적화는 Phase 0에서 기본 정렬이 최신순이고 page 간 결과가 안정적인 것이 확인된 뒤에만 켠다. 전체 backfill은 token이 끝날 때까지 계속한다.

### 5.4 채널 영상 응답 mapping

| SearchAPI.io | 용도 | canonical 저장 정책 |
| --- | --- | --- |
| `videos[].id` | YouTube 영상 ID | `videos.youtube_video_id` 후보 |
| `title` | 발견 preview | 공식 `videos.list` 값으로 보강 |
| `views` | 발견 시점 preview | 공식 video snapshot을 canonical로 사용 |
| `length` | 발견 preview | 공식 ISO 8601 duration을 canonical로 사용 |
| `published_time` | 상대 표시 | `videos.published_at`에 저장하지 않음 |
| `thumbnail.static/rich` | 썸네일 후보 | discovery evidence에 선택 저장 가능 |
| `position` | provider 순번 | page + position으로 발견 근거 저장 |

상대 `published_time`을 수집 시각에서 계산할 수는 있지만 채널·키워드 discovery에서는 그럴 필요가 없다. 공식 `videos.list`가 정확한 `snippet.publishedAt`을 제공하므로, 추정 timestamp로 canonical 값을 오염시키지 않는다.

## 6. 키워드별 영상 검색 계획

### 6.1 요청과 pagination

첫 요청은 다음 형태다.

```text
engine=youtube
q=<keyword>
gl=<region, default kr>
hl=<interface language, default ko>
sp=<optional native YouTube filter>
```

문서상 다음 page token은 새 요청의 `sp` 값으로 전달한다. checkpoint에는 `provider=searchapi`, `engine=youtube`, `queryHash`, `spToken`, `page`, `schemaVersion`을 저장한다.

### 6.2 포함·제외 결과

수집 후보:

- top-level `videos[]`
- Shorts section의 video ID. 현재 공식 `search.list(type=video)`와 recall 차이를 줄이기 위해 포함하되 결과 종류를 `short`로 표시

제외:

- `ads[]`
- channel, playlist, post, company 및 추천용 비영상 section
- 영상 ID가 없거나 유효하지 않은 항목

같은 ID가 여러 section/page에 나타나면 처음 발견된 page/position은 보존하고 collection 대상은 한 번만 생성한다.

`keyword_search_results` additive 확장 후보:

- `provider`, `provider_result_kind`: `video`, `short`
- `provider_position`, `provider_section`
- `preview_title`, `preview_thumbnail_url`
- `provider_published_text`

검색 preview는 발견 증거이며 canonical video 데이터가 아니다. 제목, 게시 시각, duration, 통계는 공식 `videos.list` 결과를 우선한다.

### 6.3 기존 검색 옵션 호환성

현재 키워드 config에는 `publishedAfter`, `publishedBefore`, `regionCode`, `relevanceLanguage`, `order`가 있다. SearchAPI 문서는 임의 날짜 범위 parameter 대신 YouTube native filter를 나타내는 opaque `sp`만 문서화한다.

따라서 Phase 0에서 다음 mapping을 실제 요청으로 확정한다.

| 기존 옵션 | 계획 |
| --- | --- |
| `regionCode` | SearchAPI `gl`로 mapping |
| `relevanceLanguage` | `hl`과 의미가 다르므로 자동 동일시하지 않음; 미지원이면 coverage에 기록 |
| `order=relevance` | 기본 검색 또는 검증된 relevance token |
| `order=date` | 검증된 Upload date/sort token 사용 |
| `order=viewCount` | 검증된 view count token 사용 |
| `publishedAfter/Before` | 공식 `videos.list` exact `publishedAt`으로 후처리 |

임의 날짜 범위는 검색 전 filtering이 아니라 검색 후 filtering이므로 완전한 recall을 자동 보장할 수 없다.

- `order=date`이고 하한보다 오래된 영상만 연속으로 나타나는 것이 확인되면 안전한 중단 규칙을 적용한다.
- relevance/viewCount 정렬에서 날짜 범위를 함께 쓰면 page 예산 내 결과만 수집하고 coverage를 `limited`로 표시한다.
- 지원되지 않는 옵션 때문에 결과 의미가 바뀌는데도 조용히 무시하지 않는다. API/job warning과 coverage metadata에 이유를 남긴다.
- 공식 `search.list`로 자동 fallback하지 않는다.

### 6.4 SearchAPI 검색 전용 정보

SearchAPI 검색 결과가 제공하는 `position`, `live`, `extensions`, static/rich thumbnail, channel verification/thumbnail은 발견 근거와 UI preview에 유용하다. 다만 값의 안정성과 의미가 canonical YouTube resource와 다르므로 `videos` 본체보다 provider evidence에 둔다.

## 7. 공식 `videos.list` 보강과 댓글 유지

발견된 영상 ID는 중복 제거 후 최대 50개씩 기존 공식 `videos.list`로 조회한다.

유지 목적:

- 정확한 `snippet.publishedAt`
- channel ID, title, description
- duration, privacy, made-for-kids
- view/like/comment count snapshot
- 댓글 수집 대상과 coverage 계산

댓글 수집은 현재 구현을 그대로 유지한다.

- top-level: `commentThreads.list(order=time)`
- 부족한 reply: `comments.list(parentId=...)`
- 정확한 `publishedAt`, `updatedAt` 저장
- page/checkpoint transaction과 ID 기반 upsert 유지
- SearchAPI comments endpoint, 상대 댓글 시각, SearchAPI 댓글 전용 필드는 이번 계획에서 사용하지 않음

따라서 앞서 검토한 댓글의 추정 `published_at` schema도 추가하지 않는다.

## 8. 채널·키워드 소스별 즉시 재수집

이 기능은 자동 수집을 수동 방식으로 바꾸는 기능이 아니다. 채널·키워드는 등록된 상태에서 기본 6시간 예약 수집을 계속하며, `지금 재수집`은 사용자가 필요할 때 다음 예약을 기다리지 않고 동일한 refresh를 한 번 앞당겨 실행하는 보조 기능이다.

### 8.1 사용자 동작

채널 목록과 키워드 목록의 각 source 카드 또는 행에 `지금 재수집` 버튼을 둔다.

- 대상: 사용자가 소유한 `channel`, `keyword` source
- 제외: 직접 영상 source는 이번 범위에 포함하지 않음
- 자동 수집이 일시 중지된 source도 수동 재수집은 허용하되, pin을 자동으로 다시 켜지는 않음
- 삭제되었거나 접근 권한이 없는 source는 실행하지 않음
- 파괴적 작업이 아니므로 확인 modal은 기본적으로 띄우지 않음

클릭 직후 버튼 상태는 다음처럼 바뀐다.

```text
지금 재수집
-> 요청 중
-> 대기 중 / 수집 중
-> 완료 시 마지막 수집 시각 갱신
```

active job이 있으면 `이미 수집 중`으로 표시하고 그 job의 진행 상태를 연결한다. 여러 번 클릭해도 job이 중복 생성되지 않아야 한다.

### 8.2 API 계약

기존 `POST /v1/sources/{sourceId}/jobs`의 즉시 실행 능력은 재사용하되, 제품 의미를 명확히 하기 위해 다음 둘 중 하나로 contract를 확정한다.

권장안:

```http
POST /v1/sources/{sourceId}/refresh
Idempotency-Key: <client generated key>
```

request body는 비워 두거나 `reason=manual`만 받는다. 댓글 포함 여부, page 예산, 전체 수집 여부를 browser가 다시 계산하지 않고 source/target의 현재 canonical config에서 읽는다.

응답:

```json
{
  "sourceId": "...",
  "targetId": "...",
  "disposition": "queued | joined | successor_queued",
  "job": { "id": "...", "state": "queued" },
  "nextRunAt": "..."
}
```

- `queued`: 즉시 새 parent job 생성
- `joined`: 같은 target의 충분한 범위를 가진 queued/running/waiting job 재사용
- `successor_queued`: active job 범위가 부족하면 종료 후 실행할 successor 요청을 하나만 보장

대안은 기존 jobs endpoint에 `mode=manual_refresh`를 추가하는 것이다. 어느 쪽을 선택하든 repository의 target lock과 active-job dedup을 우회하는 별도 job 생성 코드를 만들지 않는다.

### 8.3 실행 범위

수동 재수집은 전체 과거 데이터를 다시 긁는 기능이 아니라 **지금 시점의 증분 refresh**다.

채널:

- SearchAPI `youtube_channel` profile 재조회
- `youtube_channel_videos` 최신 page부터 조회
- 신규/변경 영상 ID를 공식 `videos.list`로 보강
- source config가 댓글 수집을 포함하면 기존 공식 댓글 증분 수집 실행
- 신규 영상에만 기본 transcript 수집

키워드:

- SearchAPI `youtube`를 page 1부터 현재 config/page 예산으로 재검색
- 발견 영상 ID를 공식 `videos.list`로 보강하고 exact 날짜 조건 적용
- 신규/변경 영상의 기존 공식 댓글 증분 수집
- 신규 영상에만 기본 transcript 수집

`collectAllVideos`, `collectAllComments`가 설정된 source라도 수동 버튼이 매번 기존 데이터를 삭제하거나 전체 backfill을 처음부터 다시 하는 의미는 아니다. 저장된 coverage와 checkpoint를 활용하는 정상 refresh 정책을 따른다.

### 8.4 6시간 예약과의 관계

즉시 요청이 없어도 enabled source는 기존처럼 6시간마다 자동 수집된다. 사용자가 버튼을 누르면 `next_run_at`이 아직 남아 있어도 즉시 queued 상태가 되어 worker의 다음 polling 대상이 된다.

quota와 중복 수집을 막기 위한 권장 정책:

- 새 수동 job을 실제로 생성한 시각을 `last_dispatched_at`으로 기록
- pin이 enabled면 `next_run_at = manual_dispatched_at + interval_minutes`
- active job에 `joined`된 경우에는 이미 잡힌 예약 시각을 다시 밀지 않음
- pin이 disabled면 수동 job만 실행하고 `next_run_at`/enabled 상태를 변경하지 않음

즉, enabled source에서 즉시 재수집이 실제 dispatch되면 다음 자동 수집은 그 시점부터 약 6시간 후다. 자동 수집 자체는 계속 유지하면서, 즉시 실행 직후 원래 예약 job이 연달아 생기는 것만 막는다.

### 8.5 권한·동시성·오류

- source subscription owner만 요청 가능
- canonical target 단위로 하나의 active parent job만 허용
- 동일 `Idempotency-Key` replay는 같은 결과 반환
- SearchAPI 오류는 공식 discovery로 자동 fallback하지 않고 기존 provider retry 정책 적용
- 수동 요청 실패는 source의 자동 pin을 끄지 않음
- UI는 provider 오류, quota/credit 부족, 댓글 비활성 같은 partial warning을 job 상태에서 표시
- 수동 refresh 요청 횟수와 실제 새 job 생성 횟수를 별도로 계측해 연속 클릭과 비용을 관찰

## 9. 영상 대본 계획

대본은 SearchAPI.io `youtube_transcripts`를 사용하며 **신규 발견 영상**부터 수집한다. 기존 영상 전체 backfill은 별도 실행 승인을 받기 전 자동 생성하지 않는다.

### 9.1 언어 선택

1. `lang=ko`, `transcript_type=manual` 선호로 요청
2. 성공하면 한국어 저장
3. 한국어가 없으면 `available_languages` 확인
4. 영어 후보를 `en` -> `en-US` -> 정렬된 다른 `en-*` 순으로 선택
5. 한국어·영어가 모두 없으면 `unavailable`

`only_available=true`는 첫 available language로 임의 fallback할 수 있으므로 사용하지 않는다. 한국어→영어 순서를 애플리케이션에서 결정한다.

manual track이 없을 때 auto track으로 자동 fallback하는지는 Phase 0 contract probe로 확인한다. 번역된 한국어를 한국어 성공으로 인정할지도 정책으로 확정한다.

### 9.2 저장 모델

`video_transcripts`

- `video_id`, `provider`, `requested_language`, `resolved_language`
- `language_name`, `selection_reason`: `ko_preferred`, `en_fallback`
- `transcript_type`, `is_auto_generated`, `is_translated`
- `state`: `available`, `unavailable`, `retryable_error`, `failed`
- `full_text`, `content_hash`, `fetched_at`, `last_attempted_at`, 안전한 `error_code`

`video_transcript_segments`

- `transcript_id`, `sequence`, `start_ms`, `duration_ms`, `text`
- `UNIQUE(transcript_id, sequence)`
- `CHECK(start_ms >= 0 AND duration_ms >= 0)`

`start`, `duration` 초 단위 값은 integer millisecond로 정규화한다. 전체 segment 검증과 저장은 한 transaction으로 처리한다. 동일 content hash면 segment를 재작성하지 않고 freshness만 갱신한다.

### 9.3 제품 경계

- `GET /v1/videos/{videoId}/transcript` 조회 API 계획
- 기존 video visibility/구독 ACL 적용
- 대본 없음은 job 실패가 아니라 `state=unavailable`
- 1차 범위는 수집·저장·조회까지
- 검색 index, LLM 분석, bulk export는 저작권·보존·비용 정책 승인 후 별도 범위

## 10. provider client와 오류 정책

하나의 server-only SearchAPI client가 engine별 요청을 담당한다.

- GET 기본, channel video의 큰 token은 POST JSON body
- connect/read timeout
- response body size 제한
- 429/일시적 5xx: exponential backoff + jitter
- 인증 실패, account 비활성, credit exhaustion: 공식 discovery 자동 fallback 없이 운영 오류
- malformed response: 저장하지 않고 retry/실패
- token 오류: 제한된 횟수 후 해당 source의 page 1부터 idempotent 재시작
- key, Authorization header, page token, 대본 본문, 전체 upstream URL log 금지

댓글 endpoint 오류는 기존 YouTube 오류 분류를 계속 사용한다. provider별 waiting/retry 사유를 혼합하지 않는다.

## 11. 계측과 비용

SearchAPI.io 요청은 공식 YouTube quota ledger에 넣지 않고 별도 provider request log에 기록한다.

- provider, engine/operation, job/source/video ID
- HTTP status, 안전한 error code, attempt, latency
- page 수와 item 수
- keyword/channel/transcript 구분
- trigger: `scheduled`, `manual`; manual disposition과 source/target ID
- transcript 요청·해결 언어와 fallback 여부
- 본문과 token을 제외한 response schema version

운영 대시보드:

- 공식 `search.list`, `channels.list`, `playlistItems.list` 호출이 전환 후 0인지
- 공식 `videos.list`, `commentThreads.list`, `comments.list` 호출량이 예상 범위인지
- SearchAPI engine별 요청 수·오류율·latency·credit
- 키워드/채널별 발견 영상 수, 중복률, hydration 탈락률
- keyword limited coverage 비율
- transcript ko 성공률, en fallback률, unavailable률

## 12. 설정 후보

계획 단계의 환경 변수 후보이며 아직 `.env.example`에는 추가하지 않는다.

```text
DISCOVERY_PROVIDER=searchapi
SEARCHAPI_API_KEY=
SEARCHAPI_BASE_URL=https://www.searchapi.io/api/v1/search
SEARCHAPI_TIMEOUT_SECONDS=20
SEARCHAPI_GL=kr
SEARCHAPI_HL=ko
SEARCHAPI_ZERO_RETENTION=false
SEARCHAPI_CHANNEL_TOKEN_POST_THRESHOLD_BYTES=1800
TRANSCRIPT_COLLECTION_ENABLED=true
TRANSCRIPT_PRIMARY_LANGUAGE=ko
TRANSCRIPT_FALLBACK_LANGUAGE=en
TRANSCRIPT_TYPE_PREFERENCE=manual
TRANSCRIPT_MAX_RESPONSE_BYTES=<Phase 0에서 확정>
```

template의 secret은 공백으로 유지하고 worker에만 주입한다.

## 13. 구현 단계

### Phase 0. 실제 계약과 비용 확인

- 세 discovery API와 transcript API의 실제 계정 응답 fixture 확보
- channel ID/handle/legacy username/자유 텍스트 동작 확인
- keyword video/Shorts/광고/section schema 확인
- keyword `sp` filter와 next page token double-encoding 여부 확인
- channel videos 최신순 안정성과 큰 token POST 확인
- 404/429/credit exhaustion/5xx error schema 확인
- 요청당 credit와 예상 6시간 주기 비용 산정
- zero retention 및 vendor 보존 정책 검토

### Phase 1. provider client와 persistence expand

- SearchAPI client, secret redaction, GET/POST transport
- provider request log
- channel provider profile과 keyword discovery evidence additive migration
- transcript/segment additive migration
- provider별 checkpoint version

### Phase 2. 채널 discovery 전환

- channel resolve/profile을 `youtube_channel`로 전환
- uploads playlist 순회를 `youtube_channel_videos`로 전환
- 공식 `videos.list` hydration과 공식 댓글 경로 연결
- 기존 완료 채널의 전체 자동 backfill 없이 예약 refresh부터 적용

### Phase 3. 키워드 discovery 전환

- `youtube` 결과 parser와 video/Shorts dedup
- filter mapping과 limited coverage 표시
- keyword run/checkpoint를 SearchAPI `sp` token에 맞게 분리
- 공식 `videos.list` hydration 후 날짜 조건 적용

### Phase 4. 소스별 수동 재수집

- source-specific refresh API와 idempotency contract
- canonical config 기반 job scope 생성
- target active-job join/successor dedup
- 실제 수동 dispatch 시 enabled pin의 다음 예약 시각 재계산
- 채널·키워드 목록의 `지금 재수집` 버튼과 진행 상태 연결

### Phase 5. transcript 추가

- 신규 영상 ko 요청과 결정적 영어 fallback
- segment 검증·원자적 저장·조회 API
- 기존 영상 bulk backfill은 실행하지 않음

### Phase 6. canary와 전환

- 한국어 채널, 대형 채널, handle 변경 채널 canary
- relevance/date/viewCount 키워드와 날짜 범위 키워드 canary
- 한국어/영어/대본 없음 영상 canary
- 채널·키워드 수동 재수집, 연속 클릭, active-job join canary
- 6시간 예약 주기 1회 이상 관찰
- provider별 비용·오류·coverage 확인 후 전체 discovery 전환

## 14. 테스트 계획

### 단위 테스트

- channel/channel videos/keyword response mapping
- ads·channel·playlist·post 제외와 video/Shorts dedup
- SearchAPI pagination token과 provider mismatch 초기화
- 긴 channel token의 POST 전환과 secret/token redaction
- keyword filter mapping과 unsupported option warning
- `published_time`을 canonical timestamp로 저장하지 않는 계약
- transcript ko 성공, en fallback, unavailable routing
- transcript segment millisecond 정규화와 content hash
- manual refresh source type/owner validation과 idempotency key
- enabled/disabled pin의 다음 예약 시각 계산

### repository/worker 테스트

- discovery page와 checkpoint의 원자적 commit
- page replay 시 source-video 및 keyword result 중복 없음
- SearchAPI 장애 시 공식 `search/channels/playlistItems` 자동 호출 0
- 공식 `videos.list` 50개 batch hydration
- 댓글 수집이 기존 공식 `commentThreads/comments` client만 호출
- 신규 영상만 transcript 기본 수집
- 기존 영상 transcript bulk job이 migration으로 생성되지 않음
- 수동 refresh가 6시간을 기다리지 않고 queued되며 target별 active job을 중복 생성하지 않음
- manual dispatch 후 enabled pin만 `now + interval`로 이동하고 joined 요청은 다시 이동시키지 않음
- 수동 요청이 source canonical 댓글/page 설정을 그대로 사용함

### 통합/E2E 테스트

- channel ID/handle 등록 → SearchAPI 채널/영상 발견 → 공식 video hydration → 공식 댓글
- keyword 검색 → video/Shorts 발견 → exact date filtering → 공식 댓글
- 날짜 범위 + 비날짜 정렬의 `limited` coverage 표시
- 대형 채널 pagination과 crash resume
- ko transcript, en fallback, unavailable read API
- 기존 저장 영상·댓글 count 보존
- 채널·키워드 행별 버튼 → job 진행 → 완료 데이터 갱신 E2E
- 같은 버튼 연속 클릭과 서로 다른 사용자의 shared target 요청이 하나의 active job으로 수렴

## 15. rollout과 rollback

```text
schema expand
-> SearchAPI client 배포(아직 provider 미전환)
-> channel canary
-> keyword canary
-> source별 수동 재수집 canary
-> transcript canary
-> 예약 refresh 1회 관찰
-> DISCOVERY_PROVIDER=searchapi 전체 전환
```

rollback:

- 자동 fallback은 하지 않는다.
- 운영자가 명시적으로 discovery provider를 공식 API로 되돌릴 수 있다.
- provider가 바뀌면 SearchAPI token을 폐기하고 page 1부터 idempotent 재시작한다.
- transcript는 독립적으로 비활성화할 수 있다.
- additive schema와 이미 저장된 영상·댓글·대본은 삭제하지 않는다.

## 16. 완료 기준

- 키워드와 채널 영상 ID 발견은 SearchAPI.io만 사용한다.
- 공식 `search.list`, `channels.list`, `playlistItems.list` 호출이 전환 후 0이다.
- 영상 canonical metadata는 공식 `videos.list`로 정확하게 보강된다.
- 댓글과 답글은 기존 공식 API 경로와 exact timestamp를 유지한다.
- SearchAPI 상대 영상 시각이 canonical `published_at`을 덮어쓰지 않는다.
- 신규 영상 대본은 한국어 우선, 없으면 영어로 수집된다.
- provider별 checkpoint, 오류, 사용량, 비용이 분리된다.
- 기존 영상·댓글은 일괄 재수집·삭제 없이 보존된다.
- 6시간 예약 refresh에서 신규 채널 영상과 키워드 영상 발견을 실제로 검증한다.
- 채널·키워드는 별도 조작 없이 기본 6시간 자동 수집을 계속한다.
- 사용자는 `지금 재수집`으로 예약 시각과 무관하게 자동 refresh와 동일한 job을 한 번 즉시 요청할 수 있다.
- 반복 클릭과 shared target 동시 요청이 중복 active job이나 중복 successor를 만들지 않는다.
- 수동 dispatch 후 다음 자동 수집 시각이 일관되게 조정되고 disabled pin은 그대로 유지된다.

## 17. 운영 확인 및 후속 개선 항목

1. SearchAPI.io 실제 요금제, engine별 credit, rate limit
2. keyword `sp`의 relevance/date/viewCount 및 upload-date filter mapping
3. arbitrary `publishedAfter/Before`에서 허용할 page 예산과 limited coverage UX
4. keyword Shorts 포함 정책과 ranking 표시
5. 자유 텍스트/legacy username 채널 입력의 SearchAPI-only 해석 방식
6. channel videos 기본 최신순과 all-known page 중단의 안정성
7. 큰 channel pagination token의 POST 전환 임계값
8. `youtube_channel` 추가 metadata의 표시·보존 범위
9. 한국어 자동 번역을 한국어 성공으로 허용할지 여부
10. transcript manual/auto 선호, 최대 응답 크기, 기존 영상 backfill 별도 예산
11. 수동 refresh API를 전용 endpoint로 둘지 기존 jobs endpoint의 mode로 확장할지
12. active job 범위가 부족할 때 successor를 허용할지 기존 job 완료 후 다시 누르게 할지

현재 구현은 문서화된 기본 검색과 pagination token을 사용하고, 정확한 날짜 범위는 공식 `videos.list`의 `publishedAt`으로 후처리한다. `sp` 정렬 token과 임의 날짜 범위의 완전한 recall은 아직 운영 검증 항목이므로, 해당 조건에서는 설정한 page 예산 내 결과라는 한계가 있다. 나머지 항목도 실제 계정·채널 canary 결과를 근거로 조정하며, 댓글 provider는 이 검토 범위에 포함하지 않는다.
