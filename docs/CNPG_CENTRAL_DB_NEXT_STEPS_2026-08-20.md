# Monitube 중앙 CNPG 향후 진행 로드맵

작성일: 2026-08-20  
상태: **실행 전 계획**  
상위 설계: `CNPG_CENTRAL_DB_STABILITY_IMPROVEMENT_PLAN_2026-08-20.md`

## 1. 현재 출발점

2026-08-20 13:16 UTC 기준:

- `central-pg-data-5`가 RW Primary다.
- `central-pg-data`는 CR상 3/3 Healthy, instance 5·6·7은 모두 Running/restart 0이다.
- replica 6·7은 아직 archive WAL replay 중이며 Primary의 streaming connection은 0건이다.
- HA replication slot 2개는 inactive이고 Primary `pg_wal`은 약 125GiB다.
- node `DiskPressure=False`, nodefs free는 약 174GiB다.
- Monitube API는 maintenance read-only로 `/api/ready` 200이다.
- collection/NLP/analysis worker는 모두 0이다.

따라서 **읽기는 복구됐지만 write durability는 아직 복구되지 않았다.**

## 2. 권장 최종 방향

현재 single-node 운영을 당분간 유지한다면 다음 구성을 기본안으로 한다.

```text
central-pg-data: Primary 1 + Replica 1 = 총 2 instance
backup: off-node object storage
restore drill: production nodefs가 아닌 별도 storage/node
```

Replica 2개를 같은 node/root filesystem에 두는 현재 구성은 node HA를 제공하지 않으면서 data·WAL·replay
부하를 늘린다. 다만 incident 중에는 replica를 삭제하거나 instance 수를 변경하지 않는다.

장기적으로 실제 HA가 필요하면 총 3개 instance를 유지하되 3개 K3s node와 독립 disk에 하나씩 배치한다.

## 3. 단계별 실행 순서

### Phase 0 — 현재 복구 완료

현재 상태를 그대로 유지한다.

- [ ] API maintenance read-only 유지
- [ ] collection/NLP/analysis worker 0 유지
- [ ] physical backup·restore drill·failover test 중지
- [ ] replica 6·7 replay LSN과 timestamp를 2분 주기로 확인
- [ ] Primary `pg_stat_replication`, slot active, retained WAL 확인
- [ ] nodefs free와 DiskPressure 확인

다음 조건이 모두 충족돼야 Phase 1로 이동한다.

- [ ] streaming replica 2개
- [ ] replica별 replay lag 256MiB 이하가 15분 지속
- [ ] inactive HA slot 0
- [ ] Primary `pg_wal`이 정상 상한으로 감소
- [ ] nodefs free 20% 이상, DiskPressure=False 30분 지속
- [ ] DB Pod 3개, controller, Barman plugin restart 0

실패 조건:

- nodefs free가 다시 20% 아래로 하락
- Primary 변경 또는 replica recovery 오류
- WAL 증가 속도가 replay 속도보다 빠름

실패 시 write를 열지 않고 원인 진단으로 되돌아간다. slot/PVC 삭제나 강제 failover는 수행하지 않는다.

### Phase 1 — Read-only 데이터 gate

Replica가 streaming으로 전환된 뒤 다음을 읽기 전용으로 검증한다.

- [ ] Primary `pg_is_in_recovery()=false`
- [ ] current LSN이 incident 전 checkpoint `3F/F7298EF8` 이상
- [ ] migration ledger current
- [ ] invalid/unready index 0
- [ ] 기준선의 unvalidated constraint 수와 일치
- [ ] source-video/comment orphan 0
- [ ] bounded table count·min/max·aggregate hash 통과
- [ ] ungranted lock 0
- [ ] API `/health`, `/ready`, public route 200

결과는 기존 migration plan의 execution record에 시각·LSN·count와 함께 추가한다.

### Phase 2 — 복구점 확보

Phase 0·1 통과 후에만 정확히 하나의 physical backup을 만든다.

- [ ] running/started Backup CR 0 확인
- [ ] daily ScheduledBackup과 겹치지 않는 시간 선택
- [ ] Primary target 1건만 생성
- [ ] backup ID, start/stop time, completed phase 확인
- [ ] object storage inventory와 checksum 확인
- [ ] Monitube logical backup 1건 생성·TOC·checksum 확인

격리 restore는 현재 production nodefs에서 수행하지 않는다. 전용 storage 또는 별도 node가 준비된 뒤 수행한다.

- [ ] 별도 storage에 isolated Cluster 복원
- [ ] PostgreSQL 기동·schema·bounded data 검증
- [ ] RPO/RTO 기록
- [ ] 증적 보존 후 rehearsal workload 중지

### Phase 3 — Application 순차 복구

복구점과 데이터 gate가 모두 통과된 뒤 다음 순서를 고정한다.

1. collection worker 1개
2. NLP worker 1개
3. analysis worker 1개
4. API maintenance read-only 해제

각 단계에서 확인할 항목:

- [ ] Pod Ready, restart 0
- [ ] claim → write → complete/retry 실제 1건 이상
- [ ] duplicate active lease 0
- [ ] ungranted DB lock 0
- [ ] pool wait/error 0
- [ ] replica streaming 수와 lag 유지
- [ ] nodefs와 `pg_wal` 증가율 정상

다음 단계로 넘어가기 전에 최소 10분 안정 상태를 유지한다.

### Phase 4 — Single-node replica 축소

Application 복구 후 24시간 안정 상태와 새 backup/restore 증거가 있어야 수행한다.

권장 목표는 총 2 instance, 즉 Primary 1 + Replica 1이다.

- [ ] 제거 후보 replica의 LSN과 데이터가 다른 replica와 일치
- [ ] CNPG scale-down 시 PVC retention 동작을 dry-run·rehearsal로 확인
- [ ] `instances=3 → 2` 변경 전 rollback manifest 저장
- [ ] scale-down 후 제거된 instance PVC/PV는 즉시 삭제하지 않고 Retain
- [ ] 남은 replica streaming과 failover readiness 확인
- [ ] nodefs·WAL·connection·backup 정상 확인

Replica 축소는 storage 분리 또는 multi-node 도입 전의 single-node containment다. HA 개선으로 표현하지 않는다.

### Phase 5 — Restore rehearsal workload 분리

현재 `central-pg-data-restore-20260820-063400`은 production과 같은 nodefs를 사용한다.

- [ ] 기존 restore drill의 성공 증적과 RTO 보존
- [ ] 신규 restore drill용 별도 StorageClass 생성
- [ ] 별도 disk 또는 별도 node에서 동일 restore 재검증
- [ ] production nodefs의 rehearsal workload를 중지
- [ ] 기존 rehearsal PVC는 보존 정책에 따라 별도 cleanup change로 처리

### Phase 6 — DB storage 분리

새 `database-local-retain-v2`를 만든다. 기존 StorageClass는 변경하지 않는다.

- [ ] 전용 block device/filesystem 선정
- [ ] fsync/IOPS/latency benchmark
- [ ] LVM thin 또는 XFS quota로 PVC capacity enforcement
- [ ] data와 WAL volume 분리
- [ ] 새 storage에 recovery rehearsal
- [ ] source/target LSN·schema·data parity
- [ ] controlled maintenance cutover
- [ ] 기존 central PVC는 soak 종료까지 Retain

### Phase 7 — 운영 안전장치 구현

- [ ] `max_slot_wal_keep_size`를 peak WAL 기반으로 산정·rehearsal
- [ ] SQL streaming/lag를 포함한 durability probe 구현
- [ ] Backup CR 단일 실행 Lease와 preflight wrapper 구현
- [ ] 수동 Backup CR 직접 생성 권한 제한
- [ ] controller/Barman/PgBouncer 이미지를 내부 registry에 mirror
- [ ] kubelet soft/hard eviction과 minimum reclaim 재설계
- [ ] CNPG/control-plane PriorityClass와 ephemeral-storage request 설정
- [ ] PodMonitor를 실제 설치하거나 기존 monitoring에 SQL probe 연결

## 4. 변경 우선순위

| 우선순위 | 작업 | 이유 |
| --- | --- | --- |
| P0 | replica streaming·WAL 회수·read-only gate | 현재 write 재개 전 필수 |
| P0 | 새 physical/logical 복구점 | incident 이후 복구 기준 확보 |
| P1 | worker/API 순차 복구 | product path 정상화 |
| P1 | 총 instance 3→2 검토 | single-node replay·storage 부하 완화 |
| P1 | restore drill production nodefs 분리 | 검증 workload가 production 장애를 만들지 않게 함 |
| P1 | DB storage 전용 filesystem 이전 | DiskPressure 근본 제거 |
| P2 | WAL 상한·backup guard·monitoring | 재발 자동 차단 |
| P2 | 3-node HA | node 장애 보호가 필요할 때 최종 구조 |

## 5. 완료 판정

단기 incident 복구 완료:

- streaming replica 안정
- WAL 정상화
- integrity·backup gate 통과
- API와 세 worker 정상
- DiskPressure 0회로 24시간 유지

근본 개선 완료:

- DB filesystem과 K3s nodefs 분리
- single-node라면 총 2 instance로 운영 경계 명시, 또는 3-node로 실제 HA 구성
- slot WAL 상한과 durability probe 적용
- backup/restore guard와 별도 restore capacity 확보
- 7일 soak 동안 DiskPressure, replica loss, backup failure 0

## 6. 금지 사항

- Replica catch-up 중 slot/PVC/WAL 삭제
- `Healthy 3/3`만 보고 write 재개
- incident 중 replica 수 변경
- 기존 StorageClass 경로 in-place 변경
- backup 성공 전에 replica 축소
- 같은 node에 replica를 추가하고 node HA라고 주장
