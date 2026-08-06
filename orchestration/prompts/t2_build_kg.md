## Общий контекст проекта (проверено эмпирически, НЕ додумывай)

Проект: GraphRAG на LightRAG + Google AI Studio (Gemini). Корень: `D:\Projects\graphrag-project`.
Скрипты запускаются из корня проекта как `python scripts/<name>.py`. ОС — Windows, но код должен
быть кроссплатформенным (pathlib, никаких хардкоженных обратных слэшей).

Окружение (уже установлено, ничего добавлять нельзя):
- Python 3.12 venv в `.venv/`
- `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2, numpy
- Внешних зависимостей сверх этого списка НЕ использовать (argparse вместо click).

Секреты: `.env` в корне проекта содержит `GEMINI_API_KEY=...`. Загружать через
`dotenv.load_dotenv()`. Ключ никогда не логировать и не хардкодить.

Модели (id подтверждены живыми вызовами API на этом аккаунте):
- LLM: `gemini-3.6-flash`
- Embedding: `gemini-embedding-2`, нативная размерность 3072 (замерено вызовом `embedContent`).

## Требования к ответу

- Верни РОВНО ОДИН блок ```python с полным содержимым файла. Никакого текста до или после блока.
- Файл должен запускаться как есть, без плейсхолдеров и TODO.
- Докстринги и комментарии — на русском, код/идентификаторы — на английском.
- Обязательно: аннотации типов, `if __name__ == "__main__":`.
- Не изобретай API: используй ровно те сигнатуры, что даны ниже.

## Задача: написать `scripts/build_kg.py`

Пайплайн ингестии документов из `data/generated/` в LightRAG.

### Проверенные сигнатуры LightRAG 1.5.5 (прочитаны из исходников пакета — используй ровно их)

```python
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc
from lightrag.llm.gemini import gemini_complete_if_cache, gemini_embed
from lightrag.kg.shared_storage import initialize_pipeline_status
```

- `EmbeddingFunc` — dataclass с полями: `embedding_dim` (обязателен), `func` (обязателен),
  `max_token_size=None`, `send_dimensions=False`, `model_name=None`, `supports_asymmetric=False`.
- ЛОВУШКА ДВОЙНОЙ ОБЁРТКИ: `gemini_embed` УЖЕ декорирована
  `@wrap_embedding_func_with_attrs(embedding_dim=1536, model_name="gemini-embedding-001")`.
  Если обернуть её напрямую через `functools.partial(gemini_embed, ...)`, внутренняя обёртка
  переопределит настройки и размерность уедет в 1536. Нужно брать `gemini_embed.func`:
  `partial(gemini_embed.func, model="gemini-embedding-2")`. Это явно задокументировано в докстринге
  `EmbeddingFunc` — воспроизведи это как комментарий в коде, чтобы ловушка не вернулась.
- Сигнатура `gemini_embed(texts, model=..., base_url=None, api_key=None, embedding_dim=None,
  max_token_size=None, task_type=None, timeout=None, token_tracker=None, context="document")`.
  `embedding_dim` пробрасывается в `output_dimensionality` и инжектится обёрткой автоматически —
  вручную его в partial НЕ передавать. `supports_asymmetric=True` даёт правильные
  `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` task_type для индексации и запроса — включи его.
- Сигнатура `gemini_complete_if_cache(model, prompt, system_prompt=None, history_messages=None,
  ...)`. LLM-функция для LightRAG должна иметь вид
  `async def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs) -> str`
  и вызывать `gemini_complete_if_cache("gemini-3.6-flash", prompt, ...)`. Из `kwargs` НЕЛЬЗЯ
  бездумно прокидывать всё подряд — убери внутренние ключи LightRAG вроде `hashing_kv`, если они
  конфликтуют; сохрани проброс `response_format` / `keyword_extraction` / `entity_extraction`.
- Поля `LightRAG`, релевантные здесь (с дефолтами): `working_dir="./rag_storage"`,
  `embedding_func=None`, `llm_model_func=None`, `llm_model_name="gpt-4o-mini"` (ОБЯЗАТЕЛЬНО
  переопредели на `gemini-3.6-flash`), `llm_model_max_async=4`, `embedding_batch_num=10`,
  `embedding_func_max_async=8`, `max_parallel_insert=3`, `embedding_token_limit=None`,
  `summary_max_tokens=1200`.
- Инициализация ОБЯЗАТЕЛЬНО в таком порядке:
  `rag = LightRAG(...)` → `await rag.initialize_storages()` → `await initialize_pipeline_status()`.
  Пропуск второго шага — частая причина зависаний пайплайна. В конце — `await rag.finalize_storages()`
  (метод существует), в `try/finally`.
- `await rag.ainsert(input, split_by_character=None, split_by_character_only=False, ids=None,
  file_paths=None, track_id=None)` — принимает `str | list[str]`. Передавать `file_paths` вместе с
  содержимым, чтобы источники были видны в графе.
- Embedding-модель `gemini-embedding-2` принимает до 8192 токенов на вход → `max_token_size=8192`,
  `embedding_dim=3072`.

### Требования к скрипту

- ЭКСПОРТИРОВАТЬ `async def create_rag(working_dir: str = "rag_storage") -> LightRAG` — фабрику,
  которая собирает LLM-функцию, `EmbeddingFunc` и полностью инициализированный экземпляр LightRAG
  (внутри уже вызваны `initialize_storages()` и `initialize_pipeline_status()`).
  Эту функцию импортирует соседний скрипт `query_example.py`, поэтому её имя и сигнатура — 
  зафиксированный контракт, менять нельзя.
- CLI (argparse): `--input-dir` (по умолчанию `data/generated`), `--working-dir` (по умолчанию
  `rag_storage`), `--limit N` (ингестить только первые N файлов — для смоук-теста), `--batch-size`
  (сколько документов за один `ainsert`, по умолчанию 10), `--dry-run` (показать, что будет
  проингестировано, без вызовов API), `--force`.
- Идемпотентность: вести журнал уже проингестированных документов (`rag_storage/ingested.json`
  с хэшем содержимого) и по умолчанию пропускать неизменившиеся; `--force` игнорирует журнал.
- Логирование через `logging` (не print): сколько файлов найдено, сколько проингестировано,
  прогресс по батчам, итоговое время. Ошибка на одном батче не должна ронять весь прогон.
- В конце — краткая сводка, включая проверку, что в `working_dir` реально появились непустые файлы
  хранилища (граф / векторы / kv): это шаг верификации из плана проекта.
