## General Project Context (verified empirically, DO NOT extrapolate)

Project: GraphRAG on LightRAG + Google AI Studio (Gemini). Root: `D:\Projects\graphrag-project`.
Scripts are run from the project root as `python scripts/<name>.py`. OS is Windows, but the code must
be cross-platform (pathlib, no hardcoded backslashes).

Environment (already installed, nothing can be added):
- Python 3.12 venv in `.venv/`
- `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2, numpy
- DO NOT use external dependencies beyond this list (argparse instead of click).

Secrets: `.env` in the project root contains `GEMINI_API_KEY=...`. Load via
`dotenv.load_dotenv()`. Never log or hardcode the key.

Models (IDs verified by live API calls on this account):
- LLM: `gemini-3.6-flash`
- Embedding: `gemini-embedding-2`, native dimension 3072 (measured by calling `embedContent`).

## Response Requirements

- Write the complete file directly to `scripts/build_kg.py`. Do not return code in the reply: it is put through a mechanical gate, not read, so code in the response is wasted output.
- The file must run as is, without placeholders or TODOs.
- Docstrings and comments in English, code/identifiers in English.
- Mandatory: type annotations, `if __name__ == "__main__":`.
- Do not invent API: use exactly the signatures given below.

## Task: write `scripts/build_kg.py`

Pipeline to ingest documents from `data/generated/` into LightRAG.

### Verified LightRAG 1.5.5 signatures (read from the package sources — use exactly these)

```python
from lightrag import LightRAG, QueryParam
from lightrag.utils import EmbeddingFunc
from lightrag.llm.gemini import gemini_complete_if_cache, gemini_embed
from lightrag.kg.shared_storage import initialize_pipeline_status
```

- `EmbeddingFunc` — a dataclass with fields: `embedding_dim` (required), `func` (required),
  `max_token_size=None`, `send_dimensions=False`, `model_name=None`, `supports_asymmetric=False`.
- DOUBLE WRAPPING TRAP: `gemini_embed` is ALREADY decorated with
  `@wrap_embedding_func_with_attrs(embedding_dim=1536, model_name="gemini-embedding-001")`.
  If you wrap it directly using `functools.partial(gemini_embed, ...)`, the inner wrapper
  will override the settings and the dimension will reset to 1536. You need to use `gemini_embed.func`:
  `partial(gemini_embed.func, model="gemini-embedding-2")`. This is explicitly documented in the docstring
  of `EmbeddingFunc` — reproduce this as a comment in the code so the trap does not return.
- Signature: `gemini_embed(texts, model=..., base_url=None, api_key=None, embedding_dim=None,
  max_token_size=None, task_type=None, timeout=None, token_tracker=None, context="document")`.
  `embedding_dim` is passed to `output_dimensionality` and injected by the wrapper automatically —
  DO NOT pass it manually in the partial. `supports_asymmetric=True` provides the correct
  `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY` task_types for indexing and querying respectively — enable it.
- Signature: `gemini_complete_if_cache(model, prompt, system_prompt=None, history_messages=None,
  ...)`. The LLM function for LightRAG must look like
  `async def llm_model_func(prompt, system_prompt=None, history_messages=None, **kwargs) -> str`
  and call `gemini_complete_if_cache("gemini-3.6-flash", prompt, ...)`. DO NOT blindly
  forward everything from `kwargs` — remove internal LightRAG keys like `hashing_kv` if they
  conflict; preserve the forwarding of `response_format` / `keyword_extraction` / `entity_extraction`.
- `LightRAG` fields relevant here (with defaults): `working_dir="./rag_storage"`,
  `embedding_func=None`, `llm_model_func=None`, `llm_model_name="gpt-4o-mini"` (MUST
  be overridden to `gemini-3.6-flash`), `llm_model_max_async=4`, `embedding_batch_num=10`,
  `embedding_func_max_async=8`, `max_parallel_insert=3`, `embedding_token_limit=None`,
  `summary_max_tokens=1200`.
- Initialization MUST be in this exact order:
  `rag = LightRAG(...)` → `await rag.initialize_storages()` → `await initialize_pipeline_status()`.
  Skipping the second step is a common reason for pipeline freezes. At the end — `await rag.finalize_storages()`
  (this method exists), inside a `try/finally` block.
- `await rag.ainsert(input, split_by_character=None, split_by_character_only=False, ids=None,
  file_paths=None, track_id=None)` — accepts `str | list[str]`. Pass `file_paths` along with the
  content so that sources are visible in the graph.
- The embedding model `gemini-embedding-2` accepts up to 8192 tokens input → `max_token_size=8192`,
  `embedding_dim=3072`.

### Script Requirements

- EXPORT `async def create_rag(working_dir: str = "rag_storage") -> LightRAG` — a factory
  that assembles the LLM function, `EmbeddingFunc`, and a fully initialized LightRAG instance
  (with `initialize_storages()` and `initialize_pipeline_status()` already called inside).
  This function is imported by the adjacent script `query_example.py`, so its name and signature are a
  fixed contract and must not be changed.
- CLI (argparse): `--input-dir` (defaults to `data/generated`), `--working-dir` (defaults to
  `rag_storage`), `--limit N` (ingest only the first N files — for smoke testing), `--batch-size`
  (number of documents per `ainsert` batch, defaults to 10), `--dry-run` (show what will be
  ingested without calling the API), `--force`.
- Idempotency: maintain a log of already ingested documents (`rag_storage/ingested.json`
  with content hashes) and by default skip unchanged ones; `--force` ignores the log.
- Logging via `logging` (not print): number of files found, number of files ingested,
  progress by batch, and total elapsed time. A failure in one batch must not crash the entire run.
- At the end — a brief summary, including verification that non-empty storage files
  (graph / vectors / kv) actually appeared in `working_dir`: this is a verification step from the project plan.
