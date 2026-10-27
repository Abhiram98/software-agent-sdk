"""Headless tool-composition eval harness.

Each task in ``tasks.json`` is a refactoring that no single IntelliJ MCP tool
completes, but a short sequence of them does (see PROPOSALS.md). The agent
gets the task prompt only; nothing tells it which tools to use.

The main measurement is how many of the lines the session changed were
changed by refactoring tools rather than by direct edits (the file editor,
shell commands). Lines are attributed by the SDK edit ledger
(``openhands.sdk.conversation.edit_attribution``), which snapshots the
workspace around every tool call:

- ``net_lines``: lines inserted/deleted in the final diff against base_sha,
  per category (``refactoring_tool``, ``direct_edit``, ``unattributed``).
  ``tool_line_share`` is the refactoring-tool share of those lines.
- ``written_lines``: every line ever inserted or deleted per category,
  including work that was later reverted.

Also recorded: every tool call by name with its outcome (refactoring tools
report precondition conflicts as ``success: false`` results, which count as
failed calls), whether the regex ``checks`` of the task pass on the final
tree, and optionally whether the project still compiles (``--build``).

Each task starts from the ``base_sha`` pinned in ``manifest.json``, on a
throwaway branch, because prompts reference line numbers of that commit.

Prerequisites:
- ``OPENROUTER_API_KEY`` set (model: ``EVAL_MODEL``)
- IntelliJ IDEA OSS built from intellij-refactoring-fork; the harness opens
  the project and waits for the IDE MCP proxy (``INTELLIJ_MCP_URL``)

Usage:
    uv run python refactoring_evals/tool_composition/run_eval.py \\
        [--tasks extract-move-01-columnsinfo-numericstats ...] \\
        [--driver goal|precondition] [--repeats 1] [--hint] [--build] \\
        [--max-iterations 100] [--mcp-verbosity terse]
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

from pydantic import SecretStr

from openhands.sdk import LLM, Agent, get_logger
from openhands.sdk.conversation.edit_attribution import (
    DIRECT_EDIT,
    EDIT_ATTRIBUTION_LOG_ENV_VAR,
    REFACTORING_TOOL,
    REFACTORING_TOOLS_ENV_VAR,
    UNATTRIBUTED,
    categorize_tool,
    net_line_attribution,
)
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.event import ActionEvent, AgentErrorEvent, ObservationEvent
from openhands.sdk.mcp import MCPServer
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool


logger = get_logger(__name__)

EVAL_DIR = Path(__file__).parent
TASKS_PATH = EVAL_DIR / "tasks.json"
MANIFEST_PATH = EVAL_DIR / "manifest.json"
ENV_PATH = EVAL_DIR.parent / ".env"
REPOS_ROOT = Path("/Users/abhiram/Documents/TBE/evaluation_projects")
EVAL_BRANCH = "tool-composition-eval"
IDEA_APP = "IntelliJ IDEA OSS.app"
MODEL = os.environ.get("EVAL_MODEL", "openrouter/~deepseek/deepseek-v4-flash-latest")
MCP_URL = os.environ.get("INTELLIJ_MCP_URL", "http://127.0.0.1:64343/sse")
PROJECT_PATH_HEADER = "IJ_MCP_SERVER_PROJECT_PATH"
DEFAULT_BUILD_CMD = "mvn -q -o -pl main compile -DskipTests"

# Refactoring tools exposed by intellij-refactoring-fork. The ledger's built-in
# default set lacks the field-move tools, so the full set is configured here.
IDE_REFACTORING_TOOLS = (
    "extract_method",
    "inline_method",
    "introduce_parameter",
    "move_method",
    "move_static_field",
    "move_instance_field",
    "pull_up_method",
    "push_down_method",
    "rename_refactoring",
    "safe_delete",
)
CATEGORIES = (REFACTORING_TOOL, DIRECT_EDIT, UNATTRIBUTED)
# The IDE proxy also exposes `execute_tool`, which dispatches to any IDE tool by
# name. Edits are attributed by the outer tool name, so refactorings run through
# it would count as direct edits; hide it so the named tools are used instead.
HIDDEN_TOOLS_REGEX = r"^(?!execute_tool$)"
HINT = (
    "\n\nThe IDE refactoring tools are available; prefer them over editing "
    "code by hand where they apply."
)
FAILED_RESULT_RE = re.compile(r'"success"\s*:\s*false')

MCP_VERBOSITY_FILE = Path.home() / ".mcp-description-variant.txt"
MCP_VERBOSITY_LEVELS = ("terse", "medium", "verbose")


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def is_dirty(repo: Path) -> bool:
    return bool(run_git(repo, "status", "--porcelain"))


def prepare_repo(repo: Path, base_sha: str) -> bool:
    """Put the repo on a clean throwaway branch at base_sha.

    Returns whether uncommitted changes had to be stashed first.
    """
    stashed = is_dirty(repo)
    if stashed:
        logger.warning("%s has uncommitted changes; stashing them", repo)
        run_git(repo, "stash", "push", "-u", "-m", "tool-composition-eval stash")
    run_git(repo, "checkout", "-B", EVAL_BRANCH, base_sha)
    run_git(repo, "clean", "-fd")
    return stashed


def restore_repo(repo: Path, branch: str, stashed: bool) -> None:
    """Return to the branch the run started on and re-apply stashed changes."""
    run_git(repo, "reset", "--hard")
    run_git(repo, "clean", "-fd")
    run_git(repo, "checkout", branch)
    if stashed:
        run_git(repo, "stash", "pop")


def commit_changes(repo: Path, message: str) -> str | None:
    """Commit the working tree, returning the commit sha (None if no diff)."""
    if not is_dirty(repo):
        return None
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def set_mcp_verbosity(level: str | None) -> None:
    """Set MCP tool description verbosity, or clear it to use the IDE default."""
    if level is None:
        MCP_VERBOSITY_FILE.unlink(missing_ok=True)
    else:
        MCP_VERBOSITY_FILE.write_text(level)


def ensure_idea_ready(repo: Path, timeout_s: float = 180.0) -> None:
    """Open the project in the IDE and wait until its MCP proxy answers."""
    subprocess.run(
        ["open", "-na", IDEA_APP, "--args", str(repo)], check=True, timeout=60
    )
    logger.info("Waiting for IDE MCP server at %s ...", MCP_URL)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(MCP_URL, timeout=5).close()
            return
        except urllib.error.HTTPError:
            return  # proxy answered (non-200); it is up
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
    raise RuntimeError(f"MCP server at {MCP_URL} not reachable after {timeout_s}s")


def load_env(path: Path = ENV_PATH) -> None:
    """Populate os.environ from a KEY=VALUE .env file (never overrides)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def build_agent(repo: Path) -> tuple[Agent, LLM]:
    load_env()
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"Set OPENROUTER_API_KEY (or LLM_API_KEY) in the environment "
            f"or in {ENV_PATH} before running."
        )
    llm = LLM(usage_id="agent", model=MODEL, api_key=SecretStr(api_key))
    agent = Agent(
        llm=llm,
        tools=[Tool(name=TerminalTool.name), Tool(name=FileEditorTool.name)],
        filter_tools_regex=HIDDEN_TOOLS_REGEX,
        mcp_config={
            "intellij": MCPServer(
                url=MCP_URL,
                transport="sse",
                headers={PROJECT_PATH_HEADER: SecretStr(str(repo))},
            )
        },
    )
    return agent, llm


def read_jsonl(path: Path, session_id: str) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    return [r for r in records if r.get("session_id") == session_id]


def tool_calls(conversation: LocalConversation) -> list[dict[str, Any]]:
    """Every tool call of the session in order, with its category and outcome."""
    calls: dict[str, dict[str, Any]] = {}
    for event in conversation.state.events:
        if isinstance(event, ActionEvent):
            calls[event.tool_call_id] = {
                "tool": event.tool_name,
                "category": categorize_tool(event.tool_name),
                "ok": None,
            }
        elif isinstance(event, ObservationEvent) and event.tool_call_id in calls:
            obs = event.observation
            failed = obs.is_error or bool(FAILED_RESULT_RE.search(obs.text))
            calls[event.tool_call_id]["ok"] = not failed
        elif isinstance(event, AgentErrorEvent) and event.tool_call_id in calls:
            calls[event.tool_call_id]["ok"] = False
    return list(calls.values())


def written_lines(records: list[dict]) -> dict[str, int]:
    """Lines ever inserted or deleted per category, including reverted work."""
    totals = Counter()
    for record in records:
        for f in record["files"]:
            totals[record["category"]] += f["insertions"] + f["deletions"]
    return {c: totals.get(c, 0) for c in (REFACTORING_TOOL, DIRECT_EDIT)}


def tool_line_share(net: dict[str, dict[str, int]]) -> float | None:
    changed = {c: net[c]["inserted"] + net[c]["deleted"] for c in CATEGORIES}
    total = sum(changed.values())
    return round(changed[REFACTORING_TOOL] / total, 3) if total else None


def run_checks(repo: Path, task: dict[str, Any], base_sha: str) -> list[dict]:
    results = []
    for check in task["checks"]:
        path = repo / check["path"]
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if "present" in check:
            passed = re.search(check["present"], text) is not None
        elif "absent" in check:
            passed = re.search(check["absent"], text) is None
        else:
            diff = run_git(repo, "diff", "--name-only", base_sha, "--", check["path"])
            passed = not diff
        results.append({**check, "passed": passed})
    return results


def run_build(repo: Path, command: str, run_dir: Path, label: str) -> bool:
    result = subprocess.run(
        shlex.split(command), cwd=repo, capture_output=True, text=True, timeout=1800
    )
    (run_dir / f"{label}.build.log").write_text(result.stdout + result.stderr)
    return result.returncode == 0


def run_one(
    repo: Path,
    task: dict[str, Any],
    base_sha: str,
    run_dir: Path,
    args: argparse.Namespace,
    repeat: int,
) -> dict[str, Any]:
    prepare_repo(repo, base_sha)
    label = f"{task['id']}__r{repeat}"
    edit_log = run_dir / "edit_attribution.jsonl"
    os.environ[EDIT_ATTRIBUTION_LOG_ENV_VAR] = str(edit_log)
    os.environ[REFACTORING_TOOLS_ENV_VAR] = ",".join(IDE_REFACTORING_TOOLS)
    prompt = task["prompt"] + (HINT if args.hint else "")

    logger.info("%s: %s", label, prompt)
    agent, llm = build_agent(repo)
    conversation = LocalConversation(
        agent=agent,
        workspace=repo,
        persistence_dir=None,  # keep the eval repo tree clean
        max_iteration_per_run=args.max_iterations,
    )
    started = time.monotonic()
    conversation.send_message(prompt)
    conversation.run()
    duration_s = time.monotonic() - started

    commit_sha = commit_changes(repo, f"tool-composition eval: {label}")
    session_id = str(conversation.id)
    records = read_jsonl(edit_log, session_id)
    net = net_line_attribution(str(repo), records, base_sha, commit_sha)
    calls = tool_calls(conversation)
    checks = run_checks(repo, task, base_sha)
    built = run_build(repo, args.build_cmd, run_dir, label) if args.build else None
    return {
        "task_id": task["id"],
        "repeat": repeat,
        "category": task["category"],
        "composition_driver": task["composition_driver"],
        "hint": args.hint,
        "model": MODEL,
        "session_id": session_id,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "diff_shortstat": (
            run_git(repo, "diff", "--shortstat", base_sha, commit_sha)
            if commit_sha
            else ""
        ),
        "net_lines": net,
        "tool_line_share": tool_line_share(net),
        "written_lines": written_lines(records),
        "tool_calls": calls,
        "refactoring_calls": sum(c["category"] == REFACTORING_TOOL for c in calls),
        "failed_refactoring_calls": sum(
            c["category"] == REFACTORING_TOOL and c["ok"] is False for c in calls
        ),
        "min_known_refactoring_calls": min(len(s) for s in task["known_solutions"]),
        "checks": checks,
        "checks_passed": all(c["passed"] for c in checks),
        "build_ok": built,
        "llm_cost": llm.metrics.accumulated_cost,
        "duration_s": round(duration_s, 1),
    }


def summarize(results: list[dict[str, Any]]) -> str:
    def lines(r: dict, category: str) -> int:
        return (
            r["net_lines"][category]["inserted"] + r["net_lines"][category]["deleted"]
        )

    header = (
        f"{'task':<58} {'tool/direct/unattr lines':>24} {'share':>6} "
        f"{'ref calls (fail/min)':>21} {'checks':>6} {'build':>5}"
    )
    rows = [header, "-" * len(header)]
    for r in results:
        split = "/".join(str(lines(r, c)) for c in CATEGORIES)
        share = "-" if r["tool_line_share"] is None else f"{r['tool_line_share']:.2f}"
        calls = (
            f"{r['refactoring_calls']} ({r['failed_refactoring_calls']}/"
            f"{r['min_known_refactoring_calls']})"
        )
        build = {None: "-", True: "ok", False: "FAIL"}[r["build_ok"]]
        rows.append(
            f"{r['task_id'] + ' r' + str(r['repeat']):<58} {split:>24} {share:>6} "
            f"{calls:>21} {'ok' if r['checks_passed'] else 'FAIL':>6} {build:>5}"
        )
    tool = sum(lines(r, REFACTORING_TOOL) for r in results)
    total = sum(lines(r, c) for r in results for c in CATEGORIES)
    passed = sum(r["checks_passed"] for r in results)
    rows.append("")
    rows.append(
        f"overall: {tool}/{total} changed lines via refactoring tools "
        f"({tool / total:.0%})"
        if total
        else "overall: no lines changed"
    )
    rows.append(f"checks passed: {passed}/{len(results)}")
    return "\n".join(rows)


class _Tee:
    def __init__(self, *streams: IO[str]) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._streams[0], name)


def tee_output_to(run_dir: Path) -> None:
    """Mirror stdout/stderr into run_dir/run.log for post-hoc inspection."""
    log_file = (run_dir / "run.log").open("a")
    sys.stdout = cast(Any, _Tee(sys.stdout, log_file))
    sys.stderr = cast(Any, _Tee(sys.stderr, log_file))


def select_tasks(tasks: list[dict], args: argparse.Namespace) -> list[dict]:
    if args.driver:
        tasks = [t for t in tasks if t["composition_driver"] == args.driver]
    if args.tasks:
        unknown = set(args.tasks) - {t["id"] for t in tasks}
        if unknown:
            raise RuntimeError(f"Unknown task ids: {sorted(unknown)}")
        tasks = [t for t in tasks if t["id"] in set(args.tasks)]
    if not tasks:
        raise RuntimeError("No tasks selected")
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="OpenRefine")
    parser.add_argument("--tasks", nargs="*", help="task ids to run")
    parser.add_argument("--driver", choices=("goal", "precondition"))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--hint", action="store_true", help="append a tool hint")
    parser.add_argument("--build", action="store_true", help="compile after each run")
    parser.add_argument("--build-cmd", default=DEFAULT_BUILD_CMD)
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--mcp-verbosity", choices=MCP_VERBOSITY_LEVELS)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    raw = json.loads(TASKS_PATH.read_text())
    tasks = select_tasks(raw[args.project], args)
    base_sha = json.loads(MANIFEST_PATH.read_text())[args.project]["base_sha"]
    repo = REPOS_ROOT / args.project
    if not repo.is_dir():
        raise RuntimeError(f"Project repo not found: {repo}")

    run_dir = EVAL_DIR / "results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tee_output_to(run_dir)
    set_mcp_verbosity(args.mcp_verbosity)
    ensure_idea_ready(repo)

    results: list[dict[str, Any]] = []
    start_branch = run_git(repo, "branch", "--show-current")
    if not start_branch or start_branch == EVAL_BRANCH:
        raise RuntimeError(
            f"Start the run from a named branch other than {EVAL_BRANCH}"
        )
    stashed = prepare_repo(repo, base_sha)
    try:
        for task in tasks:
            for repeat in range(args.repeats):
                results.append(run_one(repo, task, base_sha, run_dir, args, repeat))
                (run_dir / "results.json").write_text(
                    json.dumps(results, indent=2) + "\n"
                )
    finally:
        restore_repo(repo, start_branch, stashed)

    print(summarize(results))
    print(f"\nResults: {run_dir / 'results.json'}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
