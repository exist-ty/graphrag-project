# MEMORY — текущий контекст проекта

Последнее обновление: 2026-08-06

## Текущее состояние

- Проект создан в `D:\projects\graphrag-project\`. На диске уже есть: `.venv` (Python 3.12,
  `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2), `.env` с `GEMINI_API_KEY`,
  `.gitignore` (игнорирует `.env`, `.venv/`, `rag_storage/`, `data/generated/`).
- `scripts/`, `data/`, `rag_storage/` — созданы как пустые директории, содержимое ещё не
  написано (`generate_synthetic_data.py`, `build_kg.py`, `query_example.py` из PLAN.md — не
  начаты).
- `orchestration.md`, `CLAUDE.md`, `MEMORY.md` — созданы 2026-08-06.
- Прогресс по шагам PLAN.md: **0 из 3** скриптов написано, генерация синтетики не запускалась,
  верификация не проводилась.

## Git

- Remote: **https://github.com/exist-ty/graphrag-project** (ветка `main`).
- Репозиторий инициализирован локально 2026-08-06 (`git init -b main`), первый коммит — только
  документация. `gh` CLI на машине **не установлен** — работать через обычный `git`, не через `gh`.
- Под gitignore: `.env`, `.venv/`, `rag_storage/`, `data/generated/`, `pip_install.log`,
  `orchestration/runs/` (сырые JSON-ответы делегированных вызовов `agy`).
- Коммиты делать по ходу работы, не копить.

## Подтверждённые технические факты

- Модели Google AI Studio подтверждены доступными на аккаунте прямым вызовом `GET
  /v1beta/models`: `gemini-3.6-flash` (LLM для извлечения сущностей/связей),
  `gemini-embedding-2` (embedding).
- `embedding_dim` для `gemini-embedding-2` **проверен эмпирически** (2026-08-06, прямой вызов
  `embedContent`): нативная размерность — **3072**; `output_dimensionality` тоже поддерживается,
  проверены 768 / 1536 / 3072 — все возвращают ровно запрошенную длину. Проект фиксирует **3072**.
  Блокирующий шаг из PLAN.md раздел 2 — закрыт.
- **Ловушка двойной обёртки в LightRAG**: `lightrag.llm.gemini.gemini_embed` уже декорирована
  `@wrap_embedding_func_with_attrs(embedding_dim=1536, model_name="gemini-embedding-001")`. Оборачивать
  надо `gemini_embed.func` (`partial(gemini_embed.func, model="gemini-embedding-2")`), иначе внутренняя
  обёртка переопределит настройки и размерность молча уедет в 1536.
- Порядок инициализации LightRAG 1.5.5: `LightRAG(...)` → `await rag.initialize_storages()` →
  `await initialize_pipeline_status()` (из `lightrag.kg.shared_storage`); закрытие —
  `await rag.finalize_storages()`. Пропуск `initialize_pipeline_status()` — типичная причина зависаний.
- `QueryParam.mode` допускает `local|global|hybrid|naive|mix|bypass` (дефолт `mix`), а
  `enable_rerank` по умолчанию `True` — реранкер в проекте не сконфигурирован, ставим `False` явно.
- `agy` CLI (Antigravity CLI, v1.1.10, `D:\.gemini\agy\agy.exe`, в PATH как `agy`) проверен
  живыми вызовами: headless-режим (`--print`/`--output-format json`) работает,
  `--conversation <id>` реально продолжает сессию с сохранением контекста, `agy agent`/`agy
  plugin list` — пусты (именных агентов и плагинов не сконфигурировано). Подробности и параметры
  — в `orchestration.md`.
- Стоимость вызова `agy`: даже тривиальный промпт даёт ~20-27k input-токенов системного оверхеда;
  `cache_read_tokens` во всех тестовых вызовах был `0` — кэш между отдельными `--print`-вызовами
  не подтверждён.

## Открытые вопросы / риски

- Интерфейс запросов (встроенный `lightrag-server` с Web UI / MCP-обёртка / просто CLI) —
  выбор отложен пользователем на потом, не начинать без явного запроса.
- Полный объём генерации синтетики (~1000 документов) не запускать до прохождения верификации на
  малой партии (~20-30 документов) — расход API-квоты пропорционален объёму.
- `embedding_dim` для `gemini-embedding-2` — см. выше, требует одного тестового вызова
  `embedContent` перед тем, как фиксировать его в коде.

## Лог сессий

- **2026-08-06**: обсудили и проверили `agy` CLI как исполнителя для делегирования рутины
  (headless-вызовы, модели, параметры) → зафиксировано в `orchestration.md`. Создан
  project-level `CLAUDE.md` со ссылками на `PLAN.md`/`orchestration.md` и правилами ведения этого
  файла. Сама разработка проекта (скрипты, генерация, ингестия) ещё не начиналась.
