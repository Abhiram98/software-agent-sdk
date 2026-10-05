"""Track which tool made which file edits during a conversation.

Every tool call is checked for file edits: after the tool runs, the text
files in the workspace are scanned, and every changed line is credited to
the tool that just ran.

Tools fall into two categories:

- ``refactoring_tool``: structural refactoring tools, such as the IntelliJ
  IDEA extract method tool, recognized by tool name;
- ``direct_edit``: everything else that changes files, including shell
  commands and the built-in editors.

A line is credited to the tool that last wrote it. When a tool deletes
lines, the deletions count against that tool. Two running totals are kept:

- ``final_lines``: lines present in the current code, per category. Work
  that is later reverted contributes nothing here.
- ``written_lines``: every line ever inserted or deleted, including
  reverted work.

Because edits are detected by scanning, shell commands and MCP tools are
handled automatically; tools do not have to report their own edits. Two
approximations follow from this design: (1) files changed between tool
calls by background processes are credited to the next tool call that
notices them; (2) when parallel tool calls change the same files at the
same time, all of those changes are credited to whichever call is checked
first.

One record is appended per tool call that changed files, as a JSON line
joinable with the refactoring log via ``session_id``. The destination
defaults to ``~/.openhands/edit_attribution.jsonl`` and can be overridden
with ``OPENHANDS_EDIT_ATTRIBUTION_LOG``. Tracking can be disabled with a
falsy ``OPENHANDS_EDIT_ATTRIBUTION`` value.

Note: the MCP client does not say which configured server a tool came from,
so refactoring tools are recognized by tool name (exact or glob pattern)
configured via ``OPENHANDS_REFACTORING_TOOLS``.
"""

import fnmatch
import json
import os
import threading
from collections import Counter
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from difflib import SequenceMatcher
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

# Files larger than this are skipped (attribution focuses on source code).
MAX_FILE_BYTES = 1_000_000

# Directories that never carry agent-authored source edits.
SKIP_DIRS = frozenset(
    {
        ".agent_tmp",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".openhands",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)

_lock = threading.Lock()


def get_edit_log_path() -> Path:
    return Path(os.environ.get(EDIT_ATTRIBUTION_LOG_ENV_VAR) or DEFAULT_EDIT_LOG)


def is_edit_tracking_enabled() -> bool:
    """Whether edit tracking is enabled (on by default)."""
    value = os.environ.get(EDIT_ATTRIBUTION_ENV_VAR)
    if value is None:
        return True
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _configured_refactoring_tools() -> tuple[str, ...]:
    raw = os.environ.get(REFACTORING_TOOLS_ENV_VAR, "")
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


def categorize_tool(tool_name: str) -> str:
    """Classify a tool as a refactoring tool or a direct edit.

    Matching is exact against ``DEFAULT_REFACTORING_TOOL_NAMES`` plus any
    entry of ``OPENHANDS_REFACTORING_TOOLS``; entries containing ``*`` or
    ``?`` are treated as glob patterns so families of refactoring tools can
    be enabled at once. Everything else is a direct edit.
    """
    configured = _configured_refactoring_tools()
    names = DEFAULT_REFACTORING_TOOL_NAMES | {
        c for c in configured if "*" not in c and "?" not in c
    }
    if tool_name in names:
        return REFACTORING_TOOL
    return (
        REFACTORING_TOOL
        if any(
            fnmatch.fnmatchcase(tool_name, c)
            for c in configured
            if "*" in c or "?" in c
        )
        else DIRECT_EDIT
    )


class EditLedger:
    """Credits each changed line to the tool call that wrote it."""

    def __init__(self, root: str, session_id: str):
        self.root = root
        self.session_id = session_id
        self.seq = 0
        # Per category: lines in the current code / lines ever written.
        self.final_lines: Counter[str] = Counter()
        self.written_lines: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._seeded = False
        # Path -> content as of the last checked tool call.
        self._current: dict[str, str] = {}
        # Path -> {line index -> category}; lines missing here are BASELINE.
        self._categories: dict[str, dict[int, str]] = {}
        # Path -> (size, mtime_ns) so unchanged files are not re-read.
        self._stats: dict[str, tuple[int, int]] = {}

    @contextmanager
    def track_edits(self, tool_name: str) -> Generator[None]:
        """Credit any file edits made inside this block to ``tool_name``.

        Tracking must never alter tool behavior: seeding and recording
        failures are logged and swallowed, including in the finally block, so
        a tool's own exception propagates untouched.
        """
        category = categorize_tool(tool_name)
        try:
            with self._lock:
                if not self._seeded:
                    self._current, self._stats = self._scan()
                    self._seeded = True
        except Exception as e:
            logger.warning("Edit tracking initial scan failed: %s", e)
        try:
            yield
        finally:
            try:
                self._check_for_edits(tool_name, category)
            except Exception as e:
                logger.warning("Edit tracking failed: %s", e)

    def line_categories(self, path: str) -> list[str]:
        """Which category wrote each line of a tracked file's current content."""
        content = self._current.get(path)
        if content is None:
            return []
        categories = self._categories.get(path, {})
        return [categories.get(i, BASELINE) for i in range(len(content.splitlines()))]

    def _scan(self) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
        """Read all tracked text files under the workspace root."""
        contents: dict[str, str] = {}
        stats: dict[str, tuple[int, int]] = {}
        # Never track our own log file, even when it lives in the workspace:
        # its writes would be credited to whichever tool just ran.
        excluded = {str(get_edit_log_path().resolve())}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in filenames:
                path = os.path.join(dirpath, name)
                if os.path.realpath(path) in excluded:
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                key = (st.st_size, st.st_mtime_ns)
                if path in self._stats and self._stats[path] == key:
                    contents[path] = self._current[path]
                    stats[path] = key
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                except OSError:
                    continue
                if b"\x00" in data[:8192]:
                    continue
                contents[path] = data.decode("utf-8", errors="replace")
                stats[path] = key
        return contents, stats

    def _check_for_edits(self, tool_name: str, category: str) -> None:
        record: dict[str, object]
        with self._lock:
            self.seq += 1
            post, post_stats = self._scan()
            files: list[dict[str, object]] = []
            for path in sorted(set(post) | set(self._current)):
                before = self._current.get(path)
                after = post.get(path)
                if before == after:
                    continue
                insertions, deletions = self._credit(path, before, after, category)
                status = (
                    "created"
                    if before is None
                    else "deleted"
                    if after is None
                    else "modified"
                )
                files.append(
                    {
                        "path": path,
                        "status": status,
                        "insertions": insertions,
                        "deletions": deletions,
                    }
                )
            self._current = post
            self._stats = post_stats
            self.final_lines = Counter(
                cat
                for categories in self._categories.values()
                for cat in categories.values()
            )
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": self.session_id,
                "seq": self.seq,
                "tool_name": tool_name,
                "category": category,
                "files": files,
                "written_lines_total": dict(self.written_lines),
                "final_lines_total": dict(self.final_lines),
            }
        if files:
            self._append_log(record)

    def _credit(
        self,
        path: str,
        before: str | None,
        after: str | None,
        category: str,
    ) -> tuple[int, int]:
        """Credit the lines changed between the old and new content of one
        file to ``category``; returns (inserted, deleted) line counts."""
        if before is None:
            assert after is not None  # callers skip unchanged files
            lines = after.splitlines()
            self._categories[path] = dict.fromkeys(range(len(lines)), category)
            self.written_lines[category] += len(lines)
            return len(lines), 0
        if after is None:
            self.written_lines[category] += len(before.splitlines())
            self._categories.pop(path, None)
            return 0, len(before.splitlines())

        old_categories = self._categories.get(path, {})
        new_categories: dict[int, str] = {}
        matcher = SequenceMatcher(a=before.splitlines(), b=after.splitlines())
        insertions = deletions = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    cat = old_categories.get(i1 + k)
                    if cat is not None:
                        new_categories[j1 + k] = cat
                continue
            if tag != "insert":
                deletions += i2 - i1
            if tag != "delete":
                insertions += j2 - j1
                for j in range(j1, j2):
                    new_categories[j] = category
        self.written_lines[category] += insertions + deletions
        self._categories[path] = new_categories
        return insertions, deletions

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
    scanned; tracking problems never break tool execution.
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
