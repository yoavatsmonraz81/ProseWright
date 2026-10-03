#!/usr/bin/env python3
"""Beat / msg navigation helpers (no LLM)."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_editor import config, loader, structure as st


def _mini_log(path: Path) -> None:
    rows = [
        {"user_name": "u", "name": "Wren", "is_user": True, "mes": "u0"},
        {"user_name": "u", "name": "Wren", "is_user": False, "mes": "a1"},
        {"user_name": "u", "name": "Ilse", "is_user": False, "mes": "e2"},
        {"user_name": "u", "name": "Wren", "is_user": False, "mes": "a3"},
    ]
    with path.open("w", encoding="utf-8") as fh:
        fh.write("{}\n")
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _mini_spine(path: Path) -> None:
    path.write_text(
        json.dumps({
            "log": "x",
            "beats": [{
                "beat_id": 0,
                "title": "Test beat",
                "justification": "x",
                "start_msg_id": 0,
                "end_msg_id": 3,
            }],
        }),
        encoding="utf-8",
    )


def test_locate_and_resolve_beat_position():
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        ws = home / "workspace"
        ws.mkdir()
        log_path = ws / "story.jsonl"
        _mini_log(log_path)
        _mini_spine(ws / "derived_spine.json")

        saved = {k: getattr(config, k) for k in ("HOME_DIR", "WORKSPACE_DIR", "DERIVED_SPINE", "WORKING_LOG")}
        try:
            config.HOME_DIR = home
            config.WORKSPACE_DIR = ws
            config.DERIVED_SPINE = ws / "derived_spine.json"
            config.WORKING_LOG = log_path

            log = loader.load(log_path)
            loc = st.locate_message(log, 2)
            assert loc.beat_id == 0
            assert loc.position_in_beat == 3
            assert loc.total_in_beat == 4

            loc2 = st.resolve_beat_position(log, 0, 2)
            assert loc2.msg_id == 1
            assert loc2.speaker == "Wren"

            wren = st.resolve_beat_position(log, 0, 2, speaker="Wren")
            assert wren.msg_id == 1
            assert wren.position_filtered == 2
        finally:
            # Restore every setting this test retargets; leaving WORKSPACE_DIR or
            # WORKING_LOG on a deleted temp dir leaks into every later test.
            for k, v in saved.items():
                setattr(config, k, v)


def main() -> int:
    failed = 0
    for fn in (test_locate_and_resolve_beat_position,):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            print(f"FAIL  {fn.__name__} — {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
