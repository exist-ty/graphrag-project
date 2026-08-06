# Task: make entity identity structural, resolved during ingestion

## How to read files

You can READ files, but you cannot run shell commands — `ls`, `grep`, `find` and the like are
auto-denied in headless mode, and the entire answer is lost when that happens. Do not explore
directories; open the exact paths listed below.

Project files: `scripts/build_kg.py`, `scripts/verify_graph.py`, `scripts/disambiguate.py`,
`scripts/generate_synthetic_data.py`, `data/ground_truth.json`, `MEMORY.md`, `orchestration.md`,
`rag_storage/graph_chunk_entity_relation.graphml`.

LightRAG 1.5.5 sources (all exist, verified):
- `.venv/Lib/site-packages/lightrag/prompt.py` (767 lines) — the entity extraction prompts
- `.venv/Lib/site-packages/lightrag/operate.py` (6324 lines) — extraction and entity **merging**;
  `upsert_node(node_id, ...)` around line 2078 is where identity is decided
- `.venv/Lib/site-packages/lightrag/lightrag.py` (6061 lines) — the `LightRAG` class and its fields
- `.venv/Lib/site-packages/lightrag/addon_params.py` — user-supplied extraction parameters
- `.venv/Lib/site-packages/lightrag/base.py`, `constants.py`, `namespace.py`, `utils.py`
- `.venv/Lib/site-packages/lightrag/kg/nano_vector_db_impl.py` — the vector store

## The problem, measured rather than assumed

The corpus deliberately contains homonyms: an orbital station and a dreadnought both named
`Aurelia-Prime`, and two different ships both named `Vanguard` (a heavy strike cruiser of one house,
an assault transport of another). `scripts/verify_graph.py` reports **0 of 2 homonym groups
separated** — LightRAG merged each pair into a single node. Everything else scores 100%: entity
coverage, hierarchy coverage across 4 levels, multi-hop reach, timeline coverage.

The root cause is structural: **LightRAG uses the entity name string as the node ID**. While the key
is a name, homonyms are required to collide. Detecting and repairing collisions afterwards cannot
fix this, and the existing `scripts/disambiguate.py` demonstrates why it is the wrong layer: it
rewrites the GraphML only and never touches `vdb_entities.json`, so after it runs the graph and the
vector store disagree about which entities exist. `local` and `hybrid` retrieval consult the vector
store first, so that desync silently breaks retrieval while the report looks better.

## What to design and implement

Identity must be **correct at creation time**, so that the graph and the vector store are consistent
by construction rather than reconciled afterwards.

Two coupled changes:

1. **Structural entity keys.** A node's identity should derive from discriminating attributes — type
   and owner alongside the name (`Vanguard (Heavy Strike Cruiser, House Vance)`, or a surrogate ID
   with the display name kept as an attribute) — so that two same-named entities cannot occupy the
   same node. Decide the exact key scheme yourself; justify it.
2. **Resolution during ingestion.** The canonicalisation must happen inside the ingestion path in
   `scripts/build_kg.py`, so embeddings are computed for the resolved entity. Nothing may be left
   for a post-processing pass.

Read `prompt.py`, `addon_params.py` and `operate.py` before choosing the mechanism. The likely lever
is the extraction prompt and `addon_params` — but verify what LightRAG 1.5.5 actually supports
instead of assuming; version details matter here.

## Input contract you may rely on

A separate change (already under way) splits the ground truth into two files:

- `data/entity_registry.json` — a canonical entity registry the pipeline **may** consume: entity
  ids, canonical names, aliases, types, owners. This is legitimate master data, the analogue of a
  production MDM registry.
- `data/eval_holdout.json` — entities deliberately **withheld** from the pipeline, used only for
  evaluation.

Both share the schema of the corresponding sections in `data/ground_truth.json`. Your mechanism may
read `data/entity_registry.json`. It must **never** read `data/eval_holdout.json` or
`data/ground_truth.json` — that would make the evaluation circular, which is exactly the flaw being
corrected. Design for the registry being incomplete: entities absent from it must still be
disambiguated as well as the available evidence allows, since a production registry never covers
everything.

## No entity may be named in the mechanism

The mechanism states **rules**, never instances. Do not write the name, alias or callsign of any
specific entity into the code, the prompt text, or a lookup table — not one drawn from the corpus,
not one you inferred. Rules look like "when a name is ambiguous, qualify it with the type and owner
stated nearby in the text"; instances look like "map `X` to `Y`".

A previous attempt hardcoded the designations of the two withheld ships. Those strings do occur in
the corpus, so learning them was legitimate — writing them into the mechanism was not. It separates
that pair because it was handed the answer, which is the failure the holdout exists to expose. The
gate now rejects this automatically: `python orchestration/extract.py --gate <your output file>`
fails if any held-out name appears as a literal. Run it on your own output before answering.

Entities present in `data/entity_registry.json` may be resolved by reading that file **at runtime**.
Even then, do not paste its contents into the source.

## Hard constraints

- Dependencies: stdlib, numpy, networkx, and the already-installed `lightrag-hku`, `google-genai`,
  `python-dotenv`. Nothing new.
- **API quota is per-minute and narrow** (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` for
  `gemini-3.5-flash-lite`, `EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier` for
  `gemini-embedding-2`). A design needing hundreds of extra LLM calls is not viable. Extra calls
  folded into extraction that already happens are acceptable; a separate resolution pass over every
  entity is not.
- `scripts/build_kg.py` must keep exporting `async def create_rag(working_dir: str = "rag_storage")
  -> LightRAG`, fully initialised. `scripts/query_example.py` imports it and must keep working.
- Keep the existing rate limiting and 429 retry in `build_kg.py`; do not weaken them.
- Do not modify the corpus in `data/generated/`. Enriching the source documents is explicitly out of
  scope — it would make the task easier than the real one.
- English docstrings and comments, English identifiers, type annotations, `argparse`, `logging`.

## Success criteria

- `scripts/verify_graph.py` reports **2 of 2 homonym groups separated**.
- Alias splitting decreases; entity, hierarchy, multi-hop and timeline coverage do not regress.
- The graph and `vdb_*.json` agree on which entities exist — no post-hoc reconciliation step.
- The result holds for entities that are **not** in `data/entity_registry.json`.

## Response format

1. `## Design` — plain text, 15-30 lines: the key scheme chosen, the mechanism inside LightRAG used
   to enforce it, why this rather than the alternatives, what it costs, and what would falsify it.
   State explicitly what happens to entities absent from the registry.
2. `## Risks` — 3-6 lines: what can go wrong and how it would show up.
Write the code artifacts to files under `orchestration/runs/<your-output-dir>/` (the prompt names
the directory): the patched `build_kg.py` in full, and any new module beside it. Do not touch
`scripts/` — a competing design is being produced in parallel and must not be overwritten.

Return only the two prose sections above. The orchestrator judges designs by reading them and puts
code through a mechanical gate instead of reading it, so code in the reply is wasted output.
