"""Shared pytest setup for the unit suite.

Every test session runs against a scratch copy of the bundled example project
(``examples/lantern-quay``), never the repository's own files. The copy is made
here, at conftest import: ``story_editor.config`` resolves the project home from
the environment when it is imported, which the test modules do at collection
time, before any fixture would get a chance to run.

A leftover ``STORY_EDITOR_LOG`` (the ``tests/phase6`` harness exports one) is
dropped for the same reason: it would silently retarget the suite.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRATCH = Path(tempfile.mkdtemp(prefix="story-editor-tests-"))
EXAMPLE_HOME = _SCRATCH / "lantern-quay"
shutil.copytree(_REPO / "examples" / "lantern-quay", EXAMPLE_HOME)
os.environ.pop("STORY_EDITOR_LOG", None)
os.environ["STORY_EDITOR_HOME"] = str(EXAMPLE_HOME)
# The machine's real project registry is never read or written by tests.
_REAL_REGISTRY_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "story-editor"
os.environ["STORY_EDITOR_REGISTRY"] = str(_SCRATCH / "registry" / "projects.json")
os.environ["STORY_EDITOR_PROJECTS_DIR"] = str(_SCRATCH / "projects")
_REAL_PROJECTS_DIR = Path.home() / "story-editor-projects"

# --- Write fence ----------------------------------------------------------------
# The repository's bundled examples and the machine's project registry are
# read-only to the test suite. Any write
# the test process attempts under them is recorded and fails the run at the end
# (see pytest_sessionfinish). Writes by child processes (git, model servers) are
# outside what an audit hook can see.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from write_fence import Fence  # noqa: E402

_FENCE = Fence(guarded=[_REPO / "examples", _REAL_REGISTRY_DIR, _REAL_PROJECTS_DIR]).install()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _FENCE.context = item.nodeid


def pytest_sessionfinish(session, exitstatus):
    if _FENCE.violations:
        seen = sorted(set(_FENCE.violations))
        lines = "\n".join(
            f"  {event}: {path}\n      by: {', '.join(sorted(_FENCE.by_context[(event, path)])[:4])}"
            for event, path in seen[:40]
        )
        sys.stderr.write(
            f"\nWRITE FENCE: the test suite wrote into the repository's examples "
            f"({len(seen)} distinct writes):\n{lines}\n"
        )
        session.exitstatus = 1
    shutil.rmtree(_SCRATCH, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_edit_history(tmp_path, monkeypatch):
    """Give every test its own edit history and the workspace state files
    operators rewrite as a side effect (the last sweep's context and report), so
    no test sees another's leftovers."""
    from story_editor import config

    monkeypatch.setattr(config, "EDIT_HISTORY", tmp_path / "edit_history.jsonl")
    state = tmp_path / "workspace-state"
    monkeypatch.setattr(config, "LAST_SWEEP_CONTEXT", state / "last_sweep_context.json")
    monkeypatch.setattr(config, "LAST_SWEEP_REPORT", state / "last_sweep_report.md")


@pytest.fixture
def example_home(tmp_path):
    """A private, writable copy of the bundled example project, with
    ``story_editor.config`` re-resolved against it for the test's duration."""
    import importlib

    from story_editor import config

    home = tmp_path / "lantern-quay"
    shutil.copytree(_REPO / "examples" / "lantern-quay", home)
    os.environ["STORY_EDITOR_HOME"] = str(home)
    importlib.reload(config)
    try:
        yield home
    finally:
        os.environ["STORY_EDITOR_HOME"] = str(EXAMPLE_HOME)
        importlib.reload(config)
