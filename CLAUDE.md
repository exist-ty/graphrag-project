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

`MEMORY.md` is a living project state file, separate from `PLAN.md`. `PLAN.md` is a plan
written once and not rewritten as work progresses; `MEMORY.md` is what has actually occurred
and what is actually true right now. When working on the project:

1. **At the start of work** — read `MEMORY.md` before relying on `PLAN.md` out of
   context: `MEMORY.md` may contain deviations from the plan, empirically found facts, or
   blocked steps that are not yet in `PLAN.md`.
2. **As work progresses** — actively write to `MEMORY.md`, without postponing to the end of the session, when:
   - a step from `PLAN.md` is completed, or is completed differently than written in it;
   - a decision is made that is not in `PLAN.md` (for example, an empirically found
     `embedding_dim` value, a model replacement, a query interface selection);
   - something unexpected is discovered (a bug, an API quirk, a quota, a blocked path);
   - a delegated `agy` call is made (see `orchestration.md`), the result of which affects
     subsequent steps.
3. **Format** — facts, not a chronological log: update/delete outdated entries instead of
   accumulating new ones next to them. `MEMORY.md` is loaded in its entirety into the context of each session — keep
   it compact.

## Delegation via agy

Before calling `agy` — check `orchestration.md` (model selection by task class, flags
`--effort`/`--output-format json`/`--add-dir`/`--conversation`, restriction on
`--dangerously-skip-permissions` only by explicit consent for a specific run). If the
result of a delegated call affects subsequent steps of the project, record it in `MEMORY.md`.
