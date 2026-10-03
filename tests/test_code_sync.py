"""Code sync against a throwaway GitHub stand-in (a bare repo)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from story_editor import code_sync, config


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> None:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


@pytest.fixture
def machines(tmp_path: Path, monkeypatch):
    hub = tmp_path / "hub.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(hub)], check=True)
    here, there = tmp_path / "here", tmp_path / "there"
    for repo in (here, there):
        subprocess.run(["git", "clone", "-q", str(hub), str(repo)], check=True, capture_output=True)
        _git(repo, "checkout", "-q", "-b", "main")
    _commit(here, "a.py", "1", "first")
    _git(here, "push", "-q", "origin", "main")
    _git(there, "pull", "-q", "origin", "main")
    monkeypatch.setattr(config, "PROJECT_ROOT", here)
    return here, there


def test_update_fast_forwards_and_reports_changes(machines) -> None:
    here, there = machines
    assert code_sync.status().kind == "up_to_date"
    _commit(there, "a.py", "2", "second")
    _commit(there, "requirements.txt", "x", "deps")
    _git(there, "push", "-q", "origin", "main")

    st = code_sync.status()
    assert st.kind == "behind" and st.behind == 2 and st.can_update
    assert st.incoming == ["deps", "second"]
    result = code_sync.update()
    assert result["updated"] and result["dependencies_changed"]
    assert (here / "a.py").read_text() == "2"
    assert code_sync.status().kind == "up_to_date"


def test_update_refuses_over_uncommitted_work(machines) -> None:
    here, there = machines
    _commit(there, "a.py", "2", "second")
    _git(there, "push", "-q", "origin", "main")
    (here / "a.py").write_text("local edit")
    assert code_sync.status().dirty == ["a.py"]
    with pytest.raises(code_sync.UpdateRefused):
        code_sync.update()
    assert (here / "a.py").read_text() == "local edit"


def test_diverged_is_refused_and_ahead_can_push(machines) -> None:
    here, there = machines
    _commit(here, "b.py", "1", "local")
    assert code_sync.status().kind == "ahead"
    code_sync.push()
    assert code_sync.status().kind == "up_to_date"

    _git(there, "pull", "-q", "origin", "main")
    _commit(there, "c.py", "1", "remote")
    _git(there, "push", "-q", "origin", "main")
    _commit(here, "d.py", "1", "local again")
    assert code_sync.status().kind == "diverged"
    with pytest.raises(code_sync.UpdateRefused):
        code_sync.update()
