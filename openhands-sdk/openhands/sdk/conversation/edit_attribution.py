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
are handled automatically; tools do not have to report their own edits. Two
approximations follow from this design: (1) files changed between tool
calls by background processes are credited to the next tool call that
notices them; (2) when parallel tool calls change the same files at the
same time, all of those changes are credited to whichever call is checked
first.

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
import re
import subprocess
import tempfile
import threading
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
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
UNATTRIBUTED = "unattributed"

# Tool names recognized as structural refactoring tools without configuration.
DEFAULT_REFACTORING_TOOL_NAMES = frozenset({"extract_method"})

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


def _parse_hunks(diff_text: str) -> list[dict[str, Any]]:
    """Parse a single-file unified diff into ordered -U0 hunks."""
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk_re = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            m = hunk_re.match(line)
            if m is None:
                continue
            if current is not None:
                hunks.append(current)
            current = {
                "old_start": int(m.group(1)),
                "old_count": int(m.group(2)) if m.group(2) is not None else 1,
                "new_start": int(m.group(3)),
                "new_count": int(m.group(4)) if m.group(4) is not None else 1,
                "removed": [],
                "added": [],
            }
        elif current is not None:
            if line.startswith("-"):
                current["removed"].append(line[1:])
            elif line.startswith("+"):
                current["added"].append(line[1:])
    if current is not None:
        hunks.append(current)
    return hunks


def _simulate_file(
    base_lines: list[str],
    records: list[dict[str, Any]],
    path: str,
    diff_fetch: Callable[[str, str, str], str | None],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Replay snapshot-chain diffs over the baseline content.

    ``diff_fetch(parent_sha, commit_sha, path)`` returns the -U0 diff text
    for one file between two snapshots, or None when unavailable. Returns
    (all_base_line_states, final_entries). Base line states carry
    ``deleted_by``: the category of the snapshot whose diff last removed
    them (None if present at the end). Final entries of kind ``new`` carry
    the category of the snapshot that inserted them. Returns None when a
    diff no longer applies (e.g. pruned snapshot commits).

    A deleted baseline line is considered restored (net zero, back to the
    baseline) only when identical content re-enters near where it was
    deleted; otherwise identical text inserted elsewhere is a new line of
    the inserting category. This keeps brace-only or blank-line collisions
    from misattributing net changes. ``last_deleted_by`` survives
    restorations, so callers can still attribute a line git represents as
    moved (delete + re-insert at a different position).
    """
    base = [
        {
            "kind": "base",
            "text": t,
            "deleted_by": None,
            "last_deleted_by": None,
            "pos": i,
        }
        for i, t in enumerate(base_lines)
    ]
    entries = list(base)
    for rec in records:
        if path not in {f["path"] for f in rec["files"]}:
            continue
        diff_text = diff_fetch(rec["parent"], rec["commit"], path)
        if diff_text is None:
            return None
        shift = 0
        for hunk in _parse_hunks(diff_text):
            # A zero-count old range ("-l,0") means "insert after old line l",
            # not before it.
            start = hunk["old_start"] - 1 + shift
            if hunk["old_count"] == 0:
                start += 1
            if start < 0 or start + hunk["old_count"] > len(entries):
                return None
            removed = entries[start : start + hunk["old_count"]]
            del entries[start : start + hunk["old_count"]]
            for offset, entry in enumerate(removed):
                if entry["kind"] == "base":
                    entry["deleted_by"] = rec["category"]
                    entry["last_deleted_by"] = rec["category"]
                    entry["pos"] = start + offset
            delta = len(hunk["added"]) - hunk["old_count"]
            for entry in base:
                if entry["deleted_by"] is None:
                    continue
                if entry["pos"] >= start + hunk["old_count"]:
                    entry["pos"] += delta
                elif entry["pos"] >= start:
                    entry["pos"] = start
            insert_idx = start
            for text in hunk["added"]:
                candidates = [e for e in base if e["deleted_by"] and e["text"] == text]
                if candidates:
                    best = min(
                        candidates,
                        key=lambda e: (abs(e["pos"] - insert_idx), e["pos"]),
                    )
                    best["deleted_by"] = None
                    entries.insert(insert_idx, best)
                else:
                    entries.insert(
                        insert_idx,
                        {
                            "kind": "new",
                            "text": text,
                            "category": rec["category"],
                        },
                    )
                insert_idx += 1
            shift += delta
    return base, entries


def net_line_attribution(
    repo: str,
    records: list[dict[str, Any]],
    base_sha: str,
    commit_sha: str | None,
) -> dict[str, dict[str, int]]:
    """Net lines contributed to the final commit, per attribution category.

    Counts come from git, not from the ledger's running totals: the
    baseline -> commit diff is the ground truth for which lines the session
    inserted and deleted. Each inserted line is attributed via the ledger's
    final per-line blame map (``attributed_lines``: last writer of that line
    in the final content); each deleted line via a replay of the
    snapshot-chain diffs (last snapshot that removed it). Lines deleted and
    later restored to their baseline content are net zero and stay with the
    baseline. Files where the replay cannot be validated against the commit
    (e.g. an IDE changed files after the last tool call) fall back to
    ``unattributed`` so totals always reconcile with the commit.

    ``records`` are the JSONL records logged by :class:`EditLedger` for one
    session (same ``session_id``); each must carry ``category``, ``parent``,
    ``commit`` and per-file entries with ``path`` and ``attributed_lines``.
    ``commit_sha`` may be None (no changes committed), yielding all-zero
    counts.
    """
    categories = [REFACTORING_TOOL, DIRECT_EDIT, UNATTRIBUTED]
    result = {c: {"inserted": 0, "deleted": 0} for c in categories}
    if commit_sha is None:
        return result

    def numstat(sha_a: str, sha_b: str) -> dict[str, tuple[int, int]]:
        out = _run_git(repo, "diff", "--numstat", "--no-renames", sha_a, sha_b)
        files: dict[str, tuple[int, int]] = {}
        for line in out.splitlines():
            ins, dels, path = line.split("\t", 2)
            if ins == "-" or dels == "-":
                continue
            files[path] = (int(ins), int(dels))
        return files

    def show(sha: str, path: str) -> list[str]:
        try:
            content = _run_git(repo, "show", f"{sha}:{path}")
        except RuntimeError:
            return []
        return content.splitlines() if content else []

    def diff_text(sha_a: str, sha_b: str, path: str) -> str | None:
        try:
            return _run_git(repo, "diff", "-U0", sha_a, sha_b, "--", path)
        except RuntimeError:
            return None

    def diff_positions(diff: str) -> tuple[list[int], list[int]]:
        """1-based (removed old-file, added new-file) line numbers."""
        removed_pos: list[int] = []
        added_pos: list[int] = []
        for hunk in _parse_hunks(diff):
            for i in range(hunk["old_count"]):
                removed_pos.append(hunk["old_start"] + i)
            for i in range(hunk["new_count"]):
                added_pos.append(hunk["new_start"] + i)
        return removed_pos, added_pos

    commit_files = numstat(base_sha, commit_sha)
    touched_paths = set(commit_files)
    for rec in records:
        touched_paths.update(f["path"] for f in rec["files"])

    for path in sorted(touched_paths):
        ins_total, dels_total = commit_files.get(path, (0, 0))
        if not ins_total and not dels_total:
            continue
        base_lines = show(base_sha, path)
        commit_lines = show(commit_sha, path)
        recs = [r for r in records if path in {f["path"] for f in r["files"]}]
        simulated = _simulate_file(base_lines, recs, path, diff_text)
        # The replay must reproduce the committed content exactly, otherwise
        # the snapshot chain diverged from the commit and per-line
        # attribution is unreliable.
        if simulated is None or [e["text"] for e in simulated[1]] != commit_lines:
            logger.warning(
                "Could not attribute %s (commit != replayed snapshots); "
                "counting %d+/-%d- as unattributed",
                path,
                ins_total,
                dels_total,
            )
            result[UNATTRIBUTED]["inserted"] += ins_total
            result[UNATTRIBUTED]["deleted"] += dels_total
            continue
        base_state, entries = simulated

        attributed: dict[int, str] = {}
        for rec in reversed(recs):
            for f in rec["files"]:
                if f["path"] == path and f.get("attributed_lines"):
                    attributed = {int(k): v for k, v in f["attributed_lines"].items()}
                    break
            if attributed:
                break

        removed_pos, added_pos = diff_positions(
            diff_text(base_sha, commit_sha, path) or ""
        )
        for pos in added_pos:
            category = attributed.get(pos)
            if category is None:
                entry = entries[pos - 1] if pos - 1 < len(entries) else None
                if entry is not None and entry["kind"] == "new":
                    category = entry["category"]
            result[category or UNATTRIBUTED]["inserted"] += 1
        for pos in removed_pos:
            base_entry = base_state[pos - 1] if pos - 1 < len(base_state) else None
            category = None
            if base_entry is not None:
                category = base_entry["deleted_by"] or base_entry["last_deleted_by"]
            result[category or UNATTRIBUTED]["deleted"] += 1
    return result


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

        Tracking must never alter tool behavior: snapshot failures are
        logged and swallowed, including in the finally block, so a tool's
        own exception propagates untouched.
        """
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
        # Parent the baseline on HEAD when one exists, so blame on lines the
        # session never touched continues into the repo's real history. Only
        # a pre-session uncommitted delta gets the base author.
        try:
            head = _run_git(self._repo, "rev-parse", "HEAD")
        except RuntimeError:
            head = None  # no commits yet
        self._parent = _snapshot(self._repo, head, _BASE_AUTHOR, exclude) or head

    def _check_for_edits(self, tool_name: str, category: str) -> None:
        with self._lock:
            repo, previous = self._repo, self._parent
            if self._unusable or repo is None or previous is None:
                return
            exclude = _exclude_relpath(repo)
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
