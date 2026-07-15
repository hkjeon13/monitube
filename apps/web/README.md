# Monitube Web

Next.js 기반의 Monitube 수집 콘솔입니다. 채널, 키워드, 동영상 URL/ID를 선택해 동영상 메타데이터와 공개 댓글을 수집·분석하고, 수집 전 quota 예상치 및 `waiting_quota` 자동 재개 상태를 표시합니다.

## Run locally

```bash
cp .env.example .env.local
npm install
npm run dev
```

기본 주소는 `http://localhost:3000`입니다.

## Browser API contract

`NEXT_PUBLIC_API_BASE_URL`은 브라우저가 호출할 API origin입니다. 예: `http://localhost:8000`. 끝의 `/`는 넣지 않습니다. 운영 YouTube API credential은 백엔드에서만 관리하며 브라우저 환경 변수나 요청에 포함하지 않습니다.

웹은 다음 JSON endpoint를 사용합니다. API는 CORS에서 웹 origin을 허용해야 합니다.

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| `POST` | `/v1/sources` | `CreateCollectionSourceRequest` | `CollectionSource` |
| `POST` | `/v1/sources/{sourceId}/jobs` | `{ include_comments, max_videos?, max_comments_per_video? }` | `JobStatus` |
| `GET` | `/v1/jobs/{jobId}` | — | `JobStatus` |
| `GET` | `/v1/sources` | — | source 목록 |
| `GET` | `/v1/sources/{sourceId}/results` | — | `{ source, latestJob, videos, commentSummary }` |
| `GET` | `/v1/videos/{videoId}/comments` | optional `cursor` | 배열 또는 `{ comments, nextCursor? }` |

공유 타입은 `@monitube/contracts`에서 가져옵니다. `JobStatus`는 최소한 다음을 포함해야 합니다.

```ts
{
  id: string;
  state: "queued" | "running" | "waiting_retry" | "waiting_quota" | ...;
  currentStage: string;
  progress: { completed: number; total?: number; unit: string };
  quotaBucket?: "search_queries" | "core";
  pauseReason?: string;
  resumeAt?: string;
  resumeIsAutomatic: boolean;
  partialErrors: [];
}
```

`waiting_quota` 응답은 backend-managed credential의 checkpoint를 유지하고, `resumeAt` 이후 같은 운영 credential으로 재개하는 상태를 뜻합니다. 클라이언트에는 운영 credential 정보를 반환하지 않습니다.

현재 초기 API는 quota 예상 endpoint를 제공하지 않으므로, quota 화면은 보수적인 브라우저 미리보기를 사용합니다. 작업 등록 시 백엔드가 실제 quota reservation을 수행하고, 이후 job polling 응답에서 실제 `waiting_quota`/자동 재개 상태를 표시합니다.

수집 작업이 완료되면 웹은 source 결과 endpoint를 자동 조회합니다. 결과 화면에서는 source를 선택해 새로고침하고, 저장된 동영상 메타데이터·공개 댓글 요약을 확인할 수 있습니다. 댓글 endpoint는 배열과 cursor 기반 페이지 응답을 모두 허용하도록 방어적으로 해석합니다.
