"""Headless move-refactoring eval harness.

Companion to ``refactoring_evals/extract_method``, for refactorings whose two
halves need not land in the same agent step: moving a method or field from one
class to another, and moving a declaration up or down a hierarchy.

Each task in ``tasks.json`` names a concrete member, its source class and its
destination class, the refactoring family RefactoringMiner should report, and
the IntelliJ MCP tool that performs the move. Every task's reference solution
was applied by hand and mined, and the observed RefactoringMiner type string is
recorded in ``verified_type``, so a zero in the results means the agent did not
produce the refactoring rather than that the detector cannot see it.

Two things differ from the extract-method harness and matter for the numbers:

- ``OPENHANDS_REFACTORING_TOOLS`` is set here. The edit ledger's default
  refactoring-tool set is ``{"extract_method"}``, so without this the move
  tools would be attributed as direct edits.
- Field moves have no IntelliJ MCP tool (``ide_tool: null``). The agent cannot
  route them through the IDE even in principle, so they act as a control: a
  guardrail should not demand a tool that does not exist.

Mining: the existing per-step miner only ever reports refactorings whose
source and destination land in the same step, and the existing nudge only
matches "Extract Method". ``--mode guardrail`` is therefore expected to behave
identically to ``--mode baseline`` until multi-step detection lands; the
harness warns when that mode is selected.

Prerequisites:
- ``OPENROUTER_API_KEY`` set (model: ``EVAL_MODEL``)
- RefactoringMiner available (default path from
  ``openhands.sdk.conversation.refactoring_miner``)
- IntelliJ IDEA OSS installed; the harness opens the project and waits for
  the IDE MCP proxy (``INTELLIJ_MCP_URL``, default 127.0.0.1:64343/sse)

Usage:
    uv run python refactoring_evals/move_code/run_eval.py \\
        [--project OpenRefine] [--tasks static-01-stringutils-tostring ...] \\
        [--category "static method"] [--mode baseline] [--max-iterations 100] \\
        [--mcp-verbosity terse]
"""

import argparse
import json
import os
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
    REFACTORING_TOOL,
    UNATTRIBUTED,
    net_line_attribution,
)
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
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
BRANCH = "master"
UPSTREAM_REF = "origin/master"
IDEA_APP = "IntelliJ IDEA OSS.app"
MODEL = os.environ.get("EVAL_MODEL", "openrouter/~deepseek/deepseek-v4-flash-latest")
MCP_URL = os.environ.get("INTELLIJ_MCP_URL", "http://127.0.0.1:64343/sse")
PROJECT_PATH_HEADER = "IJ_MCP_SERVER_PROJECT_PATH"

NUDGE_ENV_VAR = "OPENHANDS_REFACTORING_NUDGE"
EDIT_LOG_ENV_VAR = "OPENHANDS_EDIT_ATTRIBUTION_LOG"
MINER_LOG_ENV_VAR = "OPENHANDS_REFACTORING_LOG"
REFACTORING_TOOLS_ENV_VAR = "OPENHANDS_REFACTORING_TOOLS"
MODE_NUDGE = {"baseline": "0", "guardrail": "1"}

# IDE refactoring tools that count as a tool-applied edit rather than a direct
# edit. The ledger's built-in default covers extract_method only.
MOVE_IDE_TOOLS = (
    "extract_method",
    "move_method",
    "pull_up_method",
    "push_down_method",
    "inline_method",
    "introduce_parameter",
    "rename_refactoring",
    "safe_delete",
)

# RefactoringMiner type strings grouped by the move they represent. A single
# logical move can surface under any type in its family -- renaming a method
# while moving it turns "Move Method" into "Move And Rename Method" -- so
# detection is checked per family, not per type string.
MOVE_FAMILIES: dict[str, frozenset[str]] = {
    "move_method": frozenset(
        {
            "Move Method",
            "Move And Rename Method",
            "Extract And Move Method",
            "Move And Inline Method",
        }
    ),
    "pull_up_method": frozenset({"Pull Up Method"}),
    "push_down_method": frozenset({"Push Down Method"}),
    "move_attribute": frozenset({"Move Attribute", "Move And Rename Attribute"}),
    "pull_up_attribute": frozenset({"Pull Up Attribute"}),
    "push_down_attribute": frozenset({"Push Down Attribute"}),
}
ALL_MOVE_TYPES = frozenset().union(*MOVE_FAMILIES.values())

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


def set_mcp_verbosity(level: str | None) -> None:
    """Set MCP tool description verbosity, or clear it to use the IDE default."""
    if level is None:
        if MCP_VERBOSITY_FILE.exists():
            MCP_VERBOSITY_FILE.unlink()
            logger.info("Removed MCP verbosity control file (using IDE default)")
    else:
        MCP_VERBOSITY_FILE.write_text(level)
        logger.info("Set MCP verbosity to '%s'", level)


def prepare_repo(repo: Path) -> str:
    """Force the repo onto a clean BRANCH and return the base sha."""
    if is_dirty(repo):
        logger.warning(
            "%s has uncommitted changes; stashing them before checkout", repo
        )
        run_git(repo, "stash", "push", "-u", "-m", "move-eval auto-stash")
    run_git(repo, "checkout", BRANCH)
    run_git(repo, "reset", "--hard", UPSTREAM_REF)
    run_git(repo, "clean", "-fd")
    return run_git(repo, "rev-parse", "HEAD")


def commit_changes(repo: Path, message: str) -> str | None:
    """Commit the working tree, returning the commit sha (None if no diff)."""
    if not is_dirty(repo):
        return None
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def open_idea_project(repo: Path) -> None:
    subprocess.run(
        ["open", "-na", IDEA_APP, "--args", str(repo)], check=True, timeout=60
    )


def wait_for_mcp_server(url: str, timeout_s: float = 180.0) -> None:
    """Block until the IDE MCP proxy answers, so the move tools are reachable."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5).close()
            return
        except urllib.error.HTTPError:
            return  # proxy answered (non-200); it is up
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2)
    raise RuntimeError(f"MCP server at {url} not reachable after {timeout_s}s")


def ensure_idea_ready(repo: Path) -> None:
    open_idea_project(repo)
    logger.info("Waiting for IDE MCP server at %s ...", MCP_URL)
    wait_for_mcp_server(MCP_URL)


def build_mcp_config(repo: Path) -> dict[str, MCPServer]:
    return {
        "intellij": MCPServer(
            url=MCP_URL,
            transport="sse",
            headers={PROJECT_PATH_HEADER: SecretStr(str(repo))},
        )
    }


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
        mcp_config=build_mcp_config(repo),
    )
    return agent, llm


def run_agent(
    repo: Path, prompt: str, max_iterations: int
) -> tuple[LocalConversation, LLM]:
    agent, llm = build_agent(repo)
    conversation = LocalConversation(
        agent=agent,
        workspace=repo,
        persistence_dir=None,  # keep eval repo tree clean
        max_iteration_per_run=max_iterations,
    )
    conversation.send_message(prompt)
    conversation.run()
    return conversation, llm


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def collect_tool_calls(records: list[dict]) -> dict[str, int]:
    """Tool-call counts per attribution category for one session."""
    calls = Counter(r["category"] for r in records)
    return {
        REFACTORING_TOOL: calls.get(REFACTORING_TOOL, 0),
        DIRECT_EDIT: calls.get(DIRECT_EDIT, 0),
    }


def mined_move_refactorings(mined: list[dict]) -> list[dict]:
    """Every move-family refactoring the miner logged for the session."""
    return [
        x
        for r in mined
        for x in r.get("refactorings", [])
        if x.get("type") in ALL_MOVE_TYPES
    ]


def expected_detected(task: dict[str, Any], moves: list[dict]) -> bool:
    """Whether the task's own refactoring family turned up in the mined set."""
    family = MOVE_FAMILIES[task["family"]]
    member = str(task["member"]).split(".")[-1].split(" ")[0]
    return any(
        m["type"] in family and member in str(m.get("description", "")) for m in moves
    )


def run_one(
    repo: Path,
    task: dict[str, Any],
    mode: str,
    run_dir: Path,
    max_iterations: int,
) -> dict[str, Any]:
    base_sha = prepare_repo(repo)
    os.environ[NUDGE_ENV_VAR] = MODE_NUDGE[mode]
    os.environ[EDIT_LOG_ENV_VAR] = str(run_dir / "edit_attribution.jsonl")
    os.environ[MINER_LOG_ENV_VAR] = str(run_dir / "refactoring_log.jsonl")
    os.environ[REFACTORING_TOOLS_ENV_VAR] = ",".join(MOVE_IDE_TOOLS)

    logger.info("[%s] %s: %s", mode, task["id"], task["prompt"])
    started = time.monotonic()
    conversation, llm = run_agent(repo, task["prompt"], max_iterations)
    duration_s = time.monotonic() - started
    commit_sha = commit_changes(
        repo, f"move eval: {task['id']} ({mode})\n\n{task['prompt']}"
    )
    session_id = str(conversation.id)
    records = [
        r
        for r in read_jsonl(run_dir / "edit_attribution.jsonl")
        if r.get("session_id") == session_id
    ]
    mined = [
        r
        for r in read_jsonl(run_dir / "refactoring_log.jsonl")
        if r.get("session_id") == session_id
    ]
    moves = mined_move_refactorings(mined)
    net_lines = net_line_attribution(str(repo), records, base_sha, commit_sha)
    diff = run_git(repo, "diff", "--shortstat", base_sha, "HEAD") if commit_sha else ""
    return {
        "project": repo.name,
        "task_id": task["id"],
        "category": task["category"],
        "member": task["member"],
        "family": task["family"],
        "ide_tool": task["ide_tool"],
        "prompt": task["prompt"],
        "mode": mode,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "session_id": session_id,
        "model": MODEL,
        "llm_cost": llm.metrics.accumulated_cost,
        "duration_s": round(duration_s, 1),
        "diff_shortstat": diff,
        "tool_calls_by_category": collect_tool_calls(records),
        "net_lines_by_category": net_lines,
        "expected_move_detected": expected_detected(task, moves),
        "move_refactorings_detected": len(moves),
        "move_refactoring_types": sorted({m["type"] for m in moves}),
    }


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def load_tasks() -> dict[str, list[dict[str, Any]]]:
    raw = json.loads(TASKS_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def summarize(results: list[dict[str, Any]]) -> str:
    def net_sum(rows: list[dict], category: str, key: str) -> int:
        return sum(
            int(r["net_lines_by_category"].get(category, {}).get(key, 0)) for r in rows
        )

    def calls(rows: list[dict], category: str) -> int:
        return sum(int(r["tool_calls_by_category"].get(category, 0)) for r in rows)

    lines = [
        "",
        "mode       | tasks | tool calls | direct calls | net ins tool/direct "
        "| expected move found",
        "-----------|-------|------------|--------------|---------------------"
        "|--------------------",
    ]
    for mode in MODE_NUDGE:
        rows = [r for r in results if r["mode"] == mode]
        if not rows:
            continue
        found = sum(1 for r in rows if r["expected_move_detected"])
        lines.append(
            f"{mode:<11}| {len(rows):>5} | {calls(rows, REFACTORING_TOOL):>10} | "
            f"{calls(rows, DIRECT_EDIT):>12} | "
            f"{net_sum(rows, REFACTORING_TOOL, 'inserted'):>9}/"
            f"{net_sum(rows, DIRECT_EDIT, 'inserted'):<9} | "
            f"{found:>2}/{len(rows)}"
        )
    lines.append("")
    lines.append("by category:")
    for category in sorted({r["category"] for r in results}):
        rows = [r for r in results if r["category"] == category]
        found = sum(1 for r in rows if r["expected_move_detected"])
        lines.append(
            f"  {category:<18} {found:>2}/{len(rows):<2} expected moves detected, "
            f"{calls(rows, REFACTORING_TOOL):>2} tool calls, "
            f"{calls(rows, DIRECT_EDIT):>2} direct-edit calls"
        )
    return "\n".join(lines)


def write_report(results: list[dict[str, Any]], run_dir: Path, run_id: str) -> Path:
    """Analysis-ready report: net per-commit contribution plus run totals."""
    categories = [REFACTORING_TOOL, DIRECT_EDIT, UNATTRIBUTED]
    totals = {
        category: {
            "inserted": sum(
                int(r["net_lines_by_category"].get(category, {}).get("inserted", 0))
                for r in results
            ),
            "deleted": sum(
                int(r["net_lines_by_category"].get(category, {}).get("deleted", 0))
                for r in results
            ),
            "tool_calls": sum(
                int(r["tool_calls_by_category"].get(category, 0)) for r in results
            ),
        }
        for category in categories
    }
    report = {
        "run_id": run_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "model": MODEL,
        "totals": totals,
        "expected_moves_detected": sum(
            1 for r in results if r["expected_move_detected"]
        ),
        "tasks": [
            {
                key: r[key]
                for key in (
                    "project",
                    "task_id",
                    "category",
                    "member",
                    "family",
                    "ide_tool",
                    "mode",
                    "session_id",
                    "base_sha",
                    "commit_sha",
                    "net_lines_by_category",
                    "tool_calls_by_category",
                    "expected_move_detected",
                    "move_refactorings_detected",
                    "move_refactoring_types",
                    "llm_cost",
                    "duration_s",
                )
            }
            for r in results
        ],
    }
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report_path


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="restrict to one project key")
    parser.add_argument("--tasks", nargs="*", help="task ids to run")
    parser.add_argument("--category", help="restrict to one task category")
    parser.add_argument("--mode", choices=[*MODE_NUDGE, "both"], default="baseline")
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument(
        "--mcp-verbosity",
        choices=MCP_VERBOSITY_LEVELS,
        help=(
            "MCP tool description verbosity level: 'terse', 'medium', or 'verbose'. "
            "If unset, deletes the control file to use the IDE's default."
        ),
    )
    args = parser.parse_args()

    modes = list(MODE_NUDGE) if args.mode == "both" else [args.mode]
    if "guardrail" in modes:
        logger.warning(
            "The guardrail arm currently only nudges on 'Extract Method' and only "
            "sees refactorings whose halves land in a single step, so move tasks "
            "are expected to behave identically to the baseline arm."
        )

    set_mcp_verbosity(args.mcp_verbosity)

    tasks_by_project = load_tasks()
    projects = [args.project] if args.project else list(tasks_by_project)

    run_dir = EVAL_DIR / "results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    tee_output_to(run_dir)

    manifest = load_manifest()
    results: list[dict[str, Any]] = []
    for project in projects:
        repo = REPOS_ROOT / project
        if not repo.is_dir():
            raise RuntimeError(f"Project repo not found: {repo}")
        tasks = tasks_by_project[project]
        if args.category:
            tasks = [t for t in tasks if t["category"] == args.category]
        if args.tasks:
            wanted = set(args.tasks)
            unknown = wanted - {t["id"] for t in tasks}
            if unknown:
                raise RuntimeError(f"Unknown task ids: {sorted(unknown)}")
            tasks = [t for t in tasks if t["id"] in wanted]
        if not tasks:
            raise RuntimeError(f"No tasks selected for {project}")

        ensure_idea_ready(repo)
        base_sha = prepare_repo(repo)
        manifest[project] = {
            "branch": BRANCH,
            "base_sha": base_sha,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        save_manifest(manifest)

        for task in tasks:
            for mode in modes:
                prepare_repo(repo)
                results.append(run_one(repo, task, mode, run_dir, args.max_iterations))
                (run_dir / "results.json").write_text(
                    json.dumps(results, indent=2) + "\n"
                )
        prepare_repo(repo)  # leave the tree clean after the project finishes

    print(summarize(results))
    report_path = write_report(results, run_dir, args.run_id)
    print(f"\nResults: {run_dir / 'results.json'}")
    print(f"Report:  {report_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
