import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from openhands.sdk.conversation.edit_attribution import (
    BASELINE,
    DIRECT_EDIT,
    REFACTORING_TOOL,
    UNATTRIBUTED,
    EditLedger,
    categorize_tool,
    get_edit_log_path,
    is_edit_tracking_enabled,
    net_line_attribution,
    run_with_edit_tracking,
)


BASE_FILE = """\
class Calculator {
    int process(List<Integer> items) {
        int total = 0;
        for (int it : items) {
            total += it;
        }
        return total;
    }
}
"""

SPLIT_FILE = """\
class Calculator {
    int process(List<Integer> items) {
        return subMethod(items);
    }

    int subMethod(List<Integer> items) {
        int total = 0;
        for (int it : items) {
            total += it;
        }
        return total;
    }
}
"""

EXTRACTED_FILE = """\
class Calculator {
    int process(List<Integer> items) {
        return subMethod(items);
    }

    private int subMethod(List<Integer> items) {
        int total = 0;
        for (int it : items) {
            total += it;
        }
        return total;
    }
}
"""

PROTECTED_FILE = EXTRACTED_FILE.replace(
    "    private int subMethod", "    protected int subMethod"
)


class StubConversation:
    def __init__(self, root):
        self.workspace = SimpleNamespace(working_dir=str(root))
        self.id = "test-session"


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # The log lives outside the workspace, like ~/.openhands does relative
    # to a typical repo checkout.
    monkeypatch.setenv("OPENHANDS_EDIT_ATTRIBUTION_LOG", str(tmp_path / "prov.jsonl"))
    monkeypatch.delenv("OPENHANDS_REFACTORING_TOOLS", raising=False)
    ws = tmp_path / "workspace"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    (ws / "Calculator.java").write_text(BASE_FILE)
    return ws


def _run(conversation, tool_name, fn):
    return run_with_edit_tracking(conversation, tool_name, fn)


def _sig_line(path: str) -> int:
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if "int subMethod" in line:
            return i
    raise AssertionError("subMethod signature not found")


def test_split_revert_extract_tweak_scenario(workspace):
    """The motivating scenario: the signature line is a direct edit, the new
    lines from the extraction are attributed to the refactoring tool, and the
    reverted split round-trip only shows up in churn. Body lines moved
    verbatim into subMethod keep their baseline attribution (git blame
    semantics for relocated code)."""
    ledger = EditLedger(str(workspace), "test-session")
    target = str(workspace / "Calculator.java")

    with ledger.track_edits("file_editor"):
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
    # New lines: the call site, closing brace, blank line, and signature.
    assert ledger.final_lines == {DIRECT_EDIT: 4}
    direct_split_churn = ledger.written_lines[DIRECT_EDIT]

    with ledger.track_edits("file_editor"):
        (workspace / "Calculator.java").write_text(BASE_FILE)
    # The revert erases the net contribution but keeps the churn.
    assert ledger.final_lines.get(DIRECT_EDIT, 0) == 0
    assert ledger.written_lines[DIRECT_EDIT] > direct_split_churn

    with ledger.track_edits("extract_method"):
        (workspace / "Calculator.java").write_text(EXTRACTED_FILE)
    assert ledger.final_lines == {REFACTORING_TOOL: 4}
    refactoring_churn = ledger.written_lines[REFACTORING_TOOL]

    with ledger.track_edits("file_editor"):
        (workspace / "Calculator.java").write_text(PROTECTED_FILE)
    # Only the signature line flips to direct edit; the rest of the
    # extraction stays refactoring.
    assert ledger.final_lines == {REFACTORING_TOOL: 3, DIRECT_EDIT: 1}
    assert ledger.written_lines[REFACTORING_TOOL] == refactoring_churn

    categories = ledger.line_categories(target)
    assert categories[_sig_line(target)] == DIRECT_EDIT
    other_new = [
        cat
        for i, cat in enumerate(categories)
        if i != _sig_line(target) and cat != BASELINE
    ]
    assert other_new == [REFACTORING_TOOL] * 3


def test_shell_edits_are_direct_edits(workspace):
    conversation = StubConversation(workspace)

    def edit_via_shell():
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
        return "ok"

    assert _run(conversation, "terminal", edit_via_shell) == "ok"
    log = get_edit_log_path()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]["tool_name"] == "terminal"
    assert records[-1]["category"] == DIRECT_EDIT
    assert records[-1]["final_lines_total"] == {DIRECT_EDIT: 4}


def test_created_and_deleted_files(workspace):
    ledger = EditLedger(str(workspace), "test-session")
    created = workspace / "New.java"

    with ledger.track_edits("extract_method"):
        created.write_text("class New {}\n")
    assert ledger.final_lines == {REFACTORING_TOOL: 1}

    with ledger.track_edits("file_editor"):
        created.unlink()
    assert ledger.final_lines.get(REFACTORING_TOOL, 0) == 0
    assert ledger.written_lines[REFACTORING_TOOL] == 1  # 1 insertion from the create
    assert ledger.written_lines[DIRECT_EDIT] == 1  # 1 deletion from the delete


def test_modified_line_counts_insertion_and_deletion(workspace):
    ledger = EditLedger(str(workspace), "test-session")
    with ledger.track_edits("file_editor"):
        (workspace / "Calculator.java").write_text(
            BASE_FILE.replace("int total = 0;", "int total = 10;")
        )
    assert ledger.final_lines == {DIRECT_EDIT: 1}
    assert ledger.written_lines[DIRECT_EDIT] == 2  # 1 deletion + 1 insertion


def test_unchanged_window_records_nothing(workspace):
    ledger = EditLedger(str(workspace), "test-session")
    with ledger.track_edits("terminal"):
        pass
    assert ledger.final_lines == {}
    assert ledger.written_lines == {}


def test_edit_log_inside_workspace_is_not_tracked(workspace, monkeypatch):
    """A log placed inside the workspace must not be attributed as an edit."""
    log = workspace / "prov.jsonl"
    monkeypatch.setenv("OPENHANDS_EDIT_ATTRIBUTION_LOG", str(log))
    ledger = EditLedger(str(workspace), "test-session")
    with ledger.track_edits("file_editor"):
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
    # Window 1 writes the log; window 2 must not attribute it.
    with ledger.track_edits("terminal"):
        pass
    assert ledger.final_lines == {DIRECT_EDIT: 4}


def test_records_joinable_and_carry_both_views(workspace):
    conversation = StubConversation(workspace)

    def edit():
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
        return "ok"

    _run(conversation, "apply_patch", edit)
    log = get_edit_log_path()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    record = records[-1]
    assert record["session_id"] == "test-session"
    assert record["tool_name"] == "apply_patch"
    assert record["category"] == DIRECT_EDIT
    assert record["written_lines_total"] == {DIRECT_EDIT: 4}
    assert record["final_lines_total"] == {DIRECT_EDIT: 4}


def test_categorize_tool_with_env_patterns(monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_TOOLS", "rename_symbol, intellij_*")
    assert categorize_tool("extract_method") == REFACTORING_TOOL
    assert categorize_tool("rename_symbol") == REFACTORING_TOOL
    assert categorize_tool("intellij_inline_method") == REFACTORING_TOOL
    assert categorize_tool("terminal") == DIRECT_EDIT
    assert categorize_tool("file_editor") == DIRECT_EDIT


def test_disabled_env_var_disables_attribution(workspace, monkeypatch):
    monkeypatch.setenv("OPENHANDS_EDIT_ATTRIBUTION", "0")
    assert not is_edit_tracking_enabled()
    conversation = StubConversation(workspace)

    def mutate():
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
        return "ok"

    assert _run(conversation, "file_editor", mutate) == "ok"
    assert not get_edit_log_path().exists()


def test_baseline_seeded_before_first_tool(workspace):
    """Files already in the workspace are baseline, not first-tool edits."""
    ledger = EditLedger(str(workspace), "test-session")
    with ledger.track_edits("terminal"):
        pass
    assert ledger.final_lines == {}
    assert ledger.written_lines == {}
    assert set(ledger.line_categories(str(workspace / "Calculator.java"))) == {BASELINE}


def _commit_all(repo, message: str) -> str:
    env = dict(os.environ) | {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        env=env,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _numstat(repo, sha_a: str, sha_b: str) -> tuple[int, int]:
    out = subprocess.run(
        ["git", "diff", "--numstat", "--no-renames", sha_a, sha_b],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    ins, dels = 0, 0
    for line in out.splitlines():
        a, d, _ = line.split("\t", 2)
        if a != "-" and d != "-":
            ins += int(a)
            dels += int(d)
    return ins, dels


def _zero_result() -> dict:
    return {
        c: {"inserted": 0, "deleted": 0}
        for c in (REFACTORING_TOOL, DIRECT_EDIT, UNATTRIBUTED)
    }


def test_net_line_attribution_reconciles_and_splits(workspace):
    """Net counts equal the commit numstat, and each net-inserted line is
    attributed to the snapshot that last wrote it."""
    base_sha = _commit_all(workspace, "base")
    (workspace / "Calculator.java").write_text(SPLIT_FILE)
    split_sha = _commit_all(workspace, "split")
    (workspace / "Calculator.java").write_text(EXTRACTED_FILE)
    extracted_sha = _commit_all(workspace, "extract")

    records = [
        {
            "category": DIRECT_EDIT,
            "parent": base_sha,
            "commit": split_sha,
            "files": [{"path": "Calculator.java", "attributed_lines": {}}],
        },
        {
            "category": REFACTORING_TOOL,
            "parent": split_sha,
            "commit": extracted_sha,
            "files": [
                {
                    "path": "Calculator.java",
                    "attributed_lines": {
                        "3": DIRECT_EDIT,
                        "4": DIRECT_EDIT,
                        "5": DIRECT_EDIT,
                        "6": REFACTORING_TOOL,
                    },
                }
            ],
        },
    ]
    result = net_line_attribution(str(workspace), records, base_sha, extracted_sha)

    ins, dels = _numstat(workspace, base_sha, extracted_sha)
    assert sum(v["inserted"] for v in result.values()) == ins
    assert sum(v["deleted"] for v in result.values()) == dels
    assert result[DIRECT_EDIT]["inserted"] == 3
    assert result[REFACTORING_TOOL]["inserted"] == 1
    assert sum(v["deleted"] for v in result.values()) == 0


def test_net_line_attribution_revert_roundtrip_is_net_zero(workspace):
    """A delete-then-restore roundtrip leaves no net attributed lines."""
    base_sha = _commit_all(workspace, "base")
    (workspace / "Calculator.java").write_text(SPLIT_FILE)
    split_sha = _commit_all(workspace, "split")
    (workspace / "Calculator.java").write_text(BASE_FILE)
    revert_sha = _commit_all(workspace, "revert")

    records = [
        {
            "category": DIRECT_EDIT,
            "parent": base_sha,
            "commit": split_sha,
            "files": [{"path": "Calculator.java", "attributed_lines": {}}],
        },
        {
            "category": DIRECT_EDIT,
            "parent": split_sha,
            "commit": revert_sha,
            "files": [{"path": "Calculator.java", "attributed_lines": {}}],
        },
    ]
    result = net_line_attribution(str(workspace), records, base_sha, revert_sha)
    assert result == _zero_result()
