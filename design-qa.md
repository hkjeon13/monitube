# YouTube 스타일 댓글 — Design QA

## Comparison target

- Source visual truth paths:
  - `docs/assets/youtube-comment-reference.png`
  - `docs/assets/youtube-comment-replies-reference.png`
- Rendered implementation: `https://monitube.fin-ally.net/`
- Intended viewport: 1619 × 1232 desktop; 390 × 844 responsive check.
- State: Explore 댓글 검색 결과에서 원댓글 상세를 열고 실제 저장 대댓글 1개를 펼친 상태.
- Implementation screenshot path: unavailable. The in-app browser rendered and exposed the production DOM, but every supported screenshot call timed out before returning image bytes.

## Evidence captured

- Both source reference images were opened and visually inspected.
- The deployed production page was opened in the in-app browser after deployment.
- Browser-rendered DOM evidence confirmed:
  - one central `dialog` rather than a side drawer or stacked modal;
  - video thumbnail, description, metrics, and paged public-comment region;
  - semantic comment `article` rows with author, relative `time`, body, and read-only like count;
  - explicit long-text expansion and comment-detail controls;
  - a real parent comment with `답글 1개 보기`, expanded child reply, connecting thread structure, and `답글 접기` state;
  - comment-detail back navigation and a single active modal.
- Browser console warnings and errors were checked after the interaction pass; none were recorded.

## Full-view comparison evidence

Blocked. The reference images are available, and the implementation was rendered and interactively tested, but the browser-rendered implementation screenshot could not be captured. A side-by-side visual comparison input therefore cannot be produced without pretending that DOM evidence is equivalent to a screenshot.

## Focused region comparison evidence

Blocked for the same reason. The intended focused region is the author/time/body/action row plus the parent-to-reply connector and indentation shown in the two supplied YouTube references.

## Findings

- [Blocked] Required browser-rendered screenshot is unavailable.
  - Location: production comment-thread modal.
  - Evidence: repeated supported in-app-browser screenshot attempts timed out for the Explore viewport, video modal, and focused comment-thread state.
  - Impact: typography, spacing, color, image fidelity, and final copy wrapping cannot be truthfully passed through the required combined visual comparison.
  - Fix: capture the open production modal through a functioning in-app-browser screenshot session, place it beside both reference images, and rerun the five fidelity checks.

## Required fidelity surfaces

| Surface | Result |
| --- | --- |
| Fonts and typography | Blocked pending combined visual evidence. DOM semantics and hierarchy are correct. |
| Spacing and layout rhythm | Blocked pending combined visual evidence. Component structure, reply nesting, and modal bounds were verified in the rendered DOM. |
| Colors and visual tokens | Blocked pending combined visual evidence. Implementation reuses the existing Monitube tokens. |
| Image quality and asset fidelity | Blocked pending combined visual evidence. Video detail uses the real YouTube thumbnail; comments currently use the documented fallback avatar because profile images are a second-phase data change. |
| Copy and content | Structurally verified with real production comments, authors, relative times, likes, and reply labels; final wrapping remains blocked pending screenshot comparison. |

## Primary interactions tested

- Search scope selection and comment-only search.
- Video card to central video-detail modal.
- Comment list pagination fallback control presence.
- Comment detail view and return to video comments.
- Real stored reply expansion and collapse state.
- Long-comment expansion control presence.
- Browser console warning/error log was empty after the tested flows.

## Comparison history

- Pass 1: source images opened; production implementation rendered and interactions passed; screenshot capture failed repeatedly. No visual fixes were made because a valid comparison input was not available.

## Implementation checklist

- [x] API and database thread contract.
- [x] Shared comment row and thread components.
- [x] Central video/comment modal.
- [x] Production deployment and interaction checks.
- [ ] Capture browser-rendered implementation and run combined desktop/focused-region visual comparison.
- [ ] Repeat at the mobile breakpoint and check browser console errors.

## Final result

final result: blocked

Blocker: the supported in-app-browser screenshot operation timed out, so the required combined visual comparison evidence is missing.
