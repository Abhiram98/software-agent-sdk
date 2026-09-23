import json
import uuid
from unittest.mock import MagicMock

import pytest

from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.tools.apply_patch.definition import (
    ApplyPatchAction,
    ApplyPatchExecutor,
)
from openhands.tools.edit_audit import (
    EDIT_AUDIT_LOG_ENV_VAR,
    is_file_edit_command,
    patch_target_paths,
)
from openhands.tools.file_editor.definition import FileEditorAction
from openhands.tools.file_editor.impl import FileEditorExecutor


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    log_path = tmp_path / "audit" / "edit_audit.jsonl"
    monkeypatch.setenv(EDIT_AUDIT_LOG_ENV_VAR, str(log_path))
    return log_path


def read_entries(log_path):
    return [json.loads(line) for line in log_path.read_text().splitlines()]


def test_file_editor_logs_mutating_commands_not_view(audit_log, tmp_path):
    executor = FileEditorExecutor(workspace_root=str(tmp_path))
    target = tmp_path / "hello.txt"

    executor(
        FileEditorAction(command="create", path=str(target), file_text="hello world")
    )
    executor(
        FileEditorAction(
            command="str_replace",
            path=str(target),
            old_str="hello",
            new_str="goodbye",
        )
    )
    executor(FileEditorAction(command="view", path=str(target)))

    entries = read_entries(audit_log)
    assert [(e["tool"], e["command"]) for e in entries] == [
        ("file_editor", "create"),
        ("file_editor", "str_replace"),
    ]
    assert all(e["status"] == "ok" for e in entries)
    assert all(e["paths"] == [str(target)] for e in entries)
    assert all("timestamp" in e for e in entries)
    assert entries[0]["params"] == {"path": str(target), "file_text": "hello world"}
    assert entries[1]["params"] == {
        "path": str(target),
        "old_str": "hello",
        "new_str": "goodbye",
    }


def test_file_editor_logs_conversation_id_and_params(audit_log, tmp_path):
    conversation = MagicMock(spec=LocalConversation)
    conversation.id = uuid.uuid4()
    executor = FileEditorExecutor(workspace_root=str(tmp_path))
    target = tmp_path / "hello.txt"

    executor(
        FileEditorAction(command="create", path=str(target), file_text="hello"),
        conversation,
    )

    entry = read_entries(audit_log)[0]
    assert entry["conversation_id"] == str(conversation.id)
    assert entry["params"] == {"path": str(target), "file_text": "hello"}


def test_file_editor_logs_failed_edits(audit_log, tmp_path):
    executor = FileEditorExecutor(workspace_root=str(tmp_path))
    missing = tmp_path / "missing.txt"

    result = executor(
        FileEditorAction(
            command="str_replace", path=str(missing), old_str="a", new_str="b"
        )
    )

    assert result.is_error
    entries = read_entries(audit_log)
    assert len(entries) == 1
    assert entries[0]["status"] == "error"
    assert entries[0]["paths"] == [str(missing)]


def test_file_editor_logs_blocked_edits(audit_log, tmp_path):
    allowed = tmp_path / "allowed.txt"
    executor = FileEditorExecutor(
        workspace_root=str(tmp_path), allowed_edits_files=[str(allowed)]
    )

    result = executor(
        FileEditorAction(
            command="str_replace",
            path=str(tmp_path / "other.txt"),
            old_str="a",
            new_str="b",
        )
    )

    assert result.is_error
    entries = read_entries(audit_log)
    assert len(entries) == 1
    assert entries[0]["status"] == "blocked"


def test_apply_patch_logs_target_files(audit_log, tmp_path):
    executor = ApplyPatchExecutor(workspace_root=str(tmp_path))
    conversation = MagicMock(spec=LocalConversation)
    conversation.id = uuid.uuid4()
    (tmp_path / "a.txt").write_text("foo\n")
    (tmp_path / "b.txt").write_text("bar\n")

    patch = """*** Begin Patch
*** Update File: a.txt
@@
-foo
+foot
*** Add File: b2.txt
+new
*** Delete File: b.txt
*** End Patch
"""
    result = executor(ApplyPatchAction(patch=patch), conversation)

    assert not result.is_error
    entries = read_entries(audit_log)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["conversation_id"] == str(conversation.id)
    assert entry["tool"] == "apply_patch"
    assert entry["status"] == "ok"
    assert entry["params"]["patch"] == patch
    assert entry["paths"] == [
        str(tmp_path / "a.txt"),
        str(tmp_path / "b.txt"),
        str(tmp_path / "b2.txt"),
    ]


def test_is_file_edit_command():
    assert is_file_edit_command("create")
    assert is_file_edit_command("str_replace")
    assert is_file_edit_command("insert")
    assert is_file_edit_command("undo_edit")
    assert not is_file_edit_command("view")


def test_patch_target_paths_resolves_relative_to_workspace(tmp_path):
    patch = "*** Begin Patch\n*** Update File: src/x.py\n@@\n*** End Patch\n"

    paths = patch_target_paths(patch, str(tmp_path))

    assert paths == [tmp_path / "src" / "x.py"]
