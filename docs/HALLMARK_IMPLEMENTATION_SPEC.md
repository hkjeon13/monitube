# Monitube Hallmark 리디자인 구현 명세

> Status: implementation-ready  
> Locked direction: Warm Research Workbench  
> Design authority: `design.md`  
> Master plan: `docs/HALLMARK_LAYOUT_REDESIGN_PLAN.md`  
> Scope: frontend layout and visual/interaction layer; API contracts remain unchanged

## 1. 구현 원칙

이 명세의 목적은 구현자가 디자인 결정을 다시 하지 않게 만드는 것이다. 구현 중 새로운 palette, font, radius, shadow, card pattern, route structure가 필요해 보이면 먼저 `design.md`를 수정하고 검토한다.

다음 순서를 지킨다.

1. 현재 동작을 자동화된 fixture와 smoke test로 고정한다.
2. 시각을 바꾸지 않고 route/shell 경계를 분리한다.
3. token과 primitive를 적용한다.
4. route를 한 개씩 새 macrostructure로 이전한다.
5. 모든 route 이전 후 old CSS와 dead component를 삭제한다.

구조 refactor와 route의 대규모 visual redesign은 같은 PR에 넣지 않는다.

## 2. 확정된 선택

더 이상 선택 항목으로 남겨두지 않는 결정이다.

| 항목 | 확정값 |
| --- | --- |
| 방향 | Warm Research Workbench |
| genre | modern-minimal utility + restrained editorial rhythm |
| app macrostructure | Workbench |
| palette | 기존 warm paper + orange + moss를 semantic token으로 정리 |
| display font | Archivo Variable |
| Korean/body font | Pretendard Variable |
| data font | IBM Plex Mono |
| desktop nav | 216px left rail |
| tablet nav | 72px compact rail |
| mobile nav | four primary destinations + More bottom navigation |
| icon set | existing Heroicons outline only |
| content shadow | 사용하지 않음 |
| overlay shadow | menu와 modal에만 허용 |
| responsive strategy | mobile-first `min-width` |
| first migration | shell → Explore → registry → Channels → Status → Analysis → overlays/login |

## 3. 변경 금지 경계

다음은 이 프로젝트에서 별도 요청 없이 변경하지 않는다.

- API path, request, response type
- authentication behavior
- collection job state machine and polling interval
- source dedupe and selection semantics
- search matching and result ranking
- analysis calculation and sort semantics
- video/comment modal data loading
- existing public route paths
- production copy의 사실 관계

UI refactor가 이 경계를 건드려야 한다면 visual PR을 멈추고 별도 기능 PR로 분리한다.

## 4. 목표 컴포넌트 경계

### 4.1 AppShell

File: `apps/web/app/components/app-shell.tsx`

```ts
type AppRoute = "explore" | "channels" | "sources" | "status" | "analysis";

type AppShellProps = {
  activeRoute: AppRoute;
  settingsOpen: boolean;
  onOpenSettings: (trigger: HTMLElement) => void;
  children: ReactNode;
};
```

Responsibilities:

- skip link
- desktop/full rail
- tablet/compact rail
- mobile bottom navigation
- main landmark
- shared settings trigger
- safe-area handling

It does not fetch data and does not know about sources, videos, comments, or analysis.

### 4.2 PrimaryNav

File: `apps/web/app/components/primary-nav.tsx`

```ts
type NavItem = {
  id: AppRoute;
  label: string;
  href: string;
  icon: ComponentType<SVGProps<SVGSVGElement>>;
};
```

Canonical order:

1. Explore
2. Channels
3. Sources
4. Status
5. Analysis

Mobile primary order:

1. Explore
2. Channels
3. Sources
4. Analysis
5. More → Status, Settings

Routes remain unchanged. “More” is navigation chrome, not a new route.

### 4.3 WorkspaceHeader

File: `apps/web/app/components/workspace-header.tsx`

```ts
type WorkspaceHeaderProps = {
  title: string;
  description?: string;
  context?: ReactNode;
  status?: ReactNode;
  actions?: ReactNode;
  width?: "wide" | "workbench" | "report" | "registry";
};
```

Rules:

- one `h1`
- no decorative uppercase kicker by default
- title and action share the same visual row at 768px+
- mobile order is title → description/context → actions
- breadcrumb renders only for actual drill-down states

### 4.4 Button

File: `apps/web/app/components/ui/button.tsx`

```ts
type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "quiet" | "danger";
  size?: "compact" | "default";
  loading?: boolean;
  result?: "idle" | "error" | "success";
  icon?: ReactNode;
};
```

Contract:

- default height 44px
- compact height is still 44px; only horizontal padding and type size differ
- label never wraps
- `loading` keeps width and replaces label content without adding a new adjacent spinner
- error/success state does not change geometry
- focus uses the double ring defined in `design.md`

### 4.5 Field

File: `apps/web/app/components/ui/field.tsx`

```ts
type FieldProps = {
  id: string;
  label: string;
  helper?: string;
  error?: string;
  required?: boolean;
  children: ReactElement;
};
```

Contract:

- visible label above control
- helper/error shares one stable one-line slot
- error sets `aria-invalid` and `aria-describedby`
- border width is always 1px
- 24px right-edge state slot is reserved

### 4.6 StatusIndicator

File: `apps/web/app/components/ui/status-indicator.tsx`

```ts
type StatusTone = "neutral" | "active" | "success" | "warning" | "danger";

type StatusIndicatorProps = {
  tone: StatusTone;
  label: string;
  detail?: string;
  compact?: boolean;
};
```

Status mapping:

| Job state | Tone | Primary label |
| --- | --- | --- |
| queued/running | active | 수집 중 |
| waiting_retry | warning | 재시도 대기 |
| waiting_quota | warning | Quota 대기 |
| completed | success | 완료 |
| completed_with_warnings | warning | 경고와 함께 완료 |
| failed | danger | 실패 |
| cancelled | neutral | 취소됨 |
| no job | neutral | 실행 기록 없음 |

The component never renders color without a text label.

### 4.7 MetricBand

File: `apps/web/app/components/ui/metric-band.tsx`

```ts
type MetricItem = {
  id: string;
  label: string;
  value: string;
  detail?: string;
  actionLabel?: string;
  onAction?: () => void;
};

type MetricBandProps = {
  items: MetricItem[];
  columns?: 3 | 4 | 5;
  ariaLabel: string;
};
```

Rules:

- definition-list semantics
- separators, no individual card fill/shadow
- responsive 2-column layouts are allowed; horizontal scrolling is not; the last odd item spans only when visually balanced
- clickable metric exposes a button inside the item instead of making the entire definition list a button

### 4.8 OverlayFrame

File: `apps/web/app/components/ui/overlay-frame.tsx`

```ts
type OverlayFrameProps = {
  mode: "dialog" | "drawer";
  titleId: string;
  open: boolean;
  triggerRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  children: ReactNode;
};
```

Contract:

- native `dialog` when the current browser support and React integration are verified
- otherwise preserve the existing focus-trap hook until parity is proven
- Escape, backdrop, explicit close, focus restore
- mobile dialog becomes full-screen without changing reading order
- drawer and dialog share tokens and action footer, not necessarily identical markup

## 5. Route implementation contracts

### 5.1 Explore

Primary file: `apps/web/app/features/collection/workbench-explore.tsx`

New files:

- `apps/web/app/features/explore/explore-search-command.tsx`
- `apps/web/app/features/explore/media-catalogue.tsx`
- `apps/web/app/features/explore/media-tile.tsx`
- `apps/web/app/features/explore/search-results.tsx`
- `apps/web/app/styles/pages/explore.css`

DOM order:

1. WorkspaceHeader, visually compact
2. Search command
3. Active context row
4. Search results or media catalogue
5. Load-more sentinel

Featured rule:

- Do not feature `index === 0` merely because it is first.
- Feature only when the API supplies a meaningful sort or the current list is explicitly view-count sorted.
- Otherwise all first-row items use equal importance.

Media tile content order:

1. thumbnail
2. publication date
3. title
4. view/comment metrics

Title uses a maximum of two lines on desktop and three on mobile. Full title remains in the modal and an accessible title attribute is not the sole label.

### 5.2 Channels

New primary file: `apps/web/app/features/channels/channels-page.tsx`

New supporting files:

- `apps/web/app/features/channels/source-context.tsx`
- `apps/web/app/features/channels/performance-ranking.tsx`
- `apps/web/app/features/channels/operations-rail.tsx`
- `apps/web/app/features/channels/recent-video-index.tsx`
- `apps/web/app/styles/pages/channels.css`

DOM order:

1. WorkspaceHeader with source context and add/refresh actions
2. MetricBand
3. Performance ranking
4. Operations rail
5. Recent video index

Desktop grid:

- performance ranking: columns 1–8
- operations rail: columns 9–12
- recent video index: columns 1–12

When there is no active/waiting/error state, operations rail collapses to a compact metadata block. It does not reserve a large empty card.

### 5.3 Sources and Keywords

Split current `workbench-pages.tsx` into:

- `apps/web/app/features/sources/source-registry-page.tsx`
- `apps/web/app/features/sources/source-registry-row.tsx`
- `apps/web/app/features/sources/source-actions-menu.tsx`
- `apps/web/app/styles/pages/registry.css`

Desktop columns:

```text
identity minmax(18rem, 2fr)
state 9rem
coverage minmax(16rem, 1.5fr)
updated 8rem
actions 3rem
```

Mobile summary:

```text
row 1: identity                        actions
row 2: state                           updated
row 3: coverage, full width
```

The mobile version uses the same semantic row/article and changes layout through CSS. It does not clone a second hidden DOM tree.

### 5.4 Status

New files:

- `apps/web/app/features/status/status-page.tsx`
- `apps/web/app/features/status/operations-log.tsx`
- `apps/web/app/features/status/operation-event.tsx`
- `apps/web/app/styles/pages/status.css`

Desktop event grid:

```text
time 8rem
source minmax(12rem, 1fr)
reason minmax(18rem, 2fr)
next-action 9rem
code 8rem
```

Clear history remains a secondary destructive action. If the action removes only the local view and can be restored, implement Undo; if it deletes server data irreversibly, keep an explicit confirmation.

### 5.5 Analysis

Primary file remains `apps/web/app/features/analysis/analysis-dashboard.tsx` during migration.

Extract:

- `analysis-toolbar.tsx`
- `analysis-summary-band.tsx`
- `analysis-trend-section.tsx`
- `analysis-performance-section.tsx`
- `analysis-comparison-table.tsx`
- `analysis-keyword-section.tsx`
- `analysis-comment-section.tsx`
- `analysis-footnotes.tsx`
- `apps/web/app/styles/pages/analysis.css`

The page component owns fetch/filter state. Extracted sections receive normalized data and callbacks only.

Desktop order:

1. WorkspaceHeader
2. Sticky toolbar
3. MetricBand
4. Primary trend, 8 columns
5. Coverage/explanation rail, 4 columns
6. Performance ranking, 12 columns
7. Comparison table, 12 columns
8. Publishing heatmap and comment signals, 7/5 columns
9. Keywords and top comments, 6/6 columns
10. Footnotes, 12 columns

Do not wrap steps 4–10 in one outer rounded panel.

### 5.6 Login and overlays

Current files:

- `workbench-components.tsx`
- `workbench-drawers.tsx`
- `workbench-video-modal.tsx`
- `comment-thread.tsx`
- `overlays.css`

Target split:

- `apps/web/app/features/auth/login-screen.tsx`
- `apps/web/app/features/settings/settings-drawer.tsx`
- `apps/web/app/features/collection/collection-drawer.tsx`
- `apps/web/app/features/video/video-dialog.tsx`
- `apps/web/app/features/comments/comment-thread.tsx`
- `apps/web/app/styles/pages/auth.css`
- `apps/web/app/styles/overlays.css`

The existing YouTube-style comment work remains visually recognizable and functionally unchanged. It adopts only shared type, color, spacing, focus, and overlay tokens.

## 6. Deterministic fixture specification

No real account or customer data is committed. All fixtures are synthetic and clearly named.

### 6.1 File layout

```text
apps/web/tests/
├── e2e/
│   ├── flows.spec.ts
│   ├── responsive.spec.ts
│   └── visual.spec.ts
└── fixtures/
    ├── auth-user.json
    ├── sources-populated.json
    ├── sources-empty.json
    ├── explore-populated.json
    ├── explore-empty.json
    ├── analysis-populated.json
    ├── analysis-empty.json
    ├── job-running.json
    ├── job-waiting-quota.json
    ├── job-failed.json
    ├── comments-threaded.json
    └── long-content.json
```

### 6.2 Required scenarios

| Scenario | Purpose |
| --- | --- |
| populated | normal desktop/mobile layout with enough rows and media |
| empty | action-oriented empty states |
| loading | skeleton geometry and no layout shift |
| running | progress and active status |
| waiting quota | long reason, resume time, warning semantics |
| failed | retryability, error code, child failure count |
| partial warning | completed result plus visible partial errors |
| long content | 100-character Korean title, long handle, 8-digit metric, long error reason |
| threaded comments | parent, replies, selected comment, collapsed/expanded text |

Freeze test time at `2026-08-12T12:00:00+09:00` so relative time and resume labels do not drift.

### 6.3 API interception

Playwright intercepts the existing `/api/v1/...` requests and returns the fixture JSON. Production code does not receive a mock-data switch or a new visual-test route.

## 7. PR execution plan

### PR 0 — locked design documentation

Already produced:

- `design.md`
- `docs/HALLMARK_LAYOUT_REDESIGN_PLAN.md`
- `docs/HALLMARK_IMPLEMENTATION_SPEC.md`

Acceptance:

- selected direction is singular, not a menu of unresolved options;
- token, type, layout, route, and state decisions agree across all three files.

### PR 1 — baseline and test harness

Create:

- `apps/web/playwright.config.ts`
- `apps/web/tests/e2e/flows.spec.ts`
- `apps/web/tests/e2e/responsive.spec.ts`
- `apps/web/tests/e2e/visual.spec.ts`
- fixture files listed in section 6

Modify:

- `apps/web/package.json`
- `apps/web/package-lock.json`

Acceptance:

- current Explore, Channels, Sources, Status, Analysis open under intercepted fixtures;
- video dialog and collection drawer flows are covered;
- screenshots exist at 1440×1000 and 390×844;
- current failures are documented rather than silently blessed.

### PR 2 — token foundation with visual parity

Create:

- `apps/web/app/styles/tokens.css`
- `apps/web/app/styles/reset.css`

Modify:

- `apps/web/app/globals.css`
- `apps/web/app/styles/base.css`
- `apps/web/app/layout.tsx`

Work:

- copy canonical tokens from `design.md`;
- map legacy variables to canonical variables temporarily;
- add font loaders with the exact contract below, but do not apply new display roles yet;
- add root `overflow-x: clip`;
- preserve current geometry for this PR.

```ts
import { Archivo, IBM_Plex_Mono } from "next/font/google";

const archivo = Archivo({
  subsets: ["latin"],
  weight: "variable",
  style: "normal",
  display: "swap",
  variable: "--font-archivo",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  style: "normal",
  display: "swap",
  variable: "--font-ibm-plex-mono",
});
```

`layout.tsx` also loads the version-pinned Pretendard stylesheet:

```tsx
<link
  rel="stylesheet"
  crossOrigin="anonymous"
  href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable-dynamic-subset.min.css"
/>
```

Set `--font-pretendard: "Pretendard Variable"` in `tokens.css`. If the deployed CSP blocks jsDelivr, self-host the identical v1.3.9 subset files before merging this PR.

Acceptance:

- no intended screenshot change except font loading fallback metrics if documented;
- typecheck, build, flow tests pass;
- token aliases make later page migrations incremental.

### PR 3 — AppShell extraction with visual parity

Create:

- `app-shell.tsx`
- `primary-nav.tsx`
- `workspace-header.tsx`
- `mobile-nav.tsx`
- `styles/shell.css`

Modify:

- `collection-workbench.tsx`
- `base.css`
- `sources.css`
- route page files only if their `page` prop needs an explicit route value

Work:

- move navigation and common header markup out of `CollectionWorkbench`;
- render only the active page surface;
- remove `.page-* ... display: none` route switching;
- preserve current visual style.

Acceptance:

- inactive route surfaces are absent from DOM;
- navigation, settings, source selection, and overlays retain behavior;
- no route content is hidden solely by page class.

### PR 4 — new shell and Explore

Create/modify files from sections 4 and 5.1.

Work:

- apply fonts and canonical button/control roles;
- implement full/compact/mobile navigation;
- migrate Explore to media catalogue;
- replace loading text with catalogue skeleton;
- remove index-based featured treatment.

Acceptance:

- all Explore search scopes work;
- catalogue and search results share stable page rhythm;
- real thumbnail color remains visible;
- no horizontal overflow at required widths;
- bottom navigation labels do not wrap.

### PR 5 — Sources and Keywords registry

Create/modify files from section 5.3.

Work:

- split registry from `workbench-pages.tsx`;
- implement desktop row and mobile summary layout;
- standardize status and actions menu;
- remove forced `min-width` table behavior.

Acceptance:

- source open, refresh, pause/resume, and delete work;
- no horizontal mobile table scroll;
- status has readable text and icon, not color alone.

### PR 6 — Channels workbench

Create/modify files from section 5.2.

Work:

- move current Channels render out of `CollectionWorkbench`;
- replace KPI cards with MetricBand;
- implement 8/4 primary split and full-width recent video index;
- collapse quiet collection state.

Acceptance:

- source switch, refresh, ranking metric switch, video open, and job state work;
- no card-in-card;
- primary data is visible in the first desktop viewport.

### PR 7 — Status operations timeline

Create/modify files from section 5.4.

Acceptance:

- clear/refresh behavior is unchanged;
- long failure reason reflows without hiding next action;
- mobile reading order matches the contract.

### PR 8 — Analysis report workbench

Create/modify files from section 5.5.

Work:

- split the 634-line page by data section, not by decorative card;
- replace toolbar outer card with sticky ruled toolbar;
- replace KPI cards with MetricBand;
- implement report section order and footnotes;
- remove repeated section kickers.

Acceptance:

- Overview/Videos/Comments and all filters remain URL/state compatible;
- sortable tables retain semantics and keyboard behavior;
- charts expose text summaries;
- populated, empty, loading, and error states all use stable geometry.

### PR 9 — forms, overlays, comments, login

Create/modify files from section 5.6.

Work:

- apply Button, Field, OverlayFrame;
- preserve comment thread behavior;
- verify focus trap and restore;
- replace generic spinners with inline progress or shape skeleton as appropriate.

Acceptance:

- keyboard-only flows pass;
- field error/helper does not shift layout;
- modal and drawers close through all supported methods;
- mobile full-screen dialog respects safe area.

### PR 10 — cleanup and hardening

Remove only after explicit diff review:

- legacy token declarations superseded by `tokens.css`;
- unused page-routing selectors;
- migrated component fragments;
- duplicated breakpoint rules;
- dead color literals and shadows.

Acceptance:

- raw color/font literal lint passes;
- no user-readable text below 12px;
- only overlay/menu shadows remain;
- all route, responsive, accessibility, build, and typecheck tests pass.

## 8. Required viewport and screenshot matrix

### 8.1 Every migrated PR

- 390×844 mobile populated state
- 1440×1000 desktop populated state

### 8.2 Final QA

- 320×800
- 375×812
- 414×896
- 768×1024
- 1024×768
- 1440×1000

### 8.3 Required screenshot states

| Route | States |
| --- | --- |
| Explore | populated, search results, empty, loading, video dialog |
| Channels | populated, no source, running, waiting quota, failed |
| Sources | populated, empty, action menu open, long content |
| Status | populated failure log, empty, loading, error |
| Analysis | populated Overview, Videos, Comments, loading, empty, error |
| Global | login, settings drawer, collection drawer, mobile More menu |

## 9. Test commands

Expected final command set:

```bash
cd apps/web
npm run typecheck
npm run build
npm run test:e2e
npm run test:visual
```

Add scripts only when the corresponding tests exist. Do not add placeholder scripts that always pass.

## 10. Review checklist for every PR

### Behavior

- [ ] Existing API calls and callback order are unchanged.
- [ ] Loading, empty, error, and populated states are all reachable.
- [ ] Focus returns to the trigger after overlay close.
- [ ] Active navigation and route URL agree.

### Visual system

- [ ] All colors and fonts come from canonical tokens.
- [ ] No new generic card wrapper was introduced.
- [ ] No repeated decorative kicker was introduced.
- [ ] Content hierarchy comes from width, placement, and type—not shadow.
- [ ] Page matches the route macrostructure in `design.md`.

### Responsive

- [ ] 320/375/414/768px were inspected for the touched surface.
- [ ] No horizontal overflow.
- [ ] No two-line clickable label.
- [ ] Touch targets are at least 44px.
- [ ] Mobile reading order is intentional.

### Accessibility

- [ ] Keyboard path works.
- [ ] Focus is visible.
- [ ] Heading and landmark structure are valid.
- [ ] State is not color-only.
- [ ] Contrast pairs come from the validated token contract.

## 11. Completion metrics

| Metric | Required result |
| --- | ---: |
| Route surfaces hidden through `.page-* display:none` | 0 |
| Token-external raw color/font literals | 0 |
| User-readable text below 12px | 0 |
| Decorative `section-kicker` inside data panels | 0 |
| Shadowed normal content panels | 0 |
| Horizontal-scroll mobile registry/table | 0 |
| Missing primary control states | 0 |
| Required viewport overflow failures | 0 |
| Core route/flow test failures | 0 |

## 12. Stop conditions

Stop the current PR and split or request direction when:

- a visual change requires an API contract change;
- a route or component directory must be deleted before parity exists;
- the chosen font cannot meet performance or Korean rendering requirements;
- a mobile layout can work only by hiding essential data;
- a chart has no accessible equivalent;
- a shared component would need route-specific exception props to serve its second consumer;
- the implementation requires a color, radius, or pattern not defined in `design.md`.

## 13. First implementation action

Start with PR 1: add deterministic API interception fixtures and Playwright flow/visual tests against the current UI. Do not apply the visual redesign until the baseline is reproducible.
