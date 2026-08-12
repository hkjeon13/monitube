# 폐기된 분석 계획

이 문서는 파일 경로를 참조하는 기존 링크와 배포 이력을 깨지 않기 위해 남겨 둔 폐기 안내다.
과거에 검토했던 TF-IDF 기반 순위는 제품 및 Rust 전환 범위에서 사용하지 않는다.

현재의 단일 기준 문서는 [Rust 전체 전환 개발 지침](RUST_FULL_MIGRATION_GUIDELINES.md)이다.

## 현재 확정 규칙

- Python `Mecab + NLTK` tokenizer API는 정제된 token 목록과 analyzer version만 반환한다.
- Rust NLP worker가 token 목록을 문서별 sparse BoW로 변환한다.
- 문서 추가·수정·삭제 시 Rust가 old/new BoW delta를 PostgreSQL 집계에 원자적으로 반영한다.
- 단어 순위는 `total_term_frequency DESC, term ASC`만 사용한다.
- `document_frequency`는 문서 수와 문서 비율을 표시하기 위한 보조 통계이며 순위에 사용하지 않는다.
- TF-IDF/IDF score는 계산, 저장, 조회, 캐시, API 응답 또는 UI 표시에 사용하지 않는다.
- 분석 조회는 원문을 다시 tokenize하거나 매번 전체 문서를 재집계하지 않고 저장된 BoW 집계를 읽는다.

## 이력 호환성

`database/migrations/020_transcript_search_mecab_tfidf.sql`은 이미 적용된 마이그레이션 이력이므로
파일명을 변경하지 않는다. 이 migration이 만든 DF 우선 인덱스는
`023_pure_frequency_ranking.sql`에서 제거되고 순수 빈도 인덱스로 대체된다.

새 구현과 운영 판단에서 이 문서의 과거 파일명은 분석 방식의 이름이 아니라 이력 식별자로만 취급한다.
