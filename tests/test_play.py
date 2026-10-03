"""Play inside the editor (story_editor/play.py), without model calls.

A small play folder with its own git turn history stands in for a played
story; the tests run in a fresh interpreter so config resolves the project."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]


def _row(name, mes, *, user, scene, kind, ts, **meta):
    return {"name": name, "is_user": user, "is_system": False, "send_date": ts, "mes": mes,
            "extra": {"player_mode": {"scene": scene, "kind": kind, **meta}}}


def _write_log(play: Path, rows: list[dict]) -> None:
    lines = [json.dumps({"chat_metadata": {"player_mode": True}})] + [json.dumps(r) for r in rows]
    (play / "log.jsonl").write_text("\n".join(lines) + "\n")


def _commit(play: Path, msg: str) -> None:
    subprocess.run([*_GIT, "-C", str(play), "add", "-A"], check=True, capture_output=True)
    subprocess.run([*_GIT, "-C", str(play), "commit", "-q", "-m", msg], check=True, capture_output=True)


ROWS = [
    _row("Narrator", "The quay is cold.", user=False, scene="quay", kind="narration", ts="t1",
         pov="wren", voices=[]),
    _row("Wren", "Wren waits.", user=True, scene="quay", kind="move", ts="t2", pov="wren"),
    _row("Narrator", "Hobb waves from the inn door.", user=False, scene="quay", kind="narration", ts="t3",
         pov="wren", voices=["Hobb of the Salt Anchor"]),
]
TURN = [
    _row("Wren", "Wren waves back.", user=True, scene="quay", kind="move", ts="t4", pov="wren"),
    _row("Narrator", "Hobb and Ilse cross the stones together.", user=False, scene="quay", kind="narration",
         ts="t5", pov="wren", voices=["Hobb of the Salt Anchor", "Ilse"]),
]


def _setup(tmp: Path) -> tuple[Path, Path]:
    play = tmp / "play"
    for d in ("world/cast", "state", "state_init"):
        (play / d).mkdir(parents=True)
    config = {"name": "Test Quay", "models": {}}
    for name in ("project.json", "play.json"):  # the engine's config file, under either name
        (play / name).write_text(json.dumps(config))
    (play / "world" / "spine.json").write_text(json.dumps([
        {"id": "quay", "title": "Dusk · the quay", "question": "Who comes?", "ladder": ["a", "b"], "dwell_min": 2}]))
    for cid, name in (("wren", "Wren"), ("ilse", "Ilse")):
        (play / "world" / "cast" / f"{cid}.json").write_text(json.dumps({"name": name, "public": "", "card": ""}))
    state = {"scene": "quay", "rung": 1, "moves": 1, "pov": "wren", "party": ["ilse"], "stage": [], "mystery": ""}
    for d in ("state", "state_init"):
        (play / d / "state.json").write_text(json.dumps(state))
    (play / "state" / "cast.json").write_text(json.dumps({"npcs": {
        "Hobb of the Salt Anchor": {"court": "harbour folk", "standing": "the innkeeper"}}, "aliases": {}}))
    _write_log(play, ROWS)
    subprocess.run([*_GIT, "-C", str(play), "init", "-q"], check=True)
    _commit(play, "init")

    home = tmp / "editor"
    (home / "canon").mkdir(parents=True)
    (home / "project.json").write_text(json.dumps({
        "schema": "story-editor/project@1", "id": "test-quay", "title": "Test Quay", "log": "workspace/story.jsonl",
        "integrations": {"player_mode": {"home": str(play)}}}))
    (home / "canon" / "characters.json").write_text(json.dumps({"schema": "story-editor/characters@1", "characters": {
        "wren": {"aliases": ["Wren"], "label": "Wren"}, "ilse": {"aliases": ["Ilse"], "label": "Ilse"}}}))
    return play, home


def _py(home: Path, code: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in (
        "STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    env["STORY_EDITOR_HOME"] = str(home)
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _log_rows(home: Path) -> list[dict]:
    return [json.loads(line) for line in (home / "workspace" / "story.jsonl").read_text().splitlines()[1:]]


def test_view_and_sync(tmp_path):
    play, home = _setup(tmp_path)
    v = _py(home, "import json; from story_editor import play; print(json.dumps(play.view()))")
    assert v["configured"] and v["name"] == "Test Quay"
    assert v["status"]["title"] == "Dusk · the quay" and v["status"]["rungs"] == 2
    assert v["status"]["pov_name"] == "Wren" and [c["id"] for c in v["cast"]] == ["ilse", "wren"]
    assert len(v["rows"]) == 3 and v["not_in_log"] == 3 and v["can_undo"] is False

    r = _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")
    assert r["added"] == 3 and r["filed"] == ["hobb"]
    rows = _log_rows(home)
    assert rows[0]["mes"].startswith("[ 📍 Dusk · the quay ]")
    assert _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")["added"] == 0

    # The play folder gets a turn; only the new messages are appended.
    _write_log(play, ROWS + TURN)
    _commit(play, "turn 1")
    r = _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")
    assert r["added"] == 2
    labels = json.loads((home / "canon" / "voice_attribution.json").read_text())["labels"]
    last = labels[_log_rows(home)[-1]["extra"]["se_uid"]]
    assert last["voice"] == "ensemble" and [s["voice"] for s in last["segments"]] == ["hobb", "ilse"]


def test_undo_takes_the_turn_out_of_the_log_unless_edited(tmp_path):
    play, home = _setup(tmp_path)
    _write_log(play, ROWS + TURN)
    _commit(play, "turn 1")
    _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")
    assert len(_log_rows(home)) == 5

    r = _py(home, "import json; from story_editor import play; print(json.dumps(play.undo()))")
    assert r["removed"] == 2 and r["kept_edited"] == 0
    assert len(_log_rows(home)) == 3
    assert len([json.loads(x) for x in (play / "log.jsonl").read_text().splitlines()[1:]]) == 3

    # Play it again, edit the reply in the editor's log, then undo: the edit stays.
    _write_log(play, ROWS + TURN)
    _commit(play, "turn 1 again")
    _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")
    rows = _log_rows(home)
    rows[-1]["mes"] = "Hobb and Ilse cross the wet stones together, arguing."
    lines = (home / "workspace" / "story.jsonl").read_text().splitlines()
    (home / "workspace" / "story.jsonl").write_text("\n".join([lines[0]] + [json.dumps(x) for x in rows]) + "\n")
    r = _py(home, "import json; from story_editor import play; print(json.dumps(play.undo()))")
    assert r["removed"] == 1 and r["kept_edited"] == 1
    assert [x["mes"] for x in _log_rows(home)][-1].endswith("arguing.")


def test_one_turn_at_a_time(tmp_path):
    play, home = _setup(tmp_path)
    r = _py(home, """
import json
from story_editor import play
play._LOCK.acquire()
try:
    play.sync()
    out = "ran"
except play.PlayBusy as exc:
    out = str(exc)
print(json.dumps({"out": out}))
""")
    assert "already being played" in r["out"]


def test_a_project_without_a_play_folder(tmp_path):
    home = tmp_path / "plain"
    (home / "workspace").mkdir(parents=True)
    (home / "project.json").write_text(json.dumps({
        "schema": "story-editor/project@1", "id": "plain", "title": "Plain", "log": "workspace/story.jsonl"}))
    v = _py(home, "import json; from story_editor import play; print(json.dumps(play.view()))")
    assert v == {"configured": False}


_FAKE_ENGINE = r"""
import itertools, json
from story_editor import play
backend, store, turn = play._engine()
PLAN = {"read": "r", "move": "consequence", "rung_action": "hold", "rung_after": 1, "reacts": ["Ilse"],
        "entrance": {"present": False, "name": "", "court": "", "affiliation": "", "standing": "", "why_here": "",
                     "wants": "", "named": False},
        "stage_after": [], "mystery_after": "", "direction": "Ilse answers.", "must_not": [], "target_words": 50,
        "mood": False, "events": [], "exit": False, "summary": "",
        "shed_pending": {"present": False, "truth": "", "scented_by": ""},
        "reveal_pending": {"present": False, "truth": "", "noticed_by": ""}}
tellings = itertools.count(1)
def fake(model, system, prompt, effort=None, schema=None):
    if schema:
        return backend.Result(text=json.dumps(PLAN), data=PLAN, cost_usd=0.02)
    return backend.Result(text=f"Telling {next(tellings)}.", cost_usd=0.01)
backend.call = fake
def log_rows():
    lines = open(play.config.working_log()).read().splitlines()[1:]
    return [json.loads(x) for x in lines]
out = {}
"""


def test_retell_swipe_and_replan_keep_the_log_in_step(tmp_path):
    play, home = _setup(tmp_path)
    r = _py(home, _FAKE_ENGINE + """
play.turn("Wren waves.")
out["after_turn"] = [x["mes"] for x in log_rows()][-2:]
play.renarrate()
out["after_retell"] = log_rows()[-1]["mes"]
row = json.loads(open(play.home() / "log.jsonl").read().splitlines()[-1])
out["swipes"] = [row["swipes"], row["swipe_id"]]
play.choose_swipe(0)
out["after_swipe_back"] = log_rows()[-1]["mes"]
out["play_rows"] = len(play.Folder(play.home()).rows())
r = play.reroll()
out["replan"] = {"removed": r["removed"], "added": r["synced"]["added"]}
out["after_replan"] = [x["mes"] for x in log_rows()][-2:]
out["play_rows_after"] = len(play.Folder(play.home()).rows())
print(json.dumps(out))
""")
    assert r["after_turn"] == ["Wren waves.", "Telling 1."]
    assert r["after_retell"] == "Telling 2."
    assert r["swipes"] == [["Telling 1.", "Telling 2."], 1]
    assert r["after_swipe_back"] == "Telling 1."
    # Re-plan replays the same move: the old move and reply leave the log, the new ones arrive.
    assert r["replan"] == {"removed": 2, "added": 2}
    assert r["after_replan"] == ["Wren waves.", "Telling 3."]
    assert r["play_rows_after"] == r["play_rows"]


def test_a_reply_edited_in_the_log_is_never_retold_over(tmp_path):
    play, home = _setup(tmp_path)
    r = _py(home, _FAKE_ENGINE + """
play.turn("Wren waves.")
path = play.config.working_log()
lines = open(path).read().splitlines()
last = json.loads(lines[-1]); last["mes"] = "Edited by hand."
lines[-1] = json.dumps(last)
open(path, "w").write("\\n".join(lines) + "\\n")
res = play.renarrate()
out["kept"] = res["kept_edited"]
out["last"] = log_rows()[-1]["mes"]
print(json.dumps(out))
""")
    assert r == {"kept": 1, "last": "Edited by hand."}


def test_a_new_played_project_opens_with_an_empty_log(tmp_path):
    play, home = _setup(tmp_path)
    assert not (home / "workspace" / "story.jsonl").exists()
    r = _py(home, """
import json
from story_editor import config, loader, server, structure
server._start_empty_play_log()
log = loader.load(config.working_log())
print(json.dumps({"messages": len(log), "scenes": len(structure.segment_scenes(log))}))
""")
    assert r == {"messages": 0, "scenes": 0}
    # The first sync appends to it like to any log.
    r = _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")
    assert r["added"] == 3


def test_a_character_with_a_bible_is_not_filed_again(tmp_path):
    play, home = _setup(tmp_path)
    bibles = json.loads((home / "canon" / "characters.json").read_text())
    bibles["characters"]["varga"] = {"aliases": ["Varga", "Captain Varga"],
                                     "label": "Captain Amos Varga — master of the Gull's Due"}
    (home / "canon" / "characters.json").write_text(json.dumps(bibles))
    (play / "state" / "cast.json").write_text(json.dumps({"npcs": {
        "Captain Amos Varga": {"standing": "master of the brig"},  # matched by the bible's label
        "Mistress Ilse": {"standing": "the clerk"},                # by alias, once the title is off
        "Hobb of the Salt Anchor": {"court": "harbour folk", "standing": "the innkeeper"}}, "aliases": {}}))
    _commit(play, "cast")
    r = _py(home, "import json; from story_editor import play; print(json.dumps(play.sync()))")
    assert r["filed"] == ["hobb"]
    keys = set(json.loads((home / "canon" / "characters.json").read_text())["characters"])
    assert keys == {"wren", "ilse", "varga", "hobb"}
