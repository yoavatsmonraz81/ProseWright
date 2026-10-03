# player-mode (Claude Code plugin)

Play the story: a spine-aware, co-authored roleplay engine. You play one character; a planner directs the world from the story's spine and ledgers, and a narrator writes close-third prose that can only see what your character perceives. Design notes: `../../docs/PLAYER_MODE.md`.

Every model call goes through headless Claude Code (`claude -p`) on your own login, with a replaced system prompt and no tools, so each request carries only what the engine compiled for it.

## Install
```bash
ln -sf "$PWD/bin/kp" ~/.local/bin/kp     # the launcher (stdlib Python 3.10+, no packages)
```
In Claude Code, for the slash commands and the operating skill:
```
/plugin marketplace add <path to prosewright>/plugins
/plugin install player-mode@prosewright-plugins
```
(or start a session with `claude --plugin-dir <path>/plugins/player-mode`).

## Quick start
```bash
cp -r examples/lantern-quay-play ~/stories/lantern-quay   # play a copy, outside the repo
kp use ~/stories/lantern-quay                              # remember it
kp play                                                    # the Zork-style REPL
```
A fresh copy gets its own git history (one commit per turn), which is what `/undo` rolls back. Played in place inside the repository it still works, without undo.

## Play
- **Terminal (recommended):** `kp play`. Type your character's moves; Enter on an empty line accepts a pending cut; `/as ilse` switches the point of view, `/undo`, `/threads`, `/debug`, `/quit`. `/renarrate` (`/rn`) tells the last reply again from the same plan (same events, new prose; every telling is kept, `/swipe N` picks one); `/replan` (`/rp`) plays the last turn again from scratch (other events).
- **Inside Claude Code:** `/player-mode:play <move>`, `/player-mode:autoplay 3`, `/player-mode:status`. The play command has Claude Code echo the engine's output, which spends a little of your main model's usage per turn; the REPL doesn't.

When a hidden truth reaches your character hard enough to change who they are, the engine stops with a **⟡ REVEAL** prompt: the world only shows the onset, and your next move decides how your character takes it.

## Play folder
```
play.json            name and models
world/
  contract.md        the narrator's standing instructions (voice, POV, style)
  canon.md           public world facts the narrator may use
  truths.md          hidden facts only the director sees
  rules.md           world mechanics (director only)
  secrets.json       hidden terms the narrator must never print, and who knows them
  spine.json         scene cards: setting, question, ladder of rungs, minimum dwell, exit
  cast/*.json        character cards (`public`: what others see; `card`: the full card)
state_init/          starting ledgers (state, knowledge, cast, threads, motifs)
```
Once played it also holds `state/`, `log.jsonl` (a SillyTavern-format chat ProseWright can open as a working log, to novelize and edit), and `status.json`.

## Your Claude credentials

The engine never carries anyone's account: every call runs on the credentials of whoever is playing. Two routes reach Claude:

- **Claude Code** (used automatically when `claude` is installed). Calls go through headless Claude Code on your own login: a Claude subscription, or an API key if that's what Claude Code is signed in with. Set up once with `claude auth login`.
- **Anthropic API** (used automatically when Claude Code isn't installed). Calls go straight to the API through the official SDK: `pip install anthropic`, then `export ANTHROPIC_API_KEY=...` (or `ant auth login`). Billed to your API account at list prices; the engine shows the cost per turn.

To pick a route yourself, add `"claude": "code"` or `"claude": "api"` to the play folder's config, or set `PLAYER_MODE_CLAUDE`. If Claude Code reports that your organisation has disabled subscription access, the API route is the way around it.

## Models

By default every call goes to Claude (see above for how): the **planner** and **narrator** on Sonnet 5.5, and Haiku 4.5 for the player agent (`kp autoplay`). Each role can be set in the play folder's config, under `"models"`:

```json
"models": {
  "planner": "claude-sonnet-5-5", "planner_effort": "low",
  "narrator": "claude-sonnet-5-5", "narrator_effort": "low",
  "player": "claude-haiku-4-5"
}
```

A role can also run on a **local model server** with an OpenAI-compatible API (text-generation-webui with `--api`, llama.cpp's `llama-server`, vLLM, LM Studio, Ollama):

```json
"narrator": {"url": "http://127.0.0.1:5000/v1", "model": "gemma-4-31b"},
"planner":  {"url": "http://127.0.0.1:5000/v1", "model": "gemma-4-31b", "json": "grammar"}
```

- `json` (planner only): `grammar` sends a GBNF grammar built from the plan's schema, so the reply is valid JSON by construction (text-generation-webui, llama.cpp); `schema` sends an OpenAI `response_format`; `prompt` relies on instructions. Every mode parses leniently and retries once.
- `thinking` (default `false`) keeps thinking models (Gemma 4, Qwen 3) from putting their reasoning in the reply; any that slips through is stripped.
- `slot` pins a role to a llama.cpp server slot, keeping its prompt cache warm when two roles share one server; two servers (one per GPU) need no slot.
- Also `temperature`, `max_tokens`, `api_key_env` and `extra` (any other request fields).

The planner has the harder job (a dense rulebook and strict JSON); the narrator turns a direction into prose. A good first local setup is **Sonnet planning, a local model narrating**.

## Comparing setups

`kp bench MOVES --setup claude --setup hybrid=hybrid.json --setup local=local.json` plays the same scripted moves (separated by lines of `---`) through each setup, each on a throwaway copy of the play folder, and writes a report: turns that worked, seconds per turn and per role, words, what the engine's checks caught, cost, and every turn's prose side by side. A setup file holds a `"models"` object like the one above.

## Cost (measured, Sonnet 5.5 at low effort)
About 4–6¢ per turn at list price (planner ~12k in / ~1k out, narrator ~8k in / ~0.5k out, 60–65% of input served from cache), about 17–20 seconds per turn. On a subscription that's usage, not money.
