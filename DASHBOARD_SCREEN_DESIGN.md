# Monitube 대시보드 화면 설계 v1

> 목적: 사용자가 API 키나 수집 인프라를 의식하지 않고, 선택한 채널·키워드·영상 source의 수집 결과와 분석 상태를 빠르게 파악하고 다음 수집을 시작하게 한다.

## 1. 설계 방향

참고 이미지의 밝고 정돈된 분석 콘솔 분위기를 Monitube에 맞게 재해석한다. 큰 소개 문구와 수집 폼이 첫 화면을 차지하는 현재 구조에서 벗어나, **분석 결과를 먼저 보여 주고 수집 설정은 필요할 때 여는 구조**로 바꾼다.

- 화면 성격: 따뜻한 아이보리 캔버스 위의 정밀한 분석 워크스페이스
- 정보 위계: 선택 source → 핵심 수치 → 상위 영상과 수집 상태 → 댓글/영상 상세
- 주요 행동: source 전환, 새 수집 시작, 영상 상세 열기
- 표현 원칙: 지금 저장·조회 가능한 데이터만 표시한다. 감성 점수, 구독자 변화, 순위 변화, 성장률처럼 아직 제공하지 않는 분석은 디자인에 넣지 않는다.
- credential 원칙: API key, OAuth, quota key, Google project 정보는 어떤 사용자 화면에도 노출하지 않는다.

## 2. 비주얼 언어

현재 프로젝트의 딥 모스 계열 상태색은 유지하되, 참고 이미지의 인상을 살리기 위해 배경과 주 행동을 아래처럼 정리한다.

| 역할 | 제안 토큰 | 용도 |
| --- | --- | --- |
| 캔버스 | `#F7F4EE` | 페이지 전체의 따뜻한 아이보리 바탕 |
| 기본 표면 | `#FFFDF9` | 카드·테이블·드로어 |
| 잉크 | `#1D2420` | 제목, 주요 수치, 본문 |
| 보조 텍스트 | `#74756E` | 설명, 시각, 보조 메타데이터 |
| 구분선 | `#E9E5DC` | 얇은 카드/행 구분선 |
| 주 행동 | `#F26E35` | `새 수집`, 선택 지표, 핵심 강조 |
| 주 행동의 연한 표면 | `#FFF0E7` | 선택 상태, 강조 카드 |
| 모스 | `#174535` | 탐색, 안정 상태, 현재 디자인과의 연결 |
| 세이지 | `#EDF2EC` | 보조 패널, 필터 표면 |

- 제목은 현재 Georgia 계열의 개성을 유지하되, `Overview`처럼 짧고 기능적인 이름에만 쓴다.
- 본문은 기존 sans-serif를 유지한다. 숫자는 탭형 숫자(tabling numeral)로 정렬해 분석 화면의 밀도를 높인다.
- 카드 반경은 14–18px, 그림자는 매우 약하게 사용한다. 중첩 카드와 두꺼운 테두리는 피한다.
- 오렌지는 하나의 CTA와 선택된 데이터 시리즈에만 사용한다. 경고·성공은 기존 의미색을 유지한다.

## 3. 기본 화면: Source Overview

데스크톱 기준 너비 1440px, 높이 1024px 이상에서 완결되는 첫 화면이다. 12-column main grid와 고정 폭 사이드바를 사용한다.

| 영역 | 레이아웃 | 표시 내용 | 핵심 상호작용 |
| --- | --- | --- | --- |
| 좌측 탐색 | 216px 고정 | Monitube, Overview, Sources, Collection jobs, Insights | Overview가 기본 활성. 다른 항목은 향후 화면 확장의 진입점이다. |
| 상단 바 | 64px | source 선택기, source 유형 chip, 마지막 갱신 시각, `새 수집` | source 전환 시 전체 대시보드를 즉시 재집계한다. |
| 제목 행 | main 전체 폭 | `Overview`, 선택 source 제목, 수집 범위 요약 | 키워드 source는 검색어·기간·언어·지역을 한 줄 badge로 명시한다. |
| 핵심 수치 | 4등분 | 수집 동영상, 수집 공개 댓글, 최근 업로드, 수집 상태 | 수치 카드 전체를 단순 정보로 두고, 단독 그래프를 넣지 않는다. |
| 상위 영상 성과 | 8 columns | Top 5–8 영상, 조회/좋아요/YouTube 표시 댓글 전환 | metric 전환은 새 요청 없이 내려온 최신 snapshot을 클라이언트에서 재정렬한다. |
| 수집 상태 | 4 columns | latest job stage, progress, partial warning, 다음 재개 예정 | `waiting_quota`일 때만 자동 재개 시간을 명확히 알린다. quota key/계정은 숨긴다. |
| 댓글 주요 단어 | 5 columns | `topWords` 순위, 출현 수, “수집된 공개 댓글 기준” 주석 | 단어를 클릭해도 현재는 가짜 필터를 만들지 않는다. |
| 최근 동영상 | 7 columns | 제목, 게시일, 조회, 좋아요, YouTube 댓글, 행 이동 affordance | 행 클릭 시 영상 상세 드로어를 연다. |

### 상단 바와 제목 행

- 좌측: `Overview` (세리프 40–48px)와 “선택한 수집 source의 공개 데이터 요약” 보조 설명
- 우측: source combobox → type chip → “마지막 갱신 2분 전” → 주황색 `+ 새 수집`
- source가 없을 때에는 source selector 대신 “아직 수집 source가 없습니다”와 `첫 수집 시작` CTA를 쓴다.
- `새 수집`은 새 페이지가 아니라 우측 드로어를 연다. Overview의 맥락을 잃지 않게 한다.

### 핵심 수치 카드

첫 줄은 같은 높이의 4개 카드로 고정한다.

1. **수집 동영상** — `analysis.videoCount`
2. **수집 공개 댓글** — `analysis.commentCount`
3. **최근 업로드** — `analysis.latestVideo.publishedAt`, 없으면 `—`
4. **수집 상태** — `latestJob.state` + 진행률, 완료일 때 `완료`, 진행 중일 때 `68%`

수집 댓글 수와 YouTube가 영상에 표시하는 `commentCount`는 범위가 다를 수 있으므로, 전자는 항상 **수집 공개 댓글**이라고 쓴다.

### 상위 영상 성과 카드

참고 이미지의 큰 분석 패널 역할을 한다. 가짜 시계열 대신 현재 API에서 사실인 “최신 영상 통계 기준 상위 영상”을 보여 준다.

- 헤더: `상위 영상 성과`, 정보 툴팁, `조회 · 좋아요 · YouTube 댓글` segmented control
- 본문: 썸네일 대신 작은 순위·제목·게시일과 수평 bar 5–8개
- 막대: 중립 회색 기본 막대, 1위 또는 선택 영상만 주황색
- 하단: “최신 수집 snapshot 기준”과 마지막 수집 시각
- 빈 상태: “아직 표시할 동영상이 없습니다. 수집이 완료되면 성과를 비교할 수 있습니다.”

### 수집 상태 카드

분석 결과와 운영 상태를 같은 무게로 보여 주되, 운영 credential은 보여 주지 않는다.

- 상태 pill: `running`, `completed`, `completed_with_warnings`, `waiting_quota`, `failed`
- progress bar: `latestJob.progress`와 `currentStage`
- 메시지: partial error의 짧은 요약만 표시하고, 상세 오류는 Collection jobs에서 본다.
- `waiting_quota`: “할당량을 기다리는 중 · 2026.07.17 16:00 이후 자동 재개”처럼 확정된 `resumeAt`만 제시한다.
- API key가 아직 설정되지 않은 fixture/no-op 환경: “라이브 수집이 아직 연결되지 않았습니다”와 운영자 전용 안내를 표시한다. 사용자에게 키 입력을 요구하지 않는다.

### 댓글 주요 단어 카드

`analysis.topWords`로 구성한다. 워드클라우드 대신 순위 리스트를 사용해 읽기 쉽고, 데이터가 적어도 안정적으로 보이게 한다.

- 1–10위 단어, 출현 수, 비중형 막대
- 표기: “수집된 공개 댓글 기준”
- 금지: 감성 긍정/부정, AI 요약, 토픽 신뢰도처럼 아직 존재하지 않는 모델 결과

### 최근 동영상 테이블

표는 카드 속 카드가 아니라 하나의 넓은 surface 안에서 얇은 행 구분선으로 구성한다.

| 열 | 소스 데이터 | 비고 |
| --- | --- | --- |
| 영상 | `title`, `videoId` | 제목은 2줄 제한, ID는 보조 metadata |
| 게시일 | `publishedAt` | 로컬 날짜 형식 |
| 조회 | 최신 `viewCount` | 오른쪽 정렬 |
| 좋아요 | 최신 `likeCount` | 비공개/없음은 `—` |
| YouTube 댓글 | 최신 `commentCount` | 수집 공개 댓글과 별도 라벨 |
| 상태 | `privacyStatus`, `madeForKids` | 필요할 때만 compact chip |

행을 클릭하면 우측 영상 상세 드로어에서 메타데이터와 공개 댓글 목록을 보여 준다. 현재 댓글 API가 cursor 기반 추가 로드를 제공하므로, “댓글 더 보기”는 실제 cursor가 있을 때만 보여 준다.

## 4. 수집 시작 드로어

현재 첫 화면의 큰 source form을 `새 수집` 드로어로 옮긴다. 넓이는 480–560px, 모바일에서는 전체 화면이다.

- 헤더: `새 수집` / “수집할 대상을 고르고 범위를 정하세요.”
- source 선택: 채널, 키워드, 단일 영상 — 기존 세 유형을 유지
- 공통 범위: 최대 페이지, 댓글 수집 여부 및 상한
- 채널: 채널 URL 또는 ID
- 키워드: 검색어, 기간, 언어, 지역, 정렬
- 단일 영상: 영상 URL 또는 ID
- 하단 고정 action: 예상 수집 범위(보수적 안내) + `수집 시작`
- 성공 후: 드로어 닫기 → Overview의 수집 상태 카드가 즉시 running 상태로 전환

## 5. 보조 화면의 역할

첫 구현은 Overview를 완성하고, 기존 단일 페이지 컴포넌트를 아래 경계로 나눈다.

| 화면/컴포넌트 | 책임 |
| --- | --- |
| `AppShell` | 사이드바, 상단 바, 전역 layout |
| `DashboardOverview` | source별 KPI와 분석 surface 조합 |
| `SourceSwitcher` | source 목록, type/status, 전체 결과 전환 |
| `CollectionHealthPanel` | job polling, 상태/진행/재개 시각 |
| `TopVideosPanel` | 최신 snapshot 기준 지표 정렬 및 랭킹 |
| `CommentWordPanel` | top words의 정직한 요약 |
| `VideoTable` | 최신 동영상 목록과 상세 진입 |
| `VideoDetailDrawer` | 영상 메타데이터와 댓글 cursor 조회 |
| `NewCollectionDrawer` | 기존 collection form의 재배치 |

`Sources`, `Collection jobs`, `Insights`는 처음에는 보조 패널/필터 상태를 정리하는 수준으로 두고, 별도 라우트와 URL 상태는 Overview 안정화 뒤 추가한다.

## 6. 상태 설계

| 상태 | Overview 표현 | 다음 행동 |
| --- | --- | --- |
| source 없음 | 큰 빈 상태, “첫 수집 시작” | 드로어 열기 |
| queued/running | KPI 상태 card + progress, 기존 결과는 계속 읽기 가능 | Collection jobs 열기 |
| completed | 초록 상태 pill, 마지막 완료 시각 | source 전환 또는 새 수집 |
| completed_with_warnings | 완료 pill + 경고 수 | 부분 오류 상세 확인 |
| waiting_quota | 주황 안내 surface + `resumeAt` | 자동 재개를 기다림 |
| failed | 오류 요약 + 재시도 action | 동일 source 재수집 |
| 결과 없음 | 분석 패널별 이해 가능한 빈 상태 | 수집 범위 수정 또는 기다림 |

색만으로 상태를 구분하지 않고, 모든 state pill에 읽을 수 있는 텍스트와 아이콘 접근성 이름을 제공한다.

## 7. 반응형 원칙

- **1200px 이상:** 좌측 사이드바 + 12-column 분석 그리드
- **768–1199px:** 사이드바는 compact rail, KPI 2×2, 분석/상태 패널 1열 순차 배치
- **767px 이하:** 상단 source selector, KPI 2×2, 드로어 full-screen, 테이블은 핵심 3열(영상/조회/게시일)만 보이고 나머지는 상세로 이동
- 테이블/차트의 의미를 잃는 가로 스크롤은 피한다. 작은 화면에서는 정보를 축약하고 drilldown으로 이동한다.

## 8. 데이터 정확성 경계

| 지금 바로 구현 가능 | 후속 API가 생긴 뒤에만 추가 |
| --- | --- |
| source 수, 동영상 수, 수집 공개 댓글 수 | 영상/채널 성과 시계열 |
| 최근 업로드/최근 댓글 | 구독자·채널 조회수 KPI |
| 최신 snapshot의 조회·좋아요·YouTube 댓글 | 성장률, 이전 기간 비교 |
| top words | 감성·토픽·AI 요약 |
| job 상태, progress, partial warning, resumeAt | 키워드 검색 순위/coverage 추이 |

미래의 시계열 화면은 `video_stat_snapshots`/`channel_snapshots`를 기간별로 반환하는 전용 read API가 추가된 뒤 설계한다. 현재는 참고 이미지처럼 보이기 위한 장식용 그래프를 만들지 않는다.

## 9. 구현 순서

1. 기존 `CollectionWorkbench`를 `AppShell`, Overview, 드로어, 결과 패널 단위로 분리한다.
2. API adapter가 이미 반환하는 `analysis`, `latestJob`, 영상의 최신 통계를 빠짐없이 정규화한다.
3. Overview의 KPI, 상위 영상 랭킹, 상태 패널, top words, 동영상 테이블을 구현한다.
4. 새 수집 드로어로 기존 form/polling 동작을 이전한다.
5. 영상 상세 드로어와 댓글 cursor 로드를 연결한다.
6. 실제 snapshot history API가 준비되면 기간별 추이 panel을 추가한다.

## 10. 완료 기준

- 첫 화면이 수집 폼이 아니라 선택 source의 분석 결과를 중심으로 보인다.
- 사용자는 키·OAuth·프로젝트 정보 없이 source를 전환하고 새 수집을 시작할 수 있다.
- 보이는 모든 숫자와 상태가 현재 API에서 실제로 반환되는 데이터와 연결된다.
- quota 대기와 부분 실패가 숨겨지지 않고, 자동 재개/다음 행동이 명확하다.
- 데스크톱의 촘촘한 분석감과 모바일의 읽기 쉬운 우선순위가 모두 유지된다.
