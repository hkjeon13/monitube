# Monitube 리팩터링 지침

> 작성일: 2026-07-19
> 적용 범위: API, Worker, Web, Database, Infrastructure

## 1. 목적

Monitube 전체 코드를 기능과 운영 안정성을 유지하면서 점진적으로 리팩터링한다.

이번 작업은 전면 재작성이 아니다. 공개 계약과 핵심 정책을 먼저 보호한 뒤, 책임이 집중된 모듈을 작은 경계로 단계적으로 분리한다.

현재 기준선은 다음과 같다.

- Python 테스트 77개 통과
- Web TypeScript 타입 검사 통과
- 주요 책임 집중 파일
  - `apps/api/monitube_api/postgres_repository.py`: 4,415줄
  - `apps/api/monitube_api/repositories.py`: 2,433줄
  - `apps/web/app/components/collection-workbench.tsx`: 2,385줄
  - `apps/web/app/lib/api.ts`: 1,050줄
  - `apps/api/monitube_api/services.py`: 775줄
  - `apps/worker/monitube_worker/collector.py`: 749줄

## 2. 최상위 원칙

### 2.1 리팩터링과 기능 변경을 분리한다

- 하나의 변경에서 동작 변경과 구조 변경을 함께 하지 않는다.
- API 응답, DB 상태, 작업 상태 전이 또는 UI 동작이 달라지면 기능 변경으로 취급한다.
- 대규모 포맷 변경과 의미 있는 코드 변경을 같은 변경에 포함하지 않는다.

### 2.2 빅뱅 방식으로 재작성하지 않는다

- 한 번에 하나의 책임 경계만 분리한다.
- 기존 테스트가 통과하는 상태를 유지하면서 새 구조로 이동한다.
- 새 구현으로 한 번에 교체하기보다 필요하면 짧은 호환 계층을 사용한다.
- 호환 계층에는 제거 조건과 제거 시점을 기록한다.

### 2.3 외부 계약을 먼저 고정한다

다음 계약을 테스트 또는 명시적인 스키마로 보호한 후 내부 구조를 변경한다.

- HTTP 경로와 JSON 스키마
- 인증과 소유권 검사
- job 상태 전이
- checkpoint 형식
- pagination cursor 형식과 정렬 규칙
- DB 제약조건과 데이터 저장 의미
- quota 및 credential 정책
- 프런트엔드가 기대하는 응답 형태

### 2.4 파일 크기가 아니라 책임을 기준으로 분리한다

- 단순히 일정 줄 수를 기준으로 파일을 자르지 않는다.
- 같은 이유로 변경되는 코드끼리 모은다.
- 이름만 다른 범용 `utils`, `helpers`, `common` 모듈을 무분별하게 만들지 않는다.
- 분리된 모듈의 책임을 한 문장으로 설명할 수 있어야 한다.

### 2.5 변경 단위를 작게 유지한다

한 변경은 하나의 구조적 목적만 가져야 한다.

예:

- source 라우터 분리
- job repository 분리
- 댓글 수집 단계 분리
- API 계약 타입 생성 경로 도입

## 3. 반드시 보존할 프로젝트 불변조건

리팩터링 과정에서 다음 조건을 훼손해서는 안 된다.

### 3.1 Credential 및 보안

- 브라우저, API 응답, DB 또는 로그에 YouTube API key를 노출하지 않는다.
- raw key, authorization header 또는 secret 값을 queue payload에 저장하지 않는다.
- 사용자 입력 URL은 YouTube allowlist와 parser를 통과해야 한다.
- 사용자별 source, target, job 소유권 검사를 우회하지 않는다.

### 3.2 Quota 정책

- quota 소진 시 다른 key, account 또는 project로 우회하지 않는다.
- 동일 runtime config와 checkpoint를 사용해 허용된 시점에 재개한다.
- quota 소진과 일시적인 rate limit, 5xx, timeout을 구분한다.
- quota reservation, request log 및 checkpoint의 의미를 유지한다.

### 3.3 작업 내구성

- 수집 작업을 HTTP 요청 안에서 장시간 실행하지 않는다.
- job lease, checkpoint, retry, `waiting_retry`, `waiting_quota`의 의미를 유지한다.
- video와 comment upsert의 멱등성을 유지한다.
- 부분 실패와 전체 실패를 구분한다.
- worker가 중단되어도 checkpoint에서 안전하게 재개할 수 있어야 한다.

### 3.4 데이터 및 인프라

- PostgreSQL을 운영 환경의 source of truth로 유지한다.
- Redis는 재생성 가능한 파생 캐시로만 사용한다.
- 이미 적용된 DB migration 파일을 수정하지 않는다.
- DB 변경은 기본적으로 `expand → migrate/backfill → contract` 순서로 진행한다.
- 기능 플래그는 전환이 검증될 때까지 유지한다.
- 기능 플래그 제거와 기존 경로 삭제는 별도 변경으로 진행한다.

## 4. 목표 책임 구조

다음 구조는 방향을 나타낸다. 디렉터리 구조 자체보다 각 계층의 책임과 의존 방향을 우선한다.

### 4.1 API

`main.py`는 애플리케이션 조립과 공통 설정만 담당하도록 축소한다.

```text
monitube_api/
  api/
    dependencies.py
    exception_handlers.py
    routers/
      auth.py
      sources.py
      jobs.py
      videos.py
      comments.py
      explore.py
  application/
    source_service.py
    job_service.py
    result_service.py
    explore_service.py
  domain/
    source.py
    job.py
    collection.py
    quota.py
  ports/
    source_repository.py
    job_repository.py
    collection_repository.py
    result_repository.py
  infrastructure/
    postgres/
    memory/
    cache/
```

계층별 책임은 다음과 같다.

| 계층 | 책임 |
| --- | --- |
| Router | HTTP 입출력, dependency 연결, 권한 확인 |
| Application service | 유스케이스 조합과 트랜잭션 의도 |
| Domain | 상태 전이, 정책, 순수 계산 |
| Repository port | 애플리케이션이 요구하는 영속성 계약 |
| Infrastructure | PostgreSQL, Redis 등 기술별 구현 |
| External adapter | YouTube API 등 외부 시스템 통신 |

규칙:

- 라우터는 SQL이나 저장소 구현 세부사항을 알지 않는다.
- 서비스는 FastAPI의 `Request`나 `Response`에 의존하지 않는다.
- 도메인은 PostgreSQL, Redis, FastAPI에 의존하지 않는다.
- 저장소 밖으로 DB row를 그대로 반환하지 않는다.

### 4.2 저장소

현재의 큰 `CollectionRepository` 계약을 기능별 작은 인터페이스로 분리한다.

- `SourceRepository`
- `SubscriptionRepository`
- `JobRepository`
- `CollectionWriteRepository`
- `ResultReadRepository`
- `QuotaRepository`
- `AnalysisRepository`

규칙:

- 메모리와 PostgreSQL 구현은 동일한 repository contract test를 통과해야 한다.
- 메모리 저장소를 운영 구현처럼 과도하게 확장하지 않는다.
- 공유 가능한 순수 정책과 row mapping을 중복 구현하지 않는다.
- SQL은 source, job, comment, result 등 조회 목적별 모듈로 분리한다.
- 원자성이 필요한 작업을 임의로 여러 repository 호출로 쪼개지 않는다.
- transaction boundary는 application use case 또는 명시적인 unit of work로 드러낸다.

### 4.3 Worker

`collector.py`는 orchestration과 수집 단계별 전략으로 분리한다.

```text
collection/
  orchestrator.py
  channel_collector.py
  keyword_collector.py
  video_collector.py
  comment_collector.py
  checkpoint.py
  error_policy.py
  progress.py
```

분리 후에도 다음 과정은 일관된 하나의 처리 흐름으로 유지해야 한다.

```text
quota 예약
  → YouTube 요청
  → 데이터 저장
  → checkpoint 갱신
  → reservation 및 request log 확정
```

예외 분류는 중앙화한다.

- quota 소진
- 재시도 가능 오류
- 인증 또는 설정 오류
- 부분 실패
- 영구 실패
- lease 상실

YouTube 응답 파싱은 orchestration과 분리하고, 수집 단계는 정규화된 내부 모델을 사용한다.

### 4.4 Web

`collection-workbench.tsx`를 기능 단위로 분리한다.

```text
app/features/
  collection/
    api/
    components/
    hooks/
    model/
  jobs/
  results/
  comments/
  explore/
```

규칙:

- 서버 응답 타입과 UI 상태 타입을 구분한다.
- fetching, polling, mutation 로직은 컴포넌트에서 hook 또는 service로 이동한다.
- 순수 표시 컴포넌트는 네트워크 요청을 하지 않는다.
- `api.ts`는 source, job, result, comment 등 리소스별 client로 분리한다.
- Python Pydantic 모델과 OpenAPI를 공개 API 계약의 기준으로 삼는다.
- TypeScript API 타입은 가능하면 OpenAPI에서 생성한다.
- `globals.css`는 디자인 토큰, 공통 레이아웃, 기능별 스타일로 분리한다.
- 스타일 구조 변경과 시각적 디자인 변경을 같은 작업에서 진행하지 않는다.

## 5. 타입과 계약 지침

- 계층 경계에서 무제한 `dict[str, Any]` 사용을 줄인다.
- YouTube 원본 응답은 adapter 경계에서 typed DTO 또는 내부 모델로 변환한다.
- Pydantic API 모델과 내부 domain model을 필요에 따라 분리한다.
- API와 Web에서 같은 계약을 수작업으로 이중 관리하지 않는다.
- cursor, checkpoint, feature flag 이름을 흩어진 문자열 대신 전용 타입이나 상수로 관리한다.
- 공개 응답 필드 제거 또는 의미 변경은 버전이나 명시적인 migration 없이 진행하지 않는다.
- 타입 오류를 숨기기 위한 신규 `type: ignore`, `@ts-ignore`, 광범위한 `Any` 추가를 지양한다.
- 예외적으로 필요한 경우 무시 사유와 제거 조건을 같은 위치에 기록한다.

## 6. 오류 처리 및 관측성 지침

- 넓은 범위의 `except Exception`으로 오류를 삼키지 않는다.
- 외부 API 오류, domain 오류, repository 오류, HTTP 오류를 구분한다.
- 사용자 응답에는 credential, query parameter secret 또는 내부 SQL 정보를 포함하지 않는다.
- worker 오류는 재시도 여부와 실패 scope를 구조화해 기록한다.
- 로그 메시지는 job, source, video 등 안전한 correlation identifier를 포함한다.
- 리팩터링 과정에서 기존 metric, health, readiness 의미를 변경하지 않는다.

## 7. 테스트 지침

모든 리팩터링은 다음 순서를 따른다.

1. 현재 동작을 설명하는 characterization test를 추가한다.
2. 기존 구현에서 테스트가 통과하는지 확인한다.
3. 구조를 변경한다.
4. 같은 테스트로 동작 보존을 확인한다.
5. 새 경계에 대한 단위 테스트를 추가한다.

필수 검증 계층:

- 순수 domain 정책 단위 테스트
- API contract 테스트
- 메모리/PostgreSQL repository contract 테스트
- 실제 PostgreSQL 통합 테스트
- worker checkpoint 및 재개 테스트
- quota, retry, lease 상실 테스트
- fresh DB migration 테스트
- 기존 DB에서의 순차 migration 테스트
- Web typecheck와 production build
- 핵심 사용자 흐름 UI 테스트

기본 검증 명령:

```sh
uv run --project apps/api pytest
cd apps/web && npm run typecheck
cd apps/web && npm run build
```

Python에는 formatter/linter와 정적 타입 검사를 도입한다. 기존 코드 전체를 한 번에 수정하지 않고 변경한 영역부터 적용한다.

## 8. 리팩터링 우선순위

### 8.1 1단계: 보호막 강화

- PostgreSQL repository 통합 테스트 마련
- 메모리/PostgreSQL repository contract test 정의
- API schema snapshot 또는 OpenAPI diff 검사 추가
- Web production build를 검증 명령에 추가
- worker 상태 전이와 checkpoint 테스트 보강

### 8.2 2단계: 계약 단일화

- Python API 계약과 Web 타입의 중복 제거
- 상태, cursor, pagination, 오류 응답 규격 고정
- `dict[str, Any]`가 계층을 넘어가는 지점 축소

### 8.3 3단계: API 분리

- `main.py`에서 router와 권한 정책 분리
- `services.py`를 source, job, result, explore 유스케이스로 분리
- 공통 예외 응답 정리

### 8.4 4단계: 저장소 분리

- repository protocol을 기능별 port로 분리
- `postgres_repository.py`를 조회 및 명령 영역별로 분해
- 메모리와 PostgreSQL 구현에 동일 contract test 적용

저장소 분리는 위험도와 효과가 모두 크므로 API 계약을 고정한 후 진행한다.

### 8.5 5단계: Worker 분리

- collector orchestration과 단계별 수집 로직 분리
- checkpoint와 오류 정책 중앙화
- YouTube 응답 파싱을 수집 흐름에서 분리

### 8.6 6단계: Web 분리

- API client 분리
- workbench 상태와 네트워크 로직 분리
- 기능별 컴포넌트와 스타일 분리
- 구조 분리가 완료된 후 UX 개선 진행

### 8.7 7단계: 운영 코드 정리

- 전환이 끝난 feature flag 제거
- 중복 rollup/read path 제거
- 문서와 실제 설정 동기화
- 배포 및 rollback 절차 재검증

## 9. 변경 완료 기준

각 리팩터링 단위는 다음 조건을 모두 만족해야 완료로 본다.

- 기존 외부 동작이 유지된다.
- 기존 테스트와 신규 테스트가 모두 통과한다.
- Web typecheck와 production build가 통과한다.
- 과거 DB migration을 수정하지 않는다.
- credential, quota, ownership 불변조건이 유지된다.
- 새로운 순환 import가 없다.
- 새 무제한 `Any` 또는 무의미한 범용 helper가 늘지 않는다.
- 변경 후 각 모듈의 책임을 이름과 한 문장으로 설명할 수 있다.
- 관련 문서가 코드와 일치한다.
- 임시 호환 코드와 제거 예정 feature flag에 종료 조건이 기록되어 있다.
- rollback 또는 기존 경로 복귀 방법이 명확하다.

## 10. 금지 사항

- 전체 시스템을 한 번에 재작성하지 않는다.
- 테스트가 없는 상태에서 거대 저장소 구현부터 분리하지 않는다.
- 이미 적용된 migration을 수정하지 않는다.
- public API 응답을 조용히 변경하지 않는다.
- feature flag 없이 운영 read/write 경로를 한 번에 교체하지 않는다.
- 리팩터링을 이유로 보안, quota 또는 소유권 검사를 약화하지 않는다.
- 단순 줄 수 감소를 성공 지표로 사용하지 않는다.
- 동작을 이해하지 못한 코드에 추상화를 먼저 추가하지 않는다.
- 사용처가 하나뿐인 코드를 성급하게 공통화하지 않는다.

## 11. 첫 실행 권장안

첫 실제 작업은 `postgres_repository.py`를 바로 분리하는 것이 아니라 다음 보호막을 만드는 것이다.

1. repository contract test를 정의한다.
2. 메모리와 PostgreSQL 구현에 같은 테스트를 적용한다.
3. OpenAPI 계약 변경을 감지하는 검사를 추가한다.
4. `main.py`의 router 분리를 시작한다.
5. 공개 타입 중복을 줄인다.
6. 보호막이 확보된 뒤 PostgreSQL 저장소를 기능별로 분리한다.

이 순서를 전체 리팩터링의 기본 작업 순서로 사용한다.
