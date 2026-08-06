# graphrag-project — Instructions for Claude Code

## Sources of Truth

- **`PLAN.md`** — implementation plan. Follow its steps in order. In particular: do not
  scale synthetic data generation to the full volume (~1000 documents) until the
  "Verification" section has been passed on a small batch (~20-30 documents).
- **`orchestration.md`** — rules for delegating routine tasks to the external `agy` CLI (which model
  for which task, which parameters, which limitations). Use for any delegation of
  subtasks via `agy`; do not reinvent parameters.
- **`MEMORY.md`** — current working context of the project, see below.

## Context management: `MEMORY.md`

`PLAN.md` is the forward plan, revised only when the plan itself changes; `MEMORY.md` is what
actually happened and what is true now. Read `MEMORY.md` before acting on `PLAN.md` — it holds deviations, empirical findings and
blocked steps the plan does not know about.

Write to it **as work progresses**, not at the end of a session, whenever: a plan step completes or
completes differently; a decision is made that is not in the plan; something unexpected turns up
(a bug, an API quirk, a quota, a blocked path); or a delegated call produces a result that affects
later steps.

Record facts, not a chronicle: update or delete a stale entry rather than adding a newer one beside
it. The whole file is read at the start of every session — keep it compact, and keep delegation
mechanics out of it (those belong in `orchestration.md`).

## Working rules

**Token conservation has absolute priority.** These files are read every session; every rule below
exists because ignoring it cost context on a real run.

- **Never read delegated code in full as the first check.** Order, stopping at the first rejection:
  `python orchestration/extract.py <tag>` (mechanical gate) → smoke run with a small `--limit` →
  delegated review → own reading, only at the lines the earlier steps pointed to. Measured on this
  project: ~1900 lines read by hand yielded one bug; running the code and a delegated review found
  everything expensive.
- **Verify every input a spec names exists before delegating.** A missing input does not stop the
  agent; it substitutes one silently, including files the spec forbade.
- **Write everything an agent reads in English** — specs, prompts, and the docstrings you ask
  delegated code to produce. Russian is for conversation with the user only.
- **Check `git log` and `git status` after every delegated run.** Agents write outside their
  stated scope and one has committed — and a commit leaves `git diff` clean, so diff alone misses it.
- **Do not poll background tasks.** Completion arrives as a notification.
- **Verify a claim before acting on it**, whether it comes from a delegated review, another agent,
  or this file. Reviews produce confident findings that do not reproduce.
- **Commit as work progresses and push**; do not accumulate. Remote is in `MEMORY.md`.
- **Prefer an exit code to a report.** A check that returns 0/1 (`verify_graph.py`, the extract
  gate) is cheaper and more honest than prose describing whether something worked.

## Delegation via agy

Put the task spec in `orchestration/prompts/`, keep the argv prompt to one line, and let the agent
read project files via `--add-dir` — passing data inside the prompt wastes tokens and breaks past
the 32767-byte argv limit. Extract results only through `orchestration/extract.py`, never by hand.

Before calling `agy` — check `orchestration.md` (model selection by task class, flags
`--effort`/`--output-format json`/`--add-dir`/`--conversation`, restriction on
`--dangerously-skip-permissions` only by explicit consent for a specific run). If the
result of a delegated call affects subsequent steps of the project, record it in `MEMORY.md`.
