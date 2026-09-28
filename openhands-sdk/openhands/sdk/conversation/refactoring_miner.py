"""Per-step refactoring mining with RefactoringMiner.

Opt-in via the ``OPENHANDS_REFACTORING_MINER`` environment variable pointing at
the RefactoringMiner launcher script (e.g.
``.../RefactoringMiner-3.0.10/bin/RefactoringMiner``). After every agent step,
the workspace working tree is snapshotted as a dangling git commit and
RefactoringMiner is run on that commit to detect the refactorings introduced by
the step. Findings are appended as JSON lines joinable with the conversation
event export via ``session_id`` + ``step``.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openhands.sdk.logger import get_logger


if TYPE_CHECKING:
    from openhands.sdk.conversation.impl.local_conversation import LocalConversation

logger = get_logger(__name__)

REFACTORING_MINER_ENV_VAR = "OPENHANDS_REFACTORING_MINER"
REFACTORING_LOG_ENV_VAR = "OPENHANDS_REFACTORING_LOG"
REFACTORING_TIMEOUT_ENV_VAR = "OPENHANDS_REFACTORING_TIMEOUT"
DEFAULT_REFACTORING_LOG = Path.home() / ".openhands" / "refactoring_log.jsonl"
DEFAULT_TIMEOUT_SECONDS = 600

_lock = threading.Lock()

# Snapshot commits are authored deterministically so git never fails on
# repos/hosts without a configured identity.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "openhands",
    "GIT_AUTHOR_EMAIL": "openhands@all-hands.dev",
    "GIT_COMMITTER_NAME": "openhands",
    "GIT_COMMITTER_EMAIL": "openhands@all-hands.dev",
}


def get_miner_path() -> str | None:
    return os.environ.get(REFACTORING_MINER_ENV_VAR) or None


def get_refactoring_log_path() -> Path:
    return Path(os.environ.get(REFACTORING_LOG_ENV_VAR) or DEFAULT_REFACTORING_LOG)


def _run_git(repo: str, *args: str, index_file: str | None = None) -> str:
    env = dict(os.environ) | _GIT_IDENTITY
    if index_file is not None:
        env["GIT_INDEX_FILE"] = index_file
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def snapshot_working_tree(repo: str) -> str | None:
    """Capture the working tree as a dangling commit and return its sha.

    A temporary index is used so HEAD, the real index, and the working tree
    are all left untouched. Returns None when the tree matches HEAD.
    """
    head = _run_git(repo, "rev-parse", "HEAD")
    fd, index_file = tempfile.mkstemp(prefix="openhands-refminer-index-")
    os.close(fd)
    # Git refuses to build an index on top of an empty file; let it create one.
    os.unlink(index_file)
    try:
        _run_git(repo, "add", "-A", "--", ".", index_file=index_file)
        tree = _run_git(repo, "write-tree", index_file=index_file)
        head_tree = _run_git(repo, "rev-parse", "HEAD^{tree}")
        if tree == head_tree:
            return None
        return _run_git(
            repo, "commit-tree", tree, "-p", head, "-m", "openhands step snapshot"
        )
    finally:
        os.unlink(index_file)


def run_refactoring_miner(
    miner: str, repo: str, commit: str, timeout: float
) -> list[dict[str, Any]]:
    """Run RefactoringMiner on a single commit and return its refactorings."""
    fd, out_file = tempfile.mkstemp(prefix="openhands-refminer-", suffix=".json")
    os.close(fd)
    try:
        result = subprocess.run(
            [miner, "-c", repo, commit, "-json", out_file],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0 or not Path(out_file).exists():
            raise RuntimeError(
                f"RefactoringMiner failed (rc={result.returncode}): "
                f"{result.stderr.strip()[-2000:]}"
            )
        with open(out_file, encoding="utf-8") as f:
            payload = json.load(f)
    finally:
        os.unlink(out_file)
    refactorings: list[dict[str, Any]] = []
    for entry in payload.get("commits", []):
        refactorings.extend(entry.get("refactorings", []))
    return refactorings


@contextmanager
def _origin_remote(repo: str):
    """Lend the repo a temporary ``origin`` remote if it has none.

    RefactoringMiner builds its JSON output around the ``origin`` URL and
    fails with a NullPointerException on repos that lack one.
    """
    added = False
    try:
        _run_git(repo, "remote", "get-url", "origin")
    except RuntimeError:
        _run_git(repo, "remote", "add", "origin", repo)
        added = True
    try:
        yield
    finally:
        if added:
            try:
                _run_git(repo, "remote", "remove", "origin")
            except Exception as e:
                logger.warning("Failed to remove temporary origin remote: %s", e)


def log_step_refactorings(record: dict[str, Any]) -> None:
    log_path = get_refactoring_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def mine_step_refactorings(conversation: "LocalConversation", step: int) -> None:
    """Detect and log refactorings introduced by the step that just finished.

    Never raises: mining failures are recorded as an error record and logged.
    """
    miner = get_miner_path()
    if miner is None:
        return
    workspace = str(conversation.workspace.working_dir)
    try:
        repo = _run_git(workspace, "rev-parse", "--show-toplevel")
    except Exception as e:
        logger.debug(
            "Refactoring mining skipped: workspace %s is not a git repo (%s)",
            workspace,
            e,
        )
        return

    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": str(conversation.state.id),
        "step": step,
        "workspace": workspace,
        "repo_root": repo,
    }
    started = time.monotonic()
    try:
        with _lock, _origin_remote(repo):
            commit = snapshot_working_tree(repo)
            if commit is None:
                record.update(status="no_changes", num_refactorings=0, refactorings=[])
            else:
                timeout = float(
                    os.environ.get(REFACTORING_TIMEOUT_ENV_VAR)
                    or DEFAULT_TIMEOUT_SECONDS
                )
                refactorings = run_refactoring_miner(miner, repo, commit, timeout)
                record.update(
                    commit=commit,
                    status="ok",
                    num_refactorings=len(refactorings),
                    refactorings=refactorings,
                )
    except Exception as e:
        record["status"] = "error"
        record["error"] = str(e)
        logger.warning("Refactoring mining failed: %s", e)
    record["duration_ms"] = int((time.monotonic() - started) * 1000)
    try:
        log_step_refactorings(record)
    except OSError as e:
        logger.warning("Failed to write refactoring log: %s", e)
