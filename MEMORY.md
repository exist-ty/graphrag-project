# MEMORY — текущий контекст проекта

Последнее обновление: 2026-08-06

## Текущее состояние

- Проект создан в `D:\projects\graphrag-project\`. На диске уже есть: `.venv` (Python 3.12,
  `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2), `.env` с `GEMINI_API_KEY`,
  `.gitignore` (игнорирует `.env`, `.venv/`, `rag_storage/`, `data/generated/`).
- Все **3 скрипта написаны** и закоммичены (делегированы разным моделям `agy`, см. ниже):
  `scripts/generate_synthetic_data.py`, `scripts/build_kg.py`, `scripts/query_example.py`.
- `data/ground_truth.json` — сгенерирован и проверен: 3 дома, 3 мегакорпорации, 8 персон (у всех
  алиасы/титулы/позывные), 6 станций и кораблей с **реальными омонимами** (`Aurelia-Prime` ×2,
  `Vanguard` ×2), иерархия глубиной 3 уровня (level 2→4), 8 событий таймлайна.
- `orchestration.md`, `CLAUDE.md`, `MEMORY.md` — созданы 2026-08-06.
- Прогресс по PLAN.md: код готов; шаг «Верификация» — seed-проход пройден, дальше по списку
  генерация малой партии → `build_kg.py` → запросы в разных режимах.

## Делегирование: что реально сработало

Схема прогона: я (Claude Code) — оркестратор, готовлю самодостаточные промпты с **верифицированными
сигнатурами API** (интроспекция установленных пакетов, а не память модели), `agy --print` пишет код,
я его читаю и правлю. Промпты лежат в `orchestration/prompts/`, извлечение из JSON —
`orchestration/extract.py`, сырые ответы — `orchestration/runs/` (gitignored).

- Маршрутизация: `build_kg.py` → `claude-sonnet-4-6` (самая тонкая интеграция),
  `query_example.py` и `generate_synthetic_data.py` → `gemini-3.6-flash-high`.
- **`gpt-oss-120b-medium` провалил `generate_synthetic_data.py`**: написал весь скрипт на старом,
  не установленном SDK `google.generativeai` (`genai.configure`, `genai.AsyncClient`,
  `genai.exceptions.RateLimitError`, `generation_config=`). Причина — в промпте не был зафиксирован
  контракт `google-genai`. После добавления точных сигнатур в промпт и переезда на
  `gemini-3.6-flash-high` результат стал рабочим с первого раза.
- **Вывод для будущих делегирований**: качество результата определяется не столько моделью, сколько
  тем, зафиксирован ли в промпте проверенный API. Без этого модель уверенно пишет по памяти —
  и промахивается мимо версии пакета.
- Что пришлось чинить руками после делегатов: `build_kg.py` искал `*.txt`, тогда как генератор
  производит `*.md` (нашёл бы ноль файлов).

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
- Стоимость вызова `agy`: даже тривиальный промпт даёт ~20-27k input-токенов системного оверхеда.
  На реальных промптах делегирования — 29-46k input. **Кэш между отдельными `--print`-вызовами
  всё-таки работает**: у gemini-моделей `cache_read_tokens` был 77k и 86k (у `claude-sonnet-4-6` и
  `gpt-oss-120b-medium` — 0). Раннее наблюдение «кэш не подтверждён» относилось только к
  тривиальным тестовым промптам.
- **`--effort` не поддерживается моделью `claude-sonnet-4-6`**: вызов падает с
  `invalid model selection ... --effort is not supported for model "claude-sonnet-4-6"`,
  `status: ERROR`, ноль потраченных токенов. Рекомендация в `orchestration.md` использовать
  `claude-sonnet-4-6 --effort medium` — неверна, флаг надо просто опускать.
- **Gemini-модели в `agy` ведут себя агентно даже на чисто текстовой задаче**: `gemini-3.6-flash-high`
  попытался вызвать инструмент, требующий разрешения `command`, headless-режим авто-отклонил его, и
  `response` вернулся ПУСТЫМ при `status: SUCCESS` и 12k output-токенов. Реальная причина видна
  только в stderr, не в JSON. Вывод: (1) всегда проверять и stderr тоже, а не только
  `status`; (2) пустой `response` при ненулевом `output_tokens` — признак авто-отклонённого
  инструмента. При этом сам результат агент успел записать прямо в файл проекта — то есть
  делегированный вызов может изменить рабочее дерево даже без `--dangerously-skip-permissions`.

## Открытые вопросы / риски

- Интерфейс запросов (встроенный `lightrag-server` с Web UI / MCP-обёртка / просто CLI) —
  выбор отложен пользователем на потом, не начинать без явного запроса.
- Полный объём генерации синтетики (~1000 документов) не запускать до прохождения верификации на
  малой партии (~20-30 документов) — расход API-квоты пропорционален объёму.
- Ингестия (`build_kg.py`) на реальном корпусе ещё **не прогонялась** — то есть связка
  `EmbeddingFunc` + `gemini_embed.func` + `ainsert` проверена только чтением кода, не запуском.
  Это ближайший непройденный шаг верификации.
- `orchestration.md` местами разошёлся с реальностью (`--effort` для `claude-sonnet-4-6`, вывод про
  кэш) — правки перечислены выше в «Подтверждённых фактах», сам документ ещё не обновлён.

## Лог сессий

- **2026-08-06**: обсудили и проверили `agy` CLI как исполнителя для делегирования рутины
  (headless-вызовы, модели, параметры) → зафиксировано в `orchestration.md`. Создан
  project-level `CLAUDE.md` со ссылками на `PLAN.md`/`orchestration.md` и правилами ведения этого
  файла. Сама разработка проекта (скрипты, генерация, ингестия) ещё не начиналась.
