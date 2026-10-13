#!/bin/bash
# Запуск Vacancy Retriever: веб-сервер (127.0.0.1) + иконка в меню-баре.
# Повторный запуск безопасен: вторую копию не запускает.
#   scripts/start.sh            — запустить и открыть ленту в браузере
#   scripts/start.sh --no-open  — запустить без открытия браузера
# Порт можно поменять: VR_PORT=8800 scripts/start.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${VR_PORT:-8765}"
BIN="$ROOT/.venv/bin/vacancy-retriever"
SERVER_PID="$ROOT/data/server.pid"
MENUBAR_PID="$ROOT/data/menubar.pid"
OPEN_BROWSER=1
[[ "${1:-}" == "--no-open" ]] && OPEN_BROWSER=0

alive() { [[ -s "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

if [[ ! -x "$BIN" ]]; then
  echo "Не найдено окружение .venv. Сначала выполните: scripts/setup.sh" >&2
  exit 1
fi
mkdir -p "$ROOT/data" "$ROOT/logs"

if alive "$SERVER_PID"; then
  echo "Сервер уже запущен (PID $(cat "$SERVER_PID")): http://127.0.0.1:$PORT/"
else
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Порт $PORT занят другой программой. Запустите с другим портом: VR_PORT=8800 scripts/start.sh" >&2
    exit 1
  fi
  cd "$ROOT"
  nohup "$BIN" serve --port "$PORT" >>"$ROOT/logs/server.out" 2>&1 &
  echo $! >"$SERVER_PID"
  for _ in $(seq 1 40); do
    curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1 && break
    sleep 0.25
  done
  if ! curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
    echo "Сервер не поднялся. Смотрите logs/server.out и logs/serve.log" >&2
    exit 1
  fi
  echo "Сервер запущен: http://127.0.0.1:$PORT/"
fi

if alive "$MENUBAR_PID"; then
  echo "Иконка в меню-баре уже запущена (PID $(cat "$MENUBAR_PID"))"
else
  cd "$ROOT"
  nohup "$BIN" menubar --port "$PORT" >>"$ROOT/logs/menubar.out" 2>&1 &
  echo $! >"$MENUBAR_PID"
  echo "Иконка в меню-баре запущена"
fi

[[ $OPEN_BROWSER -eq 1 ]] && open "http://127.0.0.1:$PORT/"
exit 0
