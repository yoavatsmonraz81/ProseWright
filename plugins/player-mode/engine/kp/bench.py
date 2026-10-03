"""kp bench: the same scripted moves through different model setups, side by side.

    kp bench MOVES --setup claude --setup local=local.json [--reset] [--out report.md]

MOVES is a text file of moves separated by lines of `---` (as for `kp replay`;
"y" answers a scene cut). Each --setup is NAME or NAME=FILE: "claude" alone
means the default Claude models; FILE is a JSON object of role → model, the
same shape as "models" in the play folder's config, e.g.

    {"planner": "claude-sonnet-5-5",
     "narrator": {"url": "http://127.0.0.1:5000/v1", "model": "gemma-4-31b"}}

Every setup plays on its own throwaway copy of the play folder, from its
current state (or, with --reset, from state_init/), so the real story is never
touched. The report gives, per setup, how many turns worked, the time per
turn, tokens, what the engine's checks caught (POV leaks, stage-cap trims,
dwell-guard blocks, repeated phrases), and the prose of every turn.
"""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import turn as turn_mod
from .backend import ModelError
from .store import Project

DEFAULTS = {"planner": "claude-sonnet-5-5", "narrator": "claude-sonnet-5-5", "player": "claude-haiku-4-5"}
CHECKS = {"leak": "POV leak retried", "stage cap": "stage trimmed", "dwell guard": "exit blocked (dwell)",
          "refrains": "phrase repeated", "habit name": "stock name used"}


def _setup(spec: str) -> tuple[str, dict]:
    name, _, path = spec.partition("=")
    if not path:
        if name != "claude":
            raise SystemExit(f"--setup {spec}: give NAME=FILE (only 'claude' stands alone)")
        return name, dict(DEFAULTS)
    models = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    models = models.get("models", models)
    return name, {**DEFAULTS, **models}


def _copy(home: Path, dest: Path, reset: bool) -> Project:
    shutil.copytree(home, dest, ignore=shutil.ignore_patterns(".git"))
    if reset:
        for name in ("log.jsonl", "status.json"):
            (dest / name).unlink(missing_ok=True)
        shutil.rmtree(dest / "state", ignore_errors=True)
        shutil.copytree(dest / "state_init", dest / "state")
    return Project(dest)


def _describe(model) -> str:
    if isinstance(model, dict):
        return f"{model.get('model') or 'local'} @ {model.get('url', '?')}"
    return str(model)


def run(home: Path, moves: list[str], setups: list[str], *, reset: bool = False) -> str:
    results = []
    for spec in setups:
        name, models = _setup(spec)
        print(f"── {name}: planner {_describe(models['planner'])} · narrator {_describe(models['narrator'])}")
        with tempfile.TemporaryDirectory(prefix="kp-bench-") as tmp:
            project = _copy(home, Path(tmp) / "home", reset)
            project.config["models"] = models
            turns = []
            script = ([None] if not project.turns() else []) + list(moves)
            for move in script:
                started = time.monotonic()
                try:
                    out = turn_mod.run(project, move)
                except (ModelError, ValueError, KeyError) as exc:
                    turns.append({"move": move, "ok": False, "error": str(exc)[:300]})
                    print(f"   ✗ {str(exc)[:120]}")
                    continue
                last = project.turns()[-1] if project.turns() else {}
                meta = ((last.get("extra") or {}).get("player_mode") or {})
                caught = [label for key, label in CHECKS.items() if any(key in line for line in out.debug)]
                turns.append({
                    "move": move, "ok": True, "prose": out.prose, "prompt": out.prompt,
                    "seconds": round(time.monotonic() - started, 1),
                    "planner_s": (meta.get("seconds") or {}).get("planner"),
                    "narrator_s": (meta.get("seconds") or {}).get("narrator"),
                    "words": meta.get("words"), "cost": out.cost,
                    "tokens": meta.get("tokens"), "caught": caught,
                })
                print(f"   ✓ {turns[-1]['seconds']}s · {meta.get('words')} words{' · ' + ', '.join(caught) if caught else ''}")
        results.append({"name": name, "models": models, "turns": turns})
    return _report(home, moves, results, reset)


def _report(home: Path, moves: list[str], results: list[dict], reset: bool) -> str:
    lines = [f"# kp bench — {home.name}", "",
             f"{datetime.now():%Y-%m-%d %H:%M} · {len(moves)} scripted move(s)"
             f" · from {'state_init/' if reset else 'the current state'}", "",
             "| setup | planner | narrator | turns ok | s/turn (median) | planner s | narrator s | words | checks caught | cost |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        ok = [t for t in r["turns"] if t["ok"]]

        def med(key):
            vals = [t[key] for t in ok if isinstance(t.get(key), (int, float))]
            return f"{statistics.median(vals):.1f}" if vals else "–"
        caught = sum(len(t["caught"]) for t in ok)
        lines.append(f"| {r['name']} | {_describe(r['models']['planner'])} | {_describe(r['models']['narrator'])} | "
                     f"{len(ok)}/{len(r['turns'])} | {med('seconds')} | {med('planner_s')} | {med('narrator_s')} | "
                     f"{med('words')} | {caught} | ${sum(t.get('cost') or 0 for t in ok):.3f} |")
    longest = max((len(r["turns"]) for r in results), default=0)
    for i in range(longest):
        move = next((r["turns"][i]["move"] for r in results if i < len(r["turns"])), None)
        lines += ["", f"## Turn {i + 1}", "", f"**Move:** {move if move is not None else '(scene start)'}"]
        for r in results:
            if i >= len(r["turns"]):
                continue
            t = r["turns"][i]
            lines += ["", f"### {r['name']}"]
            if not t["ok"]:
                lines.append(f"✗ failed: {t['error']}")
                continue
            lines.append(f"*{t['seconds']}s · {t['words']} words"
                         + (f" · {', '.join(t['caught'])}" if t["caught"] else "") + "*")
            lines += [""] + [f"> {line}" if line else ">" for line in t["prose"].splitlines()]
    return "\n".join(lines) + "\n"
