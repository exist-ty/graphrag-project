## Общий контекст проекта (проверено эмпирически, НЕ додумывай)

Проект: GraphRAG на LightRAG + Google AI Studio (Gemini). Корень: `D:\Projects\graphrag-project`.
Скрипты запускаются из корня проекта как `python scripts/<name>.py`. ОС — Windows, но код должен
быть кроссплатформенным (pathlib, никаких хардкоженных обратных слэшей).

Окружение (уже установлено, ничего добавлять нельзя):
- Python 3.12 venv в `.venv/`
- `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2, numpy
- Внешних зависимостей сверх этого списка НЕ использовать (argparse вместо click).

Модели проекта: LLM `gemini-3.6-flash`, embedding `gemini-embedding-2` (размерность 3072).
Но в ЭТОМ файле их конфигурировать не нужно — см. контракт ниже.

## Требования к ответу

- Верни РОВНО ОДИН блок ```python с полным содержимым файла. Никакого текста до или после блока.
- Файл должен запускаться как есть, без плейсхолдеров и TODO.
- Докстринги и комментарии — на русском, код/идентификаторы — на английском.
- Обязательно: аннотации типов, `if __name__ == "__main__":`.
- Не изобретай API: используй ровно те сигнатуры, что даны ниже.

## Задача: написать `scripts/query_example.py`

Минимальный CLI поверх готового графа LightRAG — для смоук-теста конвейера.

### Зафиксированный контракт с соседним модулем

Соседний скрипт `scripts/build_kg.py` (пишется параллельно другой моделью) экспортирует:

```python
async def create_rag(working_dir: str = "rag_storage") -> LightRAG: ...
```

Она возвращает УЖЕ инициализированный экземпляр (внутри уже сделаны `initialize_storages()` и
`initialize_pipeline_status()`). `query_example.py` обязан переиспользовать её, а не дублировать
сборку LLM-функции и embedding-функции:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # чтобы работал импорт соседа
from build_kg import create_rag
```

Никакой другой конфигурации моделей в этом файле быть не должно — единственный источник истины
по моделям и размерности живёт в `build_kg.py`.

### Проверенные сигнатуры (прочитаны из исходников пакета)

- `await rag.aquery(query: str, param: QueryParam = ..., system_prompt: str | None = None) -> str | AsyncIterator[str]`
- `QueryParam.mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass"]`, по умолчанию `"mix"`.
- Прочие поля `QueryParam` с дефолтами: `top_k=40`, `chunk_top_k=20`, `stream=False`,
  `response_type="Multiple Paragraphs"`, `enable_rerank=True`, `include_references=False`,
  `only_need_context=False`.
- ВАЖНО: `enable_rerank=True` — дефолт, но реранкер в этом проекте НЕ сконфигурирован.
  Выставляй `enable_rerank=False` явно, иначе возможны предупреждения и лишние ветки кода.
- Завершение: `await rag.finalize_storages()` в `try/finally`.

### CLI (argparse)

- Позиционный аргумент `question` (необязательный).
- `--mode` с выбором из перечисленных литералов, по умолчанию `hybrid`.
- `--all-modes` — прогнать один и тот же вопрос по `local`, `global`, `hybrid` и напечатать ответы
  рядом, с заголовками. Это ровно то, что нужно для шага верификации проекта (2-3 тестовых запроса
  в разных режимах, сверить с `ground_truth.json`).
- `--working-dir` (по умолчанию `rag_storage`), `--top-k`, `--only-context` (вернуть найденный
  контекст без генерации ответа — полезно, чтобы понять, ЧТО именно нашёл ретривер).
- Если `question` не передан — интерактивный REPL: читает вопросы из stdin в цикле, `exit`/`quit`/
  Ctrl-D завершают. Между вопросами граф НЕ переинициализируется.
- `--questions-file <path>` — прогнать пачку вопросов из файла (по одному на строку).

### Что критично

- Вывод человекочитаемый: заголовок с режимом и временем ответа, затем текст.
- Понятная ошибка, если `working_dir` пуст или не существует: подсказать сначала запустить
  `python scripts/build_kg.py`, а не падать трейсбэком.
- Никаких зависаний: обработать KeyboardInterrupt аккуратно.
