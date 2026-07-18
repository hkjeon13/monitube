# Monitube 데이터 조회 성능 최적화 계획

작성일: 2026-07-18
검토 반영일: 2026-07-18
대상 환경: `server-4096` 단일 서버 Docker Compose 운영 환경

## 1. 결론

현재 단계에서는 PostgreSQL을 다른 데이터베이스로 교체하지 않는다.

댓글 약 213만 건과 전체 DB 약 1.68GB는 PostgreSQL이 충분히 처리할 수 있는 규모다. 현재 지연의 주원인은 standalone PostgreSQL 자체가 아니라 다음 애플리케이션 동작이다.

1. `/sources/{source_id}/results`가 응답에 포함하지 않는 댓글 원문 전체를 읽는다.
2. API가 읽은 댓글을 Python 객체로 변환해 매 요청마다 요약을 다시 계산한다.
3. 수집 중 웹이 무거운 결과 API와 source 목록을 5초마다 호출하며 요청이 중첩될 수 있다.
4. Explore와 검색 API가 큰 범위를 반복 집계하거나 선행 와일드카드 검색을 수행한다.
5. 댓글 저장이 단건 연결·조회·upsert·commit을 반복한다.
6. PostgreSQL이 서버 자원에 비해 작은 기본 메모리 설정으로 실행 중이다.

최적화 순서는 다음으로 확정한다.

```text
긴급 요청 증폭 제거
→ 계측 bootstrap
→ 새 summary/video API 도입
→ batch comment 저장과 connection pool
→ rollup dual-write/backfill/read 전환
→ Explore/cache와 검색 최적화
→ PostgreSQL·API 용량 조정
```

## 2. 현재 기준선과 해석 범위

2026-07-18 원격 서버에서 확인한 값이다.

| 항목 | 현재 값 |
| --- | ---: |
| CPU | 24 logical CPUs, Intel i7-13700K |
| 메모리 | 62GiB, available 약 48GiB |
| PostgreSQL DB 크기 | 약 1.68GB |
| 댓글 수 | 약 2,128,000건 |
| 영상 수 | 약 8,879건 |
| `comments` 총 크기 | 약 1.6GB, 인덱스 포함 |
| PostgreSQL 볼륨 | NVMe의 `/var/lib/docker` |
| NVMe 여유 공간 | 약 108GB, 사용률 88% |
| `shared_buffers` | 128MB |
| `effective_cache_size` | 4GB |
| `work_mem` | 4MB |
| 관측된 DB cache hit | 약 20% |
| PostgreSQL 누적 temp bytes | 약 1,057GB |

느린 조회 중 API 컨테이너는 CPU 한 코어 이상과 약 2GB 이상의 메모리를 사용했다. 같은 시점에 PostgreSQL은 전체 댓글 결과를 API로 전송하면서 `ClientWrite` 또는 `MessageQueueSend`에서 대기했다.

cache hit와 temp bytes는 통계 reset 시각이 기록되지 않은 누적값이다. 변경 전후 비교에는 그대로 사용하지 않고, 0B 단계에서 reset 시각과 관측 기간을 명시한 delta를 새로 측정한다. 전체 cache hit 95%를 단독 성공 기준으로 사용하지 않으며 route latency, 읽은 row 수, temp/WAL 증가율과 함께 판단한다.

## 3. 목표와 완료 기준

### 3.1 사용자 체감 목표

- 수집 중에도 작업 상태와 기존 결과 화면이 끊기지 않는다.
- 결과 화면 응답 시간이 전체 댓글 수에 비례해 증가하지 않는다.
- Explore 다음 페이지가 채널 전체 통계를 다시 계산하지 않는다.
- 댓글·영상 검색이 전체 댓글을 API로 전달하지 않는다.
- 여러 채널·키워드 작업이 동시에 진행돼도 UI 요청이 중첩되지 않는다.

### 3.2 성능 목표

0B 단계에서 고정된 workload와 측정 방법으로 기준선을 다시 잡은 뒤 아래 목표를 적용한다.

| 경로 | warm p95 목표 | 추가 기준 |
| --- | ---: | --- |
| source overview | 500ms 이하 | 댓글 원문 hydration 0건 |
| source video page | 500ms 이하 | 최대 100건, keyset pagination |
| Explore first page | 750ms 이하 | 사용자 scope가 적용된 집계 |
| Explore next page | 500ms 이하 | channel 집계 재실행 없음 |
| 3자 이상 contains 검색 | 1초 이하 | ACL 적용 후 후보 최대 300건 |
| 2자 검색 | 별도 기준 | ID/title/handle prefix-only, 1자는 422 |

동시 overview 조회 10건에서 오류율 1% 미만, API RSS가 workload 종료 후 안정 상태로 회복되고 지속적으로 1GB씩 증가하지 않아야 한다. 절대 RSS 상한은 API process 수와 container memory budget을 확정한 뒤 정한다.

### 3.3 데이터·보안 완료 기준

- 배포 전후 영상 수, 댓글 수, 최신 댓글 시각이 일치한다.
- rollup과 원본 집계의 오차가 0이다.
- 사용자 A가 사용자 B만 구독한 target/video/집계/search candidate를 볼 수 없다.
- cursor와 Redis key는 owner scope와 filter를 교차해 재사용할 수 없다.
- PostgreSQL, Redis 장애가 raw credential이나 댓글 원문을 로그에 남기지 않는다.

## 4. 범위와 비범위

### 4.1 포함

- 결과 조회와 웹 폴링 개선
- summary freshness와 data version 계약
- 영상·댓글 목록 pagination
- 댓글 batch upsert와 DB connection pool
- 영상별 댓글 rollup과 resumable backfill
- Explore 쿼리 분리 및 Redis cache
- PostgreSQL 검색 인덱스와 후보 제한
- PostgreSQL runtime 설정과 API process 조정
- 계측, 부하 검증, 배포, 자동 rollback 준비

### 4.2 제외

- PostgreSQL을 SQLite 또는 DuckDB로 이전
- 1차 범위에서 ClickHouse 또는 OpenSearch를 필수 운영 구성으로 추가
- 수집 데이터 삭제 또는 보존 기간 변경
- YouTube API key·quota 정책 변경
- 검색 결과 품질을 바꾸는 형태소 분석기 도입

## 5. 확정 아키텍처 계약

### 5.1 source of truth

- 영상, 댓글, job, subscription과 권한의 source of truth는 PostgreSQL이다.
- Redis는 재생성 가능한 파생 cache로만 사용한다.
- rollup과 analysis result는 원본에서 재구축 가능한 파생 데이터다.
- 외부 검색 엔진 또는 ClickHouse를 도입하더라도 PostgreSQL을 대체하지 않고 파생 index/store로만 사용한다.

### 5.2 summary identity와 freshness

공유 canonical target을 기준으로 summary를 생성한다. 브라우저의 subscription ID와 내부 worker source ID를 target-backed cache identity로 사용하지 않는다. `target_id IS NULL`인 legacy source만 `source_id`를 fallback scope로 사용하며 target-backed 결과와 섞지 않는다.

`analysis_runs`를 다음 방향으로 확장한다.

- `target_id`: summary 대상 canonical target
- `job_id`: summary가 반영한 parent collection job
- `data_version`: target 단위 단조 증가 version
- `state`: `queued`, `running`, `completed`, `failed`, `cancelled`
- `sample_plan`, `coverage`, `pipeline_version`
- `lease_owner`, `lease_expires_at`, `retry_count`, `resume_at`, `last_error`

`collection_targets`와 targetless legacy용 `collection_sources`에는 `data_version BIGINT NOT NULL DEFAULT 0`을 추가한다. parent job이 terminal이 될 때 그 job이 처리한 영상들을 포함하는 모든 target ID를 조회하고 UUID 순서로 잠근 뒤 version을 한 번 증가시킨다. targetless legacy는 해당 source version만 증가시킨다. 성공뿐 아니라 warning/failed/cancelled terminal도 committed partial data가 있으면 version을 증가시킨다. summary와 Redis key는 이 version을 사용한다. 다른 target을 통해 같은 공개 영상의 댓글이 갱신돼도 해당 영상을 포함하는 모든 target summary가 stale로 판정돼야 한다.

parent terminal transaction은 `job terminal 전환 → 영향 target version 증가 → completed/warning이면 analysis_runs queued 생성`을 원자적으로 수행한다. 브라우저가 terminal 상태를 관측했는데 해당 version의 summary 작업 자체가 존재하지 않는 상태를 만들지 않는다.

latest summary 조회는 `target_id`, `result_kind`, completed 상태, `data_version DESC` 기준으로 결정한다. `analysis_results.deleted_at IS NULL`, `expires_at IS NULL OR expires_at > now()` 조건을 적용한다. 동일 target/version/kind의 중복 completed 결과를 방지하는 unique constraint 또는 idempotency key를 둔다.

기존 `save_analysis_summary() → get_source_results()` 의존을 제거하고 결과 read path와 독립된 `recompute_target_summary(target_id, job_id, data_version)` producer를 만든다. 새 producer와 summary write/backfill을 먼저 배포한 뒤 cached read를 켠다. read path가 summary 재계산을 직접 예약할 때도 동일 target/version의 queued/running run이 있으면 중복 생성하지 않는다.

analysis worker 1개가 `FOR UPDATE SKIP LOCKED`와 lease를 사용해 queued/due-retry run을 claim한다. timeout 또는 process 종료 후 만료된 lease를 reclaim하며, 동일 target/version/pipeline에는 한 run만 존재하도록 unique constraint를 둔다. retry 한도 초과 시 run을 failed로 남기고 이전 completed summary를 유지한다.

Compose에는 collection worker와 분리된 `analysis-worker` service를 기본 1 replica로 추가한다. 분석 지연이 collection queue claim과 YouTube lease 갱신을 막지 않도록 두 queue를 섞지 않는다.

API는 다음 상태를 반환한다.

- `fresh`: 최신 completed parent job까지 반영
- `stale`: 이전 completed summary를 반환 중
- `building`: exact summary 또는 top words 생성 중
- `failed`: 최신 생성이 실패해 이전 summary 또는 SQL fallback 사용 중

응답에는 `generatedAt`, `asOfJobId`, `dataVersion`, `status`를 포함한다. summary 생성 실패는 collection parent job을 실패시키지 않는다.

parent job 결과와 파생 summary의 의미는 다음과 같다.

| parent 상태 | 새로 저장된 video/comment 공개 | exact count/time | top words |
| --- | --- | --- | --- |
| `completed` | 공개 | 최신 rollup, 해당 data version | 비동기 재계산, 완료 전 이전 값과 `building` |
| `completed_with_warnings` | 공개 | 저장된 범위 기준, coverage에 경고 기록 | bounded sample 재계산 가능, `partial` 표시 |
| `failed` | 현재 저장 정책대로 부분 공개 | live rollup 값과 `partialData=true` | 이전 completed 값 유지, `stale` |
| `cancelled` | 취소 전 commit된 데이터만 공개 | live rollup 값과 `partialData=true` | 이전 completed 값 유지, `stale` |

성공 parent가 terminal로 관측된 뒤 exact count/time은 반드시 그 job의 committed child 결과를 포함해야 한다. top words freshness는 별도 `topWordsStatus`로 표시해 exact summary 상태와 혼동하지 않는다.

exact count와 최신 시각은 SQL/rollup으로 계산한다. top words는 요청 경로에서 계산하지 않는다. 1차 전환에서는 기존 top words를 stale 값으로 유지하거나 빈 배열과 `building` 상태를 반환한다. 이후 별도 analysis run이 bounded sample을 처리한다.

top words sample은 전체 댓글을 Python 메모리에 올리지 않고 다음 원칙을 따른다.

- target 전체에서 최대 표본 수를 설정한다.
- 특정 대형 영상이 표본을 독점하지 않도록 영상별 상한을 둔다.
- server-side cursor 또는 SQL page로 streaming한다.
- `analysis_runs.sample_plan`과 `coverage`에 표본 방법과 비율을 기록한다.
- 새 summary가 완성되기 전에는 이전 completed summary를 계속 제공한다.
- 동일 target/version의 analysis run은 최대 한 개만 queued/running 상태가 될 수 있다.
- analysis worker의 RSS, 읽은 row 수, 실행 시간 상한을 두고 초과 시 이전 top words를 유지한다.

### 5.3 pagination과 cursor

기존 `/sources/{id}/results`의 `videos`를 첫 60건으로 조용히 축소하지 않는다. 다음 additive API를 먼저 배포한다.

- `GET /v1/sources/{id}/overview`: source, latest parent job, summary, server-side top videos
- `GET /v1/sources/{id}/videos?limit=&cursor=`: paginated video list
- 기존 `/results`: 전환 기간 유지 후 usage 확인과 deprecation 공지 뒤 제거 또는 compatibility wrapper로 축소

영상 정렬 tuple은 다음으로 확정한다.

```text
effective_published_at = COALESCE(published_at, source_fetched_at, 'epoch')
ORDER BY effective_published_at DESC, youtube_video_id DESC
```

index도 동일한 expression과 tie-breaker를 사용한다. cursor payload에는 다음을 포함한다.

- cursor version
- effective timestamp
- YouTube video ID
- snapshot timestamp
- target/source scope
- normalized filter hash와 sort

cursor는 불투명하게 encode하고 서버가 version, sort, filter, target과 현재 owner 접근 권한을 다시 검증한다. cursor 자체를 권한 증명으로 신뢰하지 않는다. target membership의 `first_seen_at <= snapshot_at` 조건을 사용해 한 pagination session에서 새 영상 유입으로 인한 중복을 줄인다.

기존 UI가 전체 영상으로 계산하던 top videos는 `overview`에서 서버가 전체 scope를 대상으로 계산한다. 첫 page의 영상만으로 계산하지 않는다.

web에는 video page의 `nextCursor`를 사용하는 더 보기 또는 infinite scroll을 먼저 구현한다. `analysis.videoCount`는 전체 target 영상 수이고 `videos.length`는 현재 page의 수라는 계약을 TypeScript/API schema에 명시한다.

`/videos/{id}/comments` unbounded endpoint는 usage를 계측한 뒤 paginated comment-thread endpoint로 client를 전환한다. 전환 후 limit 없는 호출을 금지하고 deprecation 일정을 문서화한다.

### 5.4 rollup 의미와 사용자 scope

`video_comment_rollups`는 전역 영상 단위 사실만 저장한다.

- `stored_count`: 현재 동작과 호환되는 persisted comment row 수
- `top_level_count`: `youtube_parent_comment_id IS NULL`
- `reply_count`: `youtube_parent_comment_id IS NOT NULL`
- `latest_published_at`: `MAX(COALESCE(published_at, source_fetched_at))`
- `stored_count = top_level_count + reply_count`

soft delete 정책은 이번 범위에서 바꾸지 않는다. 현재 조회가 포함하는 row와 동일한 의미를 유지하고, 삭제 정책 변경은 별도 migration으로 다룬다.

Explore의 사용자별 수치는 전역 channel count를 그대로 노출하지 않는다. 다음 순서로 계산한다.

```text
owner의 enabled subscription
→ target membership에서 DISTINCT visible video_id
→ video_comment_rollups join
→ channel별 합산
```

한 영상이 여러 target에 속해도 사용자 화면에서는 한 번만 집계한다.

### 5.5 feature flags

다음 flag를 server-side 환경 설정으로 둔다. 기본값은 false이며 rollback 시 rebuild 없이 되돌릴 수 있어야 한다.

- `ENABLE_SOURCE_OVERVIEW_V2`
- `ENABLE_TARGET_SUMMARY_WRITE`
- `ENABLE_TARGET_SUMMARY_READ`
- `ENABLE_ANALYSIS_WORKER`
- `ENABLE_VIDEO_KEYSET_PAGINATION`
- `ENABLE_COMMENT_BATCH_WRITE`
- `ENABLE_COMMENT_ROLLUP_DUAL_WRITE`
- `ENABLE_COMMENT_ROLLUP_READ`
- `ENABLE_EXPLORE_ROLLUP`
- `ENABLE_SEARCH_TRIGRAM`
- `ENABLE_REDIS_DERIVED_CACHE`

schema 추가는 flag보다 먼저 배포하고, read flag는 backfill/reconciliation 완료 후에만 켠다.

## 6. 단계별 실행 계획

### 6.0A 긴급 요청 증폭 완화

현재 운영 부하를 가장 먼저 낮추는 독립 hotfix다. schema 변경 없이 배포한다.

#### 작업

- 수집 중 5초 polling에서 `/results`와 Explore 갱신을 제거한다.
- 현재 `listSources()` 5초 polling도 제거한다.
- `GET /v1/jobs/active`를 추가해 현재 사용자가 접근 가능한 active parent job만 한 번에 반환한다. child video job은 UI polling 목록에 직접 노출하지 않는다.
- `setInterval` 대신 한 요청이 완료된 뒤 다음 요청을 예약하는 recursive `setTimeout`을 사용한다.
- in-flight guard와 `AbortController`를 적용한다.
- 탭이 hidden이면 polling을 30초 이상으로 낮춘다.
- 네트워크 오류에는 exponential backoff와 jitter를 적용한다.
- parent job이 terminal로 처음 바뀔 때 overview, video first page, source list, Explore를 한 번만 invalidate한다.
- child video job 완료마다 Explore cache를 지우지 않는다.
- `waiting_quota`와 `waiting_retry`는 `resumeAt`을 기준으로 polling 간격을 늘린다.

오류별 동작을 다음으로 고정한다.

- 401: polling 중지, 인증 상태 초기화, 로그인 화면 전환
- 404: 해당 job/source polling 중지, 로컬 선택 상태 정리
- 429/5xx/network: exponential backoff와 jitter 후 재시도
- abort: 사용자 오류로 표시하지 않음
- tab이 다시 visible이 되면 즉시 한 번 갱신한 뒤 정상 간격 복귀

#### repository 보완

`list_sources()`의 subscription별 N+1 조회를 단일 set-based query와 latest-parent-job lateral join으로 바꾼다. target과 legacy source 분기는 `OR` 한 개에 의존하지 않고 각각 index를 사용할 수 있는 query/UNION으로 분리한다.

#### 완료 조건

- 한 browser tab에서 동일 job status 요청이 중첩되지 않는다.
- active job 중 `/results` 반복 호출이 access log에서 사라진다.
- source 목록 조회 횟수가 job 수나 subscription 수에 비례해 증가하지 않는다.

### 6.0B 계측 bootstrap

`pg_stat_statements`는 이후 PostgreSQL tuning 단계가 아니라 기준선 측정 전에 활성화한다.

#### 적용 순서

1. 기존 PostgreSQL 설정과 이미지/commit SHA를 기록한다.
2. 기존 컨테이너 상태에서 backup과 restore preflight를 수행한다.
3. `shared_preload_libraries=pg_stat_statements`, `track_io_timing=on`을 적용한다.
4. PostgreSQL을 재시작하고 readiness를 확인한다.
5. `CREATE EXTENSION IF NOT EXISTS pg_stat_statements`를 migration owner로 실행한다.
6. 통계 reset 시각을 기록하고 고정 workload로 기준선을 수집한다.

#### 수집 항목

- route template별 latency p50/p95/p99, error, response bytes
- normalized query ID별 calls, total/mean time, rows, temp blocks
- DB connection/pool wait, active/idle-in-transaction 수
- API/PostgreSQL/worker CPU와 RSS
- cache hit/miss, Redis timeout
- temp bytes, WAL bytes, checkpoint rate
- lock wait, deadlock, autovacuum, dead tuple/bloat
- queue depth, oldest queued age, expired lease, worker heartbeat
- NVMe 사용률과 증가 속도

`log_min_duration_statement`는 초기 500ms, `log_temp_files`는 초기 64MB로 두고 로그량을 확인한다. raw URL ID, 검색어, 댓글 원문을 metric label이나 구조화 로그에 넣지 않는다.

#### health endpoint

- `/health`: process liveness만 확인
- `/ready`: DB 연결, 필수 migration version, pool acquire를 확인
- Redis는 optional derived cache이므로 장애 상태를 표시하되 API readiness를 실패시키지 않는다.

### 6.1 source overview와 video pagination

#### summary schema와 write path

- `collection_targets.data_version`, `collection_sources.data_version`과 `analysis_runs.target_id/job_id/data_version/lease/retry` column을 expand migration으로 추가한다.
- 기존 analysis run은 `analysis_runs.source_id → collection_sources.target_id`로 backfill한다.
- target별 최신 completed `basic_summary`만 version 0의 stale seed로 승격하고 중복 legacy run은 보존하되 read 후보에서 제외한다.
- latest completed 조회 index와 target/version/pipeline idempotency constraint를 추가한다. targetless legacy는 source/version/pipeline partial unique constraint를 별도로 사용한다.
- parent terminal transaction의 version 증가와 queued analysis run 생성을 먼저 배포한다.
- 기존 target 전체에 exact summary run을 한 번 enqueue하고 완료/실패를 검증한 뒤 summary read flag를 켠다.

#### 새 read path

`get_source_results()`의 댓글 전체 hydration을 신규 overview 경로에서 사용하지 않는다.

```text
subscription/target 권한 확인
→ latest parent job
→ latest completed target summary 또는 exact SQL fallback
→ server-side top videos
→ response
```

fallback은 `COUNT`, `MAX` 같은 DB aggregate만 수행하고 댓글 text를 반환하지 않는다. top words가 준비되지 않았으면 이전 값 또는 빈 배열과 상태를 반환한다.

#### 배포 순서

1. API에 overview/video page endpoint를 additive하게 배포한다.
2. target-aware summary producer와 기존 summary backfill을 배포한다.
3. latest summary의 freshness와 target mapping을 검증한다.
4. API contract test와 owner scope test를 통과한다.
5. web의 load-more/topVideos 처리를 새 endpoint로 전환한다.
6. access log에서 기존 `/results` client가 남아 있는지 확인한다.
7. deprecation 기간 뒤 기존 endpoint를 제한하거나 제거한다.

#### index

target/source membership과 다음 정렬을 함께 지원하는 index를 `EXPLAIN (ANALYZE, BUFFERS)` 후 확정한다.

```sql
CREATE INDEX videos_effective_published_idx
ON videos (
  COALESCE(published_at, source_fetched_at, 'epoch'::timestamptz) DESC,
  youtube_video_id DESC
);
```

membership join이 먼저 선택되는 경우 target/source membership의 `video_id`, `first_seen_at` index를 함께 검토한다.

#### 완료 조건

- overview 실행 시 comment text row가 API로 전달되지 않는다.
- pagination 중 중복·누락·다른 filter cursor 재사용이 없다.
- 기존 UI의 top video 의미가 유지된다.
- API 메모리가 target의 전체 댓글 수에 비례해 증가하지 않는다.

### 6.2 connection pool과 comment page batch 저장

rollup dual-write보다 먼저 구현한다.

#### connection budget

다음 공식을 사용한다.

```text
API process × API pool max
+ worker replica × worker pool max
+ analysis process × analysis pool max
+ migration/admin reserve
≤ PostgreSQL max_connections의 70~80%
```

초기 후보는 다음과 같으며 부하 테스트 후 확정한다.

- API process 1~2, process당 pool 4~8
- collection worker 2개, worker당 pool 2
- analysis worker 1개, pool 2
- migration/admin reserve 10
- PostgreSQL `max_connections` 초기 후보 60

`PostgresRepository`와 `AuthStore`가 process 내부 pool을 공유하도록 한다. `psycopg_pool`을 명시적 dependency로 추가하고 FastAPI lifespan 또는 worker process 시작 후 pool을 생성한다. fork 이전 전역 pool은 금지한다.

pool acquire timeout 시 무제한 대기하지 않고 API는 503과 retryable 응답을 반환한다. pool wait, timeout, checked-out 수를 계측한다. 반환된 connection은 rollback/reset을 보장하고 DB 재시작 후 stale connection 회복을 테스트한다.

#### batch 저장 계약

새 `persist_comment_page()` repository method가 한 YouTube response page를 한 transaction으로 저장한다.

lock 순서는 모든 writer/backfill에서 다음으로 통일한다.

```text
video row FOR UPDATE
→ existing comment IDs batch 조회
→ top-level comments batch upsert
→ parent map 생성
→ replies batch upsert
→ rollup 갱신(dual-write flag가 켜진 경우)
→ job checkpoint/progress
→ commit
```

실제 새로 insert된 ID와 기존 update ID를 구분해 rollup delta 중복을 막는다. parent 관계가 변경되거나 삭제 의미가 도입되면 해당 영상 rollup을 transaction 안에서 absolute recompute한다.

target `data_version` 증가는 comment page transaction마다 수행하지 않는다. parent terminal 처리에서 해당 parent/child가 다룬 distinct video ID를 기준으로 영향 target을 한 번에 invalidation해 두 collection worker가 같은 target row를 매 page마다 직렬화하지 않도록 한다.

#### 종료·lease 계약

- batch 크기와 statement timeout이 worker lease 갱신 주기보다 짧아야 한다.
- `stop_grace_period`는 YouTube timeout, 최대 DB transaction, lease 정리 시간을 합한 값보다 크게 둔다. 초기 후보는 worker 180초, API 30초다.
- SIGKILL 후 같은 page 재실행이 중복 row나 rollup 증가를 만들지 않는지 테스트한다.

### 6.3 video comment rollup expand/backfill/cutover

#### schema

```sql
CREATE TABLE video_comment_rollups (
  video_id UUID PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
  stored_count BIGINT NOT NULL DEFAULT 0 CHECK (stored_count >= 0),
  top_level_count BIGINT NOT NULL DEFAULT 0 CHECK (top_level_count >= 0),
  reply_count BIGINT NOT NULL DEFAULT 0 CHECK (reply_count >= 0),
  latest_published_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_reconciled_at TIMESTAMPTZ,
  CHECK (stored_count = top_level_count + reply_count)
);

CREATE TABLE maintenance_backfills (
  name TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  cursor TEXT,
  processed BIGINT NOT NULL DEFAULT 0,
  total BIGINT,
  last_error TEXT,
  started_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
```

#### 상태 머신

```text
schema_ready
→ dual_write_enabled
→ backfill_running
→ reconciling
→ ready
→ read_enabled
```

1. expand-only schema를 배포한다.
2. batch writer의 dual-write를 켜고 read는 기존 경로를 유지한다.
3. backfill은 `video.id` 순서와 durable cursor로 재개 가능하게 실행한다.
4. writer와 backfill 모두 동일한 video row lock을 먼저 획득한다.
5. backfill은 한 영상의 absolute aggregate를 계산해 upsert한다.
6. batch 크기, sleep, lock timeout을 환경값으로 두고 수집 latency가 상승하면 속도를 낮춘다.
7. 전체 영상에 대해 reconciliation을 수행한다.
8. mismatch 0, missing rollup 0, backfill state `ready`일 때만 read flag를 켠다.

대용량 backfill을 schema migration의 단일 transaction 안에서 실행하지 않는다. 별도 command/script로 실행하며 중단 후 cursor에서 재개한다. backfill/index 후 `ANALYZE`를 실행한다.

#### 완료 조건

- 원본과 rollup count/latest timestamp의 mismatch가 0이다.
- concurrent collection 중 backfill을 반복해도 lost update가 없다.
- read flag rollback 시 기존 원본 aggregate가 동작한다.

### 6.4 Explore 분리와 Redis derived cache

#### API 분리

- `/explore/channels`: owner-visible distinct videos를 기준으로 channel summary 반환
- `/explore/videos`: keyset-paginated video page와 `nextCursor` 반환
- first page 이후 channel summary를 재계산하지 않는다.
- video cursor는 effective published time, source fetched time, unique ID와 filter/snapshot을 encode한다.

기존 `/v1/explore`의 `channels + videos + nextOffset` 계약은 전환 기간 유지한다. 새 endpoints를 먼저 배포하고 web을 전환한 뒤 access log에서 legacy client가 사라진 것을 확인하고 offset 계약을 deprecate한다. 잘못된 cursor는 400, 다른 filter/scope의 cursor는 400, 접근 권한이 사라진 target은 404로 통일한다.

#### Redis 계약

cache key 예시는 다음 차원을 모두 포함한다.

```text
monitube:v1:owner:{owner_id}:explore:{normalized_filter_hash}:data:{generation}
monitube:v1:target:{target_id}:summary:{data_version}
```

- TTL 30~60초에 jitter를 더한다.
- 동일 miss의 동시 재계산은 short lock/single-flight로 합친다.
- connect/read timeout을 짧게 두고 실패 시 PostgreSQL로 fail-open한다.
- commit 이전에는 invalidate하지 않는다.
- parent terminal, unsubscribe, source delete 후 관련 generation을 올리거나 key를 삭제한다.
- 댓글 원문과 credential은 cache에 저장하지 않는다.
- subscription DTO와 ACL 판정 결과는 cache하지 않는다. cache hit 전에도 현재 subscription 접근 권한을 확인한다.
- subscription pause는 현재 제품의 기존 데이터 접근 의미를 보존하고, subscription 삭제는 즉시 접근을 제거한다.
- 현재 Redis의 AOF 사용 목적을 확인한다. derived cache 전용이면 AOF를 끄거나 별도 cache instance/DB를 사용한다.
- `maxmemory`와 `allkeys-lru` 또는 검증된 eviction 정책을 설정한다.

Redis Python client dependency, serialization version, cache hit/miss/error metric을 함께 추가한다. cache가 없어도 정확성과 권한 검사가 유지되어야 한다.

### 6.5 검색과 조회 index

#### 검색 정책

- 3자 이상 comment/video contains 검색: `pg_trgm` GIN 사용
- 2자: YouTube ID, channel handle, title prefix-only
- 1자 이하는 기존 계약대로 422 반환
- 1~2자 comment contains 검색은 지원하지 않는다.
- 외부 검색 엔진 도입 전까지 이 계약을 UI에 명시한다.

후보 `LIMIT` 전에 owner-visible target membership을 적용한다. 미인가 row가 후보를 밀어내거나 timing 차이로 존재를 노출하지 않아야 한다.

#### search document

- comment: normalized `search_text` column과 GIN trigram index
- video: `video_search_documents(video_id PK, document, updated_at)` table
- document에는 video title/description과 channel title/handle의 normalize된 값만 포함
- video/channel metadata upsert transaction 이후 search document를 idempotent하게 갱신
- backfill과 reconciliation cursor 제공

DB 후보는 최대 100~300건으로 제한하고 최종 20~50건에만 Python fuzzy ranking을 적용한다. recall과 기존 relevance 차이를 golden query set으로 검증한다.

golden query set에는 한글/영문/오타/ID를 포함하고 기존 top-20 대비 recall 90% 이상을 초기 gate로 둔다. `scope`, `score`, `matchedFields`, tie-break 순서의 API 의미를 유지한다.

#### 우선 index 후보

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX comments_author_recent_idx
ON comments (
  author_channel_id,
  published_at DESC NULLS LAST,
  source_fetched_at DESC
)
WHERE author_channel_id IS NOT NULL;

CREATE INDEX comments_parent_effective_published_idx
ON comments (
  youtube_parent_comment_id,
  COALESCE(published_at, source_fetched_at, 'epoch'::timestamptz),
  youtube_comment_id
)
WHERE youtube_parent_comment_id IS NOT NULL;

CREATE INDEX comments_search_text_trgm_idx
ON comments USING gin (search_text gin_trgm_ops)
WHERE search_text IS NOT NULL;

CREATE INDEX videos_channel_fetched_idx
ON videos (channel_id, source_fetched_at DESC);

CREATE INDEX sync_jobs_source_created_idx
ON sync_jobs (source_id, created_at DESC);
```

실제 생성 전 existing index와 중복 여부, query plan, 예상 index 크기, 생성 중 temp/WAL 최악치를 확인한다.

현재 migration runner는 single transaction이므로 `CREATE INDEX CONCURRENTLY`를 지원하지 않는다. 기본 선택은 maintenance window에 API/worker를 중지하고 일반 index를 생성하는 것이다. concurrent build가 필요해지면 non-transactional migration type을 runner에 명시적으로 추가한다.

### 6.6 PostgreSQL와 API 용량 조정

P0 read/write 구조 개선 후 적용한다.

#### 메모리 예산 선행 조건

24시간 peak 기준으로 다음을 표로 기록한다.

- API process 합계 RSS
- collection/analysis worker RSS
- PostgreSQL shared memory와 connection work memory 상한
- Redis/MinIO/기타 server workload
- OS page cache와 최소 16GB 안전 여유

`work_mem` 잠재 사용량은 `connection × sort/hash node × parallel worker`로 계산한다. container memory limit 또는 경보 기준 없이 `shared_buffers`만 8GB로 올리지 않는다.

#### 초기 후보값

```text
shared_buffers=4GB
effective_cache_size=32GB
work_mem=8MB
maintenance_work_mem=512MB
autovacuum_work_mem=256MB
max_connections=60
random_page_cost=1.1
effective_io_concurrency=200
max_wal_size=8GB
checkpoint_completion_target=0.9
wal_compression=on
track_io_timing=on
idle_in_transaction_session_timeout=30s
```

controlled migration/index session만 `SET LOCAL maintenance_work_mem` 또는 별도 값을 사용한다. API와 worker는 서로 다른 statement/lock timeout을 적용한다. API 초기 후보는 statement 5초, lock 2초이며 collection batch는 lease 예산 안의 더 긴 timeout을 사용한다.

Compose에는 PostgreSQL `shm_size` 초기 1GB와 service별 메모리 경보/제한을 검토한다. Uvicorn process는 1개에서 기준선을 잡고 2개로 올린 뒤 pool/CPU/RSS를 다시 측정한다.

#### 디스크 gate

NVMe 사용률이 현재 88%이므로 다음 조건을 충족해야 index/backfill을 시작한다.

- 예상 index + temp + WAL의 2배보다 큰 작업 공간
- 최소 10% 절대 여유와 프로젝트가 정한 20% 목표 중 더 엄격한 기준 검토
- 90% 도달 또는 WAL/temp 증가가 예상치를 초과하면 작업 중단
- Docker image/build cache와 PostgreSQL data/WAL을 별도 측정
- backup은 `/data`에 저장하되 off-host 사본을 별도로 유지

PostgreSQL data directory를 HDD로 이동하지 않는다.

### 6.7 목록 endpoint 전환 목록

목록 성격의 endpoint를 빠뜨리지 않도록 다음 계약을 기준으로 구현한다.

| endpoint | 현재 문제 | 목표 계약 | 전환 방식 |
| --- | --- | --- | --- |
| `/v1/sources/{id}/results` | 전체 영상과 summary 결합 | legacy compatibility only | overview/video API 전환 후 deprecate |
| `/v1/sources/{id}/overview` | 신규 | bounded summary와 top videos | additive 배포 |
| `/v1/sources/{id}/videos` | 신규 | keyset, 기본 60/최대 100 | additive 배포 |
| `/v1/videos/{id}/comments` | 댓글 전체 반환 | 사용 중단 | comment-threads 전환 후 410 또는 bounded wrapper |
| `/v1/videos/{id}/comment-threads` | keyset 제공 중 | 유지, cursor 검증 강화 | backward-compatible |
| `/v1/comments/{id}/replies` | keyset 제공 중 | 유지, cursor 검증 강화 | backward-compatible |
| `/v1/comments/{id}`의 `authorComments` | 최대 50 preview이나 의미 불명확 | preview임을 명시하고 `hasMore` 제공 | 별도 author comment page API 추가 |
| `/v1/explore` | channel 집계와 offset video 결합 | legacy compatibility only | channels/videos API 전환 후 deprecate |
| `/v1/search` | 대규모 후보 hydration | 최종 결과만 bounded 반환 | 내부 query 교체, contract 유지 |

legacy endpoint 제거 날짜는 access log에서 미전환 client가 없음을 확인한 후 정한다. API 문서와 TypeScript type에 전체 count, page count, preview와 complete list의 차이를 명시한다.

## 7. 테스트 계획

### 7.1 단위·통합 테스트

- overview가 comment text 전체 조회 없이 summary를 반환
- summary `fresh/stale/building/failed` 상태와 fallback
- target/subscription/source ID mapping과 최신 completed summary 선택
- cursor NULL ordering, tie, filter/sort mismatch, tampering, owner mismatch
- pagination 중 신규 영상 유입에도 중복 없음
- top videos가 전체 target 기준으로 유지
- polling 요청 무중첩, hidden-tab 감속, backoff, terminal one-shot invalidation
- batch comment upsert와 checkpoint의 transaction 원자성
- SIGKILL/retry 시 comment/rollup idempotency
- backfill과 live writer 동시 실행 시 lost update 없음
- Redis down/timeout/eviction 시 DB fallback과 권한 유지
- pool exhaustion 시 timeout/503와 recovery
- search ACL이 candidate limit보다 먼저 적용
- 기존/new API contract snapshot

### 7.2 데이터 fixture

고정 seed를 사용하고 다음 분포를 포함한 sanitized staging fixture를 만든다.

- 약 200만 댓글 또는 운영과 동등한 query selectivity
- 긴 댓글과 짧은 댓글
- top-level/reply 비율
- 댓글이 매우 많은 영상과 적은 영상
- 같은 영상이 여러 target에 속한 경우
- 여러 사용자와 subscription visibility 차이
- 한글/영문/숫자/ID 검색어

운영 서버에서 OS cache를 drop하지 않는다. 운영 데이터 측정은 read-only, 낮은 traffic 시간, 중단 기준을 둔 상태에서만 수행한다.

### 7.3 부하·장애 테스트

각 benchmark는 warm-up, 요청률, concurrency, 15분 이상 측정 구간을 명시하고 p50/p95/p99, 오류율, timeout, rows, RSS plateau, pool wait를 기록한다.

- source overview 동시 10건
- Explore first page와 다음 5 page
- 2자 prefix, 1자 422, 3자 이상 contains 검색
- collection worker 2개가 수집 중인 혼합 workload
- 30~60분 soak
- Redis 장애
- PostgreSQL restart와 stale pool connection
- worker SIGTERM/SIGKILL
- rollup/search backfill 동시 실행
- index build 중단과 재시도

### 7.4 go/no-go 기준

다음 중 하나라도 발생하면 read flag 또는 배포를 되돌린다.

- 5xx 오류율 1% 이상
- 기존 대비 핵심 route p95가 20% 이상 악화
- rollup mismatch 발생
- pool wait p95가 acquire timeout의 50% 초과
- DB connection이 budget의 80% 초과
- NVMe 90% 도달
- API/PostgreSQL OOM 또는 지속적인 RSS 증가
- queue oldest age가 기존 기준선의 2배 초과

## 8. 배포 계획

### 8.1 기본 순서

현재 deploy script를 다음 순서로 개선한다.

1. production credential/bind/preflight 검증
2. 기존 `PREVIOUS_SHA`, image tag, PostgreSQL 설정 기록
3. 기존 container 상태에서 backup 생성·checksum·`pg_restore --list` 검증
4. API/web/worker image build와 test를 쓰기 중지 전에 완료
5. schema 변경이 있을 때만 maintenance mode와 worker drain
6. API/worker 중지 후 expand-only migration
7. PostgreSQL config 변경이 있으면 적용·restart·readiness 확인
8. backward-compatible API 먼저 기동
9. web 기동·smoke test
10. collection/analysis worker 기동
11. feature flag 단계 전환
12. 집중 monitoring과 go/no-go 판정

schema migration이 없는 app-only 배포는 전체 DB backup과 PostgreSQL 중지를 반복하지 않도록 분기한다.

### 8.2 expand/migrate/contract

```text
expand schema
→ old+new compatible app
→ dual-write
→ resumable backfill
→ reconciliation
→ read flag cutover
→ deprecation window
→ legacy code 제거
→ 별도 후속 배포에서만 contract schema 정리
```

원본 `videos`, `comments` column/table은 이 최적화 배포에서 삭제하지 않는다.

### 8.3 backup과 복원

- backup 파일이 non-empty인지뿐 아니라 checksum과 `pg_restore --list`를 확인한다.
- 정기적으로 disposable DB에 restore하고 schema, row count, constraint smoke test를 실행한다.
- 보존 수, 보존 기간, 삭제 기준을 정의한다.
- `/data`와 별도의 off-host 사본을 유지한다.
- restore RTO/RPO 목표와 담당 명령을 runbook에 기록한다.

### 8.4 readiness와 smoke test

- `/health` liveness
- `/ready` DB/migration readiness
- 로그인과 owner scope
- source overview/video first page
- comment thread page
- Explore first/next page
- search sample query
- worker claim/lease/checkpoint
- rollup/cache flag 상태

## 9. 롤백 계획

기본 rollback은 expand schema를 남기고 application과 feature flag만 되돌리는 것이다.

### 9.1 자동·수동 rollback 준비

- 배포 전 `PREVIOUS_SHA`와 이전 image tag 저장
- 이전 PostgreSQL command/config 복사본 저장
- readiness 또는 smoke test 실패 시 이전 API/web/worker image 재기동
- DB config 문제면 이전 config로 restart
- read flag 문제면 즉시 false로 전환하고 기존 query 사용
- dual-write 문제면 read는 기존 경로 유지, writer flag를 끈 뒤 원본 검증
- Redis 문제면 cache flag를 끄고 DB fallback

deploy script가 API health 실패 후 worker가 정지된 채 종료하지 않도록 이전 service 복구 절차를 구현한다.

### 9.2 rollback 후 검증

- row count와 latest timestamps
- active/queued job과 lease
- API auth/owner scope
- old endpoint contract
- PostgreSQL connection, WAL, disk
- worker 2 replica 상태

신규 index는 query rollback 직후 성급하게 삭제하지 않는다. 사용 여부와 write cost를 확인한 뒤 maintenance window에서 제거한다.

## 10. 운영·보안 사전 점검

- production에서 example PostgreSQL/MinIO credential을 거부한다.
- runtime DB role과 migration/extension owner role을 분리한다.
- runtime role의 schema CREATE 권한을 제거한다.
- PostgreSQL과 Redis host bind가 loopback인지 확인한다.
- search term, 댓글 원문, API key를 로그/metric label에 남기지 않는다.
- worker `stop_grace_period`, lease, DB timeout의 관계를 검증한다.
- cache가 optional dependency로 유지되는지 chaos test로 확인한다.
- queue depth, oldest age, expired lease, worker heartbeat 경보를 연결한다.

## 11. 외부 데이터 엔진 도입 기준

| 후보 | 도입 기준 | 용도 |
| --- | --- | --- |
| Redis | 이번 계획에서 사용 | 짧은 TTL의 파생 summary/Explore cache |
| Meilisearch/Typesense | PostgreSQL 검색이 한글 품질·응답 목표를 충족하지 못할 때 | 검색 보조 index |
| OpenSearch | 형태소 분석, 복잡한 ranking, 대규모 검색 운영이 필요할 때 | 검색 보조 index |
| ClickHouse | 댓글 수가 수천만~수억 건이고 전역 분석이 주 workload가 될 때 | 분석 전용 파생 store |
| DuckDB | 운영 API가 아닌 offline report가 필요할 때 | Parquet 기반 batch 분석 |

SQLite와 DuckDB는 다중 worker 동시 쓰기, job lease, `FOR UPDATE SKIP LOCKED`, 지속적인 upsert가 필요한 운영 DB를 대체하지 않는다.

## 12. 구현 묶음과 의존성

| 묶음 | 내용 | 선행 조건 | rollback |
| --- | --- | --- | --- |
| A | 긴급 polling/listSources 완화 | 없음 | 이전 web/API image |
| B | 계측 bootstrap | backup, DB restart window | 이전 PostgreSQL config |
| C | target summary schema/write/backfill, overview/video API와 web 전환 | A, B | summary read/v2 flag off |
| D | connection pool과 batch write | B | batch flag off/direct path |
| E | rollup schema/dual-write/backfill | D | read flag off |
| F | Explore/Redis | E | cache/Explore flag off |
| G | trigram/search documents | B, disk gate | search flag off |
| H | PostgreSQL/API capacity tuning | C~G 기준선 | 이전 config/process 수 |

가장 먼저 A와 B를 완료한다. 현재 문제의 대부분은 A와 C에서 제거되며, 이후 단계는 전체 댓글 반복 조회의 잡음 없이 측정할 수 있다.

## 13. 예상 변경 영역과 산출물

| 영역 | 주요 파일·산출물 |
| --- | --- |
| API contract | `apps/api/monitube_api/contracts.py`, `main.py`, OpenAPI snapshot |
| service/repository | `services.py`, `repositories.py`, `postgres_repository.py`, `auth.py` |
| collection write path | `apps/worker/monitube_worker/collector.py`, batch repository method |
| analysis queue | analysis run schema, 신규 analysis worker module/service |
| web polling/page | `apps/web/app/lib/api.ts`, `collection-workbench.tsx` |
| schema/index | 순차 SQL migrations, backfill/reconciliation scripts |
| runtime | `docker-compose.yml`, `.env.example`, PostgreSQL config |
| deploy/runbook | `scripts/deploy_remote.sh`, backup/restore/rollback runbook |
| 검증 | API/repository/web tests, golden search set, load/soak scripts |

각 구현 묶음은 다음 산출물을 포함해야 완료로 간주한다.

- 코드와 migration
- feature flag와 기본값
- 단위·통합·성능 검증 결과
- 적용/확인/rollback 명령
- 변경 전후 지표 snapshot
- 남은 위험과 다음 단계 진입 조건

## 14. 구현 현황

2026-07-18 기준으로 이 문서의 애플리케이션·schema·운영 자동화 구현과 원격 운영 데이터 cutover를 완료했다. 운영 애플리케이션 release는 `378c7e9`이며 schema migration은 016까지 적용되어 있다.

### 14.1 구현 완료

- active parent job 단일 polling, recursive timeout, visibility/backoff, terminal 1회 invalidate
- source overview와 source/explore video keyset pagination, legacy compatibility wrapper
- owner ACL·scope·filter·snapshot을 검증하는 cursor
- shared `psycopg_pool`, pool acquire timeout 503, `/health`·`/ready` 분리
- comment page batch transaction과 checkpoint 원자성
- 신규 comment delta rollup과 관계·시각 변경 시 absolute recompute
- target data version, terminal transaction invalidation, 별도 lease 기반 analysis worker
- resumable rollup backfill, reconciliation, zero-mismatch `ready` gate
- owner-scoped Redis derived cache, 짧은 timeout·TTL jitter·bounded single-flight·fail-open
- Redis derived-cache 전용 `maxmemory`/`allkeys-lru`, AOF 비활성화
- `pg_trgm` 검색 문서·index, 1자 422, 2자 prefix-only, ACL 선적용 후보 300건 상한
- scalar trigram 조건과 narrow `MATERIALIZED` 후보 barrier, ACL 이후 정렬·LIMIT, PK 상세 재조회
- 검색·ACL 핵심 테이블의 명시적 `ANALYZE`와 크기별 auto-analyze threshold
- PostgreSQL runtime tuning, worker 2개와 analysis worker 1개 pool budget
- migration check/status, backup 검증, app-only/DB-change 배포 분기, 자동 application/config rollback
- backup/restore, rollback, performance cutover runbook

fanout child와 direct-video parent의 `youtubeVideoId`는 모든 checkpoint에서 보존한다. 따라서 재시도 시 영상 단위 작업이 전체 source 작업으로 확대되지 않고, 동일 영상을 공유하는 모든 target의 `data_version`도 terminal transaction에서 갱신된다. 채널 검색문서 trigger는 metadata가 실제로 바뀐 경우에만 fanout하므로 channel video 수에 대한 제곱형 write amplification을 피한다.

### 14.2 검증 완료

- API/worker 전체 테스트 73개 통과
- web TypeScript 검사와 production build 통과
- Python bytecode, Bash/sh 문법, Compose config, `git diff --check` 통과
- 원격 PostgreSQL 16 disposable DB에서 migration 001~016 전체 적용, 확장·reloptions 검증 및 `--check` 통과
- 원격 기존 데이터의 active analysis/result 중복 제약 충돌 사전 점검 통과
- 원격 Redis keyspace가 비어 있고 현재 AOF 데이터가 0임을 확인

### 14.3 원격 운영 반영 결과

- PostgreSQL·MinIO/S3 예제 credential을 회전했으며 실제 값은 server-only env에만 유지한다.
- cutover 전 custom-format backup의 checksum과 `pg_restore --list`를 검증했다. 검색 후속 배포 backup은 `/data/psyche/backups/monitube/monitube-pre-change-20260718T110514Z.dump`, release metadata는 `/data/psyche/backups/monitube/releases/20260718T110514Z-378c7e906142`에 있다.
- PostgreSQL 16 runtime 설정과 migration 001~016, `pg_stat_statements`, `pg_trgm`, 검색 index 2개의 valid/ready 상태를 확인했다.
- collection worker 2개와 analysis worker 1개가 실행 중이며 API·PostgreSQL·Redis health, web HTTP 200을 확인했다.
- 모든 performance flag를 활성화했다. `/ready`는 migration current, pool enabled, wait/error 0, Redis derived cache `ok`를 반환한다.
- rollup backfill은 `ready`, missing 0, mismatch 0이다. 2026-07-18 검증 시 raw comment와 rollup 합계가 모두 2,850,797건이었고 summary gate 미충족 target은 0건이었다.
- 동일 운영 read smoke 3회에서 source overview 16.4~22.7ms, Explore channel 35.9~37.5ms, Explore video 30.8~31.5ms였다.
- `video` 통합 검색은 5,808.5ms에서 131.5~139.8ms로 약 43배 개선됐다. generic plan 검증에서도 narrow candidate SQL은 약 9.7ms로 GIN plan을 유지했다.
- 배포 직후 API·web·collection worker·analysis worker·PostgreSQL의 critical log pattern은 모두 0건이었다.

### 14.4 후속 운영 hardening

1. 검증된 backup을 off-host/object storage에도 복제하고 정기 restore drill을 수행한다.
2. PostgreSQL runtime role과 migration role을 분리하고 최소 권한을 적용한다.
3. root filesystem 사용률 89%를 경보 대상으로 두고 오래된 Docker build cache와 image 보존 정책을 운영한다. DB와 backup이 위치한 `/data`는 검증 시 34% 사용 중이었다.
4. 실제 장기 workload에서 p50/p95/p99, pool wait, temp/WAL, RSS, queue age와 YouTube quota 대기 시간을 계속 관찰한다.

배포 script는 예제 PostgreSQL 또는 MinIO credential을 감지하면 service나 DB를 변경하기 전에 실패하며, DB 변경 배포는 검증된 backup과 rollback metadata가 생성된 뒤에만 cutover한다.
