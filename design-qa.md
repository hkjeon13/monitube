# Design QA — Monitube modern sans-serif dashboard

## Comparison target

- **Source visual truth path:** `/Users/psyche/Desktop/스크린샷 2026-07-16 오전 1.30.34.png`
- **Implementation screenshot path:** in-app Browser captures of [http://127.0.0.1:13001/](http://127.0.0.1:13001/) at **390 × 844** and **1440 × 1000**. The final browser-rendered overview captures are attached to this QA run.
- **State:** populated `K-pop dance` keyword source, six collected videos, an in-progress comment collection job, public-comment word ranking, and mobile recent-video cards.

The supplied reference is a framed desktop-monitor photograph, while the implementation capture is an unframed product viewport with Monitube data. The comparison intentionally evaluates the shared design language—warm ivory canvas, quiet card grid, orange emphasis, compact controls, sans-serif product hierarchy, soft borders, and restrained shadows—rather than monitor framing, perspective, or unrelated generic metrics. The replacement of the former serif hierarchy is an explicit user-directed deviation.

## Full-view comparison evidence

The final 390 × 844 and 1440 × 1000 implementations preserve the reference's warm ivory/off-white balance, light rounded cards, orange primary action, muted moss active state, and compact utility navigation. The new sans-serif hierarchy makes the overview title, KPIs, panel headings, drawer titles, and numeric metadata read as one cohesive product system. On mobile it deliberately becomes a one-column analysis flow: two-column KPIs, full-width metric tabs, and tap-friendly panels instead of the reference's desktop chart layout.

The mobile capture has no horizontal overflow at 320 or 390 CSS pixels. At 390 pixels, source selection, refresh, and the primary collection action remain above the fold; the next section begins without clipping. The focused mobile collection state keeps its primary action visible at the safe-area-aware bottom of the sheet.

## Focused region comparison evidence

- **Top bar and KPI region:** the reference's low-contrast top bar, rounded pale surfaces, small utility labels, and orange accent are maintained. Monitube intentionally uses collection metrics in place of the reference's cost metrics.
- **Collection drawer:** the full-screen mobile drawer keeps the reference's light panel rhythm and orange accent while replacing generic controls with channel, keyword, and video collection settings. The keyword preflight state was captured with its complete estimate visible and `수집 작업 시작` held in the bottom action bar.
- **Video detail:** a tap on a recent-video card opens a full-screen detail sheet containing stored metadata and public comments. The new sans title and numeric hierarchy wrap safely without crowding the close action.
- **Navigation and controls:** mobile navigation remains icon-led visually but exposes `Overview`, `Sources`, `Collection jobs`, and `Insights` labels to assistive technology. The source navigation destination lands 80 pixels below the sticky bar.

## Required fidelity surfaces

- **Fonts and typography:** all former Georgia display and metric treatment now uses one `--font-sans` token (`ui-sans-serif`/system UI with Korean-capable fallbacks). The overview title, KPIs, panel titles, drawer headings, estimates, and metadata use aligned 730–780 weights, measured negative tracking, and safer 1.05–1.15 line-height. Monospace remains only for technical identifiers. Mobile labels and metadata remain readable, long titles wrap safely in drawers, and dense mobile lists use two-line title clamping instead of horizontal truncation.
- **Spacing and layout rhythm:** all authored dimensions in `apps/web/app/globals.css` remain `rem`-based. Mobile uses a `--touch-target: 2.75rem` token, safe-area-aware sheet/footer padding, a 64-pixel-equivalent sticky bar, and one-column panel flow.
- **Colors and visual tokens:** existing `--canvas`, `--surface`, `--line`, `--moss`, and `--orange` tokens preserve the source's warm ivory, muted green, and restrained orange hierarchy. Focus states use the same orange family.
- **Image quality and asset fidelity:** the source monitor photo is presentation-only and is not imitated as app UI. No custom placeholder art was introduced; functional icons remain from the Heroicons library.
- **Copy and app content:** all displayed values are current source/job/video/comment data. Unsupported cost, growth, or subscriber metrics are not invented.

## Comparison history

1. **[P1, fixed] Recent-video table was too dense for a phone.**
   - Earlier evidence: the desktop table retained too many columns and a small detail control at 390 pixels.
   - Fix: added a mobile-only, full-row video card list with title, publish date, views, comments, chevron, and a 44-pixel-equivalent target; the desktop table remains for larger viewports.
   - Post-fix evidence: the final 390 × 844 DOM exposes one named `최근 동영상 목록` with six complete, tappable cards.

2. **[P1, fixed] The long keyword collection flow hid the primary action below the fold.**
   - Earlier evidence: the collection action followed the estimate panel in document flow.
   - Fix: moved the action into a sticky, safe-area-aware drawer footer; after preflight it changes from `키워드 검색 예상치 확인` to `수집 작업 시작`.
   - Post-fix evidence: the 390 × 844 keyword estimate capture shows the full estimate and a visible 44-pixel action (`top: 696`, `bottom: 740`).

3. **[P1, fixed] Mobile drawers could lose keyboard focus or allow background scroll.**
   - Earlier evidence: drawers had `aria-modal` but no focus trap, return focus, or body-scroll lock.
   - Fix: added initial close-button focus, Tab containment, Escape/backdrop close, trigger-focus return, body scroll lock, and `100dvh`/overscroll containment.
   - Post-fix evidence: both collection and video detail sheets closed with Escape and returned focus to their originating `새 수집` button or video card.

4. **[P2, fixed] Compact navigation did not expose labels and destination focus used the browser default outline.**
   - Fix: supplied explicit navigation labels and a rounded, token-aligned destination focus style; added scroll margin for sticky navigation.
   - Post-fix evidence: final 390 × 844 semantic snapshot names all four navigation controls and places `#source-selector` at 80 pixels from the viewport top.

5. **[P0, preview-only, fixed] A local development preview went blank after an optimized build reused its cache.**
   - Fix: restarted the local development server after the build, then reloaded and re-captured the browser page.
   - Post-fix evidence: final 390 × 844 populated overview rendered successfully; `npm run build` also completed successfully. This was a preview-cache interaction, not an application UI defect.

6. **[P2, fixed] The display hierarchy mixed serif titles with compact sans-serif utility text.**
   - Earlier evidence: the overview, KPI values, panel headings, drawer titles, estimates, and detail metadata used Georgia while the rest of the application used a system sans stack.
   - Fix: introduced shared `--font-sans` and `--font-mono` tokens, removed all serif declarations, tuned heading/number weights and tracking, and reduced the narrow mobile overview heading to `2.5rem` with a more forgiving line-height.
   - Post-fix evidence: final 1440 × 1000 and 390 × 844 captures show a consistent sans-serif hierarchy with no clipped controls or text; the 320 × 720 capture has `scrollWidth === innerWidth`.

No actionable P0, P1, or P2 findings remain.

## Primary interactions tested

- Changed the ranking metric from `조회` to `좋아요` and confirmed `aria-pressed` changes with the displayed values.
- Opened the collection drawer; switched to keyword collection; filled a keyword; generated the conservative quota estimate; and verified the sticky `수집 작업 시작` action at the bottom of the long form.
- Opened the first mobile recent-video card; confirmed stored metadata and public comments; then closed the drawer with Escape.
- Confirmed the sans-serif treatment in the populated 1440 × 1000 overview, 390 × 844 overview, and 390 × 844 video detail drawer.
- Verified drawer focus return, body-scroll containment, mobile navigation labels, and sticky-header scroll positioning.
- Checked 320 × 720 and 390 × 844 layouts; `documentElement.scrollWidth` equalled `window.innerWidth` at 320.
- Ran `npm run typecheck` and `npm run build` successfully. Browser logs were checked after the final server restart and contain only normal development information; no application errors were recorded.

## Follow-up polish

- **[P3]** The source photograph's monitor bezel and cinematic background remain intentionally outside the product viewport. They would be appropriate for a future marketing/landing context, not the analytics workspace.

## Implementation checklist

- [x] Replaced the mobile table with responsive recent-video cards.
- [x] Added touch-target, safe-area, reduced-motion, and overflow protections.
- [x] Added modal focus management and return focus.
- [x] Added sticky mobile collection actions for quota preflight and start.
- [x] Replaced the serif display hierarchy with a modern Korean-capable system sans-serif stack.
- [x] Preserved rem-based authored sizing.
- [x] Checked populated overview, keyword collection, video detail, 320/390 layouts, semantic controls, typecheck, and production build.

final result: passed
