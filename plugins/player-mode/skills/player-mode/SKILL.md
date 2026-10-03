---
name: player-mode
description: Operate, debug and tune the player-mode roleplay engine (kp). Use when the user wants to play, dry-run, autoplay, inspect ledgers, or tune the planner/narrator of a player-mode project home.
---

# Player mode: operating and tuning

The engine is `kp` (Python, stdlib only) in this plugin's `engine/`, launched by `bin/kp`. It plays a story from a **play folder** (e.g. `examples/lantern-quay-play`); `kp use PATH` remembers it. Design notes: `docs/PLAYER_MODE.md` in ProseWright repo.

## How a turn works
1. **Planner** (headless `claude -p`, structured JSON): sees everything, including `world/truths.md`. It decides one move, the rung, the stage (max 3), entrances, events, exit and cut, and writes a POV-safe `direction`.
2. **Narrator** (headless `claude -p`): sees only the contract, the public canon, the POV character's card and knowledge, the stage's public descriptions, the recent turns and the direction. It writes close-third prose.
3. **Code checks:** secret-term leaks (retry once), four-word refrains (added to the avoid-list), the dwell guard on exits, the stage cap, habit names.
4. **Ledgers:** `state/` (state, knowledge, cast, threads, motifs), `log.jsonl` (SillyTavern-compatible), `status.json` (for ProseWright's GUI), and a git commit per turn (`kp undo`).

## Rules for you (Claude Code) while operating it
- You are the operator, not the narrator. Never write or rewrite story prose yourself during play; show the engine's output verbatim.
- Don't quote `world/truths.md` or ledger secrets to the user during play unless they ask as the author.
- Tuning edits go in: `engine/kp/prompts.py` (planner rules, narrator task, player agent), the project's `world/contract.md`, `world/rules.md`, `world/spine.json` (scene cards: ladder, dwell_min, exit), and `world/secrets.json` (leak terms).
- After a prompt edit, the first turn re-creates the cache (expect higher cost once).
- To re-test a turn: `kp undo`, then replay the same move.

## Commands
`kp begin` · `kp turn "<move>"` · `kp play` (REPL: /as, /undo, /threads, /debug, /quit) · `kp autoplay N` · `kp replay FILE` (moves separated by lines of `---`) · `kp status` · `kp as CHAR` · `kp reset --yes` (back to `state_init/`) · add `--debug` for planner lines, tokens and cost.
