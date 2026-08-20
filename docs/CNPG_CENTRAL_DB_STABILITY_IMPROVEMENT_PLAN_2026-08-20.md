# Monitube 중앙 CNPG 근본 안정화 계획

작성일: 2026-08-20  
상태: **계획 수립 완료 — 구현 전**  
관련 문서: `CNPG_CENTRAL_DB_MIGRATION_PLAN_2026-08-20.md`

## 1. 목표

이번 개선의 목표는 단순히 `central-pg-data`를 다시 기동하는 것이 아니다.

1. PostgreSQL 데이터·WAL 증가가 K3s nodefs와 전체 workload를 동시에 중단시키지 않게 한다.
2. `3/3 Healthy`가 실제 streaming replica 2개와 bounded lag를 의미하게 한다.
3. replica 지연이나 inactive slot이 Primary의 WAL을 무제한 보존하지 못하게 한다.
4. backup·restore가 장애를 확대하지 않고, 독립 용량에서 반복 검증되게 한다.
5. 단일 노드 구성을 HA로 오인하지 않고 실제 node failure 경계를 명시한다.
6. Monitube API와 worker가 DB 내구성 gate를 통과한 뒤에만 write를 재개하게 한다.

## 2. 확인된 구조적 원인

### 2.1 Storage failure domain 혼재

- K3s node는 `mobichat-k3s-1` 한 대뿐이다.
- `central-pg-data` 3개 instance도 모두 같은 node에 배치된다.
- `database-local-retain`은 `/var/lib/rancher/k3s/database`를 사용하며 node root filesystem에 있다.
- 100Gi PVC 3개와 restore drill 40Gi PVC가 같은 root filesystem을 공유한다.
- local-path directory는 PVC 표기 용량만으로 host filesystem quota가 보장되지 않는다.
- 따라서 DB data, WAL, container runtime, kubelet state가 같은 filesystem 고갈에 함께 노출된다.

### 2.2 Replica 지연과 WAL retention

- CNPG status는 `3/3 Healthy`였지만 Primary의 `pg_stat_replication`은 0건이었다.
- replica 6·7은 streaming이 아니라 오래된 archive WAL을 replay하고 있었다.
- HA replication slot은 inactive 상태였고 Primary `pg_wal`은 약 125GiB까지 증가했다.
- Cluster CR의 `Healthy`만으로 replica freshness와 장애조치 가능성을 판정한 것이 핵심 오판이었다.

### 2.3 Kubelet eviction 정책

현재 kubelet 설정은 다음과 같다.

```text
evictionHard: nodefs.available=5%, imagefs.available=5%
evictionMinimumReclaim: nodefs.available=10%, imagefs.available=10%
evictionPressureTransitionPeriod: 5m
```

915GiB nodefs에서 pressure는 약 46GiB에 시작하지만 해제를 위해 약 137GiB까지 회수해야 한다.
이 넓은 hysteresis와 늦은 hard threshold 때문에 대량 eviction 뒤에도 scheduling 차단이 오래 지속됐다.

### 2.4 Control plane·image 공급망 동시 장애

- CNPG controller와 Barman plugin도 같은 node에서 eviction됐다.
- image GC 뒤 GHCR pull이 동시에 발생해 `pull QPS exceeded`, `ErrImagePull`이 추가 지연을 만들었다.
- `monitoring.enablePodMonitor=true`이지만 실제 Cluster에는 PodMonitor CRD가 없어 해당 monitoring 계약이 작동하지 않는다.

### 2.5 Backup 운영 guard 부재

- 수동 Backup CR을 만들기 전 SQL streaming, slot lag, WAL size, nodefs 여유, plugin readiness를 강제하는 gate가 없다.
- failed Backup CR과 daily ScheduledBackup은 존재하지만, manual backup serialization과 incident freeze가 시스템적으로 강제되지 않는다.
- isolated restore Cluster가 production과 같은 nodefs를 사용해 restore 검증 자체가 production 용량을 소비한다.

## 3. 현재 incident 종료 gate

아래가 모두 충족되기 전에는 API write와 worker를 재개하지 않는다.

| 영역 | 필수 조건 |
| --- | --- |
| Node | `DiskPressure=False`가 30분 지속되고 nodefs free가 20% 이상 |
| Controller | CNPG controller와 Barman plugin Ready, restart 0 |
| Primary | currentPrimary와 targetPrimary가 동일하고 `pg_is_in_recovery()=false` |
| Replica | 2개 모두 `pg_is_in_recovery()=true`, Primary `pg_stat_replication`에 `streaming` 2건 |
| Lag | 두 replica replay lag가 각각 256MiB 이하로 15분 지속 |
| WAL | inactive HA slot 0, `pg_wal`이 산정된 정상 상한 아래로 감소 |
| Integrity | migration current, invalid index 0, bounded count/orphan/constraint gate 통과 |
| Application | API read-only readiness 200, worker 0 유지 |

Cluster CR의 `phase=Healthy`, Pod `Running`, endpoint 존재만으로 통과시키지 않는다.

## 4. 목표 아키텍처

### 4.1 P0 — 즉시 containment

1. Monitube API maintenance read-only와 worker 0을 유지한다.
2. physical backup, restore drill, failover test를 모두 중단한다.
3. Primary·replica LSN, inactive slot, `pg_wal`, nodefs를 2분 주기로 수집한다.
4. 기존 PVC, legacy DB, failed Backup CR은 incident evidence로 보존한다.
5. replica streaming과 WAL 회수가 확인된 뒤 read-only integrity gate만 수행한다.

완료 기준: 위의 incident 종료 gate 전부 통과.

### 4.2 P1 — DB storage를 nodefs에서 분리

권장안은 CNPG 전용 block device 또는 전용 filesystem을 구성하는 것이다.

- 새 StorageClass `database-local-retain-v2`를 만들고 기존 StorageClass를 in-place 수정하지 않는다.
- data/WAL을 container runtime의 root filesystem과 다른 mount에 둔다.
- 단순 directory가 아니라 LVM thin volume 또는 XFS project quota 등 실제 capacity enforcement를 사용한다.
- `/data` 사용은 별도 disk임을 확인했으나, production DB에 사용하기 전 IOPS, fsync latency,
  failure domain, backup 동시 부하를 측정한다.
- WAL volume을 data volume과 분리하고 각각 alert·capacity budget을 둔다.
- restore rehearsal은 production nodefs가 아닌 별도 storage class 또는 별도 node에서 수행한다.

기존 PVC를 이동하지 않는다. 새 Cluster/PVC에 backup recovery 또는 controlled replication으로
복구한 뒤 parity·cutover한다. 원본 PVC는 soak 종료까지 Retain한다.

완료 기준:

- DB volume 증가가 nodefs `df`에 영향을 주지 않음
- PVC별 실제 quota 초과가 다른 PVC와 kubelet을 고갈시키지 않음
- fio/pg_test_fsync와 restore 부하에서 승인된 latency 충족

### 4.3 P2 — 실제 node HA

최종 권장안은 최소 3개 K3s node와 node별 독립 DB disk다.

- CNPG `instances=3`
- `required` pod anti-affinity와 topology spread
- instance별 서로 다른 node·disk
- DB 전용 taint/toleration과 PriorityClass
- control plane, CNPG controller, Barman plugin도 장애 node 하나에 함께 사라지지 않게 배치
- off-node object storage backup을 node-loss 복구 기준으로 유지

단일 node를 유지해야 한다면 문서상 `node HA 없음`으로 명시한다. 이 경우 replica 3개는 process/PVC
복구 편의일 뿐 node 장애 보호가 아니며, off-node backup RTO를 실제 가용성 계약으로 사용한다.

### 4.4 P3 — WAL·replication slot 상한

1. 운영 Healthy gate를 아래 SQL 증거로 교체한다.
   - Primary `pg_is_in_recovery()=false`
   - `pg_stat_replication.state='streaming'` 2건
   - replay LSN lag와 replay timestamp 상한
   - HA slot active 여부와 retained bytes
2. 아래 식으로 `max_slot_wal_keep_size`를 산정한다.

```text
peak WAL bytes/min × detection minutes
+ replica reconnect/rebuild safety window
+ 30% margin
```

3. 상한 초과로 slot이 invalidated되면 Primary 디스크를 희생하지 않고 replica를 새 base backup에서
   재생성하는 절차를 rehearsal한다.
4. `wal_keep_size`, archive timeout, slot retention은 서로 다른 목적임을 runbook에 명시한다.
5. replica가 archive recovery에 머무는 동안 `Healthy`를 반환하지 않도록 별도 synthetic condition과
   alert를 만든다.

완료 기준: replica 단절 rehearsal에서 nodefs가 설정된 상한 이상 증가하지 않고 Primary가 유지됨.

### 4.5 P4 — Kubelet eviction·resource 정책

DB storage 분리 뒤 kubelet 정책을 변경한다. storage 분리 전에 threshold만 낮추는 것은 금지한다.

권장 시작값:

```text
soft: nodefs.available=20%, imagefs.available=20% (5m grace)
hard: nodefs.available=15%, imagefs.available=15%
minimum reclaim: 2~3%
pressure transition: 2m
```

실제 값은 nodefs 정상 사용량과 image pull burst 측정 후 확정한다.

- system-reserved와 kube-reserved를 명시한다.
- 모든 workload에 ephemeral-storage request/limit을 추가한다.
- CNPG controller/plugin/instance에 검증된 PriorityClass를 적용한다.
- eviction alert를 soft threshold 이전에 발생시킨다.
- PDB가 kubelet hard eviction을 막지 못한다는 점을 runbook에 명시한다.

완료 기준: controlled disk-fill test에서 low-priority workload만 먼저 제한되고 DB/control plane은 유지됨.

### 4.6 P5 — image 공급망 복원력

- CNPG controller, PostgreSQL, Barman plugin/sidecar, PgBouncer 이미지를 내부 registry에 digest로 mirror한다.
- 모든 manifest는 digest 또는 승인된 immutable tag를 사용한다.
- node boot/upgrade 전에 필수 이미지를 pre-pull한다.
- registry 자체가 같은 node 하나에만 있으면 cold-start 경계를 문서화하고 registry backup/replica를 둔다.
- image pull QPS/backoff를 alert하고 incident 중 동시 pull storm을 제한한다.

완료 기준: 외부 GHCR 차단 상태에서 빈 node가 CNPG stack을 기동할 수 있음.

### 4.7 P6 — backup·restore 안전장치

Backup CR 직접 생성 권한을 제한하고 승인된 wrapper만 사용한다. wrapper는 Kubernetes Lease로 한 번에
하나만 실행하며 다음 preflight를 강제한다.

- Cluster CR Healthy
- streaming replica 2개와 lag 상한 통과
- inactive HA slot 0
- nodefs·DB filesystem free 25% 이상
- controller/plugin Ready, restart 0
- running/started Backup 0
- incident/maintenance backup freeze가 꺼져 있음

추가 개선:

- daily ScheduledBackup과 수동 backup의 동시 실행 차단
- standby backup은 실제 streaming standby가 있을 때만 선택
- restore drill은 별도 node/storage에서 실행
- backup 성공은 `phase=completed`, backup ID, start/stop time, object checksum과 restore 성공으로 판정
- failed Backup CR은 감사 보존하되 운영 목록과 alert에서 terminal 상태로 분리

완료 기준: backup concurrency test와 isolated restore/RTO 측정 통과.

### 4.8 P7 — Application fail-closed gate

- API maintenance switch와 worker replica를 하나의 release state로 관리한다.
- DB write enable 조건에 SQL-level durability gate 결과를 포함한다.
- 복구 순서는 API read-only → collection → NLP → analysis → API write 순으로 고정한다.
- 각 worker의 claim→write→complete/retry 증거가 다음 단계의 조건이다.
- `/ready`에는 `databasePrimary`, `streamingReplicas`, `maxReplayLagBytes`, `maintenance.readOnly`를
  별도 필드로 노출한다.
- DB가 읽기 가능하지만 durability가 부족한 경우 read route는 유지하고 mutation/worker만 차단한다.

완료 기준: replica 단절·Primary restart·pool reconnect fault injection에서 중복 claim과 dual-write 0.

### 4.9 P8 — Monitoring과 운영 자동화

현재 PodMonitor CRD가 없으므로 다음 중 하나를 명시적으로 선택한다.

1. Prometheus Operator/PodMonitor를 설치하고 CNPG 기본 query를 실제 scrape한다.
2. 기존 monitoring stack에 exporter/synthetic SQL probe를 직접 연결한다.

필수 alert:

- nodefs 25/20/15% 단계별 경고
- `pg_wal` bytes와 증가 속도
- inactive slot retained bytes
- streaming replica 수가 2 미만
- replay lag bytes/time
- archive failure와 last successful archive age
- controller/plugin unavailable
- backup running time·실패·최근 성공 age
- API maintenance와 worker replica가 기대 release state와 불일치

자동화는 관찰·fence까지만 수행한다. backup 생성, slot/PVC 삭제, failover, K3s restart는 동일 monitor가
자동 실행하지 않으며 별도 runbook과 preflight gate를 사용한다.

## 5. 실행 순서

| 순서 | 작업 | production write | rollback 자산 |
| --- | --- | --- | --- |
| 0 | 현재 incident 종료 gate 통과 | 차단 | Primary 5 PVC, legacy DB, logical backup |
| 1 | SQL durability probe·alert·backup lock 구현 | 제한적으로 허용 | 기존 chart/release |
| 2 | image mirror·pre-pull | 허용 | 기존 immutable image |
| 3 | 전용 DB storage와 새 StorageClass rehearsal | 허용 | 기존 PVC 전부 Retain |
| 4 | 새 storage/Cluster로 controlled migration | maintenance | 기존 central PVC·object backup |
| 5 | kubelet eviction 정책 변경과 disk-fill test | maintenance | 이전 K3s config |
| 6 | multi-node 배치 또는 single-node 한계 공식화 | maintenance | 기존 Cluster/PVC |
| 7 | backup/restore drill과 7일 soak | 허용 | legacy·이전 central PVC 보존 |

## 6. 최종 완료 기준

다음을 모두 만족해야 이 안정화 작업을 완료로 선언한다.

- 7일 동안 DiskPressure 0회
- DB filesystem과 nodefs가 물리적으로 분리됨
- streaming replica 2개, restart 0, lag SLO 준수
- inactive slot retained WAL이 설정 상한을 넘지 않음
- 외부 registry 장애 상태의 cold-start 성공
- physical backup 1회와 별도 storage restore drill 성공
- Monitube logical backup과 bounded parity 성공
- collection/NLP/analysis full cycle 완료
- API authenticated read/write와 public route 정상
- legacy와 기존 central PVC는 합의된 보존 기간까지 삭제하지 않음

## 7. 명시적 비목표

- 현재 incident 중 PVC, slot, WAL, failed Backup CR을 삭제해 공간을 만드는 것
- 같은 single node에 replica 수만 늘려 node HA라고 주장하는 것
- Cluster CR `Healthy`만으로 write를 여는 것
- restore가 검증되지 않은 backup을 복구점으로 인정하는 것
- 기존 StorageClass의 경로를 in-place 변경해 기존 PV 의미를 바꾸는 것
