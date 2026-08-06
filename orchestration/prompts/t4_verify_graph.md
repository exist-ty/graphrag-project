# Task for a strong model with large context

You are writing a single Python file: `scripts/verify_graph.py` — automated verification of the constructed
LightRAG knowledge graph against the "ground truth" from which the corpus was generated. Below is EVERYTHING you need:
real project data, real storage formats (retrieved via introspection, not memory), and the real
code of the adjacent module. Do not invent APIs — everything you need to know is in this prompt.

## Response Requirements

- Write the complete file directly to `scripts/verify_graph.py`. Do not return code in the reply: it is put through a mechanical gate, not read, so code in the response is wasted output.
- No file operations or tool calls — simply return the code as text.
- The file must run as is. Dependencies: stdlib + numpy + networkx only (networkx is already
  installed as a dependency of lightrag). DO NOT use LLMs and DO NOT access the network: verification must be
  deterministic, free, and instant.
- Docstrings and comments in English, code/identifiers in English. Type annotations.

## Project Context

GraphRAG on LightRAG + Gemini. The synthetic corpus about the cyber-feudal Aurelia Sector empire
has been generated in two passes: first, a single "ground truth" (entity registry + timeline), then
24 documents from DIFFERENT biased narrators (encyclopedia / dossier / propaganda / log) that
intentionally distort the same facts. The corpus is specifically designed for the challenging parts of GraphRAG:
aliases, HOMONYMS, contradictions between sources, change of facts over time, nested hierarchies,
multi-hop relationships.

Goal of the task: to understand **which parts of the embedded complexity the graph actually retained, and what was lost**.
This is a verification step that was described in the project plan as manual visual verification — it needs to be
automated.

## Data that needs to be read directly

The project working directory is accessible to you — read the files directly, they are real:

- `data/ground_truth.json` — complete entity registry and timeline (this is the ground truth)
- `rag_storage/graph_chunk_entity_relation.graphml` — constructed graph
- `data/generated/*.md` — 24 corpus documents with frontmatter
- `scripts/build_kg.py` — adjacent module, for style and pipeline understanding
- `rag_storage/kv_store_full_entities.json`, `kv_store_doc_status.json` — auxiliary storages

Read them before designing metrics: the ground truth structure and real node names in the
graph determine how mapping should be done.

## Graph Format: `rag_storage/graph_chunk_entity_relation.graphml`

Read via `networkx.read_graphml(path)`. This is an undirected multigraph of entities.
Verified structure (extracted from a real file):

- **The node ID is the entity name string itself**, for example `id="Great Imperial Hegemony"`.
- Node attributes: `entity_id` (duplicates the name), `entity_type` (e.g., `organization`, `person`,
  `category`), `description`, `source_id` (chunk ID of the form
  `doc-<hash>-chunk-000`), `file_path` (source .md file name), `created_at`, `truncate`.
- Edge attributes: `weight` (float), `description`, `keywords` (comma-separated, e.g.,
  `blesses,rules`), `source_id`, `file_path`, `created_at`, `truncate`.

Current scale for reference: 74 nodes, 87 edges for 24 documents.

**Important**: names in the graph DO NOT match the canonical names from the ground truth word-for-word. We can already see
in the real data: in the ground truth it's `Empress Cassandria Aurelius`, in the graph it is `Cassandra Aurelius`;
in the ground truth it is `Aurelia-Prime`, in the graph `Aurilia-Prime` appears. This means mapping must
be robust to spelling variations rather than requiring strict equality.

## Other Storage Files (might be useful)

- `rag_storage/kv_store_full_entities.json` — dictionary `doc-<hash>` → `{"entity_names": [...], ...}`
- `rag_storage/kv_store_doc_status.json` — document statuses, field `status` (`processed`, etc.)
- `rag_storage/kv_store_full_relations.json`, `kv_store_text_chunks.json`, `vdb_*.json`

## Example of a Generated Document

Take any file from `data/generated/*.md`, e.g., `0020_dossier_vanguard.md`.

The frontmatter of each document contains `doc_id`, `narrator`, `subject_entity_ids` (entity IDs from the
ground truth), and `source_event_ids`. This means **the connection between the document and ground truth can be tracked precisely**,
and you should take advantage of this.

## Adjacent Module

`scripts/build_kg.py` — read it for code style and pipeline understanding.

## What exactly `verify_graph.py` should check

Design the metrics yourself, but make sure to cover the following — these are the failures that the
corpus was built to detect:

1. **Entity coverage.** How many ground truth entities (houses, corporations, persons, stations, and
   ships) actually found reflection in the graph. Matching must be by canonical name AND by all
   aliases/titles/callsigns, with resilience to spelling variations (normalization of case and
   punctuation + fuzzy matching via `difflib.SequenceMatcher`; choose a threshold and define it as a
   constant). List **not found** entities separately — this is the most valuable part of the report.

2. **Homonym merging — the main check.** The ground truth intentionally contains different objects with the
   same name (two different `Vanguard`s, two different `Aurelia-Prime`s, marked with
   `is_homonym_risk`). A classic GraphRAG failure is collapsing them into a SINGLE node. Determine for each
   homonym group whether it is represented in the graph by a single node (merged — bad) or multiple
   distinguishable ones (good), and show this explicitly.

3. **Aliases resolution in reverse.** The opposite issue: the same entity split into MULTIPLE
   graph nodes (canonical + alias as separate nodes, e.g., `Cassandra Aurelius` and `Crown Prime`).
   Find such splits.

4. **Relationships and hierarchy coverage.** Ground truth hierarchy (`hierarchy`: `parent_id` → `child_id`,
   with a `level` field) — check if these relationships reach the graph: is there an edge (direct or path)
   between the nodes corresponding to parent and child. Separately calculate how many levels of the hierarchy
   are actually tracked.

5. **Multi-hop connectivity.** For pairs of entities connected in the ground truth via an intermediate link,
   verify reachability in the graph and the shortest path length. List sparse/isolated nodes in a
   separate list.

6. **Timeline events coverage.** For each ground truth event — whether it is mentioned in the graph
   (by participants and edge keywords).

7. **Narrators' contribution.** Using the `file_path` attribute of nodes/edges, calculate how many nodes and edges each
   narrator type contributed (encyclopedia / dossier / propaganda / log). If some type contributed almost
   nothing — this is a sign that its style is poorly processed by entity extraction.

### CLI and Output

- argparse: `--working-dir` (defaults to `rag_storage`), `--ground-truth` (defaults to
  `data/ground_truth.json`), `--docs-dir` (defaults to `data/generated`),
  `--json <path>` (export the complete report in a machine-readable format), `--fuzzy-threshold`,
  `--verbose` (print all matches rather than just problems).
- To stdout — a human-readable report by section, with a summary at the beginning or end:
  coverage percentages and an explicit list of what was lost.
- Clear error if the graph is missing: suggest running `python scripts/build_kg.py` first.
- Return code: 0 if all checks pass their thresholds, 1 if there are failures — so that the script can be used
  as a gate in automation.

The report must be detailed enough to make a DECISION: scale up generation to
the full volume or fix extraction first. Not just "coverage 62%", but exactly what was lost.
