import json
import stat
import uuid
from types import SimpleNamespace
from typing import cast

import pytest

from openhands.sdk.conversation.impl.local_conversation import LocalConversation
from openhands.sdk.conversation.refactoring_miner import (
    DEFAULT_REFACTORING_MINER,
    get_miner_path,
    mine_step_refactorings,
    snapshot_working_tree,
)


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
    assert mine_step_refactorings(_conversation_stub(str(git_repo)), 2) is None


def test_chain_survives_no_change_steps(git_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENHANDS_REFACTORING_MINER", _stub_miner(tmp_path, []))
    log = tmp_path / "refactorings.jsonl"
    monkeypatch.setenv("OPENHANDS_REFACTORING_LOG", str(log))

    stub = _conversation_stub(str(git_repo))
    (git_repo / "src" / "App.java").write_text("class App { void a() {} }\n")
    first = mine_step_refactorings(stub, 1)
    assert first is not None

    # An idle step emits a no_changes record but must keep the chain anchored
    # on the last snapshot instead of resetting it back to HEAD.
    assert mine_step_refactorings(stub, 2, first) == first

    # A later edit is therefore mined against the last snapshot only, so it
    # cannot re-report refactorings already attributed to earlier steps.
    (git_repo / "src" / "New.java").write_text("class New {}\n")
    second = mine_step_refactorings(stub, 3, first)
    assert second is not None
    assert _git(str(git_repo), "rev-parse", f"{second}^") == first
    assert _git(str(git_repo), "diff", "--name-only", f"{second}^..{second}") == (
        "src/New.java"
    )


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
