# Design Pattern Retrofit Benchmark

Tasks that ask an agent to apply or remove one instance of a GoF design pattern
in a real Java project.

Each task has two project states that behave the same from the outside:

- `pattern`: the code contains the pattern instance.
- `no_pattern`: the code does not. This is either the commit just before the
  pattern was introduced upstream, or a reversal made by hand on a branch.

Each task has two subtasks:

- `apply`: start from `no_pattern` and introduce the pattern.
- `remove`: start from `pattern` and take it out.

A subtask is available only when its starting state exists.

## Files

| File | Contents |
|---|---|
| `benchmark.json` | All high-confidence tasks with full data (states, targets, checks, provenance). |
| `benchmark_lite.json` | A list of task IDs: 1-2 representative tasks per pattern. |
| `BENCHMARK_SUMMARY.md` | Counts per pattern and the lite set. Generated; do not edit. |
| `PATTERN_INVENTORY.md` | Notes on each pattern: what's strong, what's weak, what's missing. |
| `build_benchmark.py` | Builds the two JSON files and `BENCHMARK_SUMMARY.md`. |
| `resolve_dpdf.py` | Finds DPD-F classes in each project's latest HEAD (writes `raw_data/data/dpdf_resolved.json`). |
| `raw_data/` | Inputs from earlier mining efforts. Most files are symlinks into `Agent4Refactoring/`. |

## Regenerating

```bash
cd refactoring_evals/design_pattern_retrofits
python3 resolve_dpdf.py      # only needed when DPD-F clones change
python3 build_benchmark.py
```

Both scripts read git history from the clones in
`/Users/abhiram/Documents/TBE/evaluation_projects/<project>`. A missing commit is
logged with `!` and the task is skipped.

## Task schema (`benchmark.json`)

```jsonc
{
  "task_id": "builder_hbase_07e0a30",          // <pattern>_<project>_<sha7 | class>
  "pattern": "Builder",
  "project": "hbase",
  "repo_url": "https://github.com/apache/hbase",
  "states": {
    "pattern":    {"kind": "commit", "origin": "upstream_history", "commit": "07e0a30e..."},
    "no_pattern": {"kind": "commit", "origin": "upstream_history", "commit": "<parent>"}
    // or {"kind": "branch", "origin": "synthesized_branch", "base_commit", "branch",
    //     "head_commit", "local_repo_path", "pushed_to_remote_refs", ...}
    // or null when only the remove subtask exists
  },
  "subtasks": {
    "apply":  {"available", "start_state", "reference_state", "target": {"class_name", "file_paths"}, "instruction", "prompts"},
    "remove": {"available", "start_state", "reference_state", "target": {...}, "instruction", "prompts"}
  },
  "completeness": "complete | remove_only",
  "verification": "manual_case_study | human_validated | manual_reversal | llm_validated | tool_detected | manual_case_study_borderline",
  "roles": {},          // GoF role -> files (tesis pipeline only)
  "context": {},        // commit subject/date, bad smell, detection reasoning, ...
  "alternatives": [],   // other no-pattern versions (e.g. synthesized removal patches)
  "checks": {},         // automated sanity signals; see below
  "provenance": [{"source", "file", "record_id"}]
}
```

`target.file_paths` are relative to the repository root at the subtask's start
state. The `prompts` fields hold multi-style prompts where earlier work wrote
them; currently that's only `builder_hbase_07e0a30` and `decorator_ant_99deca2`.

## Sources and how each becomes a task

| Source | Pattern state | No-pattern state |
|---|---|---|
| Tesis verified cases (`apply-pattern-casos-verificados.md`, 20 commits) | the retrofit commit | its parent |
| Reversal branches (`reversal_branches.json`, plus 5 local branches not listed there) | merge-base with the default branch | the reversal branch head |
| Pattern candidates (`aggregated_candidates.json`) | birth commit | its parent |
| Tesis pipeline (`master_dataset.json`) | birth commit | its parent |
| DPD-F (`dpdf_dataset.json`) | latest default-branch HEAD | none (apply needs a synthesized reversal) |
| Removal patches (`undo_results.jsonl`) | attached to the matching candidate as `alternatives` | |

Records are merged into one task when they describe the same
`(project, pattern, class)`, or when records from different sources point at
the same upstream retrofit commit. Distinct classes mined from one source keep
separate tasks, such as the three hbase `*MetricsBuilder` classes from commit
`448ba3a`. The best-verified record wins, and every contributing record stays
in `provenance`.

## Filters

Records are dropped when any of these apply:

- Candidates that failed human validation, or whose pattern file is not Java.
- Tesis pipeline instances that the LLM rejected.
- DPD-F rows with no pattern label, from projects not marked `likely_buildable`,
  or whose class can't be found at the latest HEAD.
- Duplicate removal patches. `UserMetricsBuilder` B-1 is byte-identical to
  `ServerMetricsBuilder` B-2.

These confidence filters run after merging. Their rules and drop counts are
recorded in `benchmark.json` under `confidence_filter`:

- `dpdf_structural_check_not_passed`: a DPD-F class that fails, or has no,
  structural check for its pattern at HEAD. The checks are regexes in
  `resolve_dpdf.py`, for example a private constructor plus a static self
  reference for Singleton. They mainly catch name-only matches, such as
  `NewInstanceOfSingletonInspection` or Liferay's `LayoutPrototype`.
- `llm_analysis_saw_no_code`: tesis pipeline approvals where the LLM said the
  code was never shown to it.
- `remove_target_missing_at_pattern_commit`: history tasks whose target class
  isn't found at the pattern commit. This usually means a wrong birth commit
  or a rename that couldn't be traced.

## `checks`

These flags are kept on each task for downstream filtering:

- **History tasks:** `remove_target_in_pattern_state`,
  `remove_target_preexisted_in_no_pattern_state`, `modifies_existing_code`,
  `likely_feature_addition`, `subject_suggests_non_retrofit`, and the counts of
  Java files added, modified, and deleted.
- **Tesis pipeline:** `llm_analysis_saw_code`.
- **DPD-F:** `structural_check`, `target_file_lines`, `target_in_test_code`,
  `repo_archived`, `repo_stars`, `repo_last_push`.

## Lite selection

The full rule is stored in `benchmark_lite.json` under `selection`.

- Pick up to 2 tasks per pattern, each from a different project.
- Rank by:
  1. both states available
  2. verification level
  3. upstream history over synthesized reversals
  4. fewer red flags in `checks`
  5. extra assets (alternatives, prompts)
  6. smaller diffs
- Remove-only DPD-F tasks are used only for patterns with no eligible complete
  task. They must also have at least 30 lines and a non-test target.

## Known limitations

- **Reversal branches:** 9 of the 10 reversal-branch tasks exist only in local
  clones, and `pushed_to_remote_refs` is empty for those. The exception is cxf
  `WSDLServiceBuilder`. Push the others before sharing the benchmark.
- **Mixed-language reversal:** `visitor_flink_datatypevisitor` is a Java
  Visitor, but its reversal branch also edits two Scala call sites. These are
  listed in `non_java_files_changed`.
- **DPD-F states:** these point at the HEAD from when the clone was made. The
  commit SHA is pinned, so tasks are reproducible, but the code is not the
  newest.
- **Tesis birth commits:** the pipeline sometimes recorded the wrong commit.
  One was corrected by hand (`BIRTH_OVERRIDES` in `build_benchmark.py`). Tasks
  where the target already existed in the parent commit are flagged with
  `remove_target_preexisted_in_no_pattern_state`.
- **No-pattern states:** none have been checked to build or pass tests.
- **Old analysis script:** `analyze_benchmark.py` reads the previous
  `benchmark.json` format and no longer works.
  `BENCHMARK_SUMMARY.md` replaces it.
