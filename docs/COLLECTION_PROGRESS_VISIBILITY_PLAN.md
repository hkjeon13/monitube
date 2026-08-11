# 수집 진행률·실시간 반영·공식 API 쿼터 개선 계획

> 상태: 2026-08-11 구현 완료. 신규 작업은 최대 50개 영상 metadata batch와 영상별 comment job을 사용하며, 기존 `jobKind=video` 작업은 그대로 호환 처리한다. 원격 배포 결과는 배포된 Git SHA와 운영 검증 기록을 기준으로 확인한다.
>
> 기준 시각: 2026-08-11 16:08 KST 원격 운영 상태와 배포 코드 `99176a2a4b46f1326386f904171067093cc37ece`를 진단한 결과를 바탕으로 작성했다. 큐 수치는 시시각각 변할 수 있다.

## 1. 목적

현재 화면의 `영상 수집률`, `댓글 수집률`, `진행 중` 표시는 서로 다른 의미의 데이터를 섞어 사용한다. 그 결과 다음과 같은 혼란이 발생한다.

- 기존 영상이 수백 개 있어도 최신 재수집 작업이 시작되지 못하면 영상 수집률이 `0%`로 보인다.
- 영상 정보가 이미 저장됐어도 댓글 수집이 끝나지 않으면 부모 작업의 영상 완료 건수에 포함되지 않는다.
- `waiting_quota`, `waiting_retry`도 소스 목록에서는 모두 `진행 중`으로 표시된다.
- DB에는 영상이 순차 저장되지만 영상 목록과 채널 집계는 부모 작업이 종료될 때 갱신되어 화면에서는 한꺼번에 반영되는 것처럼 보인다.
- SearchAPI.io가 채널·키워드 discovery를 담당해도 발견된 영상을 공식 `videos.list`로 한 건씩 보강하면 공식 API quota를 불필요하게 많이 사용한다.

이번 개선의 목표는 다음과 같다.

1. 누적 보유량과 이번 작업 진행률을 분리한다.
2. 영상 메타데이터, 대본, 댓글의 진행률과 대기 원인을 분리한다.
3. 저장된 영상이 작업 중에도 화면에 점진적으로 나타나게 한다.
4. 공식 `videos.list`를 최대 50개 단위로 묶어 quota 사용량을 줄인다.
5. 수천 건의 기존 active job과 저장 데이터에 영향을 주지 않고 점진 배포한다.

## 2. 확인된 현재 동작과 원인

### 2.1 운영 상태 스냅샷

진단 시점의 원격 자식 작업 상태는 다음과 같았다.

| 상태 | 작업 수 | 의미 |
| --- | ---: | --- |
| `waiting_quota` | 4,509 | 공식 YouTube API `quotaExceeded` 후 자동 재개 대기 |
| `queued` | 1,063 | worker 실행 순서 대기 |
| `running` | 2 | 실제 실행 중 |

실제 pause reason에는 다음 두 종류가 모두 있었다.

- `YouTube videos request failed with HTTP 403 (quotaExceeded)`
- `YouTube comments request failed with HTTP 403 (quotaExceeded)`

대표적인 표시 불일치는 다음과 같다.

| 수집 대상 | 누적 보유 영상 | 최신 작업 | 화면이 오해를 만드는 이유 |
| --- | ---: | ---: | --- |
| 키워드 `키큰녀` | 691 | 영상 `0/17` | 최신 17건이 모두 quota 대기여서 누적 보유량과 무관하게 0% 표시 |
| 채널 `@뽀구미` | 1,836 | 영상 작업 `0/1,091` | 채널 누적 coverage와 최신 작업 진행률이 화면 위치에 따라 서로 다르게 보일 수 있음 |
| 채널 `@moviethink` | 18 | 발견 `1,098` | 아주 적은 완료 건수는 반올림되어 한동안 0%로 보이고 목록은 작업 종료 전까지 갱신되지 않음 |

### 2.2 코드상 원인

- 키워드 행의 영상·댓글 퍼센트는 누적 보유량이 아니라 `latestJob.videoProgress`, `latestJob.commentProgress`로 계산한다.
- 채널 행은 Explore의 누적 coverage를 사용하므로 채널과 키워드가 같은 열에서 서로 다른 의미를 가진다.
- 자식 작업은 영상 저장 후 대본을 시도하고, 댓글 수집까지 끝난 뒤 terminal 상태가 된다.
- 부모의 영상 완료 건수는 영상 저장 성공 건수가 아니라 terminal 자식 작업 수다. 댓글 quota 대기 중인 자식은 영상이 저장되어도 영상 완료로 계산되지 않는다.
- 소스 목록 상태는 `running`, `queued`, `waiting_quota`, `waiting_retry`를 모두 `진행 중` 하나로 합친다.
- active job polling은 진행 상태만 갱신하며, `refreshResults()`와 `refreshExplore()`는 작업이 terminal이 되었을 때만 호출한다.
- fan-out된 영상 자식 작업은 공식 `videos.list`를 영상 ID 한 개씩 호출한다. API가 최대 50개 ID를 받을 수 있는데도 대형 채널은 영상 수만큼 요청을 만든다.

## 3. 표시 모델 결정

### 3.1 누적 보유량과 이번 작업을 분리한다

소스 목록의 기존 `영상 수집률`, `댓글 수집률` 두 열을 다음 의미로 재구성한다.

| 표시 | 값 | 비고 |
| --- | --- | --- |
| 보유 데이터 | `영상 691개 · 댓글 12,430개` | 현재 사용자가 볼 수 있는 target membership 기준 누적 저장량 |
| 이번 수집 | `영상 8/17 · 댓글 3/17` | 최신 parent job의 단계별 성공 건수 |
| 수집 상태 | `수집 중`, `할당량 대기`, `재시도 대기`, `완료`, `일부 실패`, `실패` | backend job state와 phase 대기 원인을 보존 |

퍼센트만 단독으로 보여주지 않는다. 항상 `완료/전체`를 함께 표시한다.

- `0/17`은 `0% · 0/17`로 표시한다.
- `5/1,098`처럼 0보다 크지만 1% 미만이면 `<1% · 5/1,098`로 표시한다.
- 전체 수가 아직 발견되지 않았으면 `발견 중 · 12개 저장`으로 표시한다.
- 작업 기록이 없으면 `수집 기록 없음`으로 표시한다.

### 3.2 채널 coverage와 키워드 보유량의 의미를 분리한다

채널은 채널 snapshot의 전체 영상 수가 있으므로 누적 coverage를 보조 정보로 제공할 수 있다.

```text
영상 1,836 / 채널 전체 약 2,000개 (92%)
```

- snapshot 전체 수가 없으면 저장 영상이 있어도 `0%`로 만들지 않고 `영상 1,836개 · 전체 수 미확인`으로 표시한다.
- 저장 수가 snapshot보다 많으면 snapshot 지연으로 간주해 `100%+`를 만들지 않고 `1,157개 / 최근 확인 1,100개`와 `채널 수치 갱신 필요`를 표시한다.
- coverage는 누적 보유 상태이며 이번 작업 진행률과 섞지 않는다.

키워드는 YouTube 전체 검색 결과의 안정적인 분모가 없으므로 누적 `수집률`을 만들지 않는다.

```text
보유 영상 691개
이번 수집 0/17 · 할당량 대기
```

### 3.3 상태를 정확히 노출한다

소스 목록과 작업 상세에서 동일한 상태 vocabulary를 사용한다.

| backend 상태/조건 | 사용자 표시 |
| --- | --- |
| `queued` | 대기 중 |
| `running` + discovery | 영상 찾는 중 |
| `running` + video metadata | 영상 정보 저장 중 |
| `running` + transcript | 대본 수집 중 |
| `running` + comments | 댓글 수집 중 |
| `waiting_quota` | 할당량 대기 |
| `waiting_retry` | 재시도 대기 |
| `completed` | 완료 |
| `completed_with_warnings` | 일부 경고와 함께 완료 |
| child 일부 실패 | 일부 실패 |
| `failed` | 실패 |

`waiting_quota`에는 `resumeAt`을 KST로 표시하고, `waiting_retry`에는 다음 재시도 시각과 마지막 오류의 안전한 요약을 표시한다. API key, provider request URL, page token은 노출하지 않는다.

## 4. 작업·진행률 모델 개선

### 4.1 단계별 성공 수와 terminal 수를 분리한다

부모 job API에 다음 집계를 제공한다.

```json
{
  "discoveryProgress": { "completed": 1098, "total": 1098, "unit": "videos" },
  "videoProgress": { "completed": 18, "total": 1098, "failed": 0, "unit": "videos" },
  "transcriptProgress": { "completed": 12, "total": 18, "failed": 1, "skipped": 5, "unit": "videos" },
  "commentProgress": { "completed": 7, "total": 1098, "failed": 0, "unit": "videos" },
  "workProgress": { "terminal": 7, "total": 1098, "failed": 0, "unit": "videos" }
}
```

정의는 다음과 같다.

- `videoProgress.completed`: canonical video metadata 저장과 target/source link가 성공한 영상 수
- `transcriptProgress.completed`: 대본 저장 성공 수
- `transcriptProgress.skipped`: 대본 없음, 언어 없음, 기존 영상이라 미수집 등 정책상 정상 skip 수
- `commentProgress.completed`: 댓글 수집이 필요 없거나, 이미 최신이거나, 이번 댓글 sync가 성공한 영상 수
- `workProgress.terminal`: 필요한 모든 단계가 terminal인 영상 수
- `failed`: 성공 수에 포함하지 않고 별도 표시

부모 job의 최종 완료 조건은 기존과 같이 필요한 모든 자식 단계의 terminal 여부를 사용한다. 다만 UI의 영상 진행률은 더 이상 terminal 자식 수를 사용하지 않는다.

### 4.2 영상 저장 직후 영상 단계 완료를 기록한다

영상 자식 작업 순서를 다음처럼 변경한다.

```text
공식 videos.list 보강
-> video + stat snapshot 저장
-> target/source membership 저장
-> videoProgress 성공 반영
-> SearchAPI 대본 시도
-> 공식 API 댓글 수집
-> 전체 자식 작업 terminal
```

대본 또는 댓글이 느리거나 quota 대기여도 이미 저장된 영상 정보의 진행률은 되돌아가지 않는다.

### 4.3 집계 저장 방식

1차 구현은 repository에 `child_phase_summary(parent_job_id)`를 추가해 자식 checkpoint의 단계별 성공·실패·대기 수를 한 번에 집계한다.

- `parent_job_id` 기존 인덱스를 사용한다.
- 부모가 재시도될 때 한 SQL로 모든 phase를 계산한다.
- 10,000개 자식 작업 기준 실행 계획과 latency를 측정한다.
- JSON checkpoint 집계가 polling 부하를 만들면 `sync_jobs`에 additive phase 상태 컬럼 또는 별도 `job_progress_counters`를 추가하고 자식 전이와 같은 transaction에서 증가시킨다.
- 실패·재시도로 동일 자식을 두 번 세지 않도록 단계 완료 전이를 idempotent하게 만든다.

## 5. 공식 `videos.list` 배치화

SearchAPI discovery 이후의 canonical 보강은 최대 50개 ID를 한 요청에 넣는다.

### 5.1 권장 작업 구조

```text
parent discovery job
  -> video_batch job: ID 최대 50개, 공식 videos.list 1회
       -> 영상·통계·membership 각각 저장
       -> 신규 영상 SearchAPI transcript 시도
       -> 댓글 대상 comment job 생성
  -> parent가 video/comment/transcript 단계를 독립 집계
```

- 1,098개 영상은 최대 1,098회의 `videos.list` 대신 22회로 줄인다.
- 존재하지 않거나 비공개로 반환되지 않은 ID는 batch 전체 실패가 아니라 ID별 `unavailable`로 기록한다.
- batch 요청 자체가 quota 대기이면 해당 batch만 재시도한다.
- 댓글 API는 영상별 endpoint이므로 기존 per-video 요청을 유지한다.
- 댓글은 SearchAPI로 전환하지 않는다.
- 대본은 기존 SearchAPI 한국어 우선, 없으면 영어 fallback 정책을 유지한다.

### 5.2 기존 작업 호환

배포 시 이미 존재하는 수천 건의 legacy 영상 자식 작업을 삭제하거나 변환하지 않는다.

- collector는 기존 `jobKind=video`와 신규 `jobKind=video_batch`, `comment`를 모두 처리한다. 대본은 `video_batch` 내부의 독립 progress 단계로 기록한다.
- 배포 이후 새 parent job만 batch 구조를 사용한다.
- 기존 active parent는 현재 child ID와 checkpoint를 유지하며 끝까지 처리할 수 있어야 한다.
- 동일 parent/video에 신규 comment job이 중복 생성되지 않도록 idempotency key를 `comment:<parentId>:<videoId>`로 고정한다.
- 기존 저장 영상·댓글·대본은 재작성하거나 삭제하지 않는다.

## 6. 작업 중 화면 점진 갱신

### 6.1 갱신 트리거

active job polling 응답에서 다음 값이 달라졌을 때 선택한 소스 데이터를 다시 가져온다.

- `videoProgress.completed`
- `commentProgress.completed`
- phase별 `failed`, `waitingQuota`
- parent job state 또는 stage

갱신 정책:

- 선택한 소스의 영상 목록: 영상 완료 수 증가 시 최대 5초에 한 번 갱신
- 선택한 영상의 댓글 창: 해당 영상 comment job 완료 시 갱신, 사용자가 작성한 정렬·cursor 상태는 안전하게 초기화
- Sources 보유량: 최대 10초에 한 번 갱신
- Explore 채널 집계: 최대 10초에 한 번 갱신
- terminal 전환 시 기존처럼 전체 최종 갱신
- 브라우저가 background이면 빈도를 낮추고 visible 전환 시 즉시 갱신

동일 poll에서 여러 값이 바뀌어도 `refreshResults`, `refreshSources`, `refreshExplore`는 각각 한 번만 호출한다. 이전 요청이 끝나지 않았으면 중복 요청을 만들지 않는다.

### 6.2 목록 안정성

- 진행 중 새 영상이 첫 페이지에 추가되어도 사용자가 보고 있던 상세 영상은 닫지 않는다.
- cursor 기반 다음 페이지에 snapshot boundary가 있다면 현재 목록을 조용히 덧붙이지 않고 첫 페이지를 재조회한다.
- 스크롤 점프를 막기 위해 새 항목 수를 알리는 `새 영상 N개` 배지를 먼저 보여주고 사용자가 누르면 상단에 반영하는 방식도 E2E 검증 후 선택할 수 있다.
- 최소 제품 범위에서는 첫 페이지 재조회 후 현재 선택 ID와 스크롤 위치를 최대한 유지한다.

## 7. API·계약 변경

### 7.1 JobStatus 확장

`packages/contracts`, FastAPI contract, presenter, web normalizer를 함께 변경한다.

- `discoveryProgress?`
- `videoProgress?`에 `failed?`, `waitingQuota?` 추가
- `transcriptProgress?`
- `commentProgress?`에 `failed?`, `waitingQuota?` 추가
- `workProgress?`
- `currentStage`
- `resumeAt`, `retryAt`
- `statusReasonCode`: UI가 원문 오류 문장을 파싱하지 않도록 제한된 enum 사용

기존 client와 legacy job을 위해 모든 신규 필드는 optional로 시작한다. 기존 `videoProgress`, `commentProgress` 필드 이름은 유지하되 의미를 성공한 phase 수로 바로잡고 API changelog에 기록한다.

### 7.2 SourceSummary 확장

- `storedVideoCount`
- `storedCommentCount`
- 채널만 `reportedVideoCount?`, `coveragePercent?`, `coverageAsOf?`
- `latestJob`의 단계별 진행률

소스 목록을 그리기 위해 전체 Explore payload를 target ID로 다시 조합하는 현재 결합을 제거하고, SourceSummary가 자기 행에 필요한 누적 카운터를 직접 제공하게 한다. Explore는 탐색 화면 전용으로 유지한다.

## 8. 구현 단계

### Phase 0. 계약·운영 기준 고정

- 운영 DB snapshot과 job state 분포를 다시 기록한다.
- `videoProgress`, `commentProgress`, `workProgress`의 성공·skip·실패 정의를 테스트 fixture로 고정한다.
- legacy active job 수와 최소/최대 `resumeAt`을 확인한다.
- 공식 API 호출량 metric의 현재 baseline을 확보한다.

### Phase 1. 표시 정확성 우선 개선

- `waiting_quota`, `waiting_retry`를 `진행 중`과 분리한다.
- 키워드의 누적 보유량과 최신 작업 진행률을 분리한다.
- 1% 미만 진행을 `<1%`와 실제 분수로 표시한다.
- 채널 전체 수가 없을 때 0% 대신 `전체 수 미확인`을 표시한다.
- SourceSummary에 행 단위 누적 영상·댓글 카운터를 추가한다.

### Phase 2. 단계별 backend 진행률

- 영상 저장 성공과 child terminal을 분리한다.
- 부모가 자식의 video/comment/transcript phase를 독립 집계한다.
- quota 대기 phase와 재개 시각을 API로 전달한다.
- legacy checkpoint가 없는 작업은 현재 방식으로 fallback하되 `legacyProgress=true`를 표시한다.

### Phase 3. 점진적 화면 갱신

- 진행률 delta 기반으로 선택 소스 결과와 누적 카운터를 throttle 갱신한다.
- terminal 최종 갱신은 유지한다.
- 느린 네트워크, background tab, poll 실패에서 요청 폭증이 없는지 검증한다.

### Phase 4. `videos.list` 최대 50개 배치화

- 신규 `video_batch` job과 ID별 결과 저장을 구현한다.
- metadata 보강 직후 transcript 단계를 기록하고, 댓글은 영상별 comment job으로 fan-out한다.
- legacy `video` job 처리 경로를 유지한다.
- quota ledger에 provider operation과 batch size를 기록한다.

### Phase 5. 운영 정리

- 기존 quota 대기 작업이 정상 재개·종료되는지 관찰한다.
- 신규 작업의 `videos.list` 요청 수가 `ceil(discovered/50)`에 근접하는지 확인한다.
- 장기 대기 parent가 상태상 완료되지 못하는 orphan child를 탐지하는 운영 query와 경고를 추가한다.

## 9. 테스트 계획

### 9.1 Worker/API

- 영상 저장 후 댓글이 quota 대기여도 `videoProgress=1/1`, `commentProgress=0/1`이다.
- 영상 상세 요청부터 quota 대기면 `videoProgress=0/1`이다.
- 댓글이 없거나 이미 최신이면 `commentProgress=1/1`로 정상 skip 처리한다.
- 자식 실패는 성공 건수에 들어가지 않고 `failed`에만 들어간다.
- 50개 ID는 `videos.list` 1회, 51개는 2회 호출한다.
- batch 중 일부 ID가 미반환되어도 반환된 영상은 저장된다.
- legacy `jobKind=video` 작업은 배포 후에도 재개된다.
- retry와 worker crash 후에도 phase counter가 중복 증가하지 않는다.
- comments는 공식 YouTube API client, transcripts는 SearchAPI client를 계속 사용한다.

### 9.2 Web

- 보유 영상 691개, 최신 작업 0/17인 키워드는 `보유 영상 691개 · 이번 수집 0/17 · 할당량 대기`로 보인다.
- 5/1,098은 `0%`가 아니라 `<1% · 5/1,098`로 보인다.
- `waiting_quota`와 `waiting_retry`가 `진행 중`으로 합쳐지지 않는다.
- 작업 중 `videoProgress`가 증가하면 terminal 전에도 영상 목록이 갱신된다.
- 댓글 progress가 증가해도 영상 목록 요청이 과도하게 반복되지 않는다.
- 한글 채널명·키워드, 긴 숫자, 모바일 폭에서 열이 깨지지 않는다.

### 9.3 성능·운영

- 자식 10,000개 parent의 phase 집계 query latency와 실행 계획을 측정한다.
- polling 중 API·DB QPS가 현재 대비 허용 범위를 넘지 않는다.
- 1,000개 영상 채널의 공식 video detail 요청이 약 20회 수준인지 quota ledger로 확인한다.
- worker 2개에서도 채널 하나의 대량 fan-out이 다른 소스를 영구 starvation시키지 않는다.

## 10. 배포 계획

1. DB migration 없이 additive API contract를 배포한다.
2. legacy/new jobKind를 모두 이해하는 worker를 배포한다.
3. 진행률 v2를 읽되 legacy checkpoint fallback이 가능한 web을 배포한다.
4. 배포 이후 생성되는 신규 parent부터 최대 50개 `video_batch`를 사용한다.
5. 운영 canary로 작은 키워드, 50개 이하 채널, 1,000개 이상 채널을 순서대로 실행한다.
6. DB 저장량, 화면 점진 반영, official quota ledger, SearchAPI usage ledger를 함께 확인한다.

별도 runtime flag 없이 `jobKind` 자체를 호환 경계로 사용한다. 기존 `video` 자식은 legacy 경로로 계속 처리되고 신규 작업만 `video_batch`와 `comment`를 생성한다. rollback은 배포 스크립트가 기록한 이전 immutable image로 수행하며, 저장된 영상·댓글·대본은 삭제하지 않는다.

## 11. 완료 기준

- 기존 영상이 있는데 최신 작업이 0건이라는 이유만으로 `영상 수집률 0%`만 단독 표시되지 않는다.
- 누적 보유량, 이번 영상 진행, 이번 댓글 진행, 전체 작업 상태가 서로 독립적으로 보인다.
- 영상 정보 저장 후 10초 이내에 선택 소스 목록에서 확인할 수 있다.
- 댓글 quota 대기 중에도 이미 저장된 영상 progress와 영상 목록은 유지된다.
- `waiting_quota`, `waiting_retry`, 실제 `running`이 사용자에게 구분된다.
- 1,098개 영상 metadata 보강이 약 22회의 `videos.list` 요청으로 수행된다.
- 기존 active job과 저장 데이터는 중단·삭제·중복 생성 없이 처리된다.
- API/worker 전체 테스트, web typecheck/build, 대규모 parent 성능 테스트, 원격 canary, 실제 브라우저 E2E가 모두 통과한다.

## 12. 이번 계획에서 제외하는 범위

- 댓글 provider를 SearchAPI comments로 전환하는 작업
- 기존 댓글의 timestamp나 provider schema 변경
- 기존 저장 영상·댓글·대본의 일괄 재수집
- worker 개수를 무조건 늘리는 방식의 해결
- 자동 6시간 수집 주기와 source별 `지금 재수집` 정책 변경
- SearchAPI 장애 시 공식 YouTube discovery로 자동 fallback

6시간 자동 수집과 사용자가 누르는 즉시 재수집은 그대로 유지한다. 이번 계획은 그 작업의 내부 처리 효율과 사용자에게 보이는 진행 상태를 정확하게 만드는 데 한정한다.
