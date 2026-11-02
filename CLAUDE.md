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
- linkedin.com через общий HTTP-клиент не открывать никогда (он это запрещает). Описания LinkedIn — только
  `collectors/job_search.py` (Google for Jobs через JSearch/SerpApi, без запросов к LinkedIn); что не нашлось,
  оценивается по письму. Запасной путь `mail.yaml → linkedin_public_pages` **по умолчанию выключен; включает только сам
  пользователь** в Settings (рабочий прокси + фраза, `tests/test_linkedin_off.py`), Claude его не включает. Когда включён: промахи ждут вне ленты и
  не раньше чем через день читаются `collectors/linkedin_guest.py` — анонимно (адрес только из номера вакансии, без
  cookies / Referer / редиректов, без браузера) и **только через приватный выход** (`collectors/egress.py`: прокси или
  Xray; перед каждой страницей — не домашний IPv4/IPv6, не сменился). Никогда с домашнего IP: на этом же Mac и в этой
  же сети пользователь залогинен в LinkedIn. Ссылки и картинки из писем LinkedIn (токены входа) не открывать никогда.
  Риски и решения — `docs/LINKEDIN.md`; ослаблять любую из этих проверок нельзя.
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
