"""Project bundles — save a whole project to one file, verify it, load it back.

    <id>-<UTC stamp>.sebundle          a gzip tar archive:

    MANIFEST.json      schema story-editor/bundle@1: project id and title,
                       created, machine, code commit, what was excluded, and
                       {path, size, sha256} for every file below
    home/…             the project's own folder (workspace/, canon/, worlds/,
                       project.json…)
    external/…         files the project reaches outside its folder (its
                       SillyTavern chat, lorebooks, authored spine, a working
                       log kept elsewhere), each recorded with its original path

Guarantees:

* ``save`` never writes a half-finished bundle under the final name, and
  refuses (writing nothing) if any file changes while it is being saved.
* ``verify`` checks every byte against the manifest, and refuses a truncated
  or corrupted archive, unlisted or missing members, and unsafe member names.
* ``load`` restores into a folder that must not exist yet, checks every file as
  it is written, and only then moves the result into place. It never
  overwrites anything. The loaded copy is isolated: its external files point at
  the copies inside it, and its Drive folder and SillyTavern character folder
  are switched off, so it can never write into the original's integrations.

This module must not import ``config``: it works on any project home, not
only the open one.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import tarfile
import tempfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import project as project_mod
from . import registry as registry_mod

SCHEMA = "story-editor/bundle@1"
SUFFIX = ".sebundle"
MANIFEST = "MANIFEST.json"
_CHUNK = 1 << 20
_CODE_ROOT = Path(__file__).resolve().parent.parent

# Machine-local or rebuildable; opt back in with with_backups / with_index.
BACKUPS = "workspace/backups/"
# Imported font files are licensed per machine; the font *choices* travel.
FONTS = "workspace/fonts/"
FONT_SETTINGS = "workspace/fonts/fonts.json"
ALWAYS_EXCLUDED = (
    "workspace/.drive_sync/",  # this machine's sync state
    ".git/",                   # a play folder's turn history; not project data
)
# When the home is the code repository itself (a project.json at the repo
# root), only these top-level entries are project data; the rest is code.
CODE_ROOT_ENTRIES = ("project.json", "mini_spine.json", "mini_spine.template.json",
                     "workspace/", "canon/", "cards/", "worlds/")
INDEX_SUFFIXES = (".index.sqlite3", ".index.sqlite3-wal", ".index.sqlite3-shm", ".index.sqlite3-journal")
JUNK_NAMES = ("__pycache__", ".DS_Store")


class BundleError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Save
# --------------------------------------------------------------------------- #


@dataclass
class _Item:
    source: Path   # where the bytes come from
    name: str      # archive member name
    size: int
    sha256: str
    mtime: float


def _hash_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def excluded_patterns(*, with_backups: bool, with_index: bool) -> list[str]:
    out = list(ALWAYS_EXCLUDED) + [f"{FONTS}* (font files; fonts.json, the font choices, is kept)"]
    if not with_backups:
        out.append(BACKUPS)
    if not with_index:
        out += [f"*{s}" for s in INDEX_SUFFIXES]
    out += [f"**/{n}" for n in JUNK_NAMES] + [f"*{SUFFIX}", f"*{SUFFIX}.partial"]
    return out


def _is_excluded(rel: str, *, with_backups: bool, with_index: bool) -> bool:
    parts = rel.split("/")
    if any(p in JUNK_NAMES for p in parts):
        return True
    if rel.endswith((SUFFIX, SUFFIX + ".partial")):
        return True
    if rel.startswith(FONTS) and rel not in (FONTS, FONT_SETTINGS):
        return True
    prefixes = list(ALWAYS_EXCLUDED) + ([] if with_backups else [BACKUPS])
    if any(rel.startswith(p) for p in prefixes):
        return True
    return not with_index and rel.endswith(INDEX_SUFFIXES)


def is_code_root(home: Path) -> bool:
    return (home / "story_editor" / "__init__.py").is_file()


def _walk_home(home: Path, *, with_backups: bool, with_index: bool) -> tuple[list[tuple[Path, str]], list[str]]:
    """(source, relative path) for every file to bundle, and the symlinks that
    were followed. Symlinked files and folders are stored as what they point at,
    so a loaded copy never depends on the original machine's layout."""
    code_root = is_code_root(home)

    def wanted(rel: str) -> bool:
        if _is_excluded(rel, with_backups=with_backups, with_index=with_index):
            return False
        return not code_root or "/" not in rel.rstrip("/") and rel in CODE_ROOT_ENTRIES \
            or any(rel.startswith(e) for e in CODE_ROOT_ENTRIES if e.endswith("/"))

    files: list[tuple[Path, str]] = []
    followed: list[str] = []
    seen_dirs: set[str] = set()
    for root, dirs, names in os.walk(home, followlinks=True):
        real = os.path.realpath(root)
        if real in seen_dirs:  # a symlink loop
            dirs[:] = []
            continue
        seen_dirs.add(real)
        rel_root = Path(root).relative_to(home).as_posix()
        rel_root = "" if rel_root == "." else rel_root + "/"
        dirs[:] = sorted(d for d in dirs if wanted(rel_root + d + "/"))
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                followed.append(rel_root + d + "/")
        for n in sorted(names):
            rel = rel_root + n
            src = Path(root) / n
            if not wanted(rel):
                continue
            if not src.is_file():  # sockets, fifos, dangling links
                continue
            if src.is_symlink():
                followed.append(rel)
            files.append((src, rel))
    return files, followed


def _under(path: Path, home: Path) -> bool:
    try:
        path.resolve().relative_to(home)
        return True
    except ValueError:
        return False


def _effective_manifest(home: Path) -> tuple[dict[str, Any], bool]:
    """The project's manifest as written, or a generated one for a bare home."""
    manifest = project_mod.load(home)
    if manifest is not None:
        return dict(manifest.raw), False
    pid, title = registry_mod.describe(home)
    log = "workspace/story.jsonl"
    logs = sorted((home / "workspace").glob("*.jsonl")) if (home / "workspace").is_dir() else []
    if not (home / log).exists() and len(logs) == 1:
        log = f"workspace/{logs[0].name}"
    return {"schema": project_mod.SCHEMA, "id": pid, "title": title, "log": log, "integrations": {}}, True


def _externals(home: Path, raw: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(bundled, not bundled) external references of a manifest."""
    m = project_mod.parse(raw, home)
    bundled: list[dict[str, Any]] = []
    not_bundled: list[dict[str, Any]] = []
    used: set[str] = set()

    def add(role: str, folder: str, p: Path | None) -> None:
        if p is None or _under(p, home):
            return
        if not p.is_file():
            not_bundled.append({"role": role, "original": str(p), "reason": "missing"})
            return
        name = p.name
        k = 2
        while f"external/{folder}/{name}" in used:
            name = f"{p.stem}-{k}{p.suffix}"
            k += 1
        member = f"external/{folder}/{name}"
        used.add(member)
        bundled.append({"role": role, "original": str(p), "path": member})

    add("log", "log", m.log)
    add("sillytavern.chat", "sillytavern", m.st_chat)
    for i, lb in enumerate(m.lorebooks or []):
        add(f"lorebooks[{i}]", "lorebooks", lb)
    add("authored_spine_md", "spine", m.authored_spine_md)
    if m.play_home is not None and not _under(m.play_home, home):
        not_bundled.append({"role": "player_mode.home", "original": str(m.play_home),
                            "reason": "a play folder; the played log is in the working log"})
    if m.st_characters_dir is not None and not _under(m.st_characters_dir, home):
        not_bundled.append({"role": "sillytavern.characters_dir", "original": str(m.st_characters_dir),
                            "reason": "a shared SillyTavern folder; portraits are not project data"})
    return bundled, not_bundled


def _code_commit() -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(_CODE_ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def default_name(project_id: str, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return f"{project_id}-{when.strftime('%Y%m%d-%H%M%SZ')}{SUFFIX}"


def save(home: str | Path, out: str | Path, *, with_backups: bool = False,
         with_index: bool = False) -> Path:
    """Bundle the project at ``home``. ``out`` is a folder (the file gets the
    default name) or a file path ending in .sebundle. Returns the bundle path."""
    home = Path(home).expanduser().resolve()
    if not home.is_dir():
        raise BundleError(f"not a project folder: {home}")
    raw, generated = _effective_manifest(home)
    out = Path(out).expanduser()
    target = out / default_name(raw["id"]) if out.is_dir() or not out.name.endswith(SUFFIX) else out
    if not target.name.endswith(SUFFIX):
        target = target.with_name(target.name + SUFFIX)
    target = target.resolve()
    if target.exists():
        raise BundleError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)

    bundled, not_bundled = _externals(home, raw)
    home_files, followed = _walk_home(home, with_backups=with_backups, with_index=with_index)
    sources = [(src, f"home/{rel}") for src, rel in home_files]
    sources += [(Path(e["original"]), e["path"]) for e in bundled]

    # Pass 1: what we are about to store.
    items: list[_Item] = []
    for src, name in sources:
        digest, size = _hash_file(src)
        items.append(_Item(src, name, size, digest, src.stat().st_mtime))

    manifest = {
        "schema": SCHEMA,
        "project": {"id": raw["id"], "title": raw.get("title") or raw["id"]},
        "project_manifest": raw,
        "manifest_generated": generated,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": socket.gethostname(),
        "code_commit": _code_commit(),
        "source_home": str(home),
        "options": {"with_backups": with_backups, "with_index": with_index},
        "excluded": excluded_patterns(with_backups=with_backups, with_index=with_index),
        "followed_symlinks": followed,
        "external": bundled,
        "not_bundled": not_bundled,
        "files": [{"path": i.name, "size": i.size, "sha256": i.sha256} for i in items],
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    # Pass 2: write, re-hashing every file as it streams in. A file that
    # changed since pass 1 means the project is being edited: refuse.
    partial = target.with_name(target.name + ".partial")
    try:
        with partial.open("xb") as raw_out, \
                gzip.GzipFile(filename="", mode="wb", fileobj=raw_out) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo(MANIFEST)
            info.size = len(manifest_bytes)
            info.mtime = int(datetime.now(timezone.utc).timestamp())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(manifest_bytes))
            for item in items:
                info = tarfile.TarInfo(item.name)
                info.size = item.size
                info.mtime = item.mtime
                info.mode = 0o644
                with item.source.open("rb") as fh:
                    reader = _HashingReader(fh)
                    try:
                        tar.addfile(info, reader)
                    except OSError:  # the file shrank under us
                        reader.size = -1
                    if reader.size != item.size or reader.hexdigest() != item.sha256 or fh.read(1):
                        raise BundleError(
                            f"{item.source} changed while the bundle was being written; "
                            f"nothing was saved — try again when the project is idle")
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


class _HashingReader:
    """A file wrapper that hashes what tarfile reads through it."""

    def __init__(self, fh):
        self._fh = fh
        self._h = hashlib.sha256()
        self.size = 0

    def read(self, n: int = -1) -> bytes:
        data = self._fh.read(n)
        self._h.update(data)
        self.size += len(data)
        return data

    def hexdigest(self) -> str:
        return self._h.hexdigest()


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #


@dataclass
class Report:
    ok: bool
    bundle: str
    project_id: str = ""
    title: str = ""
    files: int = 0
    bytes: int = 0
    errors: list[str] = field(default_factory=list)
    manifest: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {"ok": self.ok, "bundle": self.bundle, "project_id": self.project_id, "title": self.title,
                "files": self.files, "bytes": self.bytes, "errors": self.errors,
                "created": self.manifest.get("created"), "machine": self.manifest.get("machine"),
                "external": self.manifest.get("external", []),
                "not_bundled": self.manifest.get("not_bundled", [])}


def _safe_member_name(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    parts = PurePosixPath(name).parts
    if any(p in ("..", ".", "") for p in parts):
        return False
    return name == MANIFEST or (len(parts) >= 2 and parts[0] in ("home", "external"))


def _check_manifest(manifest: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise BundleError(f"not a story-editor bundle (schema must be {SCHEMA!r})")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise BundleError("manifest has no file list")
    listed: dict[str, dict[str, Any]] = {}
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str) \
                or not isinstance(f.get("size"), int) or not isinstance(f.get("sha256"), str):
            raise BundleError("manifest file entries need path, size and sha256")
        if not _safe_member_name(f["path"]) or f["path"] == MANIFEST:
            raise BundleError(f"unsafe path in manifest: {f['path']!r}")
        if f["path"] in listed:
            raise BundleError(f"duplicate path in manifest: {f['path']!r}")
        listed[f["path"]] = f
    raw = manifest.get("project_manifest")
    errors = project_mod.validate(raw)
    if errors:
        raise BundleError("bundled project manifest is invalid: " + "; ".join(errors))
    return listed


def _walk(bundle: Path, sink=None) -> Report:
    """Stream through a bundle checking everything; ``sink(name, chunks)``
    receives each verified-in-progress file (load writes it to disk)."""
    report = Report(ok=False, bundle=str(bundle))
    seen: set[str] = set()
    listed: dict[str, dict[str, Any]] | None = None
    try:
        with tarfile.open(bundle, "r:gz") as tar:
            for member in tar:
                name = member.name
                if not _safe_member_name(name):
                    raise BundleError(f"unsafe member name: {name!r}")
                if not member.isfile():
                    raise BundleError(f"{name}: only regular files may be bundled")
                fh = tar.extractfile(member)
                assert fh is not None
                if listed is None:
                    if name != MANIFEST:
                        raise BundleError("MANIFEST.json must be the first member")
                    try:
                        manifest = json.loads(fh.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise BundleError(f"MANIFEST.json is not valid JSON ({exc})") from exc
                    listed = _check_manifest(manifest)
                    report.manifest = manifest
                    report.project_id = manifest["project_manifest"]["id"]
                    report.title = manifest.get("project", {}).get("title", "")
                    continue
                if name == MANIFEST:
                    raise BundleError("a second MANIFEST.json")
                entry = listed.get(name)
                if entry is None:
                    raise BundleError(f"{name}: not listed in the manifest")
                if name in seen:
                    raise BundleError(f"{name}: appears twice")
                seen.add(name)
                if member.size != entry["size"]:
                    raise BundleError(f"{name}: size {member.size}, manifest says {entry['size']}")
                h = hashlib.sha256()
                writer = sink(name, member) if sink else None
                try:
                    while chunk := fh.read(_CHUNK):
                        h.update(chunk)
                        if writer:
                            writer.write(chunk)
                finally:
                    if writer:
                        writer.close()
                if h.hexdigest() != entry["sha256"]:
                    raise BundleError(f"{name}: checksum does not match the manifest")
                report.files += 1
                report.bytes += member.size
            # tarfile stops at the end-of-archive marker; read the compressed
            # stream to its end so gzip checks its CRC over every byte —
            # headers and padding included, not only the files' contents.
            while tar.fileobj.read(_CHUNK):
                pass
        if listed is None:
            raise BundleError("empty archive")
        missing = sorted(set(listed) - seen)
        if missing:
            raise BundleError(f"{len(missing)} listed file(s) missing from the archive, e.g. {missing[0]}")
    except BundleError as exc:
        report.errors.append(str(exc))
        return report
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile, OSError) as exc:
        report.errors.append(f"the archive is truncated or corrupt ({exc.__class__.__name__}: {exc})")
        return report
    report.ok = True
    return report


def verify(bundle: str | Path) -> Report:
    """Check a bundle completely without loading it."""
    bundle = Path(bundle).expanduser().resolve()
    if not bundle.is_file():
        return Report(ok=False, bundle=str(bundle), errors=[f"no such file: {bundle}"])
    return _walk(bundle)


# --------------------------------------------------------------------------- #
# Load
# --------------------------------------------------------------------------- #


@dataclass
class Loaded:
    home: Path
    project_id: str
    title: str
    files: int
    rewrites: list[str]
    registered: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"home": str(self.home), "project_id": self.project_id, "title": self.title,
                "files": self.files, "rewrites": self.rewrites, "registered": self.registered}


def _isolate(raw: dict[str, Any], external: list[dict[str, Any]], new_id: str | None) -> tuple[dict[str, Any], list[str]]:
    """The loaded copy's manifest: externals point inside it, outward
    integrations are off. Returns (manifest, human-readable rewrites)."""
    raw = json.loads(json.dumps(raw))
    integ = raw.setdefault("integrations", {})
    by_role = {e["role"]: e["path"] for e in external}
    notes: list[str] = []

    if "log" in by_role:
        raw["log"] = by_role["log"]
        notes.append(f"log → {raw['log']}")
    st = integ.get("sillytavern") or {}
    if "sillytavern.chat" in by_role:
        st["chat"] = by_role["sillytavern.chat"]
        notes.append(f"sillytavern.chat → {st['chat']} (a private copy; the original chat is never written)")
    if st.get("characters_dir"):
        notes.append("sillytavern.characters_dir → off (shared SillyTavern folder)")
        st["characters_dir"] = None
    if st:
        integ["sillytavern"] = st
    if isinstance(integ.get("lorebooks"), list):
        lore = list(integ["lorebooks"])
        for i in range(len(lore)):
            if f"lorebooks[{i}]" in by_role:
                lore[i] = by_role[f"lorebooks[{i}]"]
                notes.append(f"lorebooks[{i}] → {lore[i]}")
        integ["lorebooks"] = lore
    if "authored_spine_md" in by_role:
        integ["authored_spine_md"] = by_role["authored_spine_md"]
        notes.append(f"authored_spine_md → {integ['authored_spine_md']}")
    play = integ.get("player_mode") or {}
    if play.get("home") and not str(play["home"]).startswith("external/"):
        notes.append("player_mode.home → off (the copy never plays into the original's play folder)")
        play["home"] = None
        integ["player_mode"] = play
    drive = integ.get("drive") or {}
    if drive.get("remote"):
        notes.append(f"drive.remote {drive['remote']!r} → off (point it at a new folder to sync this copy)")
        drive["remote"] = None
        integ["drive"] = drive
    if new_id and new_id != raw["id"]:
        notes.append(f"id {raw['id']!r} → {new_id!r}")
        raw["id"] = new_id
        title = raw.get("title") or new_id
        if not title.endswith("(copy)"):
            raw["title"] = f"{title} (copy)"
            notes.append(f"title → {raw['title']!r}")
    return raw, notes


def load(bundle: str | Path, into: str | Path, *, new_id: str | None = None,
         register: bool = False, registry_path: Path | None = None) -> Loaded:
    """Restore a bundle into ``into``, which must not exist yet."""
    bundle = Path(bundle).expanduser().resolve()
    into = Path(into).expanduser().resolve()
    if into.exists() or into.is_symlink():
        raise BundleError(f"{into} already exists; a bundle only loads into a new folder")
    if new_id is not None and not project_mod._ID_RE.match(new_id):
        raise BundleError("id must be lowercase letters, digits and dashes (1-63 chars)")

    first = verify(bundle)  # cheap insurance: never start writing a bad bundle
    if not first.ok:
        raise BundleError("; ".join(first.errors))
    manifest = first.manifest
    pid = new_id or manifest["project_manifest"]["id"]
    if register:
        reg = registry_mod.load(registry_path)
        clash = reg.get(pid)
        if clash is not None:
            raise BundleError(f"a project called {pid!r} is already registered at {clash.home}; "
                              f"load it under another id")

    into.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{into.name}.loading-", dir=into.parent))
    try:
        def sink(name: str, member: tarfile.TarInfo):
            rel = name[len("home/"):] if name.startswith("home/") else name
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            return _Restoring(dest, member.mtime)

        report = _walk(bundle, sink)  # checks every byte again as it lands
        if not report.ok:
            raise BundleError("; ".join(report.errors))
        raw, rewrites = _isolate(manifest["project_manifest"], manifest.get("external", []), new_id)
        if manifest.get("manifest_generated"):
            rewrites.insert(0, "project.json → generated (the original folder had none)")
        if manifest.get("manifest_generated") or rewrites:
            mpath = staging / project_mod.MANIFEST_NAME
            mpath.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        project_mod.parse(raw, staging)  # the result must be a valid project
        if into.exists() or into.is_symlink():
            raise BundleError(f"{into} appeared while loading; nothing was changed")
        os.rename(staging, into)  # same filesystem; fails rather than overwrite
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    loaded = Loaded(home=into, project_id=raw["id"], title=raw.get("title") or raw["id"],
                    files=report.files, rewrites=rewrites)
    if register:
        registry_mod.add(into, registry_path=registry_path)
        loaded.registered = True
    return loaded


class _Restoring:
    def __init__(self, dest: Path, mtime: float):
        self._dest = dest
        self._mtime = mtime
        self._fh = dest.open("xb")  # never overwrite, even inside staging

    def write(self, data: bytes) -> None:
        self._fh.write(data)

    def close(self) -> None:
        self._fh.close()
        os.utime(self._dest, (self._mtime, self._mtime))


# --------------------------------------------------------------------------- #
# Loading from the GUI: upload, inspect, then load under a chosen id
# --------------------------------------------------------------------------- #

_TOKEN_CHARS = set("0123456789abcdef")
_UPLOAD_TTL = 24 * 3600


def _uploads(projects: Path) -> Path:
    return projects / ".uploads"


def _upload_path(token: str, projects: Path) -> Path:
    if len(token) != 16 or not set(token) <= _TOKEN_CHARS:
        raise BundleError("unknown upload")
    path = _uploads(projects) / f"{token}{SUFFIX}"
    if not path.is_file():
        raise BundleError("unknown upload (it may have expired; choose the file again)")
    return path


def suggest_id(project_id: str, projects: Path, *, registry_path: Path | None = None) -> str:
    """A free id for a loaded copy: the bundle's own, unless it is taken here."""
    import re

    reg = registry_mod.load(registry_path)

    def taken(c: str) -> bool:
        return reg.get(c) is not None or (projects / c).exists()

    base = f"{project_id}-copy" if project_id == getattr(project_mod, "LEGACY_ID", None) else project_id
    if not taken(base):
        return base
    # A copy of "story-2" continues as "story-3", not "story-2-2".
    numbered = re.match(r"^(.*\D)-(\d+)$", base)
    stem, n = (numbered.group(1), int(numbered.group(2)) + 1) if numbered else (base, 2)
    while taken(f"{stem}-{n}"):
        n += 1
    return f"{stem}-{n}"


def stage_upload(stream, length: int, projects: Path, *,
                 registry_path: Path | None = None) -> dict[str, Any]:
    """Store an uploaded bundle, verify it, and say what loading it would do.
    Nothing is loaded yet; the upload waits for ``load_upload`` or ``discard_upload``."""
    import secrets
    import time

    uploads = _uploads(projects)
    uploads.mkdir(parents=True, exist_ok=True)
    for old in uploads.glob(f"*{SUFFIX}"):  # forgotten uploads
        if time.time() - old.stat().st_mtime > _UPLOAD_TTL:
            old.unlink(missing_ok=True)
    token = secrets.token_hex(8)
    path = uploads / f"{token}{SUFFIX}"
    remaining = length
    try:
        with path.open("xb") as fh:
            while remaining > 0:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise BundleError("the upload was cut short; choose the file again")
                fh.write(chunk)
                remaining -= len(chunk)
        report = verify(path)
        if not report.ok:
            raise BundleError("; ".join(report.errors))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    suggested = suggest_id(report.project_id, projects, registry_path=registry_path)
    return {
        "token": token,
        "bundle": report.to_json(),
        "suggested_id": suggested,
        "projects_dir": str(projects),
        "target": str(projects / suggested),
    }


def load_upload(token: str, new_id: str, projects: Path, *,
                registry_path: Path | None = None) -> Loaded:
    """Load a staged upload into ``projects/<new_id>`` and register it."""
    path = _upload_path(token, projects)
    if not new_id or not project_mod._ID_RE.match(new_id) or new_id == getattr(project_mod, "LEGACY_ID", None):
        raise BundleError("the name must be lowercase letters, digits and dashes")
    report = verify(path)
    if not report.ok:
        raise BundleError("; ".join(report.errors))
    loaded = load(path, projects / new_id,
                  new_id=new_id if new_id != report.project_id else None,
                  register=True, registry_path=registry_path)
    path.unlink(missing_ok=True)
    return loaded


def discard_upload(token: str, projects: Path) -> None:
    try:
        _upload_path(token, projects).unlink(missing_ok=True)
    except BundleError:
        pass
