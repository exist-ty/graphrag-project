## General Project Context (verified empirically, DO NOT extrapolate)

Project: GraphRAG on LightRAG + Google AI Studio (Gemini). Root: `D:\Projects\graphrag-project`.
Scripts are run from the project root as `python scripts/<name>.py`. OS is Windows, but the code must
be cross-platform (pathlib, no hardcoded backslashes).

Environment (already installed, nothing can be added):
- Python 3.12 venv in `.venv/`
- `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2, numpy
- DO NOT use external dependencies beyond this list (argparse instead of click).

Project models: LLM `gemini-3.6-flash`, embedding `gemini-embedding-2` (dimension 3072).
But they do not need to be configured in THIS file — see the contract below.

## Response Requirements

- Return EXACTLY ONE ```python block with the complete file contents. No text before or after the block.
- The file must run as is, without placeholders or TODOs.
- Docstrings and comments in English, code/identifiers in English.
- Mandatory: type annotations, `if __name__ == "__main__":`.
- Do not invent API: use exactly the signatures given below.

## Task: write `scripts/query_example.py`

A minimal CLI wrapper around the ready LightRAG graph — for pipeline smoke testing.

### Fixed Contract with the Adjacent Module

The adjacent script `scripts/build_kg.py` (written in parallel by another model) exports:

```python
async def create_rag(working_dir: str = "rag_storage") -> LightRAG: ...
```

It returns an ALREADY initialized instance (with `initialize_storages()` and
`initialize_pipeline_status()` already done inside). `query_example.py` must reuse it instead of duplicating
the LLM function and embedding function assembly:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))  # to make importing the neighbor work
from build_kg import create_rag
```

There should be no other model configuration in this file — the single source of truth
for models and dimension lives in `build_kg.py`.

### Verified Signatures (read from package sources)

- `await rag.aquery(query: str, param: QueryParam = ..., system_prompt: str | None = None) -> str | AsyncIterator[str]`
- `QueryParam.mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass"]`, defaults to `"mix"`.
- Other fields of `QueryParam` with defaults: `top_k=40`, `chunk_top_k=20`, `stream=False`,
  `response_type="Multiple Paragraphs"`, `enable_rerank=True`, `include_references=False`,
  `only_need_context=False`.
- IMPORTANT: `enable_rerank=True` is the default, but a reranker is NOT configured in this project.
  Set `enable_rerank=False` explicitly, otherwise warnings and redundant code paths may occur.
- Termination: `await rag.finalize_storages()` inside a `try/finally` block.

### CLI (argparse)

- Positional argument `question` (optional).
- `--mode` with choice from the listed literals, defaults to `hybrid`.
- `--all-modes` — run the same question through `local`, `global`, and `hybrid` modes and print responses
  side-by-side or sequentially with headers. This is exactly what is needed for the project verification step (2-3 test queries
  in different modes, checking against `ground_truth.json`).
- `--working-dir` (defaults to `rag_storage`), `--top-k`, `--only-context` (return retrieved
  context without generating a response — useful to understand exactly WHAT the retriever found).
- If `question` is not provided — interactive REPL: read questions from stdin in a loop, exit with `exit`/`quit`/
  Ctrl-D. The graph is NOT reinitialized between questions.
- `--questions-file <path>` — run a batch of questions from a file (one question per line).

### What is critical

- Output is human-readable: a header indicating the mode and response time, followed by the text.
- A clear error message if `working_dir` is empty or does not exist: suggest running
  `python scripts/build_kg.py` first instead of crashing with a traceback.
- No freezes: handle KeyboardInterrupt gracefully.
