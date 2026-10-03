---
description: Play one move in the current player-mode story (answer "y"/"n" to a pending cut)
argument-hint: "<your character's move>"
allowed-tools: Bash(kp:*)
---

Run exactly this command, passing the player's move as a single quoted argument:

`kp turn "$ARGUMENTS"`

Then reply with the command's output **verbatim**: the narration, the status line and any ⟡ prompt, nothing added, nothing summarised, no commentary. You are not the narrator; the engine is. Do not read or reveal anything from the project's `world/truths.md`.

If the arguments are empty, run `kp begin` instead (it establishes the current scene).
