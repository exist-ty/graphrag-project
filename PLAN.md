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
  data/ground_truth.json     # the generated source of truth (evaluation reference)
  data/entity_registry.json  # registry the pipeline may consume
  data/eval_holdout.json     # withheld entities — evaluation only, never read by the pipeline
  rag_storage/               # LightRAG working dir (graph + vectors + kv)
  scripts/
    generate_synthetic_data.py   # two-pass corpus generation
    build_kg.py                  # ingestion; exports create_rag()
    query_example.py             # query CLI / REPL
    verify_graph.py              # scores the graph against ground truth; exits 1 on failure
    split_ground_truth.py        # registry / holdout split, Phase 4 step 1
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
| Alias splitting | 15 entities |
| Ingestion | 24/24 documents, zero failures |

The pipeline captures everything except the two cases the corpus exists to test. That is the work
that remains, and Phase 4 below tracks it step by step.

## Phase 4 — entity resolution done properly

**Root cause.** LightRAG uses the entity name string as the node ID (`upsert_node(node_id, ...)`,
`operate.py:2078`). While identity is a name, homonyms are *required* to collide; no amount of
downstream repair changes that.

`scripts/disambiguate.py` was the first attempt and is superseded. It **must not be run with
`--in-place`**: it rewrites the GraphML alone and never touches `vdb_entities.json`, so afterwards
the graph and the vector store disagree about which entities exist, and `local`/`hybrid` retrieval —
which consults the vector store first — breaks silently while the report improves.

### Step 1 — split the ground truth ✔ done

`scripts/split_ground_truth.py` produces `data/entity_registry.json` (consumable by the pipeline,
the analogue of production master data) and `data/eval_holdout.json` (withheld, evaluation only).
One complete homonym group, the `Vanguard` pair, is withheld so it must be separated by the
mechanism rather than looked up; the `Aurelia-Prime` pair stays in the registry and exercises the
assisted path. Hierarchy links naming a withheld entity are dropped from the registry so its id and
owner do not leak.

### Steps 2 and 3 — resolve during ingestion, with structural keys ◐ partially done

`scripts/build_kg.py` now canonicalises names inside the ingestion path: a disambiguation rule in
the extraction prompt, plus rewriting of entity and relation tuples in `llm_model_func` before
LightRAG stores them. GraphML and `vdb_*.json` therefore receive the same ids by construction — the
thing post-processing could not achieve.

Result of the first clean rebuild (24/24 documents, zero failures, 208 nodes): coverage metrics all
hold at 100%, alias splitting improves 18 → 15, and homonyms still read **0 of 2**.

Not because nothing happened. The graph now contains qualified nodes — `VD-Vanguard-Alpha (Heavy
Strike Cruiser)`, `Valerius Vanguard (Landing Transport)`, `Aurelia-Prime Space Station`, `Aurelia
Prime Flagship` — but the bare forms survive alongside them (`Vanguard`, `Vanguard-2`, `Aurelia
Prime`, `Aurilia-Prime`) and fuzzy-match both members of a pair, which is what the check reports.
This is Risk 1 from the design's own list: the extractor still emits a short name on noisy chunks
and the resolver passes it through when it finds nothing to qualify it with.

### Step 4 — metrics that can fail ▶ next

Do this **before** touching the resolver again. The binary separated/collapsed count hid real
progress — it read 0 of 2 both when nothing worked and when half the mentions were already resolved
— so it would hide a regression just as well, and the next rebuild would be guesswork.

Replace it with precision/recall over resolved mentions, scored separately for entities in the
registry and entities withheld from it. A mechanism that only handles registry entries is a lookup,
and the split exists precisely to show that.

### Step 5 — close the resolver's fallback ▶ after step 4

The resolver must never emit an unqualified name for an entity type known to be ambiguous. With no
evidence in the chunk it should fall back to a deterministic qualifier — the chunk's dominant owner
or the entity type — rather than leaving the bare form to collide.

**Phase 4 is done when**: 2 of 2 homonym groups separated, holdout scored no worse than registry
entries, no coverage regression, and graph and `vdb_*.json` agreeing without a reconciliation pass.

Each rebuild costs ~20 minutes and a meaningful slice of the daily quota. Measure first, then change
one thing.

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
