import json
import stat
import uuid
from types import SimpleNamespace
from typing import cast

import pytest

from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.conversation.refactoring_miner import (
    DEFAULT_REFACTORING_MINER,
    direct_edit_paths,
    extract_method_detected,
    extracted_method_names,
    get_miner_path,
    is_refactoring_nudge_enabled,
    mine_step_refactorings,
    refactoring_file_paths,
    seed_refactoring_baseline,
    snapshot_working_tree,
)
from openhands.sdk.event import ActionEvent
from openhands.sdk.llm import MessageToolCall
from openhands.sdk.tool.schema import Action


def _git(repo: str, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "App.java").write_text("class App {}\n")
    _git(str(repo), "init", "-q")
    _git(str(repo), "config", "user.email", "test@example.com")
    _git(str(repo), "config", "user.name", "test")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-q", "-m", "initial")
    return repo


def _stub_miner(tmp_path, refactorings: list[dict]) -> str:
    script = tmp_path / "stub_refactoring_miner.sh"
    payload = json.dumps({"commits": [{"refactorings": refactorings}]})
    script.write_text(f"#!/bin/sh\nprintf '%s' '{payload}' > \"$5\"\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _conversation_stub(working_dir) -> LocalConversation:
    return cast(
        LocalConversation,
        SimpleNamespace(
            workspace=SimpleNamespace(working_dir=working_dir),
            state=SimpleNamespace(id=uuid.uuid4()),
        ),
    )


def test_miner_path_resolution(monkeypatch):
    monkeypatch.delenv("OPENHANDS_REFACTORING_MINER", raising=False)
    default = get_miner_path()
    assert default == str(DEFAULT_REFACTORING_MINER)
    assert default is not None and default.endswith("bin/RefactoringMiner")

    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", "/custom/miner")
    assert get_miner_path() == "/custom/miner"

    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", "")
    assert get_miner_path() is None


def test_disabled_with_empty_miner_env(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", "")
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))

    mine_step_refactorings(_conversation_stub(str(git_repo)), 1)

    assert not log.exists()


def test_snapshot_working_tree_untouched(git_repo):
    head_before = _git(str(git_repo), "rev-parse", "HEAD")

    (git_repo / "src" / "App.java").write_text("class App { void f() {} }\n")
    (git_repo / "src" / "New.java").write_text("class New {}\n")

    commit = snapshot_working_tree(str(git_repo))

    assert commit is not None
    assert _git(str(git_repo), "rev-parse", "HEAD") == head_before
    # Working tree and index keep their dirty state.
    assert len(_git(str(git_repo), "status", "--porcelain").splitlines()) == 2
    # The snapshot commit captures the dirty tree with HEAD as parent.
    assert _git(str(git_repo), "rev-parse", f"{commit}^") == head_before
    assert _git(str(git_repo), "diff", "--name-only", f"{commit}^..{commit}") != ""


def test_snapshot_clean_tree_returns_none(git_repo):
    assert snapshot_working_tree(str(git_repo)) is None


def test_mine_step_refactorings_logs_record(git_repo, tmp_path, monkeypatch):
    refactorings = [
        {
            "type": "Extract Method",
            "description": "Extract Method private f() extracted in class App",
            "leftSideLocations": [],
            "rightSideLocations": [],
        }
    ]
    monkeypatch.setenv(
        "OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, refactorings)
    )
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))
    (git_repo / "src" / "App.java").write_text("class App { void g() {} }\n")

    session_id = uuid.uuid4()
    stub = cast(
        LocalConversation,
        SimpleNamespace(
            workspace=SimpleNamespace(working_dir=str(git_repo)),
            state=SimpleNamespace(id=session_id),
        ),
    )
    mine_step_refactorings(stub, 3)

    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["session_id"] == str(session_id)
    assert record["step"] == 3
    assert record["parent_commit"] is None
    assert record["status"] == "ok"
    assert record["num_refactorings"] == 1
    assert record["refactorings"][0]["type"] == "Extract Method"
    assert record["repo_root"] == str(git_repo)
    assert record["duration_ms"] >= 0


def test_snapshots_chain_across_steps(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))

    (git_repo / "src" / "App.java").write_text("class App { void a() {} }\n")
    mine_step_refactorings(_conversation_stub(str(git_repo)), 1)
    first = json.loads(log.read_text().splitlines()[0])["commit"]

    (git_repo / "src" / "New.java").write_text("class New {}\n")
    mine_step_refactorings(_conversation_stub(str(git_repo)), 2, first)
    second = json.loads(log.read_text().splitlines()[1])["commit"]

    # First snapshot diffs against HEAD; the second only against the first,
    # so each step's refactorings are scoped to that step alone.
    assert _git(str(git_repo), "rev-parse", f"{first}^") == _git(
        str(git_repo), "rev-parse", "HEAD"
    )
    assert _git(str(git_repo), "rev-parse", f"{second}^") == first
    assert _git(str(git_repo), "diff", "--name-only", f"{second}^..{second}") == (
        "src/New.java"
    )


def test_mine_step_refactorings_no_changes(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))

    mine_step_refactorings(_conversation_stub(str(git_repo)), 1)

    record = json.loads(log.read_text().splitlines()[0])
    assert record["status"] == "no_changes"
    assert record["refactorings"] == []
    assert mine_step_refactorings(_conversation_stub(str(git_repo)), 2)[0] is None


def test_chain_survives_no_change_steps(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))

    stub = _conversation_stub(str(git_repo))
    (git_repo / "src" / "App.java").write_text("class App { void a() {} }\n")
    first = mine_step_refactorings(stub, 1)[0]
    assert first is not None

    # An idle step emits a no_changes record but must keep the chain anchored
    # on the last snapshot instead of resetting it back to HEAD.
    assert mine_step_refactorings(stub, 2, first)[0] == first

    # A later edit is therefore mined against the last snapshot only, so it
    # cannot re-report refactorings already attributed to earlier steps.
    (git_repo / "src" / "New.java").write_text("class New {}\n")
    second = mine_step_refactorings(stub, 3, first)[0]
    assert second is not None
    assert _git(str(git_repo), "rev-parse", f"{second}^") == first
    assert _git(str(git_repo), "diff", "--name-only", f"{second}^..{second}") == (
        "src/New.java"
    )


def test_extract_method_detected():
    extract = {"type": "Extract Method", "description": "Extract Method f()"}
    other = {"type": "Inline Method", "description": "Inline Method f()"}

    assert extract_method_detected([other, extract])
    assert not extract_method_detected([other])
    assert not extract_method_detected([])


def test_extracted_method_names():
    extract = {
        "type": "Extract Method",
        "description": (
            "Extract Method protected computeFacetsRowBased() : void extracted "
            "from public computeFacets() : void in class com.google.Engine"
        ),
    }
    other = {
        "type": "Extract Method",
        "description": "Extract Method createImage(size int) : void extracted "
        "from public Visitor(int x) in class Visitor",
    }
    unparseable = {"type": "Extract Method", "description": "no signature here"}

    assert extracted_method_names([extract, other, unparseable]) == [
        "computeFacetsRowBased",
        "createImage",
    ]
    assert extracted_method_names([unparseable]) == []
    assert extracted_method_names([]) == []


def test_nudge_env_toggle(monkeypatch):
    monkeypatch.delenv("OPENHANDS_REFACTORING_NUDGE", raising=False)
    assert is_refactoring_nudge_enabled()

    for value in ("1", "true", "YES", " True "):
        monkeypatch.setenv("OPENHANDS_REFACTORING_NUDGE", value)
        assert is_refactoring_nudge_enabled()

    for value in ("", "0", "false", "off"):
        monkeypatch.setenv("OPENHANDS_REFACTORING_NUDGE", value)
        assert not is_refactoring_nudge_enabled()


def _nudge_stub(events: list, working_dir: str = "/ws") -> LocalConversation:
    return cast(
        LocalConversation,
        SimpleNamespace(
            _on_event=events.append,
            workspace=SimpleNamespace(working_dir=working_dir),
        ),
    )


class _EditAction(Action):
    path: str | None = None
    patch: str | None = None


def _action_event(tool_name: str, **fields) -> ActionEvent:
    return ActionEvent(
        thought=[],
        tool_name=tool_name,
        tool_call_id="call_1",
        tool_call=MessageToolCall(
            id="call_1", name=tool_name, arguments="{}", origin="completion"
        ),
        llm_response_id="resp_1",
        action=_EditAction(**fields),
    )


def _extract_refactoring(file_path: str = "src/App.java") -> dict:
    return {
        "type": "Extract Method",
        "description": "Extract Method protected f() : void extracted "
        "from public g() : void in class App",
        "leftSideLocations": [{"filePath": file_path}],
        "rightSideLocations": [{"filePath": file_path}],
    }


def test_nudge_emitted_for_direct_edit_extraction(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENHANDS_REFACTORING_NUDGE", "1")
    emitted: list = []
    refactoring = _extract_refactoring("src/App.java")
    edit = _action_event("file_editor", path=str(tmp_path / "src" / "App.java"))

    LocalConversation._maybe_nudge_extract_method(
        _nudge_stub(emitted, str(tmp_path)), [refactoring], [edit]
    )

    assert len(emitted) == 1
    content = emitted[0].llm_message.content[0]
    assert "f" in content.text
    assert "revert" in content.text
    assert "IntelliJ IDEA MCP" in content.text


def test_nudge_skipped_when_edit_not_directly_attributed(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENHANDS_REFACTORING_NUDGE", "1")
    emitted: list = []
    refactoring = _extract_refactoring("src/App.java")
    # IDE-tool extraction: no direct-edit action in the step.
    LocalConversation._maybe_nudge_extract_method(
        _nudge_stub(emitted, str(tmp_path)), [refactoring], []
    )
    # Direct edit on an unrelated file.
    other_file = _action_event("file_editor", path=str(tmp_path / "src" / "Other.java"))
    LocalConversation._maybe_nudge_extract_method(
        _nudge_stub(emitted, str(tmp_path)), [refactoring], [other_file]
    )

    assert emitted == []


def test_nudge_skipped_without_extract_method_or_toggle(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENHANDS_REFACTORING_NUDGE", "1")
    emitted: list = []
    edit = _action_event("file_editor", path=str(tmp_path / "src" / "App.java"))
    other = [_extract_refactoring()]
    other[0]["type"] = "Inline Method"

    LocalConversation._maybe_nudge_extract_method(
        _nudge_stub(emitted, str(tmp_path)), other, [edit]
    )

    monkeypatch.setenv("OPENHANDS_REFACTORING_NUDGE", "")
    LocalConversation._maybe_nudge_extract_method(
        _nudge_stub(emitted, str(tmp_path)), [_extract_refactoring()], [edit]
    )

    assert emitted == []


def test_direct_edit_paths(tmp_path):
    events = [
        _action_event("file_editor", path=str(tmp_path / "a.java")),
        _action_event(
            "apply_patch",
            patch=(
                "*** Begin Patch\n"
                "*** Update File: rel/b.java\n"
                "+x\n"
                "*** Add File: /abs/c.java\n"
                "+y\n"
                "*** End Patch"
            ),
        ),
        _action_event("execute_bash", path=str(tmp_path / "d.java")),
        _action_event("file_editor"),
    ]

    paths = direct_edit_paths(events, str(tmp_path))

    assert paths == {
        str(tmp_path / "a.java"),
        str(tmp_path / "rel" / "b.java"),
        "/abs/c.java",
    }


def test_refactoring_file_paths(tmp_path):
    refactorings = [
        {
            "type": "Extract Method",
            "leftSideLocations": [{"filePath": "src/rel.java"}],
            "rightSideLocations": [{"filePath": str(tmp_path / "src" / "abs.java")}],
        },
        {"type": "Extract Method", "leftSideLocations": [], "rightSideLocations": []},
    ]

    assert refactoring_file_paths(refactorings, str(tmp_path)) == {
        str(tmp_path / "src" / "rel.java"),
        str(tmp_path / "src" / "abs.java"),
    }
    assert refactoring_file_paths([], str(tmp_path)) == set()


def test_mine_step_refactorings_skips_non_git_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    mine_step_refactorings(_conversation_stub(str(plain_dir)), 1)

    assert not log.exists()


def test_temporary_origin_remote_lifecycle(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))

    mine_step_refactorings(_conversation_stub(str(git_repo)), 1)

    assert _git(str(git_repo), "remote") == ""

    real_url = "https://example.com/real.git"
    _git(str(git_repo), "remote", "add", "origin", real_url)
    mine_step_refactorings(_conversation_stub(str(git_repo)), 2)

    assert _git(str(git_repo), "remote", "get-url", "origin") == real_url


def test_seed_baseline_absorbs_pre_existing_changes(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    stub = _conversation_stub(str(git_repo))
    # Uncommitted change that predates the conversation.
    (git_repo / "src" / "Existing.java").write_text("class Existing {}\n")

    baseline = seed_refactoring_baseline(stub)
    assert baseline is not None
    assert _git(str(git_repo), "rev-parse", f"{baseline}^") == _git(
        str(git_repo), "rev-parse", "HEAD"
    )

    # The agent's edit is mined against the baseline only, so the
    # pre-existing change is not attributed to the agent's first step.
    (git_repo / "src" / "App.java").write_text("class App { void a() {} }\n")
    commit, _ = mine_step_refactorings(stub, 1, baseline)
    assert commit is not None
    assert _git(str(git_repo), "rev-parse", f"{commit}^") == baseline
    assert _git(str(git_repo), "diff", "--name-only", f"{commit}^..{commit}") == (
        "src/App.java"
    )


def test_seed_baseline_clean_tree_returns_head(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    assert seed_refactoring_baseline(_conversation_stub(str(git_repo))) == _git(
        str(git_repo), "rev-parse", "HEAD"
    )


def test_seed_baseline_skips_disabled_miner(git_repo, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", "")
    assert seed_refactoring_baseline(_conversation_stub(str(git_repo))) is None
