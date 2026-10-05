"""Track which tool made which file edits during a conversation.

Every tool call is checked for file edits: after the tool runs, the git
working tree is snapshotted as a dangling commit, parented on the previous
snapshot and authored according to the tool's category. Every changed line
is credited to the tool that last wrote it (git blame semantics): when a
tool deletes lines, the deletions count against that tool.

Tools fall into two categories:

- ``refactoring_tool``: structural refactoring tools, such as the IntelliJ
  IDEA extract method tool, recognized by tool name;
- ``direct_edit``: everything else that changes files, including shell
  commands and the built-in editors.

Two running totals are kept:

- ``final_lines``: lines present in the current code, per category. Work
  that is later reverted contributes nothing here.
- ``written_lines``: every line ever inserted or deleted, including
  reverted work.

The snapshots are dangling commits: no branch or ref points at them, so
``git log``, ``git status`` and IDEs never show them. They are discoverable
only by ``git fsck``, and ``git gc`` may eventually prune them; the JSONL
log keeps the attribution data independently.

Because edits are detected by snapshotting, shell commands and MCP tools
are handled automatically; tools do not have to report their own edits.
Only tools that can plausibly mutate files are tracked (the built-in
editors, the terminal, and refactoring tools), so read-only calls cost
nothing. Custom mutating tools opt in via ``OPENHANDS_MUTATING_TOOLS``.
Two approximations follow from this design: (1) files changed between
tracked tool calls by background processes are credited to the next
tracked tool call that notices them; (2) when parallel tool calls change
the same files at the same time, all of those changes are credited to
whichever call is checked first.

One record is appended per tool call that changed files, as a JSON line
joinable with the refactoring log via ``session_id``. Each changed file
entry carries ``attributed_lines`` (line number -> category) for the
agent-authored lines of the file's new state, so diffs can be annotated
after the session ends. The destination defaults to
``~/.openhands/edit_attribution.jsonl`` and can be overridden with
``OPENHANDS_EDIT_ATTRIBUTION_LOG``. Tracking can be disabled with a falsy
``OPENHANDS_EDIT_ATTRIBUTION`` value.

The workspace must be a git repository; otherwise tracking is skipped.
Note: the MCP client does not say which configured server a tool came from,
so refactoring tools are recognized by tool name (exact or glob pattern)
configured via ``OPENHANDS_REFACTORING_TOOLS``.
"""

import fnmatch
import json
import os
import subprocess
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

from openhands.sdk.logger import get_logger


if TYPE_CHECKING:
    from openhands.sdk.conversation.impl.local_conversation import LocalConversation

logger = get_logger(__name__)

EDIT_ATTRIBUTION_ENV_VAR = "OPENHANDS_EDIT_ATTRIBUTION"
EDIT_ATTRIBUTION_LOG_ENV_VAR = "OPENHANDS_EDIT_ATTRIBUTION_LOG"
REFACTORING_TOOLS_ENV_VAR = "OPENHANDS_REFACTORING_TOOLS"
DEFAULT_EDIT_LOG = Path.home() / ".openhands" / "edit_attribution.jsonl"

REFACTORING_TOOL = "refactoring_tool"
DIRECT_EDIT = "direct_edit"
BASELINE = "base"

# Tool names recognized as structural refactoring tools without configuration.
DEFAULT_REFACTORING_TOOL_NAMES = frozenset({"extract_method"})

# Tools that can plausibly mutate workspace files; only these run inside
# attribution windows, keeping read-only calls free of any overhead.
# Refactoring tools always qualify. Custom mutating tools opt in via
# ``OPENHANDS_MUTATING_TOOLS`` (exact names or glob patterns).
DEFAULT_MUTATING_TOOLS = frozenset(
    {
        "apply_patch",
        "edit",
        "file_editor",
        "planning_file_editor",
        "terminal",
        "write_file",
    }
)
MUTATING_TOOLS_ENV_VAR = "OPENHANDS_MUTATING_TOOLS"

# Snapshot commits are authored by category so `git blame` attributes each
# line. Committer identity is deterministic so git never fails on repos or
# hosts without a configured identity.
_BASE_AUTHOR = "openhands-edit-base"
_CATEGORY_AUTHORS = {
    REFACTORING_TOOL: "openhands-refactoring-tool",
    DIRECT_EDIT: "openhands-direct-edit",
}
_AUTHOR_CATEGORIES = {
    _BASE_AUTHOR: BASELINE,
    **{author: category for category, author in _CATEGORY_AUTHORS.items()},
}
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "openhands",
    "GIT_AUTHOR_EMAIL": "openhands@all-hands.dev",
    "GIT_COMMITTER_NAME": "openhands",
    "GIT_COMMITTER_EMAIL": "openhands@all-hands.dev",
}

_lock = threading.Lock()


def get_edit_log_path() -> Path:
    return Path(os.environ.get(EDIT_ATTRIBUTION_LOG_ENV_VAR) or DEFAULT_EDIT_LOG)


def is_edit_tracking_enabled() -> bool:
    """Whether edit tracking is enabled (on by default)."""
    value = os.environ.get(EDIT_ATTRIBUTION_ENV_VAR)
    if value is None:
        return True
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _env_tool_patterns(env_var: str, default: frozenset[str]) -> tuple[str, ...]:
    raw = os.environ.get(env_var)
    if raw is None:
        return tuple(default)
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def _matches_any(tool_name: str, entries: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatchcase(tool_name, e) if "*" in e or "?" in e else tool_name == e
        for e in entries
    )


def is_mutating_tool(tool_name: str) -> bool:
    """Whether this tool should run inside an attribution window."""
    if categorize_tool(tool_name) == REFACTORING_TOOL:
        return True
    return _matches_any(
        tool_name, _env_tool_patterns(MUTATING_TOOLS_ENV_VAR, DEFAULT_MUTATING_TOOLS)
    )


def categorize_tool(tool_name: str) -> str:
    """Classify a tool as a refactoring tool or a direct edit.

    Matching is exact against ``DEFAULT_REFACTORING_TOOL_NAMES`` plus any
    entry of ``OPENHANDS_REFACTORING_TOOLS``; entries containing ``*`` or
    ``?`` are treated as glob patterns so families of refactoring tools can
    be enabled at once. Everything else is a direct edit.
    """
    if tool_name in DEFAULT_REFACTORING_TOOL_NAMES:
        return REFACTORING_TOOL
    configured = _env_tool_patterns(REFACTORING_TOOLS_ENV_VAR, frozenset())
    return REFACTORING_TOOL if _matches_any(tool_name, configured) else DIRECT_EDIT


def _run_git(
    repo: str, *args: str, author: str | None = None, index_file: str | None = None
) -> str:
    env = dict(os.environ) | _GIT_IDENTITY
    if author is not None:
        env["GIT_AUTHOR_NAME"] = author
        env["GIT_AUTHOR_EMAIL"] = f"{author}@openhands.local"
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


def _snapshot(
    repo: str,
    parent: str | None,
    author: str,
    exclude_relpath: str | None = None,
) -> str | None:
    """Capture the working tree as a dangling commit authored by ``author``.

    A temporary index is used so HEAD, the real index, and the working tree
    are all left untouched. The commit is parented on ``parent`` (when given)
    so consecutive snapshots diff against each other. Returns None when the
    tree matches the parent's tree.
    """
    fd, index_file = tempfile.mkstemp(prefix="openhands-editattr-index-")
    os.close(fd)
    # Git refuses to build an index on top of an empty file; let it create one.
    os.unlink(index_file)
    try:
        _run_git(repo, "add", "-A", "--", ".", index_file=index_file)
        if exclude_relpath:
            _run_git(
                repo,
                "rm",
                "--cached",
                "--ignore-unmatch",
                "--",
                exclude_relpath,
                index_file=index_file,
            )
        tree = _run_git(repo, "write-tree", index_file=index_file)
        if parent is not None:
            parent_tree = _run_git(repo, "rev-parse", f"{parent}^{{tree}}")
            if tree == parent_tree:
                return None
        args = ["commit-tree", tree, "-m", "edit tracking snapshot"]
        if parent is not None:
            args += ["-p", parent]
        return _run_git(repo, *args, author=author, index_file=index_file)
    finally:
        os.unlink(index_file)


def _changed_files(repo: str, parent: str, child: str) -> list[tuple[str, int, int]]:
    """Repo-relative (path, insertions, deletions) between two snapshots."""
    out = _run_git(repo, "diff", "--numstat", "--no-renames", parent, child)
    files: list[tuple[str, int, int]] = []
    for line in out.splitlines():
        ins, dels, path = line.split("\t", 2)
        if ins == "-" or dels == "-":
            continue  # binary file
        files.append((path, int(ins), int(dels)))
    return files


def _blame_categories(repo: str, commit: str, relpath: str) -> list[str] | None:
    """Per-line attribution of a file at ``commit``, or None if unavailable."""
    try:
        out = _run_git(repo, "blame", "--line-porcelain", commit, "--", relpath)
    except RuntimeError:
        return None
    categories: list[str] = []
    author: str | None = None
    for line in out.splitlines():
        if line.startswith("\t"):
            categories.append(_AUTHOR_CATEGORIES.get(author or "", BASELINE))
        elif line.startswith("author "):
            author = line[len("author ") :]
    return categories


def _exclude_relpath(repo: str) -> str | None:
    """Log path relative to the repo when it lives inside it, else None."""
    relpath = os.path.relpath(str(get_edit_log_path().resolve()), repo)
    return None if relpath.startswith("..") else relpath


def _worktree_changed(repo: str, parent: str, exclude_relpath: str | None) -> bool:
    """Cheap check for whether the worktree differs from ``parent``.

    Two read-only git calls; far cheaper than building a snapshot index for
    the common case of tools that did not touch any files. The snapshot
    itself remains the authority — this gate may only over-report.
    """
    pathspecs = [
        "--",
        ".",
        *(f":(exclude){exclude_relpath}" if exclude_relpath else []),
    ]
    if _run_git(repo, "diff", "--name-only", parent, *pathspecs):
        return True
    return (
        _run_git(repo, "ls-files", "--others", "--exclude-standard", *pathspecs) != ""
    )


class EditLedger:
    """Credits each changed line to the tool call that wrote it.

    The ledger is a chain of dangling git snapshot commits; per-line
    attribution is read back with ``git blame``.
    """

    def __init__(self, root: str, session_id: str):
        self.root = root
        self.session_id = session_id
        self.seq = 0
        # Per category: lines in the current code / lines ever written.
        self.final_lines: Counter[str] = Counter()
        self.written_lines: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._seeded = False
        self._unusable = False
        self._repo: str | None = None
        self._parent: str | None = None  # latest snapshot sha
        # Repo-relative paths edited by tools, with their cached line counts.
        self._final_cache: dict[str, Counter[str]] = {}

    @contextmanager
    def track_edits(self, tool_name: str) -> Generator[None]:
        """Credit any file edits made inside this block to ``tool_name``.

        Only tools that can plausibly mutate files are tracked; read-only
        calls are a no-op. Tracking must never alter tool behavior:
        snapshot failures are logged and swallowed, including in the finally
        block, so a tool's own exception propagates untouched.
        """
        if not is_mutating_tool(tool_name):
            yield
            return
        category = categorize_tool(tool_name)
        try:
            with self._lock:
                if not self._seeded:
                    self._seed()
        except Exception as e:
            logger.warning("Edit tracking initial snapshot failed: %s", e)
        try:
            yield
        finally:
            try:
                self._check_for_edits(tool_name, category)
            except Exception as e:
                logger.warning("Edit tracking failed: %s", e)

    def line_categories(self, path: str) -> list[str]:
        """Which category wrote each line of a tracked file's current content."""
        if self._repo is None or self._parent is None:
            return []
        relpath = os.path.relpath(os.path.realpath(path), self._repo)
        if relpath.startswith(".."):
            return []
        return _blame_categories(self._repo, self._parent, relpath) or []

    def _seed(self) -> None:
        self._seeded = True
        try:
            self._repo = os.path.realpath(
                _run_git(self.root, "rev-parse", "--show-toplevel")
            )
        except Exception as e:
            self._unusable = True
            logger.warning(
                "Edit tracking skipped: %s is not a git repo (%s)", self.root, e
            )
            return
        exclude = _exclude_relpath(self._repo)
        # On a clean worktree the baseline is HEAD itself — no snapshot
        # needed. Any line blame traces back beyond our chain (unknown
        # authors) maps to BASELINE either way.
        try:
            head = _run_git(self._repo, "rev-parse", "HEAD")
            clean = not _run_git(self._repo, "status", "--porcelain")
        except RuntimeError:
            head, clean = None, False
        if head and clean:
            self._parent = head
        else:
            self._parent = _snapshot(self._repo, None, _BASE_AUTHOR, exclude)

    def _check_for_edits(self, tool_name: str, category: str) -> None:
        with self._lock:
            repo, previous = self._repo, self._parent
            if self._unusable or repo is None or previous is None:
                return
            exclude = _exclude_relpath(repo)
            if not _worktree_changed(repo, previous, exclude):
                return  # tool did not touch any files
            sha = _snapshot(repo, previous, _CATEGORY_AUTHORS[category], exclude)
            if sha is None:
                return  # nothing changed
            self.seq += 1
            files: list[dict[str, object]] = []
            for relpath, insertions, deletions in _changed_files(repo, previous, sha):
                categories = _blame_categories(repo, sha, relpath) or []
                attributed = {
                    str(i + 1): cat
                    for i, cat in enumerate(categories)
                    if cat != BASELINE
                }
                files.append(
                    {
                        "path": relpath,
                        "insertions": insertions,
                        "deletions": deletions,
                        "attributed_lines": attributed,
                    }
                )
                self.written_lines[category] += insertions + deletions
                self._final_cache[relpath] = Counter(attributed.values())
            self._parent = sha
            self.final_lines = Counter()
            for counts in self._final_cache.values():
                self.final_lines.update(counts)
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": self.session_id,
                "seq": self.seq,
                "tool_name": tool_name,
                "category": category,
                "commit": sha,
                "parent": previous,
                "files": files,
                "written_lines_total": dict(self.written_lines),
                "final_lines_total": dict(self.final_lines),
            }
        if files:
            self._append_log(record)

    def _append_log(self, record: dict[str, object]) -> None:
        try:
            log_path = get_edit_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with _lock, log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.warning("Failed to write edit log: %s", e)


# One ledger per conversation; entries die with the conversation object.
_ledgers: WeakKeyDictionary = WeakKeyDictionary()
_ledgers_lock = threading.Lock()


def _ledger_for(conversation: "LocalConversation") -> EditLedger:
    with _ledgers_lock:
        ledger = _ledgers.get(conversation)
        if ledger is None:
            ledger = EditLedger(
                root=str(conversation.workspace.working_dir),
                session_id=str(conversation.id),
            )
            _ledgers[conversation] = ledger
        return ledger


def run_with_edit_tracking[T](
    conversation: "LocalConversation",
    tool_name: str,
    execute: Callable[[], T],
) -> T:
    """Run a tool call with edit tracking around it.

    A no-op passthrough when tracking is disabled or the workspace cannot be
    tracked; tracking problems never break tool execution.
    """
    if not is_edit_tracking_enabled():
        return execute()
    try:
        ledger = _ledger_for(conversation)
    except Exception as e:
        logger.warning("Edit tracking unavailable: %s", e)
        return execute()
    with ledger.track_edits(tool_name):
        return execute()
