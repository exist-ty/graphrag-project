# Task: translate delegation specs to English

## How to read files

You can READ files, but you cannot run shell commands — `ls`, `grep`, `find` and the like are
auto-denied in headless mode, and your whole answer is lost when that happens. Do not explore
directories with commands; open the exact paths listed in the prompt.

## What to do

Each file named in the prompt lives in `orchestration/prompts/` and is currently written in Russian.
Translate its contents into English and **write the translation back into the same file**, replacing
the content entirely.

These files are task specifications handed to AI agents. They are read, not admired: precision
matters far more than elegance.

## How to translate

- **Technical English, no literalism.** Write the way engineering specs are written: direct,
  unambiguous, imperative where the original is imperative. Do not mirror Russian syntax.
- **Preserve the markdown structure exactly**: same headings at the same levels, same lists, tables,
  code blocks, same section order. The result is diffed against git history.
- **Do not translate or alter a single character inside**:
  - fenced code blocks and inline code in backticks;
  - file paths (`scripts/build_kg.py`, `.venv/Lib/site-packages/...`) and file names;
  - identifiers, function, class and constant names, JSON field names;
  - model names (`gemini-3.6-flash`, `claude-sonnet-4-6`, `gpt-oss-120b-medium`, ...);
  - CLI flags (`--effort`, `--add-dir`, `--dangerously-skip-permissions`, ...);
  - quota metric names (`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` and similar);
  - error messages quoted verbatim;
  - URLs.
- **Carry every number across unchanged**: limits, dimensions, counts, dates, percentages. `3072`
  stays `3072`; `20 запросов в сутки` becomes `20 requests per day`; `2026-08-06` stays as is.
- **Keep emphasis** (`**bold**`, `*italic*`) wherever it marks something as critical. Several of
  these specs use bold to flag traps that already cost a failed run — that weight must survive.
- **Add nothing, drop nothing.** No translator's notes, no new sections, no "improved" wording, no
  trimming of anything that looks redundant.

## One substantive change to make

Some specs instruct the agent to write Russian docstrings and comments (typically phrased as
"комментарии и докстринги на русском, идентификаторы на английском"). Change that instruction to
require **English** docstrings and comments, keeping identifiers in English as before. This is the
one place where you change meaning rather than preserving it; everything else is a faithful
translation.

## Result

Write each translation back into its own file, replacing the content entirely. Do not touch any
other file in the project — not the scripts, not the documentation, not files you were not asked
about. Reply with one line per file: the file name and the resulting line count. Do not print the
translated text.
