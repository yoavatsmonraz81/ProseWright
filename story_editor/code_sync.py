"""Keep this machine's editor code in step with GitHub.

GitHub (``origin/main``) is the reliable source for the code; the workspace
travels separately through Drive (see ``drive_sync``). Updating is
fast-forward only: it never merges, and it never touches uncommitted changes —
if the checkout has any, or has local commits GitHub lacks, it refuses and
says why. After an update the engine restarts itself so the new code is live.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

from . import config

BRANCH = "main"
REMOTE = "origin"

# Files whose change means the Python environment may need updating too.
DEPENDENCY_FILES = ("requirements.txt", "environment.yml")

_LOCK = threading.Lock()


class CodeSyncError(RuntimeError):
    pass


class UpdateRefused(ValueError):
    pass


def _git(*args: str, timeout: int = 60, strip: bool = True) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a password prompt
    try:
        proc = subprocess.run(
            ["git", "-C", str(config.PROJECT_ROOT), *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except FileNotFoundError as exc:
        raise CodeSyncError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise CodeSyncError(f"git {args[0]} timed out") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout).strip().splitlines()
        raise CodeSyncError(lines[-1] if lines else f"git {args[0]} failed")
    return proc.stdout.strip() if strip else proc.stdout


@dataclass
class CodeStatus:
    kind: str                      # up_to_date | behind | ahead | diverged | no_repo | error
    detail: str = ""
    branch: str = ""
    head: str = ""
    remote_head: str = ""
    behind: int = 0
    ahead: int = 0
    dirty: list[str] = field(default_factory=list)
    incoming: list[str] = field(default_factory=list)   # commit subjects
    fetched: bool = False

    @property
    def can_update(self) -> bool:
        return self.kind == "behind" and not self.dirty

    @property
    def can_push(self) -> bool:
        return self.kind == "ahead"

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "branch": self.branch,
            "head": self.head,
            "remote_head": self.remote_head,
            "behind": self.behind,
            "ahead": self.ahead,
            "dirty": self.dirty,
            "incoming": self.incoming,
            "fetched": self.fetched,
            "can_update": self.can_update,
            "can_push": self.can_push,
        }


def status(*, fetch: bool = True) -> CodeStatus:
    try:
        _git("rev-parse", "--git-dir")
    except CodeSyncError:
        return CodeStatus("no_repo", "this folder is not a git checkout — clone it from GitHub")
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        head = _git("rev-parse", "--short", "HEAD")
        # Porcelain lines are "XY path"; the leading status column may be a
        # space, so the output must not be stripped before slicing.
        porcelain = _git("status", "--porcelain", "--untracked-files=no", strip=False)
        dirty = [line[3:] for line in porcelain.splitlines() if line.strip()]
        fetched = False
        fetch_error = ""
        if fetch:
            try:
                _git("fetch", "--quiet", REMOTE, BRANCH, timeout=120)
                fetched = True
            except CodeSyncError as exc:
                fetch_error = str(exc)
        upstream = f"{REMOTE}/{BRANCH}"
        try:
            remote_head = _git("rev-parse", "--short", upstream)
        except CodeSyncError:
            return CodeStatus(
                "error", fetch_error or f"no {upstream} yet — push this branch once",
                branch=branch, head=head, dirty=dirty,
            )
        counts = _git("rev-list", "--left-right", "--count", f"HEAD...{upstream}").split()
        ahead, behind = int(counts[0]), int(counts[1])
        incoming = _git("log", "--format=%s", f"HEAD..{upstream}").splitlines() if behind else []
    except CodeSyncError as exc:
        return CodeStatus("error", str(exc))

    if branch != BRANCH:
        kind, detail = "error", f"checked out on {branch}, not {BRANCH}"
    elif ahead and behind:
        kind, detail = "diverged", "this machine and GitHub both have commits the other lacks"
    elif behind:
        kind, detail = "behind", f"GitHub has {behind} newer commit(s)"
    elif ahead:
        kind, detail = "ahead", f"{ahead} local commit(s) not on GitHub yet"
    else:
        kind, detail = "up_to_date", "matches GitHub"
    if fetch_error:
        detail = f"{detail} (could not reach GitHub: {fetch_error})"
    if dirty and kind in ("behind", "up_to_date"):
        detail += f"; {len(dirty)} uncommitted change(s) here"
    return CodeStatus(
        kind, detail, branch=branch, head=head, remote_head=remote_head,
        behind=behind, ahead=ahead, dirty=dirty, incoming=incoming, fetched=fetched,
    )


def update() -> dict[str, Any]:
    """Fast-forward to ``origin/main``. Refuses rather than merge or clobber."""
    with _LOCK:
        st = status()
        if st.kind in ("no_repo", "error"):
            raise CodeSyncError(st.detail)
        if st.kind == "up_to_date":
            return {"status": st.to_json(), "updated": False, "changed": [], "dependencies_changed": False}
        if st.dirty:
            raise UpdateRefused(
                "this machine has uncommitted code changes: " + ", ".join(st.dirty[:10])
                + ". Commit or discard them first."
            )
        if st.kind != "behind":
            raise UpdateRefused(st.detail + ". Resolve it in a terminal (git pull --rebase).")
        before = _git("rev-parse", "HEAD")
        _git("merge", "--ff-only", f"{REMOTE}/{BRANCH}")
        changed = _git("diff", "--name-only", before, "HEAD").splitlines()
        return {
            "status": status(fetch=False).to_json(),
            "updated": True,
            "changed": changed,
            "dependencies_changed": any(f in DEPENDENCY_FILES for f in changed),
        }


def push() -> dict[str, Any]:
    """Publish local commits. Only ever a fast-forward of GitHub."""
    with _LOCK:
        st = status()
        if not st.can_push:
            raise UpdateRefused(st.detail or "nothing to push")
        _git("push", REMOTE, f"HEAD:{BRANCH}", timeout=180)
        return {"status": status(fetch=False).to_json()}


def restart_soon(delay: float = 0.8) -> None:
    """Replace this process with a fresh ``python -m story_editor …`` so new
    code is loaded. Runs after the HTTP response has gone out. The listening
    socket is not inheritable, so the new process binds the port afresh."""

    def _go() -> None:
        argv = [sys.executable, "-m", "story_editor", *sys.argv[1:]]
        os.chdir(config.PROJECT_ROOT)
        os.execv(sys.executable, argv)

    threading.Timer(delay, _go).start()
