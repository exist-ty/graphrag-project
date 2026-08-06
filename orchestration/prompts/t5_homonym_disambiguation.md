# Spec: homonym disambiguation during graph construction

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

## The problem — it is confirmed by measurement, not a hypothesis

The project builds GraphRAG using LightRAG over a synthetic corpus specifically designed to
stress-test the pain points of graph RAGs. One of them is **homonyms**: in `data/ground_truth.json`
DIFFERENT objects with the same names are intentionally introduced (marked as `is_homonym_risk`):

- the station `Aurelia-Prime` and the dreadnought `Aurelia-Prime` — different entities, different owners;
- two ships named `Vanguard` — a heavy strike cruiser and a landing transport, belonging to different houses.

Running `scripts/verify_graph.py` on the constructed graph showed a **critical failure**: both groups
collapsed into a single node each (`Aurilia-Prime`, `Vanguard-2`). That is, the entity extraction
of LightRAG merges different objects based on name match — which is exactly what the corpus was
designed to prevent.

A mirror problem occurs in the same place: the exact same entity is split across MULTIPLE nodes (canonical name
and alias as different nodes) — for example, `Empress Cassandria Aurelius` resulted in nodes `Crown Prime` and
`Lady Of The Crown Citadel`, and `Chief Architect Archon Kaelen` yielded `Mindsmith` and `Zero Core`.

## What needs to be done

Design and implement a homonym and alias disambiguation mechanism for this pipeline.
The result should be **one new file** `scripts/disambiguate.py` plus, if necessary, targeted changes
to `scripts/build_kg.py` (describe them separately, see response format).

You make the decision — this is not a "apply a ready-made recipe" task. Solve it mindfully, not
by the first method that comes to mind: before writing code, look at WHAT is actually available in the pipeline.

## What to read before designing (files are available, read them yourself)

- `data/ground_truth.json` — ground truth: entity structure, aliases, `is_homonym_risk`, hierarchy
- `scripts/build_kg.py` — how LightRAG is built, where the extension points are
- `scripts/verify_graph.py` — how the failure is measured; your work must move EXACTLY these metrics
- `rag_storage/graph_chunk_entity_relation.graphml` — real graph: node names, attributes
- `data/generated/*.md` — documents; the frontmatter contains `subject_entity_ids` with references to the
  ground truth, which is a precise mapping of the document to the ground truth reference
- `.venv/Lib/site-packages/lightrag/` — **LightRAG 1.5.5 source files**. Make sure to look at
  `prompt.py` (entity extraction prompts), `lightrag.py` (`LightRAG` parameters, in particular
  `addon_params` and how user-defined entity types are passed), `operate.py`
  (entity merging — where exactly the name-based merging happens), `utils.py`.
  Do not guess from memory: the version is specific, read the code.

## Approaches to consider (not a mandatory list, but food for thought)

1. Document enrichment: append a qualifying attribute to the text/frontmatter
   (`Vanguard (Heavy Strike Cruiser, House Vance)`) — cheap, but modifies the corpus.
2. Custom extraction prompt via `addon_params` / overriding `PROMPTS` — force the
   model to distinguish same-named objects by type and owner.
3. Graph post-processing: splitting a merged node into multiple ones based on attributes from
   `source_id`/`file_path`/descriptions, and vice versa — merging alias nodes into a single canonical one.
4. Hybrid: post-processing relying on ground truth as a synonym dictionary.

Each option has a cost: (1) distorts the corpus, making the task easier than
the real one; (2) requires extra LLM calls, whereas the free tier quota is per-minute and narrow; (3) is deterministic
and free, but works on traces rather than the core substance. **Explicitly justify your choice** — why exactly this one,
and what is lost.

## Hard constraints

- Dependencies: only stdlib + numpy + networkx + already installed `lightrag-hku`, `google-genai`,
  `python-dotenv`. Do not install anything new.
- **API Quota — per-minute and narrow** (LLM `gemini-3.5-flash-lite`, embedding `gemini-embedding-2`).
  A solution requiring hundreds of extra LLM calls is unacceptable. A deterministic offline
  solution is preferred; if LLM is still needed — justify it and minimize the number of calls.
- The script must not break the already built storage irreversibly: work on a copy or use an explicit
  `--in-place` flag, by default — dry run with a report on what will be changed.
- CLI using argparse, logging via `logging`, type annotations, comments and docstrings in English, identifiers in English.
- `python scripts/disambiguate.py --help` must work without the built graph.

## Success criteria

After applying your mechanism, a rerun of `scripts/verify_graph.py` must show:
- both groups of homonyms are SPLIT, not merged;
- the number of split aliases is reduced;
- coverage of entities and hierarchy has not degraded.

## Response format

1. First, a brief section `## Solution` — plain text, 10-20 lines: which approach is chosen,
   why, what is lost, and which metrics in `verify_graph.py` it should shift.
2. Write the complete file directly to `scripts/disambiguate.py` — do not return it in the reply.
3. If changes are needed in `scripts/build_kg.py` — follow it with a separate ```diff block containing
   a targeted diff. If no changes are needed, do not add the block.

Do not create or edit the files yourself — return everything as text in the response.

