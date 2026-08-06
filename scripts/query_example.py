"""Минимальный CLI поверх готового графа LightRAG для смоук-теста конвейера."""

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import List

# Добавляем родительский каталог скриптов в sys.path для импорта build_kg
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_kg import create_rag
from lightrag import LightRAG, QueryParam


def parse_args() -> argparse.Namespace:
    """Парсинг аргументов командной строки."""
    parser = argparse.ArgumentParser(
        description="CLI интерфейс для выполнения запросов к графу знаний LightRAG."
    )
    parser.add_argument(
        "question",
        nargs="?",
        default=None,
        type=str,
        help="Текст вопроса для RAG (если не указан, запускается интерактивный REPL).",
    )
    parser.add_argument(
        "--mode",
        choices=["local", "global", "hybrid", "naive", "mix", "bypass"],
        default="hybrid",
        help="Режим поиска (по умолчанию: hybrid).",
    )
    parser.add_argument(
        "--all-modes",
        action="store_true",
        help="Выполнить запрос последовательно в режимах local, global, hybrid.",
    )
    parser.add_argument(
        "--working-dir",
        default="rag_storage",
        type=str,
        help="Директория с сохранёнными хранилищами RAG (по умолчанию: rag_storage).",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=40,
        help="Количество извлекаемых контекстов top_k (по умолчанию: 40).",
    )
    parser.add_argument(
        "--only-context",
        action="store_true",
        help="Вернуть только извлечённый контекст без генерации ответа LLM.",
    )
    parser.add_argument(
        "--questions-file",
        type=str,
        default=None,
        help="Путь к файлу с вопросами (по одному вопросу на строку).",
    )
    return parser.parse_args()


def validate_working_dir(working_dir: str) -> Path:
    """Проверяет существование и непустоту рабочей директории RAG."""
    dir_path = Path(working_dir).resolve()
    if not dir_path.exists() or not any(dir_path.iterdir()):
        print(
            f"Ошибка: Директория '{working_dir}' не существует или пуста.\n"
            f"Сначала запустите сборку графа знаний: python scripts/build_kg.py",
            file=sys.stderr,
        )
        sys.exit(1)
    return dir_path


async def execute_single_query(
    rag: LightRAG,
    question: str,
    mode: str,
    top_k: int,
    only_context: bool,
) -> None:
    """Выполняет один запрос к LightRAG и выводит результат с таймингом."""
    query_param = QueryParam(
        mode=mode,
        top_k=top_k,
        enable_rerank=False,
        only_need_context=only_context,
    )

    start_time = time.perf_counter()
    response = await rag.aquery(question, param=query_param)
    elapsed = time.perf_counter() - start_time

    if isinstance(response, str):
        result_text = response
    else:
        chunks: List[str] = []
        async for chunk in response:
            chunks.append(chunk)
        result_text = "".join(chunks)

    print("=" * 60)
    print(f"РЕЖИМ: {mode} | ВРЕМЯ: {elapsed:.2f} сек.")
    print("=" * 60)
    print(result_text)
    print()


async def process_questions(
    rag: LightRAG,
    questions: List[str],
    modes: List[str],
    top_k: int,
    only_context: bool,
) -> None:
    """Обрабатывает список вопросов для заданных режимов."""
    for q_idx, question in enumerate(questions, 1):
        if len(questions) > 1:
            print(f"\n>>> ВОПРОС [{q_idx}/{len(questions)}]: {question}\n")
        for mode in modes:
            await execute_single_query(
                rag=rag,
                question=question,
                mode=mode,
                top_k=top_k,
                only_context=only_context,
            )


async def run_repl(
    rag: LightRAG,
    modes: List[str],
    top_k: int,
    only_context: bool,
) -> None:
    """Запускает интерактивный REPL цикл обработки вопросов."""
    print("=== Интерактивный режим LightRAG CLI ===")
    print("Введите вопрос или 'exit'/'quit' для завершения (Ctrl+C / Ctrl+D).\n")

    while True:
        try:
            user_input = input("[Query]> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nЗавершение работы REPL...")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit"):
            print("Выход из REPL...")
            break

        try:
            for mode in modes:
                await execute_single_query(
                    rag=rag,
                    question=user_input,
                    mode=mode,
                    top_k=top_k,
                    only_context=only_context,
                )
        except KeyboardInterrupt:
            print("\nВыполнение запроса прервано пользователем.")


async def main() -> None:
    """Основная асинхронная точка входа."""
    args = parse_args()
    validate_working_dir(args.working_dir)

    modes = ["local", "global", "hybrid"] if args.all_modes else [args.mode]

    print(f"Инициализация LightRAG из '{args.working_dir}'...")
    rag = await create_rag(working_dir=args.working_dir)

    try:
        if args.question:
            await process_questions(
                rag=rag,
                questions=[args.question],
                modes=modes,
                top_k=args.top_k,
                only_context=args.only_context,
            )
        elif args.questions_file:
            q_file = Path(args.questions_file)
            if not q_file.exists():
                print(
                    f"Ошибка: Файл с вопросами не найден: '{args.questions_file}'",
                    file=sys.stderr,
                )
                sys.exit(1)

            with open(q_file, "r", encoding="utf-8") as f:
                questions = [
                    line.strip()
                    for line in f
                    if line.strip() and not line.strip().startswith("#")
                ]

            if not questions:
                print(
                    f"Предупреждение: Файл '{args.questions_file}' не содержит вопросов."
                )
                return

            await process_questions(
                rag=rag,
                questions=questions,
                modes=modes,
                top_k=args.top_k,
                only_context=args.only_context,
            )
        else:
            await run_repl(
                rag=rag,
                modes=modes,
                top_k=args.top_k,
                only_context=args.only_context,
            )
    finally:
        print("Завершение работы хранилищ LightRAG...")
        await rag.finalize_storages()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПрограмма прервана пользователем.")
        sys.exit(0)
