# Monitube Analysis 화면 설계 v1

> 목적: 사용자가 자신이 볼 수 있는 모든 수집 영상과 공개 댓글을 하나의 화면에서 비교하고, 어떤 채널·키워드·영상이 성과와 반응을 만들었는지 빠르게 파악하게 한다.

## 1. 제품 결정

### 1.1 화면의 역할

`Analysis`는 수집 작업을 관리하는 화면이 아니라 **저장된 영상 성과와 공개 댓글 반응을 탐색하고 비교하는 화면**이다.

사용자가 이 화면에서 답을 얻어야 하는 질문은 네 가지다.

1. 어떤 영상이 수집됐고 현재 성과는 어떠한가?
2. 영상과 댓글 활동은 언제, 어디에서 발생했는가?
3. 어떤 채널·키워드·영상이 성과와 반응을 만들었는가?
4. 댓글에서 어떤 단어와 실제 의견이 나타났는가?

수집 진행률, quota, 실패 원인은 기존 `Status`와 source overview에 남긴다. Analysis에는 영상 snapshot 시각, 분석 결과의 최신성, 부분 수집 여부처럼 수치를 해석하는 데 필요한 상태만 표시한다.

### 1.2 라우트와 진입점

- 신규 정식 라우트: `/analysis`
- 기존 `/insights`: `/analysis`로 redirect하여 북마크 호환
- 진입점: 상단 utility bar 우측, 설정 버튼 바로 왼쪽
- 버튼 구성: `DocumentChartBarIcon` + `Analysis`
- 활성 상태: `moss-soft` 배경과 `moss` 텍스트
- 오렌지는 기존 `수집 대상 추가` CTA와 선택된 핵심 데이터 시리즈에만 사용

좌측 사이드바에는 기존 네 개 메뉴를 유지한다. Analysis를 상단 우측에 두는 이유는 특정 collection 메뉴가 아니라 전체 워크스페이스를 가로지르는 결과 보기이기 때문이다.

### 1.3 기본 범위

최초 진입 기본값은 다음과 같다.

| 항목 | 기본값 | 이유 |
| --- | --- | --- |
| 보기 | Overview | 영상 성과와 댓글 반응을 함께 요약 |
| 분석 범위 | 전체 | 워크스페이스 전체 상태를 먼저 보여 줌 |
| 기간 | 최근 30일 | 활동 변화를 읽기 좋은 기본 범위 |
| 날짜 기준 | 보기별 게시일 | 영상은 영상 게시일, 댓글은 댓글 게시일 |
| 영상 통계 | 최신 snapshot | 현재 API가 제공하는 실제 통계만 사용 |
| 추이 단위 | 일별 | 30일 범위에 적합 |
| 비교 | 이전 30일 | KPI 변화의 기준을 명확히 함 |

기간에 해당하는 영상이나 댓글이 없으면 자동으로 `전체 기간`으로 바꾸지 않는다. 빈 상태에서 사용자가 직접 전체 기간을 선택하게 한다.

`조회수`, `좋아요`, `YouTube 표시 댓글`은 최신 저장 snapshot의 누적값이다. 따라서 v1에서는 이를 기간 중 증가량처럼 표현하지 않는다.

### 1.4 기간 적용 규칙

같은 기간 필터라도 보기마다 기준 대상이 다르다.

| 보기/영역 | 기간 기준 | 연결 지표 |
| --- | --- | --- |
| Overview 영상 영역 | 영상 `published_at` | 선택 영상의 최신 누적 통계와 전체 연결 댓글 |
| Overview 댓글 영역 | 댓글 `published_at` | 선택 기간에 게시된 수집 댓글 |
| Videos | 영상 `published_at` | 선택 영상의 최신 통계와 전체 연결 댓글 |
| Comments | 댓글 `published_at` | 선택 기간에 게시된 댓글과 연결 영상 |

Videos의 `연결 수집 댓글`은 선택된 영상에 현재 저장된 전체 댓글 수다. 댓글 자체의 게시 기간까지 제한하려면 Comments 보기로 이동한다. 각 panel subtitle에 날짜 기준을 표시하여 같은 `30일` 필터가 서로 다른 의미로 보이지 않게 한다.

## 2. 정보 구조

데스크톱은 기존 216px 사이드바와 main 영역을 유지하고, main 내부에 12-column grid를 사용한다.

| 순서 | 영역 | 데스크톱 폭 | 주요 내용 |
| --- | --- | --- | --- |
| 1 | Utility bar | 12 columns | breadcrumb, Analysis 진입점, 설정 |
| 2 | 페이지 헤더 | 12 columns | 제목, 설명, 분석 최신성 |
| 3 | 보기 전환 | 12 columns | Overview, Videos, Comments |
| 4 | 범위 필터 | 12 columns | 전체, 채널, 키워드, 대상, 기간 |
| 5 | KPI | 5등분 | 현재 보기의 핵심 영상·댓글 지표 |
| 6 | 주요 추이 | 8 columns | 업로드 또는 댓글 게시 추이 |
| 7 | 구성 요약 | 4 columns | 성과 분포 또는 원댓글/답글 비율 |
| 8 | 채널·키워드 비교 | 12 columns | 영상과 댓글 지표를 함께 비교 |
| 9 | 상세 분석 | 12 columns | 영상 순위 또는 댓글 내용 분석 |
| 10 | 데이터 범위 | 12 columns | snapshot, coverage, 표본, 부분 수집 안내 |

페이지의 핵심 시선 흐름은 `보기 선택 → 범위 → 규모 → 시간 변화 → 원인 대상 → 실제 영상/댓글` 순서다.

## 3. 상단 영역

### 3.1 페이지 헤더

- kicker: `VIDEO & COMMENT ANALYSIS`
- 제목: `Analysis`
- 설명: `수집된 영상 성과와 공개 댓글 반응을 채널·키워드별로 비교합니다.`
- 우측 상태:
  - `영상 통계 2026.07.30 14:20 기준`
  - `댓글 분석 2026.07.30 14:22 생성`
  - `최신`, `분석 중`, `이전 결과`, `부분 데이터`, `실패` 중 하나의 상태 pill
  - 새로고침 아이콘 버튼

영상 snapshot과 댓글 단어 분석의 생성 시각은 서로 다를 수 있으므로 한 시각으로 합치지 않는다. `topWords`가 오래된 경우 주요 단어 패널 안에서 별도 상태를 표시한다.

### 3.2 보기 전환

페이지 헤더 아래에 같은 route 안에서 동작하는 큰 tab을 둔다.

- `Overview`
- `Videos`
- `Comments`

tab 상태는 URL의 `view` query에 저장한다.

```text
/analysis?view=overview
/analysis?view=videos
/analysis?view=comments
```

#### Overview

영상과 댓글을 같은 무게로 요약한다.

- 영상 수와 최신 snapshot 누적 성과
- 수집 공개 댓글 수와 답글 비율
- 업로드와 댓글 활동의 간단한 추이
- 채널·키워드별 통합 비교
- 조회수 대비 댓글 반응이 큰 영상

#### Videos

현재 저장된 영상 메타데이터와 최신 통계에 집중한다.

- 게시일, 조회수, 좋아요, YouTube 표시 댓글
- 수집 공개 댓글
- 영상 길이
- 채널·키워드 귀속
- 최신 snapshot 기준 순위와 분포

#### Comments

댓글 활동과 내용 탐색에 집중한다.

- 원댓글과 답글
- 작성자, 좋아요, 게시일
- 주요 단어와 실제 댓글
- 영상·채널·키워드별 댓글 구성

### 3.3 필터 바

필터 바는 한 줄 surface로 만들고 페이지를 스크롤하면 main 상단에 sticky 처리한다.

#### 분석 범위

세그먼트 버튼:

- `전체`
- `채널`
- `키워드`

`채널` 또는 `키워드`를 선택하면 바로 오른쪽에 대상 multi-select가 나타난다.

- 초기 대상: `전체 채널` 또는 `전체 키워드`
- 선택 항목은 최대 두 개까지 chip으로 표시
- 세 개 이상이면 `외 2개` 형태로 축약
- 검색 가능한 checkbox 목록 사용
- 선택 결과가 없으면 적용 버튼을 비활성화

#### 기간

- preset: `7일`, `30일`, `90일`, `전체`
- `직접 설정` 시 시작일/종료일 date picker
- 현재 기간 아래에 비교 범위를 작게 표시
  - 예: `이전 기간 2026.06.01–06.30과 비교`
- 미래 날짜는 선택 불가
- 최대 직접 설정 범위는 전체 보유 기간까지 허용

#### 보기별 추가 필터

`Videos`:

- 지표: `조회수`, `좋아요`, `YouTube 댓글`, `연결 수집 댓글`
- 영상 길이: `전체`, `Shorts 후보`, `4–20분`, `20분 이상`

`Comments`:

- `전체`
- `원댓글`
- `답글`

Overview에는 상세 필터를 노출하지 않고 각 panel의 `자세히 보기`를 통해 Videos 또는 Comments로 이동한다.

#### 보조 동작

- `필터 초기화`
- 활성 필터가 기본값과 다를 때만 노출
- 적용된 필터는 URL query와 동기화하여 새로고침과 공유 시 유지

권장 URL 예시:

```text
/analysis?view=videos&scope=channel&targetIds=id1,id2&from=2026-07-01&to=2026-07-30&metric=views
```

## 4. KPI 카드

첫 줄은 같은 높이의 다섯 카드다. 카드 구성은 현재 보기에 맞춰 바뀐다.

### 4.1 Overview KPI

| KPI | 정의 | 보조 정보 | 클릭 동작 |
| --- | --- | --- | --- |
| 수집 영상 | 기간 내 게시된 고유 영상 수 | 최근 업로드 | Videos로 이동 |
| 최신 누적 조회수 | 대상 영상의 최신 `view_count` 합 | snapshot 기준 시각 | 조회수순 Videos |
| 최신 누적 좋아요 | 대상 영상의 최신 `like_count` 합 | 조회수 1천 회당 좋아요 | 좋아요순 Videos |
| 수집 공개 댓글 | 기간 내 게시된 고유 comment ID 수 | 이전 기간 대비 증감 | Comments로 이동 |
| 댓글 영상 | 댓글이 1개 이상인 고유 영상 수 | 영상당 수집 댓글 중앙값 | 댓글순 Videos |

영상 KPI의 기간은 영상 게시일, 댓글 KPI의 기간은 댓글 게시일을 사용한다. 카드 하단에 각각 `영상 게시일 기준` 또는 `댓글 게시일 기준`을 표시한다.

### 4.2 Videos KPI

| KPI | 정의 | 보조 정보 |
| --- | --- | --- |
| 수집 영상 | 기간 내 게시된 고유 영상 수 | 이전 기간 영상 수 |
| 최신 누적 조회수 | 영상별 최신 조회수 합 | 영상당 중앙 조회수 |
| 최신 누적 좋아요 | 영상별 최신 좋아요 합 | 조회수 1천 회당 좋아요 |
| YouTube 표시 댓글 | 영상별 최신 `comment_count` 합 | 연결 수집 댓글과 별개 |
| 연결 수집 댓글 | 해당 영상에 저장된 전체 댓글 수 | 영상당 중앙 수집 댓글 |

합계가 한두 개의 대형 영상에 치우칠 수 있으므로 조회수와 댓글 수에는 평균보다 중앙값을 보조 지표로 사용한다.

### 4.3 Comments KPI

| KPI | 정의 | 보조 정보 | 클릭 동작 |
| --- | --- | --- | --- |
| 수집 공개 댓글 | 기간 내 저장된 고유 comment ID 수 | 이전 기간 대비 증감 | 실제 댓글 목록 전체 |
| 댓글이 달린 영상 | 댓글이 1개 이상인 고유 영상 수 | 영상당 댓글 중앙값 | 상위 영상 영역으로 이동 |
| 확인 가능한 작성자 | 비어 있지 않은 `author_channel_id`의 고유 수 | 작성자 식별 coverage | 작성자 설명 tooltip |
| 답글 비율 | 답글 수 ÷ 전체 댓글 수 | 원댓글/답글 건수 | 댓글 유형을 답글로 변경 |
| 평균 좋아요 | 댓글 `like_count` 평균 | 좋아요 1개 이상 댓글 비율 | 좋아요순 댓글 보기 |

### KPI 표현 규칙

- 값은 tabular number로 정렬
- 큰 수는 화면에서 `2.85M`, tooltip에서 `2,850,797`처럼 전체 값 제공
- 비교값은 `이전 30일 대비 +12.4%`처럼 기준을 문장으로 표시
- 이전 기간의 분모가 0이면 퍼센트를 만들지 않고 `이전 기간 0건`으로 표시
- `확인 가능한 작성자`는 모든 댓글 작성자를 의미하지 않으므로 `고유 작성자`라고 단정하지 않는다
- `YouTube 표시 댓글`과 `수집 공개 댓글`을 같은 수치처럼 합치지 않는다
- 최신 누적값에는 반드시 snapshot 기준 시각을 표시한다
- 부분 수집이면 카드 우측 상단에 작은 `부분 데이터` indicator를 공통 표시

## 5. 영상 분석

Videos 보기는 현재 저장된 영상 메타데이터와 최신 `video_stat_snapshots`를 사용한다.

### 5.1 업로드 추이

- 제목: `영상 업로드 추이`
- subtitle: `영상 게시일 기준`
- 기본 mark: 세로 bar
- X축: 게시일 bucket
- Y축: 수집 영상 수
- bucket:
  - 31일 이하: 일별
  - 32–180일: 주별
  - 181일 이상: 월별
- 그룹 보기: 없음, 채널별, 키워드별

이 차트는 수집을 수행한 날짜가 아니라 실제 영상이 게시된 날짜를 보여 준다. 과거 영상이 최근에 수집돼도 과거 게시일 bucket에 포함한다.

### 5.2 최신 영상 성과

제목: `최신 snapshot 영상 성과`

metric switch:

- 조회수
- 좋아요
- YouTube 표시 댓글
- 연결 수집 댓글
- 조회수 1천 회당 좋아요
- 조회수 1천 회당 연결 댓글

기본 표현은 상위 10개 영상의 가로 막대다.

- 막대: 선택 지표
- label: 영상 제목과 채널
- 보조값: 게시일과 snapshot 시각
- 행 클릭: 기존 `VideoModal`
- `조회수 1천 회당` 지표는 분모가 0이면 `—`
- 비율 지표는 플랫폼 공식 engagement rate로 부르지 않음

누적 조회수 상위와 반응 밀도 상위는 의미가 다르므로 metric switch에서 명확히 분리한다.

### 5.3 조회수와 댓글 반응 관계

- 제목: `조회수와 댓글 반응`
- 기본 mark: scatter
- X축: 최신 누적 조회수, log scale 선택 가능
- Y축: 영상에 연결된 전체 수집 공개 댓글 수
- point size: 최신 좋아요 수
- color: 채널 또는 키워드 그룹
- point label: hover/focus 시 영상 제목

이 차트는 인과관계나 상관계수를 단정하지 않고, 조회수 규모에 비해 댓글 반응이 큰 영상을 찾는 용도로 사용한다.

- point 클릭: 영상 상세
- `주목할 영상만 보기`: 조회수·댓글 중앙값 이상인 quadrant 강조
- 데이터가 5개 미만이면 scatter 대신 설명형 영상 목록
- 색상 그룹은 상위 5개 + 기타

### 5.4 영상 길이 분포

`duration_seconds`가 있는 영상만 사용한다.

| 구간 | 의미 |
| --- | --- |
| 60초 이하 | Shorts 후보 |
| 61초–4분 | 짧은 영상 |
| 4–20분 | 일반 영상 |
| 20분 이상 | 긴 영상 |
| 알 수 없음 | duration 없음 |

각 구간은 다음 값을 전환해 볼 수 있다.

- 영상 수
- 영상당 중앙 조회수
- 영상당 중앙 연결 댓글

`60초 이하`는 길이만으로 추정한 값이므로 `Shorts`라고 단정하지 않고 `Shorts 후보`로 표시한다.

### 5.5 영상 상세 테이블

기본 20개, cursor 기반 추가 로드.

| 열 | 내용 |
| --- | --- |
| 영상 | 썸네일, 제목, 채널 |
| 게시일 | `published_at` |
| 길이 | `duration_seconds` |
| 조회수 | 최신 `view_count` |
| 좋아요 | 최신 `like_count` |
| YouTube 댓글 | 최신 `comment_count` |
| 연결 수집 댓글 | 저장된 전체 공개 댓글 수 |
| 좋아요/1천 조회 | 파생 지표 |
| 연결 수집 댓글/1천 조회 | 파생 지표 |
| 통계 기준 | 최신 snapshot `fetched_at` |

정렬:

- 최신 게시
- 조회수
- 좋아요
- YouTube 표시 댓글
- 연결 수집 댓글
- 좋아요/1천 조회
- 연결 수집 댓글/1천 조회

YouTube가 영상에 표시하는 댓글 수와 Monitube가 실제로 수집한 댓글 수는 항상 별도 열에 둔다.

### 5.6 snapshot 성장 분석 경계

현재 DB에는 `video_stat_snapshots` 이력이 있지만 web API는 최신값만 노출한다.

다음 지표는 snapshot history API가 추가된 뒤 제공한다.

- 기간 중 조회수 증가
- 기간 중 좋아요 증가
- 기간 중 YouTube 표시 댓글 증가
- 일평균 조회 증가 속도
- 성장률 상위 영상
- 채널별 누적 성과 추이

v1에서 최신 누적 조회수를 `최근 30일 조회수`처럼 표현하거나, 영상 게시일 그룹의 누적 조회수 차이를 성장률로 표현하지 않는다.

## 6. 댓글 분석

### 6.1 댓글 활동 추이

#### 기본 차트

- 제목: `댓글 활동 추이`
- subtitle: `댓글 게시일 기준`
- 기본 mark: line + 낮은 opacity의 area
- X축: 날짜
- Y축: 수집 공개 댓글 수
- series:
  - 기본 `전체 댓글`
  - 토글 시 `원댓글`, `답글`
- 범위에 따른 bucket:
  - 31일 이하: 일별
  - 32–180일: 주별
  - 181일 이상: 월별

#### 그룹 비교

차트 우측 상단의 `그룹 보기`에서 다음을 선택한다.

- 없음
- 채널별
- 키워드별

그룹 series는 선택 필터 안에서 댓글 수 상위 5개까지만 그리고 나머지는 `기타`로 합친다. 범례에는 그룹 이름과 기간 합계를 같이 표시한다.

키워드별 집계에서는 같은 영상이 여러 키워드 source에 포함될 수 있다. 이 경우 키워드별 행은 각각의 source에 귀속되며 합계가 `모든 댓글`의 고유 합계보다 클 수 있다는 안내를 차트 아래에 표시한다.

#### 상호작용

- hover/focus: 날짜, 전체 수, 원댓글 수, 답글 수 표시
- 지점 클릭: 해당 bucket과 현재 필터가 적용된 실제 댓글 패널로 이동
- 범례 클릭: series 표시/숨김
- 차트를 읽지 못하는 사용자를 위해 `표로 보기` 토글 제공
- 표는 날짜, 전체, 원댓글, 답글 열을 제공

### 6.2 댓글 구성 요약

추이 차트 오른쪽의 보조 패널이다.

#### 기본 상태

- 원댓글과 답글의 비율을 100% stacked bar로 표시
- 댓글 수 상위 채널 또는 키워드 5개를 가로 막대로 표시
- 각 막대는 건수와 전체 대비 비중을 함께 표시

#### 범위별 전환

- `모든 댓글`: `채널 구성`
- `채널`: 선택 채널별 구성
- `키워드`: 선택 키워드별 구성

원형 차트는 항목 비교와 긴 한글 라벨에 불리하므로 사용하지 않는다.

## 7. 채널·키워드 통합 비교

### 7.1 제목과 모드

- 제목: `대상별 영상·댓글 성과`
- 모드: `채널` / `키워드`
- `모든 댓글` 범위에서는 채널 모드가 기본
- `Overview`에서는 영상과 댓글 핵심 열을 함께 표시
- `Videos`에서는 영상 성과 열을 우선 표시
- `Comments`에서는 댓글 반응 열을 우선 표시
- 행 수: 기본 10개, cursor 기반 `더 보기`

### 7.2 채널 모드 열

| 열 | 정의 |
| --- | --- |
| 채널 | 썸네일, title, handle |
| 수집 영상 | 현재 필터의 고유 영상 수 |
| 최신 누적 조회수 | 영상별 최신 조회수 합 |
| 중앙 조회수 | 영상별 최신 조회수 중앙값 |
| 최신 누적 좋아요 | 영상별 최신 좋아요 합 |
| YouTube 표시 댓글 | 영상별 최신 comment count 합 |
| 연결 수집 댓글 | 선택 영상에 연결된 전체 고유 댓글 수 |
| 영상당 연결 댓글 | 연결 댓글 ÷ 수집 영상 |
| 답글 비율 | 답글 ÷ 전체 댓글 |
| 상위 영상 | 현재 선택 metric의 1위 영상 |

### 7.3 키워드 모드 열

| 열 | 정의 |
| --- | --- |
| 키워드 | source label과 주요 검색 조건 |
| 발견 영상 | source에 귀속된 고유 영상 수 |
| 발견 채널 | source 영상의 고유 채널 수 |
| 최신 누적 조회수 | 발견 영상의 최신 조회수 합 |
| 중앙 조회수 | 발견 영상의 최신 조회수 중앙값 |
| 최신 누적 좋아요 | 발견 영상의 최신 좋아요 합 |
| 연결 댓글 | 해당 키워드 source의 영상에 연결된 수집 댓글 수 |
| 영상당 댓글 | 연결 댓글 ÷ 발견 영상 |
| 중복 영상 | 다른 선택 키워드와 겹치는 영상 수 |
| coverage | complete, limited, unknown |
| 상위 영상 | 현재 선택 metric의 1위 영상 |

### 7.4 테이블 동작

- 헤더 클릭 정렬
- 행 클릭 시 해당 채널/키워드를 현재 분석 범위로 적용
- 상위 영상 클릭 시 기존 `VideoModal` 사용
- 모바일에서는 현재 보기 기준 `대상`과 핵심 지표 두 개만 노출하고 나머지는 행 상세 sheet에서 표시
- 키워드 행 합산 시 중복이 발생할 수 있음을 표 하단에 상시 안내

최신 누적 조회수와 좋아요는 각 대상에 속한 영상의 최신 snapshot을 합한 값이다. 선택 기간의 증가량이 아니다.

## 8. 주요 단어와 실제 댓글

두 패널은 하나의 탐색 흐름으로 동작한다.

### 8.1 주요 단어

- 제목: `댓글 주요 단어`
- 최대 20개
- 표시: 순위, 단어, 출현 횟수, 상대 막대
- 워드클라우드는 사용하지 않음
- 단어 count는 `댓글 수`가 아니라 token의 `출현 횟수`로 표기
- 단어 클릭 시 선택 상태를 오렌지 soft surface로 표시
- 선택 단어를 실제 댓글 패널의 검색 조건으로 전달

패널 하단에는 반드시 다음을 표시한다.

- `50,000 / 2,850,797개 댓글 분석`
- `표본 1.75%`
- `분석 생성 2026.07.30 14:20`
- 상태: `최신`, `분석 중`, `이전 결과`, `실패`

`topWordsStatus=building`이면 이전 결과가 있을 때 그대로 보여 주고 `새 분석 처리 중`을 표시한다. 이전 결과가 없으면 skeleton 대신 설명형 empty state를 사용한다.

### 8.2 실제 댓글

- 기본 제목: `대표 댓글`
- 단어 선택 후: `“선택 단어”가 포함된 댓글`
- 정렬:
  - 관련도
  - 좋아요순
  - 최신순
  - 답글 많은 순
- 한 번에 10개, cursor 기반 추가 로드
- 각 행:
  - 작성자명
  - 상대 게시 시각
  - 댓글 본문 3줄
  - 좋아요 수
  - 저장된 답글 수
  - 영상 제목과 채널
- 행 클릭 시 기존 댓글 상세 흐름을 재사용
- 검색어는 대소문자 차이를 무시하되 UI에는 사용자가 선택한 원문을 유지

대표 댓글은 의미를 추론해 AI가 선정하는 것이 아니라 선택 정렬 규칙의 상위 댓글이다.

## 9. Overview의 주목할 영상

Overview 하단에는 영상과 댓글을 함께 해석할 수 있는 compact table을 둔다.

- 제목: `주목할 영상`
- 기본 8개
- metric switch:
  - 조회수 상위
  - 좋아요 상위
  - 연결 수집 댓글 상위
  - 조회수 대비 댓글 상위
- `모든 영상 보기`: 현재 필터를 유지한 채 Videos tab으로 이동

| 열 | 내용 |
| --- | --- |
| 영상 | 썸네일, 제목, 채널 |
| 게시일 | 영상 게시일 |
| 조회수 | 최신 누적값 |
| 좋아요 | 최신 누적값 |
| YouTube 댓글 | 최신 표시값 |
| 연결 수집 댓글 | 선택 영상에 저장된 전체 공개 댓글 |
| 조회수 1천 회당 연결 댓글 | 반응 밀도 참고값 |

조회수가 적은 영상에서 비율이 과도하게 커지는 문제를 줄이기 위해 `조회수 대비 댓글 상위`는 최소 조회수 기준을 적용하고 그 기준을 panel note에 표시한다.

행 클릭은 기존 영상 상세 모달을 연다. YouTube가 영상에 표시하는 `commentCount`와 Monitube의 `연결 수집 댓글`은 같은 열에 섞지 않는다.

## 10. 데이터 범위와 신뢰도

페이지 최하단에 접을 수 있는 `이 분석의 데이터 범위` 영역을 둔다.

표시 항목:

- 분석 대상 source/target 수
- 분석 대상 영상 수
- 영상 통계가 있는 영상 수와 비율
- 최신/가장 오래된 영상 snapshot 시각
- 영상 통계가 없는 영상 수
- 정확 집계 댓글 수
- 작성자 식별 가능 댓글 수와 비율
- 주요 단어 표본 댓글 수와 비율
- 분석 생성 시각
- 기준 data version
- 부분 수집 여부
- keyword coverage 상태
- 제외된 항목:
  - 삭제된 데이터
  - 접근 권한이 없는 target
  - 통계 snapshot이 없는 영상
  - 게시일이 없어 기간 필터를 적용할 수 없는 영상
  - 작성일이 없고 기간 필터를 적용할 수 없는 댓글

사용자용 설명:

> 모든 수치는 현재 Monitube에 저장된 공개 데이터 기준입니다. 영상 조회수·좋아요·YouTube 댓글은 표시된 최신 snapshot의 누적값이며, 수집 공개 댓글은 YouTube 전체 댓글 수와 다를 수 있습니다. 주요 단어는 표시된 표본 범위에서 계산됩니다.

API key, quota key, Google project, 내부 worker 이름은 표시하지 않는다.

## 11. 상태 설계

| 상태 | 화면 표현 | 사용자 행동 |
| --- | --- | --- |
| source 없음 | Analysis 전체 빈 상태 | `수집 대상 추가` |
| 기간 내 영상·댓글 없음 | KPI는 0, 차트와 표는 설명형 빈 상태 | `전체 기간 보기` |
| 영상만 있음 | Videos 정상 표시, Comments 설명형 빈 상태 | 댓글 포함 재수집 |
| 댓글만 필터 결과 없음 | 영상 결과 유지, 댓글 panel만 빈 상태 | 댓글 필터 초기화 |
| 영상 snapshot 없음 | 영상 메타데이터 표시, 통계값은 `—` | 다음 수집 결과 기다림 |
| loading | 기존 데이터 유지 + 패널 단위 progress | 필터 계속 사용 가능 |
| 필터 변경 | 이전 결과 opacity 처리, 필터 bar에 로딩 표시 | 변경 취소 가능 |
| partial | KPI와 패널에 공통 indicator, 범위 영역 펼침 | 부분 수집 이유 확인 |
| top words building | 정확 KPI 유지, 단어 패널만 처리 중 | 이전 단어 결과 확인 |
| stale | 이전 결과임을 생성 시각과 함께 표시 | 새로고침 |
| API 오류 | 성공한 패널은 유지, 실패 패널만 retry | 패널 재시도 |
| 권한 변경 | 제외된 target을 제거하고 안내 toast | 현재 권한 범위로 계속 |

필터 하나의 실패가 전체 페이지를 빈 화면으로 만들지 않게 한다.

## 12. 반응형 설계

### 1200px 이상

- 12-column grid
- KPI 5개 한 줄
- 업로드/댓글 추이 8 columns + 구성 요약 4 columns
- Videos scatter 7 columns + 영상 길이 분포 5 columns
- 주요 단어 5 columns + 실제 댓글 7 columns

### 768–1199px

- 사이드바 compact rail
- KPI 3 + 2 형태
- 추이, scatter, 구성 요약을 각각 한 줄
- 주요 단어와 실제 댓글을 각각 한 줄
- filter bar는 두 줄 허용

### 767px 이하

- Analysis 진입점은 icon + accessible label
- Overview, Videos, Comments tab은 가로 3등분
- 필터 bar에는 현재 범위와 기간만 표시
- 나머지 필터는 full-screen filter sheet
- KPI는 2열, 마지막 카드는 전체 폭
- 차트 기본 높이 260px
- 비교 테이블은 핵심 3열만 표시
- 댓글 본문은 2줄, 상세는 기존 modal

모바일에서도 차트 가로 스크롤을 사용하지 않는다.

## 13. 접근성

- 모든 filter control은 visible label을 가짐
- segment button은 `aria-pressed`, 현재 route는 `aria-current`
- 차트에는 동일 데이터를 제공하는 표 전환 제공
- 차트 hover 정보는 keyboard focus에서도 동일하게 제공
- 색만으로 원댓글/답글/상태를 구분하지 않고 선 모양, label, icon을 병행
- table header에는 정렬 상태를 `aria-sort`로 제공
- 수치 축약값에는 전체 숫자를 accessible name 또는 tooltip으로 제공
- sticky filter bar가 focus target을 가리지 않도록 scroll margin 적용
- loading과 분석 완료는 과도하지 않은 `aria-live="polite"` 영역 하나에서 알림

## 14. API 계약

### 14.1 대시보드 조회

```http
GET /v1/analysis/overview
```

query:

| 이름 | 값 |
| --- | --- |
| `view` | `overview`, `videos`, `comments` |
| `scope` | `all`, `channel`, `keyword` |
| `targetIds` | comma-separated target IDs |
| `from` | inclusive ISO date |
| `to` | inclusive ISO date |
| `commentType` | `all`, `top_level`, `reply` |
| `videoMetric` | `views`, `likes`, `youtube_comments`, `collected_comments` |
| `duration` | `all`, `shorts_candidate`, `short`, `standard`, `long` |
| `bucket` | `day`, `week`, `month`, 서버 자동 선택 가능 |
| `groupBy` | `none`, `channel`, `keyword` |
| `compare` | `previous`, `none` |

response 예시:

```json
{
  "query": {
    "view": "overview",
    "scope": "all",
    "targetIds": [],
    "from": "2026-07-01",
    "to": "2026-07-30",
    "commentType": "all",
    "videoMetric": "views",
    "duration": "all",
    "bucket": "day",
    "groupBy": "channel"
  },
  "videoSummary": {
    "dateBasis": "videoPublishedAt",
    "videoCount": 1842,
    "totalViewCount": 812400000,
    "medianViewCount": 128400,
    "totalLikeCount": 23480000,
    "likesPerThousandViews": 28.9,
    "youtubeCommentCount": 142200,
    "attachedCollectedCommentCount": 136820,
    "latestVideoPublishedAt": "2026-07-29T08:00:00Z",
    "latestSnapshotAt": "2026-07-30T05:20:00Z"
  },
  "commentSummary": {
    "dateBasis": "commentPublishedAt",
    "commentCount": 128430,
    "commentedVideoCount": 1320,
    "identifiedAuthorCount": 68410,
    "identifiedAuthorCoverage": 0.91,
    "topLevelCount": 94320,
    "replyCount": 34110,
    "replyShare": 0.2656,
    "averageLikeCount": 1.82,
    "likedCommentShare": 0.234,
    "latestCommentPublishedAt": "2026-07-30T05:24:00Z"
  },
  "comparison": {
    "periodFrom": "2026-06-01",
    "periodTo": "2026-06-30",
    "publishedVideoCountChange": 0.082,
    "commentCountChange": 0.124,
    "identifiedAuthorCountChange": 0.107,
    "replyShareChangePoints": 0.018,
    "averageLikeCountChange": -0.04
  },
  "videoPublishTrend": [
    {
      "period": "2026-07-01",
      "videoCount": 18,
      "seriesId": "channel-id",
      "seriesLabel": "채널 이름"
    }
  ],
  "commentTrend": [
    {
      "period": "2026-07-01",
      "commentCount": 4210,
      "topLevelCount": 3190,
      "replyCount": 1020,
      "seriesId": "channel-id",
      "seriesLabel": "채널 이름"
    }
  ],
  "commentComposition": {
    "topLevelCount": 94320,
    "replyCount": 34110,
    "topGroups": [
      {
        "id": "channel-id",
        "label": "채널 이름",
        "commentCount": 30210,
        "share": 0.2352
      }
    ]
  },
  "videoPerformance": {
    "metric": "views",
    "topVideos": [],
    "scatter": [],
    "durationBuckets": []
  },
  "breakdown": {
    "mode": "channel",
    "rows": [],
    "nextCursor": null,
    "mayContainCrossRowOverlap": false
  },
  "topWords": {
    "items": [
      { "label": "리뷰", "count": 4821 }
    ],
    "sampledComments": 50000,
    "totalComments": 128430,
    "sampleRatio": 0.3893,
    "status": "fresh",
    "generatedAt": "2026-07-30T05:30:00Z"
  },
  "coverage": {
    "status": "complete",
    "partialData": false,
    "visibleTargetCount": 18,
    "includedTargetCount": 18,
    "videoCount": 1842,
    "videosWithStatistics": 1836,
    "videoStatisticsCoverage": 0.9967,
    "oldestVideoSnapshotAt": "2026-07-29T23:10:00Z",
    "latestVideoSnapshotAt": "2026-07-30T05:20:00Z",
    "commentCount": 128430,
    "dataVersion": 42,
    "generatedAt": "2026-07-30T05:30:00Z"
  }
}
```

영상 누적값은 snapshot 기간의 증가량이 아니므로 `comparison`에 조회수·좋아요 변화율을 넣지 않는다.

### 14.2 영상 상세 목록

```http
GET /v1/analysis/videos
```

대시보드와 동일한 scope/date query에 다음을 추가한다.

| 이름 | 값 |
| --- | --- |
| `sort` | `published`, `views`, `likes`, `youtube_comments`, `collected_comments`, `likes_per_view`, `comments_per_view` |
| `duration` | 영상 길이 구간 |
| `channelId` | 선택 채널 |
| `keywordTargetId` | 선택 키워드 target |
| `cursor` | keyset cursor |
| `limit` | 기본 20, 최대 50 |

각 영상은 기존 `CollectedVideo` 필드에 다음 분석 필드를 추가한다.

- `attachedCollectedCommentCount`
- `identifiedAuthorCount`
- `topLevelCommentCount`
- `replyCount`
- `likesPerThousandViews`
- `collectedCommentsPerThousandViews`
- `statisticsFetchedAt`

### 14.3 실제 댓글 drill-down

```http
GET /v1/analysis/comments
```

대시보드와 동일한 scope/date/commentType query에 다음을 추가한다.

| 이름 | 값 |
| --- | --- |
| `word` | 선택한 주요 단어 |
| `period` | 클릭한 chart bucket |
| `videoId` | 선택 영상 |
| `channelId` | 선택 채널 |
| `sort` | `relevance`, `likes`, `recent`, `replies` |
| `cursor` | keyset cursor |
| `limit` | 기본 10, 최대 50 |

응답은 기존 `CollectedComment`, `CollectedVideo`, channel title 구조를 재사용한다. 검색 결과에는 `storedReplyCount`를 포함한다.

### 14.4 대상 선택 목록

기존 `GET /v1/sources`와 Explore channel 데이터를 우선 재사용한다. Analysis 전용으로 필요한 경우 한 번에 선택 option만 반환하는 경량 endpoint를 둔다.

```http
GET /v1/analysis/dimensions
```

응답:

- 사용자가 볼 수 있는 channel 목록
- 사용자가 구독 중인 keyword target 목록
- 각 대상의 coverage와 마지막 완료 시각
- 전체 보유 영상과 댓글의 earliest/latest published date

### 14.5 영상 snapshot history

v1 필수 endpoint는 아니다. 성장 분석을 추가할 때 사용한다.

```http
GET /v1/analysis/video-stat-trend
```

필수 원칙:

- 동일 영상의 두 snapshot 차이로 증가량 계산
- snapshot 누락 구간을 0 증가로 처리하지 않음
- 채널·키워드 합산 시 영상 ID 중복 제거
- 응답에 비교 snapshot 시각과 포함 영상 coverage 제공

## 15. 집계와 성능 원칙

운영 데이터가 수많은 영상과 수백만 댓글 규모이므로 브라우저로 원본 목록을 내려보내 집계하지 않는다.

- 모든 집계는 owner가 볼 수 있는 target 범위를 서버에서 먼저 확정한 뒤 수행
- global 영상 KPI는 video ID 기준으로 중복 제거
- global KPI는 comment ID 기준으로 중복 제거
- 영상 통계는 video별 최신 snapshot을 먼저 결정한 뒤 합산
- channel breakdown은 video의 실제 channel 귀속 기준
- keyword breakdown은 source-video provenance 기준이며 행 간 중복 가능
- 업로드 추이는 video `published_at`, 댓글 추이는 comment `published_at` 사용
- trend는 PostgreSQL 집계 또는 일별 rollup 사용
- 영상·댓글 상세 목록은 cursor pagination
- top words는 기존 analysis worker의 bounded sample 결과 확장
- filter 조합 결과는 짧은 TTL로 cache
- 응답에는 항상 `generatedAt`, `dataVersion`, `coverage` 포함
- 목표:
  - warm cache dashboard p95 800ms 이하
  - cold dashboard p95 2초 이하
  - drill-down page p95 500ms 이하

날짜별 원댓글/답글 집계가 반복적으로 느리면 `comment_daily_rollups`를 video/date 단위로 추가한다. author 고유 수처럼 단순 합산할 수 없는 지표는 별도 집계하거나 cache하며 rollup 값을 단순 합산하지 않는다.

영상 최신 통계는 `video_stat_snapshots(video_id, fetched_at desc)`에서 최신 row를 선택한다. 기간별 성장 분석은 모든 snapshot을 요청마다 재집계하지 않고 별도 증분 rollup 또는 analysis artifact를 사용한다.

## 16. 프론트엔드 컴포넌트 경계

| 컴포넌트 | 책임 |
| --- | --- |
| `AnalysisPage` | URL filter state와 전체 layout |
| `AnalysisHeader` | 제목, 최신성, 새로고침 |
| `AnalysisViewTabs` | Overview, Videos, Comments 전환 |
| `AnalysisFilterBar` | scope, target, date, 보기별 상세 필터 |
| `AnalysisKpiGrid` | KPI와 이전 기간 비교 |
| `VideoPublishTrendPanel` | 영상 게시 추이 |
| `VideoPerformancePanel` | 최신 snapshot 기준 영상 순위 |
| `VideoReactionScatter` | 조회수와 연결 수집 댓글 관계 |
| `VideoDurationPanel` | 길이 구간별 영상 수와 중앙 성과 |
| `AnalysisVideoTable` | 영상 성과 상세와 cursor pagination |
| `CommentTrendPanel` | trend chart와 table 대체 보기 |
| `CommentCompositionPanel` | 원댓글/답글과 상위 그룹 |
| `TargetBreakdownTable` | channel/keyword 비교 |
| `TopWordsPanel` | 단어 순위와 sample 상태 |
| `AnalysisCommentList` | 실제 댓글 drill-down |
| `AnalysisCoverageDisclosure` | 데이터 범위와 한계 |

기존 `MetricCard`, `VideoModal`, comment row, status pill, formatting helper와 CSS token을 재사용한다.

## 17. 구현 범위

### v1

- `/analysis` route와 상단 우측 진입점
- Overview, Videos, Comments tab
- 전체/채널/키워드, 기간, 보기별 상세 필터
- 영상·댓글별 KPI와 이전 기간 게시량 비교
- 업로드 추이와 최신 snapshot 영상 순위
- 조회수와 연결 수집 댓글 scatter
- 영상 길이 분포와 상세 영상 테이블
- 댓글 활동 추이
- 영상·댓글을 함께 보여 주는 channel/keyword 비교 테이블
- 주요 단어와 실제 댓글 연결
- video snapshot coverage와 comment partial/stale/building 상태

### 후속

- 단어의 기간별 급상승
- 단어 동시 출현
- snapshot 기간별 조회수·좋아요·YouTube 댓글 증가
- 성장 속도 상위 영상과 채널
- 저장 가능한 filter view
- CSV export
- 분석 결과 공유 링크
- 검증된 모델 기반 sentiment/topic summary

감성, 토픽, AI 요약은 모델 버전, 표본, 신뢰도, 근거 댓글을 함께 제공할 수 있을 때만 추가한다.

## 18. 완료 기준

- 상단 우측 Analysis 진입점으로 `/analysis`에 접근할 수 있다.
- Overview, Videos, Comments가 같은 scope와 기간 filter를 공유한다.
- 모든 숫자가 현재 사용자의 visible target 범위에만 속한다.
- 전체 영상 집계는 video ID 중복을 제거하고 최신 snapshot 기준 시각을 표시한다.
- 조회수·좋아요·YouTube 표시 댓글 누적값을 기간 증가량으로 오해하게 표현하지 않는다.
- 영상 게시 추이와 댓글 게시 추이의 날짜 기준을 각각 확인할 수 있다.
- YouTube 표시 댓글과 수집 공개 댓글을 분리한다.
- 전체 집계는 중복 댓글을 제거하고 keyword 행의 중복 가능성은 명시한다.
- 기간과 보기별 필터를 바꾸면 KPI, 차트, 표, 단어, 영상과 댓글이 같은 filter를 공유한다.
- 차트의 날짜 기준이 `댓글 게시일`임을 항상 확인할 수 있다.
- 주요 단어는 표본 수와 상태를 숨기지 않는다.
- 사용자는 차트, 단어, 대상, 영상에서 실제 댓글까지 이동할 수 있다.
- 부분 데이터와 오래된 분석이 정상 데이터처럼 보이지 않는다.
- 모바일에서 영상 KPI, 댓글 KPI, 추이와 실제 영상·댓글 탐색이 유지된다.
- 분석 화면에서 API key나 내부 수집 credential을 노출하지 않는다.
