# Design QA — Monitube analytics dashboard

## Comparison target

- **Source visual truth path:** `/Users/psyche/Desktop/스크린샷 2026-07-16 오전 1.30.34.png`
- **Implementation screenshot path:** in-app Browser full-page capture of [http://127.0.0.1:13001/](http://127.0.0.1:13001/) (desktop capture: 1280 × 1484). The browser-rendered capture is attached to this QA run.
- **Desktop state:** populated `K-pop dance` keyword source, six collected videos, a running comment-collection job, public-comment word ranking, and a recent-video table.
- **Mobile state:** the same populated source at 390 × 844.

The source is a framed monitor photograph while the implementation is an unframed product viewport. The comparison therefore uses the dashboard content region—not the photo background, monitor bezel, or device perspective—as the fidelity target.

## Full-view comparison evidence

The reference establishes a warm ivory canvas, quiet card grid, left navigation, compact top bar, soft borders, restrained shadows, orange emphasis, and large high-contrast display type. The implementation matches those visual surfaces with a warm ivory canvas, off-white cards, a persistent left rail, orange primary action/primary rank treatment, moss active navigation, KPI cards, and an analytics-first panel grid.

At the desktop capture, the source selector, headline, four KPI cards, ranked videos, collection health, word ranking, and recent-video table are visible with no horizontal viewport overflow. At the mobile capture, the rail becomes a compact top navigation, KPI cards form two columns, the ranking controls remain on one line, and non-essential table columns are omitted instead of forcing a horizontal layout.

## Focused region comparison evidence

- **Top bar and KPI region:** both views use a low-contrast top bar, rounded light surfaces, small utility labels, and a restrained orange accent. The implementation intentionally substitutes Monitube collection metrics for the reference's generic cost metrics.
- **Primary analytics panel:** the reference's large chart card is translated into a ranked-video panel using only stored video metrics. The first-ranked row uses orange while comparison rows remain neutral, preserving the reference's visual hierarchy without inventing unsupported time-series data.
- **Navigation and controls:** the left rail, active state, compact tabs, chevrons, refresh control, and drawer actions use one outline-icon family (`@heroicons/react`) with visible keyboard focus states.

## Required fidelity surfaces

- **Fonts and typography:** body UI uses the system Arial/Helvetica stack for compact Korean utility text; Georgia is reserved for the large display and metric hierarchy. Text is truncated rather than wrapped in dense rows. The editorial display treatment is an intentional interpretation of the supplied reference rather than a literal font clone.
- **Spacing and layout rhythm:** all sizing, spacing, radius, shadows, and breakpoints in `apps/web/app/globals.css` use `rem`; a post-change scan found no `px` dimensions. The desktop uses a 12-column panel grid and the mobile breakpoint reflows it to a single content column.
- **Colors and visual tokens:** `--canvas`, `--surface`, `--line`, `--moss`, and `--orange` create the requested warm ivory, soft-card, muted-green, and orange-accent system. Status colors remain semantic and readable.
- **Image quality and asset fidelity:** the reference's monitor photo is intentionally presentation-only and is not re-created with CSS or placeholder artwork. The product viewport contains no substituted decorative raster/SVG art; functional icons come from the installed Heroicons library.
- **Copy and app content:** every visible metric and label is tied to the current source, stored videos, job status, or public comments. Unsupported metrics such as subscriber growth, sentiment, and generic cost figures are not fabricated.

## Comparison history

1. **[P2, fixed] Narrow desktop video table clipped its final columns.**
   - Earlier evidence: the first 1280-wide desktop capture exposed only part of the `YouTube 댓글` table header/value area in the 7-column panel.
   - Fix: applied a fixed table layout with proportioned columns, reduced cell padding, and title truncation in `apps/web/app/globals.css`.
   - Post-fix evidence: the subsequent desktop capture shows all six table columns, including comments and detail action, inside the card.

2. **[P2, fixed] Mobile ranking tabs wrapped awkwardly.**
   - Earlier evidence: at 390 × 844, the three ranking labels wrapped onto multiple lines.
   - Fix: mobile panel headings now wrap as a group while the metric switch takes the full row; tab buttons use `white-space: nowrap` and flexible equal sizing.
   - Post-fix evidence: the final 390 × 844 capture shows `조회`, `좋아요`, and `YouTube 댓글` on one aligned control row.

No actionable P0, P1, or P2 findings remain.

## Primary interactions tested

- Changed the ranking metric from 조회 to 좋아요 and confirmed the selected control and displayed values changed.
- Opened the 새 수집 drawer, switched it to 키워드 collection, confirmed keyword-specific controls appeared, then closed it.
- Opened the first ranked video and confirmed its stored metadata plus public comments were rendered in the detail drawer, then closed it.
- Verified the populated desktop and 390 × 844 mobile responsive states.
- Checked browser console errors after the final render: none (`[]`).

## Follow-up polish

- **[P3]** The source photograph's monitor bezel and cinematic background are intentionally not part of the application viewport. If a marketing landing page is added later, that composition could be reused there rather than inside the analytics workspace.

## Implementation checklist

- [x] Warm ivory analytics-first dashboard implemented.
- [x] Channel, keyword, and video collection flows retained in the new-collection drawer.
- [x] Real result, job, and comment data mapped into the dashboard.
- [x] Responsive desktop and mobile states checked.
- [x] CSS sizing converted to `rem`.
- [x] Type check, optimized production build, CSS pixel-unit scan, and console-error check passed.

final result: passed
