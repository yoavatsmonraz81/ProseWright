"""Loading a bundle through the API, as the GUI does: upload, inspect, load.

The whole user loop runs against an in-process server: save the open project,
load it as a new project, change it, save that, load it again — and every step
lands where it should, with nothing overwritten."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from story_editor import bundle, registry, server

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lantern-quay"


@pytest.fixture()
def api(tmp_path, monkeypatch):
    projects = tmp_path / "story-editor-projects"
    monkeypatch.setenv("STORY_EDITOR_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("STORY_EDITOR_REGISTRY", str(tmp_path / "registry" / "projects.json"))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.StoryEditorHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path: str, body=None, *, raw: bytes | None = None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        headers = {"Content-Type": "application/octet-stream" if raw is not None else "application/json"}
        req = urllib.request.Request(base + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                return resp.status, (json.loads(payload) if "json" in ctype else payload)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    call.projects = projects
    yield call
    httpd.shutdown()
    httpd.server_close()


def _bundle_bytes(home: Path, tmp_path: Path) -> bytes:
    import tempfile

    return bundle.save(home, Path(tempfile.mkdtemp(dir=tmp_path))).read_bytes()


def test_the_save_edit_save_reload_loop(api, tmp_path):
    # Save the open project from the GUI (the example, here).
    status, data = api("/project/save", {})
    assert status == 200 and isinstance(data, bytes)

    # Load it: upload, inspect, load under the suggested name.
    status, staged = api("/project/upload", raw=data)
    assert status == 200, staged
    assert staged["bundle"]["project_id"] == "lantern-quay" and staged["bundle"]["ok"]
    assert staged["suggested_id"] == "lantern-quay"
    assert staged["target"] == str(api.projects / "lantern-quay")
    status, done = api("/project/load", {"token": staged["token"], "id": staged["suggested_id"]})
    assert status == 200, done
    first = Path(done["loaded"]["home"])
    assert first == api.projects / "lantern-quay"
    assert any(p["id"] == "lantern-quay" for p in done["projects"])
    assert not list((api.projects / ".uploads").iterdir())  # the upload is cleaned up

    # Change the loaded project, then save it again.
    log = first / "workspace" / "lantern_quay.jsonl"
    log.write_text(log.read_text() + '{"name": "Wren", "is_user": true, "mes": "A new turn."}\n')
    changed = log.read_bytes()
    again = _bundle_bytes(first, tmp_path)

    # Reload that save: the name is taken now, so another is suggested.
    status, staged = api("/project/upload", raw=again)
    assert status == 200 and staged["suggested_id"] == "lantern-quay-2"
    status, done = api("/project/load", {"token": staged["token"], "id": "lantern-quay-2"})
    assert status == 200, done
    second = Path(done["loaded"]["home"])
    assert (second / "workspace" / "lantern_quay.jsonl").read_bytes() == changed
    assert json.loads((second / "project.json").read_text())["id"] == "lantern-quay-2"
    assert log.read_bytes() == changed  # loading never touched the first copy
    ids = [e.id for e in registry.load().projects]
    assert ids == ["lantern-quay", "lantern-quay-2"]


def test_a_chosen_name_is_used(api, tmp_path):
    status, staged = api("/project/upload", raw=_bundle_bytes(EXAMPLE, tmp_path))
    status, done = api("/project/load", {"token": staged["token"], "id": "harbour-draft"})
    assert status == 200, done
    home = api.projects / "harbour-draft"
    assert Path(done["loaded"]["home"]) == home
    assert json.loads((home / "project.json").read_text())["id"] == "harbour-draft"


def test_refusals_leave_nothing_behind(api, tmp_path):
    good = _bundle_bytes(EXAMPLE, tmp_path)
    # Not a bundle, a truncated one, and an empty request.
    for raw in (b"not a bundle at all", good[: len(good) // 2]):
        status, body = api("/project/upload", raw=raw)
        assert status == 400 and body["error"]
    status, body = api("/project/upload", raw=b"")
    assert status == 400
    assert not list((api.projects / ".uploads").glob("*.sebundle"))

    status, staged = api("/project/upload", raw=good)
    token = staged["token"]
    for bad_id in ("Has Spaces", "", "../escape"):
        status, body = api("/project/load", {"token": token, "id": bad_id})
        assert status == 400, bad_id
    status, body = api("/project/load", {"token": "0" * 16, "id": "x"})
    assert status == 400 and "unknown upload" in body["error"]
    status, body = api("/project/load", {"token": "../../etc/passwd", "id": "x"})
    assert status == 400

    # A name whose folder already exists is refused, and the folder is untouched.
    (api.projects / "taken").mkdir(parents=True)
    (api.projects / "taken" / "mine.txt").write_text("keep")
    status, body = api("/project/load", {"token": token, "id": "taken"})
    assert status == 400 and "already exists" in body["error"]
    assert [p.name for p in (api.projects / "taken").iterdir()] == ["mine.txt"]

    # Cancelling discards the upload.
    status, _ = api("/project/upload/discard", {"token": token})
    assert status == 200
    assert not list((api.projects / ".uploads").glob("*.sebundle"))
    assert registry.load().projects == []


def test_the_projects_view_says_where_loads_go(api):
    status, view = api("/projects")
    assert status == 200 and view["projects_dir"] == str(api.projects)


def test_switching_away_keeps_a_way_back(api, monkeypatch):
    """The open project joins the list before the switch, so the GUI can return to it."""
    from story_editor import config

    status, staged = api("/project/upload", raw=bundle.save(EXAMPLE, api.projects.parent / "b").read_bytes())
    status, done = api("/project/load", {"token": staged["token"], "id": "harbour-draft"})
    assert done["loaded"]["title"].endswith("(copy)")
    restarts = []
    monkeypatch.setattr(server, "_restart_into_registry", lambda: restarts.append(True))
    status, body = api("/project/switch", {"id": "harbour-draft"})
    assert status == 200 and restarts
    reg = registry.load()
    assert reg.active == "harbour-draft"
    assert reg.by_home(config.HOME_DIR) is not None, "the project we left is in the list"
