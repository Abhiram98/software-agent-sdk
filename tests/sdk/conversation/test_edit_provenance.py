import json
from types import SimpleNamespace

import pytest

from openhands.sdk.conversation.edit_provenance import (
    BASELINE,
    DIRECT_EDIT,
    REFACTORING_TOOL,
    EditProvenanceLedger,
    categorize_tool,
    execute_with_edit_provenance,
    get_provenance_log_path,
    is_edit_provenance_enabled,
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
    monkeypatch.setenv("OPENHANDS_EDIT_PROVENANCE_LOG", str(tmp_path / "prov.jsonl"))
    monkeypatch.delenv("OPENHANDS_REFACTORING_TOOLS", raising=False)
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "Calculator.java").write_text(BASE_FILE)
    return ws


def _run(conversation, tool_name, fn):
    return execute_with_edit_provenance(conversation, tool_name, fn)


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
    ledger = EditProvenanceLedger(str(workspace), "test-session")
    target = str(workspace / "Calculator.java")

    with ledger.window("file_editor"):
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
    # New lines: the call site, closing brace, blank line, and signature.
    assert ledger.net == {DIRECT_EDIT: 4}
    direct_split_churn = ledger.churn[DIRECT_EDIT]

    with ledger.window("file_editor"):
        (workspace / "Calculator.java").write_text(BASE_FILE)
    # The revert erases the net contribution but keeps the churn.
    assert ledger.net.get(DIRECT_EDIT, 0) == 0
    assert ledger.churn[DIRECT_EDIT] > direct_split_churn

    with ledger.window("extract_method"):
        (workspace / "Calculator.java").write_text(EXTRACTED_FILE)
    assert ledger.net == {REFACTORING_TOOL: 4}
    refactoring_churn = ledger.churn[REFACTORING_TOOL]

    with ledger.window("file_editor"):
        (workspace / "Calculator.java").write_text(PROTECTED_FILE)
    # Only the signature line flips to direct edit; the rest of the
    # extraction stays refactoring.
    assert ledger.net == {REFACTORING_TOOL: 3, DIRECT_EDIT: 1}
    assert ledger.churn[REFACTORING_TOOL] == refactoring_churn

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
    log = get_provenance_log_path()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[-1]["tool_name"] == "terminal"
    assert records[-1]["category"] == DIRECT_EDIT
    assert records[-1]["net_total"] == {DIRECT_EDIT: 4}


def test_created_and_deleted_files(workspace):
    ledger = EditProvenanceLedger(str(workspace), "test-session")
    created = workspace / "New.java"

    with ledger.window("extract_method"):
        created.write_text("class New {}\n")
    assert ledger.net == {REFACTORING_TOOL: 1}

    with ledger.window("file_editor"):
        created.unlink()
    assert ledger.net.get(REFACTORING_TOOL, 0) == 0
    assert ledger.churn[REFACTORING_TOOL] == 1  # 1 insertion from the create
    assert ledger.churn[DIRECT_EDIT] == 1  # 1 deletion from the delete


def test_modified_line_counts_insertion_and_deletion(workspace):
    ledger = EditProvenanceLedger(str(workspace), "test-session")
    with ledger.window("file_editor"):
        (workspace / "Calculator.java").write_text(
            BASE_FILE.replace("int total = 0;", "int total = 10;")
        )
    assert ledger.net == {DIRECT_EDIT: 1}
    assert ledger.churn[DIRECT_EDIT] == 2  # 1 deletion + 1 insertion


def test_unchanged_window_records_nothing(workspace):
    ledger = EditProvenanceLedger(str(workspace), "test-session")
    with ledger.window("terminal"):
        pass
    assert ledger.net == {}
    assert ledger.churn == {}


def test_provenance_log_inside_workspace_is_not_tracked(workspace, monkeypatch):
    """A log placed inside the workspace must not be attributed as an edit."""
    log = workspace / "prov.jsonl"
    monkeypatch.setenv("OPENHANDS_EDIT_PROVENANCE_LOG", str(log))
    ledger = EditProvenanceLedger(str(workspace), "test-session")
    with ledger.window("file_editor"):
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
    # Window 1 writes the log; window 2 must not attribute it.
    with ledger.window("terminal"):
        pass
    assert ledger.net == {DIRECT_EDIT: 4}


def test_records_joinable_and_carry_both_views(workspace):
    conversation = StubConversation(workspace)

    def edit():
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
        return "ok"

    _run(conversation, "apply_patch", edit)
    log = get_provenance_log_path()
    records = [json.loads(line) for line in log.read_text().splitlines()]
    record = records[-1]
    assert record["session_id"] == "test-session"
    assert record["tool_name"] == "apply_patch"
    assert record["category"] == DIRECT_EDIT
    assert record["churn_total"] == {DIRECT_EDIT: 4}
    assert record["net_total"] == {DIRECT_EDIT: 4}


def test_categorize_tool_with_env_patterns(monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_TOOLS", "rename_symbol, intellij_*")
    assert categorize_tool("extract_method") == REFACTORING_TOOL
    assert categorize_tool("rename_symbol") == REFACTORING_TOOL
    assert categorize_tool("intellij_inline_method") == REFACTORING_TOOL
    assert categorize_tool("terminal") == DIRECT_EDIT
    assert categorize_tool("file_editor") == DIRECT_EDIT


def test_disabled_env_var_disables_attribution(workspace, monkeypatch):
    monkeypatch.setenv("OPENHANDS_EDIT_PROVENANCE", "0")
    assert not is_edit_provenance_enabled()
    conversation = StubConversation(workspace)

    def mutate():
        (workspace / "Calculator.java").write_text(SPLIT_FILE)
        return "ok"

    assert _run(conversation, "file_editor", mutate) == "ok"
    assert not get_provenance_log_path().exists()


def test_baseline_seeded_before_first_tool(workspace):
    """Files already in the workspace are baseline, not first-tool edits."""
    ledger = EditProvenanceLedger(str(workspace), "test-session")
    with ledger.window("terminal"):
        pass
    assert ledger.net == {}
    assert ledger.churn == {}
    assert set(ledger.line_categories(str(workspace / "Calculator.java"))) == {BASELINE}
