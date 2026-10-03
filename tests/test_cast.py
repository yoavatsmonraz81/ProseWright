"""The cast behind the Char tab: who is voiced, who is the POV, who is named, and where."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "lantern-quay"

_PROBE = r"""
import json, sys
from story_editor import attribution as attr, cast, config, loader
log = loader.load(config.working_log())
store = attr.load_store(log=log)
# One ensemble label: Ilse and Varga in msg 13, with Wren as the POV.
attr.set_label(store, 13, attr.VoiceLabel(card="Narrator", voice="ensemble", mode="scene_narrator", focal="wren",
               segments=[{"voice": "ilse", "kind": "dialogue"}, {"voice": "varga", "kind": "dialogue"}]))
attr.save_store(store, log=log)
print(json.dumps({"roster": cast.roster(), "varga": cast.member("varga"), "nobody": cast.member("nobody")}))
"""


def _run(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not (k.startswith("STORY_EDITOR_") and k not in (
        "STORY_EDITOR_REGISTRY", "STORY_EDITOR_PROJECTS_DIR"))}
    env["STORY_EDITOR_HOME"] = str(home)
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_roster_counts_voices_pov_and_names(tmp_path):
    home = tmp_path / "lq"
    shutil.copytree(EXAMPLE, home)
    raw = json.loads((home / "canon" / "characters.json").read_text())
    raw["characters"]["hobb"] = {"aliases": ["Hobb"], "label": "Hobb", "origin": "player-mode"}
    (home / "canon" / "characters.json").write_text(json.dumps(raw))

    out = _run(home)
    rows = {r["key"]: r for r in out["roster"]["characters"]}
    assert out["roster"]["messages"] == 15
    assert rows["wren"]["counts"]["voiced"] >= 5           # her own turns (card "Wren", no label)
    assert rows["wren"]["counts"]["pov"] == 1              # focal of the ensemble turn
    assert rows["varga"]["counts"]["voiced"] == 1          # named in the ensemble's segments
    assert rows["varga"]["counts"]["mentioned"] >= 1       # and by name in the prose
    assert rows["hobb"]["origin"] == "player-mode"
    assert rows["hobb"]["counts"] == {"voiced": 0, "pov": 0, "mentioned": 0}
    assert rows["wren"]["origin"] == "cards"
    order = [r["key"] for r in out["roster"]["characters"]]
    assert order.index("wren") < order.index("hobb")       # most present first

    varga = out["varga"]
    assert varga["name"] == "Captain Amos Varga" and varga["spans"]
    scenes = [s["scene"] for s in varga["spans"]]
    assert scenes == sorted(set(scenes), key=scenes.index)  # one row per scene, in order
    voiced = [s for s in varga["spans"] if s["how"] == "voiced"]
    assert voiced and voiced[0]["jump"] == 13 and voiced[0]["scene"] == "The Lighthouse Gallery"
    assert out["nobody"] is None
