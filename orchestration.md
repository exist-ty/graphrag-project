# Delegating to the agy CLI

`agy.exe` (Antigravity CLI v1.1.10, in PATH as `agy`) is an external executor the orchestrator calls
via `Bash`. Everything here was verified by live runs, not read off `--help`.

Project-state facts live in `MEMORY.md`; this file holds delegation mechanics only.

## Call template

```
agy --print "Read <spec-file> — it is your task — and carry it out."   \
    --model <model> --output-format json --disable-slash-commands       \
    --add-dir "D:\Projects\graphrag-project" --print-timeout 15m        \
    > orchestration/runs/<tag>.json 2> orchestration/runs/<tag>.err
```

Then `python orchestration/extract.py <tag>` — it gates the result before writing it into `scripts/`
(see "Review order").

Write the full task spec to `orchestration/prompts/<tag>_<name>.md` and keep the argv prompt to one
line. Two hard reasons:

- **argv caps at 32767 bytes on Windows.** Cyrillic costs 2 bytes per character, so ~16k characters
  is the real ceiling. Exceeding it fails as `agy: Argument list too long`, exit 126, before the
  model runs — and the JSON output file is left empty, so the cause shows up only on stderr.
- **Agents read project files freely** under `--add-dir`, with no permission prompt, so passing data
  inside the prompt is pure waste. It also pays off in cache: one `gemini-3.1-pro-high` run reported
  221k `cache_read_tokens` against 37k `input_tokens`.

`--print` takes its value as an argument and does not read stdin — piping into it makes the next
flag the prompt.

## Everything an agent reads is written in English

Specs in `orchestration/prompts/`, the one-line argv prompt, and any instruction addressed to a
delegated agent — all in English. Ask delegated code for English docstrings and comments too.
Russian stays only in conversation with the user.

Three reasons, all observed here:

- Models follow English instructions more reliably. Both runs that ignored "read these files, do not
  explore" and reached for a shell instead were driven by Russian specs.
- Cyrillic costs 2 bytes per character in UTF-8, so a Russian prompt hits the 32767-byte argv ceiling
  at roughly half the length of an English one.
- Instructions and code end up in the same context during review; one language means no switching
  cost for the reviewing model.

## Every input the spec names must exist before the call

Check it, do not assume it. An agent that cannot find a file the spec promised does not stop and
does not ask — it substitutes something plausible and proceeds, and the substitution is reported
nowhere.

This happened here. `t9_structural_identity.md` told the agent to read `data/entity_registry.json`
and forbade `data/ground_truth.json`, because the whole point of that split is to keep the
evaluation from grading itself. The delegation was launched before the split had run, so the
registry did not exist. The agent read `ground_truth.json` instead and designed against the held-out
entities — the one thing the spec ruled out. Nothing in `status`, `usage` or stderr showed it; it
surfaced only by grepping the artifacts for withheld ids afterwards.

Concretely, before every call:

- Every path the spec names exists and holds what the spec says it holds. A spec phrased as "a
  change already under way will produce X" is a spec that will be run against a missing X.
- Where a spec forbids reading something, verify the result afterwards — grep the artifacts for what
  should not be there. A prohibition in a prompt is a request, not an enforcement.
- Ordering between delegated tasks and your own work is yours to enforce. Nothing else will.

## Models

| Model | Use for |
|---|---|
| `gemini-3.1-pro-high` | **Default for substantial work.** Large heterogeneous context: code that needs data schema, storage formats and adjacent modules held at once; metric and algorithm design; large-log analysis. |
| `claude-sonnet-4-6` | Tricky integrations against an unfamiliar library; second opinion. Has no `--effort` flag — passing it fails the call outright with `--effort is not supported`, zero tokens spent. |
| `gemini-3.5-flash-high` | Routine bulk text: translation, reformatting, boilerplate. |
| `gpt-oss-120b-medium` | Cheapest routine, and the only model where `--effort` applies. Weakest on library specifics — it wrote an entire script against the uninstalled `google.generativeai` SDK. |
| `claude-opus-4-6-thinking` | Architectural forks only. Rarely. |

Gemini models bake reasoning depth into the name (`-high`/`-medium`/`-low`); do not pass `--effort`
to them. Inter-call caching works on gemini models (77-221k `cache_read_tokens`) and stays at 0 for
`claude-sonnet-4-6` and `gpt-oss-120b-medium`.

Cost floor: ~20-27k input tokens of system overhead per call, 29-46k on real delegation prompts.
Aggregate small asks into one prompt.

## Flags that matter

- `--print` — headless. Without it the CLI opens a TUI.
- `--model` — always pass it; the CLI default is not guaranteed to match the task.
- `--output-format json` — `{conversation_id, status, response, duration_seconds, num_turns, usage}`.
  Check `status == "SUCCESS"` **and read stderr**: failures that never reach the JSON land there.
- `--add-dir <path>` — scopes the agent to the project instead of the whole disk.
- `--mode accept-edits` — required when the agent should write files itself; `plan` for
  propose-only.
- `--print-timeout` — default 5m, raise for long jobs.
- `--conversation <id>` — continues a session, but input tokens accumulate linearly (20.7k → 41.5k
  on the second step). Close it as soon as the task is done.
- `--dangerously-skip-permissions` — **not by default.** Only for a specific run, with a narrow
  `--add-dir`, after explicit user consent, ideally alongside `--sandbox`.

## Failure modes to recognise

- **Empty `response` with non-zero `output_tokens`** — the agent tried a tool needing the `command`
  permission, which headless mode auto-denies. The reason appears only on stderr. Fix: name exact
  file paths in the spec so plain reads suffice, instead of inviting directory exploration.
- **Delegated runs modify the working tree**, including without `--dangerously-skip-permissions`,
  and they exceed their stated scope — a translation run scoped to one file also rewrote `PLAN.md`.
  Always `git diff` after a delegated run.
- **`status: ERROR` with a non-empty `response`** — provider-side termination mid-stream. The
  partial output is often still usable; check whether the fenced block closed.

## The rule that decides quality

**Pin verified API signatures in the spec.** Quality tracks this far more than model choice: without
it, a model writes confidently against the wrong package version. Introspect what is installed
(`inspect.signature`, `dataclasses.fields`, reading `.venv` sources), paste the real signatures and
known traps into the spec, and state cross-task contracts — shared function names and signatures —
yourself rather than letting two agents agree on them independently.

## Artifacts go to files; replies carry only what needs judging

A delegated agent should write its code to a file and return only the prose the orchestrator must
read — a design rationale, a list of findings, a summary. Code in the reply body is wasted output:
it is gated mechanically, not read, so routing it through the response only risks it landing in the
orchestrator's context.

When two agents work the same task in parallel, give each its own output directory under
`orchestration/runs/` rather than forbidding writes — otherwise the second overwrites the first.

## Review order — cheapest check first

Measured here: ~1900 lines read into the orchestrator's context yielded one bug. Running the code
surfaced the silent vector-count corruption and both quota walls; a delegated review surfaced an
inverted metric for 8 KB of reading instead of 957 lines. Stop at the first rejection:

1. **Mechanical gate** — `orchestration/extract.py`, automatic: syntax, resolvability of every
   top-level import, forbidden constructs, and the prompt's contract. Rejected output goes to
   `orchestration/runs/<tag>.rejected.py` and is never read in full.
2. **Run it** — a smoke run with a small `--limit`. Silent data corruption shows up here and nowhere
   else.
3. **Delegated review** — `gemini-3.1-pro-high` against the installed library sources; read only the
   findings, and verify each before acting on it.
4. **Own reading** — last, and only at the lines the earlier steps pointed to.

Do not poll background tasks; completion arrives as a notification.
