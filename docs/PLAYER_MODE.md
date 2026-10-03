# Player mode

Play the story, then novelize it, then edit it. Player mode is the first of those three steps: a roleplay engine that knows where the story is going. You play one character; the engine runs the rest of the world toward a planned spine of scenes, and every turn lands in a log that ProseWright can turn into a novel.

The engine is `kp`, shipped as a Claude Code plugin in `plugins/player-mode/`. See its README for installing and playing; this note explains how it behaves and why.

## Why not a chat frontend

Long roleplays in chat frontends tend to drift. Each reply invents a little more: a new face, a strange noise, a meaningful glance. None of it is ever paid off, and after a while the story is a pile of loose threads. Keeping it on course means flipping switches by hand: lorebook entries on and off, author's notes rewritten, characters muted. Player mode replaces the switches with a director that knows the plan.

## How a turn works

1. **The director** (a planner model call returning structured JSON) reads your move, the current scene card, the ledgers and the hidden truths. It picks exactly one change for the world to make this turn and writes a direction for the narrator.
2. **The narrator** (a second model call) writes the reply. It sees only what your character could perceive: the public canon, your card and knowledge, what the people on stage look like, the recent turns and the direction. It cannot leak a secret it was never shown.
3. **Code checks** run on both: hidden terms in the prose trigger a retry, repeated four-word phrases go onto an avoid-list, the stage is capped, and a scene can't end before its minimum dwell.
4. **Ledgers** record what changed: who knows what, who is on stage, open threads, phrases to avoid. Each turn is a git commit, so `/undo` is exact.

## The rules the director plays by

- **One change per turn.** A consequence, a reveal, an escalation, a choice, a payoff, an entrance, a time skip, a bit of history — one, never several. Atmosphere rides along; it is never the move.
- **A small stage.** Besides your character and their party, at most three characters of interest can act or carry a hook. Everyone else is the crowd, which reacts as one and carries nothing. To bring someone on, someone leaves.
- **Entrances are introductions.** A new character presents themselves in the fiction: name, affiliation, standing, why they're here, what they want. Names are earned: "a dockhand in a patched coat" gets a name only when you engage with them or the plan needs one.
- **One mystery per scene.** Anything else strange must be explainable from what your character already knows.
- **Events serve the spine; texture is free.** New events must serve the current scene card, and an open thread is paid off before a new one opens.
- **Pacing by ladder.** Each scene card has a ladder of escalation rungs. While you're engaged (talking, asking, pursuing something), the scene holds and deepens; when you idle, it climbs. When the exit condition is met after the minimum dwell, the engine offers a cut to the next scene.
- **You're a co-author.** If your move plays a planned beat early, the director accepts it and fills in its preconditions from what's already on the page.
- **Your character is yours.** The narrator never writes your character's actions, words, thoughts or feelings, only involuntary tells: a flush, a held breath. When a hidden truth reaches your character hard enough to change who they are, the engine stops with a **⟡ REVEAL** prompt and your next move decides how they take it.
- **Switching sides.** `/as NAME` hands you another character in your party; the next reply re-establishes the scene from where they stand.

## Writing a story to play

A play folder holds the world (`world/`) and the starting ledgers (`state_init/`); `examples/lantern-quay-play/` is a complete example. The spine is a list of scene cards:

```json
{
  "id": "harbour_office_night",
  "title": "Night · the Harbour Office",
  "time": "a quarter past eleven at night",
  "location": "the harbour office",
  "stamp": {"clock": "11:15 PM", "date": "Thursday, October 4, 1792", "place": "The Harbour Office"},
  "setting": "What the scene opens on.",
  "function": "What the scene is for in the story.",
  "question": "The scene's dramatic question.",
  "dwell_min": 4,
  "ladder": ["rung 1", "rung 2", "rung 3"],
  "exit": "When the scene is done, and where it cuts to.",
  "texture": "Sensory palette for the narrator."
}
```

`truths.md` holds what only the director knows; `secrets.json` lists the words that would give those truths away, so the code can catch a leak. `contract.md` is the narrator's standing brief: point of view, tense, style.

## From play to book

The easiest way is the editor's **play** view. Name the play folder in your project's `project.json`:

```json
"integrations": { "player_mode": { "home": "/path/to/your/play-folder" } }
```

and a **play** button appears next to log and novel. It shows the transcript, the scene's status (rung, dwell, who is in the air, the open mystery, session cost) and a move box; scene cuts get *cut to the next scene* / *stay* buttons, and you can switch characters or undo. After every turn the new messages are added to the project's working log, each with a stable id, a scene header from its card's `stamp`, and a voice label naming who it involves, and characters the engine introduced are filed as lightweight bibles (the **char** tab lists them). Messages already in the log are never rewritten, so you can edit and novelize while you keep playing; undo takes a turn back out of the log unless you edited it there. Under the last reply, **retell** writes it again from the same plan (same events, new prose, about half a turn's cost; every telling is kept as a swipe and ‹ › switches between them), and **re-plan** plays the turn again from scratch, so other things can happen. The log follows whichever telling you choose.

From the shell, `python tools/import_play_log.py PLAY_FOLDER PROJECT_HOME` does the same sync.

## Cost

Measured on Sonnet 5.5 at low effort: about 4–6¢ per turn at API list prices, and about 15–20 seconds. Calls go through headless Claude Code (`claude -p`) on your own login, so on a Claude subscription a turn uses plan allowance rather than money.
