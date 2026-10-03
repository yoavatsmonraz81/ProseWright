"""tools/import_play_log.py: a Player-mode log becomes a project's working log,
with scene headers, stable ids, voice labels and bibles for minor characters."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "import_play_log.py"


def _row(name, mes, *, user, scene, kind, ts, **meta):
    return {"name": name, "is_user": user, "is_system": False, "send_date": ts, "mes": mes,
            "extra": {"player_mode": {"scene": scene, "kind": kind, **meta}}}


def _play(tmp: Path) -> Path:
    play = tmp / "play"
    (play / "world" / "cast").mkdir(parents=True)
    (play / "state").mkdir()
    (play / "world" / "spine.json").write_text(json.dumps([
        {"id": "hall", "title": "Night One · the hall", "location": "the hall"},
        {"id": "garden", "title": "Day Two · the garden", "location": "the garden"},
    ]))
    (play / "state" / "state.json").write_text(json.dumps({"pov": "wren"}))
    (play / "state" / "cast.json").write_text(json.dumps({"npcs": {
        "Hobb of the Salt Anchor": {"court": "harbour folk", "standing": "the innkeeper", "named": True,
                                    "status": "stage", "first_scene": "hall"},
        "Varga": {"court": "the Gull's Due", "standing": "a captain"},
    }, "aliases": {}}))
    rows = [
        _row("Narrator", "The hall is cold.", user=False, scene="hall", kind="narration", ts="t1",
             plan={"reacts": [], "entrance": {"present": False}}),
        _row("Wren", "Wren looks around.", user=True, scene="hall", kind="move", ts="t2"),
        _row("Narrator", "Hobb waves.", user=False, scene="hall", kind="narration", ts="t3",
             plan={"reacts": ["Hobb of the Salt Anchor"], "entrance": {"present": True, "name": "Hobb of the Salt Anchor"}}),
        _row("Narrator", "*They went to the garden.*", user=False, scene="hall", kind="summary", ts="t4"),
        _row("Narrator", "Ilse and Hobb argue.", user=False, scene="garden", kind="narration", ts="t5",
             pov="wren", voices=["Ilse", "Hobb"]),
    ]
    lines = [json.dumps({"chat_metadata": {"player_mode": True}})] + [json.dumps(r) for r in rows]
    (play / "log.jsonl").write_text("\n".join(lines) + "\n")
    return play


def _project(tmp: Path) -> Path:
    home = tmp / "editor"
    (home / "canon").mkdir(parents=True)
    (home / "project.json").write_text(json.dumps({
        "schema": "story-editor/project@1", "id": "test-story", "title": "Test", "log": "workspace/story.jsonl"}))
    (home / "canon" / "characters.json").write_text(json.dumps({"schema": "story-editor/characters@1", "characters": {
        "wren": {"aliases": ["Wren"], "label": "Wren"},
        "ilse": {"aliases": ["Ilse"], "label": "Ilse"},
        "varga": {"aliases": ["Varga"], "label": "Varga — written by hand", "role": "kept as is"},
    }}))
    return home


def _run(play: Path, home: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in (
        "STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    return subprocess.run([sys.executable, str(TOOL), str(play), str(home), *extra], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _labels(home: Path) -> dict[str, dict]:
    return json.loads((home / "canon" / "voice_attribution.json").read_text())["labels"]


def test_import_files_headers_ids_labels_and_minor_characters(tmp_path):
    play, home = _play(tmp_path), _project(tmp_path)
    before = _tree(play)
    proc = _run(play, home)
    assert proc.returncode == 0, proc.stderr
    assert _tree(play) == before  # the play folder is only read

    rows = [json.loads(line) for line in (home / "workspace" / "story.jsonl").read_text().splitlines()[1:]]
    assert rows[0]["mes"].startswith("[ 📍 Night One · the hall ]")
    assert rows[4]["mes"].startswith("[ 📍 Day Two · the garden ]")
    uids = [r["extra"]["se_uid"] for r in rows]
    assert len(set(uids)) == 5 and all(u.startswith("m-") for u in uids)

    bibles = json.loads((home / "canon" / "characters.json").read_text())["characters"]
    assert bibles["hobb"]["origin"] == "player-mode"
    assert bibles["hobb"]["aliases"] == ["Hobb", "Hobb of the Salt Anchor"]
    assert bibles["hobb"]["label"] == "Hobb — harbour folk"
    assert bibles["varga"]["label"] == "Varga — written by hand"  # never overwritten

    labels = _labels(home)
    by_uid = {u: labels.get(u) for u in uids}
    assert by_uid[uids[0]]["voice"] == "narrator" and by_uid[uids[0]]["focal"] == "wren"
    assert by_uid[uids[1]]["voice"] == "wren" and by_uid[uids[1]]["mode"] == "pov"
    assert by_uid[uids[2]]["voice"] == "hobb" and by_uid[uids[2]]["mode"] == "mixed"
    assert by_uid[uids[3]] is None  # a scene summary is plain narration
    ensemble = by_uid[uids[4]]
    assert ensemble["voice"] == "ensemble" and ensemble["mode"] == "scene_narrator"
    assert [s["voice"] for s in ensemble["segments"]] == ["ilse", "hobb"]
    assert all(lab["source"] == "player_mode" for lab in labels.values())


def test_reimport_keeps_ids_and_reviewed_labels(tmp_path):
    play, home = _play(tmp_path), _project(tmp_path)
    assert _run(play, home).returncode == 0
    first_uids = [json.loads(line)["extra"]["se_uid"]
                  for line in (home / "workspace" / "story.jsonl").read_text().splitlines()[1:]]

    # The author corrects one label in the review queue.
    store = json.loads((home / "canon" / "voice_attribution.json").read_text())
    store["labels"][first_uids[2]].update(voice="ilse", reviewed=True, source="manual")
    (home / "canon" / "voice_attribution.json").write_text(json.dumps(store))

    # Running it again adds nothing and keeps the reviewed label.
    again_run = _run(play, home)
    assert again_run.returncode == 0, again_run.stderr
    assert "0 message(s) added" in again_run.stdout and "1 reviewed label(s) kept" in again_run.stdout
    proc = _run(play, home, "--replace")
    assert proc.returncode == 0, proc.stderr
    assert "1 reviewed label(s) kept" in proc.stdout
    again = [json.loads(line)["extra"]["se_uid"]
             for line in (home / "workspace" / "story.jsonl").read_text().splitlines()[1:]]
    assert again == first_uids
    kept = _labels(home)[first_uids[2]]
    assert kept["voice"] == "ilse" and kept["reviewed"] is True
    assert list((home / "workspace" / "backups").glob("story.*before-play-import*.jsonl"))
