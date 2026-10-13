# CHANGES_ON_HOST — что сборка изменила на машине сборки вне папки проекта

Машина сборки: MacBook Pro M4, 2026-10-13.

## Homebrew — ничего не установлено

Всё нужное уже было:

| Инструмент | Путь | Версия | Откуда |
|---|---|---|---|
| uv | `/opt/homebrew/bin/uv` | 0.12.23 | был до сборки |
| Python 3.12 | `/opt/homebrew/bin/python3.12` | 3.12.15 | был до сборки |
| Claude Code CLI | `/opt/homebrew/bin/claude` → cask `claude-code@latest` 2.1.293 | | был до сборки (8 октября) |

`brew install` / `upgrade` / `uninstall` не выполнялись. `scripts/setup.sh` на этой машине запускался только с
`--dry-run` и `--skip-brew --yes` (на временном клоне внутри проекта, клон потом отправлен в Корзину).

## Файлы вне папки проекта

| Что | Где | Почему | Как откатить |
|---|---|---|---|
| Транскрипты LLM-вызовов приложения (~4–6 МБ) | `~/.claude/projects/-Users-mdmkudryashov-projects-vacancy-retriever--cache-llm-cwd/` | До исправления каждый вызов Claude через SDK сохранял сессию. Исправлено флагом `--no-session-persistence` (коммит «Claude SDK: --no-session-persistence…»); папка пополнялась только во время отладки и первого полного прогона | `trash ~/.claude/projects/-Users-mdmkudryashov-projects-vacancy-retriever--cache-llm-cwd` |
| История этой сессии Claude Code и её субагентов | `~/.claude/projects/-Users-mdmkudryashov-projects/` | неизбежно (так работает Claude Code) | не требуется |
| Временные скрипты проверки источников | `/private/tmp/claude-501/…/scratchpad/` | смоук-тесты сайтов субагентами | очищается системой |
| Уведомление macOS «Run finished…» | Центр уведомлений; при первом показе macOS может добавить «Script Editor» в Настройки → Уведомления | приложение шлёт уведомление через `osascript` | Системные настройки → Уведомления |

## Не менялось

- `~/.zshrc`, `~/.bashrc` и другие rc-файлы, `~/Library` (включая LaunchAgents), системные настройки.
- Глобальный git-конфиг (коммиты сделаны существующей идентичностью; локальный конфиг репозитория не менялся).
- Глобальные Python-пакеты, `npm -g`, `pipx` — не использовались. Кэш uv — `./.cache/uv` внутри проекта;
  `~/.cache/uv` и `~/.local/share/uv` сборкой не трогались (даты изменения — до 13.10).
- Память и глобальные настройки Claude Code (`~/.claude/CLAUDE.md`, `settings.json`) — не менялись.

## Процессы

На момент завершения сборки могут работать сервер и иконка меню-бара, запущенные `scripts/start.sh`
(PID в `data/server.pid`, `data/menubar.pid`). Остановка: `scripts/stop.sh`.
