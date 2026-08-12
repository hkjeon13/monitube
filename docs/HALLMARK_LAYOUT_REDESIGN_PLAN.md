# Monitube Hallmark 기반 레이아웃 리디자인 계획

> 상태: 구현 방향 확정  
> 작성일: 2026-08-12  
> 대상: `apps/web` 전체 UI  
> Hallmark 분석 기준: v1.1.0, commit `13ac0ec7e148655948100b6396439e481361d690`  
> 원본: <https://github.com/Nutlope/hallmark>  
> 디자인 시스템: [`../design.md`](../design.md)  
> 구현 명세: [`HALLMARK_IMPLEMENTATION_SPEC.md`](HALLMARK_IMPLEMENTATION_SPEC.md)

## 1. 결론

Hallmark는 참고 사이트나 UI 컴포넌트 라이브러리가 아니라, AI가 자주 만드는 획일적인 화면을 피하기 위한 디자인 스킬과 검수 규칙의 모음이다. Monitube에는 Hallmark의 예제 화면을 복제하기보다 다음 다섯 가지를 적용하는 것이 가장 효과적이다.

1. 멀티페이지 앱 전체에 하나의 `design.md`와 토큰 시스템을 먼저 고정한다.
2. 앱의 기본 매크로구조는 Hallmark의 **Workbench**로 정하되, 각 라우트는 업무에 맞는 서로 다른 정보 구조를 사용한다.
3. 반복되는 둥근 카드, 장식용 영문 kicker, 작은 회색 글씨, 즉흥적인 색상값을 줄인다.
4. 기능과 데이터 흐름은 보존하고, 시각·레이아웃 레이어를 작은 PR 단위로 교체한다.
5. Hallmark의 anti-pattern 및 slop-test를 Monitube 전용 완료 기준으로 변환해 매 단계 검수한다.

확정 디자인 방향은 **Warm Research Workbench**다. 현재의 따뜻한 아이보리, moss, orange 브랜드 자산은 살리고, 카드형 SaaS 대시보드 대신 리서치 데스크·데이터 인덱스처럼 보이는 밀도와 리듬을 만든다. 색상·타입·간격·레이아웃의 구체적인 값은 루트 `design.md`, 파일·컴포넌트·fixture·PR 단위 작업은 `docs/HALLMARK_IMPLEMENTATION_SPEC.md`를 최종 기준으로 사용한다.

## 2. 범위와 비범위

### 이번 리디자인의 범위

- 전역 앱 셸, 내비게이션, 페이지 헤더, 콘텐츠 폭과 그리드
- Explore, Channels, Sources, Keywords, Status, Analysis의 페이지별 레이아웃
- 로그인, 설정, 수집 대상 추가, 영상/댓글 상세 overlay
- 색상, 타이포그래피, 간격, radius, shadow, motion, 상태 표현
- 데스크톱·태블릿·모바일 반응형 구조
- 디자인 변경을 안전하게 만들기 위한 프론트엔드 컴포넌트 경계 정리

### 기본적으로 유지할 것

- 기존 라우트와 URL
- 현재 정보 구조의 핵심 개념: Explore / Channels / Sources / Status / Analysis
- API 계약, 인증, 수집 작업, polling, 검색, 댓글 조회 동작
- 실제 데이터에 기반한 숫자와 상태
- Heroicons 한 종류만 사용하는 현재 아이콘 정책
- YouTube 썸네일과 실제 수집 데이터

### 이번 계획에서 제외할 것

- 백엔드 API 또는 데이터베이스 구조 변경
- 마케팅용 landing page, hero, pricing, footer 제작
- Hallmark 예제 사이트의 픽셀 복제
- 기능 근거가 없는 장식 이미지·3D 오브젝트·AI 일러스트 추가
- 기존 route/component 파일의 선제 삭제
- 여러 페이지에 서로 다른 Hallmark theme를 적용하는 방식

## 3. 분석한 Hallmark 구성

Hallmark 레포의 핵심은 `skills/hallmark/SKILL.md` 하나이며, 아래 reference들이 실제 실행 규칙을 제공한다.

| Hallmark 모듈 | Monitube 적용 | 적용 방식 |
| --- | --- | --- |
| `verbs/redesign.md` | 적극 사용 | 멀티페이지 앱은 하나의 디자인 시스템을 먼저 고정하고, 기능·라우트·IA를 보존한 상태로 시각 레이어를 교체한다. |
| `macrostructures/05-workbench.md` | 적극 사용 | 고정 도구 영역 + 가변 작업 영역 + 고밀도 데이터 화면이라는 앱의 기본 골격으로 사용한다. |
| `anti-patterns.md` | 적극 사용 | card-in-card, 반복 eyebrow, 시스템 폰트 단독 사용, 과도한 radius/shadow 등 현재 증상을 진단하는 기준으로 사용한다. |
| `slop-test.md` | 적극 사용 | 시각·구조·반응형·상태·접근성 완료 체크리스트로 변환한다. |
| `layout-and-space.md` | 적극 사용 | 4pt 간격 체계, 한 화면 안에서 서로 다른 section rhythm, containment 절제를 적용한다. |
| `typography.md` | 선별 사용 | 2+1 폰트 규칙과 읽기 가능한 최소 크기를 사용한다. 마케팅용 대형 display 규칙은 제외한다. |
| `color.md` | 적극 사용 | 한 개의 accent, tinted neutral, semantic token, contrast 검증을 적용한다. |
| `interaction-and-states.md` | 적극 사용 | default/hover/focus/active/disabled/loading/error/success 상태를 표준화한다. |
| `responsive.md` | 적극 사용 | mobile-first, 320/375/414/768px 검증, nowrap affordance, horizontal overflow 금지를 적용한다. |
| theme catalog | 참고만 사용 | 페이지마다 theme를 바꾸지 않는다. 앱 전체는 하나의 잠긴 시스템을 사용한다. |
| hero enrichment, nav/footer archetype | 사용하지 않음 | Monitube는 마케팅 페이지가 아니라 업무용 앱이다. |
| example sites | 진단 참고만 사용 | 결과물을 모방하거나 구조를 그대로 가져오지 않는다. |

### Hallmark 스킬을 프로젝트에 가져올지

실제 리디자인 단계에서는 프로젝트 범위의 `.codex/skills/hallmark/`에 Hallmark를 두는 것이 유용하다. 단, `main`을 매번 그대로 받아오는 방식보다 위 분석 commit을 고정해 검토된 버전을 사용하는 편이 안전하다.

권장 정책은 다음과 같다.

- 이번 계획 문서 작성에는 외부 레포의 v1.1.0 규칙을 직접 분석해 적용한다.
- 구현 착수 시에만 project-scoped skill로 추가한다.
- upstream 업데이트는 자동 반영하지 않고 changelog와 규칙 차이를 검토한 뒤 수동 반영한다.
- Hallmark 원본 전체를 제품 코드에 섞지 않고 `.codex/skills/hallmark/` 아래에만 둔다.
- Monitube 고유 결정은 외부 skill이 아니라 루트 `design.md`가 최종 권위가 되게 한다.

## 4. 현재 Monitube 진단

### 4.1 이미 잘된 부분

- 아이보리 canvas, 짙은 green, orange 조합은 제품 개성이 있으며 유지할 가치가 있다.
- Heroicons 한 종류를 사용해 아이콘 stroke voice가 일관적이다.
- `--touch-target: 2.75rem`과 `prefers-reduced-motion` 처리가 이미 있다.
- loading, empty, error, quota waiting, partial failure 등 실제 운영 상태를 숨기지 않는다.
- 데이터 숫자에 `tabular-nums`를 적용한 영역이 있다.
- Explore의 비대칭 media grid는 획일적인 3열 카드 그리드보다 좋은 출발점이다.
- 실제 API 데이터가 UI 문구와 지표에 연결되어 있어 가짜 통계를 만들 필요가 없다.
- `transition-all`을 사용하지 않는다.

### 4.2 AI처럼 보이는 핵심 원인

아래 수치는 2026-08-12 현재 `apps/web` 정적 분석 기준이다.

| 문제 | 현재 증거 | 사용자에게 보이는 결과 |
| --- | --- | --- |
| 한 가지 시스템 sans가 모든 역할을 담당 | `--font-sans`가 body, heading, wordmark에 공통 적용 | 제목, 데이터, 설명이 크기만 다른 같은 목소리로 보인다. |
| 장식용 영문 kicker 반복 | JSX에서 `section-kicker` 36회 | 거의 모든 영역이 `UPPERCASE LABEL + 큰 제목 + 카드` 패턴으로 반복된다. |
| 지나치게 작은 텍스트 | `0.5rem`~`0.6875rem` font-size 선언 140회 | 정보 밀도가 아니라 흐릿하고 축소된 화면처럼 보이며 접근성도 약해진다. |
| radius와 shadow 남용 | `border-radius` 113회, `box-shadow` 38회 | 서로 다른 의미의 영역이 모두 부드러운 SaaS 카드처럼 보인다. |
| 토큰 밖 색상 즉흥 추가 | CSS에 hex literal 268회, 200개 이상의 고유 hex 값 | 비슷하지만 다른 beige/green/orange가 누적되어 시스템이 느슨해진다. |
| CSS 규모와 selector 파편화 | CSS 1,078줄, 약 345개 class selector | 작은 시각 변경도 여러 파일과 breakpoint를 동시에 건드려야 한다. |
| desktop-first 반응형 | `max-width` media block 7개, `min-width` block 0개 | 모바일이 별도 설계가 아니라 데스크톱을 접은 결과에 가깝다. |
| 일부 모바일 영역이 수평 스크롤에 의존 | `overflow-x: auto` 8회, Sources 목록 `min-width: 42rem` | 작은 화면에서 읽기 순서와 조작 흐름이 깨질 수 있다. |
| route와 view 구조가 강하게 결합 | `CollectionWorkbench` 1,462줄 | 레이아웃 변경과 데이터 상태 변경의 영향 범위가 지나치게 크다. |
| CSS로 비활성 페이지를 숨김 | `.page-* ... display: none` selector 10개 | 각 route가 독립 page surface가 아니라 하나의 거대한 DOM 변형처럼 동작한다. |

### 4.3 화면별 현재 문제

#### 전역 셸

- 좌측 rail은 안정적이지만, 모든 페이지가 같은 breadcrumb·큰 제목·비슷한 panel 문법을 반복한다.
- 모바일에서 다섯 개 메뉴가 라벨 없는 상단 아이콘으로 줄어들어 탐색 의미가 약해진다.
- 설정은 우측의 작은 gear 하나로만 존재해 계정/환경 맥락이 잘 보이지 않는다.
- main의 최대 폭은 넓지만 page별 content density 차이가 거의 없다.

#### Explore

- 검색과 media grid의 방향은 좋지만, 강한 dark shade가 썸네일을 균일한 검은 tile처럼 만든다.
- featured tile과 일반 tile의 크기 차이는 있으나 콘텐츠 우선순위가 데이터에 따라 달라지지 않는다.
- 검색 전·검색 중·검색 결과 상태의 구조가 크게 달라져 전환 시 시선이 튄다.
- channel context가 첫 화면에서 약해 “무엇을 탐색 중인지”보다 grid 자체가 먼저 보인다.

#### Channels

- source selector, KPI card, panel grid가 겹쳐 전형적인 AI dashboard 문법에 가장 가깝다.
- KPI 4개와 분석 panel이 모두 개별 container를 가져 containment가 과하다.
- 수집 상태, 상위 영상, 댓글 단어, 최근 영상이 같은 시각적 무게를 가진다.
- panel마다 영문 kicker가 반복되어 중요한 제목과 보조 제목의 구분이 약하다.

#### Sources / Keywords

- 실제 성격은 “관리 registry”인데 page hero와 rounded tab/plus button 문법이 먼저 보인다.
- desktop table을 모바일에서 넓은 `min-width`로 유지해 수평 스크롤에 의존한다.
- 행 선택, 상태, 진행률, 관리 메뉴의 hierarchy가 작은 글씨와 미세한 색상 차이에 의존한다.

#### Status

- 장애/실패 이력이라는 운영 화면이 일반 panel 카드 안의 list로 보여 긴급도와 시간 흐름이 약하다.
- retryable, error code, child count가 모두 pill로 표현되어 pill이 의미 구분을 대신한다.

#### Analysis

- 큰 `Analysis` 제목 아래에 rounded filter toolbar, KPI cards, rounded panels가 연속된다.
- 정보는 풍부하지만 모든 분석 단위가 동일한 “제목 + kicker + 우측 meta + card” 구조를 반복한다.
- 작은 label과 muted copy가 많아 데이터 자체보다 chrome이 먼저 인지된다.
- 차트, ranking, table, comment signal이 서로 다른 데이터 문법을 가져야 하는데 같은 container voice에 갇혀 있다.

#### Overlay와 form

- drawer, modal, toast의 개별 구현은 기능적으로 풍부하지만 radius, shadow, 색상값이 각자 정의되어 있다.
- input의 focus 상태는 있으나 error/success/loading을 포함한 공통 8-state 계약이 없다.
- reversible action도 confirm에 의존하는 부분이 있어 undo 패턴 검토가 필요하다.

## 5. 목표 디자인: Warm Research Workbench

### 5.1 한 문장 정의

> “영상·댓글 수집 라이브러리를 조사하고 운영하는 따뜻한 데이터 데스크.”

### 5.2 시각 원칙

1. **페이지보다 작업이 먼저 보이게 한다.** 큰 hero성 제목보다 검색, 필터, 선택 상태, 결과를 먼저 둔다.
2. **container를 줄이고 rule을 사용한다.** 모든 영역을 카드에 넣지 않고 hairline, column, whitespace로 구분한다.
3. **accent는 행동과 현재 상태에만 쓴다.** orange는 primary action, moss는 selection/success에 제한한다.
4. **정보 유형마다 다른 문법을 쓴다.** media는 image-led, registry는 row-led, analysis는 report-led, status는 timeline-led로 만든다.
5. **작은 글씨로 밀도를 만들지 않는다.** 본문과 metadata의 읽기 크기를 올리고, 공간과 alignment로 밀도를 만든다.
6. **앱 전체는 하나의 system을 공유한다.** 페이지별 구조는 달라도 color, type, spacing, control voice는 같다.

### 5.3 확정 장르와 폰트

- Genre: `modern-minimal`을 기반으로 한 utility/editorial hybrid
- Latin display/wordmark: `Archivo Variable` 650–800
- Korean/body: `Pretendard Variable` 400–700
- Data/metadata: `IBM Plex Mono` 400–600
- 최대 세 family만 사용하고, 한글·숫자·영문 혼합 샘플을 visual regression fixture에 포함한다.
- Archivo와 IBM Plex Mono는 `next/font`, Pretendard는 v1.3.9 dynamic subset을 기본으로 사용한다.
- CSP 또는 offline 배포가 외부 subset을 허용하지 않으면 같은 버전의 Pretendard를 self-host한다.
- 모든 font는 `font-display: swap`을 사용한다.
- 장식용 uppercase는 mono를 사용하더라도 페이지당 0~1회로 제한한다.

세부 weight, type scale, fallback, 공식 소스와 license는 루트 `design.md`를 따른다. 성능 또는 한글 렌더링이 완료 조건을 통과하지 못할 때만 `design.md`를 먼저 수정하고 대안을 검토한다.

### 5.4 색상 방향

현재 brand palette를 버리지 않고 semantic token으로 압축한다.

| 역할 | 방향 |
| --- | --- |
| Paper | 현재 `#f7f4ee` 계열의 warm neutral 유지 |
| Surface | paper보다 2~4% 밝거나 어두운 한 단계만 사용 |
| Ink | green 쪽으로 살짝 기운 near-black 유지 |
| Muted ink | body contrast를 통과하는 한 단계로 통합 |
| Rule | 기본/강조 두 단계만 사용 |
| Accent | current orange, primary action과 핵심 active marker 전용 |
| Selection | current moss, nav/selection/success 전용 |
| Semantic | success/warning/danger/info 각각 surface·ink·rule pair 정의 |
| Focus | paper와 accent 양쪽에서 3:1 이상 보이는 독립 token |

규칙은 “색을 줄이는 것”보다 “모든 색이 이름과 역할을 갖게 하는 것”이다. `tokens.css` 외 파일의 hex/rgb/oklch literal은 0을 목표로 한다.

### 5.5 간격, radius, shadow

- 4pt 기반 named spacing scale을 만든다.
- page padding, section gap, control gap을 별도 token으로 둔다.
- radius는 `control`, `panel`, `pill`, `round` 네 역할로 제한한다.
- panel은 기본적으로 borderless 또는 0~8px radius를 사용한다.
- pill은 status와 compact filter에만 쓴다.
- shadow는 modal, popover, floating menu에만 사용한다.
- 일반 content panel과 table row에는 shadow를 사용하지 않는다.
- 동일 페이지의 모든 section에 같은 상하 padding을 반복하지 않는다.

## 6. 페이지별 목표 매크로구조

전역은 Workbench를 공유하지만, 각 route는 업무에 맞는 하위 구조를 사용한다.

| Route | 목표 구조 | 핵심 변화 |
| --- | --- | --- |
| `/` Explore | Media catalogue + search canvas | 검색을 상단 command surface로 만들고, channel context와 media mosaic를 연결한다. |
| `/channels` | Master-detail workbench | 좌측/상단 source context와 우측/본문 분석을 분리하고 primary insight에 폭을 몰아준다. |
| `/sources` | Index-first registry | hero와 card를 줄이고 관리용 table/list를 첫 화면의 중심으로 만든다. |
| `/keywords` | Index-first registry variant | Sources와 system은 공유하되 keyword coverage와 refresh 상태를 중심으로 column을 다르게 둔다. |
| `/jobs` | Operations timeline | 최신 실패와 실행 상태를 시간 순서로 읽는 log/timeline으로 만든다. |
| `/analysis` | Long report workbench | sticky filter rail + 핵심 summary + full-width chart/table + 보조 marginalia 구조를 사용한다. |

### 6.1 전역 AppShell

#### Desktop

- 224px navigation rail + 가변 workspace를 기본으로 한다.
- 960~1279px에서는 72px compact rail을 사용한다.
- rail에는 logo, primary navigation, workspace/account context를 명확히 분리한다.
- breadcrumb는 drill-down이 있는 화면에서만 사용하고, 단일 route에서는 제거한다.
- page title과 primary action은 하나의 compact workspace header에 둔다.
- main content는 page type에 따라 `wide`, `report`, `registry` container preset을 사용한다.

#### Mobile

- 현재의 라벨 없는 상단 아이콘 row 대신 4개 primary destination + `더보기` bottom navigation을 검토한다.
- route는 유지하고 Status 또는 낮은 빈도 메뉴를 `더보기` sheet로 이동한다.
- 설정은 account/workspace menu 안에서 명시적으로 노출한다.
- safe-area와 44px 이상 touch target을 보장한다.

### 6.2 Explore

- 검색창을 페이지 첫 content로 유지하되, label/shortcut/scope를 하나의 command surface로 정리한다.
- 현재 channel 또는 “전체 라이브러리” context를 검색 바로 아래에 표시한다.
- 썸네일 shade를 줄이고 image의 밝기와 색을 살린다.
- featured tile은 단순히 첫 번째 item이 아니라 명확한 기준을 갖게 한다. 기준이 없으면 featured treatment를 제거한다.
- grid는 8/4, 4/4/4, full-width row가 섞이는 catalogue rhythm을 사용한다.
- 검색 결과도 같은 media/list vocabulary를 사용해 상태 전환 때 레이아웃이 급변하지 않게 한다.
- loading은 grid shape을 보여 주는 skeleton으로 교체한다.

### 6.3 Channels

- source 선택은 page header의 compact selector로 옮긴다.
- KPI 네 장을 동일 카드로 나열하지 않고 하나의 metric strip 또는 definition list로 바꾼다.
- primary area는 “상위 영상 성과”에 8 columns를 주고, status/word는 4 columns secondary rail로 둔다.
- 최근 영상은 독립 카드가 아니라 full-width table/index로 연결한다.
- `COLLECTION HEALTH`, `PUBLIC COMMENTS` 같은 반복 kicker를 제거하고 상태 또는 데이터 기준을 subtitle로 옮긴다.
- 상태가 active일 때만 운영 rail을 강조하고, completed일 때는 조용한 metadata로 축소한다.

### 6.4 Sources / Keywords

- 페이지 제목, 설명, 추가 action을 1개 compact header row로 합친다.
- channel/keyword switch는 header 아래의 text tab 또는 segmented control로 단순화한다.
- row는 name, collection state, coverage, updated time, action의 명확한 column을 갖는다.
- 색상 pill 대신 text + icon + progress를 조합하고, 실패/대기는 color 외 문구로도 구분한다.
- mobile은 가로 table을 유지하지 않고 row를 2단 summary item으로 재구성한다.
- 1차 정보는 이름·상태·coverage, 2차 정보는 updated time·관리 action으로 고정한다.
- row menu는 Popover API 또는 현재 접근성 계약을 유지하는 menu component로 표준화한다.

### 6.5 Status

- 최신 event가 위에 오는 chronological operations log를 사용한다.
- 실패 원인, source, time, retryability를 한 줄의 scan path로 정리한다.
- retryable 여부와 error code를 pill 여러 개가 아니라 icon + label + mono code로 분리한다.
- “기록 없음”은 성공 상태로 표현하되 과도한 green card는 사용하지 않는다.
- refresh와 clear는 toolbar secondary action으로 두고 destructive scope를 명확히 한다.

### 6.6 Analysis

- page title을 축소하고 sticky filter bar를 workspace header 바로 아래에 둔다.
- Overview/Videos/Comments는 large tab card가 아니라 report section switch로 만든다.
- KPI는 card 5개 대신 한 줄 metric band 또는 2-row definition grid로 만든다.
- 주요 trend와 performance ranking을 full-width 또는 8/4 구조로 배치한다.
- 빈도 기반 단어, comment signals, coverage는 marginal/secondary section으로 시각 무게를 낮춘다.
- table은 실제 header와 row rule을 사용하고 card 안에 다시 넣지 않는다.
- chart와 heatmap에는 항상 text/table fallback 또는 accessible summary를 제공한다.
- 모든 panel에 kicker를 넣지 않고, 데이터 기준은 subtitle/legend/footnote로 표현한다.

### 6.7 Login, drawer, modal

- Login은 중앙 rounded card 한 장보다 split 또는 compact identity layout을 검토한다. 단, 인증 흐름은 바꾸지 않는다.
- Settings와 새 수집 drawer는 같은 form primitive와 footer action pattern을 공유한다.
- video/comment modal은 실제 콘텐츠가 주인공이 되게 border/shadow/radius를 절제한다.
- desktop modal과 mobile full-screen view는 동일한 reading order를 유지한다.
- input, select, checkbox, button은 공통 8-state contract를 사용한다.
- reversible delete/pause는 가능하면 실행 후 undo를 제공하고, irreversible action만 confirm을 유지한다.

## 7. 목표 프론트엔드 구조

시각 개편 전에 동작을 보존하는 구조 분리를 먼저 해야 한다. 단, 대규모 재작성은 피하고 현재 data orchestration을 단계적으로 이동한다.

### 7.1 현재 병목

- `apps/web/app/components/collection-workbench.tsx`가 인증, 모든 route 데이터, polling, 검색, modal, navigation, page render를 함께 소유한다.
- route page는 대부분 `CollectionWorkbench page="..."`만 전달한다.
- 일부 page surface는 항상 render한 뒤 `.page-*` CSS selector로 숨긴다.
- style 파일은 기능별로 나뉘어 있지만 token과 primitive 계층이 없다.

### 7.2 권장 파일 경계

```text
apps/web/
├── app/
│   ├── components/
│   │   ├── app-shell.tsx
│   │   ├── primary-nav.tsx
│   │   ├── workspace-header.tsx
│   │   ├── mobile-nav.tsx
│   │   └── ui/
│   │       ├── button.tsx
│   │       ├── field.tsx
│   │       ├── status-indicator.tsx
│   │       ├── metric-band.tsx
│   │       ├── data-list.tsx
│   │       └── overlay.tsx
│   ├── features/
│   │   ├── shell/
│   │   │   └── use-workspace-controller.ts
│   │   ├── explore/
│   │   │   └── explore-page.tsx
│   │   ├── channels/
│   │   │   └── channels-page.tsx
│   │   ├── sources/
│   │   │   ├── sources-page.tsx
│   │   │   └── source-registry.tsx
│   │   ├── status/
│   │   │   └── status-page.tsx
│   │   └── analysis/
│   │       └── analysis-dashboard.tsx
│   └── styles/
│       ├── tokens.css
│       ├── reset.css
│       ├── shell.css
│       ├── primitives.css
│       └── pages/
│           ├── explore.css
│           ├── channels.css
│           ├── registry.css
│           ├── status.css
│           └── analysis.css
└── ...
design.md
```

파일명은 구현 시 기존 convention과 충돌 여부를 다시 확인한다. 핵심은 다음 경계다.

- controller/hook: API, state, polling, callbacks
- route surface: 해당 route에서 실제로 render할 content
- shared shell: navigation, workspace header, mobile navigation
- primitive: button, field, status, metric, overlay
- page CSS: route별 structure
- tokens: 모든 시각 값의 단일 출처

### 7.3 삭제 정책

초기 PR에서는 기존 production file을 삭제하지 않는다. 새 구조의 parity가 확인된 후 다음 단계에서만 old selector와 dead CSS를 제거한다. 삭제 대상과 이유는 PR 설명에 파일 단위로 명시하고 별도 승인을 받는다.

## 8. 단계별 실행 계획

각 단계는 독립적으로 review 가능한 PR 크기로 유지한다.

### Phase 0 — 기준 화면과 동작 고정

#### 작업

- production 또는 local fixture에서 다음 route의 desktop/mobile screenshot을 저장한다: `/`, `/channels`, `/sources`, `/jobs`, `/analysis`.
- 대표 상태를 고정한다: loading, empty, populated, error, modal open, drawer open.
- 핵심 사용자 흐름을 목록화한다.
- 현재 color, type, spacing, radius, shadow 사용량을 baseline으로 기록한다.
- Playwright 도입 여부를 결정하고 최소 smoke test를 만든다.

#### 완료 조건

- 변경 전 화면과 핵심 동작을 재현할 수 있다.
- visual change가 기능 regression인지 의도한 변화인지 구분할 수 있다.

### Phase 1 — 디자인 방향 잠금 — 완료

**Warm Research Workbench**를 단일 방향으로 확정했다.

- app shell: Workbench
- Explore: Media Catalogue
- Channels: Master-detail Workbench
- Sources/Keywords: Index-first Registry
- Status: Operations Timeline
- Analysis: Long Report Workbench
- theme: warm paper + orange + moss의 단일 시스템
- typography: Archivo + Pretendard + IBM Plex Mono

#### 완료 조건

- [x] 한 방향을 선택했다.
- [x] type/color/radius/layout 결정을 `design.md`에 잠갔다.
- [x] route별 macrostructure와 구현 순서를 구현 명세에 연결했다.

### Phase 2 — `design.md`와 token foundation

#### 생성

- 루트 `design.md`: 앱 전체의 잠긴 디자인 시스템
- `apps/web/app/styles/tokens.css`: 실제 CSS token source of truth

#### 정의할 내용

- genre와 Workbench macrostructure family
- color roles와 contrast pair
- 2+1 typography
- 4pt spacing scale
- radius와 shadow 역할
- motion/easing/reduced-motion
- CTA와 secondary action voice
- page별 허용 구조
- 금지 패턴

#### 완료 조건

- 색상과 font의 직접 선언은 token 파일에만 존재한다.
- 기존 palette와 새 palette 매핑표가 있다.
- focus, error, warning, success contrast가 검증된다.

### Phase 3 — 동작 보존형 AppShell 분리

#### 작업

- navigation, workspace header, mobile navigation을 `CollectionWorkbench` 밖으로 이동한다.
- page별 content를 조건부로 실제 render한다.
- CSS의 `.page-* ... display: none` route switching을 제거한다.
- data orchestration을 hook/controller로 단계 분리한다.
- 기존 route, URL, API callback은 유지한다.

#### 완료 조건

- 모든 route가 동일 기능으로 열리고 current navigation state가 정확하다.
- 비활성 route의 page content가 DOM에 render되지 않는다.
- typecheck/build와 핵심 smoke test가 통과한다.

### Phase 4 — Shell + Explore pilot

#### 작업

- 새 navigation, workspace header, responsive behavior를 적용한다.
- Explore search와 catalogue grid를 새 visual direction으로 구현한다.
- thumbnail shade, loading skeleton, search result transition을 개선한다.
- desktop, compact rail, tablet, mobile navigation을 모두 확인한다.

#### 완료 조건

- 새 design system이 실제 데이터가 있는 첫 화면에서 검증된다.
- 320/375/414/768px에 horizontal scroll이 없다.
- 검색, scope 변경, video modal 진입이 유지된다.

### Phase 5 — Registry와 operations 화면

#### 작업

- Sources/Keywords를 공통 registry pattern으로 바꾼다.
- mobile row summary를 별도로 설계한다.
- Status를 chronological operations log로 바꾼다.
- menu, status indicator, progress primitive를 공통화한다.

#### 완료 조건

- source 생성/선택/refresh/pause/delete 흐름이 유지된다.
- 상태는 색만으로 구분하지 않는다.
- mobile에서 table 수평 스크롤이 없다.

### Phase 6 — Channels와 Analysis

#### 작업

- Channels를 master-detail workbench로 재구성한다.
- KPI cards를 metric band로 바꾼다.
- Analysis를 report-led 구조로 바꾸고 filter bar를 sticky 처리한다.
- chart, ranking, table, keyword, comment signal에 서로 다른 시각 문법을 준다.
- 반복 `section-kicker`를 제거한다.

#### 완료 조건

- 핵심 데이터가 첫 viewport에서 우선순위대로 보인다.
- card-in-card와 동일 panel 반복이 없다.
- filter, tab, sort, table, video/comment drill-down이 유지된다.

### Phase 7 — Overlay, form, login, 상태 완성

#### 작업

- button, input, select, checkbox, menu, toast의 8-state contract를 적용한다.
- drawer와 modal을 공통 overlay primitive로 정리한다.
- login의 identity와 form hierarchy를 새 system에 맞춘다.
- error/helper 영역의 높이를 안정화해 validation layout shift를 막는다.
- focus trap, Escape, backdrop click, restore focus를 검증한다.

#### 완료 조건

- keyboard-only로 모든 주요 흐름을 완료할 수 있다.
- focus가 보이고 modal 뒤로 tab이 새지 않는다.
- loading/error/success에서 control geometry가 바뀌지 않는다.

### Phase 8 — dead CSS 제거와 최종 slop-test

#### 작업

- 미사용 selector, old page routing CSS, 중복 token을 제거한다.
- Hallmark pre-emit critique 6축을 Monitube 기준으로 채점한다.
- anti-pattern과 responsive gate를 전체 route에 적용한다.
- production-like data에서 최종 visual QA를 수행한다.

#### 완료 조건

- 아래 Definition of Done을 모두 만족한다.
- 기존 파일 삭제가 있다면 대상과 이유가 승인되었다.

## 9. PR 분할 권장안

| PR | 내용 | 기능 위험 |
| --- | --- | --- |
| 1 | baseline, `design.md`, tokens, QA checklist | 낮음 |
| 2 | AppShell/controller 분리, 기존 visual 유지 | 중간 |
| 3 | 새 shell + Explore pilot | 중간 |
| 4 | Sources/Keywords registry + Status log | 중간 |
| 5 | Channels workbench | 중간 |
| 6 | Analysis report layout | 높음 |
| 7 | overlay/form/login state system | 높음 |
| 8 | responsive/a11y/performance polish + dead CSS 제거 | 중간 |

구조 refactor와 대규모 visual change를 같은 PR에 넣지 않는 것이 핵심이다. PR 2에서 동작을 보존한 채 경계를 만들고, 이후 PR에서 눈에 보이는 구조를 바꾼다.

## 10. 검증 계획

### 10.1 viewport matrix

Hallmark의 필수 폭을 그대로 사용한다.

- 320px
- 375px
- 414px
- 768px
- 1024px
- 1440px

각 폭에서 다음을 확인한다.

- horizontal scroll 없음
- navigation label과 CTA가 두 줄로 wrap되지 않음
- display heading이 긴 한글/영문에서 overflow되지 않음
- image-bearing grid는 `minmax(0, 1fr)` 사용
- touch target 44×44px 이상
- sticky header, bottom nav, drawer가 safe area와 충돌하지 않음

### 10.2 핵심 flow matrix

1. 로그인 / 계정 생성
2. Explore 전체 검색 / 영상 검색 / 댓글 검색
3. Explore video modal / comment detail / reply expand
4. Channels source 전환 / result refresh
5. 수집 대상 추가 drawer / validation / submit
6. Sources row open / refresh / pause / delete
7. Status refresh / clear
8. Analysis Overview / Videos / Comments 전환
9. Analysis scope / period / comment filter 변경
10. mobile navigation과 overlay back/close

### 10.3 접근성

- body text 4.5:1 이상, large text/icon/focus 3:1 이상
- decorative label을 제외한 텍스트는 12px 미만 사용 금지
- visible focus, logical tab order, focus restore
- semantic heading 순서
- form label, helper, `aria-invalid`, `aria-describedby`
- loading과 결과 갱신의 live-region 검토
- status를 color 단독으로 표현하지 않음
- `prefers-reduced-motion`에서 motion 제거
- chart에 accessible summary 제공

### 10.4 성능

- Explore 첫 viewport thumbnail에 적절한 priority 적용
- below-the-fold image만 lazy-load
- image width/height 또는 aspect-ratio로 CLS 방지
- font subset과 `font-display: swap`
- route별 불필요 component render 제거
- skeleton이 실제 content geometry와 일치

### 10.5 자동화 권장

- `npm run typecheck`
- `npm run build`
- Playwright route smoke test
- viewport별 screenshot capture
- axe 또는 동등 도구의 자동 접근성 scan
- CSS token lint: token 파일 외 raw color/font literal 금지

## 11. Monitube용 Hallmark 완료 체크리스트

### 구조

- [ ] 모든 route가 같은 `hero → filter card → KPI cards → panel cards` 구조를 반복하지 않는다.
- [ ] 카드 안 카드가 없다.
- [ ] 비활성 page를 CSS로 숨기지 않는다.
- [ ] 한 화면의 primary area가 명확하고 secondary area는 실제로 더 조용하다.
- [ ] page별 macrostructure는 다르지만 shell과 design token은 같다.

### 타이포그래피

- [ ] display/body/data 역할이 구분된다.
- [ ] heading은 roman이며 장식용 italic을 쓰지 않는다.
- [ ] panel마다 uppercase kicker를 반복하지 않는다.
- [ ] 읽어야 하는 텍스트는 12px 미만이 아니다.
- [ ] 숫자 column은 tabular figures를 사용한다.

### 색상과 surface

- [ ] token 파일 외 raw hex/rgb/oklch가 없다.
- [ ] accent가 viewport를 지배하지 않는다.
- [ ] 일반 panel에 shadow를 쓰지 않는다.
- [ ] 의미 없는 pill과 rounded card를 줄였다.
- [ ] 상태는 color + text/icon을 함께 사용한다.

### interaction

- [ ] 모든 주요 control에 default/hover/focus/active/disabled/loading/error/success가 있다.
- [ ] hover-only action이 없다.
- [ ] focus ring이 즉시 보이고 animate되지 않는다.
- [ ] input border width가 state에 따라 바뀌지 않는다.
- [ ] reversible action은 undo 가능성을 검토했다.

### responsive

- [ ] 320/375/414/768px에서 검증했다.
- [ ] horizontal scroll이 없다.
- [ ] primary clickable label이 두 줄로 wrap되지 않는다.
- [ ] mobile registry는 desktop table 축소판이 아니다.
- [ ] desktop-only hover interaction이 없다.

## 12. 정량 목표

| 항목 | 현재 | 목표 |
| --- | ---: | ---: |
| `CollectionWorkbench` LOC | 1,462 | shell/controller/page 경계로 분리; 단일 파일 500줄 이하 권장 |
| JSX `section-kicker` | 36회 | 장식용 0회, 필요한 page-level context만 최대 1회 |
| 12px 미만 font-size 선언 | 140회 | 사용자 읽기 텍스트 0회 |
| token 밖 raw color | hex 268회 | 0회 |
| 일반 content shadow | 다수 | 0회 |
| page route용 `display: none` selector | 10개 | 0개 |
| mobile-first `min-width` media block | 0개 | 새 layout CSS는 mobile-first 100% |
| 필수 viewport overflow | 미검증/일부 scroll 의존 | 6개 viewport 모두 0 |

LOC는 품질 자체가 아니라 책임 경계의 보조 지표로만 사용한다. 기능을 억지로 압축해서 목표를 맞추지 않는다.

## 13. 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 구조 refactor 중 API/polling regression | visual change 전에 동작 보존형 PR을 분리하고 flow smoke test를 먼저 만든다. |
| 실제 데이터에 따라 layout이 깨짐 | empty/loading/error뿐 아니라 긴 title, 큰 숫자, 긴 Korean copy fixture를 사용한다. |
| 폰트 로딩으로 성능 저하 | subset, local hosting, swap, fallback metric 검토 후 확정한다. |
| 기존 palette의 개성을 잃음 | orange/moss/warm paper는 유지하고 role만 정리한다. |
| Hallmark 규칙을 기계적으로 적용 | marketing 규칙은 제외하고 app workbench에 필요한 gate만 사용한다. |
| 페이지마다 다른 스타일로 분열 | 루트 `design.md`와 token을 모든 route의 최종 권위로 둔다. |
| 한 번에 너무 많은 시각 변경 | shell과 route를 별도 PR로 나누고, 각 route는 1440×1000과 390×844 reference screenshot을 통과한 뒤 다음 route로 이동한다. |
| mobile에서 정보 손실 | 단순 hide가 아니라 page별 mobile reading order를 정의한다. |

## 14. 확정된 구현 결정

| 결정 | 확정값 |
| --- | --- |
| 디자인 방향 | Warm Research Workbench |
| palette | 현재 warm paper + moss + orange 유지 |
| typography | Archivo + Pretendard + IBM Plex Mono |
| navigation | desktop 216px rail, tablet 72px rail, mobile bottom nav + More |
| route macrostructure | Explore catalogue, Channels workbench, registry index, Status timeline, Analysis report |
| Hallmark 설치 | 구현 시 project-scoped, commit pin 후 사용 |
| 기능/IA 변경 | route와 핵심 IA는 유지, layout/label hierarchy만 조정 |
| 첫 PR | Playwright API fixture + flow/visual baseline, production visual 변경 없음 |
| 디자인 권위 | 루트 `design.md` |
| 실행 권위 | `docs/HALLMARK_IMPLEMENTATION_SPEC.md` |

## 15. Definition of Done

리디자인은 다음 조건을 모두 만족할 때 완료다.

- 모든 기존 핵심 flow가 동작한다.
- 각 route가 업무에 맞는 서로 다른 정보 구조를 갖는다.
- 앱 전체는 하나의 type/color/spacing/control system을 공유한다.
- 장식용 kicker, card-in-card, 무의미한 pill/shadow가 제거되었다.
- 모든 색상과 폰트가 token을 통해 적용된다.
- 320/375/414/768/1024/1440px에서 layout을 확인했다.
- keyboard, focus, modal, form validation, status announcement를 검증했다.
- `npm run typecheck`, `npm run build`, 핵심 smoke test가 통과한다.
- Hallmark pre-emit critique의 Philosophy, Hierarchy, Execution, Specificity, Restraint, Variety가 모두 3/5 이상이다.
- 변경 전후 screenshot과 의도된 차이를 PR에서 설명할 수 있다.

## 16. 다음 단계

구현 명세의 PR 1부터 시작한다. Playwright에서 기존 `/api/v1/...` 요청을 synthetic fixture로 intercept해 현재 Explore, Channels, Sources, Status, Analysis와 주요 overlay 흐름을 재현하고, 1440×1000 및 390×844 baseline screenshot을 만든다. 이 baseline이 안정된 뒤에만 token과 layout 변경을 시작한다.
