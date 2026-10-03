---
description: Let the player agent play N moves (a dry run for tuning), with engine lines shown
argument-hint: "[number of moves, default 3]"
allowed-tools: Bash(kp:*)
---

Run `kp --debug autoplay ${ARGUMENTS:-3}` (allow it up to 10 minutes; each move takes about 20 seconds).

Show the output to the user verbatim. Afterwards, add a short tuning note under a `Tuning` heading covering only what the output shows:
- replies over 350 words;
- the narrator writing the POV character's thoughts or feelings;
- new hooks or characters the plan didn't ask for;
- the same background element or phrase returning;
- leaks flagged by the engine;
- ladder behaviour (climbs on idle, holds on engagement);
- cost per turn.
