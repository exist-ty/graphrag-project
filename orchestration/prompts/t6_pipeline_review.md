# Spec: end-to-end GraphRAG pipeline review

## IMPORTANT: how to read files

You can READ files, but you cannot execute shell commands — running `ls`, `grep`, `find`, etc.
will be automatically denied, and your entire response will be lost. Therefore, do not explore directories
using commands: open the files listed below directly by their paths.

Exact paths to the LightRAG 1.5.5 source files (all exist, verified):

- `.venv/Lib/site-packages/lightrag/prompt.py` (767 lines) — entity extraction prompts
- `.venv/Lib/site-packages/lightrag/operate.py` (6324 lines) — extraction and MERGING of entities
- `.venv/Lib/site-packages/lightrag/lightrag.py` (6061 lines) — LightRAG class and its parameters
- `.venv/Lib/site-packages/lightrag/addon_params.py` — custom extraction parameters
- `.venv/Lib/site-packages/lightrag/utils.py` — EmbeddingFunc, wrap_embedding_func_with_attrs
- `.venv/Lib/site-packages/lightrag/base.py`, `constants.py`, `namespace.py`
- `.venv/Lib/site-packages/lightrag/llm/gemini.py` — Gemini binding
- `.venv/Lib/site-packages/lightrag/kg/shared_storage.py`
- `.venv/Lib/site-packages/lightrag/kg/nano_vector_db_impl.py`

Project files (in the root of the working directory): `data/ground_truth.json`, `scripts/build_kg.py`,
`scripts/verify_graph.py`, `scripts/generate_synthetic_data.py`, `scripts/query_example.py`,
`MEMORY.md`, `rag_storage/graph_chunk_entity_relation.graphml`.
Corpus documents: `data/generated/0001_propaganda_cygnus-prime-station.md` and others with prefixes
`0001`..`0024` (24 files).

You are an independent reviewer. Task: find REAL defects in the project code by verifying it not against general
conceptions, but against the source code of the installed libraries. Do not fix anything — only find
and justify.

## What to review

- `scripts/generate_synthetic_data.py` — synthetic corpus generator (Gemini API directly)
- `scripts/build_kg.py` — ingestion into LightRAG
- `scripts/query_example.py` — CLI on top of the built graph
- `scripts/verify_graph.py` — graph verification against `data/ground_truth.json`

## What to compare against (read them yourself, files are available)

- `.venv/Lib/site-packages/lightrag/` — **LightRAG 1.5.5 source files**: `lightrag.py`, `operate.py`,
  `utils.py` (containing `EmbeddingFunc` and `wrap_embedding_func_with_attrs`), `llm/gemini.py`,
  `kg/shared_storage.py`, `kg/nano_vector_db_impl.py`.
- `.venv/Lib/site-packages/google/genai/` — SDK `google-genai` 2.16.0 source files.
- `data/ground_truth.json`, `data/generated/*.md` — real data.
- `rag_storage/` — real storage (might be incomplete, ingestion was running with quota errors).
- `MEMORY.md` — accumulated facts about the project: confirmed pitfalls, quota limits, decisions
  made. Be sure to read it to avoid "finding" already known and accounted for issues.

## What to look for first

1. **Discrepancies with the actual library API** — signatures, initialization order, side effects
   of decorators, parameters that are silently ignored.
2. **Errors that do NOT raise an exception** but silently corrupt data: incorrect vector
   dimensionality, loss of part of a batch, overwriting, desynchronization between log and actual state.
3. **Concurrency**: race conditions for shared state, semaphores and rate limiters, errors in
   `asyncio.gather`, swallowed exceptions, incorrect behavior on partial failure.
4. **API failure handling**: retries that catch the wrong exception class; retries where they
   are pointless; absence of retries where they are necessary.
5. **Consistency between scripts**: `create_rag` contract, file formats, names and extensions,
   assumptions of one script about the output of another.
6. **Metrics correctness in `verify_graph.py`** — this file is especially important: if it MEASURES INCORRECTLY,
   all conclusions about the graph quality are false. Verify fuzzy matching logic, detection of homonym
   merging and alias splitting, hierarchy traversal, multi-hop counting.

## What NOT to do

- Do not suggest stylistic edits, renaming, or reorganization for the sake of beauty.
- Do not describe what the code does.
- Do not report what is already described in `MEMORY.md` as known and deliberately accepted.
- Do not suggest new dependencies.

## Response format

Text only, no code patches. For each finding strictly:

```
### <краткое название>
Файл: <path>:<строка>
Серьёзность: критическая | высокая | средняя
Что не так: <одно-два предложения>
Как проявится: <конкретный сценарий: входные данные/состояние -> неверный результат или падение>
Доказательство: <ссылка на конкретное место в исходниках библиотеки или на факт из данных>
```

Sort by severity, the most dangerous first. If there are no findings — say so, do not make them up.
At the end — section `## Итог` of 5-10 lines: where the pipeline is most fragile and what to fix first.
