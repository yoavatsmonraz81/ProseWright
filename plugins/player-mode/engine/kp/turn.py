"""One turn: planner call → POV-scoped narrator call → code checks → ledgers → log."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import backend, prompts
from .store import Project, write_json

RECENT_TURNS = 6
SUMMARIES_KEPT = 4
MOTIFS_KEPT = 14
HABIT_NAMES = ["Maelis", "Elara", "Seraphina", "Lyria", "Kael", "Thorne", "Vex", "Nyx", "Aria"]
YES = {"y", "yes", ""}


@dataclass
class TurnOutput:
    prose: str = ""
    status: str = ""
    prompt: str = ""  # a question for the player (cut, reveal)
    debug: list[str] = field(default_factory=list)
    cost: float = 0.0


# --- helpers --------------------------------------------------------------------
def _name(project: Project, char_id: str) -> str:
    return project.cast_card(char_id).get("name", char_id)


def _words(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", text))


# The scene header written for ProseWright (see _scene_header); models never see it.
_HEADER = re.compile(r"^\s*\[[^\]\n]*(?:🕰️|🗓️|📍)[^\]\n]*\]\s*")


def _fmt_turns(rows: list[dict]) -> str:
    out = []
    for r in rows:
        kind = (r.get("extra") or {}).get("player_mode", {}).get("kind", "")
        label = "SUMMARY" if kind == "summary" else ("PLAYER" if r.get("is_user") else "NARRATOR")
        out.append(f"[{label} · {r.get('name')}]\n{_HEADER.sub('', r.get('mes', '')).strip()}")
    return "\n\n".join(out) or "(nothing yet)"


def _scene_rows(project: Project, scene_id: str) -> list[dict]:
    return [r for r in project.turns()
            if (r.get("extra") or {}).get("player_mode", {}).get("scene") == scene_id]


def _public_card(card: dict) -> dict:
    """The parts of a scene card the narrator may see."""
    return {k: card.get(k) for k in ("title", "setting", "question", "texture") if card.get(k)}


# --- planner --------------------------------------------------------------------
def _planner_system(project: Project) -> str:
    return "\n\n".join([
        prompts.PLANNER_RULES,
        "# WORLD RULES\n" + project.world_text("rules.md"),
        "# THE TRUTH (director only)\n" + project.world_text("truths.md"),
        "# CANON (public)\n" + project.world_text("canon.md"),
    ])


def _is_idle(move: str | None) -> bool:
    """A move with no speech, no question and little action: the scene should climb."""
    if not move or move.startswith("("):
        return False
    spoken = re.search(r"[\"“”]", move) is not None
    return not spoken and "?" not in move and _words(move) <= 18


def _planner_prompt(project: Project, state: dict, move: str | None) -> str:
    card = project.scene_card(state["scene"])
    cast = project.load("cast")
    knowledge = project.load("knowledge")
    threads = [t for t in project.load("threads") if t.get("status") == "open"]
    rows = _scene_rows(project, state["scene"])[-RECENT_TURNS:]
    pov = state["pov"]
    if move is None:
        move_text = "(scene start: establish the scene; no player move yet)"
    elif state.get("pov_switched"):
        move_text = f"(the player has switched POV to {_name(project, pov)}; re-establish from their position) {move}"
    else:
        move_text = move
    parts = {
        "SCENE CARD": card,
        "STATE": {
            "pov": pov, "party": state.get("party", []), "rung": state.get("rung", 1),
            "player_moves_in_scene": state.get("moves", 0), "dwell_min": card.get("dwell_min", 0),
            "player_idle": _is_idle(move),
            "stage": state.get("stage", []), "mystery": state.get("mystery", ""),
            "time": state.get("time", ""), "location": state.get("location", ""),
        },
        "CAST LEDGER": cast,
        "OPEN THREADS": threads,
        "KNOWLEDGE LEDGER": knowledge,
        "EARLIER SCENES": state.get("summaries", [])[-SUMMARIES_KEPT:],
        "RECENTLY USED (avoid)": project.load("motifs").get("avoid", []),
        "BANNED HABIT NAMES": HABIT_NAMES,
    }
    body = "\n\n".join(f"## {k}\n{json.dumps(v, ensure_ascii=False, indent=1)}" for k, v in parts.items())
    return (f"{body}\n\n## THIS SCENE SO FAR\n{_fmt_turns(rows)}\n\n"
            f"## THE PLAYER'S MOVE ({_name(project, pov)})\n{move_text}\n\n"
            "Decide this turn. Return the JSON plan.")


# --- narrator -------------------------------------------------------------------
def _narrator_system(project: Project, pov: str) -> str:
    card = project.cast_card(pov)
    return "\n\n".join([
        project.world_text("contract.md"),
        "# CANON (what the world openly knows)\n" + project.world_text("canon.md"),
        f"# THE POV CHARACTER: {card.get('name', pov)}\n{card.get('card', '')}",
    ])


def _narrator_prompt(project: Project, state: dict, move: str | None, plan: dict, extra_must_not: list[str],
                     *, drop_last: bool = False) -> str:
    pov = state["pov"]
    pov_name = _name(project, pov)
    card = project.scene_card(state["scene"])
    cast = project.load("cast").get("npcs", {})
    stage_desc = []
    for who in plan.get("stage_after", []):
        cast_id = project.find_cast(who)
        entry = cast.get(who) or {}
        public = project.cast_card(cast_id).get("public") if cast_id else entry.get("seen_as")
        stage_desc.append(f"- {who}: {public or '(as described on the page so far)'}")
    party = [f"- {_name(project, p)}: {project.cast_card(p).get('public', '')}" for p in state.get("party", [])]
    pov_public = project.cast_card(pov).get("public", "")
    knows = project.load("knowledge").get(pov, {})
    rows = _scene_rows(project, state["scene"])
    if drop_last and rows:
        rows = rows[:-1]  # re-narrating: the reply being replaced is not "so far"
    rows = rows[-RECENT_TURNS:]
    must_not = list(plan.get("must_not", [])) + extra_must_not
    sections = [
        f"## SCENE\n{json.dumps(_public_card(card), ensure_ascii=False)}\nTime: {state.get('time', '')} · Place: {state.get('location', '')}",
        f"## {pov_name.upper()} (as others see them)\n{pov_public}",
        "## WITH HER (keep their appearance exact)\n" + ("\n".join(party) or "(alone)"),
        "## ON STAGE\n" + ("\n".join(stage_desc) or "(no one of interest; the crowd only)"),
        f"## WHAT {pov_name.upper()} KNOWS\n" + "\n".join(f"- {f}" for f in knows.get("facts", [])),
        "## EARLIER\n" + ("\n".join(state.get("summaries", [])[-SUMMARIES_KEPT:]) or "(this is the first scene)"),
        "## THIS SCENE SO FAR\n" + _fmt_turns(rows),
        "## RECENTLY USED IMAGES AND PHRASES (do not reuse)\n" + ", ".join(project.load("motifs").get("avoid", [])),
        f"## THE PLAYER'S MOVE\n{move or '(scene start)'}",
        "## DIRECTION\n" + plan["direction"],
        "## MUST NOT\n" + ("\n".join(f"- {m}" for m in must_not) or "- (nothing extra)"),
        prompts.NARRATOR_TASK.format(pov=pov_name, words=plan.get("target_words", 220)),
    ]
    return "\n\n".join(sections)


# --- checks (code, not model) -----------------------------------------------------
def _leaks(project: Project, pov: str, prose: str) -> list[str]:
    known = set(project.load("knowledge").get(pov, {}).get("secrets", []))
    hits = []
    for secret in project.secrets():
        if pov in secret.get("known_by", []) or secret["id"] in known:
            continue
        for term in secret.get("terms", []):
            if re.search(rf"\b{re.escape(term)}\b", prose, re.IGNORECASE):
                hits.append(f"{term} ({secret['id']})")
    return hits


def _refrains(project: Project, prose: str) -> list[str]:
    """Four-word phrases this reply shares with the last narrator replies."""
    def grams(text: str) -> set[str]:
        w = re.findall(r"[a-z’']+", text.lower())
        return {" ".join(w[i:i + 4]) for i in range(len(w) - 3)}
    prior = [_HEADER.sub("", r["mes"]) for r in project.turns() if not r.get("is_user")][-6:]
    seen = set().union(*(grams(p) for p in prior)) if prior else set()
    stop = {"the", "a", "of", "and", "to", "in", "her", "she", "his", "he", "their", "they", "it", "is", "at", "on", "as", "that"}
    return sorted(g for g in grams(prose) & seen if len(set(g.split()) - stop) >= 2)[:8]


# --- ledgers ---------------------------------------------------------------------
def _apply(project: Project, state: dict, plan: dict) -> None:
    knowledge = project.load("knowledge")
    cast = project.load("cast")
    cast.setdefault("npcs", {})
    cast.setdefault("aliases", {})
    threads = project.load("threads")
    ent = plan.get("entrance", {})
    if ent.get("present") and ent.get("name"):
        cast["npcs"].setdefault(ent["name"], {}).update({
            "affiliation": ent.get("affiliation", ""), "standing": ent.get("standing", ""),
            "wants": ent.get("wants", ""), "why_here": ent.get("why_here", ""),
            "named": bool(ent.get("named")), "status": "stage", "first_scene": state["scene"],
        })
    for ev in plan.get("events", []):
        kind, who, what = ev["type"], ev["who"], ev["what"]
        if kind in ("knows", "suspects"):
            entry = knowledge.setdefault(who, {"facts": [], "secrets": []})
            entry.setdefault("facts", []).append(what if kind == "knows" else f"suspects: {what}")
            if ev.get("secret"):
                entry.setdefault("secrets", []).append(ev["secret"])
        elif kind == "thread_open":
            threads.append({"id": f"t{len(threads) + 1}", "text": what, "owner": who, "status": "open", "opened": state["scene"]})
        elif kind == "thread_close":
            for t in threads:
                if t["status"] == "open" and (t["id"] == what or t["text"][:40].lower() in what.lower() or what.lower() in t["text"].lower()):
                    t["status"] = "closed"
        elif kind == "promote":
            cast["npcs"].setdefault(who, {}).update({"named": True, "seen_as": what, "status": "stage"})
        elif kind == "dormant":
            if who in cast["npcs"]:
                cast["npcs"][who]["status"] = "dormant"
        elif kind == "alias":
            cast["aliases"][what] = who
        elif kind == "want":
            cast["npcs"].setdefault(who, {})["wants"] = what
        elif kind == "time":
            state["time"] = what
        elif kind == "location":
            state["location"] = what
    stage = list(plan.get("stage_after", []))[:3]
    for name, npc in cast["npcs"].items():
        if npc.get("status") == "stage" and name not in stage:
            npc["status"] = "dormant"
    state["stage"] = stage
    state["mystery"] = plan.get("mystery_after", "")
    rungs = len(project.scene_card(state["scene"]).get("ladder", [])) or 1
    current = state.get("rung", 1)
    after = int(plan.get("rung_after") or current)
    if plan.get("rung_action") in ("climb", "answered") and after <= current:
        after = current + 1  # a climbed or answered rung always advances
    state["rung"] = max(1, min(after, rungs))
    project.save("knowledge", knowledge)
    project.save("cast", cast)
    project.save("threads", threads)


# --- status ----------------------------------------------------------------------
def status_line(project: Project, state: dict) -> str:
    card = project.scene_card(state["scene"])
    rungs = len(card.get("ladder", [])) or 1
    stage = state.get("stage", [])
    slots = " ".join([f"● {s}" for s in stage] + ["○"] * (3 - len(stage)))
    mystery = f"● {state['mystery']}" if state.get("mystery") else "–"
    head = f" {project.config.get('name', 'PLAY').upper()} ─ {card.get('title', state['scene'])} · {_name(project, state['pov'])}"
    scene = (f" Scene: \"{card.get('question', '')}\"  rung {state.get('rung', 1)}/{rungs} · "
             f"move {state.get('moves', 0)} (dwell {card.get('dwell_min', 0)})")
    tail = f" In the air: {slots}   Mystery: {mystery}   ${state.get('session_cost', 0):.3f} session"
    return "\n".join([head, scene, tail])


def _write_status(project: Project, state: dict, last: dict) -> None:
    write_json(project.home / "status.json", {
        "scene": state["scene"], "pov": state["pov"], "rung": state.get("rung"), "moves": state.get("moves"),
        "stage": state.get("stage"), "mystery": state.get("mystery"), "pending": state.get("pending"),
        "session_cost": state.get("session_cost", 0), "last_turn": last,
    })


# --- the turn --------------------------------------------------------------------
def _scene_header(card: dict) -> str:
    """ProseWright's scene header, so a played log opens as scenes there.

    From the card's optional ``stamp`` ({"clock", "date", "place"}); the place
    falls back to the card's location."""
    stamp = card.get("stamp") or {}
    bits = []
    if stamp.get("clock"):
        bits.append(f"🕰️ Time {stamp['clock']}")
    if stamp.get("date"):
        bits.append(f"🗓️ {stamp['date']}")
    place = stamp.get("place") or card.get("location")
    if place:
        bits.append(f"📍 {place}")
    return f"[ {' | '.join(bits)} ]\n" if bits else ""


def _cut(project: Project, state: dict) -> tuple[str, dict | None]:
    """Close the scene: store its summary, advance to the next card."""
    pending = state.pop("pending", None) or {}
    summary = pending.get("summary", "").strip()
    if summary:
        state.setdefault("summaries", []).append(summary)
        project.append_log("Narrator", f"*{summary}*", is_user=False,
                           meta={"scene": state["scene"], "kind": "summary"})
    nxt = project.next_scene(state["scene"])
    if not nxt:
        return summary, None
    state.update({"scene": nxt["id"], "rung": 1, "moves": 0, "stage": [], "mystery": ""})
    if nxt.get("time"):
        state["time"] = nxt["time"]
    if nxt.get("location"):
        state["location"] = nxt["location"]
    return summary, nxt


def run(project: Project, move: str | None, *, debug: bool = False) -> TurnOutput:
    cfg = project.config.get("models", {})
    state = project.load("state")
    out = TurnOutput()
    pending = state.get("pending")
    original_input = move  # replayed as is by reroll()

    if pending and pending.get("type") == "cut" and move is not None:
        if move.strip().lower() in YES:
            summary, nxt = _cut(project, state)
            out.prose = f"*{summary}*\n\n" if summary else ""
            if nxt is None:
                project.save("state", state)
                out.prose += "*— End of the written spine. —*"
                return out
            move = None  # establish the new scene
        else:
            state.pop("pending", None)
            move = "(the player chooses to stay in the scene)"

    if move is not None and not move.startswith("("):
        state["moves"] = state.get("moves", 0) + 1
        project.append_log(_name(project, state["pov"]), move, is_user=True,
                           meta={"scene": state["scene"], "kind": "move", "pov": state["pov"]})

    plan_res = backend.call(
        cfg.get("planner", "claude-sonnet-5-5"), _planner_system(project), _planner_prompt(project, state, move),
        effort=cfg.get("planner_effort", "low"), schema=prompts.PLAN_SCHEMA,
    )
    plan = plan_res.data or json.loads(plan_res.text)
    card = project.scene_card(state["scene"])
    if plan.get("exit") and state.get("moves", 0) < card.get("dwell_min", 0):
        plan["exit"] = False
        out.debug.append(f"dwell guard: exit blocked ({state.get('moves', 0)}/{card.get('dwell_min')} moves)")
    if len(plan.get("stage_after", [])) > 3:
        out.debug.append(f"stage cap: trimmed {plan['stage_after'][3:]}")
        plan["stage_after"] = plan["stage_after"][:3]
    if plan.get("entrance", {}).get("name") in HABIT_NAMES:
        out.debug.append(f"habit name used: {plan['entrance']['name']}")

    narr_system = _narrator_system(project, state["pov"])
    extra: list[str] = []
    prose_res = None
    for attempt in range(2):
        prose_res = backend.call(
            cfg.get("narrator", "claude-sonnet-5-5"), narr_system,
            _narrator_prompt(project, state, move, plan, extra), effort=cfg.get("narrator_effort", "low"),
        )
        leaks = _leaks(project, state["pov"], prose_res.text)
        if not leaks:
            break
        out.debug.append(f"leak (attempt {attempt + 1}): {', '.join(leaks)}")
        extra = [f"Do not use the words: {', '.join(l.split(' (')[0] for l in leaks)}"]
    prose = prose_res.text
    refrains = _refrains(project, prose)
    if refrains:
        motifs = project.load("motifs")
        motifs["avoid"] = (motifs.get("avoid", []) + refrains)[-MOTIFS_KEPT:]
        project.save("motifs", motifs)
        out.debug.append(f"refrains: {', '.join(refrains)}")

    _apply(project, state, plan)
    cost = plan_res.cost_usd + prose_res.cost_usd
    state["session_cost"] = round(state.get("session_cost", 0) + cost, 5)
    state["turn"] = state.get("turn", 0) + 1
    state.pop("pov_switched", None)
    # Who the reply involves, in plain names, for ProseWright's voice labels:
    # the POV character, and everyone the plan had react or enter.
    voices = list(dict.fromkeys(
        [n for n in plan.get("reacts", []) if n]
        + ([plan["entrance"]["name"]] if plan.get("entrance", {}).get("present") and plan["entrance"].get("name") else [])
    ))
    meta = {
        "scene": state["scene"], "kind": "narration", "move": plan.get("move"),
        "pov": state["pov"], "voices": voices, "input": original_input, "narrated_move": move,
        "rung": state["rung"], "words": _words(prose), "cost": round(cost, 5),
        "seconds": {"planner": plan_res.seconds, "narrator": prose_res.seconds},
        "models": {"planner": plan_res.model, "narrator": prose_res.model},
        "tokens": {"planner": [plan_res.tokens_in, plan_res.cached, plan_res.tokens_out],
                   "narrator": [prose_res.tokens_in, prose_res.cached, prose_res.tokens_out]},
        "plan": plan,
    }
    # A scene's opening reply carries the header; the screen shows prose only.
    logged = _scene_header(card) + prose if move is None else prose
    project.append_log("Narrator", logged, is_user=False, meta=meta)

    if plan.get("exit"):
        state["pending"] = {"type": "cut", "summary": plan.get("summary", "")}
        out.prompt = "⟡ SCENE EXIT — cut to the next scene? [Y/n]"
    if plan.get("reveal_pending", {}).get("present"):
        rp = plan["reveal_pending"]
        out.prompt = (out.prompt + "\n" if out.prompt else "") + (
            f"⟡ REVEAL — {_name(project, state['pov'])}: {rp['truth']}. Noticed by: {rp.get('noticed_by') or 'no one'}. "
            "How does your character take it? (write it as your next move)")

    project.save("state", state)
    _write_status(project, state, meta | {"plan": None})
    project.commit(f"turn {state['turn']} · {state['scene']}")

    out.prose = (out.prose + prose).strip()
    out.status = status_line(project, state)
    out.cost = cost
    out.debug[:0] = [
        f"▸ plan · {plan.get('move')} · rung {plan.get('rung_action')}→{plan.get('rung_after')} · "
        f"reacts: {', '.join(plan.get('reacts', [])) or '–'} · ~{plan.get('target_words')}w{' · mood' if plan.get('mood') else ''}",
        f"  read: {plan.get('read')}",
        f"  events: {'; '.join(e['type'] + ':' + e['who'] + '→' + e['what'] for e in plan.get('events', [])) or '–'}",
        f"  words {_words(prose)} · planner {plan_res.tokens_in}in/{plan_res.cached}cached/{plan_res.tokens_out}out {plan_res.seconds}s"
        f" · narrator {prose_res.tokens_in}in/{prose_res.cached}cached/{prose_res.tokens_out}out {prose_res.seconds}s · ${cost:.4f}",
    ]
    return out


def switch_pov(project: Project, char_id: str) -> str:
    state = project.load("state")
    if not project.cast_card(char_id):
        return f"no character {char_id!r}"
    old = state["pov"]
    party = [p for p in state.get("party", []) if p != char_id]
    if old not in party:
        party.append(old)
    state.update({"pov": char_id, "party": party, "pov_switched": True})
    project.save("state", state)
    project.commit(f"pov → {char_id}")
    return f"POV → {_name(project, char_id)}. Your next move re-establishes the scene from their side."


def player_move(project: Project, profile: str) -> str:
    """Autoplay: the player agent writes the POV character's next move."""
    cfg = project.config.get("models", {})
    state = project.load("state")
    pov = state["pov"]
    card = project.cast_card(pov)
    knows = project.load("knowledge").get(pov, {}).get("facts", [])
    rows = project.turns()[-4:]
    system = prompts.PLAYER_SYSTEM.format(name=card.get("name", pov), profile=profile,
                                          card=card.get("card", ""))
    prompt = ("## WHAT SHE KNOWS\n" + "\n".join(f"- {k}" for k in knows) +
              "\n\n## THE STORY SO FAR (most recent last)\n" + _fmt_turns(rows) +
              f"\n\nWrite {card.get('name', pov)}'s next move.")
    res = backend.call(cfg.get("player", "claude-haiku-4-5"), system, prompt)
    state["session_cost"] = round(state.get("session_cost", 0) + res.cost_usd, 5)
    project.save("state", state)
    return res.text


# --- swipes: another telling of the last reply, or another turn altogether -------
def _last_reply(project: Project) -> tuple[list[dict], dict, dict]:
    rows = project.turns()
    last = rows[-1] if rows else None
    meta = ((last or {}).get("extra") or {}).get("player_mode") or {}
    if not last or last.get("is_user") or meta.get("kind") != "narration" or not meta.get("plan"):
        raise ValueError("the last message isn't a narrated reply")
    return rows, last, meta


def _narrated_move(rows: list[dict], meta: dict) -> str | None:
    if "narrated_move" in meta:
        return meta["narrated_move"]
    before = rows[-2] if len(rows) > 1 else None  # replies from before the engine recorded it
    if before and before.get("is_user") and ((before.get("extra") or {}).get("player_mode") or {}).get("scene") == meta.get("scene"):
        return before.get("mes")
    return None


def renarrate(project: Project, *, debug: bool = False) -> TurnOutput:
    """Tell the last reply again: same plan (same events, same people), new prose.
    Every telling is kept as a swipe; the new one is chosen."""
    cfg = project.config.get("models", {})
    rows, last, meta = _last_reply(project)
    state = project.load("state")
    if state.get("scene") != meta.get("scene"):
        raise ValueError("the story has moved to another scene since that reply")
    plan = meta["plan"]
    move = _narrated_move(rows, meta)
    work = dict(state, pov=meta.get("pov") or state["pov"])
    out = TurnOutput()
    narr_system = _narrator_system(project, work["pov"])
    extra: list[str] = []
    prose_res = None
    for attempt in range(2):
        prose_res = backend.call(
            cfg.get("narrator", "claude-sonnet-5-5"), narr_system,
            _narrator_prompt(project, work, move, plan, extra, drop_last=True),
            effort=cfg.get("narrator_effort", "low"),
        )
        leaks = _leaks(project, work["pov"], prose_res.text)
        if not leaks:
            break
        out.debug.append(f"leak (attempt {attempt + 1}): {', '.join(leaks)}")
        extra = [f"Do not use the words: {', '.join(l.split(' (')[0] for l in leaks)}"]
    prose = prose_res.text
    refrains = _refrains(project, prose)
    if refrains:
        motifs = project.load("motifs")
        motifs["avoid"] = (motifs.get("avoid", []) + refrains)[-MOTIFS_KEPT:]
        project.save("motifs", motifs)
        out.debug.append(f"refrains: {', '.join(refrains)}")
    swipes = list(last.get("swipes") or [last.get("mes", "")])
    swipes.append(prose)
    last["swipes"], last["swipe_id"], last["mes"] = swipes, len(swipes) - 1, prose
    meta.setdefault("swipe_costs", [meta.get("cost", 0)]).append(round(prose_res.cost_usd, 5))
    meta["words"] = _words(prose)
    last.setdefault("extra", {})["player_mode"] = meta
    project.replace_last_row(last)
    state["session_cost"] = round(state.get("session_cost", 0) + prose_res.cost_usd, 5)
    project.save("state", state)
    _write_status(project, state, meta | {"plan": None})
    project.commit(f"re-narrate turn {state.get('turn', 0)} · telling {len(swipes)}")
    out.prose = prose
    out.status = status_line(project, state)
    out.cost = prose_res.cost_usd
    out.debug.append(f"re-narrated · telling {len(swipes)} · narrator {prose_res.tokens_in}in/"
                     f"{prose_res.cached}cached/{prose_res.tokens_out}out {prose_res.seconds}s · ${prose_res.cost_usd:.4f}")
    return out


def choose_swipe(project: Project, index: int) -> str:
    """Make one of the last reply's tellings the one in the log."""
    _, last, meta = _last_reply(project)
    swipes = last.get("swipes") or [last.get("mes", "")]
    if not 0 <= index < len(swipes):
        raise ValueError(f"there is no telling {index + 1} (there are {len(swipes)})")
    if last.get("swipe_id", len(swipes) - 1) == index:
        return f"telling {index + 1} of {len(swipes)}"
    last["swipe_id"], last["mes"] = index, swipes[index]
    meta["words"] = _words(swipes[index])
    project.replace_last_row(last)
    project.commit(f"swipe · telling {index + 1} of {len(swipes)}")
    return f"telling {index + 1} of {len(swipes)}"


def reroll(project: Project, *, debug: bool = False) -> TurnOutput:
    """Play the last turn again from scratch: a new plan (other events), new prose.
    The turn and all its tellings are discarded first."""
    rows, last, meta = _last_reply(project)
    if "input" in meta:
        replay = meta["input"]
    else:  # replies from before the engine recorded it
        before = rows[-2] if len(rows) > 1 else {}
        bmeta = (before.get("extra") or {}).get("player_mode") or {}
        replay = before.get("mes") if before.get("is_user") else ("y" if bmeta.get("kind") == "summary" else None)
    if not (project.home / ".git").exists():
        raise ValueError("re-planning needs the play folder's turn history (it is not a git folder)")
    subjects = project._git("log", "--format=%s", "-n", "200").stdout.splitlines()
    k = 0
    while k < len(subjects) and subjects[k].startswith(("re-narrate", "swipe")):
        k += 1
    if k >= len(subjects) or not subjects[k].startswith("turn "):
        raise ValueError("the last change wasn't a turn (a POV switch or a reset came after it); undo that first")
    project._git("reset", "-q", "--hard", f"HEAD~{k + 1}")
    return run(project, replay, debug=debug)
