"""Audit log for direct file edits performed by built-in write tools.

Every mutating edit is appended as one JSON object per line so edits can be
traced no matter which agent or conversation triggered them. The destination
defaults to ``~/.openhands/edit_audit.jsonl`` and can be overridden with the
``OPENHANDS_EDIT_AUDIT_LOG`` environment variable.
"""

import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from openhands.sdk.conversation import LocalConversation

EDIT_AUDIT_LOG_ENV_VAR = "OPENHANDS_EDIT_AUDIT_LOG"
DEFAULT_EDIT_AUDIT_LOG = Path.home() / ".openhands" / "edit_audit.jsonl"

_FILE_EDIT_COMMANDS = frozenset({"create", "str_replace", "insert", "undo_edit"})

_PATCH_TARGET_RE = re.compile(
    r"^\*\*\* (?:Update|Add|Delete|Move) File: (.+)$", re.MULTILINE
)

_lock = threading.Lock()


def get_edit_audit_log_path() -> Path:
    return Path(os.environ.get(EDIT_AUDIT_LOG_ENV_VAR) or DEFAULT_EDIT_AUDIT_LOG)


def is_file_edit_command(command: str) -> bool:
    return command in _FILE_EDIT_COMMANDS


def patch_target_paths(patch: str, workspace_root: str | None = None) -> list[Path]:
    """Extract the file paths an apply_patch operation targets."""
    root = Path(workspace_root) if workspace_root is not None else None
    paths = []
    for raw in _PATCH_TARGET_RE.findall(patch):
        path = Path(raw)
        if root is not None and not path.is_absolute():
            path = root / path
        paths.append(path)
    return paths


def conversation_audit_id(
    conversation: "LocalConversation | None",
) -> str | None:
    return str(conversation.id) if conversation is not None else None


def log_file_edit(
    *,
    tool: str,
    command: str,
    paths: list[Path],
    status: str,
    conversation_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    entry = {
        "timestamp": datetime.now(UTC).isoformat(),
        "conversation_id": conversation_id,
        "tool": tool,
        "command": command,
        "paths": sorted({str(p.resolve()) for p in paths}),
        "status": status,
        "params": params or {},
    }
    try:
        log_path = get_edit_audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with _lock, log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Auditing must never break tool execution.
        pass
