# 대본 검색·MeCab+NLTK·TF-IDF 분석 구현 계획

- 작성일: 2026-08-12
- 상태: 구현 전 계획
- 대상 저장소: `monitube`
- 참조 구현: `../ai-assistant/app/installation/mecab`, `../ai-assistant/ai_core/extractor/noun_extractor.py`

## 1. 목표

현재 수집 중인 영상 대본을 실제 제품 기능에 연결하고, 댓글과 영상의 키워드 분석을 단순 빈도 집계에서 TF-IDF 기반 중요도 집계로 전환한다.

최종 사용자 경험은 다음과 같다.

1. Explore의 영상 검색은 제목·채널뿐 아니라 저장된 대본까지 검색한다.
2. 대본으로 일치한 영상 검색 결과에는 전체 대본이 아니라 가장 관련 있는 짧은 대본 스니펫만 표시한다.
3. Analysis는 영상 내용과 시청자 반응을 섞지 않고 다음 두 결과를 별도로 제공한다.
   - 영상 키워드: 영상 대본 기반 TF-IDF
   - 댓글 키워드: 수집 댓글 기반 TF-IDF
4. 한국어는 MeCab, 영어는 NLTK 품사 분석을 사용한다.
5. MeCab/정규식/PeCab 간 자동 품질 저하 폴백은 허용하지 않는다.
6. 원문 전체를 분석 요청마다 Python 메모리에 적재하지 않고, 수집 후 한 번 계산한 문서별 용어 통계를 PostgreSQL에 저장하여 재사용한다.
7. 새 문서가 추가될 때 전체 코퍼스를 다시 세지 않고 관련 scope의 N, DF, 총 TF와 일별 통계만 증분 갱신한다.

## 구현 반영 기록 (2026-08-12)

이 계획은 `020_transcript_search_mecab_tfidf.sql`과 애플리케이션 코드에 반영되었다.

- `../ai-assistant`의 MeCab 소스·한국어 사전 파일을 저장소 내부 `infra/mecab/vendor`로 복사하고 SHA-256 검증, 로컬 빌드, 사전 smoke test를 수행한다.
- NLTK `punkt_tab`, 영어 품사 태거도 `infra/nltk/vendor`에 포함해 이미지 빌드가 런타임 다운로드에 의존하지 않는다.
- production 분석기는 KoNLPy MeCab + NLTK 하나이며 PeCab, 정규식 토큰화, Kiwi 폴백은 없다. 초기화 실패는 readiness/worker 시작 실패로 드러난다.
- 대본과 댓글은 leased NLP queue에서 처리하고 문서별 term frequency와 segment 검색 term을 저장한다.
- target/owner membership 및 ref-count를 저장한다. 같은 영상이 여러 target을 통해 보이더라도 owner 코퍼스의 N/DF에는 한 번만 반영한다.
- 전체 기간과 일별 N, DF, 총 TF를 old/new delta로 갱신한다. 분석 요청은 원문이나 문서별 term 전체를 다시 세지 않고 집계 row로 TF-IDF를 계산한다.
- 영상 검색은 대본 segment GIN 후보에 ACL을 적용한 뒤 segment 원문과 시작 시각만 스니펫으로 반환한다.
- 영상 대본 TF-IDF와 댓글 TF-IDF는 서로 다른 코퍼스로 API와 Analysis 화면에 표시한다.
- YouTube commentThreads/replies의 비-quota HTTP 403은 삭제·비공개·댓글 제한 항목으로 기록하고 다음 영상/댓글로 진행한다. quota 계열 403은 기존 quota 대기 정책을 유지한다.
8. 계산된 IDF는 문서마다 저장하지 않고 N과 DF로 산출하며, 최종 상위 키워드 결과만 캐시한다.

## 2. 현재 상태

### 2.1 대본

- SearchAPI로 대본을 수집한다.
- 한국어를 우선 요청하고, 없으면 영어 대본을 시도한다.
- `video_transcripts.full_text`에 전체 대본을 저장한다.
- `video_transcript_segments`에 시작 시각, 길이, 텍스트를 구간별로 저장한다.
- 개별 영상 대본 조회 API는 존재하지만 웹에서는 사용하지 않는다.
- 현재 통합 검색과 분석은 저장된 대본을 읽지 않는다.

### 2.2 검색

- 영상 검색 대상은 영상 ID, 제목, 설명, 채널명, 핸들이다.
- 댓글 검색은 댓글 원문을 대상으로 한다.
- PostgreSQL 16과 `pg_trgm` 확장이 이미 구성되어 있다.
- 검색 응답의 영상 결과에는 대본 일치 스니펫 필드가 없다.

### 2.3 분석

- 현재 `kiwipiepy.Kiwi`가 댓글 텍스트를 분석한다.
- 일반명사 `NNG`, 고유명사 `NNP`의 출현 횟수를 그대로 합산한다.
- 자주 쓰이지만 의미 구분력이 낮은 단어를 문서 빈도로 낮추지 못한다.
- 일부 경로는 분석 요청 시 댓글 표본을 읽어 형태소 분석하므로 CPU 비용이 반복된다.
- 영상 대본 키워드는 계산하지 않는다.

## 3. 확정 결정

### 3.1 형태소 분석기

`../ai-assistant`의 혼합 언어 분석 방식을 Monitube에 맞게 이식한다.

- 한국어: `konlpy.tag.Mecab`
- 영어: NLTK `word_tokenize` + `pos_tag`
- 한국어와 영어가 섞인 문장은 MeCab의 `SL` 구간을 모아 NLTK로 전달한다.
- 숫자, URL, 이모지, 기호는 키워드 정책에 따라 제외한다.
- 결과는 Unicode 정규화와 영문 소문자화를 거친다.

초기 키워드 허용 품사는 의미 없는 의존명사·대명사 유입을 줄이기 위해 참조 구현보다 보수적으로 시작한다.

- 한국어: `NNG`, `NNP`
- 영어: `NN`, `NNS`, `NNP`, `NNPS`
- `NNB`, `NP`, `NR`, `SN`은 기본 제외하며 품질 평가 후 별도 승인으로 추가한다.

### 3.2 폴백 금지

다음 동작은 구현하지 않는다.

- PeCab 폴백
- 정규식 토큰화 폴백
- MeCab 초기화 실패 후 Kiwi 사용
- NLTK 리소스 누락 시 영문 정규식 추출
- 런타임 NLTK 자동 다운로드 후 조용한 계속 실행

MeCab 또는 필수 NLTK 리소스가 없으면 다음 원칙을 적용한다.

1. 컨테이너 빌드 단계의 스모크 테스트를 실패시킨다.
2. 애플리케이션 시작 시 분석기 초기화를 검증한다.
3. 초기화 실패는 명시적인 오류 코드와 로그를 남긴다.
4. NLP 기능 플래그가 활성화된 서비스는 readiness를 실패시키거나 분석 워커를 종료한다.
5. 기존 빈도 분석으로 자동 우회하지 않는다.

기능 플래그가 꺼진 구버전 경로와 런타임 폴백은 구분한다. 단계적 배포 중 플래그를 끄는 것은 롤백 수단이지만, 활성화된 MeCab 경로 내부에는 대체 분석기를 두지 않는다.

### 3.3 PostgreSQL 기능의 사용 범위

PostgreSQL 내장 기능은 검색과 집계 기반으로 사용한다.

- `pg_trgm` + GIN: 기존 제목·설명·댓글 검색과 원문 부분 일치 보조
- 배열 GIN 또는 `tsvector` + GIN: MeCab으로 정규화한 대본 용어 검색
- 증분 SQL 집계: 문서 추가·수정·삭제 시 문서 수, 문서 빈도, 용어 빈도 갱신
- 분석 결과 캐시: 기존 `analysis_runs`, `analysis_results` 활용

PostgreSQL의 `ts_rank`는 전체 코퍼스의 문서 빈도를 사용하지 않으므로 TF-IDF 자체로 간주하지 않는다. `ts_stat`은 검증·운영 진단에는 사용할 수 있지만, 매 요청마다 전체 벡터를 스캔하는 주 계산 경로로 사용하지 않는다.

참고:

- PostgreSQL 검색 순위: <https://www.postgresql.org/docs/current/textsearch-controls.html>
- PostgreSQL 문서 통계 `ts_stat`: <https://www.postgresql.org/docs/current/textsearch-features.html>
- PostgreSQL `pg_trgm`: <https://www.postgresql.org/docs/16/pgtrgm.html>

## 4. MeCab 및 NLTK 설치 계획

### 4.1 참조 구현에서 가져올 요소

`../ai-assistant`에서 다음을 기준으로 삼는다.

- `mecab-0.996-ko-0.9.2.tar.gz`
- `mecab-ko-dic-2.1.1-20180720.tar.gz`
- `konlpy.tag.Mecab` 인터페이스
- 한국어/영어 구간 분리 방식
- NLTK noun POS 추출 방식

참조 `install.sh`를 그대로 복사하지는 않는다. 해당 스크립트의 `! command` 형태는 명령 실패 상태를 반전시켜 설치 오류를 숨길 수 있고, 설치 중 외부 `curl` 스크립트를 실행하므로 재현 가능한 빌드에 적합하지 않다.

### 4.2 Monitube용 설치 방식

1. 필요한 MeCab 소스와 사전 아카이브를 Monitube 빌드 컨텍스트에 버전 고정하여 둔다.
2. 각 아카이브의 SHA-256을 문서와 빌드에서 검증한다.
3. `infra/mecab/install.sh`는 `set -euo pipefail`로 시작한다.
4. 빌드 의존성을 설치하고 MeCab 및 한국어 사전을 컴파일한다.
5. Python 패키지는 `konlpy`, MeCab Python 바인딩, `nltk`를 버전 고정한다.
6. NLTK 리소스는 이미지 빌드 중 고정 경로에 설치한다.
   - `punkt_tab` 또는 현재 고정 NLTK 버전에 필요한 tokenizer 리소스
   - `averaged_perceptron_tagger_eng`
7. 운영 런타임에는 네트워크 다운로드를 허용하지 않는다.
8. API와 worker 이미지 모두 동일한 분석기 런타임을 포함한다.

API에도 MeCab을 설치하는 이유는 짧은 검색 질의를 수집 문서와 동일한 규칙으로 정규화해야 하기 때문이다. 장문 대본·댓글의 색인은 analysis-worker가 담당하여 API 요청 경로의 CPU 사용을 제한한다.

### 4.3 빌드 및 시작 검증

이미지 빌드에서 최소한 다음을 검증한다.

```python
from konlpy.tag import Mecab

tagger = Mecab()
assert ("영상", "NNG") in tagger.pos("영상 분석")
```

NLTK는 리소스 존재와 영문 고유명사 추출을 검증한다.

```python
import nltk

nltk.data.find("tokenizers/punkt_tab")
nltk.data.find("taggers/averaged_perceptron_tagger_eng")
```

시작 시 한 번 실행하는 analyzer health check에는 다음을 기록한다.

- `analyzer_version`
- MeCab 사전 경로와 사전 버전
- NLTK 버전과 리소스 버전
- 고정 한국어·영어·혼합 문장의 토큰 결과 해시

원문이나 사용자 검색어는 로그에 남기지 않는다.

## 5. NLP 모듈 설계

새 모듈 예시:

```text
apps/api/monitube_api/nlp/
  __init__.py
  analyzer.py
  policy.py
  tfidf.py
  health.py
```

핵심 인터페이스:

```python
class NounAnalyzer(Protocol):
    version: str

    def extract(self, text: str) -> list[str]: ...
```

구현체는 `MecabNltkNounAnalyzer` 하나만 제공한다. 테스트용 fake는 허용하지만 운영용 대체 구현은 제공하지 않는다.

정규화 정책:

- 입력의 NUL 제거
- Unicode NFC 정규화
- 영문 `casefold()`
- 앞뒤 공백 제거
- 기본 최소 길이 2자
- 한국어 단일 글자 고유명사는 별도 allowlist가 없으면 제외
- URL, 이메일, 순수 숫자 제외
- stopword는 코드에 하드코딩하지 않고 버전이 있는 정책 파일로 관리
- 동일 문서 내 중복은 TF 계산을 위해 보존

분석기 버전 예시:

```text
mecab-nltk-v1:mecab-0.996-ko-0.9.2:dic-2.1.1:nltk-<pinned>:policy-v1
```

버전이 바뀌면 기존 색인을 묵시적으로 섞지 않고 재색인 대상으로 표시한다.

## 6. 데이터 모델

신규 migration은 `020_transcript_search_mecab_tfidf.sql`로 작성한다.

### 6.1 NLP 문서 상태

```sql
CREATE TABLE nlp_documents (
  document_kind TEXT NOT NULL CHECK (document_kind IN ('video_transcript', 'comment')),
  document_id UUID NOT NULL,
  video_id UUID NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
  content_hash TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  document_date DATE NOT NULL,
  token_count INTEGER NOT NULL CHECK (token_count >= 0),
  state TEXT NOT NULL CHECK (state IN ('pending', 'ready', 'failed')),
  error_code TEXT,
  indexed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (document_kind, document_id)
);
```

`document_id`는 종류에 따라 `video_transcripts.id` 또는 `comments.id`를 가리킨다. PostgreSQL의 다형 FK를 억지로 만들지 않고, 삽입 서비스에서 종류별 존재를 검증한다. `document_date`는 대본이면 영상 게시일, 댓글이면 댓글 게시일을 사용하고 없으면 source fetched date로 대체한다. 삭제 정리는 원본 삭제 트리거 또는 명시적인 repository delete 경로로 보장한다.

### 6.2 문서별 용어 통계

```sql
CREATE TABLE nlp_document_terms (
  document_kind TEXT NOT NULL,
  document_id UUID NOT NULL,
  term TEXT NOT NULL,
  term_count INTEGER NOT NULL CHECK (term_count > 0),
  PRIMARY KEY (document_kind, document_id, term),
  FOREIGN KEY (document_kind, document_id)
    REFERENCES nlp_documents(document_kind, document_id)
    ON DELETE CASCADE
);

CREATE INDEX nlp_document_terms_term_document_idx
  ON nlp_document_terms (document_kind, term, document_id);
```

원문 대신 용어와 빈도만 저장하므로 분석 요청의 읽기량을 제한할 수 있다.

### 6.3 대본 세그먼트 검색 용어

기존 `video_transcript_segments`에 다음을 추가한다.

```sql
ALTER TABLE video_transcript_segments
  ADD COLUMN search_terms TEXT[] NOT NULL DEFAULT '{}',
  ADD COLUMN analyzer_version TEXT;

CREATE INDEX video_transcript_segments_search_terms_gin_idx
  ON video_transcript_segments USING gin (search_terms);
```

세그먼트 원문은 스니펫 표시용으로 유지하고, `search_terms`는 질의 후보를 빠르게 찾는 데 사용한다. 다중 검색어는 GIN 후보를 먼저 얻은 후 일치 용어 수와 용어 중요도로 순위를 계산한다.

### 6.4 범위별 문서 membership

동일한 영상이나 댓글이 여러 수집 target에 연결될 수 있으므로 단순히 target 통계를 더하면 중복 집계가 발생한다. 각 분석 범위에서 문서를 정확히 한 번만 세기 위한 membership을 저장한다.

```sql
CREATE TABLE nlp_scope_documents (
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('target', 'owner')),
  scope_id UUID NOT NULL,
  document_kind TEXT NOT NULL,
  document_id UUID NOT NULL,
  document_date DATE NOT NULL,
  membership_ref_count INTEGER NOT NULL DEFAULT 1
    CHECK (membership_ref_count > 0),
  PRIMARY KEY (scope_kind, scope_id, document_kind, document_id),
  FOREIGN KEY (document_kind, document_id)
    REFERENCES nlp_documents(document_kind, document_id)
    ON DELETE CASCADE
);

CREATE INDEX nlp_scope_documents_date_idx
  ON nlp_scope_documents (
    scope_kind, scope_id, document_kind, document_date, document_id
  );
```

- target 범위는 collection target 하나를 나타낸다.
- owner 범위는 사용자가 볼 수 있는 모든 문서의 중복 제거된 합집합이다.
- 같은 문서가 한 owner에게 여러 subscription 경로로 보이면 `membership_ref_count`만 증가한다.
- N/DF/TF는 ref count가 0에서 1이 될 때만 증가하고, 1에서 0이 될 때만 감소한다.
- 채널·키워드 분석은 해당 target 통계를 사용하거나 선택된 target 집합을 owner membership 기준으로 중복 제거한다.

### 6.5 전체 기간 증분 통계

TF-IDF 계산에 필요한 `N`, `DF`, 총 TF를 원문이나 모든 문서별 term에서 매번 다시 집계하지 않는다. 범위와 코퍼스별로 증분 저장한다.

```sql
CREATE TABLE nlp_corpus_stats (
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('target', 'owner')),
  scope_id UUID NOT NULL,
  document_kind TEXT NOT NULL
    CHECK (document_kind IN ('video_transcript', 'comment')),
  analyzer_version TEXT NOT NULL,
  document_count BIGINT NOT NULL DEFAULT 0 CHECK (document_count >= 0),
  total_token_count BIGINT NOT NULL DEFAULT 0 CHECK (total_token_count >= 0),
  data_version BIGINT NOT NULL DEFAULT 0 CHECK (data_version >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (scope_kind, scope_id, document_kind, analyzer_version)
);

CREATE TABLE nlp_term_stats (
  scope_kind TEXT NOT NULL,
  scope_id UUID NOT NULL,
  document_kind TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  term TEXT NOT NULL,
  document_frequency BIGINT NOT NULL DEFAULT 0
    CHECK (document_frequency >= 0),
  total_term_frequency BIGINT NOT NULL DEFAULT 0
    CHECK (total_term_frequency >= 0),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (
    scope_kind, scope_id, document_kind, analyzer_version, term
  ),
  FOREIGN KEY (scope_kind, scope_id, document_kind, analyzer_version)
    REFERENCES nlp_corpus_stats(
      scope_kind, scope_id, document_kind, analyzer_version
    ) ON DELETE CASCADE
);

CREATE INDEX nlp_term_stats_rank_idx
  ON nlp_term_stats (
    scope_kind, scope_id, document_kind, analyzer_version,
    document_frequency DESC, total_term_frequency DESC
  );
```

저장 의미:

- `document_count`: 코퍼스 전체 문서 수 `N`
- `total_token_count`: 코퍼스 전체 유효 token 수
- `document_frequency`: 해당 term이 한 번 이상 나타난 문서 수 `DF`
- `total_term_frequency`: 코퍼스 전체에서 해당 term이 나타난 총 횟수
- `data_version`: 통계를 바꾸는 transaction마다 증가하여 분석 캐시 freshness 판정에 사용

계산된 IDF 자체는 저장하지 않는다. 새 문서가 추가되면 N이 변해 모든 term의 IDF가 변하기 때문에 IDF row를 일괄 갱신하지 않고, 저장된 N과 DF로 조회 시 계산한다.

### 6.6 기간별 증분 통계

임의 기간 분석도 문서별 term을 다시 스캔하지 않도록 문서가 속한 날짜 bucket의 통계를 함께 유지한다.

```sql
CREATE TABLE nlp_corpus_daily_stats (
  scope_kind TEXT NOT NULL,
  scope_id UUID NOT NULL,
  document_kind TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  bucket_date DATE NOT NULL,
  document_count BIGINT NOT NULL DEFAULT 0 CHECK (document_count >= 0),
  total_token_count BIGINT NOT NULL DEFAULT 0 CHECK (total_token_count >= 0),
  PRIMARY KEY (
    scope_kind, scope_id, document_kind, analyzer_version, bucket_date
  )
);

CREATE TABLE nlp_term_daily_stats (
  scope_kind TEXT NOT NULL,
  scope_id UUID NOT NULL,
  document_kind TEXT NOT NULL,
  analyzer_version TEXT NOT NULL,
  bucket_date DATE NOT NULL,
  term TEXT NOT NULL,
  document_frequency BIGINT NOT NULL DEFAULT 0
    CHECK (document_frequency >= 0),
  total_term_frequency BIGINT NOT NULL DEFAULT 0
    CHECK (total_term_frequency >= 0),
  PRIMARY KEY (
    scope_kind, scope_id, document_kind, analyzer_version,
    bucket_date, term
  )
);

CREATE INDEX nlp_term_daily_stats_range_idx
  ON nlp_term_daily_stats (
    scope_kind, scope_id, document_kind, analyzer_version,
    bucket_date, term
  );
```

한 문서는 정확히 하나의 `document_date`에 속하므로 기간의 `N`, `DF`, 총 TF는 일별 row를 합산해 정확히 계산할 수 있다. 날짜가 수정되면 이전 bucket에서 차감하고 새 bucket에 더한다.

### 6.7 분석 결과 캐시

`analysis_results`에 저장되는 영상/댓글 상위 키워드는 다음 값으로 식별한다.

- scope kind/id
- document kind
- 기간 필터
- analyzer version
- TF-IDF pipeline version
- corpus `data_version`

문서나 membership 변경으로 관련 통계가 갱신되면 연결된 캐시를 `stale`로 표시하고 analysis-worker가 새 상위 결과를 생성한다. API 요청은 원문 분석이나 전체 DF 재집계를 수행하지 않는다.

## 7. 색인 파이프라인

### 7.1 작업 소유권

- 수집 worker는 영상·대본·댓글 원문 저장에 집중한다.
- analysis-worker가 `pending` NLP 문서를 claim하여 MeCab+NLTK 분석을 수행한다.
- API는 장문 원문을 토큰화하지 않는다.
- API의 검색어처럼 짧은 입력만 동기 분석한다.

### 7.2 대본 색인

1. `video_transcripts`가 `available`로 upsert되면 NLP 문서를 `pending`으로 등록한다.
2. content hash가 기존 ready 문서와 같고 analyzer version도 같으면 건너뛴다.
3. analysis-worker가 전체 대본을 한 번 분석하여 문서별 term count를 저장한다.
4. 각 대본 segment도 같은 분석기로 처리하여 `search_terms`를 저장한다.
5. 문서 term과 기존 term의 차이를 계산한다.
6. 관련 target/owner의 전체 기간 및 일별 N/DF/총 TF를 증분 갱신한다.
7. 문서 term, segment terms, 집계 통계, NLP 상태를 한 트랜잭션에서 ready로 전환한다.
8. 관련 분석 캐시를 stale로 표시한다.
9. 실패 시 `failed`와 안전한 error code를 저장하며 원문은 유지한다.

### 7.3 댓글 색인

1. 댓글 page가 저장된 후 신규·변경 댓글 ID를 NLP queue에 등록한다.
2. 댓글 하나를 TF-IDF의 문서 하나로 정의한다.
3. batch 단위로 원문을 읽고 분석한다.
4. 기존 content hash와 analyzer version이 같으면 건너뛴다.
5. 관련 target/owner의 전체 기간 및 일별 통계를 같은 트랜잭션에서 증분 갱신한다.
6. 댓글 삭제 시 문서 용어와 scope 통계를 반대로 차감한다.

### 7.4 증분 통계 변경 규칙

신규 문서가 한 scope에 처음 포함될 때:

```text
corpus.document_count += 1
corpus.total_token_count += document.token_count

문서의 각 고유 term마다:
  term.document_frequency += 1
  term.total_term_frequency += document.term_count
```

동일 문서가 같은 scope에 다른 membership 경로로 다시 연결되면 ref count만 증가하고 통계는 바꾸지 않는다.

문서가 수정될 때는 이전 term map과 새 term map의 차이만 반영한다.

- 이전에 없고 새로 생긴 term: `DF +1`, 총 TF에 새 count 추가
- 이전에 있었고 사라진 term: `DF -1`, 총 TF에서 이전 count 차감
- 양쪽에 있는 term: DF 유지, 총 TF에 count 차이만 반영
- token 수 변경: corpus total token count에 차이 반영
- 날짜 변경: 이전 daily bucket에서 전체 문서를 차감한 뒤 새 bucket에 추가

문서가 scope에서 완전히 제거될 때는 위 증가분을 반대로 적용한다. 0이 된 term stats row는 삭제하여 테이블 팽창을 제한한다.

집계 변경 transaction은 다음 순서로 row lock을 획득한다.

1. scope를 `(scope_kind, scope_id)` 순서로 정렬
2. corpus stats row lock
3. term을 사전순으로 정렬하여 term stats row lock
4. daily stats row lock

이 순서를 고정해 동시 색인·구독 변경·삭제 간 deadlock 위험을 줄인다.

### 7.5 처리 제한

- `FOR UPDATE SKIP LOCKED` 기반 다중 워커 claim
- 기본 batch 크기: 댓글 200개, 대본 10개
- 작업당 최대 원문 바이트 제한
- 한 번에 처리할 최대 대본 segment 수 제한
- 작업 lease와 재시도 횟수 제한
- 문서별 timeout과 전체 batch timeout
- OOM 방지를 위해 iterator/batch 기반으로 처리
- analysis-worker의 기존 CPU 및 memory limit 유지

실측 후 batch 크기를 조정하며, 설정값은 환경 변수로 노출한다.

## 8. TF-IDF 계산

### 8.1 코퍼스 분리

영상과 댓글을 같은 코퍼스에 넣지 않는다.

- 영상 코퍼스: 선택 범위에서 대본 상태가 ready인 영상, 영상 하나가 문서 하나
- 댓글 코퍼스: 선택 범위에서 NLP 상태가 ready인 댓글, 댓글 하나가 문서 하나

댓글의 짧은 길이와 대본의 긴 길이가 서로 점수에 영향을 주지 않는다.

### 8.2 저장 값과 계산 값

영구 저장하는 값:

- 문서별 `term_count`
- 문서별 전체 token 수
- 코퍼스별 전체 문서 수 `N`
- term별 문서 빈도 `DF`
- term별 총 출현 횟수
- 같은 값의 일별 bucket

영구 저장하지 않는 값:

- 모든 term의 계산 완료 IDF
- 영상 또는 댓글 테이블의 단어별 동적 컬럼
- 모든 기간·필터 조합의 TF-IDF 점수

IDF는 숫자 두 개 `N`, `DF`로 즉시 계산할 수 있다. 최종 상위 키워드 결과만 기존 analysis result cache에 저장한다.

### 8.3 점수

기본 공식:

```text
scope_TF(term) = 1 + ln(total_term_frequency(term))
IDF(term)      = ln((N + 1) / (DF(term) + 1)) + 1
score(term)    = scope_TF × IDF
```

개별 영상의 키워드가 필요한 경우에는 해당 영상의 `term_count`를 TF로 사용하고, 영상이 속한 분석 scope의 N과 DF를 IDF 기준으로 사용한다.

```text
document_TF(term) = 1 + ln(document_term_count)
document_score    = document_TF × scope_IDF
```

새 문서가 추가되면 N과 일부 term의 DF/총 TF만 증분 갱신한다. 기존 모든 문서나 모든 term의 IDF row를 다시 쓰지 않는다. 상위 키워드 캐시는 변경된 `data_version`을 기준으로 백그라운드 재생성한다.

반환 순서는 다음으로 고정한다.

1. `corpus_score DESC`
2. `document_count DESC`
3. `term_count DESC`
4. `term ASC`

점수만으로 일회성 오타가 상위에 오르지 않도록 다음 guard를 적용한다.

- 기본 `document_count >= 2`
- 코퍼스가 작은 경우에만 `document_count >= 1`
- `df / N`이 지나치게 높은 용어는 IDF로 낮추되 필요하면 최대 문서 비율 필터 적용
- 최소 길이와 stopword policy 적용
- 결과 최대 15개

### 8.4 응답 모델

기존 `TopWord { word, count }`를 새 분석 결과의 유일한 모델로 계속 사용하지 않는다.

```text
KeywordScore
  term: string
  score: number
  termCount: integer
  documentCount: integer
  documentRate: number
```

`AnalysisOverviewResponse`에 다음을 추가한다.

```text
videoKeywords: KeywordScore[]
commentKeywords: KeywordScore[]
```

전환 기간에는 기존 `topWords`를 유지하되 새 UI는 새 필드를 읽는다. 안정화 후 API major contract 또는 명시된 호환 기간에 맞춰 `topWords`를 제거한다.

coverage에는 다음을 추가한다.

- `transcriptDocuments`
- `indexedTranscriptDocuments`
- `commentDocuments`
- `indexedCommentDocuments`
- `analyzerVersion`
- `keywordStatus`: `fresh | stale | building | failed`

## 9. 대본 기반 영상 검색

### 9.1 검색 동작

기존 검색 scope는 유지한다.

- `scope=videos`: 영상 메타데이터와 대본 검색
- `scope=comments`: 댓글 검색
- `scope=all`: 두 결과 모두

영상 검색 절차:

1. 검색어를 MeCab+NLTK로 정규화한다.
2. 기존 title/description/channel `pg_trgm` 후보를 구한다.
3. `video_transcript_segments.search_terms` GIN 후보를 구한다.
4. 반드시 사용자에게 보이는 video membership ACL을 적용한다.
5. 메타데이터 점수와 대본 일치 점수를 합산한다.
6. 영상별 가장 높은 대본 segment 하나만 선택한다.
7. 최종 limit을 적용한다.

대본이 아직 색인되지 않았더라도 기존 영상 메타데이터 검색은 계속 동작한다. 이것은 분석기 폴백이 아니라 독립 검색 필드의 부분 가용성이다.

### 9.2 검색 순위

초기 가중치:

- 영상 ID 완전 일치: 1.00
- 제목 일치: 0.90
- 채널/핸들 일치: 0.80
- 설명 일치: 0.65
- 대본 segment 일치: 0.70

대본 점수는 다음을 조합한다.

- 질의 용어 coverage
- segment 내 term frequency
- 분석 run에서 사용할 수 있는 IDF
- 동일 영상에서 일치한 segment 수의 제한된 boost

점수는 기존 API 계약에 맞게 0~1로 정규화한다. 페이지별 min-max 정규화는 결과 집합에 따라 값이 흔들리므로 사용하지 않고, 고정된 bounded formula를 사용한다.

### 9.3 스니펫

`SearchVideoResult`에 다음을 추가한다.

```text
transcriptSnippet?: string
transcriptStartMs?: integer
transcriptDurationMs?: integer
transcriptLanguage?: string
```

스니펫 규칙:

- 가장 높은 점수의 segment 원문 사용
- 기본 최대 180자
- 필요하면 앞뒤 인접 segment를 포함하되 최대 길이 준수
- 전체 대본은 검색 응답에 포함하지 않음
- HTML은 서버에서 생성하지 않고 클라이언트가 안전하게 강조
- 검색어가 없는 원문 앞부분을 임의 스니펫으로 사용하지 않음

`matchedFields`에는 `transcript`를 추가한다.

## 10. 웹 UI

### 10.1 Explore 검색

- 영상 검색 placeholder를 `영상 제목, 채널, 대본 검색`으로 변경한다.
- 대본 일치 영상은 제목 아래에 2~3줄 스니펫을 표시한다.
- `대본 · 03:14`처럼 일치 필드와 시작 시각을 표시한다.
- 스니펫 클릭 시 기존 영상 상세를 열고 가능하면 해당 YouTube 시각 링크를 제공한다.
- 전체 대본 본문은 검색 결과 목록에 표시하지 않는다.

### 10.2 Analysis

Analysis의 언어 패널을 뷰에 따라 분리한다.

- Overview: `영상에서 다룬 키워드`와 `댓글에서 반응한 키워드`를 나란히 표시
- Videos: 영상 대본 TF-IDF만 표시
- Comments: 댓글 TF-IDF만 표시

각 항목은 단순 빈도 대신 중요도 점수를 주로 보여주고, tooltip 또는 보조 텍스트에 문서 수와 총 출현 수를 표시한다.

예시:

```text
1. 반도체       중요도 8.42 · 영상 12개 · 38회
2. 공급망       중요도 7.91 · 영상 8개 · 17회
```

## 11. 애플리케이션 변경 범위

예상 파일과 책임:

### Runtime/설치

- `infra/mecab/install.sh`: fail-fast native 설치
- `infra/mecab/vendor/*`: 버전 고정 아카이브와 checksum
- `infra/docker/api.Dockerfile`: 검색 질의 분석용 MeCab+NLTK
- `infra/docker/worker.Dockerfile`: NLP 색인 및 분석용 MeCab+NLTK
- `apps/api/pyproject.toml`: Kiwi 제거, KoNLPy/MeCab binding/NLTK 추가

### Domain/repository

- `apps/api/monitube_api/nlp/*`: analyzer와 TF-IDF 정책
- `apps/api/monitube_api/domain.py`: NLP document/term record
- `apps/api/monitube_api/ports/*`: claim, persist, query port
- `apps/api/monitube_api/infrastructure/postgres_*`: NLP queue/term 저장, scope membership, 증분 N/DF/TF와 TF-IDF SQL
- `apps/api/monitube_api/infrastructure/memory_*`: 동일 계약의 테스트 구현
- `database/migrations/020_transcript_search_mecab_tfidf.sql`: additive schema/index

### Worker

- `apps/worker/monitube_worker/analysis_worker.py`: NLP document indexing phase 추가
- 대본/댓글 저장 경로: pending NLP document 등록
- 분석 pipeline version: `deterministic-v4-mecab-tfidf`

### API/contracts

- `apps/api/monitube_api/contracts.py`: snippet, keyword score, coverage 계약
- `apps/api/monitube_api/application/explore_service.py`: 대본 검색 응답
- `apps/api/monitube_api/application/analysis_service.py`: 영상/댓글 키워드 분리
- `apps/api/monitube_api/infrastructure/postgres_explore.py`: 대본 segment 후보와 ACL
- `apps/api/monitube_api/infrastructure/postgres_analysis.py`: scoped TF-IDF 조회

### Web

- `apps/web/app/lib/api/types.ts`: 새 계약
- `apps/web/app/lib/api/normalizers.ts`: snake/camel 호환
- `apps/web/app/features/collection/workbench-explore.tsx`: 대본 스니펫
- `apps/web/app/features/analysis/analysis-dashboard.tsx`: 영상/댓글 키워드 패널

### 제거 대상

안정화 이후 다음을 제거한다.

- `kiwipiepy` 의존성
- `analysis.py`의 Kiwi 초기화 코드
- 요청 시 원문 전체를 다시 Kiwi로 토큰화하는 경로
- 빈도 기반 `top_words_from_texts`를 production 결과에 사용하는 경로

질문 감지 정규식 등 형태소 분석과 무관한 휴리스틱은 별도 동작으로 유지할 수 있다.

## 12. 구현 단계

### Phase 0. 기준선과 품질 fixture

- 현재 Kiwi 결과와 처리 시간 측정
- 실제 댓글/대본에서 비식별 fixture 구성
- 한국어, 영어, 혼합 문장 golden token 정의
- 빈도 상위어와 TF-IDF 상위어 비교 기준 정의
- 현재 검색 latency와 분석-worker memory 기준선 기록

완료 조건:

- 최소 100개 댓글과 20개 대본의 재현 가능한 품질 fixture
- 현재 p50/p95 분석 시간 및 peak RSS 기록

### Phase 1. MeCab+NLTK runtime

- fail-fast 설치 스크립트 작성
- API/worker 이미지에 동일 버전 설치
- NLTK build-time resource 설치
- analyzer 모듈과 health check 구현
- PeCab/정규식/Kiwi 폴백 부재 테스트

완료 조건:

- 로컬/CI 컨테이너에서 고정 token fixture 통과
- MeCab 사전 제거 이미지가 빌드 또는 readiness에서 실패
- NLTK 리소스 제거 이미지가 빌드 또는 readiness에서 실패

### Phase 2. Additive schema와 색인 작업

- migration 020 추가
- NLP document claim/persist 구현
- target/owner document membership과 ref count 구현
- 전체 기간 및 일별 corpus/term stats 구현
- 대본/댓글 pending 등록
- analysis-worker batch index 구현
- analyzer version과 content hash idempotency 구현
- 추가·수정·삭제 차분을 하나의 transaction으로 반영
- aggregate data version 및 cache stale 처리 구현

완료 조건:

- 동일 문서 재처리 시 term row 중복 없음
- 대본 갱신 시 기존 terms와 segment terms가 원자적으로 교체
- 새 문서 추가 시 전체 스캔 없이 N/DF/총 TF 증가
- 수정 시 바뀐 term과 token 수 차이만 반영
- 삭제 시 N/DF/총 TF와 일별 통계가 정확히 차감
- 중복 membership은 ref count만 바꾸고 통계를 중복 증가시키지 않음

### Phase 3. 기존 데이터 backfill

- 대본과 댓글을 별도 queue로 backfill
- 대본을 먼저 처리해 검색 기능 가치를 빠르게 확보
- checkpoint, 속도 제한, 중단/재개 구현
- backfill progress와 실패 원인 노출
- backfill 완료 후 문서 term에서 aggregate stats를 재구성하는 reconciliation 실행

완료 조건:

- 중단 후 중복 없이 재개
- 수집 worker latency에 유의미한 회귀 없음
- 지정 memory/CPU 제한 안에서 완료
- 증분 stats와 reconciliation 결과 일치

### Phase 4. 대본 검색과 스니펫

- 검색 질의 MeCab+NLTK 정규화
- transcript segment GIN 후보 조회
- ACL-first 또는 candidate 후 즉시 ACL 적용 검증
- 영상별 best segment 선정
- API/Web snippet 계약 적용
- `ENABLE_TRANSCRIPT_SEARCH` 플래그로 shadow/점진 배포

완료 조건:

- 제목에 검색어가 없고 대본에만 있는 영상 검색 성공
- 응답에 전체 대본 미포함
- 다른 사용자의 영상/대본 결과 유출 없음
- 목표 p95 내 검색 완료

### Phase 5. TF-IDF 분석

- 영상/댓글의 전체 기간 및 일별 증분 stats 조회 SQL 구현
- 저장된 N/DF/총 TF에서 IDF와 score를 계산하는 SQL 구현
- `videoKeywords`, `commentKeywords` 계약 추가
- analysis result 캐시와 pipeline version 갱신
- Analysis 화면 분리
- `ENABLE_TFIDF_KEYWORDS` 플래그로 dual-read 비교

완료 조건:

- 모든 문서에 흔한 단어가 특정 주제 단어보다 낮은 점수
- 영상과 댓글 결과가 독립적으로 계산
- 분석 요청이 원문이나 전체 `nlp_document_terms`를 다시 집계하지 않음
- 기간 필터가 일별 stats 합산만으로 정확한 N/DF/총 TF를 반환
- 동일 data/analyzer/pipeline version 결과가 결정적
- API 요청 경로에서 장문 형태소 분석 없음

### Phase 6. Kiwi 제거와 최종 전환

- MeCab 결과 품질·성능 승인
- 새 필드 read 100% 전환
- Kiwi dependency와 production 호출 제거
- 이전 pipeline 결과의 보존/만료 정책 적용
- 운영 문서와 README 갱신

완료 조건:

- 저장소에서 production `Kiwi` 참조 0건
- MeCab 장애 시 자동 폴백 없이 명확한 실패
- 롤백 플래그와 이전 이미지가 검증됨

## 13. 테스트 계획

### 13.1 분석기 단위 테스트

- 한국어 일반명사/고유명사
- 영문 단수/복수/고유명사
- 한국어+영어 혼합 문장
- 조사와 어미가 붙은 한국어
- 숫자, 단위, URL, 이메일, 이모지
- 빈 문자열과 NUL 포함 문자열
- 매우 긴 문장의 timeout/분할
- 분석기 버전 결정성
- MeCab 누락 시 명시적 초기화 실패
- NLTK 리소스 누락 시 명시적 초기화 실패
- PeCab과 regex fallback 코드가 import graph에 없음을 확인

### 13.2 TF-IDF 테스트

- 한 문서에서 반복된 단어의 TF 증가
- 많은 문서에서 공통인 단어의 IDF 감소
- smoothing으로 0 나눗셈 없음
- 영상/댓글 corpus 분리
- ACL/기간/channel/keyword scope 반영
- 작은 corpus 정책
- 동일 점수 tie-break 결정성
- 신규 문서가 N을 한 번만 증가시킴
- 한 문서에서 같은 term이 여러 번 등장해도 DF는 한 번만 증가
- 문서 수정 시 추가/삭제/유지 term의 차분 갱신
- 문서 삭제 시 N/DF/총 TF 정확한 차감
- 문서 날짜 변경 시 daily bucket 이동
- 중복 target/subscription membership에서 owner 통계 중복 방지
- stats row가 0이 되면 안전하게 정리
- 동시 색인과 삭제의 row-lock 순서 및 retry
- 증분 통계와 원본 document term 전체 reconciliation 결과 일치

### 13.3 검색 테스트

- 대본에만 있는 한국어 검색어
- 대본에만 있는 영어 검색어
- 혼합 검색어
- 영상별 best segment 하나 반환
- 스니펫 길이 제한과 timestamp
- 기존 제목/채널/댓글 검색 회귀
- 2글자 한국어 명사 검색
- 접근 권한이 없는 transcript 제외
- 아직 NLP pending인 대본의 안전한 부분 가용성

### 13.4 성능 테스트

- 대본 segment 수 증가에 따른 GIN 검색 p95
- 댓글 50,000개 범위에서 저장된 aggregate stats 기반 TF-IDF analysis run
- 신규 문서 한 건당 증분 stats 갱신 시간과 lock wait
- 30/90/365일 daily stats 범위 합산 시간
- analysis-worker peak RSS/CPU
- API 검색 질의 형태소 분석 시간
- backfill 중 수집 worker와 PostgreSQL 부하
- `EXPLAIN (ANALYZE, BUFFERS)`로 ACL 및 GIN plan 확인

## 14. 관측성

로그와 메트릭은 원문을 기록하지 않고 다음만 제공한다.

- `nlp_documents_pending_total{kind}`
- `nlp_documents_processed_total{kind,state}`
- `nlp_document_processing_seconds{kind}`
- `nlp_tokens_total{kind}`
- `nlp_analyzer_init_failures_total`
- `transcript_search_candidates`
- `transcript_search_seconds`
- `tfidf_analysis_seconds{corpus}`
- `tfidf_documents{corpus}`
- `tfidf_terms{corpus}`
- `nlp_stats_updates_total{operation,kind}`
- `nlp_stats_update_seconds{operation,kind}`
- `nlp_stats_lock_wait_seconds`
- `nlp_stats_reconciliation_mismatches_total{kind}`
- `nlp_keyword_cache_stale_total{kind}`
- analyzer/policy/pipeline version

에러 코드는 예외 문자열을 그대로 노출하지 않고 다음처럼 제한한다.

- `mecab_library_missing`
- `mecab_dictionary_missing`
- `mecab_initialization_failed`
- `nltk_resource_missing`
- `nlp_document_too_large`
- `nlp_document_timeout`
- `nlp_persist_failed`

## 15. 배포 및 롤백

기능 플래그:

```text
ENABLE_MECAB_NLP_INDEX=false
ENABLE_TRANSCRIPT_SEARCH=false
ENABLE_TFIDF_KEYWORDS=false
NLP_ANALYZER_VERSION=mecab-nltk-v1
NLP_INDEX_BATCH_SIZE=200
NLP_DOCUMENT_TIMEOUT_SECONDS=30
```

배포 순서:

1. 새 이미지와 additive migration 배포, 모든 read flag off
2. analyzer health와 빈 queue 처리 smoke test
3. 신규 문서 dual-write/pending 등록 활성화
4. 증분 stats canary와 전체 reconciliation 비교
5. 대본 canary backfill
6. transcript search shadow query 및 결과 비교
7. 내부 사용자에게 transcript search read 활성화
8. 댓글/대본 TF-IDF dual-read 비교
9. TF-IDF UI 점진 활성화
10. 안정화 후 Kiwi 제거 이미지 배포

롤백:

- read flag를 꺼 기존 검색/분석 응답으로 복귀
- NLP worker flag를 꺼 새 색인 중단
- additive 테이블과 기존 대본/댓글 원문은 삭제하지 않음
- migration downgrade로 대규모 데이터 삭제를 수행하지 않음
- 이전 immutable image로 서비스 복귀

활성화된 MeCab 경로에서 분석기 오류가 나면 PeCab, regex, Kiwi로 전환하지 않고 해당 NLP 작업을 실패 상태로 남긴다.

## 16. 위험과 대응

### Native build 및 사전 호환성

- 위험: 오래된 MeCab/한국어 사전과 Python 3.12 바인딩 호환 문제
- 대응: Phase 1을 독립 PR로 진행하고, 이미지 build smoke test를 통과하기 전 schema 작업을 시작하지 않는다.

### 분석 품질 변화

- 위험: Kiwi와 MeCab의 고유명사 분리가 달라 기존 키워드 순위가 크게 변함
- 대응: golden corpus에서 token diff와 상위 TF-IDF 결과를 리뷰하고 analyzer/policy version을 고정한다.

### CPU 사용 증가

- 위험: MeCab+NLTK를 API 분석 요청에서 반복 실행
- 대응: 원문 색인은 analysis-worker에서 한 번만 실행하고 API는 짧은 검색어만 처리한다.

### DB 용량 증가

- 위험: 댓글별 term row가 댓글 원문보다 큰 저장 공간을 사용할 수 있음
- 대응: 품사·길이 필터, 문서당 최대 고유 term 수, 실제 평균 term row 수 측정, 필요 시 오래된 derived index 재생성 정책을 둔다.

### 통계 정합성

- 위험: 원문 수정·삭제·membership 변경과 증분 N/DF/TF 통계 불일치
- 대응: content hash/analyzer version idempotency, 고정 row-lock 순서, 원본 삭제 연동, data version, 정기 reconciliation과 자동 repair job을 제공한다.

### 집계 row lock 경합

- 위험: 인기 owner/target에 문서가 동시에 많이 들어오면 동일 corpus/term stats row 갱신이 직렬화될 수 있음
- 대응: batch 안에서 term delta를 먼저 합산하여 한 term당 한 번만 upsert하고, lock timeout/제한된 retry/queue lag 메트릭을 적용한다. 실측 임계치를 넘으면 문서 색인과 aggregate 반영을 별도 outbox 단계로 분리한다.

### 권한 경계

- 위험: 다른 owner의 DF 또는 대본 후보가 점수·스니펫에 섞이거나, 한 owner가 여러 target에서 보는 동일 문서가 중복 집계됨
- 대응: owner/target별 통계와 `nlp_scope_documents` ref count를 유지하고, 검색 후보는 최종 limit 전에 반드시 membership ACL을 통과시킨다.

## 17. 완료 기준

다음이 모두 충족되어야 완료로 본다.

- 대본에만 존재하는 검색어로 영상을 찾을 수 있다.
- 검색 결과는 관련 대본 스니펫과 시작 시각만 반환한다.
- 전체 대본이 검색 목록 응답에 노출되지 않는다.
- 영상 키워드와 댓글 키워드가 별도 TF-IDF 결과로 표시된다.
- 단순 빈도 기준의 흔한 단어가 IDF에 의해 낮아진다.
- 장문 원문은 분석 요청마다 재토큰화되지 않는다.
- 새 문서 추가 시 전체 코퍼스 DF를 다시 집계하지 않는다.
- N/DF/총 TF는 문서 추가·수정·삭제의 차분만으로 갱신된다.
- 기간 분석은 일별 통계 합산으로 수행된다.
- IDF 자체를 모든 문서에 중복 저장하거나 N 변경 때 일괄 갱신하지 않는다.
- production 분석 경로에 Kiwi가 남아 있지 않다.
- MeCab+NLTK가 누락되면 명확히 실패한다.
- PeCab 및 정규식 토큰화 폴백이 없다.
- 기존 제목·채널·댓글 검색과 수집 기능이 회귀하지 않는다.
- ACL, 삭제, 재색인, 롤백 검증이 완료된다.

## 18. 권장 PR 분할

1. **PR 1 — MeCab+NLTK runtime and analyzer**
   - 설치, dependency, analyzer, health, golden tests
2. **PR 2 — NLP document schema and indexing worker**
   - migration 020, pending/claim/persist, scope membership, 대본·댓글 term 색인
3. **PR 3 — Transcript video search and snippets**
   - repository/API/contracts/web, ACL/performance tests
4. **PR 4 — TF-IDF analysis and separate keyword panels**
   - 증분 N/DF/TF와 daily stats, scoped score SQL, analysis results, contracts/UI
5. **PR 5 — Backfill, rollout controls, Kiwi removal**
   - resumable backfill, metrics, final cutover and cleanup

각 PR은 독립적으로 rollback 가능해야 하며, 한 PR에서 native runtime·schema·검색·UI를 동시에 활성화하지 않는다.
