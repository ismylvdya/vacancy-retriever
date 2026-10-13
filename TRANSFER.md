# TRANSFER — перенос на другой Mac

Целевая машина: MacBook (Apple Silicon), macOS 15+, есть Homebrew. Всё ставится в папку проекта, кроме трёх
инструментов Homebrew (uv, Claude Code CLI, при необходимости Python 3.12) — они пригодятся и для других задач.

⚠ **Папку `.venv` переносить нельзя** (в ней абсолютные пути машины сборки) — она создаётся заново скриптом.
Переносится только репозиторий (через `git clone`) и, при желании, `data/` (база) и `config/.env`.

## 1. Доступ к приватному репозиторию

Репозиторий: https://github.com/ismylvdya/vacancy-retriever (приватный).

```bash
brew install gh
```
```bash
gh auth login
```
(GitHub.com → HTTPS → «Login with a web browser» — откроется браузер, войти своим аккаунтом GitHub.)

## 2. Клонирование

```bash
cd ~ && gh repo clone ismylvdya/vacancy-retriever
```
```bash
cd ~/vacancy-retriever
```

## 3. Установка

```bash
scripts/setup.sh
```
Скрипт покажет план и спросит подтверждение. Он:
- через Homebrew поставит **только недостающее**: `uv`, `claude-code` (Claude Code CLI), `python@3.12`
  (если нет Python 3.12 — например, от python.org он подойдёт). Ничего не обновляет и не удаляет;
- создаст `.venv` из `uv.lock`, скопирует `config/.env.example` → `config/.env`, создаст `data/`, `logs/`;
- проверит вход в Claude Code.

Посмотреть план без изменений: `scripts/setup.sh --dry-run`.

## 4. Вход в Claude Code (обязательно)

```bash
claude
```
Войти аккаунтом Claude с подпиской (Pro подходит). После входа можно выйти из Claude Code (`/exit`).
Проверка:
```bash
claude auth status
```
Должно быть `"loggedIn": true`. Без этого оценка вакансий не работает (сбор будет работать, вакансии будут
ждать оценки).

## 5. Почта (необязательно)

Открыть `config/.env` и вписать логины и **пароли приложений** Gmail и Яндекса — как их получить, написано в
самом файле и в docs/HANDBOOK.md, раздел 7. Без них почта показывает «not configured» — это не ошибка.

## 6. Первый запуск

```bash
scripts/start.sh
```
Откроется http://127.0.0.1:18765/, в меню-баре появится 🦮. Нажать **Start run**.
Первый прогон собирает за 7 дней и идёт дольше обычного (15–30 минут: канал RVC Global — ~2000 постов).
По завершении придёт уведомление macOS. Следующие прогоны — раз в день, только новое.

Проверить источники без прогона: `.venv/bin/vacancy-retriever check-sources`.

## 7. Если что-то не так

| Проблема | Решение |
|---|---|
| `scripts/setup.sh: Permission denied` | `chmod +x scripts/*.sh` |
| «Нет Homebrew» | установить по https://brew.sh и повторить `scripts/setup.sh` |
| `uv sync` ругается на Python | убедиться, что есть Python 3.12: `python3.12 --version`; иначе `brew install python@3.12` |
| Красная плашка про LLM / «not logged in» | `claude` → войти; проверить `claude auth status` |
| Не открывается страница | `scripts/stop.sh && scripts/start.sh`; логи — `logs/server.out`, `logs/serve.log` |
| Источник с 403 / Cloudflare в Source health | сайт блокирует; см. docs/HANDBOOK.md, раздел 11 |

Перенос базы со старой машины (если нужен): скопировать `data/vacancy_retriever.sqlite` в `data/` новой
копии проекта при остановленном приложении.

## Удаление

`scripts/stop.sh`, затем удалить папку проекта (в Корзину). Инструменты Homebrew остаются
(`brew uninstall uv claude-code` — если они больше не нужны).
