# Vacancy Retriever — заметки для Claude Code

Полное описание проекта: **docs/HANDBOOK.md** (прочитать перед любой правкой).

## Команды

```bash
scripts/setup.sh --yes            # окружение (.venv через uv, из uv.lock)
scripts/start.sh / scripts/stop.sh
.venv/bin/python -m pytest        # все тесты; без сети и без LLM
.venv/bin/vacancy-retriever run --sources <ids> --limit 10 --since 1d --db data/dev.sqlite   # отладочный прогон
.venv/bin/vacancy-retriever check-sources
.venv/bin/vacancy-retriever recalculate
```

## Правила проекта

- Только `https://telegram.me/...`, никогда `t.me` (не открывается у пользователя в Сербии).
- linkedin.com через общий HTTP-клиент не открывать никогда (он это запрещает). Единственное исключение —
  `collectors/linkedin_guest.py`: публичные страницы вакансий из писем, анонимно (адрес только из номера вакансии,
  без cookies / Referer / редиректов), с паузами и остановкой до завтра на первом отказе; включается
  `mail.yaml → linkedin_public_pages.enabled`. Ссылки из писем LinkedIn (токены входа) не открывать никогда.
- Всё внутри папки проекта: зависимости только в `.venv` через `uv` (`UV_CACHE_DIR=./.cache/uv`), данные в
  `data/`, логи в `logs/`. Никаких глобальных установок, правок `~/.zshrc`, `~/Library`.
- Настройки — в `config/*.yaml` (с комментариями по-русски), не хардкодом. Конфиги валидируются в `config.py`.
- LLM только извлекает факты (`scoring/facts.py`); группу, звёзды, ВНЖ и отсев считает
  `scoring/engine.py` по `config/rules.yaml`. Примеры из ТЗ — тесты в `tests/test_scoring_examples.py`.
- Прогон транзакционный: всё пишется со `staged=1`, видимым становится только при коммите
  (`storage/runs.py`). Не менять закоммиченные строки из прогона напрямую.
- Интерфейс — на английском; саммари вакансий — на русском; документация — на русском.
- Новый источник = сборщик в `collectors/sites/` + снимок в `samples/html/<id>/` + тест.
- Каждый упавший тест — блокер; тесты не скипать.
- LLM по умолчанию — `claude_sdk` (подписка через Claude Code CLI), см. `config/llm.yaml`.
