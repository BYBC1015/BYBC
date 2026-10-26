#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

data_dir="${CODEX_HUB_DATA_DIR:-/var/lib/codex-hub}"
app_dir="${CODEX_HUB_APP_DIR:-/opt/codex-hub}"
backup_root="${1:-/root/codex-hub-portable-backups}"
include_runtime="${CODEX_HUB_BACKUP_INCLUDE_RUNTIME:-0}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
kit_name="${stamp}-codex-hub-portable"

[[ "$data_dir" = /* && "$app_dir" = /* && "$backup_root" = /* ]]
[[ -d "$data_dir" && ! -L "$data_dir" ]]
[[ -f "$data_dir/codex_hub.db" && ! -L "$data_dir/codex_hub.db" ]]
[[ -d "$app_dir" && ! -L "$app_dir" ]]

mkdir -p -- "$backup_root"
[[ -d "$backup_root" && ! -L "$backup_root" ]]
chmod 700 -- "$backup_root"
backup_root="$(readlink -f -- "$backup_root")"
[[ -n "$backup_root" && "$backup_root" = /* ]]

kit="$backup_root/$kit_name"
[[ ! -e "$kit" ]]
mkdir -- "$kit"
chmod 700 -- "$kit"
printf 'Backup is incomplete until SHA256SUMS and COMPLETE exist.\n' > "$kit/INCOMPLETE"

migrate="$app_dir/.venv/bin/codex-hub-migrate"
if [[ ! -x "$migrate" ]]; then
  migrate="$app_dir/.venv/bin/python"
  [[ -x "$migrate" ]]
  migrate_module=("$migrate" -m codex_hub.migration)
else
  migrate_module=("$migrate")
fi

"${migrate_module[@]}" create \
  --data-dir "$data_dir" \
  --output "$kit/hub-data.zip" \
  > "$kit/create-result.json"
"${migrate_module[@]}" verify \
  --archive "$kit/hub-data.zip" \
  > "$kit/verify-result.json"

tar \
  --one-file-system \
  --exclude='codex-hub/.venv' \
  --exclude='codex-hub/.pytest_cache' \
  --exclude='codex-hub/.git' \
  --exclude='codex-hub/.codex-hub-state' \
  --exclude='codex-hub/**/__pycache__' \
  -czf "$kit/app-source.tar.gz" \
  -C "$(dirname -- "$app_dir")" "$(basename -- "$app_dir")"

if [[ "$include_runtime" == "1" ]]; then
  tar \
    --one-file-system \
    --exclude='codex-hub/.pytest_cache' \
    --exclude='codex-hub/.git' \
    --exclude='codex-hub/.codex-hub-state' \
    --exclude='codex-hub/**/__pycache__' \
    -czf "$kit/app-runtime-linux.tar.gz" \
    -C "$(dirname -- "$app_dir")" "$(basename -- "$app_dir")"
fi

if [[ -f /etc/codex-hub.env && ! -L /etc/codex-hub.env ]]; then
  cp --preserve=mode,timestamps -- /etc/codex-hub.env "$kit/codex-hub.env"
  chmod 600 -- "$kit/codex-hub.env"
fi
if [[ -f /etc/systemd/system/codex-hub.service && ! -L /etc/systemd/system/codex-hub.service ]]; then
  cp --preserve=mode,timestamps -- \
    /etc/systemd/system/codex-hub.service "$kit/codex-hub.service"
  chmod 600 -- "$kit/codex-hub.service"
fi

systemctl is-active codex-hub.service > "$kit/service-active.txt" || true
systemctl is-enabled codex-hub.service > "$kit/service-enabled.txt" || true
"$app_dir/.venv/bin/python" --version > "$kit/python-version.txt" 2>&1

printf '%s\n' \
  'This directory contains sensitive Codex Hub data, password/token hashes, files,' \
  'and possibly codex-hub.env. Keep it private and transfer it only over an' \
  'authenticated encrypted channel.' \
  '' \
  'Verify first:' \
  '  sha256sum -c SHA256SUMS' \
  '  /opt/codex-hub/.venv/bin/codex-hub-migrate verify --archive hub-data.zip' \
  '' \
  'Restore only into a new, absent data directory:' \
  '  /opt/codex-hub/.venv/bin/codex-hub-migrate restore \' \
  '    --archive hub-data.zip --target-data-dir /var/lib/codex-hub' \
  '' \
  'Read docs/codex-hub-server-migration-runbook.md before a production cutover.' \
  > "$kit/RESTORE-NOTES.txt"

rm -- "$kit/INCOMPLETE"
(
  cd -- "$kit"
  find . -maxdepth 1 -type f ! -name SHA256SUMS ! -name COMPLETE -print0 \
    | sort -z \
    | xargs -0 sha256sum > SHA256SUMS
)
printf 'created_at=%s\nformat=codex-hub-portable-kit-v1\n' "$stamp" > "$kit/COMPLETE"

printf 'kit=%s\n' "$kit"
printf 'data_archive_sha256=%s\n' "$(sha256sum "$kit/hub-data.zip" | cut -d' ' -f1)"
