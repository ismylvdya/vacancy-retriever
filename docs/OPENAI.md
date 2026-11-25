# Как перевести приложение с Claude на OpenAI (ChatGPT)

Есть два пути, и они разные:

| | (А) API OpenAI | (Б) Подписка ChatGPT через Codex CLI |
|---|---|---|
| Как платится | по токенам, отдельно от подписки ChatGPT | входит в подписку ChatGPT (Plus / Pro / …) |
| Аналог у Claude | `anthropic_api` | `claude_sdk` (подписка через Claude Code CLI) |
| Готово в приложении | **да**, `provider: openai_api` | **да**, `provider: codex_cli` |

Факты о Codex CLI сверены по официальной документации 16.11.2026 (адреса в конце). Названия моделей и тарифы
у OpenAI меняются часто: перед переключением проверьте их ещё раз.

## (А) Платный API OpenAI: работает уже сейчас

1. https://platform.openai.com → **API keys** → Create new secret key.
2. **Billing** → способ оплаты и пополнение; **Limits** → месячный бюджет (например, $10), чтобы не было сюрпризов.
3. В `config/.env` впишите ключ сами: `OPENAI_API_KEY=sk-…` (в чат его не вставлять).
4. В `config/llm.yaml`:
   - `provider: openai_api`;
   - в `models.openai_api` проверьте модели. Сейчас там только `scoring` и `dedup` (`gpt-5.4-nano`). Если не указать
     `resume`, резюме и сопроводительные (кнопка «▶») будет писать та же маленькая модель: добавьте модель посильнее.
     Проверьте на https://platform.openai.com/docs/models, что выбранные модели ещё доступны в API.
5. Перезапуск: `scripts/stop.sh`, затем `scripts/start.sh`.
6. Проверка без риска для основной базы:
   ```bash
   .venv/bin/vacancy-retriever run --sources serbiawork --limit 3 --since 1d --db data/dev.sqlite
   ```
   Затем Statistics → Runs: токены и ошибки этого прогона.

Вернуться на Claude: `provider: claude_sdk` и перезапуск.

## (Б) Подписка ChatGPT через Codex CLI

Приложение находит Codex в PATH и в установленном ChatGPT.app на Mac. Для другой установки укажите исполняемый файл в `codex_cli.cli_path`. CLI должен поддерживать `exec --ignore-user-config`. Войдите через `codex login` с ChatGPT. Проверка — `codex login status`.

В настройках LLM выберите Codex (ChatGPT) и перезапустите приложение. В `config/llm.yaml` модели для разбора и дедупликации — `gpt-6.2-sol`, для резюме и письма — `gpt-6.2-astra`. Параметры CLI находятся в разделе `codex_cli`. Приложение читает установленный навык из `~/.agents/skills/resume-tailor` и само запускает его сборщик PDF.

Чтобы вернуться на подписку Claude, выберите Claude Code и перезапустите приложение. Для него используются модели из отдельной строки `models.claude_sdk`.

## Источники (16.11.2026)

- Установка и вход: https://learn.chatgpt.com/docs/codex/cli , https://learn.chatgpt.com/docs/auth
- `codex exec`: https://learn.chatgpt.com/docs/non-interactive-mode
- Команды: https://learn.chatgpt.com/docs/developer-commands?surface=cli
- Модели: https://learn.chatgpt.com/docs/models
- Планы и лимиты: https://learn.chatgpt.com/docs/pricing

Старые адреса developers.openai.com/codex/… перенаправляют туда же.
