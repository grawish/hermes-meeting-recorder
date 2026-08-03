#!/usr/bin/env bash
set -uo pipefail

ROOT=${MEETING_RECORDER_ROOT:-/var/lib/hermes/meeting-recorder}
LOG_DIR="$ROOT/logs"
LOG_FILE="$LOG_DIR/scheduler.log"
LOCK_FILE="$ROOT/runtime/scheduler.lock"
mkdir -p "$LOG_DIR" "$ROOT/runtime"

# Prevent overlapping ticks while a meeting is being recorded.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

set -a
# shellcheck disable=SC1091
[ ! -f "$ROOT/.env" ] || . "$ROOT/.env"
set +a

if ! "$ROOT/meeting-recorder" scheduler --once >>"$LOG_FILE" 2>&1; then
  printf 'Meeting Calendar scheduler tick failed. Recent log:\n'
  tail -n 30 "$LOG_FILE"
  exit 1
fi

# Success stays silent; cron only alerts on actionable failures.
exit 0
