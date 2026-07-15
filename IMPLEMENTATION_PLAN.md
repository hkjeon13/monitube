# Monitube 구현 계획

> 갱신일: 2026-07-16
> 기준: YouTube Data API v3 공식 문서와 YouTube API Services 개발자 정책

## 1. 제품 정의

Monitube는 사용자가 채널, 키워드 또는 영상 URL/ID를 입력하면 서비스 운영자가 백엔드에 설정한 YouTube Data API credential로 공개 데이터를 수집하고, PostgreSQL에 저장한 뒤 분석 화면으로 보여주는 서비스다.

사용자는 다음만 입력하거나 선택한다.

- 채널 URL, `@handle`, 채널 ID
- 키워드와 기간·언어·지역·정렬·수집 범위
- 영상 URL 또는 영상 ID
- 댓글 포함 여부와 수집 상한

사용자는 API key, Google Cloud project, OAuth token, credential ID를 입력하거나 볼 수 없다. credential·quota·retry는 전부 서버 운영 책임이다.

### 현재 MVP 범위

| 대상 | 수집 | 저장 | 분석 |
| --- | --- | --- | --- |
| 채널 | 프로필, 업로드 영상, 공개 통계 | 가능 | 업로드 추이, 영상 목록 |
| 키워드 | 검색 결과와 발견 순위 | 가능 | 기간/키워드별 발견 추이 |
| 영상 | 메타데이터, 공개 통계 | 가능 | 영상별 요약 지표 |
| 공개 댓글 | 스레드와 답글, 좋아요·작성 시각 | 가능 | 주제·빈도·요약 등 정책 gate 후 |
| 임의 영상 자막 원문 | **MVP 제외** | 제외 | 제외 |

`captions.list`와 `captions.download`는 OAuth를 요구하며, download는 영상을 편집할 권한도 요구한다. 서버 API key만으로 임의 공개 영상 자막을 가져올 수 없으므로, 사용자가 token을 제공하지 않는 이 제품의 MVP에는 넣지 않는다. [공식 자막 가이드](https://developers.google.com/youtube/v3/guides/implementation/captions), [download 권한](https://developers.google.com/youtube/v3/docs/captions/download)

## 2. 핵심 결정

1. 서버의 단일 운영 API key를 Secret Manager/KMS에서 주입한다. 브라우저와 DB에는 raw key를 두지 않는다.
2. 채널 전체 업로드는 `search.list`가 아니라 `channels.list` → uploads playlist → `playlistItems.list`로 찾는다.
3. 키워드 source만 `search.list`를 사용하며, 각 run의 고정 시간 window·page cursor·coverage를 보존한다.
4. 영상 URL/ID는 `videos.list`로 직접 수집한다.
5. 공개 댓글은 `commentThreads.list`와 필요한 `comments.list`로 수집하며, 댓글 비활성화·삭제·비공개는 부분 경고로 기록한다.
6. quota 소진 시 key/account/project를 바꾸지 않는다. DB checkpoint와 quota ledger를 저장하고 동일 운영 config의 다음 가능 window에서 자동 재개한다.
7. 수집과 분석은 worker로 분리한다. API 요청은 작업을 만들고 상태를 돌려준다.
8. 분석 결과는 원본 video/comment 근거와 분석 버전·coverage를 함께 보관한다.

## 3. 공식 API 경로

### 3.1 채널 source

| 입력 | 정규화 | API 경로 |
| --- | --- | --- |
| `UC...` | channel ID | `channels.list(id=...)` |
| `youtube.com/channel/UC...` | channel ID 추출 | `channels.list(id=...)` |
| `@handle`, `youtube.com/@handle` | handle | `channels.list(forHandle=...)` |
| `youtube.com/user/name` | legacy username | `channels.list(forUsername=...)` |
| 일반 이름, `/c/...` | 후보 선택 필요 | `search.list(type=channel)`을 제한적으로 사용 |

정상 채널은 `contentDetails.relatedPlaylists.uploads`를 얻은 뒤 다음 순서로 처리한다.

1. `playlistItems.list(maxResults=50)`를 page 단위로 호출한다.
2. video ID를 최대 50개씩 묶어 `videos.list`로 메타데이터·통계를 갱신한다.
3. 사용자가 댓글 포함을 선택한 영상만 댓글 단계로 보낸다.

### 3.2 키워드 source

`search.list(type=video)`에 query, 기간, 지역, 언어, order를 적용한다. run 시작 시 `window_end`를 고정하고, 이전 성공 watermark와 overlap으로 `window_start`를 정한다.

- 기본 정렬: `date`
- 결과: video ID, page/rank, run window, query version
- 중복: YouTube video ID로 dedupe하되 source/run별 발견 경로는 보존
- 상한 도달: `coverage=limited`로 저장하며 전체 결과라고 표시하지 않음

2026-06-01 이후 `search.list`는 Search Queries bucket에서 호출당 1회로 관리되고, 다른 Data API 호출은 공용 quota bucket을 사용한다. 실제 quota는 서버 ledger가 판단하며 UI의 값은 보수적 예상치다. [revision history](https://developers.google.com/youtube/v3/revision_history), [quota calculator](https://developers.google.com/youtube/v3/determine_quota_cost)

### 3.3 영상 source

영상 source는 다음 입력을 네트워크 요청 없이 정규화한다.

- 11자 YouTube video ID
- `youtube.com/watch?v=...`
- `youtu.be/...`
- `youtube.com/shorts/...`

정규화 후 `videos.list(part=snippet,contentDetails,statistics,status,liveStreamingDetails)`를 호출하고, 옵션에 따라 공개 댓글을 수집한다.

### 3.4 공개 댓글

`commentThreads.list(videoId=..., part=snippet,replies, maxResults=...)`로 top-level thread를 받고, inline replies가 충분하지 않으면 `comments.list(parentId=...)`로 답글을 page 처리한다.

- `commentsDisabled`, 삭제/비공개 영상, 댓글 접근 불가는 job 실패가 아니라 `partial_error`로 기록
- thread/comment ID를 unique key로 upsert
- 페이지 token은 active job checkpoint로만 사용하고 장기 영구 cursor로 취급하지 않음
- 댓글 텍스트와 작성자 식별자는 보존·삭제 정책 및 분석 redaction을 적용

## 4. 아키텍처

```text
Browser
  └─ Next.js collection console
       ├─ source: channel | keyword | video
       ├─ quota preflight / job status
       └─ analysis result views

FastAPI
  ├─ source / job / result API
  ├─ input normalization and validation
  ├─ server-managed credential resolver
  └─ PostgreSQL repository

Worker + scheduler
  ├─ YouTube Data API adapter
  ├─ quota reservation / request log
  ├─ checkpoint / retry / automatic resume
  └─ analysis pipeline dispatch

PostgreSQL              Redis              Object storage
sources, jobs,          queue wakeups,     optional analysis artifacts
quota, data, results    leases, cache      (no caption upload flow in MVP)
```

### 책임 경계

| 계층 | 책임 | 금지 사항 |
| --- | --- | --- |
| Browser | source 범위 입력, 예상치·상태·결과 표시 | API key·project ID·credential 선택 노출 |
| API | 검증, job 생성, 읽기 API, 내부 config 선택 | YouTube long-running 호출을 request 안에서 완료 |
| Worker | API 호출, upsert, checkpoint, quota accounting | quota 오류 시 다른 key/project로 failover |
| Scheduler | due job lease, retry/resume, stale lease 복구 | dummy quota probe 반복 호출 |
| DB | source of truth, audit, idempotency | raw API key/token 저장 |

## 5. 서버 credential 및 quota

### 5.1 운영 설정

- 로컬: `.env`의 `YOUTUBE_API_KEY`
- 운영: Secret Manager/KMS secret injection
- PostgreSQL: `youtube_runtime_configs.secret_ref`, fingerprint, 활성 상태, Google project 식별자만 선택적으로 보관
- 공개 API: key, secret ref, fingerprint, project ID를 받거나 반환하지 않음

같은 API project의 key 교체는 유출·폐기 대응을 위한 운영 절차다. job의 처리량을 늘리거나 quota 소진을 우회하기 위한 key/account/project rotation은 구현하지 않는다. [YouTube 정책](https://developers.google.com/youtube/terms/developer-policies-guide#dont-spread-api-access-across-multiple-or-unknown-projects)

### 5.2 quota reservation과 자동 재개

1. worker가 endpoint, bucket, 예상 비용, `request_fingerprint`를 계산한다.
2. 짧은 transaction에서 `quota_windows` row를 lock하고 비용을 reserve한다.
3. 예산이 부족하면 YouTube API를 호출하지 않고 job을 `waiting_quota`로 전환한다.
4. API 응답과 데이터 upsert, checkpoint advance, reservation consume, request log, outbox event를 한 transaction으로 확정한다.
5. `quotaExceeded`/`dailyLimitExceeded`이면 해당 config+bucket window를 닫고 `resume_at`을 다음 quota reset 이후로 기록한다.
6. scheduler는 1~5분마다 `resume_at <= now()` 작업을 `FOR UPDATE SKIP LOCKED`로 lease하고, 동일 runtime config와 checkpoint로 `queued`에 재등록한다.
7. 429, rate limit, 5xx, timeout은 quota 소진과 분리해 exponential backoff + jitter로 `waiting_retry` 처리한다.

작업은 `queued → running → completed | completed_with_warnings | failed`를 기본으로 하며, 대기 상태는 `waiting_retry`, `waiting_quota`다. `auth_required` 상태와 사용자 credential fallback은 MVP에 없다.

## 6. 공개 API

모든 API는 시스템이 관리하는 default collection scope에서 동작한다. URL과 JSON에 project ID나 credential 정보가 없다.

| Method | Path | 설명 |
| --- | --- | --- |
| `POST` | `/v1/channel-resolutions` | 채널 입력을 API lookup 형태로 정규화 |
| `POST` | `/v1/video-resolutions` | 영상 URL/ID 정규화 |
| `POST` | `/v1/sources` | channel/keyword/video source 생성 |
| `GET` | `/v1/sources` | source 목록 |
| `GET/PATCH/DELETE` | `/v1/sources/{sourceId}` | source 조회·수정·삭제 |
| `POST` | `/v1/sources/{sourceId}/jobs` | 수집 job 생성 |
| `GET` | `/v1/jobs/{jobId}` | 진행률, 대기 사유, resume 시각 |
| `GET` | `/v1/videos/{videoId}` | 저장된 video 메타데이터 |
| `GET` | `/v1/videos/{videoId}/comments` | 저장된 공개 댓글 |
| `POST` | `/v1/analysis-runs` | 저장 데이터 분석 job 생성 (policy gate 뒤) |

예시 source:

```json
{
  "type": "video",
  "config": {
    "input": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "includeComments": true,
    "maxCommentPagesPerVideo": 3
  }
}
```

## 7. PostgreSQL 모델

| 영역 | 테이블 |
| --- | --- |
| 운영 config | `youtube_runtime_configs` |
| source | `collection_sources`, `keyword_queries`, `keyword_search_runs`, `keyword_search_results`, `source_videos` |
| YouTube 원본 | `channels`, `channel_snapshots`, `videos`, `video_stat_snapshots`, `comments` |
| 실행 | `sync_jobs`, `sync_checkpoints`, `job_events`, `outbox_events` |
| quota | `quota_windows`, `quota_reservations`, `quota_ledger`, `api_request_logs` |
| 분석 | `analysis_runs`, `analysis_results`, `analysis_evidence` |
| 삭제/보존 | `deletion_jobs`, `expires_at`, `deleted_at` |

주요 규칙:

- channel/video/comment의 YouTube ID를 unique key로 upsert한다.
- `source_videos`는 같은 영상이 여러 keyword run·channel source·direct video source에 걸쳐 발견된 provenance를 보존한다.
- raw API response, key, authorization header는 DB에 저장하지 않는다.
- YouTube 원본 데이터는 30일 이내 갱신 또는 삭제하는 lifecycle을 worker로 강제한다.
- `comments(video_id, published_at desc)`, `sync_jobs(resume_at)`, `quota_windows(reset_at)`, `keyword_search_runs(window_end)`를 index한다.

## 8. 프런트엔드

### 첫 화면

1. source 탭: 채널 / 키워드 검색 / 영상
2. 범위: 최대 영상·검색 page·영상별 댓글 page
3. 서버 기준 quota preflight와 보수적 browser 예상치
4. 작업 시작, 진행률, partial warning, `waiting_quota` 자동 재개 시각

화면에서 제거할 항목:

- 개발용 project ID 입력
- API key 입력/저장
- OAuth 연결·scope·revoke 설정
- 자막 업로드·자막 권한 선택

### 결과 화면

- source별 최신 run, coverage, 마지막 성공 시각
- 영상 목록과 통계 snapshot 변화
- 댓글 목록, thread, 시간·좋아요 필터
- 분석 결과, 표본 수, coverage, 원본 video/comment 근거

## 9. 분석 파이프라인

수집 완료 후 별도 analysis job을 실행한다.

1. 댓글 HTML 정리, 중복/빈 텍스트 제거, PII 최소화
2. 기간·영상·댓글 수를 고정한 재현 가능한 sample plan 생성
3. 결정적 집계(빈도, 업로드/댓글 추이)와 NLP/LLM map-reduce 실행
4. schema validation, coverage, confidence를 저장
5. 결과마다 comment ID/video ID 근거를 연결

YouTube 원본 지표와 자체 계산 지표는 UI에서 명확히 구분한다. 감성·주제·요약처럼 YouTube API 데이터에서 파생한 일반 사용자용 분석을 출시하려면 해당 서비스 정책의 Analytics & Reporting requirements를 검토하고 gate를 열어야 한다. [Derived Metrics Policy](https://developers.google.com/youtube/terms/derived-metrics-policy)

## 10. 보안·보존

- `YOUTUBE_API_KEY`는 client bundle, API response, 로그, queue payload, DB column에 넣지 않는다.
- 로그는 URL query의 key·Authorization header·댓글 작성자 식별자를 redaction한다.
- 댓글과 원본 메타데이터는 만료 시각, 삭제 표식, deletion job을 통해 lifecycle 처리한다.
- API 입력 URL은 YouTube allowlist와 parser로 처리하며 서버가 임의 URL을 fetch하지 않는다.
- 분석 작업에 전달하는 텍스트는 최소 필요한 범위로 제한하고, 모델·프롬프트·분석 버전을 감사 가능하게 저장한다.

## 11. 구현 순서

1. **Foundation**: server-managed config, source/job API, PostgreSQL migration, collection console
2. **Data adapter**: channel/keyword/video resolver, videos/comments upsert, idempotency
3. **Durability**: PostgreSQL repository, lease/outbox, quota reservation, resume scheduler
4. **Results**: video/comment read API, source run history, analysis dashboard
5. **Analysis**: policy gate 승인 뒤 topic/summary/trend pipeline
6. **Operations**: metrics, alerts, lifecycle/deletion worker, quota reconciliation

## 12. 완료 기준

- 사용자가 API key나 project ID 없이 channel/keyword/video source를 만들 수 있다.
- source → job → 저장된 channel/video/comment → 결과 화면 흐름이 동작한다.
- 동일 video/comment를 재수집해도 중복 row가 생기지 않는다.
- `quotaExceeded` 후 checkpoint와 quota log가 저장되고 같은 runtime config로 자동 재개된다.
- key/account/project rotation이 quota 우회 경로로 존재하지 않는다.
- 댓글 비활성화·삭제 영상·부분 실패가 job 전체 실패와 구분되어 표시된다.
- raw key/token이 DB, 로그, browser에 존재하지 않는다.

## 13. 공식 참고 문서

- [YouTube Data API 구현 가이드](https://developers.google.com/youtube/v3/guides/implementation)
- [Channels: list](https://developers.google.com/youtube/v3/docs/channels/list)
- [채널 업로드 동영상 조회](https://developers.google.com/youtube/v3/guides/implementation/videos#retrieve_a_channels_uploaded_videos)
- [PlaylistItems: list](https://developers.google.com/youtube/v3/docs/playlistItems/list)
- [Videos: list](https://developers.google.com/youtube/v3/docs/videos/list)
- [Search: list](https://developers.google.com/youtube/v3/docs/search/list)
- [CommentThreads: list](https://developers.google.com/youtube/v3/docs/commentThreads/list)
- [Comments: list](https://developers.google.com/youtube/v3/docs/comments/list)
- [오류와 quotaExceeded](https://developers.google.com/youtube/v3/docs/errors)
- [Quota Calculator](https://developers.google.com/youtube/v3/determine_quota_cost)
- [YouTube API Services 개발자 정책](https://developers.google.com/youtube/terms/developer-policies)
- [여러 project/key로 접근을 분산하지 말라는 준수 가이드](https://developers.google.com/youtube/terms/developer-policies-guide#dont-spread-api-access-across-multiple-or-unknown-projects)
- [파생 지표 정책](https://developers.google.com/youtube/terms/derived-metrics-policy)
