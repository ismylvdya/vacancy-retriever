#!/bin/bash
# Установка Vacancy Retriever на новом Mac.
#
#   scripts/setup.sh              — показать план, спросить подтверждение, выполнить
#   scripts/setup.sh --dry-run    — только показать план, ничего не менять
#   scripts/setup.sh --yes        — не задавать вопросов
#   scripts/setup.sh --skip-brew  — ничего не ставить через Homebrew (только окружение проекта)
#
# Что делает:
#   • через Homebrew ставит ТОЛЬКО недостающее: uv, Claude Code CLI, python@3.12 (если нет Python 3.12);
#     ничего не обновляет и не удаляет;
#   • создаёт .venv проекта из uv.lock (кэш uv — внутри проекта, Python не скачивается);
#   • создаёт config/.env из config/.env.example (если .env ещё нет), папки data/ и logs/;
#   • проверяет, залогинен ли Claude Code CLI.
# Чего НЕ делает: sudo, правка ~/.zshrc и других rc-файлов, ~/Library, удаление файлов.
# Повторный запуск безопасен.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY_RUN=0
ASSUME_YES=0
SKIP_BREW=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --yes) ASSUME_YES=1 ;;
    --skip-brew) SKIP_BREW=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Неизвестный флаг: $arg (см. --help)" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
find_tool() {
  local name="$1"; shift
  if command -v "$name" >/dev/null 2>&1; then command -v "$name"; return 0; fi
  local candidate
  for candidate in "$@"; do
    [[ -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  return 1
}

# ---------- проверки ----------
if [[ "$(uname -s)" != "Darwin" ]]; then
  say "Этот скрипт рассчитан на macOS." >&2
  exit 1
fi
[[ "$(uname -m)" == "arm64" ]] || say "⚠ Процессор не Apple Silicon ($(uname -m)) — должно работать, но не проверялось."

BREW="$(find_tool brew /opt/homebrew/bin/brew /usr/local/bin/brew || true)"
UV="$(find_tool uv /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv" || true)"
PY312="$(find_tool python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 || true)"
CLAUDE="$(find_tool claude /opt/homebrew/bin/claude /usr/local/bin/claude "$HOME/.local/bin/claude" \
  "$HOME/.claude/local/claude" || true)"

NEED_BREW=()
[[ -z "$UV" ]] && NEED_BREW+=("uv")
[[ -z "$PY312" ]] && NEED_BREW+=("python@3.12")
[[ -z "$CLAUDE" ]] && NEED_BREW+=("--cask claude-code")

# ---------- план ----------
say "Vacancy Retriever — установка в $ROOT"
say ""
say "Найдено:"
say "  Homebrew:        ${BREW:-нет}"
say "  uv:              ${UV:-нет}"
say "  Python 3.12:     ${PY312:-нет}"
say "  Claude Code CLI: ${CLAUDE:-нет}"
say ""
say "План:"
step=1
if [[ ${#NEED_BREW[@]} -gt 0 ]]; then
  if [[ $SKIP_BREW -eq 1 ]]; then
    say "  $step. (пропущено, --skip-brew) установить через Homebrew: ${NEED_BREW[*]}"
  else
    say "  $step. brew install ${NEED_BREW[*]}"
  fi
  step=$((step + 1))
fi
say "  $step. создать/обновить .venv из uv.lock (кэш: .cache/uv, без скачивания Python)"; step=$((step + 1))
if [[ ! -f "$ROOT/config/.env" ]]; then
  say "  $step. скопировать config/.env.example → config/.env"; step=$((step + 1))
fi
say "  $step. создать папки data/ и logs/"; step=$((step + 1))
say "  $step. проверить вход в Claude Code CLI"

if [[ ${#NEED_BREW[@]} -gt 0 && $SKIP_BREW -eq 0 && -z "$BREW" ]]; then
  say ""
  say "✗ Нет Homebrew. Установите его по инструкции с https://brew.sh (одна команда в Терминале),"
  say "  затем запустите этот скрипт снова. Сам скрипт Homebrew не ставит."
  exit 1
fi

if [[ $DRY_RUN -eq 1 ]]; then
  say ""
  say "--dry-run: ничего не изменено."
  exit 0
fi

if [[ $ASSUME_YES -eq 0 ]]; then
  say ""
  read -r -p "Выполнить? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" || "$answer" == "д" ]] || { say "Отменено."; exit 0; }
fi

# ---------- выполнение ----------
if [[ ${#NEED_BREW[@]} -gt 0 && $SKIP_BREW -eq 0 ]]; then
  [[ -z "$UV" ]] && "$BREW" install uv
  [[ -z "$PY312" ]] && "$BREW" install python@3.12
  [[ -z "$CLAUDE" ]] && "$BREW" install --cask claude-code
  UV="$(find_tool uv /opt/homebrew/bin/uv /usr/local/bin/uv || true)"
  PY312="$(find_tool python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12 || true)"
  CLAUDE="$(find_tool claude /opt/homebrew/bin/claude /usr/local/bin/claude "$HOME/.local/bin/claude" || true)"
fi

if [[ -z "$UV" || -z "$PY312" ]]; then
  say "✗ Нужны uv и Python 3.12. Установите их (или запустите без --skip-brew) и повторите." >&2
  exit 1
fi

say "→ Окружение Python (.venv)…"
cd "$ROOT"
UV_CACHE_DIR="$ROOT/.cache/uv" UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT="$ROOT/.venv" \
  "$UV" sync --frozen --python "$PY312"

if [[ ! -f "$ROOT/config/.env" ]]; then
  cp "$ROOT/config/.env.example" "$ROOT/config/.env"
  chmod 600 "$ROOT/config/.env"
  say "→ Создан config/.env — впишите туда пароли приложений для почты (необязательно)."
fi
mkdir -p "$ROOT/data" "$ROOT/logs"

say "→ Проверка конфигов…"
"$ROOT/.venv/bin/python" -c "from vacancy_retriever.config import AppConfig; AppConfig().validate_all(); print('  конфиги в порядке')"

say "→ Claude Code CLI…"
if [[ -z "$CLAUDE" ]]; then
  say "  ⚠ claude не найден. Установите: brew install --cask claude-code — затем войдите: claude"
elif "$CLAUDE" auth status 2>/dev/null | grep -q '"loggedIn": *true'; then
  say "  ✓ вход выполнен ($CLAUDE)"
else
  say "  ⚠ Claude Code не залогинен. Выполните в Терминале:  claude"
  say "    и войдите своим аккаунтом Claude (подписка Pro). Без этого оценка вакансий не работает."
fi

say ""
say "Готово. Запуск: scripts/start.sh   ·   Остановка: scripts/stop.sh   ·   Справка: docs/HANDBOOK.md"
