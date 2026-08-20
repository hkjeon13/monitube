# Monitube 중앙 CNPG 이전 runbook

이 runbook은 `monitube-prod/monitube-postgres`에서
`database/central-pg-data`의 기존 `monitube` database로 이전하는 절차다.
별도 `monitube-db` CNPG Cluster를 생성하는 이전 runbook은 폐기했다.

## 범위와 안전 경계

- source: PostgreSQL 16 legacy StatefulSet `monitube-postgres`
- target: PostgreSQL 17 CNPG `central-pg-data`, database `monitube`
- 기본 chart mode: `database.mode=legacy`
- target mode: `database.mode=central`
- source와 target에 application writer를 동시에 실행하지 않는다.
- target 첫 write 이후에는 단순히 legacy `DATABASE_URL`로 되돌리지 않는다.
- legacy recovery PV, Docker volume, Service, StatefulSet은 soak와 restore drill이 끝날 때까지 삭제하지 않는다.

이 문서는 production cutover를 자동화하지 않는다. 각 write 단계에는 change owner와
중앙 DB 운영자의 승인이 필요하다.

## 1. Preflight

실행 전에 production을 변경하지 않고 현재 상태를 수집한다.

```sh
./scripts/cnpg_central_preflight.sh
```

다음이 모두 확인돼야 한다.

- source DB, legacy PV/PVC, API/worker가 정상
- `central-pg-data`가 Ready이고 backup/`ScheduledBackup`이 정상
- target `monitube` DB/role의 생성 이력과 재사용 승인이 있음
- target DB가 migration 대상 외 data를 포함하지 않음
- central connection budget, PgBouncer session mode, target role limit이 승인됨
- central application Secret의 key 이름만 확인됨. Secret 값은 출력·Git·values에 남기지 않음

`monitube-prod` workload는 `database` namespace의 Secret을 직접 읽을 수 없다. target용
`monitube-central-db` Secret은 `monitube-prod`에 별도 provision해야 하며 다음 key를 가진다.

```text
uri
host
port
dbname
username
password
```

`uri`는 API/worker가 쓰고 나머지 key는 migration Job이 쓴다. endpoint는
`central-pg-data-pooler-rw.database.svc.cluster.local:5432`와 direct RW endpoint를
rehearsal로 비교한 뒤 하나로 확정한다. TLS/CA/`sslmode`도 같은 Secret contract와
deployment values에 명시한다.

## 2. Backup 및 restore rehearsal

1. off-node object storage에 custom-format logical dump를 만들고 SHA-256과
   `pg_restore --list`를 기록한다.
2. source와 분리된 임시 database 또는 격리 Cluster에 실제 restore한다.
3. `--no-owner --no-privileges` 사용 여부, owner/grant 재설정, extension, sequence,
   index validity, constraint, `ANALYZE`를 검증한다.
4. central physical backup의 전체 Cluster restore와 Monitube logical dump의 단일 DB restore를
   각각 rehearsal한다.
5. restore 시간이 maintenance window 안인지 기록한다.

physical restore drill에는 반드시 bounded recovery target을 지정한다. source가 계속 WAL을
archive하는 동안 target 없이 복구하면 recovery가 끝나지 않아 RTO를 측정할 수 없다. 현재
CNPG `1.27.0` + PostgreSQL 17 조합은 `targetTime`을 PostgreSQL 설정의 trailing `Z` 값으로
정규화해 기동 전에 거부하므로, 이 drill은 확인한 completed base backup ID와 `targetImmediate`
로 일관 시점에서 승격한다. 이는 physical backup/RTO 검증용이며, timestamp PITR은 CNPG
업그레이드 후 별도 호환성 rehearsal을 통과하기 전에는 cutover 근거로 사용하지 않는다.

```sh
RECOVERY_BACKUP_ID=YYYYMMDDTHHMMSS \
  ./scripts/cnpg_central_physical_restore_drill.sh
```

Cluster가 `Cluster in healthy state`가 된 뒤에는 아래 verifier로 target, promotion,
database inventory와 관측 RTO를 기록한다. 이 명령은 읽기 전용이며 row/credential을 출력하지
않는다.

```sh
./scripts/cnpg_central_physical_restore_verify.sh \
  central-pg-data-restore-YYYYMMDD-HHMMSS
```

`--clean`, `DROP DATABASE`, `DROP SCHEMA`을 active source 또는 중앙 target에 임의로
사용하지 않는다. 빈 target을 다시 준비하는 작업은 별도 승인이다.

### 논리 복제 선택 시

첫 full logical restore rehearsal은 약 33분이 걸렸다. 승인된 write freeze가 이를 넘지
않으면 final dump/restore 대신 **별도 logical-replication rehearsal**을 통과해야 한다.

- source의 `wal_level`은 현재 `replica`이므로, `legacyPostgres.logicalReplication.enabled=true`
  chart release는 source PostgreSQL 재시작을 유발한다. 이는 rehearsal/cutover 변경창에서만
  적용하고, 평시 release에는 false를 유지한다.
- source에는 publication 전용의 최소권한 replication login을, target rehearsal DB에는 별도
  subscription을 사용한다. target이 source에 연결할 때는 legacy Service DNS
  `postgres.monitube-prod.svc.cluster.local`을 사용하며, StatefulSet Pod 이름을 hostname으로
  사용하지 않는다. production `monitube` DB에 rehearsal publication/subscription을 만들지 않는다.
- full table copy 중 target I/O 때문에 source feedback이 장시간 멈출 수 있으므로,
  logical replication opt-in에는 `wal_sender_timeout=4h`를 함께 적용한다. 이 값은
  rehearsal/cutover 창에만 유지하며 정상 legacy operation에는 적용하지 않는다.
- schema/DDL, extension, sequence는 logical replication으로 자동 동기화되지 않는다. schema를
  먼저 준비하고, final writer fence 후 sequence를 재동기화한다.
- initial copy와 lag 0, replication slot WAL retention, subscriber worker 여유를 기록한다.
  target에서 application writer를 열기 전 publication과 subscription을 제거하거나 전환
  절차에 맞게 종료한다.

`scripts/cnpg_central_logical_rehearsal.sh`는 이 격리 rehearsal만 자동화한다. 실행 전
`wal_level=logical`을 read-only로 재확인하고, production target이 비어 있지 않거나 target
rehearsal DB 이름이 안전 패턴에 맞지 않으면 실패한다. source writer/endpoint와 production
target에는 application write를 하지 않는다.

initial copy가 모두 Ready가 된 뒤에는 아래 verifier로 subscription 상태, source 주 슬롯 lag,
초기 table-sync 임시 슬롯의 완전 정리, 핵심 table의 통계 count, invalid index를 기록한다. source writer가 계속 실행되는 rehearsal에서는
source/target count가 즉시 동일하다고 가정하지 않고, lag와 동일 시점 bounded parity를 함께
판정한다.

```sh
./scripts/cnpg_central_logical_rehearsal_verify.sh \
  monitube_logical_rehearsal_YYYYMMDD_HHMMSS
```

결과와 lag/slot 증적을 기록한 뒤에만 아래 cleanup을 실행한다. 정확히 timestamp 형식의 격리
rehearsal 이름과 `--confirm` 없이는 동작하지 않으며, target subscription을 먼저 제거한 후
source publication·주/테이블동기화 잔여 slot·temporary role과 격리 database를 정리한다.

```sh
./scripts/cnpg_central_logical_rehearsal_cleanup.sh \
  monitube_logical_rehearsal_YYYYMMDD_HHMMSS --confirm
```

## 3. Chart 준비와 렌더링 검증

central mode는 새 CNPG Cluster를 만들지 않는다. `cnpg.enabled`는 false로 유지한다.

```sh
helm lint infra/k8s/monitube
helm template monitube infra/k8s/monitube --namespace monitube-prod > /tmp/monitube-legacy.yaml
helm template monitube infra/k8s/monitube --namespace monitube-prod \
  --set database.mode=central > /tmp/monitube-central.yaml
```

central rendering에서는 API와 worker가 `monitube-central-db/uri`를, migration Job은 같은
Secret의 host/port/dbname/username/password key를 참조해야 한다. legacy rendering은 현재
`monitube-runtime-env`의 DB URL을 그대로 사용해야 한다.

논리 복제 rehearsal rendering은 다음처럼 opt-in으로만 생성한다. 이 rendering 검증 자체는
production StatefulSet을 변경하지 않는다.

```sh
helm template monitube infra/k8s/monitube --namespace monitube-prod \
  --set legacyPostgres.logicalReplication.enabled=true
```

새 chart version은 Devtron repository에 게시하고 Refetch Charts로 fetch 가능함을 확인한다.
이 단계는 chart를 배포하거나 `database.mode`를 바꾸는 단계가 아니다.

## 4. Cutover go/no-go

다음 중 하나라도 실패하면 cutover하지 않는다.

- independent logical backup 및 restore rehearsal 미완료
- central backup/object store/cluster health 이상
- source/target revision 또는 Secret key contract 불일치
- central connection/pool/disk/WAL 여유 부족
- rollback release values와 담당자 미준비
- rehearsal 시간이 승인된 write freeze 시간을 초과

## 5. Production cutover

### Writer quiesce

1. 예약 수집, CronJob, 외부 enqueue를 중단하고 `maintenance.apiReadOnly=true`인
   chart release로 API의 mutation을 fence한다.
2. 실제 `POST`/`PUT`/`PATCH`/`DELETE`가 `503 maintenance_read_only`로 거부되고
   `/health`, `/ready`, authenticated GET은 유지되는지 확인한다. 이 상태의 GET은
   session expiry/cookie를 갱신하지 않아 source DB write를 만들지 않는다.
3. collection, NLP, analysis worker replica를 0으로 내려 신규 claim을 중단한다.
4. active lease, transaction, lock을 drain한다.
5. source count와 WAL 위치가 승인된 안정 시간 동안 변하지 않음을 기록한다.

timeout이면 source writer를 다시 열고 cutover를 중단한다.

### Final restore와 parity

1. final logical dump와 checksum을 만든다.
2. 승인된 빈 target에 restore한다.
3. source/target의 bounded baseline을 비교한다.

```sh
./scripts/cnpg_central_parity.sh
```

parity mismatch 또는 schema/ownership/index/sequence/constraint 불일치가 있으면 endpoint를
바꾸지 않고 source를 재개한다.

### Application 전환

1. API만 `database.mode=central`로 전환한다.
2. `/health`, `/ready`, authenticated read 및 제한된 write smoke를 수행한다.
3. 첫 target write의 시각과 marker를 기록한다.
4. source를 read-only 또는 application connection 차단으로 fence한다.
5. collection → NLP → analysis worker 순으로 하나씩 기동하고 claim→complete/retry를 확인한다.
6. Web/public route, restart, pool wait/error, lock, central replica 상태를 확인한다.

## 6. Rollback

| 상태 | 대응 |
| --- | --- |
| final dump 전 | source writer를 재개 |
| restore/parity 실패, endpoint 전환 전 | target을 사용하지 않고 source 재개 |
| target endpoint 전환 후 첫 target write 전 | target Pod를 중지하고 legacy release values로 rollback |
| 첫 target write 후 | source로 단순 복귀 금지. target을 canonical로 두고 forward fix, backup recovery, 승인된 reverse migration 중 하나를 선택 |

## 7. Soak 및 정리

- full collection/NLP/analysis cycle과 승인된 soak 기간을 통과한다.
- target의 physical backup과 Monitube logical backup을 새로 생성하고 restore 가능성을 확인한다.
- source는 fenced/read-only로 보존한다.
- data owner와 중앙 DB 운영자가 승인한 별도 change에서만 legacy resource를 정리한다.
