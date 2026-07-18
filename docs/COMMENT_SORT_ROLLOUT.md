# 댓글 정렬 기능 공유 요약

## 사용자 변화

영상 상세 팝업의 **수집된 공개 댓글**에서 다음 정렬을 선택할 수 있다.

- 최신순: 최근 게시 댓글부터 표시
- 오래된 순: 먼저 게시된 댓글부터 표시
- 추천순: 저장된 좋아요 수가 높은 댓글부터 표시하고, 동률이면 최신 댓글 우선

정렬을 바꾸면 기존 목록과 cursor를 비우고 첫 페이지를 다시 불러온다. 이후 무한 스크롤과 `댓글 더 보기`는 선택한 정렬을 그대로 유지한다.

## API

```http
GET /v1/videos/{videoId}/comment-threads?sort=newest&limit=20
GET /v1/videos/{videoId}/comment-threads?sort=oldest&limit=20
GET /v1/videos/{videoId}/comment-threads?sort=recommended&limit=20
```

응답의 `sort`는 실제 적용된 정렬을 반환한다. `nextCursor`에는 정렬 기준이 포함되어 다른 정렬의 cursor를 섞어 사용할 수 없다.

추천순 keyset은 다음 순서를 사용한다.

```text
likeCount DESC → publishedAt DESC → commentId DESC
```

## 구현 범위

- FastAPI query 계약과 응답 계약
- 인메모리·PostgreSQL 저장소의 정렬별 keyset pagination
- 추천순 부분 인덱스 migration
- 댓글 팝업 정렬 셀렉트와 로딩/오류/cursor 초기화
- 정렬별 첫 페이지·다음 페이지·cursor 혼용 방지 API 테스트
- 모바일 44px 터치 영역과 기존 Monitube 토큰을 사용한 반응형 스타일

## 검증 항목

- API 전체 테스트
- 웹 TypeScript 검사
- Next.js 프로덕션 빌드
- 배포 후 세 정렬의 선택값과 첫 댓글 순서
- 무한 스크롤 시 선택 정렬 유지
- 브라우저 console warning/error
