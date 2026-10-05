"""Headless extract-method refactoring eval harness.

Runs each task in ``tasks.json`` against a local checkout of the target
project (``REPOS_ROOT``/<project>) with the standalone SDK, in two modes:

- ``baseline``: no extract-method guardrail (``OPENHANDS_REFACTORING_NUDGE=0``)
- ``guardrail``: RefactoringMiner nudge enabled (``OPENHANDS_REFACTORING_NUDGE=1``)

Per task x mode the harness resets the repo to a clean ``master``, records
the base sha in ``manifest.json``, runs the agent with the IntelliJ IDEA MCP
server (``extract_method`` tool) plus terminal/file-editor tools, commits the
resulting diff itself, and reports the *net* lines contributed to the commit
per attribution category (refactoring tool vs direct edit). Net attribution
is computed from git (baseline -> commit diff, validated against a replay of
the per-tool-call snapshot diffs), not from the edit ledger's running
totals, so reported line counts always reconcile with the commit. The
session_id joins back to the raw JSONL logs for per-step analysis. Results
land in ``results/<run_id>/``.

Prerequisites:
- ``OPENROUTER_API_KEY`` set (model: openrouter deepseek-v4-flash-latest)
- RefactoringMiner available (default path from
  ``openhands.sdk.conversation.refactoring_miner``) for the guardrail mode
- IntelliJ IDEA OSS installed; the harness opens the project and waits for
  the IDE MCP proxy (``INTELLIJ_MCP_URL``, default 127.0.0.1:64343/sse)

Usage:
    uv run python refactoring_evals/extract_method/run_eval.py \
        [--project OpenRefine] [--tasks 0 2] [--mode both] [--max-iterations 100]
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
from typing import Any

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
IDEA_APP = "IntelliJ IDEA OSS.app"
MODEL = os.environ.get("EVAL_MODEL", "openrouter/~deepseek/deepseek-v4-flash-latest")
MCP_URL = os.environ.get("INTELLIJ_MCP_URL", "http://127.0.0.1:64343/sse")
PROJECT_PATH_HEADER = "IJ_MCP_SERVER_PROJECT_PATH"

NUDGE_ENV_VAR = "OPENHANDS_REFACTORING_NUDGE"
EDIT_LOG_ENV_VAR = "OPENHANDS_EDIT_ATTRIBUTION_LOG"
MINER_LOG_ENV_VAR = "OPENHANDS_REFACTORING_LOG"
MODE_NUDGE = {"baseline": "0", "guardrail": "1"}

EXTRACT_METHOD_TYPE = "Extract Method"


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


def prepare_repo(repo: Path) -> str:
    """Force the repo onto a clean BRANCH and return the base sha."""
    if is_dirty(repo):
        logger.warning(
            "%s has uncommitted changes; stashing them before checkout", repo
        )
        run_git(repo, "stash", "push", "-u", "-m", "refactoring-eval auto-stash")
    run_git(repo, "checkout", BRANCH)
    run_git(repo, "reset", "--hard", BRANCH)
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
    """Block until the IDE MCP proxy answers, so extract_method is reachable."""
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
    repo: Path, task: str, max_iterations: int
) -> tuple[LocalConversation, LLM]:
    agent, llm = build_agent(repo)
    conversation = LocalConversation(
        agent=agent,
        workspace=repo,
        persistence_dir=None,  # keep eval repo tree clean
        max_iteration_per_run=max_iterations,
    )
    conversation.send_message(task)
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


def run_one(
    repo: Path,
    task_idx: int,
    task: str,
    mode: str,
    run_dir: Path,
    max_iterations: int,
) -> dict[str, Any]:
    base_sha = prepare_repo(repo)
    os.environ[NUDGE_ENV_VAR] = MODE_NUDGE[mode]
    os.environ[EDIT_LOG_ENV_VAR] = str(run_dir / "edit_attribution.jsonl")
    os.environ[MINER_LOG_ENV_VAR] = str(run_dir / "refactoring_log.jsonl")

    logger.info("[%s] task %d: %s", mode, task_idx, task)
    started = time.monotonic()
    conversation, llm = run_agent(repo, task, max_iterations)
    duration_s = time.monotonic() - started
    commit_sha = commit_changes(
        repo, f"refactoring eval: task {task_idx} ({mode})\n\n{task}"
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
    extract_methods = [
        x
        for r in mined
        for x in r.get("refactorings", [])
        if x.get("type") == EXTRACT_METHOD_TYPE
    ]
    net_lines = net_line_attribution(str(repo), records, base_sha, commit_sha)
    diff = run_git(repo, "diff", "--shortstat", base_sha, "HEAD") if commit_sha else ""
    return {
        "project": repo.name,
        "task_idx": task_idx,
        "task": task,
        "mode": mode,
        "base_sha": base_sha,
        "commit_sha": commit_sha,
        "session_id": session_id,
        "llm_cost": llm.metrics.accumulated_cost,
        "duration_s": round(duration_s, 1),
        "diff_shortstat": diff,
        "tool_calls_by_category": collect_tool_calls(records),
        "net_lines_by_category": net_lines,
        "extract_methods_detected": len(extract_methods),
        "extracted_method_names": [d.get("description", "") for d in extract_methods],
    }


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n")


def summarize(results: list[dict[str, Any]]) -> str:
    def net_sum(rows: list[dict], category: str, key: str) -> int:
        return sum(
            int(r["net_lines_by_category"].get(category, {}).get(key, 0)) for r in rows
        )

    def calls(rows: list[dict], category: str) -> int:
        return sum(int(r["tool_calls_by_category"].get(category, 0)) for r in rows)

    lines = [
        "",
        "Mode        | tasks | refactor | direct | net ins ref/direct",
        "            |       | calls    | calls  | | net del ref/direct | EM",
        "------------|-------|----------|--------|--------------------",
    ]
    for mode in MODE_NUDGE:
        rows = [r for r in results if r["mode"] == mode]
        if not rows:
            continue
        lines.append(
            f"{mode:<12}| {len(rows):>5} | "
            f"{calls(rows, REFACTORING_TOOL):>8} | "
            f"{calls(rows, DIRECT_EDIT):>6} | "
            f"{net_sum(rows, REFACTORING_TOOL, 'inserted'):>4}/"
            f"{net_sum(rows, DIRECT_EDIT, 'inserted'):<4} "
            f"| {net_sum(rows, REFACTORING_TOOL, 'deleted'):>4}/"
            f"{net_sum(rows, DIRECT_EDIT, 'deleted'):<4} "
            f"| {sum(int(r['extract_methods_detected']) for r in rows):>2}"
        )
    return "\n".join(lines)


def write_report(results: list[dict[str, Any]], run_dir: Path, run_id: str) -> Path:
    """Analysis-ready report: net per-commit contribution + run totals.

    Line counts are the net contribution to the actual commit (validated
    against ``git diff``), not the ledger's running totals; the session_id
    joins back to the raw per-tool-call JSONL logs for deeper analysis.
    """
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
        "totals": totals,
        "tasks": [
            {
                "project": r["project"],
                "task_idx": r["task_idx"],
                "task": r["task"],
                "mode": r["mode"],
                "session_id": r["session_id"],
                "base_sha": r["base_sha"],
                "commit_sha": r["commit_sha"],
                "net_lines_by_category": {
                    category: {
                        "inserted": int(
                            r["net_lines_by_category"]
                            .get(category, {})
                            .get("inserted", 0)
                        ),
                        "deleted": int(
                            r["net_lines_by_category"]
                            .get(category, {})
                            .get("deleted", 0)
                        ),
                    }
                    for category in categories
                },
                "tool_calls_by_category": {
                    category: int(r["tool_calls_by_category"].get(category, 0))
                    for category in (REFACTORING_TOOL, DIRECT_EDIT)
                },
                "extract_methods_detected": r["extract_methods_detected"],
                "llm_cost": r["llm_cost"],
                "duration_s": r["duration_s"],
            }
            for r in results
        ],
    }
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", help="restrict to one project key")
    parser.add_argument("--tasks", type=int, nargs="*", help="task indices to run")
    parser.add_argument("--mode", choices=[*MODE_NUDGE, "both"], default="both")
    parser.add_argument("--max-iterations", type=int, default=100)
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    tasks_by_project: dict[str, list[str]] = json.loads(TASKS_PATH.read_text())
    projects = [args.project] if args.project else list(tasks_by_project)
    modes = list(MODE_NUDGE) if args.mode == "both" else [args.mode]

    run_dir = EVAL_DIR / "results" / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest()
    results: list[dict[str, Any]] = []
    for project in projects:
        repo = REPOS_ROOT / project
        if not repo.is_dir():
            raise RuntimeError(f"Project repo not found: {repo}")
        tasks = tasks_by_project[project]
        indices = args.tasks if args.tasks else range(len(tasks))

        ensure_idea_ready(repo)
        base_sha = prepare_repo(repo)
        manifest[project] = {
            "branch": BRANCH,
            "base_sha": base_sha,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        save_manifest(manifest)

        for idx in indices:
            for mode in modes:
                prepare_repo(repo)
                result = run_one(
                    repo, idx, tasks[idx], mode, run_dir, args.max_iterations
                )
                results.append(result)
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
