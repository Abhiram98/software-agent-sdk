"""Per-step refactoring mining with RefactoringMiner.

After every agent step, the workspace working tree is snapshotted as a dangling
git commit parented on the previous step's snapshot, so RefactoringMiner only
sees the diff introduced by that single step. Findings are appended as JSON
lines joinable with the conversation event export via ``session_id`` + ``step``.

The miner binary defaults to the local RefactoringMiner distribution and can be
overridden with ``OPENHANDS_REFACTORING_MINER`` (set it to an empty string to
disable mining entirely).
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from openhands.sdk.event import ActionEvent, Event
from openhands.sdk.logger import get_logger


if TYPE_CHECKING:
    from openhands.sdk.conversation.impl.local_conversation import LocalConversation

logger = get_logger(__name__)

REFACTORING_MINER_ENV_VAR = "OPENHANDS_REFACTORING_MINER"
REFACTORING_LOG_ENV_VAR = "OPENHANDS_REFACTORING_LOG"
REFACTORING_TIMEOUT_ENV_VAR = "OPENHANDS_REFACTORING_TIMEOUT"
REFACTORING_NUDGE_ENV_VAR = "OPENHANDS_REFACTORING_NUDGE"
DEFAULT_REFACTORING_MINER = (
    Path.home()
    / "Documents/TBE/RefactoringMiner/build/distributions/"
    / "RefactoringMiner-3.0.10/bin/RefactoringMiner"
)
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
    """Resolve the RefactoringMiner launcher, or None when mining is disabled.

    An explicitly empty ``OPENHANDS_REFACTORING_MINER`` disables mining;
    an unset variable falls back to the local default distribution.
    """
    override = os.environ.get(REFACTORING_MINER_ENV_VAR)
    if override is not None and override.strip() == "":
        return None
    return override or str(DEFAULT_REFACTORING_MINER)


def get_refactoring_log_path() -> Path:
    return Path(os.environ.get(REFACTORING_LOG_ENV_VAR) or DEFAULT_REFACTORING_LOG)


def is_refactoring_nudge_enabled() -> bool:
    """Whether detected refactorings should trigger a behavioral nudge.

    On by default; a falsy ``OPENHANDS_REFACTORING_NUDGE`` value
    ("0", "false", "no", "off", case-insensitive, or empty) disables it.
    """
    value = os.environ.get(REFACTORING_NUDGE_ENV_VAR)
    if value is None:
        return True
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def extract_method_detected(refactorings: list[dict[str, Any]]) -> bool:
    """Whether any of the step's mined refactorings is an Extract Method."""
    return any(r.get("type") == "Extract Method" for r in refactorings)


def extracted_method_names(refactorings: list[dict[str, Any]]) -> list[str]:
    """Names of the methods extracted by the step's Extract Method entries.

    Parses names like ``computeFacetsRowBased`` from RefactoringMiner
    descriptions of the form
    ``"Extract Method <signature> extracted from <signature> in class <cls>"``.
    """
    names: list[str] = []
    for r in refactorings:
        if r.get("type") != "Extract Method":
            continue
        head, sep, _ = str(r.get("description", "")).partition(" extracted from ")
        if not sep:
            continue
        signature = head.removeprefix("Extract Method ").strip()
        name = signature.split("(", 1)[0].split()[-1] if signature else ""
        if name:
            names.append(name)
    return names


# Tools whose actions represent direct text edits the agent made itself.
# Edits made through shell commands are not attributed yet.
DIRECT_EDIT_TOOLS = frozenset({"file_editor", "apply_patch"})

_PATCH_TARGET_RE = re.compile(
    r"^\*\*\* (?:Update|Add|Delete|Move) File: (.+)$", re.MULTILINE
)


def _normalized_path(raw: str, root: str) -> str:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(root) / path
    return os.path.realpath(path)


def refactoring_file_paths(refactorings: list[dict[str, Any]], root: str) -> set[str]:
    """Absolute normalized paths touched by the refactorings.

    RefactoringMiner reports paths relative to the repo root; they are
    resolved against ``root`` for comparison with tool action paths.
    """
    paths: set[str] = set()
    for r in refactorings:
        for side in ("leftSideLocations", "rightSideLocations"):
            for loc in r.get(side) or []:
                raw = loc.get("filePath")
                if raw:
                    paths.add(_normalized_path(str(raw), root))
    return paths


def direct_edit_paths(events: Sequence[Event], root: str) -> set[str]:
    """Absolute normalized paths edited by direct-edit tool actions in events."""
    paths: set[str] = set()
    for event in events:
        if not isinstance(event, ActionEvent) or event.action is None:
            continue
        if event.tool_name not in DIRECT_EDIT_TOOLS:
            continue
        args = event.action.model_dump()
        if event.tool_name == "file_editor":
            raw = args.get("path")
            raws = [raw] if isinstance(raw, str) else []
        else:
            patch = args.get("patch")
            raws = _PATCH_TARGET_RE.findall(patch) if isinstance(patch, str) else []
        paths.update(_normalized_path(raw, root) for raw in raws)
    return paths


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


def snapshot_working_tree(repo: str, parent: str | None = None) -> str | None:
    """Capture the working tree as a dangling commit and return its sha.

    A temporary index is used so HEAD, the real index, and the working tree
    are all left untouched. The commit is parented on ``parent`` (or HEAD), so
    chained snapshots diff against the previous step only. Returns None when
    the tree matches the parent's tree.
    """
    if parent is None:
        parent = _run_git(repo, "rev-parse", "HEAD")
    fd, index_file = tempfile.mkstemp(prefix="openhands-refminer-index-")
    os.close(fd)
    # Git refuses to build an index on top of an empty file; let it create one.
    os.unlink(index_file)
    try:
        _run_git(repo, "add", "-A", "--", ".", index_file=index_file)
        tree = _run_git(repo, "write-tree", index_file=index_file)
        parent_tree = _run_git(repo, "rev-parse", f"{parent}^{{tree}}")
        if tree == parent_tree:
            return None
        return _run_git(
            repo, "commit-tree", tree, "-p", parent, "-m", "openhands step snapshot"
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


def _resolve_mining_target(
    conversation: "LocalConversation",
) -> tuple[str, str] | None:
    """Resolve (miner, repo) for the conversation's workspace, or None."""
    miner = get_miner_path()
    if miner is None:
        return None
    if not (Path(miner).exists() or shutil.which(miner)):
        logger.debug("Refactoring mining skipped: miner %s not found", miner)
        return None
    workspace = str(conversation.workspace.working_dir)
    try:
        repo = _run_git(workspace, "rev-parse", "--show-toplevel")
    except Exception as e:
        logger.debug(
            "Refactoring mining skipped: workspace %s is not a git repo (%s)",
            workspace,
            e,
        )
        return None
    return miner, repo


def seed_refactoring_baseline(conversation: "LocalConversation") -> str | None:
    """Anchor mining to the working tree as it exists before any agent step.

    Snapshots the tree (parented on HEAD) so pre-existing uncommitted changes
    are attributed to the baseline instead of the agent's first step. Returns
    the snapshot sha, HEAD when the tree is clean, or None when mining is not
    applicable (disabled, miner missing, or workspace not a git repo).
    """
    resolved = _resolve_mining_target(conversation)
    if resolved is None:
        return None
    _, repo = resolved
    return snapshot_working_tree(repo) or _run_git(repo, "rev-parse", "HEAD")


def mine_step_refactorings(
    conversation: "LocalConversation", step: int, parent_commit: str | None = None
) -> tuple[str | None, list[dict[str, Any]]]:
    """Detect and log refactorings introduced by the step that just finished.

    ``parent_commit`` is the previous step's snapshot sha (None for the first
    step), keeping each mined diff scoped to a single step. Returns a tuple of
    the snapshot sha to chain from and the step's mined refactorings. The
    chain sha is the new snapshot, or ``parent_commit`` when the tree was
    unchanged, so a no-change step does not reset the baseline back to HEAD
    (which would re-mine older edits). It is None only when no snapshot could
    be created at all. Never raises: mining failures are recorded as an error
    record and logged.
    """
    resolved = _resolve_mining_target(conversation)
    if resolved is None:
        return None, []
    miner, repo = resolved

    record: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": str(conversation.state.id),
        "step": step,
        "workspace": str(conversation.workspace.working_dir),
        "repo_root": repo,
        "parent_commit": parent_commit,
    }
    started = time.monotonic()
    commit: str | None = None
    refactorings: list[dict[str, Any]] = []
    try:
        with _lock, _origin_remote(repo):
            commit = snapshot_working_tree(repo, parent_commit)
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
    return commit or parent_commit, refactorings
