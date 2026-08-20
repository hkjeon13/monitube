# Monitube CNPG 중앙 DB 이전 계획

작성일: 2026-08-20  
검토 반영일: 2026-08-20  
상태: **실행 중 — 2026-08-20에 Phase 0~2 사전 검증과 복구 리허설을 시작했으며, production cutover는 parity와 별도 go/no-go 이후에만 수행한다.**

## 1. 목적과 확정된 목표

`monitube-prod`의 PostgreSQL 16 legacy StatefulSet에서 Monitube 데이터를 기존 중앙
CNPG Cluster인 `database/central-pg-data`의 전용 `monitube` database로 이전한다.

다음 토폴로지를 이 계획의 기준으로 확정한다.

- target cluster: `database/central-pg-data`
- target database/owner role: `monitube`
- application connection: 중앙 RW endpoint 또는 PgBouncer RW endpoint 중 rehearsal로 확정
- source of truth: cutover 완료 전까지 `monitube-prod/monitube-postgres`
- 목표 RPO: 0
- dual-write: 금지

별도 `monitube-prod/monitube-db` CNPG Cluster를 새로 만드는 기존 chart 경로는 이 이전의
목표가 아니다. 기존 `scripts/runbooks/cnpg-migration.md`도 별도 Cluster 생성을 전제로 하고
final sync 없이 cutover할 수 있으므로, 중앙 DB용 runbook으로 교체하기 전까지 실행하지 않는다.

## 2. 2026-08-20 현재 확인된 상태

| 항목 | 확인값 | 이전 계획에 주는 의미 |
| --- | --- | --- |
| Legacy DB | `monitube-postgres-0`, PostgreSQL 16, Running | 현재 유일한 writer/source of truth |
| Legacy storage | `monitube-postgres-recovery`, hostPath, `Retain` | 삭제 금지. 단일 노드 장애의 보호책은 아님 |
| Source 기준값 | 38 sources, 29,228 videos, 11,142,774 comments, migration 25, 약 34GB | cutover 시 새 기준선을 다시 측정해야 함 |
| 중앙 CNPG | `database/central-pg-data`, PostgreSQL 17.11, 3/3 Healthy | target Cluster는 이미 운영 중 |
| Target DB/role | `monitube` DB 및 login owner role 존재, role connection limit 100 | 새 DB를 만들지 말고 기존 빈 target의 provenance와 재사용 승인을 확인 |
| Target schema | table 0개 | 이전은 아직 완료되지 않음 |
| Target extension | `pgcrypto`, `pg_trgm`, `plpgsql` | source 필요 extension 및 `pg_stat_statements` 정책 재확인 필요 |
| 중앙 connection | `max_connections=300`, 관찰 시 약 112 sessions | 앱 pool·worker·migration reserve를 수치화해야 함 |
| PgBouncer | 2 replicas, `session` mode, replica당 default pool 50 | 앱 pool/prepared statement 및 장애 시 reconnect 검증 필요 |
| 중앙 backup | daily ScheduledBackup과 Barman ObjectStore, 최근 backup 성공 | Cluster 전체 복구와 Monitube 단독 복구를 별도로 검증해야 함 |
| 중앙 failure domain | CNPG 3개 instance가 동일한 단일 K3s node의 local storage 사용 | Pod HA는 있으나 node HA는 아님. off-node backup이 node-loss 복구 경계 |
| Target application Secret | `monitube-prod`에 없음 | 중앙 DB 전용 credential 전달 계약이 필요 |

`central-pg-data/monitube`가 비어 있더라도 임의로 drop/recreate하지 않는다. 누가 어떤 목적으로
생성했는지, 현재 접속자가 없는지, 중앙 DB 운영자가 재사용을 승인했는지 먼저 확인한다.

## 2.1 실행 기록 (2026-08-20 UTC)

| 단계 | 결과 | 근거/다음 gate |
| --- | --- | --- |
| 중앙 target 재검증 | 완료 | `monitube` DB owner와 login role은 `monitube`, superuser/createdb/createrole/replication 모두 false, public table 0개였다. |
| 중앙 접속 계약 | 완료 | `monitube-prod/monitube-central-db` namespace-local Secret을 생성했고, PgBouncer RW endpoint에 TLS로 접속 확인했다. Secret 값은 이 문서·Git·작업 로그에 기록하지 않는다. |
| connection budget | 완료 | API + 3 workers의 현재 max pool은 각각 2, 합계 8이다. migration 1, reconnect reserve 8을 더해도 role limit 100 및 central max_connections 300 안이다. |
| Source baseline | 완료 | preflight 기준 `38 collection_sources`, `2,260 channels`, `29,228 videos`, `11,142,774 comments`, migration 25, 약 34 GB. Cutover 직전에는 반드시 다시 측정한다. |
| Off-node logical backup | 완료 | source writer를 멈추지 않은 custom dump(3,552,948,934 bytes)를 생성했다. `pg_restore --list`와 SHA-256 `38a77de63e457446dbd7fdb320a8f582a432eeb8a29ca41247fc8a591a072e2b`를 검증했고, node 밖의 independent copy도 동일 checksum을 확인했다. |
| Rehearsal restore | 완료 (final parity 용도 아님) | source dump를 **별도 `monitube_rehearsal_20260820_045753` database**에 restore하고 `ANALYZE`까지 완료했다. 핵심 source snapshot count, migration 25, extension, index validity 및 orphan check가 일치했다. 다만 source writer를 멈추지 않은 snapshot이므로 이후의 NLP/queue 변화는 정상이며, final parity로 사용하면 안 된다. production target `monitube`는 public table 0개로 보존한다. |
| Physical restore drill | 진행 중 (재실행 필요) | `central-pg-data` Barman backup을 독립 1-instance CNPG recovery Cluster로 복원해 base backup 및 지속 WAL replay를 확인했다. 첫 실행은 `recoveryTarget.targetTime` 없이 시작되어 active archive tail을 계속 따라가며 승격 가능한 종료 상태/RTO를 만들지 못했다. 두 번째 실행은 target값의 trailing `Z`를 CNPG가 PostgreSQL 17 설정에 그대로 넣어 기동 전에 거부됐다. 스크립트는 이제 PostgreSQL이 수용하는 `+00:00` UTC offset의 `RECOVERY_TARGET_TIME`을 필수로 요구한다. 각 실패한 임시 Cluster의 PV는 `Retain`으로 보존했다. backup 이후·drill 시작 전 target으로 bounded restore를 재실행하고 Ready·읽기 검증·실제 RTO를 기록한다. |
| Legacy-safe chart path | 완료 | 기존 Helm release가 보존한 `database.useCnpg` values와 새 chart의 `database.mode`가 충돌하던 upgrade 문제를 수정했다. 동일 values의 `helm upgrade --dry-run` 및 `kubectl diff`는 object 변경 0건이었고, chart `0.2.4`를 release revision 9로 반영한 뒤 모든 Pod restart 0·Ready를 확인했다. logical replication opt-in은 여전히 false다. |
| Writer fence 준비 | 완료 (미활성) | API의 `MONITUBE_MAINTENANCE_READ_ONLY=true`가 모든 mutation을 `503 maintenance_read_only`로 막는 code/chart switch를 추가했다. maintenance 중 authenticated GET의 session expiry/cookie refresh도 억제해 source write를 만들지 않는다. API unit test 26개와 Helm render를 통과했고, cutover용 이미지 `cnpg-fence-6eb3e42` (digest `sha256:8ff9114757710a679afa7d419f251aaae0120627c5a8b366b02163a05916a84c`)를 registry에 게시했다. 현재 release에는 아직 적용하지 않았다. |

공유 central Cluster에는 다른 service database가 있으므로, 모든 기존 소비자의 egress/ingress를
열거하기 전에는 partial NetworkPolicy를 적용하지 않는다. 하나의 정책으로 기존 central DB traffic을
의도치 않게 차단하는 것보다, 별도 NetworkPolicy 변경으로 검토·배포하는 것이 안전하다.

## 3. 불변식과 금지 사항

1. legacy PVC/PV, Compose 원본 경로, 중앙 target DB를 임의 삭제·재생성하지 않는다.
2. API와 세 worker가 source와 target에 동시에 write하지 않는다.
3. source가 계속 쓰이는 상태의 초기 dump/import는 rehearsal일 뿐 final 데이터가 아니다.
4. final sync와 parity gate 전에는 application DB endpoint를 바꾸지 않는다.
5. target 첫 write 이후에는 단순 Secret rollback으로 source에 복귀하지 않는다.
6. Pod Running만으로 성공 판정하지 않는다. schema, data, application, worker, backup/restore를
   각각 검증한다.
7. soak와 restore drill 종료 전에는 `legacyPostgres.enabled: false`, legacy Service/PVC/PV,
   Docker volume 정리를 실행하지 않는다.
8. 중앙 DB credential 값을 Git, Helm values, 로그, 터미널 출력에 남기지 않는다.

## 4. Phase 0 — 책임·연결·용량 계약 확정

### 4.1 운영 책임과 승인값

다음을 변경 전 기록하고 중앙 DB 운영자와 승인한다.

- change owner, DB owner, incident/rollback 의사결정자
- 허용 write freeze 시간과 전체 maintenance window
- RPO 0, 목표 RTO, abort 판단 시각
- central cluster/Monitube logical backup 보존 기간
- legacy PV 보존 기간과 최종 삭제 승인자
- node-loss 시 object storage에서 복원하는 예상 RTO

### 4.2 Target database와 role

- 기존 `monitube` DB/role의 생성 주체와 사용 이력을 확인한다.
- role에는 login은 허용하되 superuser, createdb, createrole, replication을 주지 않는다.
- owner role과 application role을 분리할지 결정한다. 분리할 경우 migration role과 app role의
  권한을 명시하고 app에는 런타임에 필요한 최소권한만 부여한다.
- `public` schema의 create 권한, default privilege, `search_path`, object owner를 확정한다.
- role connection limit 100을 유지할지 connection budget 결과로 결정한다.

### 4.3 Cross-namespace Secret과 endpoint

Kubernetes Secret은 namespace 간 직접 참조할 수 없다. 중앙 DB Secret을 그대로
`monitube-prod` Pod에서 참조하는 설계는 사용할 수 없다.

다음을 설계하고 rendered manifest로 검증한다.

- `monitube-prod`에 배치할 중앙 DB 전용 Secret의 이름, 생성 주체, rotation 절차
- Secret의 `uri` 또는 host/port/db/user/password key contract
- rollback용 legacy Secret과 target Secret을 별도 이름으로 유지하는 방식
- target endpoint 후보:
  - `central-pg-data-pooler-rw.database.svc.cluster.local:5432`
  - `central-pg-data-rw.database.svc.cluster.local:5432`
- PgBouncer `session` mode에서 Rust/sqlx pool, prepared statement, transaction/lock semantics
- TLS 사용 여부, CA 전달, `sslmode`/server name verification
- NetworkPolicy를 도입할 경우 `monitube-prod → database:5432`만 허용하는 정책

chart의 `database.mode=central`은 중앙 DB target contract만 참조한다. API/worker URI와
migration Job key를 같은 namespace-local Secret 계약으로 분리하며, 기존 `cnpg.enabled`는
중앙 연결 switch로 재사용하지 않는다.

### 4.4 Connection과 resource budget

아래 식으로 평시·배포·재연결 burst의 상한을 계산한다.

```text
(API replicas × API pool max)
+ (collection worker replicas × pool max)
+ (NLP worker replicas × pool max)
+ (analysis worker replicas × pool max)
+ migration/maintenance connections
+ reconnect/failover reserve
<= approved Monitube role limit and central reserve
```

- 현재 central `max_connections=300`, 관찰 시 약 112 sessions를 기준선으로 삼는다.
- PgBouncer 2 replicas × default pool 50은 role/DB별 server connection 소비와 함께 계산한다.
- rehearsal에서 dump/restore가 central CPU, I/O, WAL, replica lag에 미치는 영향을 측정한다.
- 승인된 connection 상한과 central 여유율을 넘으면 cutover하지 않는다.

완료 기준: 책임자, endpoint/Secret/TLS, role 권한, pool budget, RPO/RTO가 문서로 승인됨.

## 5. Phase 1 — Source 기준선과 독립 복구점

### 5.1 Source inventory

변경 직전 다시 수집할 항목:

- Git/Devtron/Helm revision과 application image tag
- PostgreSQL server version, encoding, locale, timezone, extension, parameters
- migration ledger와 repository migration 파일 수
- database/schema/table/index/constraint/sequence/enum/large-object 목록
- 전체 DB size와 주요 table/index size
- API/worker pool 설정, active sessions/transactions/locks
- queued/running/waiting_retry/waiting_quota job과 active lease/checkpoint
- 예약 수집, 외부 enqueue, CronJob 등 모든 write producer 목록

### 5.2 독립 logical backup

- PostgreSQL 17 client의 `pg_dump -Fc`로 source PostgreSQL 16을 dump하는 rehearsal를 한다.
- dump는 legacy PV/동일 node 밖의 승인된 object storage에 저장한다.
- size, SHA-256, `pg_restore --list`, 생성 시각, source migration version을 기록한다.
- source role/ACL을 target에 그대로 생성하지 않도록 owner/privilege mapping을 검토한다.
- dump 파일을 임시 DB에 실제 restore하고 schema/data 검증을 통과시킨다.

### 5.3 복구 경로 두 가지 검증

1. 중앙 CNPG physical backup을 별도 Cluster로 복구하는 전체 Cluster restore drill
2. Monitube logical dump만 빈 `monitube` DB로 복원하는 single-database restore drill

daily CNPG backup 성공만으로 Monitube DB 단독 복구가 증명됐다고 판단하지 않는다.

완료 기준: source 기준선과 두 restore 경로의 실행 시간·검증 결과가 기록됨.

## 6. Phase 2 — Import/restore rehearsal

기본 방식은 **계획된 write freeze + final logical dump/restore**로 한다. 첫 logical
restore rehearsal은 약 33분이 걸렸으므로, 승인된 write freeze가 이를 넘지 않는다면 논리
복제 기반의 별도 rehearsal을 먼저 통과해야 한다. 이 경로도 dual-write를 허용하지 않으며,
cutover 직전 writer fence와 final sequence sync는 필수다.

### 6.1 Logical restore 계약

rehearsal 전에 다음을 확정한다.

- dump/restore client version과 image digest
- target이 비어 있음을 확인하는 SQL과 재사용 승인
- extension 생성 주체와 순서
- `--no-owner`/`--no-privileges` 사용 여부와 object owner/grant 재부여 방식
- destructive `--clean`, `DROP DATABASE`, `DROP SCHEMA` 사용 금지
- restore parallelism과 central 부하 상한
- schema migration의 source-of-truth: dump에 포함된 schema와 ledger
- restore 중 application migration hook 비활성화
- restore 후 sequence 값, invalid index, constraint, extension, owner/grant, `ANALYZE`
- 실패한 target을 다시 비우는 절차와 별도 승인

### 6.2 논리 복제를 선택할 경우의 추가 gate

- 모든 replicated table의 primary key/replica identity
- publication/subscription, initial copy, replication slot과 WAL 보존량
- DDL, sequence, large object, extension이 자동 복제되지 않는 경계
- lag 0 판정, sequence final sync, failback/reverse replication
- 중앙 Cluster 전체에 미치는 WAL/slot/resource 영향

완료 기준: rehearsal 총시간이 maintenance window 안에 있고, target 검증과 cleanup을 반복 가능.

## 7. Phase 3 — Chart와 Devtron 전환 경로 준비

이 Phase는 별도 PR로 구현하며 아직 production writer를 바꾸지 않는다.

- 중앙 DB endpoint와 `monitube-prod` Secret을 독립 value로 모델링한다.
- 새 CNPG Cluster 생성(`cnpg.enabled`)과 기존 중앙 DB 사용을 분리한다.
- API와 worker 3종, migration Job이 동일한 target contract를 사용하게 한다.
- source/target switch가 Secret 값을 직접 수정하지 않고 immutable release values로 가능해야 한다.
- source 모드와 target 모드의 Helm rendering, Secret name/key, endpoint를 snapshot test한다.
- migration hook은 restore/cutover 순서와 충돌하지 않도록 명시적으로 enable할 때만 실행한다.
- rollback release package와 정확한 이전 values를 Devtron에서 미리 렌더링한다.
- 기존 `scripts/runbooks/cnpg-migration.md`를 중앙 DB용 runbook으로 교체한다.

완료 기준: source 모드 배포가 실제 무변경이고 target 모드는 중앙 DB만 참조함을 manifest로 증명.

## 8. Phase 4 — Production cutover 상태 머신

### 8.1 시작 전 go/no-go

다음 중 하나라도 실패하면 변경 창을 시작하지 않는다.

- source logical backup/checksum/restore drill 미완료
- central physical backup 또는 object store 상태 비정상
- central 3/3 Ready 아님, replica lag/connection/disk/WAL 경고 존재
- source/target revision 또는 Secret contract 불일치
- legacy PV/PVC `Retain` 미확인
- rollback package/values/담당자 미준비
- rehearsal 시간이 maintenance window를 초과

### 8.2 Writer quiesce

1. 예약 수집, 외부 enqueue, CronJob 등 신규 producer를 중단한다.
2. API write route를 maintenance/read-only mode로 전환하고 실제 write 거부를 확인한다.
3. collection, NLP, analysis worker의 신규 claim을 중단한다.
4. in-flight job은 승인된 checkpoint까지 drain하고 active lease/transaction/lock 0을 확인한다.
5. source 주요 table count와 WAL 위치가 승인된 안정 시간 동안 변하지 않는지 확인한다.
6. 이 시점의 source 기준값, 시각, WAL LSN을 cutover record에 남긴다.

quiesce timeout을 넘으면 worker/API를 source에 재개하고 cutover를 중단한다.

### 8.3 Final dump/restore와 parity gate

1. final logical dump를 생성하고 checksum을 기록한다.
2. 승인된 절차로 빈 central `monitube` DB에 restore한다.
3. owner/grant/sequence/index/constraint/extension을 검증하고 `ANALYZE`를 실행한다.
4. 아래 Phase 5 parity gate를 source와 target에 같은 snapshot 기준으로 실행한다.
5. 단 하나라도 불일치하면 application endpoint를 바꾸지 않고 source로 복귀한다.

### 8.4 Application cutover

1. API만 target Secret/endpoint로 배포한다.
2. `/health`, `/ready`, authenticated read와 제한된 write smoke를 실행한다.
3. 첫 target write의 시각, target transaction marker를 기록한다.
4. source DB를 강제 read-only 또는 application 연결 차단으로 fencing한다.
5. collection → NLP → analysis worker 순서로 하나씩 기동한다.
6. 각 worker의 claim→write→complete/retry 한 건을 확인한 뒤 다음 worker를 기동한다.
7. Web/public path, restart 0, error/lock/pool/replication 상태를 확인한다.

예상하지 못한 write, schema error, duplicate lease, pool exhaustion, central replica 이상이
발생하면 신규 worker를 즉시 중지하고 rollback 결정표를 적용한다.

완료 기준: target만 writer이며 API와 세 worker의 실제 product path가 정상.

## 9. Phase 5 — Parity와 운영 검증

### 9.1 Schema/ownership

- migration ledger와 파일 수
- schema/table/column/default/enum 목록
- index 수와 `indisvalid`/`indisready`
- PK/FK/unique/check constraint
- sequence current value와 대응 table 최대 ID
- extension/version, owner, grant/default privilege, `search_path`

### 9.2 Data parity

- 모든 핵심 table count
- sources/videos/comments의 min/max key와 시간 범위
- 11M+ comments는 PK 범위별 count와 stable column hash
- source-video/comment 연결 orphan, duplicate 및 null 불변식
- queue/job/lease/checkpoint/outbox 상태
- NLP corpus/daily term stats와 comment rollup aggregate
- 고정 fixture의 analysis 결과/hash

PII 또는 원문을 검증 로그에 출력하지 않는다. 전체 대형 table hash가 운영 부하를 만들지 않도록
PK 구간별 bounded query로 실행한다.

### 9.3 Application/worker

- `/health`, `/ready`, authenticated read/write smoke, Web proxy/public route
- collection/NLP/analysis claim→complete/retry 각 1건 이상
- idempotency와 중복 active lease 0
- Pod restart 0, migration current, tokenizer fixture 정상

### 9.4 중앙 DB 운영

- connection/pool waiting과 role limit
- query p95/p99, lock wait, deadlock, slow query
- CPU/memory/disk/WAL, replica lag, failover/reconnect
- physical backup 성공과 Monitube logical backup 성공
- restore drill의 RPO/RTO

각 항목의 허용값은 rehearsal 기준선과 중앙 운영 한도로 표에 기록한다. 기준 초과 시 자동으로
성공 처리하지 않는다.

## 10. Phase 6 — Soak와 종료

- 최소 한 번의 full collection/NLP/analysis cycle을 완료한다.
- 승인된 soak 기간 동안 product/API/worker/DB/backup 지표가 허용 범위 안인지 확인한다.
- legacy DB는 fenced/read-only 상태로 유지한다.
- central physical backup과 Monitube logical backup을 각각 하나 이상 추가 생성하고 검증한다.
- data owner와 중앙 DB 운영자의 완료 승인을 받는다.

승인 후에도 legacy 정리는 별도 변경으로 실행한다.

1. legacy Service/StatefulSet 중지
2. legacy `DATABASE_URL` 제거 또는 폐기
3. 합의된 보존 기간 후 PV/PVC와 Docker volume 삭제 승인 요청
4. 삭제 직전 target backup restore 재확인

## 11. Rollback 결정표

| 상태 | 허용되는 대응 |
| --- | --- |
| quiesce/final dump 전 | source writer를 다시 열고 worker/API 재개 |
| restore/parity 실패, endpoint 전환 전 | target 사용 금지, source 재개, 원인 수정 후 재시도 |
| target endpoint 전환 후 첫 target write 전 | target Pod 중지, source endpoint release로 rollback, source 재개 |
| 첫 target write 후 | 단순 source 복귀 금지. target을 canonical로 유지하고 forward fix/backup recovery 또는 승인된 reverse migration 수행 |
| central Cluster 장애 | worker/API write 중지, central restore/failover runbook 수행. source를 임의 writer로 열지 않음 |

첫 target write 시각/marker 이후에는 legacy가 더 최신이라는 가정을 하지 않는다.

## 12. 구현 PR 제안 순서

1. 현재 상태와 기존 runbook 폐기/중앙 DB target contract 문서화
2. 중앙 role/Secret/endpoint/TLS와 connection budget
3. baseline·bounded parity·backup/restore rehearsal 도구
4. maintenance mode, producer suspend, worker drain/source fencing
5. 중앙 DB connection switch와 rendered-manifest tests
6. Devtron cutover/rollback runbook과 release package
7. backup/observability/restore drill 자동화
8. soak 승인 후 legacy decommission

PR 1~7은 source DB 삭제나 `legacyPostgres.enabled: false`를 포함하지 않는다.

## 13. 다음 승인 요청 범위

이 계획 승인 후 첫 작업은 production cutover가 아니다. Phase 0~2까지만 수행한다.

- 기존 중앙 `monitube` DB/role provenance 및 재사용 승인
- endpoint/Secret/TLS/role/connection contract 확정
- source read-only inventory
- off-node logical backup
- isolated restore rehearsal와 예상 downtime 측정
- central backup의 전체 Cluster restore 경로 확인

이 결과와 실제 소요시간을 제출한 뒤 별도로 chart 구현과 cutover 승인을 받는다.
