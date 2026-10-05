"""Per-tool-call edit provenance attribution for local conversations.

Every tool call runs inside a provenance window: the workspace's text files
are scanned after execution and the delta against the previous window's state
is attributed to the category of the tool that ran:

- ``refactoring_tool``: structural refactoring tools (e.g. the IntelliJ IDEA
  MCP server's extract method tool), recognized by tool name;
- ``direct_edit``: everything else that mutates files, including shell
  commands (treated as direct edits by design) and built-in editors.

Line-level attribution uses last-writer-wins: a line in the final state of a
file is attributed to the last window that inserted or modified it, and
deleted lines count as churn of the window that deleted them. Both a churn
view (insertions + deletions per window) and a net view (surviving lines per
category vs the session baseline) are maintained, so an edit that is later
reverted still shows up in churn but disappears from the net view.

Because deltas are captured per window, shell-command and MCP tool edits are
attributed automatically; no tool needs to report its own edits. Two
documented approximations: (1) files mutated between windows by background
processes are attributed to the next window that detects them; (2) when
parallel tool calls mutate files concurrently, the combined delta is
attributed to whichever window is recorded first.

Records are appended as JSON lines joinable with the refactoring log via
``session_id``. The destination defaults to
``~/.openhands/edit_provenance.jsonl`` and can be overridden with
``OPENHANDS_EDIT_PROVENANCE_LOG``. Attribution can be disabled entirely with
a falsy ``OPENHANDS_EDIT_PROVENANCE`` value.

Note: the MCP client does not expose which configured server a tool came
from, so refactoring tools are recognized by tool name (exact or glob
pattern) configured via ``OPENHANDS_REFACTORING_TOOLS``.
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


EDIT_PROVENANCE_ENV_VAR = "OPENHANDS_EDIT_PROVENANCE"
EDIT_PROVENANCE_LOG_ENV_VAR = "OPENHANDS_EDIT_PROVENANCE_LOG"
REFACTORING_TOOLS_ENV_VAR = "OPENHANDS_REFACTORING_TOOLS"
DEFAULT_EDIT_PROVENANCE_LOG = Path.home() / ".openhands" / "edit_provenance.jsonl"

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


def get_provenance_log_path() -> Path:
    return Path(
        os.environ.get(EDIT_PROVENANCE_LOG_ENV_VAR) or DEFAULT_EDIT_PROVENANCE_LOG
    )


def is_edit_provenance_enabled() -> bool:
    """Whether edit provenance attribution is enabled (on by default)."""
    value = os.environ.get(EDIT_PROVENANCE_ENV_VAR)
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


class EditProvenanceLedger:
    """Tracks per-tool-call file deltas and folds them into line attribution."""

    def __init__(self, root: str, session_id: str):
        self.root = root
        self.session_id = session_id
        self.seq = 0
        self.churn: Counter[str] = Counter()
        self.net: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._seeded = False
        # Path -> content as of the last recorded window.
        self._current: dict[str, str] = {}
        # Path -> {line index -> category}; missing lines are BASELINE.
        self._provenance: dict[str, dict[int, str]] = {}
        # Path -> (size, mtime_ns) for the stat fast path in _scan.
        self._stats: dict[str, tuple[int, int]] = {}

    @contextmanager
    def window(self, tool_name: str) -> Generator[None]:
        """Attribute workspace changes made inside the block to ``tool_name``.

        Attribution must never alter tool behavior: seeding and recording
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
            logger.warning("Edit provenance baseline scan failed: %s", e)
        try:
            yield
        finally:
            try:
                self._record(tool_name, category)
            except Exception as e:
                logger.warning("Edit provenance attribution failed: %s", e)

    def line_categories(self, path: str) -> list[str]:
        """Per-line attribution for a tracked file's current content."""
        content = self._current.get(path)
        if content is None:
            return []
        prov = self._provenance.get(path, {})
        return [prov.get(i, BASELINE) for i in range(len(content.splitlines()))]

    def _scan(self) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
        """Read all tracked text files under the workspace root."""
        contents: dict[str, str] = {}
        stats: dict[str, tuple[int, int]] = {}
        # Never track the provenance log itself, even when it lives in the
        # workspace: its writes would be attributed to whichever tool ran.
        excluded = {str(get_provenance_log_path().resolve())}
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

    def _record(self, tool_name: str, category: str) -> None:
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
                insertions, deletions = self._fold(path, before, after, category)
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
            self.net = Counter(
                cat for prov in self._provenance.values() for cat in prov.values()
            )
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "session_id": self.session_id,
                "seq": self.seq,
                "tool_name": tool_name,
                "category": category,
                "files": files,
                "churn_total": dict(self.churn),
                "net_total": dict(self.net),
            }
        if files:
            self._append_log(record)

    def _fold(
        self,
        path: str,
        before: str | None,
        after: str | None,
        category: str,
    ) -> tuple[int, int]:
        """Attribute a before/after delta for one file; returns (ins, dels)."""
        if before is None:
            assert after is not None  # callers skip no-op (equal) deltas
            lines = after.splitlines()
            self._provenance[path] = dict.fromkeys(range(len(lines)), category)
            self.churn[category] += len(lines)
            return len(lines), 0
        if after is None:
            self.churn[category] += len(before.splitlines())
            self._provenance.pop(path, None)
            return 0, len(before.splitlines())

        old_prov = self._provenance.get(path, {})
        new_prov: dict[int, str] = {}
        matcher = SequenceMatcher(a=before.splitlines(), b=after.splitlines())
        insertions = deletions = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    cat = old_prov.get(i1 + k)
                    if cat is not None:
                        new_prov[j1 + k] = cat
                continue
            if tag != "insert":
                deletions += i2 - i1
            if tag != "delete":
                insertions += j2 - j1
                for j in range(j1, j2):
                    new_prov[j] = category
        self.churn[category] += insertions + deletions
        self._provenance[path] = new_prov
        return insertions, deletions

    def _append_log(self, record: dict[str, object]) -> None:
        try:
            log_path = get_provenance_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with _lock, log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.warning("Failed to write edit provenance log: %s", e)


# One ledger per conversation; entries die with the conversation object.
_ledgers: WeakKeyDictionary = WeakKeyDictionary()
_ledgers_lock = threading.Lock()


def _ledger_for(conversation: "LocalConversation") -> EditProvenanceLedger:
    with _ledgers_lock:
        ledger = _ledgers.get(conversation)
        if ledger is None:
            ledger = EditProvenanceLedger(
                root=str(conversation.workspace.working_dir),
                session_id=str(conversation.id),
            )
            _ledgers[conversation] = ledger
        return ledger


def execute_with_edit_provenance[T](
    conversation: "LocalConversation",
    tool_name: str,
    execute: Callable[[], T],
) -> T:
    """Run a tool call inside an edit-provenance attribution window.

    A no-op passthrough when attribution is disabled or the workspace cannot
    be tracked; attribution failures never break tool execution.
    """
    if not is_edit_provenance_enabled():
        return execute()
    try:
        ledger = _ledger_for(conversation)
    except Exception as e:
        logger.warning("Edit provenance tracking unavailable: %s", e)
        return execute()
    with ledger.window(tool_name):
        return execute()
