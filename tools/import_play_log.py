"""Bring a Player-mode play log into a ProseWright project's working log.

    python tools/import_play_log.py PLAY_DIR PROJECT_HOME [--replace]

The editor's Play view does this after every turn; this is the same step from
the shell (story_editor/play.py has the details). The play folder is only
read. New play messages are appended to the working log with stable ids and
scene headers, characters the engine introduced are filed as lightweight
bibles, and every message gets a voice label from the engine's record.
Messages already in the log are never rewritten.

--replace rebuilds the working log from the play log instead (the previous one
is backed up first) — for when the log should start over from what was played.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("play_dir")
    ap.add_argument("project_home")
    ap.add_argument("--replace", "--force", action="store_true",
                    help="rebuild the working log from the play log (it is backed up first)")
    args = ap.parse_args()
    play = Path(args.play_dir).expanduser().resolve()
    home = Path(args.project_home).expanduser().resolve()
    # The project's own files, never whichever project is open.
    os.environ["STORY_EDITOR_HOME"] = str(home)
    os.environ.pop("STORY_EDITOR_LOG", None)
    from story_editor import backup as backup_mod, config, play as play_mod  # noqa: E402

    if Path(config.HOME_DIR) != home or config.PROJECT is None:
        sys.exit(f"{home} is not a project with a project.json")
    target = config.working_log()
    if args.replace and target.exists():
        kept = backup_mod.backup(target, label="before-play-import")
        print(f"backed up the previous working log to {kept}")
    result = play_mod.sync_into(play_mod.Folder(play), target, replace=args.replace)
    print(f"{result['added']} message(s) added to {target}")
    if result["filed"]:
        print(f"filed {len(result['filed'])} character(s) the engine introduced: {', '.join(result['filed'])}")
    print(f"voice labels: {result['labelled']} written, {result['kept_reviewed']} reviewed label(s) kept")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
