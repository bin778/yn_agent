# Hermes EC2 SQLite 백업과 복구

운영 DB에는 승인, 감사 기록, 작업 대기열이 함께 들어갑니다. 백업은 매일과
배포 전에 생성하고, EC2 한 대를 잃어도 접근할 수 있도록 암호화된 별도 위치에
보관합니다. 실제 보존 기간과 복구 기준은 운영 담당자가 정합니다.

## 서버 경로와 전제

- 코드와 Python 가상환경은 각각 `/opt/hermes/app`, `/opt/hermes/venv`에 둡니다.
  전용 Linux 사용자와 그룹 이름은 모두 `hermes`로 맞춥니다.
- 앱 설정은 `/etc/hermes/hermes.env`에 root 소유 및 `0600`으로 둡니다.
  Slack 매핑은 `/etc/hermes/slack_mapping.yaml`에 root 소유, `hermes` 그룹,
  `0640`으로 둡니다. 서비스 계정이 해당 디렉터리에 접근할 수 있어야 합니다.
- DB는 암호화된 EBS에 마련한 `/var/lib/hermes/hermes.db`를 사용합니다.
  별도 EBS를 마운트한다면 서비스를 켜기 전에 마운트를 완료합니다.
- EC2에 `sqlite3` CLI를 준비합니다. 아래 명령은 Bash를 사용하는 관리자 셸에서
  실행합니다. DB 파일 소유자는 `hermes:hermes`입니다.
- [서비스 설정](systemd/hermes-slack.service)을 설치한 뒤 Linux에서
  `sudo systemd-analyze verify /etc/systemd/system/hermes-slack.service`로 확인합니다.
  현재 서비스 진입점은 Socket Mode 연결을 점검할 수 있습니다. 운영용 실제 승인
  카드는 백엔드 핸들러 연결이 필요합니다.

## 백업 생성 및 검사

SQLite의 `.backup` 명령은 운영 프로세스가 실행 중이어도 일관된 복사본을 만듭니다.
원본 연결은 WAL의 보조 파일을 열 수 있어야 합니다. 백업 복사본의 저널 모드는
`DELETE`로 전환해 단일 파일로 보관하고, 검사에 통과한 파일에만 최종 이름을 붙입니다.

```bash
sudo bash <<'BASH'
set -euo pipefail
umask 077

database=/var/lib/hermes/hermes.db
backup_dir=/var/backups/hermes
test -s "$database"
install -d -m 0700 "$backup_dir"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
partial=$(mktemp "$backup_dir/.hermes-$stamp.XXXXXX")
trap 'rm -f -- "$partial"' EXIT
sqlite3 -bail -cmd '.timeout 10000' "$database" ".backup '$partial'"
test "$(sqlite3 -bail "$partial" 'PRAGMA journal_mode=DELETE;')" = delete
test "$(sqlite3 -bail -readonly "$partial" 'PRAGMA integrity_check;')" = ok
test -z "$(sqlite3 -bail -readonly "$partial" 'PRAGMA foreign_key_check;')"

backup="$backup_dir/hermes-$stamp-${partial##*.}.db"
mv -- "$partial" "$backup"
printf '%s\n' "$backup"
BASH
```

확인한 백업 파일을 EC2 외부의 암호화된 저장소로 복사합니다. 예를 들어 승인된
비공개 S3 버킷을 사용할 수 있습니다. 로컬 백업 파일은 외부 복사가 성공한 뒤
보존 정책에 따라 정리합니다. Slack 토큰과 모델 자격 증명은 별도로 관리합니다.

## 복구

1. 먼저 아래 교체 절차를 스테이징 DB에서 연습합니다. 운영 복구 시에는 백업 이후
   추가된 승인이나 대기열 작업이 복원본에 빠질 수 있으므로, 백업 시각을 확인하고
   복구 후 누락된 작업을 확인할 담당자를 정합니다.
2. `sudo systemctl stop hermes-slack.service`로 봇을 중지합니다. 워커와 타이머를
   연결했다면 그 서비스들도 중지하고 DB에 쓰는 프로세스가 모두 종료됐는지 확인합니다.
3. 아래의 `backup` 값을 복원할 파일의 절대 경로로 바꿔 실행합니다. 교체 전 DB와
   저널 파일은 같은 디스크의 롤백 디렉터리에 보존합니다.

```bash
sudo bash <<'BASH'
set -euo pipefail
umask 077

backup=/var/backups/hermes/REPLACE_WITH_BACKUP.db
state_dir=/var/lib/hermes
database="$state_dir/hermes.db"
test -s "$backup"
test "$(sqlite3 -bail -readonly "$backup" 'PRAGMA integrity_check;')" = ok
test -z "$(sqlite3 -bail -readonly "$backup" 'PRAGMA foreign_key_check;')"

restored=$(mktemp "$state_dir/.hermes-restore.XXXXXX")
trap 'rm -f -- "$restored"' EXIT
sqlite3 -bail -readonly "$backup" ".backup '$restored'"
test "$(sqlite3 -bail -readonly "$restored" 'PRAGMA integrity_check;')" = ok

sqlite3 -bail -readonly -header "$restored" "
SELECT 'approval_decisions' AS record_type, COUNT(*) AS count FROM approval_decisions
UNION ALL SELECT 'audit_events', COUNT(*) FROM audit_events;
SELECT handoff_type, status, COUNT(*) AS count
FROM handoff_jobs GROUP BY handoff_type, status ORDER BY handoff_type, status;
"

chown hermes:hermes "$restored"
chmod 0600 "$restored"
rollback=$(mktemp -d "$state_dir/.hermes-rollback.XXXXXX")
printf 'Rollback directory: %s\n' "$rollback"
for suffix in '' -wal -shm -journal; do
  if [ -e "$database$suffix" ]; then
    mv -- "$database$suffix" "$rollback/"
  fi
done
mv -- "$restored" "$database"
BASH
```

4. 출력된 승인·감사 건수와 대기열 상태를 백업 시점의 기록과 대조합니다.
   `sudo systemctl start hermes-slack.service`로 봇을 올리고
   `sudo journalctl -u hermes-slack.service -n 50 --no-pager`로 연결을 확인합니다.
   백업 시점 이후의 승인과 실행 여부, 복원본의 `claimed`·`pending` 작업을 확인한
   뒤 워커를 재개합니다. 이미 완료된 외부 작업을 다시 실행하지 않도록 대조합니다.
5. 되돌려야 한다면 쓰기 서비스를 다시 중지하고, 복원 DB와 관련 저널 파일을 별도
   디렉터리로 옮긴 다음, 출력된 롤백 디렉터리의 기존 DB와 저널을 함께 되돌립니다.
   교체 명령이 중간에 실패했다면 원래 DB와 저널이 어느 디렉터리에 있는지 먼저 확인합니다.

[SQLite 온라인 백업](https://sqlite.org/backup.html) · [systemd 서비스 실행 설정](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
