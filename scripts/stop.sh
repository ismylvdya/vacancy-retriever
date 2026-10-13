#!/bin/bash
# Остановка Vacancy Retriever (сервер + иконка в меню-баре).
# Идущий прогон при остановке откатится при следующем запуске (ничего не сохранится).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

stop_one() {
  local name="$1" pidfile="$2"
  if [[ ! -s "$pidfile" ]]; then
    echo "$name: не запущен"
    return
  fi
  local pid
  pid="$(cat "$pidfile")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
    echo "$name: остановлен (PID $pid)"
  else
    echo "$name: не запущен"
  fi
  : >"$pidfile"
}

stop_one "Иконка в меню-баре" "$ROOT/data/menubar.pid"
stop_one "Сервер" "$ROOT/data/server.pid"
