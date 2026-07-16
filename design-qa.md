# Explore Social Gallery — Design QA

## Comparison target

- Source visual truth: https://dribbble.com/shots/27195636-Social-Posts
- Implementation: https://monitube.fin-ally.net/explore
- Desktop viewport: in-app browser desktop capture, populated Explore state.
- Mobile viewport: 390 × 844, populated Explore state.
- Primary interactions tested: sort control changed to `조회수`; channel filter, pin button, video-card drawer, and "동영상 더 보기" controls are present; no browser-console errors were recorded.

## Evidence

- Source capture: Dribbble `Social Posts by Geex Arts` desktop shot, reviewed 2026-07-16.
- Implementation capture: production `/explore` desktop and 390 × 844 mobile captures, reviewed 2026-07-16.
- Focused comparison: the Explore masthead, horizontal profile/channel strip, featured visual card, small post cards, and mobile two-column gallery were compared against the reference's editorial hierarchy and social-card density.

## Findings

- No actionable P0/P1/P2 findings.

### Required fidelity surfaces

| Surface | Result |
| --- | --- |
| Fonts and typography | Passed. The oversized Explore wordmark creates the same editorial hierarchy; Monitube's sans-serif stack remains appropriate for Korean text. |
| Spacing and layout rhythm | Passed. The large masthead, generous whitespace, horizontal creator strip, and dense gallery establish the intended rhythm without competing with the fixed application sidebar. |
| Colors and visual tokens | Passed. The reference's neutral canvas is adapted to Monitube's warm canvas, moss-green selected state, and orange action color. |
| Image quality and asset fidelity | Passed. The gallery uses real public YouTube thumbnails, not generated or CSS placeholder imagery. Image overlays only preserve readable metadata. |
| Copy and content | Passed. App-specific data (channel, post count, views, comments, dates, pin state) replaces the reference's generic social-post content. |

## Intentional product deviations

- Monitube keeps its global sidebar and collection selector because they are required workspace navigation, not part of the gallery itself.
- The reference's arbitrary social graphics are replaced by the actual public YouTube thumbnails that users came to explore.
- The first video card spans two grid tracks to give the library a clear entry point; this is an intentional adaptation of the reference's mixed-card visual density.

## Follow-up polish

- [P3] Add persisted query parameters for selected channel and sort order when server-side Explore pagination is added.
- [P3] Persist official channel thumbnail URLs from the YouTube API to replace the first-video thumbnail in the channel strip.

## Final result

passed
