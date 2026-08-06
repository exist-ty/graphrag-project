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
import sys
import time
from functools import partial
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from lightrag import LightRAG
from lightrag.kg.shared_storage import initialize_pipeline_status
from lightrag.llm.gemini import gemini_complete_if_cache, gemini_embed
from lightrag.utils import EmbeddingFunc

# ---------------------------------------------------------------------------
# Константы моделей
# ---------------------------------------------------------------------------
LLM_MODEL = "gemini-3.6-flash"
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
    return await gemini_complete_if_cache(
        LLM_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        **safe_kwargs,
    )


# ---------------------------------------------------------------------------
# Фабрика RAG
# ---------------------------------------------------------------------------

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

    # Сырая функция без декоратора — см. «ЛОВУШКА ДВОЙНОЙ ОБЁРТКИ» выше
    raw_embed_func = partial(
        gemini_embed.func,          # type: ignore[attr-defined]
        model=EMBED_MODEL,
        api_key=api_key,
    )

    embedding_func = EmbeddingFunc(
        embedding_dim=EMBED_DIM,
        max_token_size=EMBED_MAX_TOKENS,
        func=raw_embed_func,
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
        embedding_func_max_async=8,
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
