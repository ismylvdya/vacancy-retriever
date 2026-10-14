# PROGRESS — журнал сборки v0

Старт работы: **2026-10-13 14:06 UTC**. Ограничение: новые функции не начинать после 20:06 UTC.

## Сделано

- [x] Скелет: `pyproject.toml` (version 0.0.0), `uv.lock`, `.venv` на Python 3.12 (Homebrew), `.gitignore`.
- [x] Конфиги с комментариями и валидацией: `sources.yaml`, `rules.yaml`, `llm.yaml`, `mail.yaml`, `profile.md`,
      `prompts/scoring.md`, `prompts/dedup.md`, `.env.example`.
- [x] Движок правил (группа / звёзды / ВНЖ / отсев) + все 26 примеров ТЗ как тесты.
- [x] Сборщики: Telegram (30 каналов, RVC-разбор), сайты: getmatch, habr, geekjob (JSON API), ovdejobs,
      changellenge (HTML), youngjunior, hirify (API сайтов), talentmove (RSS). Отключены с причиной:
      infostud, startuj, helloworld (Cloudflare), hirehi (недоступен). Почта: IMAP, modified UTF-7 ярлык
      «🦆 retrievered», универсальный парсер, реестр точных парсеров, `export-email-samples`.
- [x] Конвейер: сбор → дедуп сырых → предфильтр → докачка → LLM-факты → дедуп вакансий (код + LLM) → коммит.
      Staging с откатом, курсоры по источникам, `pending_llm` при отказе LLM, восстановление после сбоя.
- [x] LLM: `claude_sdk` (подписка, изоляция от настроек пользователя, без транскриптов), `anthropic_api`,
      `openai_api` (на моках). Учёт токенов в БД и `data/token_log.jsonl`.
- [x] Веб-интерфейс: Feed / Processed / Source health / Tokens; кнопки статусов, копирование текста,
      прогресс, двойной клик для отмены, пересчёт без LLM. Защита POST по Origin/Host.
- [x] Меню-бар (rumps, без Dock), уведомления через osascript, `start.sh` / `stop.sh`.
- [x] Eval Haiku vs Sonnet на 50 реальных вакансиях → Haiku по умолчанию (EVAL.md).
- [x] `setup.sh` (dry-run, --yes, --skip-brew) проверен в dry-run и на чистом клоне.
- [x] Ручные проверки в браузере: лента, Not now → Processed → Return, Health, Tokens; отмена прогона из UI
      откатывает данные и курсоры (3 раза).
- [x] Документация: HANDBOOK, README, CLAUDE.md, TRANSFER, DECISIONS, EVAL, CHANGES_ON_HOST.

- [x] Полный прогон по всем источникам: сбор прошёл (34 источника, 3500+ элементов, RVC — `truncated`),
      LLM-этап остановлен по просьбе владельца машины (882 элемента ≈ час); прерванный прогон корректно
      откатился при перезапуске («Interrupted»). Вместо него — частичный прогон 8 источников × 5 элементов
      (1.5 мин, 3 новых в ленте, почта `not_configured` без падения) и второй прогон: Telegram/RVC показали
      по 20 постов, новых 0 — курсоры работают; уже собранное не вернулось.
- [x] `git grep /Users/` — абсолютных путей машины сборки в коде нет; секретов нет; личные контакты в
      снимках замаскированы. Тег `v0`.
- [x] Порт по умолчанию 18765 (8765 занят AnkiConnect).

## Открытые вопросы

Решены пользователем 2026-10-14 (см. DECISIONS.md №33–39): RVC — отсев «нет русского и не Сербия» до LLM;
офис/гибрид вне Сербии — отсев; первый прогон — 1 день; почта пока отключена; infostud и startuj включены.

## План v1 (не вошло в v0)

- Решение пользователя: отсеивать ли офис/гибрид вне Сербии и категорию `other` (сейчас они в группе 5).
- Точные парсеры писем (нужны реальные образцы: `export-email-samples`).
- Повторная проверка infostud / startuj / helloworld / hirehi с сербского IP; для Cloudflare-сайтов — только
  если найдётся публичный API или RSS.
- UI настроек, синхронизированный с `config/*.yaml`.
- Расширить фильтры getmatch/habr (сейчас мало вакансий junior+data).
