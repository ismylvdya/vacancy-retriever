# Как перевести приложение с Claude на OpenAI (ChatGPT)

Есть два пути, и они разные:

| | (А) API OpenAI | (Б) Подписка ChatGPT через Codex CLI |
|---|---|---|
| Как платится | по токенам, отдельно от подписки ChatGPT | входит в подписку ChatGPT (Plus / Pro / …) |
| Аналог у Claude | `anthropic_api` | `claude_sdk` (подписка через Claude Code CLI) |
| Готово в приложении | **да**, `provider: openai_api` | **нет**, нужен новый провайдер `codex_cli` (см. ниже) |

Факты о Codex CLI сверены по официальной документации 16.11.2026 (адреса в конце). Названия моделей и тарифы
у OpenAI меняются часто: перед переключением проверьте их ещё раз.

## (А) Платный API OpenAI: работает уже сейчас

1. https://platform.openai.com → **API keys** → Create new secret key.
2. **Billing** → способ оплаты и пополнение; **Limits** → месячный бюджет (например, $10), чтобы не было сюрпризов.
3. В `config/.env` впишите ключ сами: `OPENAI_API_KEY=sk-…` (в чат его не вставлять).
4. В `config/llm.yaml`:
   - `provider: openai_api`;
   - в `models.openai_api` проверьте модели. Сейчас там только `scoring` и `dedup` (`gpt-5.4-nano`). Если не указать
     `resume`, резюме и сопроводительные (кнопка ▶) будет писать та же маленькая модель: добавьте модель посильнее.
     Проверьте на https://platform.openai.com/docs/models, что выбранные модели ещё доступны в API.
5. Перезапуск: `scripts/stop.sh`, затем `scripts/start.sh`.
6. Проверка без риска для основной базы:
   ```bash
   .venv/bin/vacancy-retriever run --sources serbiawork --limit 3 --since 1d --db data/dev.sqlite
   ```
   Затем Statistics → Runs: токены и ошибки этого прогона.

Вернуться на Claude: `provider: claude_sdk` и перезапуск.

## (Б) Подписка ChatGPT через Codex CLI: что нужно сделать

### Шаг 1. Ваша часть (один раз)

1. Подписка: Codex входит в планы ChatGPT Free, Go, Plus, Pro, Business, Edu и Enterprise. Лимиты:
   - **Plus**: окно 5 часов (как у Claude Pro);
   - **Pro**: 5-часового лимита сейчас нет, но может быть недельный.
2. Установка Codex CLI. В документации все способы глобальные, установки в папку проекта нет. Это исключение из
   правила «всё внутри проекта», такое же, как сам `claude`, поэтому решение за вами:
   ```bash
   brew install --cask codex
   ```
   (или `npm install -g @openai/codex`; обновление: `brew upgrade --cask codex`).
3. Вход по подписке, не по ключу:
   ```bash
   codex login
   ```
   Откроется браузер, выберите «Sign in with ChatGPT». Пароль вводите сами.
4. Проверка входа (код выхода 0 = вход есть):
   ```bash
   codex login status
   ```

Где хранится вход: `~/.codex/auth.json` или Связка ключей macOS. Папку можно перенести переменной `CODEX_HOME`. Если
приложение будет запускать Codex с `CODEX_HOME` внутри проекта, вход надо делать с той же переменной.

### Шаг 2. Часть Claude Code: новый провайдер `codex_cli`

Попросите Claude Code: «добавь провайдер `codex_cli` по docs/OPENAI.md». Что он должен сделать:

- **`llm/codex_cli.py`** — аналог `llm/claude_sdk.py`. Один вызов на один запрос:
  ```bash
  codex exec --json --ephemeral --skip-git-repo-check --sandbox read-only -m <модель> -
  ```
  - Промпт подаётся через stdin. Отдельного system prompt у `codex exec` нет, поэтому инструкции и вакансии идут
    одним текстом.
  - Ответ — из последнего сообщения (`-o <файл>` или события JSONL); дальше та же `parse_json_object`, что и сейчас.
  - Можно `--output-schema <файл.json>` для строгого JSON (как `structured_output` у Claude).
  - Токены — из события `turn.completed`: `input_tokens`, `cached_input_tokens`, `output_tokens`,
    `reasoning_output_tokens`.
  - Глубина рассуждений — через `-c <ключ>=<значение>`. Точное имя ключа в документации не подтверждено:
    проверить по `codex exec --help` на установленной версии.
- **Рабочая папка** — пустая `data/llm_cwd` (как у Claude). Codex — агент с инструментами: в режиме `read-only` он не
  пишет файлы, но читать папку может, поэтому в ней не должно быть ничего лишнего.
- **Ошибки:**
  - лимит подписки → `LlmError(kind="limit")`: прогон и отклики ждут, как сейчас при лимите Claude;
  - нет входа → `kind="auth"`, с подсказкой `codex login`.
- **`config.py` и `llm.yaml`:**
  - `provider: codex_cli`;
  - раздел `codex_cli:` (`cli_path`, `timeout_seconds`, глубина рассуждений, при необходимости `codex_home`);
  - `models.codex_cli: {scoring: …, dedup: …, resume: …}`;
  - `factory.py` — создание провайдера.
- **Тесты без сети и без настоящего Codex:** подставной `codex`-скрипт, который печатает готовые JSONL-события.
- **Документация** — этот файл и раздел 9 в `HANDBOOK.md`.

Модели Codex на 16.11.2026, по документации: `gpt-6.1-sol`, `gpt-6.1-terra`, `gpt-6-luna`, `gpt-6-astra`. Какую ставить
на извлечение фактов (дешёвая, много вызовов), а какую на резюме, лучше решить пробным прогоном. По лимиту Plus
самая «лёгкая» — `gpt-6-luna`: 350–3000 сообщений за 5 часов против 15–160 у `gpt-6.1-sol`.

### Шаг 3. Что в приложении останется «про Claude»

Работать будет, но выглядеть по-клодовски:

- **Кольцо лимитов в шапке и проценты «5h limit»** берутся из ответов Claude CLI (`llm/limits.py`,
  `data/claude_limits.json`). Для Codex их не будет. Документация не обещает, что Codex отдаёт остаток лимита в
  `--json`: это проверить на практике.
- **Надписи вида «Claude limit — retry at …»** и подписи «(Claude)» в сайдбаре Apply.
- **Цены «сколько стоило бы по API»** (`llm.yaml → pricing`): для моделей Codex их нет. Это только оценка, на работу
  не влияет.
- **Скилл резюме `resume-tailor`.** Приложение само читает его файлы и отдаёт текстом в промпт, так что он подойдёт
  любой модели. Сам скилл по-прежнему лежит в папке Claude: если Claude Desktop удалить, путь в
  `config/apply.yaml → resume.skill_dir` придётся перенести на копию скилла.

### Шаг 4. Проверка после переключения

1. `config/llm.yaml`: `provider: codex_cli`, перезапуск.
2. Пробный прогон в отдельную базу:
   ```bash
   .venv/bin/vacancy-retriever run --sources serbiawork --limit 3 --since 1d --db data/dev.sqlite
   ```
3. Statistics → Runs: токены есть, ошибок LLM нет; в ленте `dev.sqlite` факты и звёзды выглядят разумно.
4. Один отклик ▶ на неважной вакансии: резюме и письмо собрались.

## Источники (16.11.2026)

- Установка и вход: https://learn.chatgpt.com/docs/codex/cli , https://learn.chatgpt.com/docs/auth
- `codex exec`: https://learn.chatgpt.com/docs/non-interactive-mode
- Команды: https://learn.chatgpt.com/docs/developer-commands?surface=cli
- Модели: https://learn.chatgpt.com/docs/models
- Планы и лимиты: https://learn.chatgpt.com/docs/pricing

Старые адреса developers.openai.com/codex/… перенаправляют туда же.
