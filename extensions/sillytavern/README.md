# ProseWright — SillyTavern extension

Thin client for the ProseWright engine. Propose transforms from inside chat, review in the side drawer, commit back to the live log.

## Prerequisites

1. ProseWright on disk (this repository).
2. Local LLM server running (same as CLI — default `http://127.0.0.1:5000/v1`).

## Install

```bash
# 1. Start the engine API (leave running)
cd "/path/to/prosewright"
python3 -m story_editor serve

# 2. Link the extension into SillyTavern (one-time)
ln -sf "/path/to/prosewright/extensions/sillytavern" \
  "/path/to/SillyTavern/public/scripts/extensions/third-party/StoryEditor"
```

3. In SillyTavern: **Extensions** → enable **ProseWright**.
4. Open **Extensions** settings → **ProseWright** drawer: set API URL (and optional log path).
5. Reload the page.

## Slash commands

| Command | Action |
|---------|--------|
| `/editnote …` | Restyle (default: last message) |
| `/retunenote mode=tighten …` | Retune length/density (`expand` also valid) |
| `/injectnote speaker=Holmes …` | Insert new prose after a message |
| `/attribution` | Open voice-label review queue (card ≠ character voiced) |

Named span args work on restyle/retune: `speaker=`, `from=`, `to=`.

## Drawer

Open via the **ProseWright** tab on the right edge of the window, or slash commands (`/editnote`, `/attribution`, …).

Four tabs — each has a short explanation at the top:

| Tab | Purpose |
|-----|---------|
| **Edit** | Propose AI rewrites → review diff in Results → **Commit** (or Discard). Undo restores last backup. |
| **Label** | Voice attribution — presets (Focus / Player turns / …), card, confidence range, **Review queue** |
| **Audit** | Sweep (after a commit), Proofread, Canon check on pending edits |
| **History** | Past commits / undos — Show diff reloads an entry |

**Results** (bottom panel) — shared output area for diffs, label queue, sweep reports, history.

## Configuration

| Setting | Default |
|---------|---------|
| API URL | `http://127.0.0.1:8765` |
| Log path | (empty = server default from `STORY_EDITOR_LOG`) |

Server env when starting the API:

```bash
STORY_EDITOR_HOME=/path/to/your/project python3 -m story_editor serve
```

## How sync works

Before each propose, the extension pushes `getContext().chat` to `POST /sync/import`. After **Commit**, it pulls `GET /sync/export` and replaces the in-memory chat, then `saveChatConditional()`.

A prepend committed in the web editor is already on the working log. There is nothing left to Commit. Use **Load from engine** (or Commit with no pending proposal) to pull it into the open chat. Do not Propose first — a shorter chat would overwrite the new first turn; the engine now refuses that import.

`python3 -m story_editor sync push` writes the ST chat *file*. If the chat is open, reload it without saving, or use **Load from engine** instead.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| "Engine offline" | Run `python3 -m story_editor serve` |
| No changes proposed | Target may already be in-character; sharpen the note |
| Sweep: no context | Commit an edit first (writes `last_sweep_context.json`) |
