"""
scripts/build_kg.py — пайплайн ингестии документов из data/generated/ в LightRAG.

Использование:
    python scripts/build_kg.py [--input-dir PATH] [--working-dir PATH]
                               [--limit N] [--batch-size N] [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from dotenv import load_dotenv
from google.genai import errors as genai_errors
from lightrag import LightRAG
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.llm.gemini import gemini_complete_if_cache, gemini_embed
from lightrag.utils import EmbeddingFunc

# ---------------------------------------------------------------------------
# Константы моделей
# ---------------------------------------------------------------------------
# Генеративная LLM для извлечения сущностей/связей. Переведена с `gemini-3.6-flash` на lite:
# у free tier лимит 20 запросов В СУТКИ на модель, а LightRAG делает несколько вызовов на
# каждый чанк — на 3.6-flash ингестия не помещалась в квоту. Квота считается на модель,
# поэтому у lite отдельный бюджет.
LLM_MODEL = "gemini-3.5-flash-lite"
# Embedding-модель специальная и НЕ меняется вместе с LLM: у неё своя, отдельная квота.
EMBED_MODEL = "gemini-embedding-2"
EMBED_DIM = 3072       # нативная размерность gemini-embedding-2 (замерено вызовом embedContent)
EMBED_MAX_TOKENS = 8192  # максимум токенов на вход для gemini-embedding-2

# ---------------------------------------------------------------------------
# Ключи из kwargs LightRAG, которые нельзя пробрасывать в Gemini API напрямую
# ---------------------------------------------------------------------------
_LIGHTRAG_INTERNAL_KEYS = frozenset({
    "hashing_kv",
    "mode",
    "response_format",
    "stream",
    "keyword_extraction",
    "entity_extraction",
    "current_entity_types",
    "addon_params",
    "prompt_name",
})

# Ключи, которые безопасно пробрасывать в Gemini API
_GEMINI_PASSTHROUGH_KEYS = frozenset({
    "response_format",
    "keyword_extraction",
    "entity_extraction",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("build_kg")


# ---------------------------------------------------------------------------
# LLM-функция
# ---------------------------------------------------------------------------

async def llm_model_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list[dict[str, Any]] | None = None,
    **kwargs: Any,
) -> str:
    """
    Обёртка над gemini_complete_if_cache для LightRAG.

    Фильтруем внутренние ключи LightRAG (например hashing_kv), которые
    конфликтуют с Gemini API. Пробрасываем только безопасные ключи:
    response_format, keyword_extraction, entity_extraction.
    """
    safe_kwargs: dict[str, Any] = {
        key: value
        for key, value in kwargs.items()
        if key in _GEMINI_PASSTHROUGH_KEYS
    }
    return await _call_with_429_retry(
        "llm",
        _LLM_LIMITER,
        lambda: gemini_complete_if_cache(
            LLM_MODEL,
            prompt,
            system_prompt=system_prompt,
            history_messages=history_messages or [],
            **safe_kwargs,
        ),
    )


# ---------------------------------------------------------------------------
# Фабрика RAG
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ограничение частоты запросов
# ---------------------------------------------------------------------------
# ОБА лимита free tier — ПОМИНУТНЫЕ (метрики вычитаны из тел реальных 429-ошибок):
#   LLM:       GenerateRequestsPerMinutePerProjectPerModel-FreeTier
#   Embedding: EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier, 100/мин
# Поминутный лимит, в отличие от суточного, лечится ожиданием — поэтому ждать здесь дешевле,
# чем ловить 429: одна такая ошибка роняет ВЕСЬ документ в статус failed.


class RateLimiter:
    """Скользящее окно на 60 секунд: не выпускает больше `limit` запросов в минуту.

    Семафор ограничивает лишь ОДНОВРЕМЕННОСТЬ, а не частоту: несколько быстрых параллельных
    запросов легко дают сотни вызовов в минуту и выбирают поминутную квоту.
    """

    def __init__(self, limit: int, name: str) -> None:
        self.limit = limit
        self.name = name
        self._lock = asyncio.Lock()
        self._calls: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.limit:
                    self._calls.append(now)
                    return
                sleep_for = 60.0 - (now - self._calls[0]) + 0.05
            logger.debug("%s: лимит %d/мин достигнут, ждём %.1f с", self.name, self.limit, sleep_for)
            await asyncio.sleep(max(sleep_for, 0.05))


# Пороги взяты заметно ниже фактических лимитов: наблюдаемые 429 приходили и при 80/мин,
# поэтому запас должен быть большим, а не символическим.
_EMBED_LIMITER = RateLimiter(limit=45, name="embedding")
_LLM_LIMITER = RateLimiter(limit=12, name="llm")
_EMBED_SEMAPHORE = asyncio.Semaphore(2)


def _retry_delay_from_error(exc: Exception, attempt: int) -> float:
    """Достаёт `retryDelay` из тела 429-ошибки Gemini; иначе — экспоненциальный откат."""
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(exc))
    if match:
        return float(match.group(1)) + 1.0
    return min(2.0 * (2 ** attempt), 60.0)


async def _call_with_429_retry(
    what: str,
    limiter: "RateLimiter",
    call: "Any",
    attempts: int = 6,
) -> Any:
    """
    Выполняет вызов под ограничителем частоты, переживая 429.

    Свой ретрай нужен потому, что штатный `@retry` в биндингах LightRAG ловит
    `google.api_core.exceptions.ResourceExhausted`, а новый SDK бросает
    `google.genai.errors.ClientError` — декоратор проходит мимо неё.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        await limiter.acquire()
        try:
            return await call()
        except genai_errors.ClientError as exc:
            if getattr(exc, "code", None) != 429:
                raise
            last_exc = exc
            delay = _retry_delay_from_error(exc, attempt)
        logger.warning("%s: 429, попытка %d/%d, ждём %.1f с", what, attempt + 1, attempts, delay)
        await asyncio.sleep(delay)
    raise RuntimeError(f"{what}: не удалось после {attempts} попыток: {last_exc}")


async def embed_texts_one_by_one(
    texts: list[str],
    max_token_size: int | None = None,
    context: str = "document",
    **kwargs: Any,
) -> "np.ndarray":
    """
    Эмбеддинг списка текстов по одному запросу на текст.

    ЛОВУШКА БАТЧА: `gemini-embedding-2` на вход из нескольких текстов возвращает РОВНО ОДИН
    вектор — молча, без ошибки (проверено прямыми вызовами `embed_content` на 1/2/4 текстах:
    во всех случаях `len(response.embeddings) == 1`). Штатный биндинг
    `lightrag.llm.gemini.gemini_embed` шлёт весь батч одним запросом и ожидает N векторов, из-за
    чего LightRAG падает с `Vector count mismatch: expected N vectors but got 1`.

    Поэтому батч разворачивается в отдельные запросы. Это дороже по числу запросов к API,
    но единственный корректный вариант: иначе в индекс попали бы неверные векторы.
    """
    async def one(text: str) -> "np.ndarray":
        async with _EMBED_SEMAPHORE:
            return await _call_with_429_retry(
                "embedding",
                _EMBED_LIMITER,
                lambda: gemini_embed.func(  # type: ignore[attr-defined]
                    [text],
                    model=EMBED_MODEL,
                    api_key=os.environ["GEMINI_API_KEY"],
                    max_token_size=max_token_size,
                    context=context,
                    **kwargs,
                ),
            )

    parts = await asyncio.gather(*(one(text) for text in texts))
    return np.vstack(parts)


async def create_rag(working_dir: str = "rag_storage") -> LightRAG:
    """
    Собирает и полностью инициализирует экземпляр LightRAG.

    Порядок инициализации ВАЖЕН:
        1. LightRAG(...)
        2. await rag.initialize_storages()       — пропуск вызывает зависания пайплайна
        3. await initialize_pipeline_status()    — обязательный шаг перед ainsert

    ЛОВУШКА ДВОЙНОЙ ОБЁРТКИ:
        gemini_embed уже декорирована @wrap_embedding_func_with_attrs(embedding_dim=1536, ...).
        Если использовать partial(gemini_embed, ...) напрямую, внутренняя обёртка
        переопределит настройки и размерность уедёт в 1536.
        Решение: брать gemini_embed.func — сырую функцию до декоратора.

    supports_asymmetric=True активирует RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY task_type,
    что обеспечивает корректное разделение индексации и запроса.
    """
    load_dotenv()
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY не найден. Добавьте его в файл .env в корне проекта."
        )

    # Обёртка сама берёт gemini_embed.func — см. «ЛОВУШКА ДВОЙНОЙ ОБЁРТКИ» выше — и, кроме того,
    # разворачивает батч в отдельные запросы (см. «ЛОВУШКА БАТЧА» в embed_texts_one_by_one).
    embedding_func = EmbeddingFunc(
        embedding_dim=EMBED_DIM,
        max_token_size=EMBED_MAX_TOKENS,
        func=embed_texts_one_by_one,
        model_name=EMBED_MODEL,
        supports_asymmetric=True,
    )

    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=llm_model_func,
        llm_model_name=LLM_MODEL,
        embedding_func=embedding_func,
        llm_model_max_async=4,
        embedding_batch_num=10,
        embedding_func_max_async=2,
        max_parallel_insert=3,
        summary_max_tokens=1200,
    )

    await rag.initialize_storages()
    await initialize_pipeline_status()

    return rag


# ---------------------------------------------------------------------------
# Журнал ингестии
# ---------------------------------------------------------------------------

def _load_ingested_log(log_path: Path) -> dict[str, str]:
    """Загружает журнал уже проингестированных файлов {путь: sha256}."""
    if log_path.exists():
        try:
            with log_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Не удалось прочитать журнал %s: %s — начинаем заново.", log_path, exc)
    return {}


def _save_ingested_log(log_path: Path, log: dict[str, str]) -> None:
    """Сохраняет журнал проингестированных файлов."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=2)


def _file_hash(content: str) -> str:
    """Возвращает SHA-256 хэш строкового содержимого файла."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Верификация хранилища
# ---------------------------------------------------------------------------

def _report_doc_statuses(working_dir: Path) -> bool:
    """
    Читает фактические статусы документов из kv_store_doc_status.json.

    Нужно потому, что `ainsert` НЕ бросает исключение, когда внутренний пайплайн LightRAG
    останавливается на ошибке хранилища: вызов возвращается штатно, и без этой проверки сводка
    отрапортует «проингестировано N/N», хотя граф на деле неполон.

    Возвращает True, если все документы в статусе processed.
    """
    status_file = working_dir / "kv_store_doc_status.json"
    if not status_file.exists():
        logger.warning("Файл статусов %s не найден — статусы проверить нечем.", status_file)
        return False

    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Не удалось прочитать %s: %s", status_file, exc)
        return False

    counts: dict[str, int] = {}
    for record in data.values():
        status = str(record.get("status", "unknown")).lower()
        counts[status] = counts.get(status, 0) + 1

    logger.info("Статусы документов: %s", counts or "пусто")
    bad = {s: c for s, c in counts.items() if s != "processed"}
    if bad:
        logger.error(
            "НЕ все документы обработаны (%s). Граф неполон — смотрите ошибки пайплайна выше.",
            bad,
        )
        return False
    return True


def _verify_storage(working_dir: Path) -> None:
    """
    Проверяет, что в рабочей директории появились непустые файлы хранилища
    (граф, векторы, kv). Выводит итоговую сводку через logging.
    """
    storage_files = list(working_dir.rglob("*"))
    nonempty = [f for f in storage_files if f.is_file() and f.stat().st_size > 0]
    total_bytes = sum(f.stat().st_size for f in nonempty)

    logger.info("=== Верификация хранилища: %s ===", working_dir)
    if nonempty:
        logger.info(
            "Найдено %d непустых файлов, суммарный объём: %.1f KB",
            len(nonempty),
            total_bytes / 1024,
        )
        for f in sorted(nonempty):
            logger.info("  %s (%.1f KB)", f.relative_to(working_dir), f.stat().st_size / 1024)
    else:
        logger.warning(
            "В рабочей директории '%s' не найдено непустых файлов хранилища. "
            "Возможно, ингестия не прошла или был dry-run.",
            working_dir,
        )


# ---------------------------------------------------------------------------
# Пайплайн ингестии
# ---------------------------------------------------------------------------

async def run_ingestion(
    input_dir: Path,
    working_dir: Path,
    limit: int | None,
    batch_size: int,
    dry_run: bool,
    force: bool,
) -> None:
    """
    Основной пайплайн ингестии документов в LightRAG.

    Параметры
    ----------
    input_dir:   директория с исходными .md-файлами
    working_dir: рабочая директория LightRAG (хранилище графа)
    limit:       ингестировать только первые N файлов (None = все)
    batch_size:  число документов за один вызов ainsert
    dry_run:     показать план без обращения к API
    force:       игнорировать журнал и переиндексировать все файлы
    """
    start_ts = time.monotonic()

    # Собираем список файлов (генератор производит .md — см. generate_synthetic_data.py)
    all_files: list[Path] = sorted(input_dir.glob("*.md"))
    if not all_files:
        logger.warning("В директории '%s' не найдено .md-файлов.", input_dir)
        return

    if limit is not None:
        all_files = all_files[:limit]

    logger.info("Найдено %d файлов в '%s'.", len(all_files), input_dir)

    log_path = working_dir / "ingested.json"

    if dry_run:
        logger.info("=== DRY-RUN — API-вызовы не выполняются ===")
        for fp in all_files:
            logger.info("  [dry-run] %s", fp.name)
        logger.info("Итого к ингестии: %d файлов.", len(all_files))
        return

    # Загружаем журнал уже проингестированных файлов
    ingested_log: dict[str, str] = {} if force else _load_ingested_log(log_path)
    if force:
        logger.info("--force: журнал ингестии игнорируется.")

    # Фильтруем файлы: пропускаем неизменившиеся
    pending: list[tuple[Path, str, str]] = []  # (path, content, sha256)
    for fp in all_files:
        try:
            content = fp.read_text(encoding="utf-8")
        except OSError as exc:
            logger.error("Не удалось прочитать '%s': %s — пропускаем.", fp, exc)
            continue
        sha = _file_hash(content)
        key = str(fp)
        if not force and ingested_log.get(key) == sha:
            logger.debug("Пропуск '%s' (уже проингестирован, хэш не изменился).", fp.name)
            continue
        pending.append((fp, content, sha))

    skipped = len(all_files) - len(pending)
    if skipped > 0:
        logger.info(
            "Пропущено %d файлов (уже в журнале). К ингестии: %d файлов.",
            skipped,
            len(pending),
        )
    else:
        logger.info("К ингестии: %d файлов.", len(pending))

    if not pending:
        logger.info("Нечего ингестировать. Завершение.")
        _verify_storage(working_dir)
        return

    # Инициализируем RAG
    logger.info("Инициализируем LightRAG (working_dir='%s')…", working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)
    rag = await create_rag(working_dir=str(working_dir))

    total_ingested = 0
    num_batches = (len(pending) + batch_size - 1) // batch_size

    try:
        for batch_idx in range(0, len(pending), batch_size):
            batch = pending[batch_idx : batch_idx + batch_size]
            batch_num = batch_idx // batch_size + 1

            texts = [item[1] for item in batch]
            file_paths = [str(item[0]) for item in batch]
            names = [item[0].name for item in batch]

            logger.info(
                "Батч %d/%d — ингестируем: %s",
                batch_num,
                num_batches,
                ", ".join(names),
            )

            try:
                await rag.ainsert(
                    texts,
                    file_paths=file_paths,
                )
                # Обновляем журнал только после успешной ингестии батча
                for fp, _content, sha in batch:
                    ingested_log[str(fp)] = sha
                _save_ingested_log(log_path, ingested_log)

                total_ingested += len(batch)
                logger.info(
                    "Батч %d/%d завершён. Всего проингестировано: %d/%d.",
                    batch_num,
                    num_batches,
                    total_ingested,
                    len(pending),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Ошибка при ингестии батча %d/%d (%s): %s — продолжаем.",
                    batch_num,
                    num_batches,
                    ", ".join(names),
                    exc,
                    exc_info=True,
                )
    finally:
        await rag.finalize_storages()

    elapsed = time.monotonic() - start_ts
    logger.info(
        "=== Итог: проингестировано %d/%d файлов за %.1f с ===",
        total_ingested,
        len(pending),
        elapsed,
    )

    _verify_storage(working_dir)
    _report_doc_statuses(working_dir)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ингестия документов из data/generated/ в LightRAG (GraphRAG).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/generated"),
        metavar="PATH",
        help="Директория с исходными .md-файлами для ингестии.",
    )
    parser.add_argument(
        "--working-dir",
        type=Path,
        default=Path("rag_storage"),
        metavar="PATH",
        help="Рабочая директория LightRAG (хранилище графа и векторов).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Ингестировать только первые N файлов (для smoke-теста).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        metavar="N",
        help="Количество документов за один вызов ainsert.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Показать список файлов к ингестии без обращения к API.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Игнорировать журнал ingested.json и переиндексировать все файлы.",
    )
    return parser


def main() -> None:
    """Точка входа CLI."""
    load_dotenv()

    parser = _build_arg_parser()
    args = parser.parse_args()

    input_dir: Path = args.input_dir
    working_dir: Path = args.working_dir
    limit: int | None = args.limit
    batch_size: int = args.batch_size
    dry_run: bool = args.dry_run
    force: bool = args.force

    if not input_dir.exists():
        logger.error("Директория '%s' не существует. Укажите корректный --input-dir.", input_dir)
        sys.exit(1)

    if batch_size < 1:
        logger.error("--batch-size должен быть >= 1.")
        sys.exit(1)

    asyncio.run(
        run_ingestion(
            input_dir=input_dir,
            working_dir=working_dir,
            limit=limit,
            batch_size=batch_size,
            dry_run=dry_run,
            force=force,
        )
    )


if __name__ == "__main__":
    main()
