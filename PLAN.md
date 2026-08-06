# GraphRAG on LightRAG + Google AI Studio (Gemini)

Current state of the world lives in `MEMORY.md`; delegation mechanics in `orchestration.md`.
This file is the forward plan and is revised when the plan itself changes.

## Context

A pet-project GraphRAG system on LightRAG (entities, relationships, edge cases), with LLM-driven
extraction into a graph and a separate embedding model, both through one Google AI Studio API key.
There are no real documents, so the corpus is synthetic and deliberately built to stress the hard
parts of graph RAG: aliases, homonyms, contradictions between sources, facts that change over time,
nested hierarchies, multi-hop links, sparse nodes.

Everything runs locally: LightRAG keeps the graph and vectors in files (NetworkX + nano-vectordb) in
the working directory, and the only outbound traffic is HTTPS to the Gemini API.

**Aurelia Sector** — a cyber-feudal interstellar empire (houses, megacorporations, colonies,
fleets). Generation method: fix a single ground truth (timeline + entity registry) first, then have
**biased narrators** write about it. Contradictions are then controlled rather than random noise.

Models in use:
- Extraction LLM: **`gemini-3.5-flash-lite`**. The original choice, `gemini-3.6-flash`, has a
  free-tier ceiling of 20 requests **per day**, which cannot carry ingestion — LightRAG issues
  several LLM calls per chunk. Quota is metered per model, so the lite model draws a separate,
  per-minute budget.
- Embedding: **`gemini-embedding-2`**, dimension **3072** (measured, not assumed). Unchanged.

The API key lives only in `.env`.

## Project structure

```
graphrag-project/
  data/generated/            # synthetic .md corpus (24 documents)
  data/ground_truth.json     # the generated source of truth
  rag_storage/               # LightRAG working dir (graph + vectors + kv)
  scripts/
    generate_synthetic_data.py   # two-pass corpus generation
    build_kg.py                  # ingestion; exports create_rag()
    query_example.py             # query CLI / REPL
    verify_graph.py              # scores the graph against ground truth; exits 1 on failure
    disambiguate.py              # post-hoc homonym repair — superseded, see Phase 4
  orchestration/
    prompts/                 # task specs handed to delegated agents
    extract.py               # mechanical gate on delegated code
```

## Where the pipeline stands

All three original scripts are built and the corpus is ingested. Verification against ground truth:

| Metric | Result |
|---|---|
| Entity coverage | 100% (20/20) |
| Hierarchy coverage | 100% (11/11), depth 4 |
| Multi-hop reach | 100% (6/6) |
| Timeline coverage | 100% (8/8) |
| **Homonym groups separated** | **0 of 2** |
| Alias splitting | 18 entities |

The pipeline captures everything except the two cases the corpus exists to test. That is the work
that remains.

## Phase 4 — entity resolution done properly

**Root cause.** LightRAG uses the entity name string as the node ID (`upsert_node(node_id, ...)`,
`operate.py:2078`). While identity is a name, homonyms are *required* to collide; no amount of
downstream repair changes that.

`scripts/disambiguate.py` was a first attempt and is kept only as a reference point. It is not the
solution and **must not be run with `--in-place`**: it rewrites the GraphML alone and never touches
`vdb_entities.json`, so afterwards the graph and the vector store disagree about which entities
exist. `local` and `hybrid` retrieval consult the vector store first, so that desync breaks
retrieval silently while the report improves.

Four steps, in this order:

1. **Split the ground truth.** `data/entity_registry.json` — canonical entity registry the pipeline
   may consume, the analogue of production master data. `data/eval_holdout.json` — entities withheld
   from the pipeline and used only for scoring. Fixing the graph with the same file it is then
   scored against is circular; this split makes the supervision legitimate.
2. **Resolve during ingestion.** Canonicalisation belongs in the ingestion path, so embeddings are
   computed for the resolved entity and the two stores are consistent by construction rather than
   reconciled afterwards.
3. **Make identity structural.** Derive the node key from discriminating attributes — type and owner
   beside the name — so two same-named entities cannot occupy one node *by construction*. The lever
   is the extraction prompt via `addon_params` / `PROMPTS`.
4. **Metrics that can fail.** Score entities absent from the registry, and report precision/recall of
   resolution rather than a count of collapsed groups. A mechanism that only recognises names it was
   given is a lookup, not entity resolution.

Steps 2 and 3 are one change, not two.

**Done when**: `verify_graph.py` reports 2 of 2 homonym groups separated, alias splitting drops, no
other coverage regresses, graph and `vdb_*.json` agree without a reconciliation pass, and the result
holds for entities missing from the registry.

## Constraints that shape everything

- **Free-tier quota is the binding constraint.** Both limits are per-minute
  (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier`,
  `EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier`), which throttling handles;
  the per-day ceiling on top-tier models does not. Any design needing hundreds of extra LLM calls is
  not viable here.
- **The ~1000-document corpus from the original plan is out of reach on the free tier** and is not
  the bottleneck anyway: 24 documents already expose the failure worth fixing. Revisit only after
  Phase 4, and only if billing is enabled.
- **Do not enrich the corpus to make extraction easier.** Writing qualifiers into the source
  documents would solve the benchmark instead of the problem.

## Deferred

The primary query interface (built-in `lightrag-server` with web UI / an MCP wrapper over
`LightRAG.query()` / plain CLI) — deferred by the user. `query_example.py` is enough for smoke
testing. Do not build any of it without an explicit request.
