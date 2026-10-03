# ProseWright

**Play a story. Novelize it. Edit it.**

ProseWright is a workbench for co-writing fiction with language models, built for people who love roleplay and would like something to show for it afterwards. You play a story into being one move at a time, the editor turns the played log into a novel's prose, and then you revise it: every model change is proposed as a diff you accept or reject.

It runs on your own machine, on your own model credentials: your Claude subscription through Claude Code, an Anthropic API key, a local model, or a mix of these.

**New here? [The tutorial](docs/TUTORIAL.md) walks one scene from the first move of a roleplay to an exported chapter, with screenshots.**

---

## Why this exists

Long roleplays tend to drift. Each reply invents a little more (a new face, a strange noise, a meaningful glance) and nothing is ever paid off. Keeping a story on course in a chat frontend means flipping switches by hand: lorebook entries on and off, author's notes rewritten, characters muted. And when the story is good, it stays a chat log.

ProseWright treats roleplay as **co-authoring**:

- **Player mode** puts a *director* between you and the prose. It knows the story's planned scenes and hidden truths, decides what the world does each turn, and paces each scene. A *narrator* then writes close-third prose that can only see what your character perceives. You play one character; the story keeps its shape.
- **The editor** reads the played log as a manuscript in the making: it splits it into scenes, knows who speaks in each turn, files the cast, drafts chapters in a book voice you choose, and helps you revise, from restyling a line to proofreading a chapter.
- **Exports** produce a PDF, plain text, or AO3-ready HTML, a chapter per scene unless you lock a chapter plan.

If you use SillyTavern, most of this will feel familiar: character cards, lorebooks, swipes. Your SillyTavern chats can be opened as working logs, and an optional extension brings the edit tools into SillyTavern itself.

---

## What's in the box

| Part | What it is | Where |
|---|---|---|
| **The editor** | A local web app (and command line) for reading, playing, novelizing and revising a project | `story_editor/`, `app/` |
| **Player mode** | The roleplay engine (`kp`): director + narrator + ledgers, as a terminal REPL, a Claude Code plugin, and the editor's **play** view | `plugins/player-mode/` |
| **SillyTavern extension** | Optional: restyle / retune / inject and voice-label review from inside SillyTavern | `extensions/sillytavern/` |
| **Examples** | *Lantern Quay*, a three-scene story about two sisters, a late ship and a forged seal, as a project and as a play folder | `examples/` |

---

## Requirements

| Needed for | What |
|---|---|
| Everything | **Python 3.10+** on Linux or macOS (Windows is untested). [conda](https://docs.conda.io) or plain `pip` |
| The search index | Installed with the requirements: `sentence-transformers` (pulls PyTorch; CPU is fine) and `sqlite-vec`. The first index build downloads a small embedding model (`BAAI/bge-small-en-v1.5`) |
| Play turns | One of: **Claude Code** signed in to your account · an **Anthropic API key** (`pip install anthropic`) · a **local model server** with an OpenAI-compatible API |
| Editor operations (restyle, novelize, proofread…) | An **OpenAI-compatible endpoint**: a local server (text-generation-webui, llama.cpp, vLLM, LM Studio, Ollama) or a hosted one such as [OpenRouter](https://openrouter.ai) (which also serves Claude models) |
| PDF export | Google Chrome or Chromium on the `PATH` |
| Rebuilding the GUI | Node.js. Not needed to *run* it: the built app ships in `app/dist` |
| Optional | [rclone](https://rclone.org) for syncing a project between machines; SillyTavern for the extension |

---

## Quick start

```bash
git clone <this repo> prosewright && cd prosewright
conda env create -f environment.yml && conda activate prosewright    # or: pip install -r requirements.txt
python -m story_editor serve
```

Open <http://127.0.0.1:8765/app>. With nothing else configured, the editor opens the bundled example, *Lantern Quay*: read it in the **log** layer, look at the cast in the **char** tab, novelize a scene in the **novel** layer (this needs a model; see [Models and credentials](#models-and-credentials)).

### Play your first scene

The fastest way to see Player mode is in the terminal, on a copy of the example play folder:

```bash
ln -sf "$PWD/plugins/player-mode/bin/kp" ~/.local/bin/kp     # the launcher; stdlib Python, no packages
cp -r examples/lantern-quay-play ~/stories/lantern-quay-play
kp use ~/stories/lantern-quay-play
kp play
```

Type what Wren does; Enter on an empty line accepts a scene cut; `/renarrate` tells the last reply again, `/replan` plays it again from scratch, `/undo`, `/as ilse`, `/quit`.

To play **inside the editor** instead, give a project a play folder (see [Playing from the editor](#playing-from-the-editor)), or follow [the tutorial](docs/TUTORIAL.md), which does exactly that.

---

## Concepts

- **Project**: one story, one folder. It holds the working log, the cast's bibles, lorebooks and the editor's state. Projects never share files; you can keep several, switch between them, and save any of them to a single file.
- **Working log**: the story as turns, in SillyTavern's `.jsonl` chat format. Lines like `[ 🕰️ Time 7:40 PM | 🗓️ Thursday, October 4, 1792 | 📍 Lantern Quay ]` at the top of a turn mark scenes; the editor splits the log on them.
- **Layers**: the **log** (the played turns) and the **novel** (prose drafted from the log, chapter by chapter, in a chosen person, tense and focal character).
- **Play folder**: the world a Player-mode story is played in: scene cards (the spine), hidden truths, the cast, and the engine's ledgers. It is separate from the project; every turn played there is copied into the project's log.

---

## Your inputs

### A project folder

```
my-story/
  project.json             the manifest (below)
  canon/
    characters.json        character bibles: names and aliases, role, voice rules, relationships, pronouns
    story_primer.txt       a paragraph or two of premise every model call sees
    dossiers/*.json        optional: dated facts per character, pinned to passages
  worlds/*.json            optional: lorebooks, in SillyTavern's world-info format
  mini_spine.json          optional: a beat plan for Author mode
  workspace/
    my_story.jsonl         the working log
```

`project.json`:

```json
{
  "schema": "story-editor/project@1",
  "id": "my-story",
  "title": "My Story",
  "log": "workspace/my_story.jsonl",
  "integrations": {
    "sillytavern": {"chat": null, "characters_dir": null},
    "lorebooks": null,
    "drive": {"remote": null},
    "player_mode": {"home": null}
  }
}
```

Every integration is off unless named: `sillytavern.chat` (a SillyTavern chat file to push edits back to), `characters_dir` (where character-card portraits live), `lorebooks` (a list of paths; `null` means every `worlds/*.json`), `drive.remote` (an rclone remote), `player_mode.home` (a play folder). Full reference: [docs/PROJECTS.md](docs/PROJECTS.md).

**Where the log comes from:**

- **Player mode**: played turns arrive by themselves (see below).
- **A SillyTavern chat**: copy the chat's `.jsonl` (from SillyTavern's `data/<user>/chats/`) into `workspace/` and name it in `log`. To push edits back into SillyTavern, also name the original file in `sillytavern.chat`.
- **Plain prose**: write a `.story` file and convert it with `python -m story_editor import my-story.story -o workspace/my_story.jsonl` (the format is described in `story_editor/importer.py`).

The easiest start is to copy `examples/lantern-quay/` and replace its contents.

### A play folder (Player mode)

```
my-play/
  play.json                name, models, and how Claude is reached
  world/
    contract.md            the narrator's standing brief: point of view, tense, style
    canon.md               what the world openly knows
    truths.md              hidden facts only the director sees
    rules.md               world mechanics (director only)
    secrets.json           words that would give the truths away, and who knows them
    spine.json             scene cards: setting, question, a ladder of escalating beats, minimum dwell, exit
    cast/*.json            characters: what others see ("public") and the full card
  state_init/              the starting ledgers (scene, POV, party, knowledge…)
```

`examples/lantern-quay-play/` is a complete one to copy. How the director and narrator behave, and how to write a spine: [docs/PLAYER_MODE.md](docs/PLAYER_MODE.md).

---

## Models and credentials

There are **two separate settings**, because the two halves of the system talk to models differently:

| | Player mode (play turns) | The editor's operations |
|---|---|---|
| What | Director (planner) + narrator, per turn | Restyle, retune, inject, weed, proofread, novelize, spine and canon checks, Ask |
| Set in | the play folder's `play.json` | environment variables, or the **model** control in the editor's status bar |
| Reaches | Claude (via Claude Code or the Anthropic API), or a local server, per role | any OpenAI-compatible endpoint |

Nothing in this repository is tied to any account: every call runs on the credentials of the person running it.

### Player mode

**Default:** with no `models` setting, the planner and narrator run on **Claude Sonnet 5.5**, and Claude Haiku 4.5 plays your character in `kp autoplay` test runs. A turn costs about 4–6¢ at API list prices and takes 15–20 seconds.

**How Claude is reached** (the `"claude"` key in `play.json`, or the `PLAYER_MODE_CLAUDE` environment variable):

| Route | Setup | Billing |
|---|---|---|
| `"code"`: **Claude Code** | Install Claude Code and sign in once: `claude auth login` | Your Claude subscription (plan usage), or the API key Claude Code is signed in with |
| `"api"`: **Anthropic API** | `pip install anthropic`, then `export ANTHROPIC_API_KEY=...` (or `ant auth login`) | Your API account, at list prices (shown per turn) |
| `"auto"` *(default)* | Claude Code when `claude` is installed, otherwise the API | |

Through the API route, the planner uses native structured output, the long system prompt is cached between turns, and Sonnet 5.5 requests use Anthropic's server-side refusal fallback.

**Choosing models per role:**

```json
{
  "name": "My Story",
  "claude": "auto",
  "models": {
    "planner": "claude-sonnet-5-5",  "planner_effort": "low",
    "narrator": "claude-sonnet-5-5", "narrator_effort": "low",
    "player": "claude-haiku-4-5"
  }
}
```

**A local model** for any role: point it at an OpenAI-compatible server.

```json
"narrator": {"url": "http://127.0.0.1:5000/v1", "model": "gemma-4-31b"}
```

| Option | Meaning |
|---|---|
| `url` | The server's `/v1` base (text-generation-webui with `--api`, llama.cpp's `llama-server`, vLLM, LM Studio, Ollama) |
| `model` | The model name the server expects (many local servers ignore it) |
| `json` | Planner only: `"grammar"` (default; a GBNF grammar generated from the plan's schema, enforced by text-generation-webui and llama.cpp, so the plan is valid by construction), `"schema"` (OpenAI-style `response_format`, for llama.cpp, vLLM, LM Studio), or `"prompt"` (instructions only) |
| `thinking` | `false` by default: thinking models (Gemma 4, Qwen 3…) are asked not to think aloud, and any reasoning left in a reply is stripped |
| `slot` | Pin the role to a llama.cpp server slot, so its prompt cache stays warm when two roles share one server |
| `temperature`, `max_tokens`, `api_key_env`, `extra` | The usual sampling and auth settings; `extra` passes any other request fields |

**Hybrid** (recommended if you want to go local): the planner has the harder job (a dense rulebook and strict JSON), the narrator turns a direction into prose. Keep the planner on Claude and run the narrator locally:

```json
"models": {
  "planner": "claude-sonnet-5-5", "planner_effort": "low",
  "narrator": {"url": "http://127.0.0.1:5000/v1", "model": "gemma-4-31b"}
}
```

With two GPUs, run one server per role (planner and narrator on different ports) or one llama.cpp server with a `slot` per role. The planner and narrator run one after the other within a turn (the narrator writes from the planner's direction), so separate models buy warm caches and the right model per job, not parallel turns.

**Compare setups on your own story** before committing to one:

```bash
kp bench moves.txt --setup claude --setup hybrid=hybrid.json --setup local=local.json --out report.md
```

It plays the same scripted moves (separated by lines of `---`) through each setup on throwaway copies of the play folder and reports time per turn, words, what the engine's checks caught, cost, and every turn's prose side by side.

### The editor's operations

Set before starting the server (or switch later from the status bar's **model** control):

```bash
# a local server (the default): text-generation-webui, llama.cpp, vLLM… on 127.0.0.1:5000/v1
export STORY_EDITOR_MODEL_PROVIDER=local
export STORY_EDITOR_MODEL_PORT=5000            # or STORY_EDITOR_MODEL_URL=http://host:port/v1

# or a hosted OpenAI-compatible service, e.g. OpenRouter (which serves Claude models too)
export STORY_EDITOR_MODEL_PROVIDER=openrouter
export OPENROUTER_API_KEY=sk-or-...            # or STORY_EDITOR_MODEL_API_KEY
export STORY_EDITOR_MODEL_NAME=anthropic/...   # the provider's model id
```

---

## Using the editor

`python -m story_editor serve` starts the server on <http://127.0.0.1:8765/app> (`--port` to change it).

**The page** (center) shows one layer at a time:

- **play**: appears when the project has a play folder. The transcript, a move box (Enter plays a turn), the scene's status (rung, dwell, who's on stage, the open mystery, cost so far and today), *cut to the next scene* / *stay* when a scene can end, a POV switch, undo, and under the last reply **‹ 1/3 ›  retell  re-plan**: retell writes the same events again (every telling is kept, SillyTavern-swipe style), re-plan plays the turn again from scratch.
- **log**: the turns. Select a span to label who spoke, or **propose** a rewrite (restyle, retune, weed, copyedit, inject, remove). Every proposal appears in the **fork** panel as a diff you accept or reject, turn by turn; nothing is written until you accept, and accepted changes keep a backup and an undoable history.
- **novel**: novelize a scene into prose in the book's voice (person, tense, focal character), then edit it in place or send it through the prose review queue. Export chapters as **PDF**, **text**, or **AO3** HTML.

**The side panels:** **spine** (the story's derived structure and how it lines up with your plan), **queue** (prose review), **ask** (questions about the story, answered from the log); **fork** (the pending proposal), **char** (the cast: everyone with a bible, where each is voiced, is the point of view, or is named, with jumps to every scene).

**The status bar:** the **project** chip (switch projects, save a project to a `.sebundle`, load one as a new project), the **model** control, **drive** (rclone sync between machines), **code** (update from the repository's git remote).

### Playing from the editor

Name the play folder in the project's manifest:

```json
"integrations": { "player_mode": { "home": "/home/you/stories/my-play" } }
```

and a **play** button appears next to log and novel. After every turn the new messages are added to the project's working log, each with a stable id, a scene header from its scene card, and a voice label naming who it involves; characters the engine introduces are filed as lightweight bibles and show up in the **char** tab. Messages already in the log are never rewritten, so you can edit and novelize earlier scenes while you keep playing; undo and re-plan take a turn back out of the log unless you edited it there. Use one front end at a time (the editor or `kp` in a terminal), not both on the same play folder at once.

### Projects

```bash
python -m story_editor project add ~/stories/my-story      # register a folder
python -m story_editor project list                         # * marks the active one
python -m story_editor project switch my-story              # opened when the server starts
python -m story_editor project save my-story --out ~/backups
python -m story_editor project load ~/backups/my-story-20261003-101500Z.sebundle
```

A bundle is one checksummed file with everything the project needs, including files it reaches outside its folder. Loading always creates a *new* project, never overwrites one, and the copy is isolated from the original's SillyTavern chat and Drive folder. Details: [docs/PROJECTS.md](docs/PROJECTS.md).

### The command line

Everything the GUI does is also on the command line: `python -m story_editor --help`. A few examples:

```bash
python -m story_editor structure scenes                         # the scenes the log splits into
python -m story_editor restyle --scene 1 --note "drier, more clipped"
python -m story_editor edits show                               # review the proposed diff
python -m story_editor edits commit
python -m story_editor proofread
```

Player mode in the terminal: `kp begin | turn "<move>" | play | autoplay N | renarrate | swipe N | replan | undo | as CHAR | status | bench`. Inside Claude Code, install the plugin for `/player-mode:play`, `/player-mode:autoplay` and `/player-mode:status`:

```
/plugin marketplace add <path to prosewright>/plugins
/plugin install player-mode@prosewright-plugins
```

### SillyTavern extension

`extensions/sillytavern/` adds a ProseWright drawer to SillyTavern: restyle, retune and inject on the open chat, and voice-label review, backed by the same engine. See [its README](extensions/sillytavern/README.md).

---

## Your data

Everything lives in folders on your disk: projects, play folders, backups, bundles. The server listens on `127.0.0.1` (this machine only) unless you pass `--host`. Text leaves your machine only in the model calls you configure (Anthropic through Claude Code or the API, a hosted endpoint such as OpenRouter, or nowhere if every role is local), and in Drive sync if you set up an rclone remote.

Accepted edits keep timestamped backups of the log (`workspace/backups/`) and an undoable history; play folders keep one git commit per turn.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| *"Your organization has disabled Claude subscription access for Claude Code"* | Usually a lapsed Claude subscription: renew it, then `claude auth login` again. Or switch play to the API route (`pip install anthropic`, set `ANTHROPIC_API_KEY`, `"claude": "api"`) |
| *"Claude Code (`claude`) is not installed"* | Install Claude Code and sign in, or use the API route |
| *"the Anthropic API route needs the SDK"* | `pip install anthropic` in the environment that runs the editor or `kp` |
| *"local model … is not reachable"* | Start the server (text-generation-webui needs `--api`) and check the `url` port |
| *"did not return a usable plan"* | The local planner broke the JSON twice: use `"json": "grammar"` (or `"schema"`), a stronger planner model, or keep the planner on Claude |
| Prose starts with a model's reasoning | Leave `"thinking": false` on the role (the default) |
| The **play** button doesn't appear | The project's `project.json` has no `integrations.player_mode.home`, or the folder doesn't exist; restart the server after editing the manifest |
| *"Engine offline"* in the GUI or the SillyTavern extension | Start it: `python -m story_editor serve` |
| PDF export fails | Install Google Chrome or Chromium |
| The first index build is slow | It downloads the embedding model once; later builds are quick |

---

## Development

```bash
pytest                              # the unit suite; runs on scratch copies of the example, never your files
bash tests/phase6/run_tests.sh      # API integration suite on an isolated home
cd app && npm install && npm run build   # rebuild the GUI after changing app/src
```

`tests/test_public_hygiene.py` keeps private story material out of this repository by checking every committed file against a salted-hash deny list.

---

## License

[MIT](LICENSE). The bundled typefaces under `app/public/fonts/` carry their own licenses (SIL Open Font License and Apache 2.0), included next to each font.
