# Spec: project documentation translation to English

The task is routine but requires precision: this is the project's working documentation, which is
used to make decisions later. The meaning must not drift.

## What to do

Translate into English the contents of **one** file whose name will be specified in the prompt
(one of `CLAUDE.md`, `MEMORY.md`, `orchestration.md` in the root of the working directory), and **write
the translation back to the same file**, replacing the content entirely.

## How to translate

- **Technical English, no literalism.** Write the way engineering documentation is written:
  clearly and to the point. Do not mirror Russian syntax.
- **Preserve the markdown structure exactly**: same headings at the same levels, same lists, tables,
  quotes, same section order. The "section-by-section" correspondence between the original and the translation
  must be preserved — the file will later be compared against git history.
- **Do NOT translate or alter a single character inside**:
  - code blocks (``` ... ```) and inline code in backticks;
  - file paths (`scripts/build_kg.py`, `.venv/Lib/site-packages/...`), file names;
  - identifiers, function, class, and constant names, JSON field names;
  - model names (`gemini-3.6-flash`, `claude-sonnet-4-6`, `gpt-oss-120b-medium`, ...);
  - CLI flag names (`--effort`, `--add-dir`, `--dangerously-skip-permissions`, ...);
  - quota metrics (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` and similar);
  - error messages quoted verbatim;
  - URLs and links.
- **Carry every number, limit, dimension, and date across unchanged.** Not a single value should
  change: `3072`, `20 запросов в сутки` → `20 requests per day`, `2026-08-06`, etc.
- Keep emphasis (`**bold**`, `*italic*`) wherever it carries importance.
- Add nothing of your own: no translator's notes, no new sections, no "improved" phrasing of
  substance. Do not drop anything, even if it seems redundant.

## Special instructions for specific files

- `CLAUDE.md` — these are instructions for the Claude Code agent. Maintain the imperative mood:
  directives must remain directives ("follow", "do not", "record"), rather than turning into
  descriptions.
- `MEMORY.md` — the file of facts about the project state. Maintain the assertiveness of formulations: if the
  original says "verified empirically" or "do NOT run", it must remain just as
  unambiguous (`verified empirically`, `do NOT run`).
- `orchestration.md` — delegation rules. Preserve the model table exactly as a table, with the same
  number of rows and columns.

## Result

Write the translated text back to the same file, replacing the content entirely. Do not touch anything
else: do not modify other project files. In response, return a single line — the name of the processed file and
the resulting line count. Do not output the translated text itself in the response.
