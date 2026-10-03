"""Google Drive sync for the workspace — one author, two machines.

The author works from a laptop and from a workstation. The code travels through
git; the *data* (manuscript, review queue, edit history, working log, spine…)
travels through Drive, driven by rclone. Nothing here merges prose: the unit is
a whole-workspace snapshot, and the job of this module is to never let one
machine's snapshot silently overwrite the other's.

Layout on Drive (the project's ``integrations.drive.remote``, e.g. ``gdrive:My Story/sync``)::

    workspace/…       the synced files
    manifest.json     who pushed, when, and a sha256 per file — written last

Locally, ``workspace/.drive_sync/`` (never synced) keeps ``base.json`` — the
manifest this machine last pushed or pulled — plus a hash cache and a staging
copy for pulls. Comparing local files and the Drive manifest against that base
gives the four states the GUI shows:

    in_sync       nothing changed on either side
    local_ahead   this machine changed since the last sync  → push
    drive_ahead   the other machine pushed since            → pull
    diverged      both changed                              → refuse; the author picks

A pull backs up every local file it replaces or removes first, under
``workspace/backups/drive-pull-<stamp>/``.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from . import config

MANIFEST = "manifest.json"
STATE_DIR = ".drive_sync"

# Never synced: bulky or machine-local. Backups are this machine's undo trail
# (2+ GB); fonts are licensed per machine; the rest is regenerable residue.
# Top level of workspace/:
EXCLUDE_TOP_DIRS = (STATE_DIR, "backups", "fonts", "bad_replies")
EXCLUDE_TOP_FILES = ("stamps.json",)
# At any depth:
EXCLUDE_DIRS = ("__pycache__",)
EXCLUDE_NAMES = ("*.pyc", "*.tmp", "*.swp", "*~", "*.drive-sync-tmp")

IN_SYNC = "in_sync"
LOCAL_AHEAD = "local_ahead"
DRIVE_AHEAD = "drive_ahead"
DIVERGED = "diverged"
DRIVE_EMPTY = "drive_empty"
UNLINKED = "unlinked"          # both sides have data, but this machine never synced
NO_RCLONE = "no_rclone"
NO_REMOTE = "no_remote"
DRIVE_ERROR = "drive_error"
WRONG_PROJECT = "wrong_project"  # Drive holds another project's snapshot

# States that stop push and pull outright; ``force`` never overrides them.
_HARD_STOPS = (NO_RCLONE, NO_REMOTE, DRIVE_ERROR, WRONG_PROJECT)

_LOCK = threading.Lock()


class SyncRefused(ValueError):
    """The requested direction would overwrite unsynced work."""


class DriveError(RuntimeError):
    """rclone failed, or Drive is unreachable."""


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class Transport(Protocol):
    def ready(self) -> tuple[bool, str, str]: ...
    def read_manifest(self) -> dict[str, Any] | None: ...
    def write_manifest(self, data: dict[str, Any]) -> None: ...
    def upload(self, local_dir: Path) -> None: ...
    def download(self, local_dir: Path) -> None: ...


def remote() -> str | None:
    """This project's Drive folder, or None when it has none.

    Resolved per project in ``config`` from ``project.json``; a project syncs
    only where its own manifest says.
    """
    return config.DRIVE_REMOTE


def rclone_binary() -> str | None:
    return os.environ.get("STORY_EDITOR_RCLONE") or shutil.which("rclone")


class Rclone:
    """The real transport: shells out to rclone against ``remote()``."""

    def __init__(self, binary: str | None = None, target: str | None = None) -> None:
        self.binary = binary or rclone_binary()
        self.target = target or remote()

    def _run(self, *args: str, stdin: str | None = None, timeout: int = 1800) -> str:
        if not self.binary:
            raise DriveError("rclone is not installed")
        proc = subprocess.run(
            [self.binary, *args],
            input=stdin, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()
            raise DriveError(detail[-1] if detail else f"rclone {args[0]} failed")
        return proc.stdout

    def _transfer(self, *args: str) -> None:
        """Run a sync with one-second JSON stats, feeding the progress job."""
        if not self.binary:
            raise DriveError("rclone is not installed")
        cmd = [
            self.binary, *args,
            "--transfers", "8", "--checkers", "16", "--fast-list",
            "--use-json-log", "--log-level", "NOTICE",
            "--stats", "1s", "--stats-log-level", "NOTICE",
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        errors: list[str] = []
        assert proc.stderr is not None
        for line in proc.stderr:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                if line.strip():
                    errors.append(line.strip())
                continue
            if isinstance(entry.get("stats"), dict):
                _report_stats(entry["stats"])
            elif entry.get("level") in ("error", "critical"):
                errors.append(str(entry.get("msg") or "").strip())
        if proc.wait() != 0:
            raise DriveError(errors[-1] if errors else f"rclone {args[0]} failed")

    def _filters(self) -> list[str]:
        # Top-level entries are anchored with "/" so rclone and `_excluded`
        # agree exactly — a pull verifies the download against the manifest.
        patterns = [f"/{d}/**" for d in EXCLUDE_TOP_DIRS]
        patterns += [f"/{f}" for f in EXCLUDE_TOP_FILES]
        patterns += [f"{d}/**" for d in EXCLUDE_DIRS] + list(EXCLUDE_NAMES)
        out: list[str] = []
        for pattern in patterns:
            out += ["--exclude", pattern]
        return out

    def ready(self) -> tuple[bool, str, str]:
        if not self.target:
            return False, NO_REMOTE, (
                f"project {config.PROJECT_ID!r} has no Drive folder "
                "(set integrations.drive.remote in its project.json)"
            )
        if not self.binary:
            return False, NO_RCLONE, "rclone is not installed on this machine"
        name = self.target.split(":", 1)[0] + ":"
        try:
            remotes = self._run("listremotes", timeout=30).split()
        except (DriveError, subprocess.TimeoutExpired) as exc:
            return False, DRIVE_ERROR, str(exc)
        if name not in remotes:
            return False, NO_REMOTE, f"rclone has no remote called {name} — run `rclone config create {name[:-1]} drive`"
        return True, "", ""

    def read_manifest(self) -> dict[str, Any] | None:
        try:
            raw = self._run("cat", f"{self.target}/{MANIFEST}", timeout=120)
        except DriveError as exc:
            text = str(exc).lower()
            if "not found" in text or "doesn't exist" in text or "no such" in text:
                return None
            raise
        return json.loads(raw) if raw.strip() else None

    def write_manifest(self, data: dict[str, Any]) -> None:
        self._run("rcat", f"{self.target}/{MANIFEST}", stdin=json.dumps(data, indent=2), timeout=120)

    def upload(self, local_dir: Path) -> None:
        self._transfer("sync", str(local_dir), f"{self.target}/workspace", "--checksum", *self._filters())

    def download(self, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        self._transfer("sync", f"{self.target}/workspace", str(local_dir), "--checksum", *self._filters())


# --------------------------------------------------------------------------- #
# Local snapshot
# --------------------------------------------------------------------------- #


def _workspace() -> Path:
    return Path(config.WORKSPACE_DIR)


def _state_dir() -> Path:
    return _workspace() / STATE_DIR


def _excluded(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0] in EXCLUDE_TOP_DIRS or rel in EXCLUDE_TOP_FILES:
        return True
    if any(part in EXCLUDE_DIRS for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(parts[-1], pattern) for pattern in EXCLUDE_NAMES)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path | None = None, *, cache: bool = True) -> dict[str, str]:
    """``{relative path: sha256}`` for every syncable file under ``root``.

    Hashes are cached by (size, mtime) so a status check on an unchanged
    100 MB workspace costs a directory walk, not a full read.
    """
    base = Path(root or _workspace())
    cache_path = _state_dir() / "hash_cache.json"
    old: dict[str, Any] = {}
    if cache and cache_path.exists():
        try:
            old = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
    fresh: dict[str, Any] = {}
    out: dict[str, str] = {}
    if not base.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(base):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = [d for d in dirnames if not _excluded(f"{rel_dir}{d}/x")]
        for name in filenames:
            rel = f"{rel_dir}{name}"
            if _excluded(rel):
                continue
            path = Path(dirpath) / name
            try:
                st = path.stat()
            except OSError:
                continue
            key = f"{st.st_size}:{st.st_mtime_ns}"
            hit = old.get(rel) if cache else None
            digest = hit["sha256"] if hit and hit.get("key") == key else _sha256(path)
            out[rel] = digest
            fresh[rel] = {"key": key, "sha256": digest}
    if cache:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(fresh), encoding="utf-8")
    return out


def _load_base() -> dict[str, Any] | None:
    path = _state_dir() / "base.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_base(manifest: dict[str, Any]) -> None:
    path = _state_dir() / "base.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _diff(a: dict[str, str], b: dict[str, str]) -> list[str]:
    """Paths that differ between two snapshots (changed, added or removed)."""
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def _files(manifest: dict[str, Any] | None) -> dict[str, str]:
    return dict((manifest or {}).get("files") or {})


# --------------------------------------------------------------------------- #
# Status / push / pull
# --------------------------------------------------------------------------- #


@dataclass
class Status:
    kind: str
    detail: str = ""
    local_changes: list[str] | None = None
    drive_changes: list[str] | None = None
    drive: dict[str, Any] | None = None     # manifest header (no file list)
    base: dict[str, Any] | None = None

    @property
    def can_push(self) -> bool:
        return self.kind in (LOCAL_AHEAD, DRIVE_EMPTY)

    @property
    def can_pull(self) -> bool:
        return self.kind == DRIVE_AHEAD

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "remote": remote(),
            "machine": socket.gethostname(),
            "local_changes": self.local_changes or [],
            "drive_changes": self.drive_changes or [],
            "drive": self.drive,
            "base": self.base,
            "can_push": self.can_push,
            "can_pull": self.can_pull,
        }


def _header(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not manifest:
        return None
    return {k: v for k, v in manifest.items() if k != "files"} | {"file_count": len(_files(manifest))}


def _classify(
    local: dict[str, str],
    drive: dict[str, Any] | None,
    base: dict[str, Any] | None,
) -> Status:
    if drive is None:
        return Status(DRIVE_EMPTY, "Drive has no snapshot yet — push to create one",
                      local_changes=sorted(local))
    remote_files = _files(drive)
    if base is None:
        differing = _diff(local, remote_files)
        if not differing:
            return Status(IN_SYNC, "identical to Drive", [], [], _header(drive))
        return Status(
            UNLINKED,
            "this machine has never synced, and its workspace differs from Drive",
            local_changes=differing, drive_changes=differing, drive=_header(drive),
        )
    base_files = _files(base)
    local_changes = _diff(local, base_files)
    drive_moved = drive.get("id") != base.get("id")
    drive_changes = _diff(remote_files, base_files) if drive_moved else []
    if drive_moved and not drive_changes:
        drive_moved = False  # a re-push of identical content
    if not local_changes and not drive_moved:
        kind = IN_SYNC
    elif local_changes and not drive_moved:
        kind = LOCAL_AHEAD
    elif drive_moved and not local_changes:
        kind = DRIVE_AHEAD
    elif not _diff(local, remote_files):
        kind = IN_SYNC  # both moved, to the same place
    else:
        kind = DIVERGED
    return Status(kind, "", local_changes, drive_changes, _header(drive), _header(base))


def status(transport: Transport | None = None) -> Status:
    t = transport or Rclone()
    ok, kind, detail = t.ready()
    if not ok:
        return Status(kind, detail)
    try:
        drive = t.read_manifest()
    except (DriveError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        return Status(DRIVE_ERROR, str(exc))
    owner = (drive or {}).get("project")
    if owner and owner != config.PROJECT_ID:
        # A manifest with no owner predates project stamps; it is accepted.
        return Status(WRONG_PROJECT, (
            f"this Drive folder holds project {owner!r}, but the open project is "
            f"{config.PROJECT_ID!r}; nothing will be pushed or pulled"
        ), drive=_header(drive))
    local = snapshot()
    st = _classify(local, drive, _load_base())
    if st.kind == IN_SYNC and _load_base() is None and drive is not None:
        _save_base(drive)  # first contact with an identical copy: adopt it
    return st


# --------------------------------------------------------------------------- #
# Background job + progress — a first push is ~130 MB and several minutes, so
# the GUI starts a job and polls it instead of holding one request open.
# --------------------------------------------------------------------------- #

_JOB_LOCK = threading.Lock()
_JOB: dict[str, Any] = {"running": False, "direction": "", "phase": "", "progress": None,
                        "result": None, "error": "", "refused": False, "started": "", "finished": ""}


def _phase(name: str) -> None:
    with _JOB_LOCK:
        _JOB["phase"] = name
        _JOB["progress"] = None


def _report_stats(stats: dict[str, Any]) -> None:
    keys = ("bytes", "totalBytes", "transfers", "totalTransfers", "checks",
            "totalChecks", "speed", "eta", "errors")
    snap = {k: stats.get(k) for k in keys}
    snap["current"] = [t.get("name") for t in (stats.get("transferring") or [])[:3]]
    with _JOB_LOCK:
        _JOB["progress"] = snap


def job() -> dict[str, Any]:
    with _JOB_LOCK:
        return json.loads(json.dumps(_JOB))


def start(direction: str, *, force: bool = False, transport: Transport | None = None) -> dict[str, Any]:
    """Start a push or pull in the background; poll ``job()`` for progress.

    The refusal checks run up front, so a refused direction answers at once
    instead of surfacing from the job a moment later.
    """
    if direction not in ("push", "pull"):
        raise ValueError(direction)
    t = transport or Rclone()
    with _JOB_LOCK:
        if _JOB["running"]:
            raise SyncRefused(f"a {_JOB['direction']} is already running")
    st = status(t)
    if st.kind in _HARD_STOPS:
        raise DriveError(st.detail)
    if direction == "push" and not (st.can_push or force or st.kind == IN_SYNC):
        raise SyncRefused(_refusal("push", st))
    if direction == "pull" and not (st.can_pull or force or st.kind == IN_SYNC):
        raise SyncRefused(_refusal("pull", st))
    with _JOB_LOCK:
        if _JOB["running"]:
            raise SyncRefused(f"a {_JOB['direction']} is already running")
        _JOB.update(running=True, direction=direction, phase="starting", progress=None,
                    result=None, error="", refused=False, started=_now(), finished="")

    def _work() -> None:
        result: dict[str, Any] | None = None
        error, refused = "", False
        try:
            if direction == "push":
                result = {"status": push(t, force=force).to_json()}
            else:
                result = pull(t, force=force)
        except SyncRefused as exc:
            error, refused = str(exc), True
        except Exception as exc:  # noqa: BLE001 — surfaced to the GUI verbatim
            error = str(exc) or exc.__class__.__name__
        with _JOB_LOCK:
            _JOB.update(running=False, phase="done" if not error else "failed",
                        result=result, error=error, refused=refused, finished=_now())

    threading.Thread(target=_work, name=f"drive-{direction}", daemon=True).start()
    return job()


def _refusal(direction: str, st: Status) -> str:
    if direction == "push":
        if st.kind == DRIVE_EMPTY:
            return ""
        return (f"Drive has changes from another machine ({', '.join(st.drive_changes or [])[:300]}). "
                "Pull first, or push anyway to overwrite them.")
    if st.kind == DRIVE_EMPTY:
        return "Drive has no snapshot to pull yet"
    return (f"this machine has changes that are not on Drive ({', '.join(st.local_changes or [])[:300]}). "
            "Push first, or pull anyway to replace them (they are backed up).")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def push(transport: Transport | None = None, *, force: bool = False) -> Status:
    """Upload the workspace and publish a new manifest.

    Refused when Drive holds changes this machine has not pulled, unless
    ``force`` — which the GUI only offers after naming the files it will
    overwrite on Drive.
    """
    t = transport or Rclone()
    with _LOCK:
        _phase("checking")
        st = status(t)
        if st.kind in _HARD_STOPS:
            raise DriveError(st.detail)
        if st.kind == IN_SYNC:
            return st
        if not st.can_push and not force:
            raise SyncRefused(_refusal("push", st))
        base = _load_base()
        for _ in range(3):
            _phase("hashing")
            before = snapshot()
            _phase("uploading")
            t.upload(_workspace())
            _phase("verifying")
            after = snapshot()
            if after == before:
                break
        else:
            raise DriveError("the workspace kept changing during the upload; try again when idle")
        manifest = {
            "version": 1,
            "project": config.PROJECT_ID,
            "id": uuid.uuid4().hex,
            "parent": (base or {}).get("id", ""),
            "machine": socket.gethostname(),
            "pushed_at": _now(),
            "files": after,
        }
        _phase("writing manifest")
        t.write_manifest(manifest)
        _save_base(manifest)
        return Status(IN_SYNC, "pushed", [], [], _header(manifest), _header(manifest))


def pull(transport: Transport | None = None, *, force: bool = False) -> dict[str, Any]:
    """Replace the local workspace with Drive's snapshot.

    Every local file that is replaced or removed is copied under
    ``backups/drive-pull-<stamp>/`` first. Refused when this machine has
    unpushed changes, unless ``force``.
    """
    t = transport or Rclone()
    with _LOCK:
        _phase("checking")
        st = status(t)
        if st.kind in _HARD_STOPS:
            raise DriveError(st.detail)
        if st.kind == DRIVE_EMPTY:
            raise SyncRefused(_refusal("pull", st))
        if st.kind == IN_SYNC:
            return {"status": st.to_json(), "replaced": [], "removed": [], "backup": ""}
        if not st.can_pull and not force:
            raise SyncRefused(_refusal("pull", st))
        drive = t.read_manifest()
        assert drive is not None
        wanted = _files(drive)
        staging = _state_dir() / "staging"
        _phase("downloading")
        t.download(staging)
        _phase("verifying")
        got = snapshot(staging, cache=False)
        if got != wanted:
            raise DriveError("Drive changed during the download (another push?); try again")

        _phase("applying")
        ws = _workspace()
        local = snapshot()
        replaced = sorted(k for k in wanted if local.get(k) != wanted[k])
        removed = sorted(k for k in local if k not in wanted)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = Path(config.BACKUP_DIR) / f"drive-pull-{stamp}"
        for rel in replaced + removed:
            src = ws / rel
            if src.exists():
                dst = backup / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        for rel in replaced:
            dst = ws / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".drive-sync-tmp")
            shutil.copy2(staging / rel, tmp)
            os.replace(tmp, dst)
        for rel in removed:
            (ws / rel).unlink(missing_ok=True)
        _save_base(drive)
        return {
            "status": Status(IN_SYNC, "pulled", [], [], _header(drive), _header(drive)).to_json(),
            "replaced": replaced,
            "removed": removed,
            "backup": str(backup) if (replaced or removed) else "",
        }
