# Design — Monitube

> Status: locked for implementation  
> Decision date: 2026-08-12  
> System name: Warm Research Workbench  
> Scope: `apps/web`  
> Source plan: `docs/HALLMARK_LAYOUT_REDESIGN_PLAN.md`

This file is the visual source of truth for Monitube. Page implementations may extend the system only by amending this file first. A route must not introduce a private color, font, radius, shadow, spacing scale, or interaction pattern.

## 1. Product character

Monitube is a working research console for collecting, exploring, and analyzing public YouTube video and comment data. It should feel like a calm research desk, not a generic SaaS dashboard and not a marketing site.

The interface should communicate:

- trustworthy data before decoration;
- clear operating state without alarm fatigue;
- dense information that remains readable;
- media awareness without becoming an entertainment feed;
- a warm, authored visual character.

## 2. Hallmark profile

- Genre: modern-minimal utility with restrained editorial rhythm
- App macrostructure: Workbench
- Explore substructure: Media Catalogue
- Channels substructure: Master-detail Workbench
- Sources and Keywords substructure: Index-first Registry
- Status substructure: Operations Timeline
- Analysis substructure: Long Report Workbench
- Theme behavior: one locked system across every route
- Enrichment: none; real thumbnails and real data carry the page
- Navigation: stable left rail on desktop, compact rail on tablet, bottom navigation on mobile
- Footer: none inside the app shell

Marketing hero, floating pill navigation, glassmorphism, decorative illustration, fake browser chrome, and page-specific themes are not part of this system.

## 3. Structural fingerprint

### 3.1 Section heading placement

- Page title and description share one compact header row.
- Do not place an uppercase eyebrow above every heading.
- A page-level context label is allowed only when it carries real state or scope, at most once per route.
- Panel headings are direct: title first, subtitle or data basis second.

### 3.2 Body composition

- Global shell is stable; each route changes its internal reading pattern.
- Primary information receives width, not extra shadow or saturation.
- Secondary information sits in a narrow rail, marginal note, footer note, or lower-priority row.
- Use hairline rules and spacing before adding a container.
- Use at most one visible containment layer around a content group.

### 3.3 Divider language

- Primary divider: `1px solid var(--color-rule)`.
- Strong table/header divider: `1px solid var(--color-rule-strong)`.
- Section changes may use whitespace only.
- Do not use colored side stripes.

### 3.4 Button voice

- Primary: orange fill, dark ink, 8px radius, one-line verb-led label.
- Secondary: transparent or surface fill with strong rule.
- Quiet: text and icon only; surface appears on hover/focus.
- Danger: danger text on danger-soft only when the action is destructive.
- Pills are reserved for compact statuses and segmented filters.

### 3.5 Image treatment

- YouTube thumbnails are full-bleed inside the media slot.
- Use one quiet dark scrim only where text overlays the image.
- The scrim must preserve visible image color and detail.
- Do not draw device, browser, or video-player chrome around screenshots or thumbnails.
- The first viewport image uses eager loading/high priority; below-fold images use lazy loading.

### 3.6 Reveal pattern

- No universal scroll-triggered animation.
- Route entry may use one 180ms opacity transition.
- Lists and grids do not stagger.
- Loading changes use skeleton-to-content replacement without layout shift.

## 4. Canonical tokens

`apps/web/app/styles/tokens.css` must mirror this block. The CSS file is the runtime source of truth; this block is the design contract.

```css
:root {
  /* Color — derived from the existing Monitube warm paper, orange, and moss. */
  --color-paper: oklch(0.968 0.009 84.6);          /* previous #f7f4ee */
  --color-surface: oklch(0.994 0.006 84.6);        /* previous #fffdf9 */
  --color-surface-muted: oklch(0.956 0.008 114.2); /* previous #f0f1eb */
  --color-ink: oklch(0.252 0.013 160.3);           /* previous #1d2420 */
  --color-ink-soft: oklch(0.448 0.014 153.3);      /* previous #4f5751 */
  --color-muted: oklch(0.531 0.010 121.8);         /* darkened for 4.77:1 on paper */
  --color-rule: oklch(0.923 0.013 86.8);           /* previous #e9e5dc */
  --color-rule-strong: oklch(0.883 0.017 88);      /* previous #ddd8cc */

  --color-accent: oklch(0.689 0.177 41.9);         /* previous #f26e35 */
  --color-accent-hover: oklch(0.605 0.161 41.2);   /* previous #ce5928 */
  --color-accent-soft: oklch(0.965 0.020 53.3);    /* previous #fff0e7 */
  --color-accent-ink: var(--color-ink);

  --color-selection: oklch(0.354 0.058 167);       /* previous #174535 */
  --color-selection-soft: oklch(0.948 0.012 145.5);/* previous #e9f0e9 */
  --color-selection-ink: var(--color-paper);

  --color-success: oklch(0.522 0.096 158.2);
  --color-success-soft: oklch(0.960 0.016 154.5);
  --color-warning: oklch(0.551 0.121 51.3);
  --color-warning-soft: oklch(0.973 0.020 70);
  --color-danger: oklch(0.522 0.138 23.2);
  --color-danger-soft: oklch(0.969 0.015 22.4);
  --color-focus: var(--color-selection);

  /* Typography */
  --font-display: var(--font-archivo), "Pretendard Variable", sans-serif;
  --font-body: var(--font-pretendard), "Pretendard Variable", system-ui, sans-serif;
  --font-data: var(--font-ibm-plex-mono), "IBM Plex Mono", monospace;

  --text-xs: 0.75rem;
  --text-sm: 0.875rem;
  --text-md: 1rem;
  --text-lg: 1.25rem;
  --text-xl: 1.625rem;
  --text-2xl: 2.25rem;
  --text-display: clamp(2.75rem, 5vw, 4.5rem);

  --leading-tight: 1.08;
  --leading-heading: 1.18;
  --leading-body: 1.55;
  --leading-relaxed: 1.7;

  /* 4pt spacing scale */
  --space-3xs: 0.25rem;
  --space-2xs: 0.5rem;
  --space-xs: 0.75rem;
  --space-sm: 1rem;
  --space-md: 1.5rem;
  --space-lg: 2rem;
  --space-xl: 3rem;
  --space-2xl: 4.5rem;
  --space-3xl: 7rem;

  /* Geometry */
  --size-touch: 2.75rem;
  --size-rail: 13.5rem;
  --size-rail-compact: 4.5rem;
  --radius-control: 0.5rem;
  --radius-panel: 0.75rem;
  --radius-media: 0.625rem;
  --radius-pill: 999rem;

  /* Motion */
  --duration-instant: 120ms;
  --duration-short: 180ms;
  --duration-medium: 260ms;
  --ease-out: cubic-bezier(0.16, 1, 0.3, 1);
  --ease-standard: cubic-bezier(0.2, 0, 0, 1);

  /* Elevation — overlays only */
  --shadow-menu: 0 0.75rem 1.75rem oklch(0.252 0.013 160.3 / 0.14);
  --shadow-overlay: 0 1.75rem 5rem oklch(0.252 0.013 160.3 / 0.22);
}
```

## 5. Contrast contract

The locked palette has the following WCAG 2.1 contrast ratios.

| Pair | Ratio | Use |
| --- | ---: | --- |
| ink / paper | 14.43:1 | headings and body |
| ink-soft / paper | 6.80:1 | secondary body |
| muted / paper | 4.77:1 | readable metadata, 12px minimum |
| ink / accent | 5.32:1 | primary button label |
| paper / selection | 9.87:1 | selected dark surface |
| selection / selection-soft | 9.34:1 | selected navigation and success-like selection |
| danger / danger-soft | 5.32:1 | destructive/error state |
| warning / warning-soft | 4.66:1 | waiting/quota state |
| success / success-soft | 4.66:1 | completed state |

Rules:

- A surface-changing class sets both background and foreground color.
- Focus uses a double ring: 2px paper gap plus 2px focus color. This remains visible on accent and selection fills.
- Text below 18px uses a pair passing 4.5:1 or better.
- Color is never the only status signal.

## 6. Typography

### 6.1 Families

- Display: Archivo Variable, weights 650–800, normal only
- Body/Korean: Pretendard Variable, weights 400–700
- Data: IBM Plex Mono, weights 400–600

Official sources and licenses:

- Archivo: <https://github.com/Omnibus-Type/Archivo> — SIL OFL 1.1
- Pretendard: <https://github.com/orioncactus/pretendard> — SIL OFL 1.1
- IBM Plex: <https://github.com/IBM/plex> — SIL OFL 1.1

Loading decision:

- Archivo and IBM Plex Mono use `next/font` and are self-hosted by the build.
- Pretendard uses the version-pinned official dynamic subset at v1.3.9 for the first implementation.
- If CSP or offline deployment disallows the external subset, move the same v1.3.9 files into `apps/web/public/fonts/` without changing family or metrics.
- Use `font-display: swap` and record layout shift during QA.

### 6.2 Roles

| Role | Family | Size | Weight | Tracking |
| --- | --- | ---: | ---: | ---: |
| Wordmark | Archivo | 18px | 750 | -0.04em |
| Page title | Archivo | 44–72px fluid | 720 | -0.055em |
| Section title | Archivo/Pretendard | 20–26px | 680–720 | -0.025em |
| Body | Pretendard | 16px | 450 | 0 |
| Compact body/control | Pretendard | 14px | 550–650 | 0 |
| Metadata | Pretendard or IBM Plex Mono | 12px | 500–600 | 0.01em max |
| Metric value | Archivo | 24–36px | 700 | -0.035em |
| Machine ID/code | IBM Plex Mono | 12px | 500 | 0 |

No user-readable text may be smaller than 12px. Uppercase tracking is capped at `0.08em` and used only for a real state/scope label.

## 7. Layout system

### 7.1 Breakpoints

- Base: 320px and up, mobile-first
- `24rem` / 384px: two-column media becomes available
- `48rem` / 768px: compact rail and tablet layouts
- `60rem` / 960px: multi-column workbench layouts
- `80rem` / 1280px: full 216px rail
- `96rem` / 1536px: wide content cap

All new responsive CSS uses `min-width` media queries. Existing `max-width` rules are retired as each route migrates.

### 7.2 Shell

- Under 768px: no left rail; bottom navigation with four primary routes plus More.
- 768–1279px: 72px compact icon rail; every icon retains an accessible label and tooltip on hover/focus.
- 1280px and up: 216px full rail with wordmark and text labels.
- The rail uses paper, one rule on the inline edge, and no shadow.
- Main padding is `clamp(1rem, 2.5vw, 2.5rem)`.

### 7.3 Content presets

- `wide`: max 90rem, Explore and media surfaces
- `workbench`: max 86rem, Channels
- `report`: max 78rem, Analysis
- `registry`: max 80rem, Sources, Keywords, Status

The shell may be 12 columns internally, but page components own their grid. Do not expose a universal card-grid primitive.

## 8. Route contracts

### 8.1 Explore

- First content: search command, full width.
- Second content: active library/channel context.
- Desktop media grid: 12 columns; lead item spans 6 columns and 2 rows only when a real ranking rule selects it.
- If no ranking rule exists, use equal editorial rows and no featured item.
- Tablet: 3 equal tracks.
- 384–767px: 2 tracks, lead item spans 2.
- Under 384px: 1 track.
- Image scrim is one bottom-to-top neutral gradient capped at 64% opacity at the text edge and 0% by 60% height.

### 8.2 Channels

- Compact source context sits in the workspace header.
- Metric band is one rule-separated row, not four cards.
- Primary split at 960px+: 8 columns performance, 4 columns operations/keywords.
- Recent video index is full width below the split.
- Only active/waiting/failed collection state expands the operations rail.

### 8.3 Sources and Keywords

- Registry is the first main surface; no hero block.
- Desktop columns: identity, state, coverage, updated, actions.
- Rows use rules, not cards.
- Mobile rows become two-level summary items; never keep a forced wide table.
- Row menu is a popover and does not live inside an overflow-clipped ancestor.

### 8.4 Status

- Chronological list with newest event first.
- Time, source, reason, retryability, code form one scan line on desktop.
- Mobile order: time → source → reason → next action → code.
- Status pills are limited to one primary state; error code remains mono text.

### 8.5 Analysis

- Header is compact; title maximum is 48px on desktop.
- View switch and filters share one sticky toolbar, separated by a rule rather than an outer card.
- Summary metrics form one band.
- Primary trend/performance area is 8 columns; secondary explanation/coverage is 4.
- Comparison table and ranking are full width.
- TF-IDF and comment signals use secondary sections with quieter headings.
- Data basis, snapshot time, and coverage appear as report footnotes, not badges on every panel.

## 9. Component contract

### 9.1 Components to create

- `AppShell`: rail, main region, mobile navigation
- `PrimaryNav`: route items and current state
- `WorkspaceHeader`: title, description, context, actions
- `Button`: primary, secondary, quiet, danger
- `Field`: label, control, helper/error slot
- `StatusIndicator`: semantic state plus readable label
- `MetricBand`: rule-separated metric definition list
- `OverlayFrame`: drawer or dialog framing and focus contract
- `LoadingState`: skeleton or inline progress selected by content shape

### 9.2 Components not to create

- Universal `Card`
- Universal `SectionWithEyebrow`
- Icon tile
- Colored side-stripe alert
- Generic glass panel
- Wrapper components that only add radius and shadow

Route-specific visual structures stay route-specific until two real consumers prove a shared abstraction.

## 10. Interaction states

Every interactive primitive implements:

1. default
2. hover inside `@media (hover: hover) and (pointer: fine)`
3. focus-visible
4. active/pressed
5. disabled
6. loading
7. error
8. success

State changes do not alter border width, height, or padding. Input and button base height is 44px. Focus is instant and never animated.

## 11. Motion

- Control color/background: 120ms standard easing
- Menu/dialog enter: 180ms ease-out
- Route content opacity: 180ms ease-out, once
- Drawer translate: 260ms ease-out
- No bounce, elastic, scale-105, cursor follower, auto carousel, or universal fade-up
- Under `prefers-reduced-motion: reduce`, transform is removed and duration is 1ms

## 12. Copy and data

- Preserve existing factual copy intent and route names.
- Replace repeated English decorative labels with direct Korean subtitles or data-basis notes.
- Button labels begin with a verb and remain one line.
- Do not invent performance metrics, testimonials, trust claims, or AI summaries.
- Fixture data is explicitly synthetic and never ships as user-facing production content.

## 13. Accessibility

- Minimum touch target: 44×44 CSS px
- Minimum readable text: 12px; body default 16px
- Visible focus on every control
- Logical heading order with one `h1` per route
- Form labels remain visible
- Error text replaces helper text in a stable-height slot
- Dialog traps focus, supports Escape/backdrop/close, and restores focus
- Charts expose a text summary or table equivalent
- Bottom navigation accounts for safe-area insets
- 320/375/414/768px are mandatory QA widths

## 14. Prohibited patterns

- Card-in-card
- Repeated uppercase eyebrow on sections
- One rounded container per data block
- More than one shadowed content surface per viewport
- Raw color/font values outside `tokens.css`
- User-readable text below 12px
- `transition: all`
- Hover-only actions
- Two-line nav, tab, breadcrumb, or CTA labels
- Desktop table forced into horizontal mobile scrolling
- Page-specific theme or accent
- Pure black or pure white as the primary canvas
- Decorative emoji or mixed icon libraries
- Fake browser, phone, video-player, or IDE chrome

## 15. Change control

Any change to palette, font family, spacing scale, radius roles, navigation model, or route macrostructure must update this file in the same PR. Local exceptions are not allowed.

Old production files are removed only after the replacement passes functional, responsive, and visual QA. A PR that deletes route or component files lists each deletion and its replacement.

## 16. Hallmark pre-emit critique

- Philosophy: 5/5 — preserves real product identity and rejects generic dashboard defaults.
- Hierarchy: 5/5 — assigns a different reading structure to each route.
- Execution: 4/5 — exact tokens and contracts are locked; visual validation remains implementation work.
- Specificity: 5/5 — values, widths, component roles, and route behavior are explicit.
- Restraint: 5/5 — no decorative enrichment, theme swapping, or universal card abstraction.
- Variety: 4/5 — route macrostructures vary inside one coherent app system.
