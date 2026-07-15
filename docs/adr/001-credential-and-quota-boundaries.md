# ADR-001: 서버 관리 credential과 quota 경계

## 상태

승인됨 — 2026-07-16

## 맥락

Monitube의 최종 사용자는 채널, 키워드 또는 영상 URL/ID만 입력한다. YouTube API key, Google Cloud project, OAuth token을 입력·선택·보관하지 않는다. 수집과 분석은 서비스 운영자가 백엔드에 설정한 단일 운영 credential로 수행한다.

## 결정

- `YOUTUBE_API_KEY`는 서버 런타임의 Secret Manager/KMS secret 또는 로컬 개발 `.env`에만 둔다.
- PostgreSQL에는 raw key, access token, refresh token을 저장하지 않는다. 운영 환경의 secret reference, fingerprint, 활성 상태, 감사 시각만 `youtube_runtime_configs`에 저장할 수 있다.
- 공개 API와 브라우저는 credential ID, Google project ID, key fingerprint를 반환하거나 받지 않는다.
- 각 수집 job은 생성 시 내부 `runtime_config_id`를 고정한다. `quotaExceeded`가 발생하면 checkpoint와 quota ledger를 저장하고 **같은** runtime config의 다음 quota window에서 재개한다.
- 동일 서비스의 quota를 늘리기 위한 key/account/project rotation 또는 failover는 구현하지 않는다. 같은 project 안의 key 교체는 유출·폐기 대응을 위한 운영 rollover일 뿐 처리량 확장 수단이 아니다.
- 현재 MVP의 수집 범위는 공개 채널/영상 메타데이터와 공개 댓글이다. 임의 영상 자막의 list/download는 포함하지 않는다.

## 근거

YouTube API Services 정책은 하나의 API Client에 정확히 하나의 API Project를 사용하도록 하며, 여러 project/key로 접근을 분산해 quota를 우회하는 방식을 금지한다. 또한 `captions.list`는 OAuth 2.0을 요구하고 `captions.download`는 OAuth 및 해당 영상을 편집할 권한을 요구한다. 서버 API key만으로 임의 공개 영상의 자막을 수집할 수는 없다.

## 결과

- UX가 단순해진다. 사용자는 source와 수집 범위만 선택한다.
- credential과 quota는 서버 운영 책임으로 일원화된다.
- quota 대기는 자동으로 재개되지만 다른 key/project로 우회하지 않는다.
- 자막 분석은 향후 별도, 합법적 권한 모델이 확정될 때 독립 기능으로 검토한다. 현재 DB·API·UI의 MVP 흐름에는 포함하지 않는다.

## 참고

- https://developers.google.com/youtube/terms/developer-policies-guide#dont-spread-api-access-across-multiple-or-unknown-projects
- https://developers.google.com/youtube/v3/guides/implementation/captions
- https://developers.google.com/youtube/v3/docs/captions/download
