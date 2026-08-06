# MEMORY — current project context

Last update: 2026-08-06

## Current status

- The project was created in `D:\projects\graphrag-project\`. Already present on disk: `.venv` (Python 3.12,
  `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2), `.env` with `GEMINI_API_KEY`,
  `.gitignore` (ignores `.env`, `.venv/`, `rag_storage/`, `data/generated/`).
- All **3 scripts are written** and committed (delegated to different `agy` models, see below):
  `scripts/generate_synthetic_data.py`, `scripts/build_kg.py`, `scripts/query_example.py`.
- `data/ground_truth.json` — generated and verified: 3 houses, 3 megacorporations, 8 personas (all with
  aliases/titles/callsigns), 6 stations and ships with **real homonyms** (`Aurelia-Prime` ×2,
  `Vanguard` ×2), 3-level deep hierarchy (level 2→4), 8 timeline events.
- `orchestration.md`, `CLAUDE.md`, `MEMORY.md` — created on 2026-08-06.
- Progress on PLAN.md: code is ready; "Verification" step — seed pass is complete, next on the list is
  small batch generation → `build_kg.py` → queries in different modes.

## Delegation: project-specific outcomes

Specs live in `orchestration/prompts/`, raw replies in `orchestration/runs/` (gitignored), and
`orchestration/extract.py` gates every result before it reaches `scripts/`. Method is in
`orchestration.md`; only this project's outcomes are recorded here.

- Routing used: `build_kg.py` → `claude-sonnet-4-6`; `generate_synthetic_data.py`,
  `query_example.py`, `verify_graph.py`, `disambiguate.py` → `gemini-3.1-pro-high` /
  `gemini-3.5-flash-high`; documentation translation → `gemini-3.5-flash-high`.
- Fixed by hand after delegation: `build_kg.py` globbed `*.txt` while the generator emits `*.md`;
  `disambiguate.py` had `args.in-place` / `args.output-graph`, which parse as subtraction.
- `verify_graph.py` counted a collapsed parent/child pair as covered hierarchy — the metric was
  inverted, so heavier collapsing scored better. Found by a delegated review of the model's own
  earlier output.

## Git

- Remote: **https://github.com/exist-ty/graphrag-project** (branch `main`).
- Repository initialized locally on 2026-08-06 (`git init -b main`), the first commit contains only
  documentation. `gh` CLI **is not installed** on the machine — work via standard `git`, not `gh`.
- Under gitignore: `.env`, `.venv/`, `rag_storage/`, `data/generated/`, `pip_install.log`,
  `orchestration/runs/` (raw JSON responses of delegated `agy` calls).
- Make commits as you go, do not accumulate them.

## Verified technical facts

- Google AI Studio models are verified as available on the account via a direct `GET
  /v1beta/models` call: `gemini-3.6-flash` (LLM for entity/relation extraction),
  `gemini-embedding-2` (embedding).
- `embedding_dim` for `gemini-embedding-2` **verified empirically** (on 2026-08-06, direct
  `embedContent` call): native dimensionality is **3072**; `output_dimensionality` is also supported,
  768 / 1536 / 3072 were verified — all return exactly the requested length. The project locks in **3072**.
  The blocking step from PLAN.md section 2 is closed.
- **Double-wrapping trap in LightRAG**: `lightrag.llm.gemini.gemini_embed` is already decorated with
  `@wrap_embedding_func_with_attrs(embedding_dim=1536, model_name="gemini-embedding-001")`. You must wrap
  `gemini_embed.func` (`partial(gemini_embed.func, model="gemini-embedding-2")`), otherwise the inner wrapper
  will override the settings and the dimensionality will silently drop to 1536.
- **BATCH TRAP OF `gemini-embedding-2`** (found on 2026-08-06 during the first real ingestion):
  when given MULTIPLE texts as input, the model returns EXACTLY ONE vector — silently, without throwing an error.
  Verified with direct `embed_content` calls on 1/2/4 texts: in all cases,
  `len(response.embeddings) == 1`. The default binding `lightrag.llm.gemini.gemini_embed` sends the entire
  batch in a single request and expects N vectors, causing ingestion to crash with
  `IndexFlushError: NanoVectorDBStorage[entities] ... Vector count mismatch: expected 4 vectors
  but got 1`. The solution in `build_kg.py` is the `embed_texts_one_by_one` wrapper: the batch is unpacked into
  separate requests (semaphore=4) and stacked via `np.vstack`. The danger of this trap is that without
  verifying the vector count, incorrect embeddings would have ended up in the index.
- **`ainsert` does not throw an exception when the internal LightRAG pipeline stops.** In a failed run,
  the script reported "ingested 3/3" even though vector indexes (`vdb_entities.json`,
  `vdb_relationships.json`, `vdb_chunks.json`) were not created at all.
  Added `_report_doc_statuses()` function: it reads `kv_store_doc_status.json` and raises an error if at
  least one document is not in the `processed` status. A sign of successful ingestion is the presence of non-empty
  `vdb_*.json` files, not the lack of an exception.
- LightRAG 1.5.5 initialization order: `LightRAG(...)` → `await rag.initialize_storages()` →
  `await initialize_pipeline_status()` (from `lightrag.kg.shared_storage`); teardown is
  `await rag.finalize_storages()`. Skipping `initialize_pipeline_status()` is a common cause of freezes.
- `QueryParam.mode` allows `local|global|hybrid|naive|mix|bypass` (default `mix`), and
  `enable_rerank` defaults to `True` — the reranker is not configured in the project, so set `False` explicitly.
- Delegation mechanics for `agy` (models, flags, failure modes, review order) live in
  `orchestration.md` — deliberately not duplicated here.

## API QUOTA — the main project limitation

Discovered on 2026-08-06 during a generation smoke run (24 documents): the key operates on the **free tier**, and
the limit there is **20 requests per DAY per model** (`generate_content_free_tier_requests`,
`quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier`, `quotaValue: 20`). Not per minute.

- Actual result of the `--count 24` run: **7** documents created, **17** failed with 429
  RESOURCE_EXHAUSTED. The daily quota for `gemini-3.6-flash` on 2026-08-06 is exhausted.
- The quota is calculated **per model**, so different models have independent budgets.
  `gemini-3.5-flash-lite` verified with a live call — works, quota is available.
  `gemini-2.5-flash-lite` — 404, no longer available to new users.
- Retry on 429 in `generate_synthetic_data.py` is pointless with a **daily** quota: backoff only
  stretches the failure. If we remain on the free tier, 429 should be treated as "stop for today",
  rather than a reason to retry.
- **Impact on PLAN.md**: the full volume of ~1000 documents is fundamentally unattainable on the free tier
  (this would take 50 days at 20 requests per day). Moreover, `build_kg.py` will also hit a wall: LightRAG makes
  several LLM calls for EACH chunk during entity and relation extraction, so even ingesting
  the existing 7 documents does not fit into 20 requests.
- The embedding quota is separate from generation (different metric) — a run of 5 calls on
  `gemini-embedding-2` completed without errors.

### Quota clarification (important): daily limit is only for top-tier models

- `gemini-3.6-flash`: **daily** limit, 20 requests/day — resolved only by switching models or billing.
- `gemini-3.5-flash-lite`: **per-minute** — `GenerateRequestsPerMinutePerProjectPerModel-FreeTier`.
- `gemini-embedding-2`: **per-minute** — `EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier`,
  stated limit is 100/min, but 429s were received even when throttling to 80/min.

Per-minute limits are resolved by waiting, so the `RateLimiter` class (sliding window of 60s) has been
added to `build_kg.py` for BOTH call paths: LLM (12/min) and embedding (45/min), plus a custom
retry on 429 reading `retryDelay` from the error body. A semaphore does not solve this problem: it limits
concurrency, not frequency.

**Custom retry is mandatory**: default `@retry` in LightRAG bindings catch
`google.api_core.exceptions.ResourceExhausted`, while the new SDK throws `google.genai.errors.ClientError` —
which decorators do not see. A single unhandled 429 drops the ENTIRE document to the `failed` status.

**`--force` is not suitable for re-ingestion**: LightRAG rejects re-inserting the same filenames
with the error `File name already exists. Original doc_id: ...`. For a clean rebuild, you must start with
an empty `working_dir`, rather than relying on `--force`.

### Graph verification: `scripts/verify_graph.py`

Written by `gemini-3.1-pro-high` according to the file-based task description (`orchestration/prompts/t4_verify_graph.md`), 957 lines,
deterministic, no LLM and no network — can be run freely. Verifies: entity coverage
(fuzzy matching by canonical name + aliases), merging of homonyms, reverse splitting of aliases,
hierarchy coverage and depth, multi-hop reachability, timeline coverage, and narrator contributions.
Exit code 1 on failure — suitable as a gate.

**The first run (on an incomplete graph, 4 out of 24 documents) already caught the main failure**: both homonym
groups collapsed — the station and the dreadnought `Aurelia-Prime` merged into a single `Aurilia-Prime` node, while
two different `Vanguard` ships merged into `Vanguard-2`. In addition, 5 entities split into multiple nodes
(canonical name and alias as separate nodes). Coverage numbers are preliminary, repeat after full ingestion.

## Phase 4 attempt 1 — ingestion-time resolution, partial result

`scripts/build_kg.py` now canonicalises entity names inside the ingestion path (extraction prompt
rule + rewriting of entity/relation tuples in `llm_model_func` before LightRAG stores them), so
GraphML and `vdb_*.json` receive the same ids by construction. Previous version kept at
`orchestration/runs/build_kg.pre-t9c.py`, previous storage at `rag_storage.pre-t9c/`.

First full clean rebuild: **24/24 documents processed, zero failures**, 1160 s, 208 nodes.

Verification after the change:

| Metric | Before | After |
|---|---|---|
| Entity / hierarchy / multi-hop / timeline coverage | 100% | 100% |
| Homonym groups separated | 0/2 | 0/2 |
| Alias splitting | 18 | 15 |

**Why homonyms still read as collapsed.** The mechanism does produce qualified nodes —
`VD-Vanguard-Alpha (Heavy Strike Cruiser)`, `Valerius Vanguard (Landing Transport)`,
`Aurelia-Prime Space Station`, `Aurelia Prime Flagship`. It does not *displace* the unqualified
ones: `Vanguard`, `Vanguard-2`, `Aurelia Prime`, `Aurilia-Prime` also exist and fuzzy-match both
members of a pair, which is what the check reports. This is the design's own Risk 1 — the extractor
still emits a bare short name on noisy chunks.

Consequences for the next attempt:

- The resolver passes a bare name through when it finds no evidence. It must never emit an
  unqualified name for an entity type known to be ambiguous; with no evidence it should fall back to
  a deterministic qualifier (chunk's dominant owner/type) rather than leaving the bare form.
- The binary "separated / collapsed" metric hides real progress and should become precision/recall
  over resolved mentions — this is step 4 of Phase 4 and is now worth doing before another rebuild,
  so the next run is measured rather than guessed at.
- A rebuild costs ~20 minutes and a meaningful slice of daily quota. Do not iterate blindly.

## Open questions / risks

- Query interface (built-in `lightrag-server` with Web UI / MCP wrapper / simple CLI) —
  the choice is deferred by the user for later; do not start without an explicit request.
- Do not run the full volume of synthetic generation (~1000 documents) before passing verification on a
  small batch (~20-30 documents) — API quota consumption is proportional to the volume.
- Ingestion (`build_kg.py`) has not yet **been run** on the real corpus — the combination of `EmbeddingFunc` +
  `gemini_embed.func` + `ainsert` has only been verified by code review. Running it now is pointless:
  the daily LLM quota is exhausted (see the quota section), the run will crash on entity extraction.
  This is the next pending verification step — after a decision on the quota is made.
- **Quota decision made by the user on 2026-08-06: generative LLM switched to
  `gemini-3.5-flash-lite`** (it has a separate daily budget). `LLM_MODEL` in
  `build_kg.py` and `MODEL_NAME` in `generate_synthetic_data.py` have been changed.
  **Embedding model has NOT been changed**: `gemini-embedding-2` — a special embedding model with its own
  separate quota, remains as is (3072 dimensions).
- Side effect: the first 7 documents of the corpus were generated by `gemini-3.6-flash`, the rest —
  by the lite model. For the project goals, this is rather a plus (stylistic variation enhances the heterogeneity
  of the sources), but it is worth keeping in mind.
- Open: the quality of entity extraction with the lite model is lower — if the graph ends up sparse,
  the first suspect is the model, not the `build_kg.py` code.
- `orchestration.md` is out of sync with reality in some places (`--effort` for `claude-sonnet-4-6`, conclusion about
  cache) — the fixes are listed above in "Verified technical facts", but the document itself has not yet been updated.

## Session log

- **2026-08-06**: discussed and verified `agy` CLI as an executor for routine delegation
  (headless calls, models, options) → captured in `orchestration.md`. Created
  project-level `CLAUDE.md` with links to `PLAN.md`/`orchestration.md` and rules for maintaining this
  file. The actual development of the project (scripts, generation, ingestion) has not yet begun.
