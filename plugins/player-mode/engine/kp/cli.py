"""kp: the player-mode command line.

    kp begin                 establish the current scene (first turn)
    kp turn "<move>"         play one move ("y"/"n" answers a pending cut)
    kp play                  interactive REPL (the Zork-style front end)
    kp autoplay N            the player agent plays N moves
    kp replay FILE           play scripted moves, one per line
    kp status | as CHAR | undo | reset --yes
    kp renarrate             tell the last reply again (same events); tellings are kept
    kp swipe N               choose telling N of the last reply
    kp replan                play the last turn again from scratch (other events)
    kp bench MOVES --setup claude --setup local=local.json
                             the same moves through different models, side by side

    kp use PATH              remember PATH as the default project home

The project home comes from --home, $PLAYER_MODE_HOME, the home remembered by
`kp use`, or the current directory.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from . import turn as turn_mod
from .backend import ModelError
from .store import Project

DIM, BOLD, CYAN, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[0m"
REMEMBERED = Path.home() / ".config" / "player-mode" / "home"


def _default_home() -> str:
    if os.environ.get("PLAYER_MODE_HOME"):
        return os.environ["PLAYER_MODE_HOME"]
    if REMEMBERED.exists():
        return REMEMBERED.read_text(encoding="utf-8").strip()
    return "."
DEFAULT_PROFILE = "curious and careful; follows their own wants; sometimes quiet; asks the people they meet real questions"


def _c(code: str, text: str) -> str:
    return f"{code}{text}{RESET}" if sys.stdout.isatty() else text


def _prompt() -> str:
    """The input prompt. Colour codes are wrapped in \\001/\\002 so readline treats them as
    zero-width; otherwise it miscounts the prompt length and garbles lines that wrap."""
    if not sys.stdout.isatty():
        return "> "
    return f"\001{BOLD}\002> \001{RESET}\002"


def _show(out: turn_mod.TurnOutput, debug: bool) -> None:
    if debug:
        for line in out.debug:
            print(_c(DIM, line))
    if out.prose:
        print("\n" + out.prose + "\n")
    if out.status:
        print(_c(CYAN, out.status))
    if out.prompt:
        print(_c(BOLD, out.prompt))
    print()


def _turn(project: Project, move: str | None, debug: bool) -> None:
    try:
        _show(turn_mod.run(project, move, debug=debug), debug)
    except ModelError as exc:
        print(f"model error: {exc}", file=sys.stderr)


def _run(work, debug: bool) -> None:
    """A turn-like operation (re-narrate, re-plan), shown like a turn."""
    try:
        _show(work(), debug)
    except (ModelError, ValueError) as exc:
        print(f"{exc}", file=sys.stderr)


def _repl(project: Project, debug: bool) -> None:
    try:
        import readline  # noqa: F401  (line editing and history)
    except ImportError:
        pass
    state = project.load("state")
    if not project.turns():
        _turn(project, None, debug)
    else:
        print(_c(CYAN, turn_mod.status_line(project, state)) + "\n")
    while True:
        try:
            line = input(_prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            if project.load("state").get("pending"):
                _turn(project, "y", debug)
            continue
        if line in ("/quit", "/q"):
            return
        if line == "/debug":
            debug = not debug
            print(f"debug {'on' if debug else 'off'}")
        elif line == "/status":
            print(_c(CYAN, turn_mod.status_line(project, project.load("state"))))
        elif line.startswith("/as "):
            print(turn_mod.switch_pov(project, line[4:].strip().lower()))
        elif line == "/undo":
            print(project.undo())
        elif line in ("/renarrate", "/rn"):
            _run(lambda: turn_mod.renarrate(project, debug=debug), debug)
        elif line in ("/replan", "/rp"):
            _run(lambda: turn_mod.reroll(project, debug=debug), debug)
        elif line.startswith("/swipe "):
            try:
                print(turn_mod.choose_swipe(project, int(line.split()[1]) - 1))
            except (ValueError, IndexError) as exc:
                print(exc)
        elif line == "/threads":
            for t in project.load("threads"):
                print(f"  {t['id']} [{t['status']}] {t['text']} ({t.get('owner', '')})")
        else:
            _turn(project, line, debug)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kp", description="Player mode: play the story.")
    ap.add_argument("--home", default=None)
    ap.add_argument("--debug", action="store_true", help="show planner and engine lines")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("begin")
    p = sub.add_parser("turn"); p.add_argument("move")
    sub.add_parser("play")
    p = sub.add_parser("autoplay"); p.add_argument("n", type=int); p.add_argument("--profile", default=DEFAULT_PROFILE)
    p = sub.add_parser("replay"); p.add_argument("file")
    sub.add_parser("status")
    p = sub.add_parser("as"); p.add_argument("char")
    sub.add_parser("undo")
    sub.add_parser("renarrate", help="tell the last reply again (same events); every telling is kept")
    sub.add_parser("replan", help="play the last turn again from scratch (other events)")
    p = sub.add_parser("swipe", help="choose telling N of the last reply"); p.add_argument("n", type=int)
    p = sub.add_parser("bench", help="play scripted moves through different model setups, side by side")
    p.add_argument("moves", help="moves separated by lines of ---")
    p.add_argument("--setup", action="append", required=True, help="claude, or NAME=FILE (role → model JSON)")
    p.add_argument("--reset", action="store_true", help="start every setup from state_init/")
    p.add_argument("--out", help="write the report here (default: kp-bench-<time>.md)")
    p = sub.add_parser("reset"); p.add_argument("--yes", action="store_true")
    p = sub.add_parser("use"); p.add_argument("path")
    args = ap.parse_args(argv)
    if args.cmd == "use":
        project = Project(Path(args.path))
        REMEMBERED.parent.mkdir(parents=True, exist_ok=True)
        REMEMBERED.write_text(str(project.home) + "\n", encoding="utf-8")
        print(f"project home: {project.home}")
        return
    project = Project(Path(args.home or _default_home()))

    if args.cmd == "begin":
        _turn(project, None, args.debug)
    elif args.cmd == "turn":
        _turn(project, args.move, args.debug)
    elif args.cmd == "play":
        _repl(project, args.debug)
    elif args.cmd == "autoplay":
        if not project.turns():
            _turn(project, None, args.debug)
        for _ in range(args.n):
            if project.load("state").get("pending"):
                move = "y"
            else:
                move = turn_mod.player_move(project, args.profile)
                print(_c(BOLD, "> ") + move + "\n")
            _turn(project, move, args.debug)
    elif args.cmd == "replay":
        if not project.turns():
            _turn(project, None, args.debug)
        for move in Path(args.file).read_text(encoding="utf-8").split("\n---\n"):
            move = move.strip()
            if move:
                print(_c(BOLD, "> ") + move + "\n")
                _turn(project, move, args.debug)
    elif args.cmd == "status":
        print(turn_mod.status_line(project, project.load("state")))
    elif args.cmd == "as":
        print(turn_mod.switch_pov(project, args.char.lower()))
    elif args.cmd == "undo":
        print(project.undo())
    elif args.cmd == "renarrate":
        _run(lambda: turn_mod.renarrate(project, debug=args.debug), args.debug)
    elif args.cmd == "replan":
        _run(lambda: turn_mod.reroll(project, debug=args.debug), args.debug)
    elif args.cmd == "swipe":
        try:
            print(turn_mod.choose_swipe(project, args.n - 1))
        except ValueError as exc:
            sys.exit(str(exc))
    elif args.cmd == "bench":
        from . import bench
        moves = [m.strip() for m in Path(args.moves).read_text(encoding="utf-8").split("\n---\n") if m.strip()]
        report = bench.run(project.home, moves, args.setup, reset=args.reset)
        out = Path(args.out or f"kp-bench-{__import__('datetime').datetime.now():%Y%m%d-%H%M%S}.md")
        out.write_text(report, encoding="utf-8")
        print(f"report: {out}")
    elif args.cmd == "reset":
        if not args.yes:
            sys.exit("reset wipes the log and ledgers back to state_init/; pass --yes to confirm")
        for name in ("log.jsonl", "status.json"):
            (project.home / name).unlink(missing_ok=True)
        shutil.rmtree(project.home / "state", ignore_errors=True)
        shutil.copytree(project.home / "state_init", project.home / "state")
        project.commit("reset")
        print("reset to state_init/")


if __name__ == "__main__":
    main()
