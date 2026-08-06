"""Gates code produced by delegated `agy` runs before it is trusted.

Two modes:

* `--gate <path>` — the normal one. The agent wrote the file itself; this checks it in place.
* `<tag>` — legacy. Pulls a fenced block out of `orchestration/runs/<tag>.json` and gates it
  before writing. Kept only for runs made under the old specs, which asked agents to return code
  in the reply. Do not write new specs that way: code in a response body is wasted output, since
  it is gated rather than read, and it risks reaching the orchestrator's context.

Either way the gate is the same: syntax, resolvability of every top-level import, forbidden
constructs, and the contract the spec promised. Failing output is never read in full.
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


def check_holdout_leak(code: str) -> list[str]:
    """
    Find held-out entity names or ids hardcoded into the code.

    The holdout exists so a mechanism has to *derive* a discriminator rather than be handed one.
    Naming a withheld entity in the source turns entity resolution into a lookup and makes the
    evaluation meaningless — while every other check still passes, which is why this one is
    mechanical. Legitimate uses read `data/entity_registry.json` at runtime; none need a literal.
    """
    holdout = ROOT / "data" / "eval_holdout.json"
    if not holdout.exists():
        return []
    data = json.loads(holdout.read_text(encoding="utf-8"))
    terms: set[str] = set(data.get("withheld_entity_ids", []))
    for section in ("houses", "megacorporations", "persons", "stations_and_ships"):
        for entity in data.get(section, []):
            terms.add(entity["name"])
            terms.update(entity.get("aliases") or [])
            terms.update(entity.get("callsigns") or [])
    # Single short tokens produce noise; a discriminating literal is longer than that.
    return sorted(t for t in terms if t and len(t) >= 5 and t in code)


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
        # A sibling module in the same directory counts as resolvable: scripts add their own
        # directory to sys.path at runtime, so find_spec alone reports a false failure.
        if (path.parent / f"{mod}.py").exists():
            continue
        try:
            resolved = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            resolved = False
        if not resolved:
            print(f"    ✗ import does not resolve: {mod!r} — not installed and not a sibling module")
            ok = False

    # 2. Запрещённые конструкции.
    for description, pattern in FORBIDDEN:
        if re.search(pattern, code):
            print(f"    ✗ запрещённая конструкция: {description}")
            ok = False

    # 3. Held-out entities must not appear as literals — see check_holdout_leak.
    for term in check_holdout_leak(code):
        print(f"    ✗ held-out entity hardcoded: {term!r} — the mechanism must derive it, not be told")
        ok = False

    # 4. Контракт: то, о чём оркестратор договаривался в промпте.
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


def gate_existing(path: Path) -> bool:
    """Gate a file a delegated agent wrote directly — the normal path."""
    if not path.exists():
        print(f"[gate] file not found: {path}")
        return False
    code = path.read_text(encoding="utf-8")
    try:
        ast.parse(code)
    except SyntaxError as exc:
        print(f"[gate] {path.name}: SYNTAX ERROR: {exc}")
        return False
    if not run_gate(path, code):
        print(f"[gate] {path.name}: GATE FAILED — do not read it in full, send it back")
        return False
    print(f"[gate] {path.name}: OK ({code.count(chr(10))} lines)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tags", nargs="*", default=list(TARGETS), help="legacy: runs to extract")
    parser.add_argument("--gate", type=Path, action="append", default=[],
                        help="gate a file the agent wrote directly (normal mode)")
    args = parser.parse_args()

    if args.gate:
        return 0 if all(gate_existing(p) for p in args.gate) else 1

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
