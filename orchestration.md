# Orchestration via agy CLI

A plan for using the local `agy.exe` (Antigravity CLI, v1.1.10, found at `D:\.gemini\agy\agy.exe`,
available in PATH as `agy`) as an external executor that I (Claude Code) call via the
`Bash` tool to delegate routine subtasks in the `graphrag-project` project.

Everything below was verified using live `agy` calls on 2026-08-06, rather than blindly taken from `--help`.

## Models

`agy models`:

| Model | Class | When to use in this project |
|---|---|---|
| `claude-sonnet-4-6` | strong, expensive | code review of the LightRAG pipeline, debugging of non-obvious bugs, final check before adding to PLAN.md |
| `claude-opus-4-6-thinking` | strongest, most expensive | architectural decisions (e.g., choice of LightRAG query mode, `ground_truth.json` schema) — rarely |
| `gpt-oss-120b-medium` | cheap/fast | mass routine: generation of synthetic document templates, simple scripts, parsing, format checks |
| `gemini-3.6-flash-*` (high/medium/low) | cheap, fast | same as gpt-oss, plus appropriate when you want a model from the same family as the LLM itself in the pipeline (`gemini-3.6-flash`), to keep the style of synthetic texts consistent |
| `gemini-3.1-pro-high` | strong, large context | **main workhorse for large tasks**: code that needs to be written while keeping a lot of heterogeneous context in mind (data schema + storage formats + adjacent modules), designing metrics and algorithms, analyzing large logs. Especially good with a file-based task description and `--add-dir` — reads what is needed on its own |
| `gemini-3.5-flash-*`, `gemini-3.1-pro-low` | backup options | if the main model is unavailable or the quota is exhausted |

Confirmed by a smoke test (`--print "Reply with exactly one word: pong"`): `gpt-oss-120b-medium` and
`claude-sonnet-4-6` reply correctly in ~3 seconds.

**Important note on call cost**: even a trivial prompt drags in ~20-27k input tokens
of system context; on real delegation prompts, it is 29-46k (measured empirically). This means
aggregating small requests into a single larger prompt is more cost-effective than sending many small
`agy --print`.

**Inter-call caching does exist after all** (clarified on 2026-08-06 during real runs): for gemini models,
`cache_read_tokens` reached up to 77-86k, whereas for `claude-sonnet-4-6` and `gpt-oss-120b-medium` it
remained `0`. The earlier observation that "caching is not confirmed" applied only to trivial
test prompts and should not be generalized to production runs.

**Gemini models behave agentically even on purely text tasks.** For instance, `gemini-3.6-flash-high` on a task
to "return a single block of code" tried to use a tool that requires the `command` permission; headless mode
cannot prompt the user and auto-declined the call. Result: `status: SUCCESS`, `output_tokens`
12390 — and an **empty `response`**. The real reason is only visible in stderr, it is not present in the JSON. From this follow two
rules: (1) always read stderr, not just the `status`; (2) an empty `response` with a non-zero
`output_tokens` is a suppressed tool call, not "the model said nothing". Furthermore, the
agent managed to write the result directly to the project file — a delegated call can modify the
working tree even **without** `--dangerously-skip-permissions`, so checking `git status` after a run
is mandatory.

## Orchestration Parameters

Flags that are actually important for delegation (for the full list, see `agy --help` / below):

- `--print` / `-p` / `--prompt` — headless mode, the only reasonable way to call `agy` from
  my automation (without it, the CLI opens an interactive TUI).
- `--model <name>` — model selection from the table above. Mandatory for each call — otherwise, the
  CLI default is used, which is not guaranteed to match what is needed for the task.
- `--output-format json` — structured response of the form
  `{"conversation_id","status","response","duration_seconds","num_turns","usage":{...}}`.
  Use instead of text output wherever the result is parsed programmatically (rather than read by a
  human) — this makes it easier to catch `status != "SUCCESS"`.
- `--json-schema <строка-или-путь-к-файлу>` — forced JSON schema for the final result
  (works with `stream-json`). Useful when a machine-readable result of a specific form is needed —
  for example, a batch of synthetic documents as a JSON array.
- `--effort low|medium|high` — reasoning depth control. For routine tasks (template generation,
  formatting) — `low`; for code reviews/debugging — `medium`/`high`.
  **Not supported by all models.** `claude-sonnet-4-6` does not have this flag: the call immediately fails with
  `invalid model selection (--model "claude-sonnet-4-6" --effort "high"): --effort is not supported`,
  `status: ERROR`, zero tokens spent (verified 2026-08-06). For gemini models, the depth is already
  baked into the name (`-high`/`-medium`/`-low`), so `--effort` does not need to be passed to them either —
  practically, the flag is relevant only for `gpt-oss-120b-medium`.
- `--conversation <id>` / `--continue` (`-c`) — session continuation. Confirmed: a second call with
  `--conversation <id из первого ответа>` actually sees the context of the first one (tested on
  secret code — the model recalled the value). Useful for multi-step tasks assigned to the same
  sub-agent, but input tokens accumulate linearly (in the test: 20.7k → 41.5k on the second step) —
  not keeping the session open longer than actually needed.
- `--add-dir <path>` (repeatable) — extends the agent's working directory beyond the default one.
  Use to explicitly restrict `agy` only to the necessary project paths
  (`--add-dir D:\projects\graphrag-project`), rather than granting access to the entire disk.
- `--mode accept-edits|plan` — `plan` for tasks like "propose changes, do not apply";
  `accept-edits` when we delegate actual file editing and trust the result (after code review).
- `--dangerously-skip-permissions` — disables confirmation prompts for each tool call.
  **Do not use by default.** Appropriate only for fully autonomous runs with a narrow
  `--add-dir` and low risk (for example, generating synthetic `.md` files in `data/generated/`),
  and only after explicit user consent for that specific run.
- `--sandbox` — terminal sandbox constraints for the agent. Use together with
  `--dangerously-skip-permissions` if we still proceed with an autonomous run — reduces the risk of the
  agent accidentally touching something outside the task scope.
- `--print-timeout` (default 5m) — increase for long batch generations (for example, the complete
  batch of ~1000 synthetic documents from PLAN.md), otherwise the process will abort due to a timeout.
- `--project` / `--new-project` — state isolation between unrelated tasks; can be omitted for one-off
  calls initiated by me (each `--print` without `--conversation` starts fresh anyway).
- `agy agent` / `agy agents` — list of configured named agents; at the time of writing this
  plan, it is empty (`Available agents:` with no entries). This means there is currently nothing to use `--agent <name>` for —
  delegation is done directly via `--model`, without pre-installed roles.
- `agy plugin list` — no plugins are connected (`No imported plugins.`).

## JSON Response Schema (`--output-format json`)

```json
{
  "conversation_id": "uuid",
  "status": "SUCCESS",
  "response": "текст ответа",
  "duration_seconds": 3.02,
  "num_turns": 1,
  "usage": {
    "input_tokens": 20699,
    "output_tokens": 59,
    "thinking_tokens": 0,
    "cache_read_tokens": 0,
    "total_tokens": 20758
  }
}
```

Check `status == "SUCCESS"` before using `response`; during programmatic parsing, read
`usage` to monitor the budget if it is a mass run.

## How this integrates into my (Claude Code) work on the project

I call `agy` via the `Bash` tool as a regular subprocess — this is not an `Agent` sub-agent
(Claude Code sub-agents are only models of the Claude family via my internal `Agent` tool), but
an external CLI whose stdout/JSON I read and use as the result of a delegated subtask.

Practical scenarios for this project (see `PLAN.md`):

1. **Generation of synthetic document templates** (`data/generated/*.md`) — can be parallelized
   in batches via `gpt-oss-120b-medium` or `gemini-3.6-flash-low` with `--effort low`, each call being an
   independent `--print` without `--conversation` (no context accumulation). I validate the result myself
   before considering the "Verification" step from PLAN.md as completed.
2. **Review of `scripts/build_kg.py` before running** — one call to `claude-sonnet-4-6` with `--effort
   medium`, `--mode plan` (without applying edits), `--add-dir D:\projects\graphrag-project` — as
   an independent second pair of eyes before I deploy the code myself.
3. **Architectural choices** (selection of `query_example.py` mode, `ground_truth.json` schema) —
   `claude-opus-4-6-thinking`, `--effort high`, rarely and on a specific issue, not as a routine step.

Autonomous (`--dangerously-skip-permissions`) runs — only by separate consent for each
specific batch run, not as a default mode.

## The golden rule of code delegation (verified on a run on 2026-08-06)

**The quality of the delegated code is determined not by the model choice, but by whether the verified API
is fixed in the prompt.** Without this, the model confidently writes from memory — and misses the installed
package version.

A telling case: `gpt-oss-120b-medium` was tasked with `generate_synthetic_data.py` without the
`google-genai` contract in the prompt, and wrote the entire script using the **old, uninstalled** SDK
`google.generativeai` (`genai.configure()`, `genai.AsyncClient()`, `genai.exceptions.RateLimitError`,
`generation_config=` instead of `config=`) — that is, code that crashes on the first import. The same prompt with
added signatures (`from google import genai`, `client.aio.models.generate_content`,
the `errors.APIError → ClientError/ServerError` hierarchy) yielded a working result on the first try.

### How to pass context: via files, not in the argument

**Do not embed large data into the prompt text — `agy` agents can freely read project files.**
Verified by a direct test: with `--add-dir D:\Projects\graphrag-project`, the model reads
`data/ground_truth.json` and replies based on its contents, without prompting for any permissions
(unlike tools requiring the `command` permission — those are auto-declined in headless
mode).

There is also a hard technical reason: the prompt is passed as a command line argument, and in Windows,
the limit for the entire command line is **32767 bytes**. In Cyrillic, this is ~16 thousand characters, because
UTF-8 uses 2 bytes per character. An attempt to pass a task description of 38k characters, and then one trimmed to 26k,
failed in the same way even before the model started:

```
/d/.gemini/agy/agy: Argument list too long     (exit 126)
```

The error comes from the shell, and it does not end up in `--output-format json` at all — the output file remains
of zero length. Standard input is not a savior: `--print` requires the value as an argument and
does not read stdin (with `echo ... | agy --print --model X`, the `--print` flag consumes `--model` as its
value, and the model responds to the prompt "--model").

The resulting working pattern:

1. The full task description is written **as a file** in `orchestration/prompts/`.
2. The prompt in the argument is a single short line: "read such-and-such file, this is your task description, execute it".
3. `--add-dir <корень проекта>` gives access to the data; in the task description, the data is referenced **by paths**, not
   inserted as text.

A side benefit is caching: in a `gemini-3.1-pro-high` run with such a task description, `cache_read_tokens` was
221k with `input_tokens` 37k, meaning the model fetched the bulk of the context by reading files,
rather than through the argument.

Hence the working order, rather than "just sending the task to the model":

1. Before delegating — perform introspection of installed packages (`inspect.signature`,
   `dataclasses.fields`, reading sources in `.venv`), and verified signatures are inserted into the prompt
   verbatim, along with known pitfalls (e.g., double wrapping of `gemini_embed`).
2. The prompt is made **self-contained**, without FS access: otherwise the headless call gets stuck on a permission
   request and silently returns emptiness.
3. If tasks are interrelated, the contract between them (name and signature of the shared function) is specified
   by the orchestrator in both prompts, rather than negotiated between the models.
4. The delegate's result is **always** read and checked before running. From an actual run: in
   `build_kg.py` there was `glob("*.txt")` even though the generator produces `*.md` — syntactically
   flawless code that finds zero files.
