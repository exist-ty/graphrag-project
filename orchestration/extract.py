"""Извлекает код из JSON-ответов делегированных вызовов agy в scripts/.

Каждый прогон (`orchestration/runs/<tag>.json`) должен содержать ровно один
fenced-блок ```python — это условие задавалось в промпте. Скрипт проверяет
status == SUCCESS, вытаскивает блок, проверяет синтаксис через ast.parse
и только потом пишет файл.
"""

from __future__ import annotations

import argparse
import ast
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
