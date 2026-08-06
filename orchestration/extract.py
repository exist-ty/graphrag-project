"""Извлекает код из JSON-ответов делегированных вызовов agy в scripts/.

Каждый прогон (`orchestration/runs/<tag>.json`) должен содержать ровно один
fenced-блок ```python — это условие задавалось в промпте. Скрипт проверяет
status == SUCCESS, вытаскивает блок, проверяет синтаксис через ast.parse
и только потом пишет файл.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "orchestration" / "runs"
SCRIPTS = ROOT / "scripts"

# tag -> имя итогового файла в scripts/
TARGETS = {
    "t1": "generate_synthetic_data.py",
    "t2": "build_kg.py",
    "t3": "query_example.py",
    "t4": "verify_graph.py",
}

FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)

# Запрещённые конструкции: если найдены — результат брак независимо от остального.
FORBIDDEN: list[tuple[str, str]] = [
    ("старый SDK google.generativeai (в проекте установлен google-genai)", r"google\.generativeai"),
    ("несуществующий genai.AsyncClient", r"genai\.AsyncClient"),
    ("устаревший параметр generation_config= вместо config=", r"generation_config\s*="),
]

# Механический шлюз: дешёвые проверки, отсеивающие явный брак ДО того, как код попадёт
# в контекст оркестратора на чтение. Каждая запись — (описание, регулярка-требование).
# Порядок ревью должен быть: шлюз -> делегированное ревью -> собственное чтение по указанным
# местам. Обратный порядок дорог и, как показала практика, наименее урожаен.
CONTRACTS: dict[str, list[tuple[str, str]]] = {
    "build_kg.py": [
        ("экспортирует фабрику create_rag", r"async def create_rag\("),
        ("ищет .md, а не .txt", r'glob\(\s*["\']\*\.md["\']'),
        ("обходит двойную обёртку gemini_embed", r"gemini_embed\.func"),
        ("вызывает initialize_pipeline_status", r"initialize_pipeline_status\(\)"),
    ],
    "query_example.py": [
        ("переиспользует create_rag из build_kg", r"from build_kg import create_rag"),
        ("отключает нескорфигурированный реранкер", r"enable_rerank\s*=\s*False"),
    ],
    "generate_synthetic_data.py": [
        ("использует новый SDK google-genai", r"from google import genai"),
    ],
}


def run_gate(path: Path, code: str) -> bool:
    """
    Прогоняет механические проверки: компиляция, разрешимость импортов, контракт файла.

    Смысл — отклонить заведомо нерабочий результат делегата, не читая его целиком.
    """
    ok = True

    # 1. Импорты верхнего уровня: самая частая ошибка делегата — писать под другую версию SDK.
    tree = ast.parse(code)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module.split(".")[0])
    for mod in sorted(modules):
        if importlib.util.find_spec(mod) is None:
            print(f"    ✗ импорт не разрешается: {mod!r} — модуль не установлен")
            ok = False

    # 2. Запрещённые конструкции.
    for description, pattern in FORBIDDEN:
        if re.search(pattern, code):
            print(f"    ✗ запрещённая конструкция: {description}")
            ok = False

    # 3. Контракт: то, о чём оркестратор договаривался в промпте.
    for description, pattern in CONTRACTS.get(path.name, []):
        if not re.search(pattern, code, re.M):
            print(f"    ✗ нарушен контракт: {description}")
            ok = False

    return ok


def extract(tag: str, target: str) -> bool:
    run = RUNS / f"{tag}.json"
    if not run.exists() or not run.stat().st_size:
        print(f"[{tag}] нет вывода: {run}")
        return False

    payload = json.loads(run.read_text(encoding="utf-8"))
    if payload.get("status") != "SUCCESS":
        print(f"[{tag}] status={payload.get('status')} error={payload.get('error')!r}")
        return False

    blocks = FENCE.findall(payload.get("response") or "")
    if not blocks:
        print(f"[{tag}] в ответе нет fenced-блока с кодом")
        return False
    if len(blocks) > 1:
        # Берём самый длинный: модель могла добавить мелкие иллюстративные вставки.
        print(f"[{tag}] найдено {len(blocks)} блоков, беру самый длинный")
    code = max(blocks, key=len).strip() + "\n"

    try:
        ast.parse(code)
    except SyntaxError as exc:
        print(f"[{tag}] СИНТАКСИЧЕСКАЯ ОШИБКА в сгенерированном коде: {exc}")
        (RUNS / f"{tag}.rejected.py").write_text(code, encoding="utf-8")
        return False

    out = SCRIPTS / target
    if not run_gate(out, code):
        print(f"[{tag}] ШЛЮЗ НЕ ПРОЙДЕН — файл не записан, читать его целиком не нужно")
        (RUNS / f"{tag}.rejected.py").write_text(code, encoding="utf-8")
        return False

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(code, encoding="utf-8")
    print(f"[{tag}] -> {out.relative_to(ROOT)} ({len(code)} байт, {code.count(chr(10))} строк)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tags", nargs="*", default=list(TARGETS), help="какие прогоны извлечь")
    args = parser.parse_args()

    ok = True
    for tag in args.tags or TARGETS:
        if tag not in TARGETS:
            print(f"неизвестный тег {tag!r}, известные: {', '.join(TARGETS)}")
            ok = False
            continue
        ok &= extract(tag, TARGETS[tag])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
