# Extract-Method Refactoring Eval — Report

Evaluates whether a repo-level "guardrail" nudge (driven by RefactoringMiner
mining of the agent's own diff) makes an LLM coding agent use the IDE's
`extract_method` refactoring tool instead of hand-editing files.

- **Agent**: OpenHands SDK, headless `LocalConversation`, evaluated with two
  models: `openrouter/~deepseek/deepseek-v4-flash-latest` (DeepSeek v4 flash)
  and `openrouter/z-ai/glm-5.3-flash` (GLM 5.3 flash), both via OpenRouter.
- **Tasks**: 6 extract-method refactoring tasks on OpenRefine (`master`,
  pristine baseline `f6c889a3`), one target method each.
- **Arms**:
  - `baseline` — nudge off (`OPENHANDS_REFACTORING_NUDGE=0`).
  - `guardrail` — nudge on: after each run turn, RefactoringMiner mines the
    working tree; if extract-methods are detected in the diff but were not
    performed via the IDE tool, the agent receives a corrective nudge.
- **Tooling**: IntelliJ IDEA OSS MCP server exposing `extract_method` (and,
  in the first full pass, ~38 other IDE tools), plus SDK file_editor /
  terminal for direct edits. A follow-up baseline rerun narrowed the IDE
  server to 9 refactoring tools to test whether tool overload explained
  baseline non-use (see "Narrowed tool set"). The harness commits the
  working tree after each run; the agent never commits.
- **Attribution**: every edit is snapshotted and categorized
  (`refactoring_tool` vs `direct_edit`); net lines vs the base commit are
  attributed by replaying the snapshot chain against the final commit diff
  (`openhands.sdk.conversation.edit_attribution.net_line_attribution`).
  Extract-methods in the final diff are counted with RefactoringMiner.

## Headline numbers

### DeepSeek v4 flash (`deepseek-v4-flash-latest`)

| Metric (6 tasks) | Baseline | Guardrail |
|---|---:|---:|
| **% of net edited lines via extract_method tool** | **0%** | **91.6%** |
| Net lines via tool (ins+del) | 0 | 285 |
| Net lines via direct edits (ins+del) | 226 | 26 |
| Unattributed net lines | 3 | 7 |
| extract_method tool calls | 0 | 17 |
| Direct-edit tool calls | 10 | 26 |
| Extract-methods detected in final diff (RefactoringMiner) | 19 | 33 |
| Total LLM cost | $0.14 | $0.29 |
| Total wall time | 20 min | 41 min |

**Takeaway**: without the intervention the agent never touches the
refactoring tool — it performs every extraction by hand-editing (and still
produces extract-method-shaped diffs, so capability is not the bottleneck).
With the nudge, ~92% of net edited lines flow through the IDE's
`extract_method` tool, and RefactoringMiner finds ~74% more extract-methods
in the resulting commits (33 vs 19).

### GLM 5.3 flash (`z-ai/glm-5.3-flash`)

| Metric (6 tasks) | Baseline | Guardrail |
|---|---:|---:|
| **% of net edited lines via extract_method tool** | **0%** | **38.0%** |
| Net lines via tool (ins+del) | 0 | 93 |
| Net lines via direct edits (ins+del) | 252 | 152 |
| Unattributed net lines | 0 | 2 |
| extract_method tool calls | 0 | 14 |
| Direct-edit tool calls | 6 | 15 |
| Extract-methods detected in final diff (RefactoringMiner) | 19 | 33 |
| Total LLM cost | $0.05 | $0.19 |
| Total wall time | 4.4 min | 16.4 min |

GLM is much faster and cheaper per task, but far less compliant with the
nudge: it used the tool on tasks 1/3/4 yet ignored it on tasks 0/2/5
(task 2 produced no changes at all; task 5 hand-edited despite nudges). EM
outcomes match DeepSeek's (19 → 33), so the guardrail improves refactoring
structure either way — tool *adoption* is the model-sensitive part.

## Per-task results (DeepSeek v4 flash)

| Task | Target | Baseline net ins/del, all direct edits (EM) | Guardrail net ins/del: tool · direct, tool calls (EM) |
|---|---|---|---|
| 0 | `Engine.computeFacets` | +18/−10 (2) | +46/−38 · +0/−2, 2 calls (4) |
| 1 | `ScatterplotDrawingRowVisitor` ctor | +12/−0 (3) | +22/−14 · +3/−4, 3 calls (6) |
| 2 | `ScatterplotFacet.computeChoices` | +7/−1 (2) | +24/−18 · +0/−0, 2 calls (4) |
| 3 | `ExpressionRowVisitor.processRow` | +5/−2 (1) | +3/−0 · +1/−0, 1 call (2) |
| 4 | `TimeRangeFacet.computeChoices` | +39/−23 (4) | +8/−14 · +7/−0, 2 calls (3) |
| 5 | `TemplatingExporter` | +69/−40 (7) | +64/−34 · +5/−4, 7 calls (14) |

Notes:
- Guardrail task 4 took 18 min ($0.11) — the nudge loop retried several
  rounds on that task.
- All lines reconcile with the commit numstat except a small unattributed
  tail (10 of ~550 net lines, ~2%): diff-replay positions that match no
  ledger-attributed line. Totals are unaffected.

## Per-task results (GLM 5.3 flash)

| Task | Baseline net ins/del (EM) | Guardrail net ins/del: tool · direct, tool calls (EM) |
|---|---|---|
| 0 | +18/−10 (2) | +18/−10 · +0/−0, 0 calls (2) |
| 1 | +25/−17 (2) | +13/−0 · +3/−2, 4 calls (8) |
| 2 | +7/−1 (2) | no changes (0) |
| 3 | +14/−7 (2) | +31/−11 · +6/−0, 5 calls (9) |
| 4 | +32/−20 (6) | +19/−19 · +19/−3, 5 calls (9) |
| 5 | +63/−38 (5) | +0/−0 · +60/−31, 0 calls (5) |

GLM guardrail task 0 mirrors its baseline diff exactly — same final state,
hand-edited both times despite the nudge.

## Narrowed tool set (baseline rerun)

Hypothesis: baseline non-use of `extract_method` might be tool overload —
the IDE server exposed ~39 tools and the model got lost. We reran the
baseline arm with the IDE server narrowed to 9 refactoring tools
(`extract_method`, `rename_refactoring`, `move_method`, `safe_delete`,
`inline_method`, `introduce_parameter`, `pull_up_method`,
`push_down_method`, `execute_tool`); with SDK built-ins the agent saw 14
tools total instead of ~44.

| Metric (baseline arm, 6 tasks) | DeepSeek, 39 tools | DeepSeek, 9 tools | GLM, 39 tools | GLM, 9 tools |
|---|---:|---:|---:|---:|
| **% of net edited lines via tool** | **0%** | **0%** | **0%** | **2.1%** |
| extract_method calls | 0 | 0 | 0 | 1 |
| Net lines via tool (ins+del) | 0 | 0 | 0 | 4 |
| Net lines via direct edits (ins+del) | 226 | 250 | 252 | 188 |
| Direct-edit calls | 10 | 8 | 6 | 21 |
| Extract-methods detected (RefactoringMiner) | 19 | 21 | 19 | 11 |
| Unattributed net lines | 3 | 0 | 0 | 1 |
| Total LLM cost | $0.14 | $0.13 | $0.05 | $0.11 |
| Total wall time | 20 min | 12.7 min | 4.4 min | 16.8 min |

**The hypothesis is not supported.** Given nothing but refactoring tools
from the IDE, DeepSeek still hand-edited every extraction (0 tool calls);
GLM used `extract_method` exactly once (task 3, 4 inserted lines). Baseline
non-use is a model *decision*, not confusion from tool overload or a
capability gap — reinforcing that the RefactoringMiner-driven nudge is the
operative lever, and the guardrail results stand unchanged.

Secondary observations:
- DeepSeek's narrowed runs were faster (12.7 vs 20 min), slightly cheaper,
  and the unattributed tail vanished (0 vs 3 lines).
- GLM's narrowed runs were noisier: EM count fell to 11 and task 1 produced
  no commit — a malformed `file_editor` loop (missing `old_str`) tripped
  the stuck detector before any work landed.
- Per-task (baseline, narrowed): DeepSeek +163/−87 all direct, EM 2/2/2/3/4/8;
  GLM +122/−66 direct and one 4-line tool extraction, EM 2/0/2/1/2/4.

## Tool description verbosity (baseline rerun)

Hypothesis: baseline non-use of `extract_method` might be due to verbose
tool descriptions obscuring the tool's purpose. The IntelliJ MCP server
supports dynamic description verbosity levels controlled by writing to
`~/.mcp-description-variant.txt` (`terse`, `medium`, `verbose`). We reran
the baseline arm with the IDE server configured for **terse** descriptions.

### DeepSeek v4 flash — Terse descriptions

| Metric (baseline arm, 6 tasks) | DeepSeek, verbose | DeepSeek, terse |
|---|---:|---:|
| **% of net edited lines via tool** | **0%** | **0%** |
| extract_method calls | 0 | 0 |
| Net lines via tool (ins+del) | 0 | 0 |
| Net lines via direct edits (ins+del) | 226 | 254 |
| Direct-edit calls | 10 | 6 |
| Extract-methods detected (RefactoringMiner) | 19 | 18 |
| Unattributed net lines | 3 | 0 |
| Total LLM cost | $0.14 | $0.09 |
| Total wall time | 20 min | 9.9 min |

**The hypothesis is not supported.** With terse tool descriptions,
DeepSeek still made 0 `extract_method` tool calls — all 6 tasks were
completed via direct `file_editor` edits. RefactoringMiner detected 18
extract-methods in the resulting diffs, confirming the model performed
the correct refactoring pattern but chose not to use the IDE tool.

Per-task results (terse):

| Task | Target | Terse baseline net ins/del, all direct edits (EM) |
|---|---|---|
| 0 | `Engine.computeFacets` | +18/−10 (2) |
| 1 | `ScatterplotDrawingRowVisitor` ctor | +7/−0 (2) |
| 2 | `ScatterplotFacet.initializeFromConfig` | +28/−19 (3) |
| 3 | `ExpressionTimeValueBinner.processRow` | +17/−14 (2) |
| 4 | `TimeRangeFacet.computeChoices` | +25/−27 (4) |
| 5 | `TemplatingExporter.export` | +57/−32 (5) |

Secondary observations:
- Terse runs were faster (9.9 vs 20 min) and cheaper ($0.09 vs $0.14),
  likely due to fewer input tokens from shorter tool descriptions.
- All 6 tasks completed successfully with correct refactoring structure
  (18 EMs detected), confirming terse descriptions do not impair the
  model's ability to understand the task — only its tool selection.
- Run artifacts: `results/baseline-terse-deepseek/`

## Artifacts

- Harness: `run_eval.py` (also tees full transcripts to
  `results/<run-id>/run.log`)
- Results: `results/baseline-pilot2/` + `results/baseline-tasks-2-5/`
  (DeepSeek baseline), `results/guardrail-tasks-0-5/` (DeepSeek guardrail),
  `results/glm53flash-0-5/` (GLM 5.3 flash, both arms),
  `results/baseline-fewtools-deepseek/` + `results/baseline-fewtools-glm/`
  (baseline with 9 IDE tools), `results/baseline-terse-deepseek/`
  (DeepSeek baseline with terse tool descriptions)
- Each run dir: `results.json`, `report.json`, `edit_attribution.jsonl`,
  `refactoring_log.jsonl` (guardrail), `run.log`
- An earlier `smoke-guardrail/` run validated the pipeline but is superseded
  by `guardrail-tasks-0-5/`.

## Next: scaling

The pipeline is validated end-to-end; scale-outs to consider: more projects
and tasks, additional models (nudge sensitivity), and comparing EM quality
(compilability, method naming) across arms.
