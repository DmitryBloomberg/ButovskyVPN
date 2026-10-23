#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
if [[ -f run_all.pid ]]; then
  PID="$(cat run_all.pid)"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    echo "Остановка отправлена процессу $PID."
  else
    echo "Процесс уже остановлен."
  fi
  rm -f run_all.pid
else
  echo "Файл run_all.pid не найден."
fi
