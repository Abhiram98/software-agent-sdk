"""Render ``report.html`` for a ``run_reversal.py`` run directory.

The report is meant to answer "can I trust this reversal?" in four views:

1. Verdicts: build and oracle results on both states, leftover references to
   the target, test-file edits, removed public declarations.
2. Plan -> execution: the agent's task-tracker plan, and per plan item the
   tool calls made while it was in progress.
3. Final diff: every changed line is put in one review bucket:
   - ``tool``: written or deleted by an IDE refactoring tool;
   - ``explained``: a direct edit inside a code range RefactoringMiner
     attributes to a refactoring on the whole start -> result diff (coarse:
     ranges cover whole methods or classes);
   - ``deleted file``: part of a file deleted outright (the build checks
     nothing still references it);
   - ``residue``: everything else. This is the review queue.
4. Timeline: every reasoning step, tool call, observation and guardrail nudge,
   with the snapshot diff each file-changing call produced and the
   refactorings RefactoringMiner found in each agent step.

Usage:
    uv run python refactoring_evals/design_pattern_retrofits/render_report.py \\
        refactoring_evals/design_pattern_retrofits/results/<task>/<run_id>
"""

import argparse
import html
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from string import Template
from typing import Any


TEMPLATE_PATH = Path(__file__).parent / "report_template.html"
BUCKETS = ("tool", "explained", "deleted file", "residue")
CATEGORY_LABELS = {
    "refactoring_tool": "refactoring tool",
    "direct_edit": "direct edit",
    "unattributed": "unattributed",
}
NO_TASK = "(no task in progress)"
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PUBLIC_DECL_RE = re.compile(r"^\s*public\s")
TEST_PATH_RE = re.compile(r"(^|/)src/test/|(^|/)tests?/")
OBS_PREVIEW_CHARS = 4000

Plans = list[tuple[int, list[dict]]]


def git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=300
    )
    return result.stdout


def esc(text: Any) -> str:
    return html.escape(str(text))


def el(tag: str, *children: str, cls: str = "", title: str = "", is_open=False) -> str:
    """An HTML element; children are HTML, attribute values are escaped here."""
    attrs = f' class="{cls}"' if cls else ""
    attrs += f' title="{esc(title)}"' if title else ""
    attrs += " open" if is_open else ""
    return f"<{tag}{attrs}>{''.join(children)}</{tag}>"


def pre(text: str) -> str:
    return el("pre", esc(text))


def details(summary: str, *body: str, is_open: bool = False, cls: str = "") -> str:
    return el("details", el("summary", summary), *body, is_open=is_open, cls=cls)


def tag(text: str, cls: str) -> str:
    return el("span", esc(text), cls=f"tag {cls}")


def bucket_cls(bucket: str) -> str:
    return "b-" + bucket.replace(" ", "-")


def table(rows: list[tuple[str, str]]) -> str:
    return el("table", *(el("tr", el("th", k), el("td", v)) for k, v in rows))


def verdict(value: bool | None, good: str = "yes", bad: str = "no") -> str:
    if value is None:
        return el("span", "n/a", cls="muted")
    return el("span", esc(good), cls="ok") if value else el("span", esc(bad), cls="bad")


def to_utc(timestamp: str) -> datetime:
    return datetime.fromisoformat(timestamp).astimezone(UTC)


def read_jsonl(path: Path, session_id: str) -> list[dict]:
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text().splitlines() if line]
    return [r for r in records if r.get("session_id") == session_id]


# --- diffs -----------------------------------------------------------------


def parse_diff(diff: str) -> dict[str, list[dict[str, Any]]]:
    """Unified diff -> {path: [{op, text, old, new}]}, op in "@", "+", "-", " "."""
    files: dict[str, list[dict[str, Any]]] = {}
    lines: list[dict[str, Any]] | None = None
    old_path = ""
    old = new = 0
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            lines = None
        elif line.startswith("--- "):
            old_path = line[4:].removeprefix("a/")
        elif line.startswith("+++ "):
            new_path = line[4:].removeprefix("b/")
            lines = files.setdefault(
                old_path if new_path == "/dev/null" else new_path, []
            )
        elif lines is not None and (m := HUNK_RE.match(line)):
            old, new = int(m.group(1)), int(m.group(2))
            lines.append({"op": "@", "text": line, "old": None, "new": None})
        elif lines is not None and line[:1] in ("+", "-", " "):
            entry = {"op": line[0], "text": line[1:], "old": None, "new": None}
            if line[0] != "+":
                entry["old"], old = old, old + 1
            if line[0] != "-":
                entry["new"], new = new, new + 1
            lines.append(entry)
    return files


def removal_categories(repo: str, edits: list[dict]) -> dict[tuple, list[str]]:
    """(path, line text) -> categories of the snapshots that deleted it, in order."""
    removed: dict[tuple, list[str]] = defaultdict(list)
    for record in edits:
        step = parse_diff(git(repo, "diff", "-U0", record["parent"], record["commit"]))
        for path, lines in step.items():
            for line in lines:
                if line["op"] == "-":
                    removed[(path, line["text"])].append(record["category"])
    return removed


def refactoring_ranges(refactorings: list[dict]) -> dict[str, list[tuple]]:
    """op + path -> [(start, end, description)] from RefactoringMiner output."""
    ranges: dict[str, list[tuple]] = defaultdict(list)
    for r in refactorings:
        for op, key in (("-", "leftSideLocations"), ("+", "rightSideLocations")):
            for loc in r.get(key) or []:
                ranges[op + loc["filePath"]].append(
                    (loc["startLine"], loc["endLine"], r["description"])
                )
    return ranges


def annotate_final_diff(
    repo: str, results: dict, edits: list[dict], rm_total: list[dict]
) -> tuple[dict[str, list[dict]], Counter]:
    """Label each changed line of start -> result with category and bucket."""
    start, result = results["start_commit"], results["result_commit"]
    files = parse_diff(git(repo, "diff", "-U3", "--no-renames", start, result))
    deleted = set(
        git(repo, "diff", "--name-only", "--diff-filter=D", start, result).split()
    )
    # The last record touching a file holds the labels of its final content.
    final_labels: dict[str, dict[int, str]] = {}
    for record in edits:
        for f in record["files"]:
            final_labels[f["path"]] = {
                int(k): v for k, v in f["attributed_lines"].items()
            }
    removed = removal_categories(repo, edits)
    ranges = refactoring_ranges(rm_total)
    counts: Counter = Counter()
    for path, lines in files.items():
        for line in lines:
            if line["op"] == "+":
                number = line["new"]
                category = final_labels.get(path, {}).get(number, "unattributed")
            elif line["op"] == "-":
                number = line["old"]
                removers = removed.get((path, line["text"]))
                category = removers.pop(0) if removers else "unattributed"
            else:
                continue
            refs = [
                d for s, e, d in ranges.get(line["op"] + path, []) if s <= number <= e
            ]
            if category == "refactoring_tool":
                bucket = "tool"
            elif refs:
                bucket = "explained"
            elif path in deleted:
                bucket = "deleted file"
            else:
                bucket = "residue"
            line.update(category=category, bucket=bucket, refs=refs)
            counts[bucket] += 1
    return files, counts


def render_diff_lines(lines: list[dict], color_by: str) -> str:
    rows = []
    for line in lines:
        if line["op"] == "@":
            rows.append(el("div", esc(line["text"]), cls="hunk"))
            continue
        old = "" if line["old"] is None else line["old"]
        new = "" if line["new"] is None else line["new"]
        text = el("span", f"{old:>4} {new:>4}", cls="ln") + esc(
            line["op"] + " " + line["text"]
        )
        if line["op"] == " ":
            rows.append(el("div", text, cls="ctx"))
        elif color_by == "bucket":
            label = CATEGORY_LABELS[line["category"]]
            title = "\n".join([f"{line['bucket']} / {label}", *line["refs"]])
            rows.append(el("div", text, cls=bucket_cls(line["bucket"]), title=title))
        else:
            rows.append(el("div", text, cls="c-" + line["category"]))
    return el("div", *rows, cls="diff")


def render_final_diff(files: dict[str, list[dict]]) -> str:
    out = []
    for path, lines in files.items():
        counts = Counter(ln["bucket"] for ln in lines if "bucket" in ln)
        summary = (
            el("code", esc(path))
            + " "
            + "".join(
                tag(f"{b} {counts[b]}", bucket_cls(b)) for b in BUCKETS if counts[b]
            )
        )
        out.append(
            details(
                summary,
                render_diff_lines(lines, "bucket"),
                is_open=bool(counts["residue"]),
            )
        )
    return "".join(out)


# --- verdicts ----------------------------------------------------------------


def oracle_rows(run_dir: Path, oracle: dict) -> list[tuple[str, str]]:
    detail = (
        f"{oracle.get('results', 0)} parse results compared, "
        f"{oracle.get('errors_in_baseline', 0)} of them errors in the start state"
    )
    rows = [
        (
            "Oracle output identical",
            verdict(oracle["identical"]) + " " + el("span", esc(detail), cls="muted"),
        )
    ]
    hist_before = run_dir / "baseline" / "oracle" / "histogram.tsv"
    hist_after = run_dir / "final" / "oracle" / "histogram.tsv"
    if hist_before.exists():
        before = hist_before.read_text().strip().replace("\t", ": ")
        after = (
            hist_after.read_text().strip().replace("\t", ": ")
            if (hist_after.exists())
            else "missing"
        )
        cell = pre(before)
        if after != before:
            cell += el("span", "differs after:", cls="bad") + pre(after)
        rows.append(("Oracle coverage (items built)", cell))
    if oracle.get("first_changes"):
        changes = "\n".join(f"- {b}\n+ {a}" for b, a in oracle["first_changes"])
        rows.append(("First oracle differences", pre(changes)))
    return rows


def removed_public_declarations(files: dict[str, list[dict]]) -> list[str]:
    """Removed `public ...` lines whose text does not reappear anywhere."""
    added = {
        ln["text"].strip() for ls in files.values() for ln in ls if ln["op"] == "+"
    }
    return [
        f"{path}: {ln['text'].strip()}"
        for path, lines in files.items()
        for ln in lines
        if ln["op"] == "-"
        and PUBLIC_DECL_RE.match(ln["text"])
        and ln["text"].strip() not in added
    ]


def ide_sync_cell(ide_sync: dict | None) -> str:
    if ide_sync is None:
        return el("span", "not checked (run predates the check)", cls="muted")
    detail = (
        f"ready after {ide_sync['ready_after_s']}s; external edits seen after "
        f"{ide_sync['external_edit_lag_s']}s"
    )
    return verdict(ide_sync["ok"]) + " " + el("span", esc(detail), cls="muted")


def render_verdicts(run_dir: Path, results: dict, files: dict, counts: Counter) -> str:
    repo, result = results["repo"], results["result_commit"]
    status = results["execution_status"]
    rows = [
        ("Agent finished", verdict(status == "finished", status, status)),
        ("IDE refactors this clone", ide_sync_cell(results.get("ide_sync"))),
        (
            "Builds (start / result)",
            verdict(results["baseline"]["build_ok"])
            + " / "
            + verdict(results["final"]["build_ok"]),
        ),
    ]
    if results["oracle"] is not None:
        rows += oracle_rows(run_dir, results["oracle"])

    target = results["target"]
    names = {target["class_name"]} | {Path(p).stem for p in target["file_paths"]}
    for name in sorted(names) if result else []:
        hits = git(repo, "grep", "-n", "-w", name, result, "--", "*.java").strip()
        cell = verdict(not hits, "none") + (pre(hits) if hits else "")
        rows.append((f"References to {el('code', esc(name))} left", cell))

    test_files = [p for p in files if TEST_PATH_RE.search(p)]
    rows.append(
        ("Test files changed", verdict(not test_files, "none", ", ".join(test_files)))
    )
    removed_public = removed_public_declarations(files)
    rows.append(
        (
            "Public declarations removed",
            el("span", str(len(removed_public)), cls="warn")
            + pre("\n".join(removed_public))
            if removed_public
            else "none",
        )
    )

    total = sum(counts.values())
    split = ", ".join(f"{b}: {counts[b]}" for b in BUCKETS)
    residue = f"{counts['residue'] / total:.0%}" if total else "-"
    rows.append(
        (
            "Changed lines by review bucket",
            f"{split} (total {total}; " + el("b", f"residue {residue}") + ")",
        )
    )
    net = ", ".join(
        f"{CATEGORY_LABELS[c]}: +{v['inserted']}/-{v['deleted']}"
        for c, v in results["net_lines"].items()
    )
    rows += [
        ("Net lines by tool category", net),
        ("Refactoring-tool line share", esc(results["tool_line_share"])),
        (
            "Tool calls (IDE refactoring / failed)",
            f"{results['tool_calls']} ({results['refactoring_tool_calls']}"
            f" / {results['failed_tool_calls']})",
        ),
        ("Guardrail nudges", esc(results["nudges"])),
        (
            "Cost / duration",
            f"${results['llm_cost']:.3f} / {results['duration_s']:.0f}s",
        ),
        (
            "Result branch",
            el("code", esc(results["result_branch"]))
            + " "
            + el(
                "span",
                esc(f"({results['start_commit'][:10]} → {str(result)[:10]})"),
                cls="muted",
            ),
        ),
    ]
    return table(rows)


# --- plan --------------------------------------------------------------------


def plan_snapshots(events: list[dict]) -> Plans:
    """(event index, task list) for every task-tracker plan update."""
    plans = []
    for i, e in enumerate(events):
        if e["kind"] == "ActionEvent" and e["tool_name"] == "task_tracker":
            action = e["action"]
            if action["command"] == "plan":
                plans.append((i, action["task_list"]))
    return plans


def active_task(plans: Plans, index: int) -> str | None:
    """Title of the plan item in progress at event ``index``."""
    current = None
    for i, tasks in plans:
        if i > index:
            break
        current = next(
            (t["title"] for t in tasks if t["status"] == "in_progress"), None
        )
    return current


def render_plan(events: list[dict], plans: Plans) -> str:
    if not plans:
        return el("p", "The agent never recorded a plan.", cls="bad")
    first, last = plans[0][1], plans[-1][1]
    per_task: dict[str, Counter] = defaultdict(Counter)
    for i, e in enumerate(events):
        if e["kind"] == "ActionEvent" and e["tool_name"] != "task_tracker":
            per_task[active_task(plans, i) or NO_TASK][e["tool_name"]] += 1

    def tool_counts(title: str) -> str:
        return esc(", ".join(f"{n}×{c}" for n, c in per_task.get(title, {}).items()))

    status_cls = {"done": "ok", "in_progress": "warn", "todo": "bad"}
    rows = [
        el(
            "tr",
            el("th", "Plan item (final)"),
            el("th", "Status"),
            el("th", "Tool calls"),
        )
    ]
    for t in last:
        item = esc(t["title"]) + el("div", esc(t["notes"]), cls="muted")
        rows.append(
            el(
                "tr",
                el("td", item),
                el("td", esc(t["status"]), cls=status_cls[t["status"]]),
                el("td", tool_counts(t["title"])),
            )
        )
    if NO_TASK in per_task:
        rows.append(
            el(
                "tr",
                el("td", NO_TASK, cls="muted"),
                el("td"),
                el("td", tool_counts(NO_TASK)),
            )
        )
    out = el("p", f"{len(plans)} plan updates.", cls="muted") + el("table", *rows)
    if [t["title"] for t in first] != [t["title"] for t in last]:
        initial = el("ol", *(el("li", esc(t["title"])) for t in first))
        out += details("Initial plan (items changed later)", initial)
    return out


# --- timeline ----------------------------------------------------------------


def assign_by_time(events: list[dict], records: list[dict]) -> dict[int, list[dict]]:
    """Attach each log record to the last ActionEvent created before it."""
    actions = [
        (to_utc(e["timestamp"]), i)
        for i, e in enumerate(events)
        if e["kind"] == "ActionEvent"
    ]
    attached: dict[int, list[dict]] = defaultdict(list)
    for record in records:
        when = to_utc(record["timestamp"])
        before = [i for t, i in actions if t <= when]
        if before:
            attached[before[-1]].append(record)
    return attached


def render_edit(repo: str, record: dict) -> str:
    step = parse_diff(git(repo, "diff", "-U2", record["parent"], record["commit"]))
    for lines in step.values():
        for line in lines:
            line["category"] = record["category"]
    stat = ", ".join(
        f"{f['path'].rsplit('/', 1)[-1]} +{f['insertions']}/-{f['deletions']}"
        for f in record["files"]
    )
    summary = tag(CATEGORY_LABELS[record["category"]], "c-" + record["category"])
    summary += esc(f"edit #{record['seq']}: {stat}")
    body = [
        el("div", el("code", esc(p)), render_diff_lines(ls, "category"))
        for p, ls in step.items()
    ]
    return details(summary, *body)


def render_action(n: int, e: dict, task: str | None, extras: list[str]) -> str:
    header = el("b", esc(f"#{n} {e['tool_name']}"))
    if task:
        header += " " + tag(task, "task")
    header += " " + el("span", esc(e["timestamp"][11:19]), cls="muted")
    parts = [header]
    thought = "\n".join(t["text"] for t in e["thought"])
    if thought:
        parts.append(pre(thought))
    if e.get("reasoning_content"):
        parts.append(details("reasoning", pre(e["reasoning_content"])))
    args = e["tool_call"]["arguments"]
    try:
        args = json.dumps(json.loads(args), indent=1)
    except json.JSONDecodeError:
        pass  # malformed arguments from the LLM; show them raw
    parts.append(details("arguments", pre(args)))
    return el("div", *parts, *extras, cls="ev ev-action")


def render_timeline(
    repo: str, events: list[dict], edits: list[dict], steps: list[dict], plans: Plans
) -> str:
    edits_at = assign_by_time(events, edits)
    mined_at = assign_by_time(events, [s for s in steps if s.get("refactorings")])
    out = []
    n = 0
    for i, e in enumerate(events):
        kind = e["kind"]
        if kind == "MessageEvent":
            text = "\n".join(c.get("text", "") for c in e["llm_message"]["content"])
            title, cls = {
                "environment": ("Guardrail nudge", "ev-nudge"),
                "user": ("Prompt", "ev-user"),
            }.get(e["source"], ("Agent message", "ev-agent"))
            out.append(el("div", el("b", title), pre(text), cls=f"ev {cls}"))
        elif kind == "ActionEvent":
            n += 1
            extras = [render_edit(repo, r) for r in edits_at.get(i, [])]
            for record in mined_at.get(i, []):
                found = el(
                    "ul",
                    *(el("li", esc(r["description"])) for r in record["refactorings"]),
                )
                label = f"RefactoringMiner, agent step {record['step']}:"
                extras.append(el("div", esc(label), found, cls="refs"))
            out.append(render_action(n, e, active_task(plans, i), extras))
        elif kind == "ObservationEvent":
            obs = e["observation"]
            text = "\n".join(c["text"] for c in obs["content"] if c["type"] == "text")
            if len(text) > OBS_PREVIEW_CHARS:
                text = text[:OBS_PREVIEW_CHARS] + "\n…"
            label = (
                el("span", "error", cls="bad") if obs.get("is_error") else "observation"
            )
            out.append(details(label, pre(text), cls="ev"))
        elif kind in ("AgentErrorEvent", "ConversationErrorEvent"):
            text = e.get("error") or e.get("detail") or ""
            out.append(el("div", el("b", kind), pre(text), cls="ev ev-error"))
    return "".join(out)


# --- page --------------------------------------------------------------------


def render(run_dir: Path) -> Path:
    results = json.loads((run_dir / "results.json").read_text())
    events = json.loads((run_dir / "events.json").read_text())
    session = results["session_id"]
    edits = read_jsonl(run_dir / "edit_attribution.jsonl", session)
    steps = read_jsonl(run_dir / "refactoring_log.jsonl", session)
    rm_path = run_dir / "rm_total.json"
    rm_total = json.loads(rm_path.read_text()) if rm_path.exists() else []
    repo = results["repo"]

    files: dict[str, list[dict]] = {}
    counts: Counter = Counter()
    if results["result_commit"]:
        files, counts = annotate_final_diff(repo, results, edits, rm_total)
    plans = plan_snapshots(events)
    page = Template(TEMPLATE_PATH.read_text()).substitute(
        title=esc(f"{results['task_id']} {results['run_id']}"),
        heading=esc(f"Remove {results['pattern']}: {results['task_id']}"),
        subheading=esc(
            f"run {results['run_id']}, model {results['model']}, repo {repo}"
        ),
        verdicts=render_verdicts(run_dir, results, files, counts),
        plan=render_plan(events, plans),
        refactorings=el("ul", *(el("li", esc(r["description"])) for r in rm_total))
        if rm_total
        else el("p", "none"),
        legend="".join(tag(b, bucket_cls(b)) for b in BUCKETS),
        final_diff=render_final_diff(files),
        timeline=render_timeline(repo, events, edits, steps, plans),
    )
    out = run_dir / "report.html"
    out.write_text(page)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    print(render(args.run_dir))


if __name__ == "__main__":
    main()
