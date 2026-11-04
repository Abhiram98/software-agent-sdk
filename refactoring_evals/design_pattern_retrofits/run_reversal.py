"""Run an agent on the `remove` subtask of a design-pattern benchmark task.

The goal is to synthesize trustworthy `no_pattern` states: the agent removes
the pattern instance from the `pattern` state, and the run records enough
evidence for a reviewer to decide whether the result is behavior preserving.
``render_report.py`` turns a run directory into ``report.html``.

Each project gets a dedicated clone under ``RUNS_ROOT/<project>/<project>``
(hardlinked from the evaluation clone), so runs never touch the evaluation
clone's working tree. RefactoringMiner cannot open git worktrees, which is why
this is a clone and not a worktree. The clone is reset to the task's pattern
commit at the start of every run and left at the agent's result afterwards, so
it can be inspected in the IDE.

Before the agent starts, ``check_ide_sync`` renames a field of a throwaway
class through the IDE, to prove that the IDE refactors this clone (not another
checkout of the project) and sees its on-disk content. The run aborts if not.

Recorded per run (``results/<run_id>/``):

- ``ide_sync.json``: the IDE sync check, including how long the IDE took to
  notice an external edit to a file it had already loaded
- ``events.json``: the full conversation (plan, reasoning, tool calls, nudges)
- ``edit_attribution.jsonl``: per-tool-call snapshot commits and line labels
- ``refactoring_log.jsonl``: per-step RefactoringMiner findings (nudge input)
- ``rm_total.json``: RefactoringMiner on the whole start -> result diff
- ``baseline/`` and ``final/``: build log and oracle output for both states
- ``results.json``: summary metrics and verdicts

Snapshot commits are pinned under ``refs/reversal-runs/<run_id>/`` in the run
clone so ``git gc`` cannot prune them. The result is committed on branch
``reversal-run/<task_id>/<run_id>`` and pushed to the evaluation clone.

Prerequisites: ``OPENROUTER_API_KEY`` (in ``refactoring_evals/.env``), the
IntelliJ IDEA fork with the MCP proxy, and RefactoringMiner (see
``openhands.sdk.conversation.refactoring_miner``).

Usage:
    uv run python refactoring_evals/design_pattern_retrofits/run_reversal.py \\
        --task abstractfactory_zk_idomfactory
"""

import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast

from fastmcp import Client
from fastmcp.client.transports import SSETransport
from mcp.types import TextContent
from pydantic import SecretStr

from openhands.sdk import LLM, Agent, get_logger
from openhands.sdk.conversation.edit_attribution import (
    EDIT_ATTRIBUTION_LOG_ENV_VAR,
    REFACTORING_TOOL,
    REFACTORING_TOOLS_ENV_VAR,
    net_line_attribution,
)
from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.conversation.refactoring_miner import (
    REFACTORING_LOG_ENV_VAR,
    REFACTORING_NUDGE_ENV_VAR,
    get_miner_path,
    run_refactoring_miner,
)
from openhands.sdk.mcp import MCPServer
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.task_tracker import TaskTrackerTool
from openhands.tools.terminal import TerminalTool


logger = get_logger(__name__)

EVAL_DIR = Path(__file__).parent
BENCHMARK_PATH = EVAL_DIR / "benchmark.json"
CONFIG_PATH = EVAL_DIR / "reversal_config.json"
ORACLES_DIR = EVAL_DIR / "oracles"
ENV_PATH = EVAL_DIR.parent / ".env"
REPOS_ROOT = Path("/Users/abhiram/Documents/TBE/evaluation_projects")
RUNS_ROOT = REPOS_ROOT / "_reversal_runs"
MODEL = os.environ.get("EVAL_MODEL", "openrouter/~deepseek/deepseek-v4-flash-latest")
MCP_URL = os.environ.get("INTELLIJ_MCP_URL", "http://127.0.0.1:64343/sse")
IDEA_APP = os.environ.get("IDEA_APP", "IntelliJ IDEA OSS 2.app")
PROJECT_PATH_HEADER = "IJ_MCP_SERVER_PROJECT_PATH"
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
# Edits are attributed by the outer tool name, so refactorings dispatched via
# the IDE's generic `execute_tool` would count as direct edits; hide it.
HIDDEN_TOOLS_REGEX = r"^(?!execute_tool$)"
# Untracked files the agent or IDE may create that must not enter snapshots.
LOCAL_EXCLUDES = (".agent_tmp/", "*.iml")
FAILED_RESULT_RE = re.compile(r'"success"\s*:\s*false')
IDE_PROBE_CLASS = "IdeSyncProbe"
IDE_PROBE_POLL_S = 0.5
IDE_READY_TIMEOUT_S = 300.0
IDE_EXTERNAL_EDIT_TIMEOUT_S = 30.0

PROMPT = """{instruction}

The pattern is implemented by `{class_name}` ({paths}).
Remove it while preserving the observable behavior of the rest of the code base:
callers must get the same results as before, and the project must still compile.

Before changing any code, record your plan with the task tracker, and keep its
statuses up to date while you work."""


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


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


def load_task(task_id: str) -> dict[str, Any]:
    tasks = json.loads(BENCHMARK_PATH.read_text())["tasks"]
    task = next((t for t in tasks if t["task_id"] == task_id), None)
    if task is None:
        raise RuntimeError(f"Unknown task id: {task_id}")
    if not task["subtasks"]["remove"]["available"]:
        raise RuntimeError(f"{task_id} has no remove subtask")
    if task["states"]["pattern"]["kind"] != "commit":
        raise RuntimeError(f"{task_id}: pattern state is not a commit")
    return task


def prepare_clone(project: str, commit: str) -> Path:
    """Reset the project's dedicated run clone to ``commit``."""
    source = REPOS_ROOT / project
    clone = RUNS_ROOT / project / project
    if not clone.is_dir():
        clone.parent.mkdir(parents=True, exist_ok=True)
        run_git(clone.parent, "clone", "--no-checkout", str(source), project)
    try:
        run_git(clone, "cat-file", "-e", f"{commit}^{{commit}}")
    except RuntimeError:
        run_git(clone, "fetch", "origin", commit)
    run_git(clone, "checkout", "--force", "--detach", commit)
    run_git(clone, "reset", "--hard")
    run_git(clone, "clean", "-fd")
    exclude = clone / ".git" / "info" / "exclude"
    existing = exclude.read_text() if exclude.exists() else ""
    missing = [p for p in LOCAL_EXCLUDES if p not in existing.splitlines()]
    if missing:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(existing + "".join(f"{p}\n" for p in missing))
    return clone


def shell(command: str, cwd: Path, log: Path, timeout: int = 3600) -> bool:
    result = subprocess.run(
        command,
        shell=True,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    log.write_text(result.stdout + result.stderr)
    return result.returncode == 0


def check_state(
    repo: Path, project_cfg: dict, oracle: str | None, out: Path
) -> dict[str, Any]:
    """Build the current tree and run the task oracle on it."""
    out.mkdir(parents=True, exist_ok=True)
    built = shell(project_cfg["build"], repo, out / "build.log")
    oracle_ok = None
    if oracle:
        command = oracle.format(
            oracles=shlex.quote(str(ORACLES_DIR)),
            repo=shlex.quote(str(repo)),
            out=shlex.quote(str(out / "oracle")),
        )
        oracle_ok = shell(command, repo, out / "oracle.log")
    return {"build_ok": built, "oracle_ran": oracle_ok}


def compare_oracle(baseline: Path, final: Path) -> dict[str, Any]:
    before = (baseline / "oracle" / "dump.tsv").read_text().splitlines()
    after_path = final / "oracle" / "dump.tsv"
    if not after_path.exists():
        return {"identical": False, "reason": "no oracle output for final state"}
    after = after_path.read_text().splitlines()
    changed = [(b, a) for b, a in zip(before, after) if b != a]
    return {
        "identical": before == after,
        "results": len(before),
        "errors_in_baseline": sum("\terror " in line for line in before),
        "changed_results": len(changed) + abs(len(before) - len(after)),
        "first_changes": changed[:20],
    }


def ensure_idea_ready(repo: Path, timeout_s: float = 300.0) -> None:
    """Open the project in the IDE and wait until its MCP proxy answers."""
    subprocess.run(
        ["open", "-na", IDEA_APP, "--args", str(repo)], check=True, timeout=60
    )
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


def _probe_source(package: str, field: str) -> str:
    return (
        f"package {package};\n\n"
        f"class {IDE_PROBE_CLASS} {{\n"
        f"    int {field};\n"
        f"    int use() {{ return {field}; }}\n"
        f"}}\n"
    )


async def _probe_ide_sync(repo: Path, probe: Path, package: str) -> dict[str, Any]:
    """Rename a field of a throwaway class through the IDE (see check_ide_sync)."""
    rel = probe.relative_to(repo).as_posix()
    transport = SSETransport(MCP_URL, headers={PROJECT_PATH_HEADER: str(repo)})

    async def rename_until_seen(timeout_s: float) -> float | None:
        """Write a fresh field name to disk; seconds until the IDE renames it."""
        field = f"probe_{uuid.uuid4().hex[:8]}"
        probe.write_text(_probe_source(package, field))
        started = time.monotonic()
        while time.monotonic() - started < timeout_s:
            result = await client.call_tool(
                "rename_refactoring",
                {"pathInProject": rel, "symbolName": field, "newName": f"{field}_x"},
                raise_on_error=False,
            )
            text = "".join(c.text for c in result.content if isinstance(c, TextContent))
            renamed = probe.read_text().count(f"{field}_x") == 2
            if text.startswith("Successfully renamed") and renamed:
                return round(time.monotonic() - started, 1)
            await asyncio.sleep(IDE_PROBE_POLL_S)
        return None

    async with Client(transport) as client:
        ready_after_s = await rename_until_seen(IDE_READY_TIMEOUT_S)
        if ready_after_s is None:
            return {"ok": False, "ready_after_s": None, "external_edit_lag_s": None}
        lag_s = await rename_until_seen(IDE_EXTERNAL_EDIT_TIMEOUT_S)
    return {
        "ok": lag_s is not None,
        "ready_after_s": ready_after_s,
        "external_edit_lag_s": lag_s,
    }


def check_ide_sync(repo: Path, target_file: str) -> dict[str, Any]:
    """Prove the IDE refactors this clone's code as it is on disk.

    A throwaway class is written next to the target file, and one of its
    fields is renamed through the IDE's ``rename_refactoring`` tool. It counts
    as passed only when the declaration and its usage are both renamed on disk,
    which shows that the MCP project path resolves to this clone and that the
    IDE has indexed it. The file is then rewritten on disk and renamed again,
    to measure how long the IDE takes to notice an external edit (an IDE
    refactoring called sooner works on stale content). The class is deleted
    before the agent starts, so it never appears in the run's diff.
    """
    target = repo / target_file
    match = re.search(r"^package\s+([\w.]+)\s*;", target.read_text(), re.MULTILINE)
    if match is None:
        raise RuntimeError(f"No package declaration in {target_file}")
    probe = target.parent / f"{IDE_PROBE_CLASS}.java"
    try:
        return asyncio.run(_probe_ide_sync(repo, probe, match.group(1)))
    finally:
        probe.unlink(missing_ok=True)


def build_agent(repo: Path) -> tuple[Agent, LLM]:
    api_key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError(f"Set OPENROUTER_API_KEY in the environment or {ENV_PATH}")
    llm = LLM(usage_id="agent", model=MODEL, api_key=SecretStr(api_key))
    agent = Agent(
        llm=llm,
        tools=[
            Tool(name=TerminalTool.name),
            Tool(name=FileEditorTool.name),
            Tool(name=TaskTrackerTool.name),
        ],
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


def pin_snapshots(
    repo: Path, run_id: str, edits: list[dict], steps: list[dict]
) -> None:
    """Keep the dangling snapshot commits alive under refs/reversal-runs/."""
    for record in edits:
        run_git(
            repo,
            "update-ref",
            f"refs/reversal-runs/{run_id}/edit/{record['seq']}",
            record["commit"],
        )
    for record in steps:
        if record.get("commit"):
            run_git(
                repo,
                "update-ref",
                f"refs/reversal-runs/{run_id}/step/{record['step']}",
                record["commit"],
            )


def commit_result(repo: Path, branch: str, message: str) -> str | None:
    if not run_git(repo, "status", "--porcelain"):
        return None
    run_git(repo, "add", "-A")
    run_git(
        repo,
        "-c",
        "user.name=openhands",
        "-c",
        "user.email=openhands@all-hands.dev",
        "commit",
        "-q",
        "-m",
        message,
    )
    run_git(repo, "branch", "-f", branch)
    run_git(repo, "push", "-q", "-f", "origin", f"{branch}:{branch}")
    return run_git(repo, "rev-parse", "HEAD")


def mine_total(repo: Path, commit: str, out: Path) -> list[dict[str, Any]]:
    miner = get_miner_path()
    if miner is None:
        return []
    refactorings = run_refactoring_miner(miner, str(repo), commit, timeout=900)
    out.write_text(json.dumps(refactorings, indent=1) + "\n")
    return refactorings


def tool_line_share(net: dict[str, dict[str, int]]) -> float | None:
    changed = {c: v["inserted"] + v["deleted"] for c, v in net.items()}
    total = sum(changed.values())
    return round(changed[REFACTORING_TOOL] / total, 3) if total else None


def tool_call_summary(events: list[dict]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for e in events:
        if e["kind"] == "ActionEvent":
            calls[e["tool_call_id"]] = {"tool": e["tool_name"], "ok": None}
        elif e["kind"] == "ObservationEvent" and e["tool_call_id"] in calls:
            text = json.dumps(e["observation"])
            failed = e["observation"].get("is_error") or FAILED_RESULT_RE.search(text)
            calls[e["tool_call_id"]]["ok"] = not failed
        elif e["kind"] == "AgentErrorEvent" and e.get("tool_call_id") in calls:
            calls[e["tool_call_id"]]["ok"] = False
    return list(calls.values())


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="benchmark task id")
    parser.add_argument("--max-iterations", type=int, default=150)
    parser.add_argument("--max-budget", type=float, default=5.0, help="USD cap")
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()

    load_env()
    task = load_task(args.task)
    config = json.loads(CONFIG_PATH.read_text())
    project_cfg = config["projects"][task["project"]]
    oracle = config["oracles"].get(task["task_id"])
    os.environ.update(project_cfg.get("env", {}))

    run_dir = EVAL_DIR / "results" / args.task / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_file = (run_dir / "run.log").open("a")
    sys.stdout = cast(Any, _Tee(sys.stdout, log_file))
    sys.stderr = cast(Any, _Tee(sys.stderr, log_file))

    start_commit = task["states"]["pattern"]["commit"]
    repo = prepare_clone(task["project"], start_commit)
    if project_cfg.get("setup"):
        if not shell(project_cfg["setup"], repo, run_dir / "setup.log"):
            raise RuntimeError(f"Project setup failed; see {run_dir / 'setup.log'}")

    logger.info("Checking the start state (build + oracle)")
    baseline = check_state(repo, project_cfg, oracle, run_dir / "baseline")
    if not baseline["build_ok"] or baseline["oracle_ran"] is False:
        raise RuntimeError(f"Start state fails its checks: {baseline}")

    edit_log = run_dir / "edit_attribution.jsonl"
    step_log = run_dir / "refactoring_log.jsonl"
    os.environ[EDIT_ATTRIBUTION_LOG_ENV_VAR] = str(edit_log)
    os.environ[REFACTORING_LOG_ENV_VAR] = str(step_log)
    os.environ[REFACTORING_NUDGE_ENV_VAR] = "1"
    os.environ[REFACTORING_TOOLS_ENV_VAR] = ",".join(IDE_REFACTORING_TOOLS)

    ensure_idea_ready(repo)
    remove = task["subtasks"]["remove"]
    logger.info("Checking that the IDE refactors this clone's on-disk code")
    ide_sync = check_ide_sync(repo, remove["target"]["file_paths"][0])
    (run_dir / "ide_sync.json").write_text(json.dumps(ide_sync, indent=2) + "\n")
    if not ide_sync["ok"]:
        raise RuntimeError(
            f"IDE is not refactoring {repo} as it is on disk: {ide_sync}. Check "
            "that the project is open and trusted in the IDE, then rerun."
        )
    if run_git(repo, "status", "--porcelain"):
        raise RuntimeError(f"IDE sync check left changes in {repo}")
    prompt = PROMPT.format(
        instruction=remove["instruction"],
        class_name=remove["target"]["class_name"],
        paths=", ".join(f"`{p}`" for p in remove["target"]["file_paths"]),
    )
    logger.info("Prompt:\n%s", prompt)
    agent, llm = build_agent(repo)
    conversation = LocalConversation(
        agent=agent,
        workspace=repo,
        persistence_dir=None,
        max_iteration_per_run=args.max_iterations,
        max_budget_per_run=args.max_budget,
    )
    started = time.monotonic()
    conversation.send_message(prompt)
    try:
        conversation.run()
    finally:
        duration_s = time.monotonic() - started
        events = [e.model_dump(mode="json") for e in conversation.state.events]
        (run_dir / "events.json").write_text(json.dumps(events, indent=1) + "\n")

    session_id = str(conversation.id)
    branch = f"reversal-run/{task['task_id']}/{args.run_id}"
    result_commit = commit_result(
        repo,
        branch,
        f"{remove['instruction']}\n\nAgent run {args.run_id} ({MODEL}).\n\n"
        "Co-authored-by: openhands <openhands@all-hands.dev>",
    )
    edits = read_jsonl(edit_log, session_id)
    steps = read_jsonl(step_log, session_id)
    pin_snapshots(repo, args.run_id, edits, steps)

    logger.info("Checking the result state (build + oracle)")
    final = check_state(repo, project_cfg, oracle, run_dir / "final")
    oracle_cmp = (
        compare_oracle(run_dir / "baseline", run_dir / "final") if oracle else None
    )
    net = net_line_attribution(str(repo), edits, start_commit, result_commit)
    rm_total = (
        mine_total(repo, result_commit, run_dir / "rm_total.json")
        if result_commit
        else []
    )
    calls = tool_call_summary(events)

    results = {
        "task_id": task["task_id"],
        "pattern": task["pattern"],
        "project": task["project"],
        "run_id": args.run_id,
        "model": MODEL,
        "session_id": session_id,
        "execution_status": str(conversation.state.execution_status.value),
        "repo": str(repo),
        "start_commit": start_commit,
        "result_commit": result_commit,
        "result_branch": branch if result_commit else None,
        "prompt": prompt,
        "target": remove["target"],
        "baseline": baseline,
        "ide_sync": ide_sync,
        "final": final,
        "oracle": oracle_cmp,
        "net_lines": net,
        "tool_line_share": tool_line_share(net),
        "refactorings_total": len(rm_total),
        "tool_calls": len(calls),
        "refactoring_tool_calls": sum(
            c["tool"] in IDE_REFACTORING_TOOLS for c in calls
        ),
        "failed_tool_calls": sum(c["ok"] is False for c in calls),
        "nudges": sum(
            e["kind"] == "MessageEvent" and e["source"] == "environment" for e in events
        ),
        "llm_cost": llm.metrics.accumulated_cost,
        "duration_s": round(duration_s, 1),
    }
    (run_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: results[k]
                for k in (
                    "execution_status",
                    "result_branch",
                    "final",
                    "oracle",
                    "net_lines",
                    "tool_line_share",
                    "llm_cost",
                    "duration_s",
                )
            },
            indent=2,
        )
    )
    print(f"\nRun dir: {run_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
