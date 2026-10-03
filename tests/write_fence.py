"""A write fence built on Python audit hooks.

Records every filesystem write the current process attempts: opens for
writing, renames/replaces, deletes, mkdir, copies, utime/chmod and sqlite
connections. Writes by child processes (git, rclone) are not visible here.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_PATH_ARGS = {  # audit event -> indexes of path arguments that get written
    "os.rename": (0, 1), "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,),
    "os.truncate": (0,), "os.symlink": (1,), "os.link": (1,), "os.utime": (0,),
    "os.chmod": (0,), "shutil.rmtree": (0,), "shutil.copyfile": (1,),
    "shutil.copytree": (1,), "shutil.move": (0, 1),
}
# Where an event carries a dir_fd that relative path arguments resolve against
# (shutil.rmtree, for one, deletes by name relative to an open directory).
_DIR_FD_ARG = {"os.remove": 1, "os.rmdir": 1, "os.mkdir": 2, "os.utime": 3, "os.chmod": 2,
               "os.symlink": 2, "shutil.rmtree": 1}
_RENAME_DIR_FDS = (2, 3)  # os.rename(src, dst, src_dir_fd, dst_dir_fd)
_local = threading.local()


def _norm(p, dir_fd: int | None = None) -> str | None:
    if isinstance(p, int) or p is None:
        return None
    if isinstance(p, bytes):
        p = os.fsdecode(p)
    try:
        p = os.fspath(p)
        if not os.path.isabs(p) and isinstance(dir_fd, int) and dir_fd >= 0:
            p = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), p)
        return os.path.realpath(os.path.abspath(p))
    except (TypeError, ValueError, OSError):
        return None


class Fence:
    def __init__(self, *, guarded: list[Path] | None = None, allowed: list[Path] | None = None):
        """``guarded``: writes under these are violations.
        ``allowed``: when given, writes anywhere *outside* these are violations."""
        self.guarded = [str(Path(p).resolve()) for p in (guarded or [])]
        self.allowed = [str(Path(p).resolve()) for p in allowed] if allowed is not None else None
        self.violations: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self.active = True
        self.context = ""  # e.g. the running test's node id
        self.by_context: dict[tuple[str, str], set[str]] = {}

    def _under(self, path: str, roots: list[str]) -> bool:
        return any(path == r or path.startswith(r + os.sep) for r in roots)

    def _check(self, event: str, path: str) -> None:
        if path.startswith(("/dev/", "/proc/")):
            return
        self.writes.append((event, path))
        bad = (self.guarded and self._under(path, self.guarded)) or (
            self.allowed is not None and not self._under(path, self.allowed))
        if bad:
            self.violations.append((event, path))
            self.by_context.setdefault((event, path), set()).add(self.context or "?")

    def hook(self, event: str, args: tuple) -> None:
        if not self.active or getattr(_local, "busy", False):
            return
        _local.busy = True
        try:
            if event == "open":
                path, mode, flags = (list(args) + [None, None, None])[:3]
                writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & _WRITE_FLAGS)
                if writing and (p := _norm(path)):
                    self._check(event, p)
            elif event == "sqlite3.connect":
                db = args[0] if args else None
                if isinstance(db, (str, bytes, os.PathLike)) and "mode=ro" not in os.fsdecode(db) \
                        and os.fsdecode(db) != ":memory:" and (p := _norm(db)):
                    self._check(event, p)
            elif event in _PATH_ARGS:
                for i in _PATH_ARGS[event]:
                    if event == "os.rename":
                        fd_i = _RENAME_DIR_FDS[i]
                    else:
                        fd_i = _DIR_FD_ARG.get(event)
                    fd = args[fd_i] if fd_i is not None and fd_i < len(args) else None
                    if i < len(args) and (p := _norm(args[i], fd)):
                        self._check(event, p)
        finally:
            _local.busy = False

    def install(self) -> "Fence":
        sys.addaudithook(self.hook)
        return self
