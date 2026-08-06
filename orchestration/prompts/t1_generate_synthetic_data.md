## General Project Context (verified empirically, DO NOT extrapolate)

Project: GraphRAG on LightRAG + Google AI Studio (Gemini). Root: `D:\Projects\graphrag-project`.
Scripts are run from the project root as `python scripts/<name>.py`. OS is Windows, but the code must
be cross-platform (pathlib, no hardcoded backslashes).

Environment (already installed, nothing can be added):
- Python 3.12 venv in `.venv/`
- `lightrag-hku` 1.5.5, `google-genai` 2.16.0, `python-dotenv` 1.2.2, numpy
- DO NOT use external dependencies beyond this list (no rich, click, tenacity;
  argparse instead of click, custom backoff instead of tenacity).

Secrets: `.env` in the project root contains `GEMINI_API_KEY=...`. Load via
`dotenv.load_dotenv()`. Never log or hardcode the key.

Models (IDs verified by live API calls on this account):
- LLM: `gemini-3.6-flash`
- Embedding: `gemini-embedding-2`, native dimension 3072.

Synthetic data topic: Aurelia Sector — a cyber-feudal interstellar empire (houses, megacorporations,
colonies, fleets). The data is specifically designed to stress complex aspects of GraphRAG:
aliases, homonyms, contradictions between sources, changes of facts over time, nested hierarchies,
multi-hop relationships, sparse nodes.

## Verified `google-genai` 2.16.0 API (read from introspection of the installed package)

This is the NEW SDK. The `google.generativeai` package (old SDK) is NOT present in the environment — importing from it will
immediately crash the script. Neither `genai.configure()`, nor `genai.AsyncClient()`, nor
`genai.exceptions.RateLimitError` exists. Use exactly this:

```python
from google import genai
from google.genai import errors, types

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

# асинхронный вызов идёт через client.aio:
response = await client.aio.models.generate_content(
    model="gemini-3.6-flash",
    contents=[prompt],                      # str или список частей
    config=types.GenerateContentConfig(     # именно `config=`, НЕ `generation_config=`
        response_mime_type="application/json",
        response_json_schema=SCHEMA,
    ),
)
text: str = response.text
```

Error handling: the exception hierarchy is `errors.APIError` (base, has `.code` and
`.message` fields), from which `errors.ClientError` (4xx, 429 falls here) and
`errors.ServerError` (5xx) inherit. You should retry `errors.ServerError` entirely and `errors.ClientError`
only when `exc.code == 429`; other 4xx errors indicate a bad request, retrying is pointless, such
a document should be considered failed and you should proceed.

## Response Requirements

- Return EXACTLY ONE ```python block with the complete file contents. No text before or after the block.
- The file must run as is, without placeholders or TODOs.
- Docstrings and comments in English, code/identifiers in English.
- Mandatory: type annotations, `if __name__ == "__main__":`, API error handling
  (429/5xx → exponential backoff with asyncio.sleep, custom implementation).
- Do not invent API: use exactly the signatures given below.

## Task: write `scripts/generate_synthetic_data.py`

A two-pass synthetic corpus generator, using the `google-genai` SDK directly.

### Pass 1 — seed (ground truth)

A single LLM call generates a single source of truth and saves it to `data/ground_truth.json`:
a timeline of events (each with `event_id`, year, participants), an entity registry — houses,
megacorporations, persons (with a canonical name AND a list of aliases/titles/callsigns), stations and
ships (there must intentionally be homonyms: different objects with the same or almost
the same name), and a hierarchy of ownership/subordination (nested, at least 3 levels).

Use structured output:
`types.GenerateContentConfig(response_mime_type="application/json", response_json_schema=SCHEMA)`.
Declare the schema in the code as an explicit dict — it also serves to document the format.

If `data/ground_truth.json` already exists — reuse it and DO NOT spend quota unless the
`--regenerate-seed` flag is passed.

### Pass 2 — divergent (documents)

Separate `.md` documents are generated from timeline/registry elements by DIFFERENT biased
narrators. Each narrator references the same ground truth but distorts it in their own way —
this is how controlled contradictions are created, rather than random noise. Narrator types and shares:

- `encyclopedia` — official imperial encyclopedia, 30%
- `dossier` — spy dossier, 25%
- `propaganda` — news/propaganda of opposing sides, 25%
- `log` — ship/station logs and decrees, 20%

Each document is a separate file in `data/generated/` with YAML-frontmatter so that verification can
be traced back to the truth: `doc_id`, `narrator`, `subject_entity_ids` (list),
`source_event_ids` (list), `generated_at`. The file name is deterministic and sortable, for example
`0007_dossier_kesh-station.md`.

### CLI (argparse)

- `--count N` (defaults to 24 — this is a smoke test; the full volume ~1000 is run explicitly)
- `--concurrency N` (defaults to 4) — asyncio.Semaphore
- `--out-dir` (defaults to `data/generated`), `--ground-truth` (defaults to `data/ground_truth.json`)
- `--seed-only` — pass 1 only
- `--regenerate-seed` — regenerate ground truth, overwriting the existing one
- `--overwrite` — overwrite already existing documents (by default they are skipped)

### What is critical

- The batch must not fail entirely due to a single API failure: the failed document is logged and
  skipped, and a summary is printed at the end (created / skipped / failed).
- Prompts to the LLM must pass a relevant slice of the ground truth (specific events and
  entities) to the narrator, rather than the entire JSON — otherwise the context gets bloated and the model loses focus.
- **Quota savings**: the document plan (index → narrator → which entities and events it covers)
  is built BEFORE calling the API, and the "file already exists" check is ALSO done before the API call.
  Calling the LLM and then finding out the file is already on disk is unacceptable.
- **Frontmatter is written by code, not the model.** Only the markdown document body is requested from the LLM;
  YAML-frontmatter is attached programmatically from values already known to the plan. Explicitly instruct the
  model not to add its own frontmatter, and just in case, strip the leading `---` block from the response
  if it still appears — otherwise there will be two frontmatters in the file and parsing will break.
- `doc_id` — deterministic (e.g., from index and narrator), NOT a random UUID: on
  subsequent runs with `--overwrite`, the same document must get the same id.
- The narrator shares from the table above are a layout of a fixed plan (for `--count 24` →
  roughly 7/6/6/5), not an independent random choice for each document. For reproducibility,
  use `random.Random(seed)` with a fixed seed when selecting entities/events.
- The file name must be made from a safe slug (only `[a-z0-9-]`), with limited length; do not
  insert raw `entity_id` directly into the file name.
- At the end, print the path to the directory and the **actual** distribution by narrator (calculated from
  actually created files), not the expected percentages from the constant.
