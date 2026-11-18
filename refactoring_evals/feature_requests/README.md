# Feature-request refactoring evals

Measures whether an agent **discovers and performs an enabling refactoring on
its own** while implementing a feature, with no hint that one is needed.

Sits alongside the other `refactoring_evals/` categories:

- `extract_method/` — the prompt *tells* the agent to split a method.
- `tool_composition/` — the prompt states a refactoring goal (still names the
  restructuring), reachable only by chaining IDE tools.
- **`feature_requests/` (this one)** — the prompt states a *feature* goal only.
  It never mentions refactoring, extracting, splitting, or reuse. An
  extract-method step is nonetheless the clean way to satisfy it.

## The idea

Each task is a plain feature request. In every case the logic the feature
needs is trapped inside one large method (e.g. the value-detection logic inside
`ExcelImporter.extractCell`). The intended path is:

1. extract the trapped logic into its own method (an **extract-method**
   refactoring), then
2. build the feature on top of the extracted method.

The failure mode under test is an agent that **duplicates** the trapped logic
into the new code path instead. Duplication usually passes a naive functional
check today and only breaks later when the two copies drift — so every task's
feature prompt carries a correctness requirement that *forbids* drift ("the
preview must equal the real import", "identical to what `format` produces",
"configured identically"). That turns duplication from a style problem into a
correctness problem the grading can catch.

No prompt contains a hint toward the refactoring. The hint is the thing under
test. (`run_eval.py --hint` appends a generic "prefer the refactoring tools"
nudge, for running the hint/no-hint contrast.)

## What is measured

- **Shape (automated, necessary not sufficient):** the regex `checks` in
  `tasks.json`, plus optional compile (`--build`). They confirm the new method
  exists and the old call site delegates to it.
- **Drift (the real discriminator — manual/scripted, see below):** mutate one
  branch of the trapped logic and confirm *both* the feature path and the
  original path change together. Duplication fails this; extraction passes it.
- **Tool use:** `run_eval.py` records every tool call in order with its outcome
  and splits the final diff into `refactoring_tool` / `direct_edit` /
  `unattributed` lines (`tool_line_share`). A correct solution is unreachable
  without reading and editing real files, so this doubles as a "does the agent
  use its tools at all" probe.

## The drift check

The `checks` confirm a delegating structure exists but cannot, on their own,
prove the logic is defined once. To separate extraction from duplication,
after a run that passes `checks`:

1. In the extracted method, change one observable branch of the trapped logic
   (e.g. in case 01, flip the `"General"` integer-vs-float rule; in case 03,
   change `setMaxCharsPerColumn`).
2. Exercise **both** entry points on an input that hits that branch — the new
   feature method and the original one (`extractCell` / `format` / a real
   `parseOneFile`).
3. If both outputs change, the logic has a single source (extraction — pass).
   If only one changes, the logic was duplicated (fail), regardless of whether
   the functional `checks` passed.

This is deliberately behavioral: it survives any naming or placement the agent
chose, and only fails genuine duplication.

## Tasks

| id | file | trapped logic | feature |
|----|------|---------------|---------|
| `feature-extract-01-excel-detected-type-preview` | `importers/ExcelImporter.java` | value/type detection inside `extractCell` | expose per-cell detected value |
| `feature-extract-02-exporter-plain-text-value` | `exporters/CustomizableTabularExporterUtilities.java` | value→text conversion inside `CellFormatter.format` | plain display text without link decoration |
| `feature-extract-03-separator-build-parser` | `importers/SeparatorBasedImporter.java` | parser construction inside `parseOneFile` | build the configured parser without reading a file |

Each task's `enabling_refactoring` and `duplication_failure` fields are
grader-only context; give the agent the `prompt` only.

## Baseline

- Repo: `https://github.com/Abhiram98/OpenRefine.git`
- Commit (`manifest.json`): `f6c889a3a37d88fdda9173818c4ecaf1901dbeaf`
- Prompts reference line numbers of this commit; the harness checks each task
  out at this sha on a throwaway branch.

## Running

Same prerequisites and harness as `tool_composition/` (OpenRefine checkout
under `REPOS_ROOT`, `OPENROUTER_API_KEY`, IntelliJ IDEA OSS from
intellij-refactoring-fork with its MCP proxy up):

```bash
# all tasks, no hint (the primary condition)
uv run python refactoring_evals/feature_requests/run_eval.py --build

# one task, repeated, with the tool hint (contrast condition)
uv run python refactoring_evals/feature_requests/run_eval.py \
    --tasks feature-extract-01-excel-detected-type-preview --repeats 3 --hint --build
```

Results land in `results/<run_id>/` (`results.json`, `run.log`,
`edit_attribution.jsonl`, per-task build logs).

## Status

Tasks are `"verified": false`: the IntelliJ known-solution sequences have not
yet been executed against the base commit. Verify by running one known solution
per task and confirming `checks` pass and the project compiles, then flip
`verified`.
