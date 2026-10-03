"""Project bundles: save → verify → load, and everything that must be refused."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from story_editor import bundle, registry

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lantern-quay"


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture()
def home(tmp_path) -> Path:
    h = tmp_path / "projects" / "lantern-quay"
    shutil.copytree(EXAMPLE, h)
    return h


@pytest.fixture()
def saved(home, tmp_path) -> Path:
    return bundle.save(home, tmp_path / "bundles")


# --------------------------------------------------------------------------- #
# The round trip
# --------------------------------------------------------------------------- #


def test_round_trip_is_byte_identical(home, saved, tmp_path):
    before = _files(home)
    report = bundle.verify(saved)
    assert report.ok, report.errors
    assert report.project_id == "lantern-quay" and report.files == len(before)

    loaded = bundle.load(saved, tmp_path / "copy")
    assert loaded.rewrites == []
    assert _files(loaded.home) == before
    assert _files(home) == before  # saving never touches the source
    for rel in before:
        assert (loaded.home / rel).stat().st_mtime == pytest.approx((home / rel).stat().st_mtime, abs=1)


def test_bundle_name_and_no_leftovers(home, saved):
    assert saved.name.startswith("lantern-quay-") and saved.name.endswith(".sebundle")
    assert [p.name for p in saved.parent.iterdir()] == [saved.name]


def test_machine_local_and_rebuildable_files_are_left_out(home, tmp_path):
    ws = home / "workspace"
    (ws / "backups").mkdir()
    (ws / "backups" / "lantern_quay.20261001.jsonl").write_text("old")
    (ws / "lantern_quay.index.sqlite3").write_bytes(b"index")
    (ws / "fonts").mkdir()
    (ws / "fonts" / "licensed.ttf").write_bytes(b"font")
    (ws / "fonts" / "fonts.json").write_text('{"roles": {"page": {"family": "TT2020 Base"}}}')
    (ws / ".drive_sync").mkdir()
    (ws / ".drive_sync" / "base.json").write_text("{}")
    (home / ".git").mkdir()
    (home / ".git" / "HEAD").write_text("ref")
    (home / "canon" / "__pycache__").mkdir()
    (home / "canon" / "__pycache__" / "x.pyc").write_bytes(b"pyc")
    (ws / "manuscript.json").write_text("{}")

    names = {f["path"] for f in bundle.verify(bundle.save(home, tmp_path / "plain")).manifest["files"]}
    assert "home/workspace/manuscript.json" in names
    assert "home/workspace/fonts/fonts.json" in names  # the font choices travel
    assert not [n for n in names if any(x in n for x in ("backups", "sqlite3", ".ttf", ".drive_sync", ".git", "__pycache__"))]

    names = {f["path"] for f in bundle.verify(bundle.save(home, tmp_path / "full", with_backups=True,
                                                          with_index=True)).manifest["files"]}
    assert "home/workspace/backups/lantern_quay.20261001.jsonl" in names
    assert "home/workspace/lantern_quay.index.sqlite3" in names
    assert not [n for n in names if n.endswith(".ttf") or ".drive_sync" in n]


def test_a_code_checkout_home_bundles_only_project_data(tmp_path):
    root = tmp_path / "checkout"
    shutil.copytree(EXAMPLE, root)
    (root / "story_editor").mkdir()
    (root / "story_editor" / "__init__.py").write_text("")
    (root / "app" / "node_modules").mkdir(parents=True)
    (root / "app" / "node_modules" / "x.js").write_text("x")
    (root / "README.md").write_text("code")
    names = {f["path"] for f in bundle.verify(bundle.save(root, tmp_path / "b")).manifest["files"]}
    assert "home/workspace/lantern_quay.jsonl" in names and "home/project.json" in names
    assert not [n for n in names if n.startswith(("home/story_editor", "home/app", "home/README"))]


def test_a_bare_folder_bundles_with_a_generated_manifest(tmp_path):
    bare = tmp_path / "draft"
    (bare / "workspace").mkdir(parents=True)
    (bare / "workspace" / "draft.jsonl").write_text('{"chat_metadata": {}}\n')
    loaded = bundle.load(bundle.save(bare, tmp_path / "b"), tmp_path / "copy")
    raw = json.loads((loaded.home / "project.json").read_text())
    assert raw["id"] == "draft" and raw["log"] == "workspace/draft.jsonl"
    assert "generated" in loaded.rewrites[0]
    assert not (bare / "project.json").exists()


# --------------------------------------------------------------------------- #
# Externals and isolation of the loaded copy
# --------------------------------------------------------------------------- #


def _wire_externals(home: Path, tmp_path: Path) -> dict[str, Path]:
    outside = tmp_path / "elsewhere"
    (outside / "chats").mkdir(parents=True)
    (outside / "characters").mkdir()
    ext = {
        "chat": outside / "chats" / "Lantern Quay - live.jsonl",
        "lore": outside / "shared_lore.json",
        "spine": outside / "plan.md",
    }
    ext["chat"].write_text('{"chat_metadata": {}}\n{"name": "Wren", "mes": "live"}\n')
    ext["lore"].write_text('{"entries": {}}')
    ext["spine"].write_text("# Plan\n")
    raw = json.loads((home / "project.json").read_text())
    raw["integrations"] = {
        "sillytavern": {"chat": str(ext["chat"]), "characters_dir": str(outside / "characters")},
        "lorebooks": ["worlds/lantern_quay_lorebook.json", str(ext["lore"])],
        "drive": {"remote": "gdrive:stories/lantern-quay"},
        "authored_spine_md": str(ext["spine"]),
    }
    (home / "project.json").write_text(json.dumps(raw, indent=2))
    return ext


def test_externals_travel_and_the_copy_is_isolated(home, tmp_path):
    ext = _wire_externals(home, tmp_path)
    originals = {k: p.read_bytes() for k, p in ext.items()}
    path = bundle.save(home, tmp_path / "b")
    report = bundle.verify(path)
    roles = {e["role"] for e in report.manifest["external"]}
    assert roles == {"sillytavern.chat", "lorebooks[1]", "authored_spine_md"}
    assert [e["role"] for e in report.manifest["not_bundled"]] == ["sillytavern.characters_dir"]

    loaded = bundle.load(path, tmp_path / "copy")
    raw = json.loads((loaded.home / "project.json").read_text())
    integ = raw["integrations"]
    assert integ["sillytavern"]["chat"] == "external/sillytavern/Lantern Quay - live.jsonl"
    assert integ["sillytavern"]["characters_dir"] is None
    assert integ["lorebooks"] == ["worlds/lantern_quay_lorebook.json", "external/lorebooks/shared_lore.json"]
    assert integ["authored_spine_md"] == "external/spine/plan.md"
    assert integ["drive"]["remote"] is None
    assert (loaded.home / integ["sillytavern"]["chat"]).read_bytes() == originals["chat"]
    assert (loaded.home / integ["authored_spine_md"]).read_bytes() == originals["spine"]
    # Everything but the rewritten manifest is identical to the original home.
    copy = _files(loaded.home)
    for rel, data in _files(home).items():
        if rel != "project.json":
            assert copy[rel] == data
    assert {k: p.read_bytes() for k, p in ext.items()} == originals


_PROBE = r"""
import json
from pathlib import Path
from story_editor import config as c, drive_sync
out = {}
for k in sorted(dir(c)):
    if not k.isupper() or k == "MODEL_API_KEY":
        continue
    v = getattr(c, k)
    vals = v if isinstance(v, list) else [v]
    out[k] = [str(x) for x in vals if isinstance(x, (Path, str))]
out["__working_log()"] = [str(c.working_log())]
out["__drive_remote()"] = [drive_sync.remote() or ""]
print(json.dumps(out))
"""


def test_a_loaded_copy_reaches_nothing_of_the_original(home, tmp_path):
    _wire_externals(home, tmp_path)
    loaded = bundle.load(bundle.save(home, tmp_path / "b"), tmp_path / "copy")
    env = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in ("STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    env.update(STORY_EDITOR_HOME=str(loaded.home), STORY_EDITOR_REGISTRY=str(tmp_path / "reg.json"),
               PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    values = json.loads(proc.stdout)
    assert values["__drive_remote()"] == [""]
    leaks = [(k, v) for k, vs in values.items() for v in vs
             if v.startswith(str(tmp_path)) and not v.startswith(str(loaded.home))]
    assert not leaks, f"the loaded copy points back at the original: {leaks}"
    assert values["SOURCE_LOG"][0].startswith(str(loaded.home))


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_load_never_overwrites(saved, tmp_path):
    target = tmp_path / "taken"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    with pytest.raises(bundle.BundleError, match="already exists"):
        bundle.load(saved, target)
    assert _files(target) == {"keep.txt": b"mine"}
    assert not list(tmp_path.glob(".taken.loading-*"))


def test_a_truncated_bundle_is_refused(saved, tmp_path):
    cut = tmp_path / "cut.sebundle"
    cut.write_bytes(saved.read_bytes()[: saved.stat().st_size // 2])
    report = bundle.verify(cut)
    assert not report.ok and "truncated or corrupt" in report.errors[0]
    with pytest.raises(bundle.BundleError):
        bundle.load(cut, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_a_corrupted_bundle_is_refused(saved, tmp_path):
    """A flipped byte anywhere — file contents, tar headers, padding, the gzip
    trailer — is refused (the gzip CRC covers what the file checksums don't)."""
    original = saved.read_bytes()
    # Skip gzip's own header: the original file name stored there carries no
    # project data and gzip doesn't checksum it.
    start = original.index(b"\0", 10) + 1 if original[3] & 0x08 else 10
    accepted = []
    for at in range(start, len(original), max(1, len(original) // 97)):
        data = bytearray(original)
        data[at] ^= 0xFF
        bad = tmp_path / "bad.sebundle"
        bad.write_bytes(bytes(data))
        if bundle.verify(bad).ok:
            accepted.append(at)
    assert not accepted, f"corruption at byte offsets {accepted} went unnoticed"
    data = bytearray(original)
    data[-3] ^= 0xFF  # inside the gzip trailer (stream length)
    bad.write_bytes(bytes(data))
    assert not bundle.verify(bad).ok
    with pytest.raises(bundle.BundleError):
        bundle.load(bad, tmp_path / "copy")
    assert not (tmp_path / "copy").exists()
    assert not list(tmp_path.glob(".copy.loading-*"))


def _repack(src: Path, dst: Path, *, edit=None, extra: list[tuple[tarfile.TarInfo, bytes]] = (),
            drop: str | None = None) -> Path:
    """Rebuild a bundle member by member, optionally tampering with it."""
    with tarfile.open(src, "r:gz") as tin, tarfile.open(dst, "w:gz") as tout:
        for m in tin:
            if m.name == drop:
                continue
            data = tin.extractfile(m).read()
            if edit:
                data = edit(m.name, data)
            m.size = len(data)
            tout.addfile(m, io.BytesIO(data))
        for info, data in extra:
            info.size = len(data)
            tout.addfile(info, io.BytesIO(data))
    return dst


def test_changed_content_fails_its_checksum(saved, tmp_path):
    def edit(name, data):
        return data.replace(b"Wren", b"Nerw") if name == "home/workspace/lantern_quay.jsonl" else data

    report = bundle.verify(_repack(saved, tmp_path / "x.sebundle", edit=edit))
    assert not report.ok and "checksum" in report.errors[0]


def test_unlisted_missing_and_unsafe_members_are_refused(saved, tmp_path):
    extra = tarfile.TarInfo("home/canon/sneaky.json")
    report = bundle.verify(_repack(saved, tmp_path / "a.sebundle", extra=[(extra, b"{}")]))
    assert not report.ok and "not listed" in report.errors[0]

    report = bundle.verify(_repack(saved, tmp_path / "b.sebundle", drop="home/canon/characters.json"))
    assert not report.ok and "missing" in report.errors[0]

    evil = tarfile.TarInfo("home/../../evil.txt")
    report = bundle.verify(_repack(saved, tmp_path / "c.sebundle", extra=[(evil, b"x")]))
    assert not report.ok and "unsafe" in report.errors[0]
    with pytest.raises(bundle.BundleError):
        bundle.load(tmp_path / "c.sebundle", tmp_path / "out" / "copy")
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path / "out" / "copy").exists()

    link = tarfile.TarInfo("home/canon/link")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    report = bundle.verify(_repack(saved, tmp_path / "d.sebundle", extra=[(link, b"")]))
    assert not report.ok and "regular files" in report.errors[0]


def test_not_a_bundle_is_refused(tmp_path):
    other = tmp_path / "other.sebundle"
    with tarfile.open(other, "w:gz") as tar:
        data = b'{"schema": "something-else"}'
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    report = bundle.verify(other)
    assert not report.ok and "not a story-editor bundle" in report.errors[0]
    assert not bundle.verify(tmp_path / "nothing-here.sebundle").ok


def test_a_file_changing_during_save_writes_nothing(home, tmp_path, monkeypatch):
    log = home / "workspace" / "lantern_quay.jsonl"
    real = bundle._hash_file

    def hash_then_edit(path):
        result = real(path)
        if path == log:  # someone writes to the log between the two passes
            with log.open("a") as fh:
                fh.write('{"name": "Wren", "mes": "late edit"}\n')
        return result

    monkeypatch.setattr(bundle, "_hash_file", hash_then_edit)
    out = tmp_path / "b"
    with pytest.raises(bundle.BundleError, match="changed while"):
        bundle.save(home, out)
    assert not out.exists() or not list(out.iterdir())


def test_registering_refuses_an_id_clash_before_writing(saved, tmp_path):
    reg = tmp_path / "reg.json"
    other = tmp_path / "other"
    shutil.copytree(EXAMPLE, other)
    registry.add(other, registry_path=reg)
    with pytest.raises(bundle.BundleError, match="already registered"):
        bundle.load(saved, tmp_path / "copy", register=True, registry_path=reg)
    assert not (tmp_path / "copy").exists()

    loaded = bundle.load(saved, tmp_path / "copy", new_id="lq-copy", register=True, registry_path=reg)
    assert loaded.registered and loaded.project_id == "lq-copy"
    assert json.loads((loaded.home / "project.json").read_text())["id"] == "lq-copy"
    assert registry.load(reg).get("lq-copy").home == loaded.home


def test_the_manifest_records_what_it_needs(saved):
    m = bundle.verify(saved).manifest
    assert m["schema"] == bundle.SCHEMA
    assert m["project"] == {"id": "lantern-quay", "title": "Lantern Quay"}
    assert m["options"] == {"with_backups": False, "with_index": False}
    assert "workspace/backups/" in m["excluded"]
    for f in m["files"]:
        assert len(f["sha256"]) == 64 and f["size"] >= 0
    first = tarfile.open(saved, "r:gz").next()
    assert first.name == "MANIFEST.json"
    assert hashlib.sha256(json.dumps(m).encode()).hexdigest()  # JSON-serialisable
