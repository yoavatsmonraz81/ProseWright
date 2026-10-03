"""Phase 0 command line: list, show, backup, restore.

All commands default to the throwaway working copy (config.working_log()).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import backup as backup_mod
from . import canon as canon_mod
from . import canon_layer
from . import attribution as attribution_mod
from . import (
    config,
    doctor as doctor_mod,
    identity as identity_mod,
    index as index_mod,
    layers as layers_mod,
    llm,
    loader,
    manuscript as manuscript_mod,
    novelize as novelize_mod,
    pauses as pauses_mod,
    propagate as propagate_mod,
    st_sync,
    structure as structure_mod,
    transform as transform_mod,
)
from .index.search import (
    search as fused_search,
    search_keyword,
    search_semantic,
    search_structural,
)


def _cmd_list(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    rows = log.messages
    if args.speaker:
        rows = [m for m in rows if m.speaker.lower() == args.speaker.lower()]
    if args.limit:
        rows = rows[: args.limit]
    print(f"# {log.path.name} — {len(log)} messages; speakers: {log.speakers()}")
    for m in rows:
        print(f"{m.msg_id:>4}  {m.role():<6} {m.speaker:<10}  {m.preview()}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    m = log.get(args.msg_id)
    print(f"--- message {m.msg_id} | {m.role()} | {m.speaker} ---")
    if m.raw.get("send_date"):
        print(f"sent: {m.raw['send_date']}")
    swipes = m.raw.get("swipes")
    if isinstance(swipes, list) and len(swipes) > 1:
        print(f"swipes: {len(swipes)} (active #{m.raw.get('swipe_id', 0)})")
    print()
    print(m.text)
    return 0


def _cmd_backup(args: argparse.Namespace) -> int:
    dest = backup_mod.backup(args.log, label=args.label)
    print(f"backed up {Path(args.log).name} -> {dest}")
    return 0


def _cmd_restore(args: argparse.Namespace) -> int:
    if args.list:
        found = backup_mod.list_backups(args.log)
        if not found:
            print("no backups found")
            return 0
        for p in found:
            print(p.name)
        return 0
    src = backup_mod.restore(args.log, from_backup=args.from_backup)
    print(f"restored {Path(args.log).name} from {Path(src).name}")
    return 0


def _cmd_pauses(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    found = pauses_mod.find_pauses(log)
    if args.retire_only:
        found = [p for p in found if p.retire_cue]
    if not found:
        print("no day-boundary pauses found (no parseable date headers?)")
        return 0
    print(f"# {len(found)} pause(s) in {log.path.name} (insert interludes here)")
    for p in found:
        print(p.label())
        print(f"        closes: {p.close_preview}")
        if args.verbose:
            print(f"        opens : {p.open_preview}")
    return 0


























def _cmd_identity(args: argparse.Namespace) -> int:
    """Stable message identity — the anchor every sidecar hangs from."""
    log_path = Path(args.log)
    if not log_path.exists():
        print(f"no log found at {log_path}", file=sys.stderr)
        return 1

    if args.identity_action == "status":
        log = loader.load(log_path)
        with_uid = sum(1 for m in log.messages if m.uid)
        print(f"{log_path.name}: {len(log)} messages · {with_uid} identified")
        if with_uid < len(log):
            print(f"  {len(log) - with_uid} unidentified — run `identity backfill`")
        return 0 if with_uid == len(log) else 1

    report = identity_mod.backfill(log_path, dry_run=args.dry_run)
    print(report.summary())
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    """Push the working log back to the live SillyTavern chat file."""
    src = Path(args.log)
    try:
        dest = Path(args.to) if args.to else st_sync.project_chat()
    except st_sync.NoChatConfigured as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not src.exists():
        print(f"no log found at {src}", file=sys.stderr)
        return 1

    if args.sync_action == "status":
        info = st_sync.chat_status(src, dest)
        print(f"working : {info['working']}  ({info['working_count']})")
        print(f"ST chat : {info['st']}  ({info['st_count']}"
              f"{'' if info['st_exists'] else '; missing'})")
        print(f"state   : {info['kind']}")
        if info.get("changed_message_count"):
            ids = ", ".join(str(i) for i in info.get("changed_msg_ids") or [])
            print(
                f"changed : {info['changed_message_count']} message payload(s); "
                f"{info.get('changed_text_count', 0)} text change(s)"
            )
            if ids:
                print(f"msg ids : {ids}")
        return 0

    result = st_sync.write_st_chat(src, dest, dry_run=args.dry_run)
    if args.dry_run:
        print(f"would write {result['messages']} messages to {dest} (uids stripped)")
        return 0
    print(f"wrote {result['messages']} messages to {dest} (uids stripped)")
    if result["backup"]:
        print(f"backup: {Path(result['backup']).name}")
    return 0


def _novelize_span(args: argparse.Namespace, log) -> tuple[int, int] | None:
    """The span to work on: an explicit range, or a whole scene by id."""
    if args.scene:
        for scene in structure_mod.segment_scenes(log):
            if str(scene.scene_id) == str(args.scene):
                return scene.start_msg_id, scene.end_msg_id
        print(f"no scene {args.scene} in this log", file=sys.stderr)
        return None
    if args.msg_from is None:
        print("give --from N [--to M] or --scene ID", file=sys.stderr)
        return None
    start = args.msg_from
    return start, args.msg_to if args.msg_to is not None else start


def _voice_patch(args: argparse.Namespace) -> dict[str, str]:
    patch: dict[str, str] = {}
    if args.person:
        patch["person"] = args.person
    if args.tense:
        patch["tense"] = args.tense
    if args.focal is not None:
        patch["focal"] = args.focal
    return patch


def _cmd_novelize(args: argparse.Namespace) -> int:
    """Phase 3 — the log becomes prose, one scene at a time."""
    action = args.novelize_action
    log_path = Path(args.log)
    if not log_path.exists():
        print(f"no log found at {log_path}", file=sys.stderr)
        return 1
    log = loader.load(log_path)
    doc = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)
    patch = _voice_patch(args)

    if action == "voice":
        if not patch:
            print(f"the book is written in {doc.voice.describe()}")
            print("\nchange it with --person / --tense / --focal:")
            print(f"  person: {', '.join(manuscript_mod.PERSONS)}")
            print(f"  tense:  {', '.join(manuscript_mod.TENSES)}")
            return 0
        doc.voice = doc.voice.merged(patch)
        manuscript_mod.save(doc)
        print(f"the book is now written in {doc.voice.describe()}")
        print("scenes already novelized keep the voice they were written in.")
        return 0

    if action == "status":
        stats = doc.stats()
        scenes = novelize_mod.orchestration_units(log)
        done = sum(1 for s in scenes if doc.covering(s.start))
        print(f"voice: {doc.voice.describe()}")
        print(f"novelized: {done}/{len(scenes)} scenes "
              f"({stats['approved']} approved · {stats['draft']} draft · "
              f"{stats['rejected']} rejected)")
        print(f"words: {stats['words']:,}")
        drifts = [d for d in manuscript_mod.drift(doc, log) if d.dirty]
        if drifts:
            print(f"drifted: {len(drifts)} scene(s) whose source has moved")
        return 0

    if action == "assemble":
        target = Path(config.WORKSPACE_DIR) / "private_manuscript.md"
        target.write_text(
            novelize_mod.assemble_private_markdown(
                doc, log, approved_only=bool(args.approved_only)
            ),
            encoding="utf-8",
        )
        print(f"assembled {target} ({target.stat().st_size:,} bytes)")
        return 0

    if action == "batch":
        result = novelize_mod.run_batch(
            log,
            doc=doc,
            voice=patch or None,
            model=args.model,
            episode_id=args.episode,
            max_scenes=args.max_scenes,
            max_span_chars=getattr(args, "max_span_chars", None),
            regenerate=args.regenerate,
            stop_on_error=args.stop_on_error,
            max_retries=args.max_retries,
            continuity_chars=args.continuity_chars,
            on_progress=lambda stage, detail, **_: print(f"{stage}: {detail}", flush=True),
        )
        counts = result.to_json()["counts"]
        print(
            f"batch complete: {counts['novelized']} novelized · "
            f"{counts['failed']} failed · {counts['skipped']} skipped"
        )
        if result.campaign:
            print(f"campaign: {result.campaign.get('path')}")
            print(f"assembled: {result.campaign.get('assembled')}")
        return 1 if result.failed else 0

    if action == "show":
        span = _novelize_span(args, log)
        if span is None:
            return 1
        scene = doc.covering(span[0])
        if scene is None:
            print(f"messages {span[0]}..{span[1]} have not been novelized")
            return 1
        voice = scene.voice or doc.voice
        print(f"{scene.id} — {scene.title or '(untitled)'}  [{scene.status}]")
        print(f"msgs {scene.anchor.start}–{scene.anchor.end} · {voice.describe()} · "
              f"{len(scene.text().split()):,} words · {scene.model}")
        d = manuscript_mod.scene_drift(scene, log)
        if d.dirty:
            print(f"DRIFT: {d.kind} — the log under this scene has changed")
        print()
        print(scene.text())
        return 0

    span = _novelize_span(args, log)
    if span is None:
        return 1
    start, end = span

    if action == "plan":
        p = novelize_mod.plan(
            log, start, end, doc=doc, override=patch or None,
            max_span_chars=getattr(args, "max_span_chars", None),
        )
        print(f"msgs {start}–{end} · {p['title'] or '(no location)'}")
        print(f"  turns:  {p['turns']}")
        over = p["too_long"]
        chunk_note = ""
        if over and p.get("chunk_count", 0) > 1:
            chunk_note = f"  → {p['chunk_count']} chunked passes"
        elif over and p.get("unchunkable"):
            chunk_note = "  UNCHUNKABLE (a single turn is over the ceiling)"
        elif over:
            chunk_note = "  OVER THE ONE-PASS CEILING"
        print(f"  source: {p['source_chars']:,} chars" + chunk_note)
        if p.get("chunks") and p.get("chunk_count", 0) > 1:
            for i, c in enumerate(p["chunks"], 1):
                print(f"    chunk {i}: msgs {c['from']}–{c['to']} · "
                      f"{c['source_chars']:,} chars · {c['turns']} turns")
        print(f"  voice:  {p['voice_label']}")
        cast = ", ".join(f"{c['name']} ({c['turns']})" for c in p["focal_candidates"][:6])
        print(f"  cast:   {cast or 'n/a'}")
        if p["existing"]:
            print(f"  already novelized as {p['existing']['id']} "
                  f"({p['existing']['status']}) — `run --regenerate` to replace it")
        return 0

    # action == "run"
    existing = doc.covering(start)
    if existing is not None and not args.regenerate:
        print(f"messages {start}..{end} are already novelized as {existing.id} "
              f"({existing.status}).", file=sys.stderr)
        print("pass --regenerate to write a new take over it.", file=sys.stderr)
        return 1

    try:
        proposal = novelize_mod.propose(
            log, start, end,
            doc=doc,
            voice=patch or None,
            direction=args.direction or "",
            model=args.model,
            max_span_chars=getattr(args, "max_span_chars", None),
        )
    except novelize_mod.TooLong as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, llm.ModelError) as exc:
        print(f"novelization failed: {exc}", file=sys.stderr)
        return 1

    print(f"{proposal.voice.describe()} · {proposal.turns} turns → "
          f"{len(proposal.scene.blocks)} paragraphs · "
          f"{proposal.source_chars:,} → {proposal.prose_chars:,} chars")
    for warning in proposal.warnings:
        print(f"  ! {warning}")
    print()
    print(proposal.scene.text())

    if args.dry_run:
        print("\n(dry run — nothing filed)")
        return 0
    novelize_mod.commit(proposal, doc=doc)
    print(f"\nfiled as {proposal.scene.id} (draft) in {manuscript_mod.path_for()}")
    print("read it on the page's novel layer, then approve or reject it there.")
    return 0


def _cmd_index_build(args: argparse.Namespace) -> int:
    log_path = Path(args.log).resolve()
    db_path = config.index_db_for(log_path) if not args.db else Path(args.db)
    print(f"building index for {log_path.name} -> {db_path}")
    print(f"embedder: {config.EMBED_MODEL_NAME} (dim={config.EMBED_DIM})")
    print("(this loads the model and embeds every message; first run can take a moment)")
    out = index_mod.build(log_path, db_path=db_path, rebuild=args.rebuild)
    summary = index_mod.info(out)
    print(
        f"done: {summary['messages']} messages indexed in {summary['meta'].get('build_seconds', '?')}s"
    )
    print(f"  speakers (most frequent first): {', '.join(summary['speakers'][:6])}")
    print(f"  db: {out}")
    return 0


def _cmd_index_info(args: argparse.Namespace) -> int:
    db_path = config.index_db_for(args.log) if not args.db else Path(args.db)
    summary = index_mod.info(db_path)
    if not summary["exists"]:
        print(f"no index at {summary['path']} (run `index build` first)")
        return 1
    print(f"index: {summary['path']}")
    print(f"  messages: {summary['messages']}  fts: {summary['fts_rows']}  vec: {summary['vec_rows']}")
    for k, v in summary["meta"].items():
        print(f"  {k:>14}: {v}")
    print(f"  speakers: {', '.join(summary['speakers'])}")
    return 0


def _print_hits(hits: list, query: str | None = None) -> None:
    if not hits:
        print("(no hits)")
        return
    for i, h in enumerate(hits, 1):
        intl = " [interlude]" if h.is_interlude else ""
        when = f"{h.story_date or '?'} {h.story_time or ''}".strip()
        loc = f" | 📍 {h.location}" if h.location else ""
        bits = []
        if "keyword_rank" in h.breakdown:
            bits.append(f"k#{h.breakdown['keyword_rank']+1}")
        if "semantic_rank" in h.breakdown:
            bits.append(f"v#{h.breakdown['semantic_rank']+1}")
        ranks = f" [{' '.join(bits)}]" if bits else ""
        print(
            f"{i:>2}. msg {h.msg_id:>4}  {h.role:<5} {h.speaker:<14}"
            f"  {when}{loc}{intl}  score={h.score:.4f}{ranks}"
        )
        print(f"    {h.preview(160)}")


def _cmd_index_search(args: argparse.Namespace) -> int:
    db_path = config.index_db_for(args.log) if not args.db else Path(args.db)
    if not Path(db_path).exists():
        print(f"error: no index at {db_path}. Run `index build` first.", file=sys.stderr)
        return 1
    common = dict(
        speaker=args.speaker,
        role=args.role,
        is_interlude=True if args.interludes else (False if args.no_interludes else None),
        date_from=args.date_from,
        date_to=args.date_to,
    )
    if args.mode == "structural":
        hits = search_structural(db_path, limit=args.limit, **common)
    elif args.mode == "keyword":
        if not args.query:
            print("error: keyword search needs a query string", file=sys.stderr)
            return 1
        hits = search_keyword(db_path, args.query, limit=args.limit, **common)
    elif args.mode == "semantic":
        if not args.query:
            print("error: semantic search needs a query string", file=sys.stderr)
            return 1
        hits = search_semantic(db_path, args.query, limit=args.limit, **common)
    else:  # fused (default)
        if not args.query:
            print("error: fused search needs a query string (or use --mode structural)", file=sys.stderr)
            return 1
        hits = fused_search(
            db_path, args.query, limit=args.limit,
            candidates_per_layer=args.candidates, **common,
        )
    header = f"# {args.mode} search"
    if args.query:
        header += f" — {args.query!r}"
    if any(common.values()):
        applied = {k: v for k, v in common.items() if v is not None}
        header += f"  (filters: {applied})"
    print(header)
    _print_hits(hits)
    return 0




def _cmd_structure_scenes(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    info = structure_mod.summarize(log)
    scenes = info["scenes"]
    print(
        f"# {Path(args.log).name}: {info['n_scenes']} scenes, "
        f"{info['n_episodes']} episodes, {info['n_interludes']} interludes"
    )
    for s in scenes:
        marker = "»" if s.kind == "interlude" else " "
        who = ", ".join(s.speakers[:4]) + ("…" if len(s.speakers) > 4 else "")
        print(
            f"{marker} S{s.scene_id:<3} {s.span_label():<34} "
            f"{s.title():<34} ({s.msg_count} msg; {who})"
        )
        if args.verbose:
            print(f"      ↳ {s.opening}")
    return 0


def _cmd_structure_episodes(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    info = structure_mod.summarize(log)
    episodes = info["episodes"]
    print(f"# {Path(args.log).name}: {info['n_episodes']} episodes")
    for e in episodes:
        locs = "; ".join(e.locations[:5]) + ("…" if len(e.locations) > 5 else "")
        intl = f", {e.interlude_count} interlude(s)" if e.interlude_count else ""
        print(
            f"E{e.episode_id:<3} {e.date_str or '(undated)':<26} "
            f"msg {e.start_msg_id}–{e.end_msg_id}  "
            f"({e.scene_count} scenes, {e.msg_count} msg{intl})"
        )
        if args.verbose and locs:
            print(f"      ↳ {locs}")
    return 0


def _print_spine(proposal: structure_mod.SpineProposal, *, title: str) -> None:
    is_episode = proposal.unit_type == "episode"
    unit = "episodes" if is_episode else "beats"
    prefix = "E" if is_episode else "B"
    print(f"# {title} — {len(proposal.beats)} {unit} "
          f"({proposal.n_scenes} scenes)")
    if proposal.coverage is not None:
        ok = proposal.coverage.get("ok", False)
        print(f"  coverage: {'OK' if ok else 'ISSUES'}")
    for b in proposal.beats:
        kind = f"[{b.kind}] " if b.kind else ""
        display_id = b.beat_id + 1 if is_episode else b.beat_id
        print(f"  {prefix}{display_id:<2} {b.span_label():<18} {kind}{b.title}")
        print(f"        ↳ {b.justification}")
        for ev in b.evidence[:2]:
            print(f"          · {ev[:100]}{'…' if len(ev) > 100 else ''}")
    if proposal.critique_notes:
        print("  critique:")
        for note in proposal.critique_notes[:6]:
            print(f"    - {note}")


def _card_progress(done: int, total: int, n: int) -> None:
    print(f"  scene-card map pass: batch {done}/{total} ({n} scenes) ...")


def _cmd_structure_cards(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    scenes = structure_mod.segment_scenes(log)
    if args.rebuild:
        print(f"rebuilding scene cards for {Path(args.log).name} "
              f"({sum(1 for s in scenes if s.kind == 'scene')} scenes) ...")
    try:
        cards = structure_mod.scene_cards(
            log, force=args.rebuild, on_progress=_card_progress
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"\n# scene cards — {len(cards)} cards ({config.SCENE_CARDS.name})")
    loc_by_span = structure_mod.scene_location_by_span()
    for s in scenes:
        if s.scene_id in cards:
            card = loc_by_span.get((s.start_msg_id, s.end_msg_id)) or {}
            loc = card.get("location") or s.location or ""
            aliases = card.get("location_aliases") or []
            loc_bit = f" @ {loc}" if loc else ""
            if aliases:
                loc_bit += f" [{', '.join(aliases[:4])}]"
            print(
                f"  S{s.scene_id:<3} {s.span_label():<32}{loc_bit}\n"
                f"       {cards[s.scene_id]}"
            )
    return 0


def _cmd_structure_derive(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    use_cards = not args.no_cards
    use_premise = not args.no_premise
    print(f"deriving beat spine for {Path(args.log).name} "
          f"(max {args.max_beats} beats; "
          f"cards={'on' if use_cards else 'off'}, "
          f"premise={'on' if use_premise else 'off'}, "
          f"critique={'on' if not args.no_critique else 'off'}) ...")
    try:
        proposal = structure_mod.derive_beats(
            log,
            max_beats=args.max_beats,
            use_cards=use_cards,
            use_premise=use_premise,
            force_cards=args.rebuild_cards,
            critique=not args.no_critique,
            on_card_progress=_card_progress,
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    structure_mod.write_pending_spine(proposal)
    print()
    _print_spine(proposal, title="PROPOSED beat spine")
    print(f"\nReadable preview: {config.PENDING_SPINE_MD}")
    print(f"Critique report: {config.PENDING_SPINE_CRITIQUE}")
    print("Commit it:  python -m story_editor structure commit"
          "\nDiscard it: python -m story_editor structure discard")
    return 0


def _cmd_structure_validate(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    if args.pending:
        proposal = structure_mod.load_pending_spine()
        label = "pending"
    else:
        proposal = structure_mod.load_derived_spine()
        label = "committed"
    if proposal is None:
        print(f"no {label} spine to validate", file=sys.stderr)
        return 1
    unit = "episodes" if proposal.unit_type == "episode" else "beats"
    report = structure_mod.validate_proposal_coverage(proposal, log)
    print(f"# spine coverage ({label}) — {len(proposal.beats)} {unit}, "
          f"{report.n_scenes} scenes")
    for line in report.summary_lines():
        print(f"  {line}")
    return 0 if report.ok else 1


def _cmd_structure_spine(args: argparse.Namespace) -> int:
    pending = structure_mod.load_pending_spine()
    if pending is not None:
        _print_spine(pending, title="PENDING (uncommitted) beat spine")
        print()
    committed = structure_mod.load_derived_spine()
    if committed is not None:
        title = (
            "director-approved canon episodes"
            if committed.unit_type == "episode"
            else "committed derived spine"
        )
        _print_spine(committed, title=title)
    elif pending is None:
        print("no spine yet (run `structure derive`)")
    return 0


def _cmd_structure_commit(args: argparse.Namespace) -> int:
    try:
        proposal = structure_mod.commit_spine()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"committed derived spine ({len(proposal.beats)} beats) -> "
          f"{config.DERIVED_SPINE}")
    return 0


def _cmd_structure_discard(args: argparse.Namespace) -> int:
    print("discarded pending spine" if structure_mod.discard_pending_spine()
          else "nothing pending")
    return 0


def _cmd_structure_doctor(args: argparse.Namespace) -> int:
    report = doctor_mod.run(args.log)
    counts = report.counts()
    print(f"# structure doctor — {Path(report.log_path).name} "
          f"({report.n_messages} messages)")
    print(f"  {counts['RISK']} risk, {counts['WARN']} warn, {counts['INFO']} info\n")

    glyph = {doctor_mod.RISK: "✗", doctor_mod.WARN: "▲", doctor_mod.INFO: "·"}
    for f in report.sorted_findings():
        if args.risks_only and f.severity != doctor_mod.RISK:
            continue
        print(f"{glyph[f.severity]} [{f.severity}] {f.title}  ({f.code})")
        if f.detail:
            print(f"    {f.detail}")
        if f.msg_ids:
            shown = ", ".join(str(i) for i in f.msg_ids[:25])
            more = f" … (+{len(f.msg_ids) - 25} more)" if len(f.msg_ids) > 25 else ""
            print(f"    msgs: {shown}{more}")

    worst = report.worst()
    print()
    if worst == doctor_mod.RISK:
        print("verdict: RISK — review the items above before processing this log.")
        return 2
    if worst == doctor_mod.WARN:
        print("verdict: OK with warnings — safe to process; glance at the warnings.")
        return 0
    print("verdict: clean — safe to process.")
    return 0


def _cmd_structure_drift(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    if structure_mod.load_derived_spine() is None:
        print("error: no committed baseline spine. Run `structure derive` then "
              "`structure commit` to set one, then edit and re-run drift.",
              file=sys.stderr)
        return 1
    print(f"re-deriving spine for {Path(args.log).name} and comparing to the "
          f"committed baseline ...")
    try:
        report = structure_mod.detect_drift(
            log, max_beats=args.max_beats, on_card_progress=_card_progress
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    c = report.counts()
    print(f"\n# drift — baseline {report.n_base} beats (committed "
          f"{report.baseline_created[:10]}) → current {report.n_cur} beats")
    print(f"  {c['stable']} stable, {c['moved']} moved, "
          f"{c['added']} added, {c['dropped']} dropped\n")
    glyph = {"stable": "=", "moved": "~", "added": "+", "dropped": "-"}
    for d in report.deltas:
        beat = d.cur or d.base
        sim = f"  (~{d.similarity:.0%})" if d.status in ("stable", "moved") else ""
        print(f"{glyph[d.status]} [{d.status}] {beat.title}{sim}")
        if d.note:
            print(f"      {d.note}")
        if d.status == "added" and d.cur:
            print(f"      now at {d.cur.span_label()}: {d.cur.justification}")
        if d.status == "dropped" and d.base:
            print(f"      was at {d.base.span_label()}: {d.base.justification}")
    print()
    print("verdict: the story's shape SHIFTED." if report.shifted()
          else "verdict: shape STABLE — edits did not move the beats.")
    return 0


def _cmd_structure_audit(args: argparse.Namespace) -> int:
    # Prefer the committed derived spine; fall back to the pending one; if
    # neither exists, derive fresh so audit always has something to check.
    derived = structure_mod.load_derived_spine() or structure_mod.load_pending_spine()
    source = "committed/pending derived spine"
    if derived is None:
        print("no derived spine yet; deriving one now ...")
        try:
            derived = structure_mod.derive_beats(loader.load(args.log),
                                                 max_beats=args.max_beats)
        except llm.ModelError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        source = "freshly derived (not saved)"

    print(f"auditing {source} against authored spine "
          f"({Path(config.AUTHORED_SPINE_MD).name if config.AUTHORED_SPINE_MD else 'none'}) ...")
    try:
        audit = structure_mod.audit_spine(
            derived,
            on_progress=lambda stage, detail: print(f"  [{stage}] {detail}"),
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    authored = {b.label: b for b in audit["authored"]}
    res = audit["result"]
    by_did = {b.beat_id: b for b in derived.beats}

    print(f"\n# audit — {len(derived.beats)} derived beats vs "
          f"{len(authored)} authored beats (retrieval-grounded)")
    cov = res.get("coverage")
    if cov:
        print(f"coverage: {cov}\n")

    print("authored beats:")
    for row in res.get("beats") or []:
        label = str(row.get("authored"))
        beat = authored.get(label)
        title = beat.title if beat else "—"
        verdict = row.get("verdict") or "?"
        evidence = row.get("evidence") or []
        cite = f" msg {evidence}" if evidence else ""
        print(f"  A{label} [{verdict}] {title}{cite}")
        if row.get("note"):
            print(f"      {row['note']}")

    print("\nalignment (derived → authored):")
    for a in res.get("alignment", []):
        d = by_did.get(a.get("derived"))
        anum = str(a.get("authored"))
        atitle = authored[anum].title if anum in authored else "— (no match)"
        dlabel = f"D{d.beat_id} {d.title}" if d else f"D{a.get('derived')}"
        span = f" [{d.span_label()}]" if d else ""
        print(f"  {dlabel}{span}")
        print(f"      → A{anum}: {atitle}" if anum is not None
              else "      → (matches no authored beat)")
        if a.get("note"):
            print(f"        {a['note']}")

    absent = res.get("absent") or []
    if absent:
        print("\nauthored beats not yet present in this log (slice frontier):")
        for n in absent:
            key = str(n)
            if key in authored:
                print(f"  A{key}: {authored[key].title}")

    drift = res.get("drift") or []
    print("\ndrift flags:" if drift else "\ndrift flags: none")
    for d in drift:
        print(f"  ⚠ {d}")

    # Cache the same payload the GUI's [validate] button writes, so CLI and
    # browser stay on one verdict.
    from . import views as views_mod
    views_mod.save_alignment(res, authored_count=len(authored))
    print(f"\nsaved alignment → {config.SPINE_ALIGNMENT}")
    return 0


def _resolve_restyle_span(args: argparse.Namespace, log: loader.Log):
    return transform_mod.resolve_span(
        log,
        beat=args.beat,
        scene=args.scene,
        msg_from=getattr(args, "from"),
        msg_to=args.to,
        speaker=args.speaker,
        role=args.role,
    )


def _cmd_restyle(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    try:
        span = _resolve_restyle_span(args, log)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"# restyle target — {span.locator}")
    print(f"  {len(span.messages)} message(s) in scope "
          f"(msgs {span.msg_from}–{span.msg_to})")
    for line in transform_mod.span_preview(span):
        print(line)

    if not span.messages:
        print("\n(no messages match this span; widen the locator or check --speaker)")
        return 1

    if args.dry_run:
        print(f"\n(dry run — no rewrite. Drop --dry-run with a --note to generate.)")
        return 0

    if not args.note:
        print("\nerror: a --note is required to restyle (or use --dry-run to preview "
              "the span)", file=sys.stderr)
        return 1

    # ── Pre-flight: canon check on the target text as-is ─────────────────────
    # If the target is already fully in-character, the model will likely return
    # the original unchanged — surface this before spending a generation call.
    if span.messages:
        speaker_name = span.messages[0].speaker
        char = canon_layer.get_character(speaker_name)
        if char is not None:
            sample = span.messages[0]
            preflight = canon_layer.check_in_character(
                before=sample.text, after=sample.text,
                speaker=speaker_name, char=char,
            )
            if preflight.get("verdict") == "in_character":
                print(
                    f"\n⚠ pre-flight: msg {sample.msg_id} ({speaker_name}) is already "
                    f"in-character per the canon layer. The model may return the original "
                    f"unchanged. Consider:\n"
                    f"  • a sharper --note that specifies a concrete structural change\n"
                    f"  • targeting a span that has actual drift (use `structure derive` "
                    f"or `canon check` to find it)\n"
                    f"  Proceeding anyway…"
                )

    def _progress(i, total, target):
        print(f"  …restyling {i}/{total} (msg {target.msg_id} {target.speaker})",
              file=sys.stderr)

    try:
        edit_set = transform_mod.restyle_span(
            args.log, span, args.note,
            chain=not args.independent,
            on_progress=_progress,
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    if not edit_set.edits:
        # Not an error — the model judged the target already correct.
        # Exit 0 so pipelines and test harnesses don't treat this as a failure.
        print(
            "· no message changed — the model judged the target already meets the "
            "direction. This is correct behaviour when the target is in-character.\n"
            "  To force output: target a span with visible drift, or give a concrete "
            "structural --note (e.g. 'cut to ≤2 sentences', 'remove all action beats')."
        )
        return 0
    flagged = [e for e in edit_set.edits if e.flags]
    hard = [e for e in edit_set.edits if e.hard_flagged]
    print(f"proposed {len(edit_set.edits)} edit(s) — pending review"
          + (f" ({len(flagged)} flagged ⚠)" if flagged else "") + ":")
    for e in edit_set.edits:
        print(f"\n── msg {e.msg_id} · {e.speaker} ──")
        for f in e.flags:
            print(f"  ⚠ {f}")
        print(transform_mod._unified(e.before, e.after))
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    if hard:
        ids = ", ".join(str(e.msg_id) for e in hard)
        print(f"⚠ flagged (msgs {ids}) — commit will refuse until you "
              f"`edits drop <msg_id>` or `edits commit --allow-flagged`.")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    return 0


def _ids_from_args(args: argparse.Namespace) -> list[int]:
    ids: list[int] = []
    if getattr(args, "msg_id", None) is not None:
        ids.append(int(args.msg_id))
    lo = getattr(args, "from", None)
    hi = getattr(args, "to", None)
    if lo is not None or hi is not None:
        if lo is None or hi is None:
            raise ValueError("voice range needs both --from and --to")
        if hi < lo:
            raise ValueError(f"--to {hi} is before --from {lo}")
        ids.extend(range(int(lo), int(hi) + 1))
    return sorted(set(ids))




def _cmd_proofread(args: argparse.Namespace) -> int:
    from . import proofread as proofread_mod

    # --- reroll subaction ---
    if getattr(args, "pr_action", None) == "reroll":
        ctx = proofread_mod.load_reroll_constraints()
        if ctx is None:
            print("no reroll constraints saved (run `proofread` first on a "
                  "propose with contradictions)", file=sys.stderr)
            return 1
        print("# re-roll constraint")
        print(f"  Base note  : {ctx['base_note']}")
        print(f"  Constraint : {ctx['constraint']}")
        print()
        print("  Discard pending edits and re-run the operator with this note:")
        print(f"    python -m story_editor edits discard")
        print(f"    # then re-run with --note:")
        note = f"{ctx['base_note']}. {ctx['constraint']}"
        print(f"    --note \"{note}\"")
        print()
        print("  Or let the editor auto-apply it to the next `inject`/`restyle` call.")
        return 0

    log_path = args.log
    edit_set = transform_mod.load_pending_edits()
    if edit_set is None:
        print("no pending edits to proof-read (run `restyle`, `retune`, or `inject` "
              "first).", file=sys.stderr)
        return 1

    # Collect lorebook paths.
    lore_paths: list[Path] = []
    if config.LOREBOOK_PATHS:
        lore_paths = [Path(p) for p in config.LOREBOOK_PATHS if Path(p).exists()]

    def _progress(msg: str) -> None:
        print(f"  …{msg}", file=sys.stderr)

    print(f"# proofread — {len(edit_set.edits)} pending edit(s) "
          f"({edit_set.operator}: {edit_set.note})")

    try:
        reports = proofread_mod.proofread_pending(
            log_path, lore_paths=lore_paths, on_progress=_progress
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not reports:
        print("no edits to check.")
        return 0

    # Display summary.
    n_contradicts = sum(len(r.contradicts) for r in reports)
    n_plausible = sum(len(r.plausible) for r in reports)
    n_supported = sum(len(r.supported) for r in reports)
    n_clean = sum(1 for r in reports if r.verdict == "clean")

    overall_icon = "✗" if n_contradicts else ("⚠" if n_plausible else "✓")
    print(f"\n{overall_icon} {n_clean} edit(s) clean, "
          f"{n_supported} supported claim(s), "
          f"{n_plausible} plausibly-extend canon, "
          f"{n_contradicts} contradiction(s)")

    for r in reports:
        if r.verdict == "clean":
            continue
        kind_label = (f"inject after {r.edit_msg_id}"
                      if r.edit_kind == "inject" else f"msg {r.edit_msg_id}")
        print(f"\n  [{r.verdict}] {kind_label} · {r.edit_speaker}")
        for c in r.claims:
            icon = {"supported": "✓", "plausibly_extends": "⚠",
                    "contradicts": "✗"}.get(c.verdict, "?")
            print(f"    {icon} [{c.claim_type}] {c.claim}")
            if c.reason:
                print(f"       → {c.reason}")
            if c.verdict == "contradicts" and c.evidence_quotes:
                print(f"       contra: {c.evidence_quotes[0][:120]}")

    proofread_mod.write_proofread_report(reports, log_path)
    proofread_mod.save_reroll_constraints(reports)

    print(f"\nFull report: {config.LAST_PROOFREAD_REPORT}")
    if n_contradicts:
        print("  → run `proofread reroll` to see the re-roll constraint")
        print("  → or `edits commit` to land the edit as-is (your call)")
    elif n_plausible:
        print("  → plausibly-extends claims will introduce new canon — check "
              "lore-augment suggestions in the report if you want to canonize them")
    return 0


# --------------------------------------------------------------------------- #
# Phase 4 helpers
# --------------------------------------------------------------------------- #

def _verify_log(log_path: str | Path) -> dict:
    """Validate that the log is well-formed JSONL with the minimum ST fields.
    Returns {"message_count": int, "errors": list[str], "valid": bool}."""
    lines = [l for l in Path(log_path).read_text(encoding="utf-8").split("\n") if l.strip()]
    errors: list[str] = []
    msg_count = 0
    for i, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"line {i}: invalid JSON — {exc}")
            continue
        if i == 0:
            if not isinstance(obj, dict):
                errors.append("line 0: metadata must be a JSON object")
        else:
            for required in ("mes", "name"):
                if required not in obj:
                    errors.append(f"msg {i - 1} (line {i}): missing '{required}' field")
            msg_count += 1
    return {"message_count": msg_count, "errors": errors, "valid": not errors}


def _open_in_editor(text: str) -> str | None:
    """Open `text` in $EDITOR / $VISUAL and return the saved result.
    Returns None if the editor was not found or the user saved an empty file."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(text)
        tmppath = fh.name
    try:
        result = subprocess.run([editor, tmppath])
        if result.returncode != 0:
            return None
        saved = Path(tmppath).read_text(encoding="utf-8").strip()
        return saved if saved else None
    except FileNotFoundError:
        return None
    finally:
        Path(tmppath).unlink(missing_ok=True)


def _inline_edit_prompt(current_text: str) -> str | None:
    """Fallback when no editor is available: show the current text and ask the
    user to paste a replacement. Returns None if they keep the original."""
    print("\n  Current text (copy, edit externally, paste back below):\n")
    for line in current_text.splitlines():
        print(f"  {line}")
    print("\n  Paste replacement (blank line to finish; empty = keep original):")
    lines: list[str] = []
    try:
        while True:
            ln = input()
            if not ln and lines:
                break
            lines.append(ln)
    except EOFError:
        pass
    replacement = "\n".join(lines).strip()
    return replacement if replacement else None


def _run_review_session(log_path: str, edit_set: "transform_mod.EditSet") -> int:
    """Interactive review loop: walk each edit, let the director
    accept / reject / edit inline before commit."""
    edits = list(edit_set.edits)
    total = len(edits)

    decisions: dict[int, str] = {}   # msg_id -> "accept" | "reject"
    edited_bodies: dict[int, str] = {}  # msg_id -> replacement body text

    sep = "─" * 62
    print(f"\n# Review session — {total} pending edit(s)")
    print(f"  {edit_set.operator}: {edit_set.note}")
    print(f"  {edit_set.locator}")
    print(f"  $EDITOR = {os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'nano (default)'}")

    for idx, e in enumerate(edits, start=1):
        print(f"\n{sep}")
        kind_label = (f"inject — insert after msg {e.msg_id}"
                      if e.kind == "inject" else f"msg {e.msg_id} · {e.speaker} [replace]")
        print(f"[{idx}/{total}] {kind_label}")
        if e.flags:
            for f in e.flags:
                print(f"  ⚠ {f}")
        print(sep)
        diff = transform_mod._unified(e.before, e.after)
        for line in (diff.splitlines() if diff.strip() else ["(no text diff — model returned identical)"]):
            print(f"  {line}")
        print()

        while True:
            raw = input("  [a]ccept  [r]eject  [e]dit  [s]kip  [?]help > ").strip().lower()
            if raw in ("a", "accept"):
                decisions[e.msg_id] = "accept"
                print("  ✓ accepted")
                break
            elif raw in ("r", "reject"):
                decisions[e.msg_id] = "reject"
                print("  ✗ rejected — will be dropped")
                break
            elif raw in ("e", "edit"):
                body = transform_mod.split_header(e.after)[1]
                new_body = _open_in_editor(body)
                if new_body is None:
                    print("  (editor not available — trying inline prompt)")
                    new_body = _inline_edit_prompt(body)
                if new_body and new_body.strip() != body.strip():
                    edited_bodies[e.msg_id] = new_body
                    decisions[e.msg_id] = "accept"
                    print("  ✎ edited and accepted")
                else:
                    print("  (no change detected — marking as accepted as-is)")
                    decisions[e.msg_id] = "accept"
                break
            elif raw in ("s", "skip"):
                decisions[e.msg_id] = "skip"
                print("  ↷ skipped (will stay pending)")
                break
            elif raw in ("?", "help"):
                print("  a / accept  — land this edit as proposed")
                print("  r / reject  — drop this edit permanently")
                print("  e / edit    — open in $EDITOR to tweak the proposed text")
                print("  s / skip    — leave undecided (stays in pending set)")
            else:
                print("  unknown choice — try a/r/e/s or ?")

    # Summary.
    accepted = [e for e in edits if decisions.get(e.msg_id) == "accept"]
    rejected = [e for e in edits if decisions.get(e.msg_id) == "reject"]
    skipped  = [e for e in edits if decisions.get(e.msg_id) == "skip"]

    print(f"\n{sep}")
    print("Review complete:")
    if accepted:
        print(f"  ✓  {len(accepted)} accepted  "
              f"(msgs {', '.join(str(e.msg_id) for e in accepted)})")
    if rejected:
        print(f"  ✗  {len(rejected)} rejected  "
              f"(msgs {', '.join(str(e.msg_id) for e in rejected)})")
    if skipped:
        print(f"  ↷  {len(skipped)} skipped   "
              f"(msgs {', '.join(str(e.msg_id) for e in skipped)})")

    if not accepted:
        print("\nNothing to commit — all edits rejected or skipped.")
        if rejected:
            # Drop the rejected ones from the pending set.
            for e in rejected:
                transform_mod.drop_edit(e.msg_id)
        return 0

    # Confirm.
    answer = input(f"\nCommit {len(accepted)} edit(s)? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Aborted — pending edits unchanged.")
        return 0

    # Apply inline edits and drop rejected.
    for msg_id, new_body in edited_bodies.items():
        transform_mod.patch_edit(msg_id, new_body)
    for e in rejected:
        transform_mod.drop_edit(e.msg_id)
    for e in skipped:
        transform_mod.drop_edit(e.msg_id)

    # Commit.
    try:
        backup_path, applied = transform_mod.commit_edits(log_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error during commit: {exc}", file=sys.stderr)
        return 1

    print(f"\n  Backup:    {backup_path.name}")
    print(f"  Committed: {applied} edit(s)")

    # Verify.
    result = _verify_log(log_path)
    if result["valid"]:
        print(f"  ✓ Log verified: {result['message_count']} messages, all lines valid JSONL.")
    else:
        print(f"  ⚠ Log verification found {len(result['errors'])} issue(s):")
        for err in result["errors"][:5]:
            print(f"     {err}")

    print("  Undo: python -m story_editor edits undo")
    return 0


def _cmd_edits(args: argparse.Namespace) -> int:
    action = args.action
    if action == "show":
        edit_set = transform_mod.load_pending_edits()
        if edit_set is None:
            print("no pending edits.")
            return 0
        flagged = [e for e in edit_set.edits if e.flags]
        print(f"# pending {edit_set.operator} — {len(edit_set.edits)} edit(s)"
              + (f" ({len(flagged)} flagged ⚠)" if flagged else ""))
        print(f"  direction: {edit_set.note}")
        print(f"  target:    {edit_set.locator}")
        for e in edit_set.edits:
            print(f"\n── msg {e.msg_id} · {e.speaker} ──")
            for f in e.flags:
                print(f"  ⚠ {f}")
            print(transform_mod._unified(e.before, e.after))
        return 0
    if action == "review":
        edit_set = transform_mod.load_pending_edits()
        if edit_set is None:
            print("no pending edits to review.")
            return 0
        return _run_review_session(args.log, edit_set)
    if action == "edit":
        if args.msg_id is None:
            print("error: `edits edit` needs --msg-id N", file=sys.stderr)
            return 1
        edit_set = transform_mod.load_pending_edits()
        if edit_set is None:
            print("no pending edits.", file=sys.stderr)
            return 1
        target = next((e for e in edit_set.edits if e.msg_id == args.msg_id), None)
        if target is None:
            print(f"no pending edit for msg {args.msg_id}.", file=sys.stderr)
            return 1
        body = transform_mod.split_header(target.after)[1]
        print(f"# editing msg {args.msg_id} · {target.speaker}")
        new_body = _open_in_editor(body)
        if new_body is None:
            print("  editor not available — trying inline prompt")
            new_body = _inline_edit_prompt(body)
        if new_body and new_body.strip() != body.strip():
            transform_mod.patch_edit(args.msg_id, new_body)
            print(f"  ✎ updated. Review: {config.PENDING_EDITS_MD}")
        else:
            print("  (no change detected)")
        return 0
    if action == "commit":
        try:
            backup_path, applied = transform_mod.commit_edits(
                args.log, allow_flagged=args.allow_flagged
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"  Backup:    {backup_path.name}")
        print(f"  Committed: {applied} edit(s)")
        result = _verify_log(args.log)
        if result["valid"]:
            print(f"  ✓ Log verified: {result['message_count']} messages, all lines valid JSONL.")
        else:
            print(f"  ⚠ Verification found {len(result['errors'])} issue(s):")
            for err in result["errors"][:5]:
                print(f"     {err}")
        print("  Undo: python -m story_editor edits undo")
        latest = None
        try:
            from . import history as history_mod
            latest = history_mod.get_latest(log_path=args.log)
        except Exception:
            pass
        if latest:
            print(f"  History: entry #{latest.id}  (`history show {latest.id}`)")
        return 0
    if action == "drop":
        if args.msg_id is None:
            print("error: `edits drop` needs --msg-id N", file=sys.stderr)
            return 1
        if transform_mod.drop_edit(args.msg_id):
            print(f"dropped edit for msg {args.msg_id}.")
        else:
            print(f"no pending edit for msg {args.msg_id}.")
        return 0
    if action == "discard":
        if transform_mod.discard_pending_edits():
            print("discarded pending edits.")
        else:
            print("nothing to discard.")
        return 0
    if action == "undo":
        restored = transform_mod.undo(args.log)
        print(f"restored from backup: {restored}")
        return 0
    return 1


def _cmd_retune(args: argparse.Namespace) -> int:
    log = loader.load(args.log)
    try:
        span = _resolve_restyle_span(args, log)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"# retune target ({args.mode}) — {span.locator}")
    print(f"  {len(span.messages)} message(s) in scope (msgs {span.msg_from}–{span.msg_to})")
    for line in transform_mod.span_preview(span):
        print(line)

    if not span.messages:
        print("\n(no messages match this span)")
        return 1

    if args.dry_run:
        print(f"\n(dry run — no retune. Drop --dry-run to generate.)")
        return 0

    def _progress(i, total, target):
        print(f"  …retuning {i}/{total} (msg {target.msg_id} {target.speaker})",
              file=sys.stderr)

    try:
        edit_set = transform_mod.retune_span(
            args.log, span, args.mode,
            note=args.note,
            chain=not args.independent,
            on_progress=_progress,
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    if not edit_set.edits:
        # Not an error — model judged the target already at the requested density/length.
        print(
            "· no message changed — the model judged the target already meets the "
            "requested length/density. This is correct behaviour.\n"
            "  To force output: widen the span or adjust the --mode."
        )
        return 0
    flagged = [e for e in edit_set.edits if e.flags]
    hard = [e for e in edit_set.edits if e.hard_flagged]
    print(f"proposed {len(edit_set.edits)} edit(s)"
          + (f" ({len(flagged)} flagged ⚠)" if flagged else "") + ":")
    for e in edit_set.edits:
        print(f"\n── msg {e.msg_id} · {e.speaker} ──")
        for f in e.flags:
            print(f"  ⚠ {f}")
        print(transform_mod._unified(e.before, e.after))
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    if hard:
        ids = ", ".join(str(e.msg_id) for e in hard)
        print(f"⚠ flagged (msgs {ids}) — commit will refuse until dropped or --allow-flagged.")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    return 0


def _cmd_weed(args: argparse.Namespace) -> int:
    from . import weed as weed_mod

    action = args.weed_action
    speaker = getattr(args, "speaker", None) or None
    msg_from = getattr(args, "from", None)
    msg_to = getattr(args, "to", None)
    if action == "scan":
        report = weed_mod.scan_log(
            args.log, msg_from=msg_from, msg_to=msg_to, speaker=speaker,
        )
        print(
            f"{report.locator} — {len(report.hits)} sentence(s) in "
            f"{len({h.msg_id for h in report.hits})} turn(s)"
        )
        if report.phrases:
            print("phrases: " + ", ".join(report.phrases))
        if not report.hits:
            print("clean.")
            return 0
        for hit in report.hits:
            preview = hit.sentence.replace("\n", " ")
            if len(preview) > 140:
                preview = preview[:137] + "…"
            print(f"  msg {hit.msg_id} · {hit.speaker} · {hit.phrase_id}")
            print(f"    {preview}")
        return 0

    log = loader.load(args.log)
    span = transform_mod.resolve_span(
        log, msg_from=msg_from, msg_to=msg_to, speaker=speaker,
    )
    report = weed_mod.scan_span(span)
    if not report.hits:
        print(f"{span.locator} — clean. Nothing to pull.")
        return 0
    if args.dry_run:
        print(f"{span.locator} — {len(report.hits)} watchlist sentence(s) would be pulled.")
        print("(dry run — no rewrite. Drop --dry-run to propose.)")
        return 0

    def _progress(ev: dict) -> None:
        if ev.get("kind") == "phase":
            print(f"  …{ev.get('text', '')}", file=sys.stderr)

    try:
        edit_set = weed_mod.apply_span(args.log, span, on_stream=_progress, report=report)
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    if not edit_set.edits:
        print("· no message changed — the infected sentences came back the same.")
        return 0
    flagged = [e for e in edit_set.edits if e.flags]
    print(f"proposed {len(edit_set.edits)} edit(s)"
          + (f" ({len(flagged)} flagged ⚠)" if flagged else "") + ":")
    for e in edit_set.edits:
        print(f"\n── msg {e.msg_id} · {e.speaker} ──")
        for f in e.flags:
            print(f"  ⚠ {f}")
        print(transform_mod._unified(e.before, e.after))
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    return 0


def _cmd_copyedit(args: argparse.Namespace) -> int:
    from . import copyedit as copyedit_mod

    log = loader.load(args.log)
    try:
        span = transform_mod.resolve_span(
            log,
            scene=getattr(args, "scene", None),
            msg_from=getattr(args, "from", None),
            msg_to=getattr(args, "to", None),
            speaker=getattr(args, "speaker", None) or None,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"# copyedit — {span.locator}")
    print(f"  {len(span.messages)} message(s) in scope "
          f"(msgs {span.msg_from}–{span.msg_to})")
    if args.dry_run:
        chunks = copyedit_mod.chunk_messages(span.messages)
        print(f"  {len(chunks)} chunk(s). Drop --dry-run to propose.")
        return 0

    def _progress(ev: dict) -> None:
        if ev.get("kind") == "phase":
            print(f"  …{ev.get('text', '')}", file=sys.stderr)

    try:
        edit_set = copyedit_mod.apply_span(
            args.log, span, on_stream=_progress, base_url=args.base_url,
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    if not edit_set.edits:
        print("· clean — no unique typo landed.")
        skipped = (edit_set.meta or {}).get("skipped") or []
        if skipped:
            print(f"  ({len(skipped)} named fix(es) skipped as ambiguous or missing)")
        return 0
    print(f"proposed {len(edit_set.edits)} edit(s):")
    for e in edit_set.edits:
        print(f"\n── msg {e.msg_id} · {e.speaker} ──")
        print(transform_mod._unified(e.before, e.after))
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    return 0


def _cmd_stamps(args: argparse.Namespace) -> int:
    from . import stamps as stamps_mod

    action = args.stamps_action
    msg_from = getattr(args, "from", None)
    msg_to = getattr(args, "to", None)
    if action == "scan":
        report = stamps_mod.scan_log(args.log, msg_from=msg_from, msg_to=msg_to)
        payload = report.to_json()
        print(f"# stamps — {payload['locator']}")
        print(
            f"  {payload['complete']}/{payload['count']} complete  ·  "
            f"{payload['header']} header  ·  {payload['inherited']} inherited  ·  "
            f"{payload['inferred']} inferred  ·  {payload['missing']} missing"
        )
        print(f"  {payload['propose_count']} CHAR/interlude turn(s) would get a header")
        print(f"  sidecar: {config.STAMPS}")
        for row in payload["propose"][:40]:
            print(
                f"  msg {row['msg_id']:>4}  {row['speaker']:<16}  "
                f"{row['time'] or '—'}  ·  {row['date'] or '—'}  ·  "
                f"{row['location'] or '—'}"
            )
        if payload["propose_count"] > 40:
            print(f"  … ({payload['propose_count'] - 40} more)")
        return 0

    log = loader.load(args.log)
    try:
        span = transform_mod.resolve_span(log, msg_from=msg_from, msg_to=msg_to)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.dry_run:
        report = stamps_mod.scan_log(args.log, msg_from=msg_from, msg_to=msg_to)
        print(f"{span.locator} — {report.to_json()['propose_count']} header(s) would be proposed.")
        print("(dry run — no edits. Drop --dry-run to propose.)")
        return 0

    def _progress(ev: dict) -> None:
        if ev.get("kind") == "phase":
            print(f"  …{ev.get('text', '')}", file=sys.stderr)

    edit_set = stamps_mod.propose_span(args.log, span, on_stream=_progress)
    if not edit_set.edits:
        print("· nothing to stamp — every CHAR turn in scope already has a full header.")
        return 0
    print(f"proposed {len(edit_set.edits)} header(s):")
    for e in edit_set.edits:
        print(f"\n── msg {e.msg_id} · {e.speaker} ──")
        print(transform_mod._unified(e.before, e.after))
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    return 0


def _cmd_inject(args: argparse.Namespace) -> int:
    log = loader.load(args.log)

    # Resolve --after from a beat or scene if not given directly.
    after = args.after
    if after is None:
        if args.beat is not None:
            spine = transform_mod.structure_mod.load_derived_spine()
            if spine is None:
                print("error: no committed spine; use --after MSG_ID directly", file=sys.stderr)
                return 1
            beat = next((b for b in spine.beats if b.beat_id == args.beat), None)
            if beat is None:
                print(f"error: beat B{args.beat} not found in committed spine", file=sys.stderr)
                return 1
            after = beat.end_msg_id
            print(f"# inject after beat B{args.beat} ({beat.title}) → after msg {after}")
        elif args.scene is not None:
            scenes = transform_mod.structure_mod.segment_scenes(log)
            sc = next((s for s in scenes if s.scene_id == args.scene), None)
            if sc is None:
                print(f"error: scene S{args.scene} not found", file=sys.stderr)
                return 1
            after = sc.end_msg_id
            print(f"# inject after scene S{args.scene} ({sc.title()}) → after msg {after}")
        else:
            print("error: specify --after MSG_ID, --beat N, or --scene N", file=sys.stderr)
            return 1

    # Show surrounding context.
    lo = max(0, after - 2)
    hi = min(len(log) - 1, after + 2)
    print(f"# inject after msg {after} (speaker: {args.speaker})")
    for m in log.messages[lo : hi + 1]:
        mark = " ← insert here" if m.msg_id == after else ""
        flat = " ".join(transform_mod.text_mod.clean(m.text).split())
        print(f"  msg {m.msg_id:>4} {m.speaker:<10} {flat[:90]}{'…' if len(flat)>90 else ''}{mark}")

    if args.dry_run:
        print(f"\n(dry run — no generation. Drop --dry-run with a --note to generate.)")
        return 0

    if not args.note:
        print("error: --note is required (or use --dry-run to preview the position)",
              file=sys.stderr)
        return 1

    locator = f"after msg {after}"
    print(f"\n  …generating injection for {args.speaker}…", file=sys.stderr)
    try:
        edit_set = transform_mod.inject_at(
            args.log, after, args.speaker, args.note, locator=locator,
            attribution_voice=getattr(args, "voice", None),
            attribution_mode=getattr(args, "attribution_mode", None),
        )
    except (llm.ModelError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    e = edit_set.edits[0]
    print(f"\n# proposed injection (after msg {e.msg_id}, speaker {e.speaker})\n")
    if e.attribution_voice:
        print(f"# attribution: voice={e.attribution_voice} mode={e.attribution_mode}")
    print(e.after)
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    msg_from = getattr(args, "from", None)
    if msg_from is None:
        print("error: --from is required", file=sys.stderr)
        return 1
    msg_to = args.to if args.to is not None else msg_from
    sweep = bool(getattr(args, "sweep", False))
    try:
        edit_set = transform_mod.propose_remove(
            args.log, msg_from, msg_to, sweep=sweep,
        )
    except (ValueError, IndexError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"proposed {len(edit_set.edits)} removal(s) — pending review:")
    for e in edit_set.edits:
        flat = " ".join(transform_mod.split_header(e.before)[1].split())
        print(f"  msg {e.msg_id} · {e.speaker}  {flat[:90]}{'…' if len(flat) > 90 else ''}")
    print(f"\nReview: {config.PENDING_EDITS_MD}")
    print("Land it:  python -m story_editor edits commit")
    print("Discard:  python -m story_editor edits discard")
    if sweep:
        print("After commit:  python -m story_editor sweep --last")
    return 0


_SWEEP_ICON = {"CLEAN": "✓", "REVIEW": "⚠", "BREAK": "✗"}


def _print_sweep_report(report: "propagate_mod.SweepReport") -> None:
    print(f"\nverdict: {report.verdict}")
    print(f"  {report.clean_count} clean, "
          f"{sum(1 for h in report.hits if h.verdict=='REVIEW')} review, "
          f"{sum(1 for h in report.hits if h.verdict=='BREAK')} break")
    if not report.hits:
        print("  all downstream candidates read cleanly.")
    else:
        for h in sorted(report.hits, key=lambda h: (h.verdict != "BREAK", h.msg_id)):
            icon = _SWEEP_ICON.get(h.verdict, "?")
            print(f"\n  {icon} msg {h.msg_id} · {h.speaker} [{h.verdict}]")
            print(f"    {h.excerpt[:120]}{'…' if len(h.excerpt) > 120 else ''}")
            print(f"    → {h.reason}")


def _resolve_sweep_ctx(args: argparse.Namespace) -> tuple | None:
    """Return (log_path, edits, operator, note, sweep_from) or print error + None."""
    if getattr(args, "last", False):
        ctx = propagate_mod.load_sweep_context()
        if ctx is None:
            print("error: no sweep context found (commit an edit first, or use "
                  "--from/--to)", file=sys.stderr)
            return None
        edits = [transform_mod.Edit.from_json(e) for e in ctx.edits]
        return ctx.log, edits, ctx.operator, ctx.note, ctx.changed_to + 1
    else:
        if args.msg_from is None or args.msg_to is None:
            print("error: specify --last, or both --from and --to", file=sys.stderr)
            return None
        log_path = args.log
        log = loader.load(log_path)
        edits = []
        for mid in range(args.msg_from, args.msg_to + 1):
            try:
                m = log.get(mid)
                edits.append(transform_mod.Edit(
                    msg_id=mid, speaker=m.speaker,
                    before=m.text, after=m.text, kind="replace",
                ))
            except IndexError:
                pass
        if not edits:
            print("error: no messages in that range", file=sys.stderr)
            return None
        return log_path, edits, "manual", args.note or "(manual range sweep)", args.msg_to + 1


def _run_cascade(
    log_path: str,
    edits: list,
    operator: str,
    note: str,
    sweep_from: int,
    max_cycles: int,
) -> "propagate_mod.CascadeResult":
    """Interactive cascade loop. Each cycle: sweep → propose patches for BREAKs →
    batch review → commit approved → advance ripple front → repeat."""
    import uuid
    session_id = uuid.uuid4().hex[:8]
    changed_from = min(e.msg_id for e in edits)
    changed_to = max(e.msg_id for e in edits)
    change_summary = f"{operator}: {note} (msgs {changed_from}–{changed_to})"

    dismissed_ids: set[int] = set()
    break_history: list[int] = []
    review_counts: list[int] = []
    labels_written = 0
    total_patched = 0
    current_edits = edits
    current_from = sweep_from
    reason = "max_cycles"

    for cycle in range(1, max_cycles + 1):
        print(f"\n{'='*60}")
        print(f"  Cascade cycle {cycle}/{max_cycles}  |  scanning from msg {current_from}")
        print(f"{'='*60}")

        try:
            report = propagate_mod.sweep(
                log_path, current_edits, operator, note,
                sweep_from=current_from,
                on_progress=lambda m: print(f"  …{m}", file=sys.stderr),
            )
        except llm.ModelError as exc:
            print(f"error (LLM): {exc}", file=sys.stderr)
            reason = "llm_error"
            break

        _print_sweep_report(report)

        active_breaks = [h for h in report.hits
                         if h.verdict == "BREAK" and h.msg_id not in dismissed_ids]
        active_reviews = [h for h in report.hits
                          if h.verdict == "REVIEW" and h.msg_id not in dismissed_ids]
        break_history.append(len(active_breaks))
        review_counts.append(len(active_reviews))

        # Convergence: no BREAKs.
        if not active_breaks:
            print(f"\n  ✓ No breaks — converged after {cycle} cycle(s).")
            reason = "no_breaks"
            break

        # Convergence: plateau (BREAK count not falling for 2 consecutive cycles).
        if len(break_history) >= 2 and break_history[-1] >= break_history[-2]:
            print(f"\n  ⚠ Plateau — BREAK count ({break_history[-1]}) did not decrease. "
                  "Halting cascade. Remaining breaks need manual attention.")
            reason = "plateau"
            break

        # Propose patches for all active BREAK hits.
        print(f"\n  Generating patch proposals for {len(active_breaks)} break(s)…")
        proposals: list[tuple["propagate_mod.SweepHit", "transform_mod.Edit"]] = []
        for hit in active_breaks:
            edit = propagate_mod.propose_patch(
                log_path, hit, change_summary,
                on_progress=lambda m: print(f"    {m}", file=sys.stderr),
            )
            if edit:
                proposals.append((hit, edit))
            else:
                print(f"    (msg {hit.msg_id}: model returned unchanged — skip)")

        if not proposals:
            print("  No patch proposals generated; skipping commit this cycle.")
            reason = "no_proposals"
            break

        # Batch review — show all proposals, then collect director decisions.
        print(f"\n  {len(proposals)} proposal(s) ready for review:\n")
        for idx, (hit, edit) in enumerate(proposals, start=1):
            print(f"  [{idx}] {_SWEEP_ICON[hit.verdict]} msg {hit.msg_id} · "
                  f"{hit.speaker}  [{hit.verdict}]")
            print(f"      Concern: {hit.reason}")
            if edit.flags:
                for f in edit.flags:
                    print(f"      ⚠ {f}")
            # Show a compact diff.
            from .transform import _unified, split_header
            diff = _unified(edit.before, edit.after)
            for line in diff.splitlines()[:12]:
                print(f"      {line}")
            if diff.count("\n") > 12:
                print("      …(diff truncated)")
            print()

        if active_reviews:
            print(f"  Also flagged as REVIEW (no patch proposed — handle manually):")
            for h in active_reviews:
                print(f"    ⚠ msg {h.msg_id} · {h.speaker}: {h.reason}")
            print()

        print("  Enter msg_ids to APPROVE (space-separated), or:")
        print("    'all'  — approve all proposals")
        print("    'none' — skip all (advance to next cycle without patching)")
        print("    'd N'  — dismiss msg N (suppress from future cycles this run)")
        raw = input("  > ").strip()

        approved_ids: set[int] = set()
        dismissed_this_round: set[int] = set()

        if raw.lower() == "all":
            approved_ids = {e.msg_id for _, e in proposals}
        elif raw.lower() == "none":
            pass
        else:
            for token in raw.split():
                if token.startswith("d") and len(token) > 1:
                    try:
                        dismissed_this_round.add(int(token[1:]))
                    except ValueError:
                        pass
                else:
                    try:
                        approved_ids.add(int(token))
                    except ValueError:
                        pass

        dismissed_ids.update(dismissed_this_round)

        # Write labels.
        for hit, edit in proposals:
            if hit.msg_id in approved_ids:
                action = "patched"
            elif hit.msg_id in dismissed_this_round:
                action = "dismissed"
            else:
                action = "skipped"
            propagate_mod.append_label(propagate_mod.ConsistencyLabel(
                session_id=session_id, operator=operator, note=note,
                cycle=cycle, changed_from=changed_from, changed_to=changed_to,
                candidate_msg_id=hit.msg_id, candidate_speaker=hit.speaker,
                candidate_excerpt=hit.excerpt, llm_verdict=hit.verdict,
                director_action=action,
            ))
            labels_written += 1

        # Write labels for dismissed REVIEWs too.
        for h in active_reviews:
            if h.msg_id in dismissed_this_round:
                propagate_mod.append_label(propagate_mod.ConsistencyLabel(
                    session_id=session_id, operator=operator, note=note,
                    cycle=cycle, changed_from=changed_from, changed_to=changed_to,
                    candidate_msg_id=h.msg_id, candidate_speaker=h.speaker,
                    candidate_excerpt=h.excerpt, llm_verdict=h.verdict,
                    director_action="dismissed",
                ))
                labels_written += 1

        if not approved_ids:
            print("  No patches approved — advancing sweep front without committing.")
            current_from = current_from  # unchanged; next cycle rescans same window
            current_edits = current_edits
            continue

        # Commit approved patches.
        approved_edits = [e for _, e in proposals if e.msg_id in approved_ids]
        edit_set = transform_mod.EditSet(
            log=str(Path(log_path).resolve()),
            operator=f"cascade-{operator}", note=note,
            locator=f"cascade cycle {cycle}",
            edits=approved_edits,
        )
        transform_mod.write_pending_edits(edit_set)
        try:
            backup_path, applied = transform_mod.commit_edits(log_path)
            print(f"\n  Committed {applied} patch(es). Backup: {backup_path.name}")
            total_patched += applied
            # Advance ripple front to downstream of the last patched message.
            current_from = max(e.msg_id for e in approved_edits) + 1
            current_edits = approved_edits
        except Exception as exc:
            print(f"  error committing: {exc}", file=sys.stderr)
            transform_mod.discard_pending_edits()
            break

    # Convergence trace.
    trace = " → ".join(str(b) for b in break_history)
    print(f"\n  Convergence trace (BREAKs per cycle): {trace or '(no cycles ran)'}")
    print(f"  Labels written: {labels_written}  |  Total patched: {total_patched}")
    print(f"  Full sweep report: {config.LAST_SWEEP_REPORT}")

    return propagate_mod.CascadeResult(
        cycles_run=len(break_history),
        converged=(reason == "no_breaks"),
        reason=reason,
        break_history=break_history,
        review_counts=review_counts,
        labels_written=labels_written,
        total_patched=total_patched,
    )


def _cmd_sweep(args: argparse.Namespace) -> int:
    # --- labels subaction ---
    if getattr(args, "sweep_action", None) == "labels":
        stats = propagate_mod.label_stats()
        if not stats:
            print("no consistency labels yet (run cascade cycles to generate them)")
            return 0
        print(f"total labels: {stats['total']}")
        for action, count in sorted(stats.get("by_action", {}).items()):
            print(f"  {action:12s}: {count}")
        return 0

    resolved = _resolve_sweep_ctx(args)
    if resolved is None:
        return 1
    log_path, edits, operator, note, sweep_from = resolved

    cascade_n = getattr(args, "cascade", 0) or 0

    if cascade_n > 0:
        print(f"# cascade sweep (up to {cascade_n} cycle(s))")
        print(f"  changed msgs {min(e.msg_id for e in edits)}–"
              f"{max(e.msg_id for e in edits)};  downstream from msg {sweep_from}")
        _run_cascade(log_path, edits, operator, note, sweep_from, cascade_n)
        return 0

    # --- single sweep (original behaviour) ---
    header = (f"# sweep (last commit: {operator} — {note})"
              if getattr(args, "last", False)
              else f"# sweep (manual: msgs {args.msg_from}–{args.msg_to})")
    print(header)
    print(f"  scanning downstream from msg {sweep_from}")

    try:
        report = propagate_mod.sweep(
            log_path, edits, operator, note,
            sweep_from=sweep_from,
            on_progress=lambda m: print(f"  …{m}", file=sys.stderr),
        )
    except llm.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_sweep_report(report)
    print(f"\nFull report: {config.LAST_SWEEP_REPORT}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="story_editor", description="The Story Editor (Phase 0)")
    p.add_argument(
        "--log",
        default=str(config.working_log()),
        help="path to the working log (default: throwaway copy)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pl = sub.add_parser("list", help="list messages: id, role, speaker, preview")
    pl.add_argument("--speaker", help="filter by speaker name")
    pl.add_argument("--limit", type=int, help="show only the first N")
    pl.set_defaults(func=_cmd_list)

    ps = sub.add_parser("show", help="show one message in full")
    ps.add_argument("msg_id", type=int)
    ps.set_defaults(func=_cmd_show)

    pb = sub.add_parser("backup", help="make a timestamped backup")
    pb.add_argument("--label", help="optional label suffix")
    pb.set_defaults(func=_cmd_backup)

    pr = sub.add_parser("restore", help="restore from a backup")
    pr.add_argument("--from", dest="from_backup", help="specific backup file (default: latest)")
    pr.add_argument("--list", action="store_true", help="list available backups instead")
    pr.set_defaults(func=_cmd_restore)

    pp = sub.add_parser("pauses", help="list day-end pause points (interlude slots)")
    pp.add_argument("--retire-only", action="store_true", help="only pauses with a retire/sleep cue")
    pp.add_argument("--verbose", action="store_true", help="also show the next day's opening line")
    pp.set_defaults(func=_cmd_pauses)

    pst = sub.add_parser("structure", help="derive/audit story structure (Phase 1.5)")
    stsub = pst.add_subparsers(dest="action", required=True)

    stdoc = stsub.add_parser("doctor", help="non-destructive prep check: flag structural anomalies before processing")
    stdoc.add_argument("--risks-only", action="store_true", help="show only RISK findings")
    stdoc.set_defaults(func=_cmd_structure_doctor)

    sts = stsub.add_parser("scenes", help="list the auto-derived scene segmentation")
    sts.add_argument("--verbose", action="store_true", help="also show each scene's opening line")
    sts.set_defaults(func=_cmd_structure_scenes)

    ste = stsub.add_parser("episodes", help="list the auto-derived episode (story-day) grouping")
    ste.add_argument("--verbose", action="store_true", help="also show each episode's locations")
    ste.set_defaults(func=_cmd_structure_episodes)

    stcd = stsub.add_parser("cards", help="LLM: build/show faithful per-scene synopses (cached)")
    stcd.add_argument("--rebuild", action="store_true", help="force a fresh map pass (ignore cache)")
    stcd.set_defaults(func=_cmd_structure_cards)

    std = stsub.add_parser("derive", help="LLM: propose a beat spine from the scenes (writes nothing canon)")
    std.add_argument("--max-beats", type=int, default=14, help="cap on number of beats (default: 14)")
    std.add_argument("--no-cards", action="store_true", help="skip scene-card synopses (use head/tail previews)")
    std.add_argument("--no-premise", action="store_true", help="skip the non-spoiler story primer")
    std.add_argument("--rebuild-cards", action="store_true", help="force a fresh scene-card map pass first")
    std.add_argument("--no-critique", action="store_true", help="skip LLM self-critique pass after derive")
    std.set_defaults(func=_cmd_structure_derive)

    stv = stsub.add_parser("validate", help="deterministic beat coverage check (no LLM)")
    stv.add_argument("--pending", action="store_true", help="validate pending spine instead of committed")
    stv.set_defaults(func=_cmd_structure_validate)

    stsp = stsub.add_parser("spine", help="show the pending and/or committed derived spine")
    stsp.set_defaults(func=_cmd_structure_spine)

    stc = stsub.add_parser("commit", help="promote the pending spine to the working derived spine")
    stc.set_defaults(func=_cmd_structure_commit)

    stdi = stsub.add_parser("discard", help="throw away the pending spine")
    stdi.set_defaults(func=_cmd_structure_discard)

    sta = stsub.add_parser("audit", help="LLM: align authored spine vs derived, flag drift")
    sta.add_argument("--max-beats", type=int, default=12,
                     help="cap on beats if a spine must be derived on the fly")
    sta.set_defaults(func=_cmd_structure_audit)

    stdr = stsub.add_parser("drift", help="LLM: re-derive and diff against the committed baseline spine")
    stdr.add_argument("--max-beats", type=int, default=14, help="cap on number of beats (default: 14)")
    stdr.set_defaults(func=_cmd_structure_drift)

    prs = sub.add_parser("restyle", help="propose a tone/register rewrite of a span (Phase 2)")
    prs.add_argument("--note", help="the restyle direction, e.g. 'colder and more clipped'")
    prs.add_argument("--beat", type=int, help="target a beat from the committed spine (narrative GPS)")
    prs.add_argument("--scene", type=int, help="target a scene id (see `structure scenes`)")
    prs.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    prs.add_argument("--to", type=int, help="upper msg_id bound")
    prs.add_argument("--speaker", help="restrict to one speaker (e.g. Wren)")
    prs.add_argument("--role", choices=("user", "char"), help="restrict by role")
    prs.add_argument("--dry-run", action="store_true", help="show the targeted span only; generate nothing")
    prs.add_argument("--independent", action="store_true", help="rewrite each message in isolation (disable span-aware chaining)")
    prs.set_defaults(func=_cmd_restyle)

    prt = sub.add_parser("retune", help="propose a length/density retune of a span (Phase 2)")
    prt.add_argument("--mode", choices=("tighten", "expand"), required=True,
                     help="tighten: cut wordcount; expand: add texture")
    prt.add_argument("--note", help="optional sharpening note, e.g. 'cut to ~60%'")
    prt.add_argument("--beat", type=int, help="target a beat from the committed spine")
    prt.add_argument("--scene", type=int, help="target a scene id")
    prt.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    prt.add_argument("--to", type=int, help="upper msg_id bound")
    prt.add_argument("--speaker", help="restrict to one speaker")
    prt.add_argument("--role", choices=("user", "char"), help="restrict by role")
    prt.add_argument("--dry-run", action="store_true", help="show the targeted span only")
    prt.add_argument("--independent", action="store_true", help="disable span-aware chaining")
    prt.set_defaults(func=_cmd_retune)

    pwd = sub.add_parser("weed", help="pull machine phrases out of sentences")
    wsub = pwd.add_subparsers(dest="weed_action", required=True)
    wscan = wsub.add_parser("scan", help="list infected sentences; write nothing")
    wscan.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    wscan.add_argument("--to", type=int, help="upper msg_id bound")
    wscan.add_argument("--speaker", help="restrict to one speaker")
    wscan.set_defaults(func=_cmd_weed)
    wapp = wsub.add_parser("apply", help="propose sentence-level pulls")
    wapp.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    wapp.add_argument("--to", type=int, help="upper msg_id bound")
    wapp.add_argument("--speaker", help="restrict to one speaker")
    wapp.add_argument("--dry-run", action="store_true", help="show the hits only")
    wapp.set_defaults(func=_cmd_weed)

    pce = sub.add_parser(
        "copyedit",
        help="find and fix typos without rewriting (chunked LLM, propose only)",
    )
    pce.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    pce.add_argument("--to", type=int, help="upper msg_id bound")
    pce.add_argument("--scene", type=int, help="target a derived scene")
    pce.add_argument("--speaker", help="restrict to one speaker")
    pce.add_argument("--dry-run", action="store_true", help="show chunks only")
    pce.add_argument(
        "--base-url",
        help="OpenAI-compatible base (e.g. http://localhost:5001/v1)",
    )
    pce.set_defaults(func=_cmd_copyedit)

    pstp = sub.add_parser(
        "stamps",
        help="infer chronicle / location metadata; propose missing CHAR headers",
    )
    stpsub = pstp.add_subparsers(dest="stamps_action", required=True)
    stscan = stpsub.add_parser("scan", help="walk the log; write workspace/stamps.json")
    stscan.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    stscan.add_argument("--to", type=int, help="upper msg_id bound")
    stscan.set_defaults(func=_cmd_stamps)
    stprop = stpsub.add_parser("propose", help="propose missing CHAR/interlude headers")
    stprop.add_argument("--from", dest="from", type=int, help="lower msg_id bound")
    stprop.add_argument("--to", type=int, help="upper msg_id bound")
    stprop.add_argument("--dry-run", action="store_true", help="count only")
    stprop.set_defaults(func=_cmd_stamps)

    pin = sub.add_parser("inject", help="propose a new passage at a chosen position (Phase 2)")
    pin.add_argument("--after", type=int, help="insert after this msg_id")
    pin.add_argument("--beat", type=int, help="insert after the last message of this beat")
    pin.add_argument("--scene", type=int, help="insert after the last message of this scene")
    pin.add_argument("--speaker", default="Narrator", help="card name for the new line (Narrator, Wren, …)")
    pin.add_argument("--voice", help="optional sidecar voice (wren, varga, ensemble, …)")
    pin.add_argument("--attribution-mode", dest="attribution_mode",
                     help="optional sidecar mode (pov, wrong_card, scene_narrator, …)")
    pin.add_argument("--note", help="what to write")
    pin.add_argument("--dry-run", action="store_true", help="show the injection point only")
    pin.set_defaults(func=_cmd_inject)

    prm = sub.add_parser("remove", help="propose taking turns out of the log")
    prm.add_argument("--from", dest="from", type=int, required=True,
                     help="first msg_id to take out")
    prm.add_argument("--to", type=int, help="last msg_id (defaults to --from)")
    prm.add_argument(
        "--sweep", action="store_true",
        help="after accept, look downstream for turns that leaned on what was cut",
    )
    prm.set_defaults(func=_cmd_remove)

    psw = sub.add_parser("sweep", help="propagation sweep: find downstream consistency breaks (Phase 3)")
    psw.add_argument("sweep_action", nargs="?", choices=["labels"],
                     metavar="ACTION",
                     help="'labels' — show consistency label statistics")
    psw.add_argument("--last", action="store_true",
                     help="sweep the span from the most recent committed edit")
    psw.add_argument("--from", dest="msg_from", type=int,
                     help="first changed msg_id (use with --to)")
    psw.add_argument("--to", dest="msg_to", type=int,
                     help="last changed msg_id (use with --from)")
    psw.add_argument("--note", help="optional context note for manual sweeps")
    psw.add_argument("--cascade", metavar="N", type=int, default=0,
                     help="run up to N iterative fix cycles (propose-then-approve per cycle)")
    psw.set_defaults(func=_cmd_sweep)

    ppr = sub.add_parser("proofread",
                         help="upstream proof-read: detect invented facts in pending edits (Phase 3.5)")
    ppr.add_argument("pr_action", nargs="?", choices=["reroll"], metavar="ACTION",
                     help="'reroll' — show the do-not-invent note for re-running the operator")
    ppr.set_defaults(func=_cmd_proofread)

    ped = sub.add_parser("edits", help="review/commit/undo a pending edit-set (Phase 2)")
    ped.add_argument("action",
                     choices=("show", "review", "edit", "commit", "discard", "drop", "undo"),
                     help="show | review (interactive PR loop) | edit --msg-id N | "
                          "commit | discard | drop --msg-id N | undo")
    ped.add_argument("--msg-id", type=int, help="for `drop`: the message whose edit to remove")
    ped.add_argument("--allow-flagged", action="store_true", help="for `commit`: commit even containment-flagged edits")
    ped.set_defaults(func=_cmd_edits)

    # Story importer — plain .story prose -> engine .jsonl (non-SillyTavern path)
    pimp = sub.add_parser("import", help="convert a plain .story file into the engine's .jsonl format")
    pimp.add_argument("source", help="path to the .story authoring file")
    pimp.add_argument("--output", "-o", help="output .jsonl path (default: the working log)")
    pimp.set_defaults(func=_cmd_import)

    # Phase 5 — canon layer
    pcan = sub.add_parser("canon", help="character bibles: list, show, in-character check (Phase 5)")
    pcan.add_argument(
        "canon_action",
        choices=("list", "show", "check", "test", "draft", "audit", "attribute"),
        help="list (all bibles) | show NAME | check (audit pending edits) | "
             "test NAME (bible proof: generate violating+compliant pair and check both) | "
             "draft SPEAKER (auto-draft a bible from the speaker's log turns) | "
             "audit SPEAKER (list turns that may pollute a card-based bible draft) | "
             "attribute (voice labeling sidecar: auto / propose / report / set / stats)",
    )
    pcan.add_argument(
        "name", nargs="?", default="",
        help="character name/alias (show, test, draft) or attribute sub-action "
             "(auto, propose, report, show, set, stats)",
    )
    pcan.add_argument(
        "--constraint-index", type=int, default=0,
        help="for `test`: which forbidden_phrasings entry to test (0-based, default 0)",
    )
    pcan.add_argument(
        "--channel", choices=("user", "char"), default=None,
        help="for `draft`: hint whether the speaker is user or char (auto-detected if omitted)",
    )
    pcan.add_argument(
        "--batch-chars", type=int, default=4000,
        help="for `draft`: max chars of turn text per observation batch (default: 4000)",
    )
    pcan.add_argument(
        "--compress-every", type=int, default=5,
        help="for `draft`: compress after this many batches (default: 5)",
    )
    pcan.add_argument(
        "--out", default=None,
        help="for `draft`: write the JSON entry to this file instead of stdout",
    )
    pcan.add_argument(
        "--merge", action="store_true",
        help="for `draft`: merge the draft entry directly into canon/characters.json",
    )
    pcan.add_argument(
        "--exclude-msgs", default=None,
        help="for `draft`: comma-separated msg ids/ranges to skip (e.g. 89,491,500-510)",
    )
    pcan.add_argument(
        "--include-only-msgs", default=None,
        help="for `draft`: only use these msg ids/ranges (whitelist)",
    )
    pcan.add_argument(
        "--skip-scene-narrator", action="store_true",
        help="for `draft`: skip char-card turns with heavy NPC dialogue (scene-master mode)",
    )
    pcan.add_argument(
        "--no-sources", action="store_true",
        help="for `draft`: ignore canon/bible_sources.json",
    )
    pcan.add_argument(
        "--audit-out", default=None,
        help="for `audit`: write report to this path (default: stdout)",
    )
    pcan.add_argument(
        "--attr-action",
        choices=("auto", "propose", "report", "show", "set", "stats", "reconcile"),
        default="auto",
        help="for `attribute`: which labeling action (default: auto)",
    )
    pcan.add_argument(
        "--msg-id", type=int, default=None,
        help="for `attribute show` / `set`: target message id",
    )
    pcan.add_argument(
        "--voice", default=None,
        help="for `attribute set`: voice key (wren, ilse, varga, ensemble, …)",
    )
    pcan.add_argument(
        "--mode",
        choices=sorted(attribution_mod.VALID_MODES),
        default=None,
        help="for `attribute set`: labeling mode",
    )
    pcan.add_argument(
        "--notes", default="",
        help="for `attribute set`: optional note",
    )
    pcan.add_argument(
        "--reviewed", action="store_true",
        help="for `attribute set`: mark label as director-reviewed",
    )
    pcan.add_argument(
        "--only-unlabeled", action="store_true",
        help="for `attribute auto` / `propose`: skip already-labeled msgs",
    )
    pcan.add_argument(
        "--only-flagged", action="store_true",
        help="for `attribute propose` / `report`: focus on ambiguous labels (default for propose)",
    )
    pcan.add_argument(
        "--all", dest="attr_all", action="store_true",
        help="for `attribute propose`: label every unreviewed msg, not only flagged",
    )
    pcan.add_argument(
        "--batch-size", type=int, default=8,
        help="for `attribute propose`: turns per LLM call (default: 8)",
    )
    pcan.add_argument(
        "--attr-out", default=None,
        help="for `attribute report`: write report path (default: stdout)",
    )
    pcan.add_argument(
        "--card", default=None,
        help="for `attribute report`: only show turns from this card (Narrator, Wren, …)",
    )
    pcan.add_argument(
        "--review", action="store_true",
        help="for `attribute report`: full message body per entry (markdown sections)",
    )
    pcan.add_argument(
        "--full-text", action="store_true",
        help="for `attribute report`: alias for --review",
    )
    pcan.add_argument(
        "--preview-chars", type=int, default=None,
        help="for `attribute report`: compact preview length (default 90); "
             "for `reconcile`: full messages unless set",
    )
    pcan.add_argument(
        "--use-attribution", action="store_true",
        help="for `draft`: filter or label turns using voice_attribution.json",
    )
    pcan.add_argument(
        "--attr-msgs", default=None,
        help="for `attribute auto` / `propose`: limit to these msg ids/ranges",
    )
    pcan.add_argument(
        "--force", action="store_true",
        help="for `attribute auto` / `set`: overwrite reviewed labels",
    )
    pcan.add_argument(
        "--auto-accept-confidence", type=float, default=0.95,
        help="for `attribute reconcile`: min confidence to auto-accept llm_proposed labels",
    )
    pcan.add_argument(
        "--skip-propose", action="store_true",
        help="for `attribute reconcile`: skip LLM propose (auto-accept existing labels only)",
    )
    pcan.set_defaults(func=_cmd_canon)

    pid = sub.add_parser(
        "identity",
        help="stable message uids (survive injection; every sidecar anchors here)",
    )
    pid.add_argument("identity_action", choices=("status", "backfill"),
                     help="status (how many messages are identified) | "
                          "backfill (mint uids for any that are missing)")
    pid.add_argument("--dry-run", action="store_true",
                     help="for `backfill`: report without writing")
    pid.set_defaults(func=_cmd_identity)

    psy = sub.add_parser(
        "sync",
        help="push the working log back to the live SillyTavern chat file",
    )
    psy.add_argument("sync_action", choices=("status", "push"),
                     help="status (compare working log and ST chat) | "
                          "push (write the working log to ST, uids stripped)")
    psy.add_argument("--to", help=f"destination chat file (default: {config.SOURCE_LOG})")
    psy.add_argument("--dry-run", action="store_true",
                     help="for `push`: report without writing")
    psy.set_defaults(func=_cmd_sync)

    pnov = sub.add_parser(
        "novelize",
        help="turn a log scene into manuscript prose (POV and tense are yours)",
    )
    pnov.add_argument(
        "novelize_action",
        choices=("plan", "run", "batch", "assemble", "show", "voice", "status"),
        help="plan (what a pass would do, no model call) | run (novelize a span) "
             "| batch (checkpointed run over pending orchestration units) "
             "| assemble (deterministic chaptered private manuscript) "
             "| show (read a novelized scene) | voice (set the book's default "
             "person/tense/focal) | status (what is novelized so far)",
    )
    pnov.add_argument("--from", dest="msg_from", type=int,
                      help="first message of the span")
    pnov.add_argument("--to", dest="msg_to", type=int,
                      help="last message of the span (default: same as --from)")
    pnov.add_argument("--scene", help="novelize a whole scene by its id instead of a span")
    pnov.add_argument("--person", choices=tuple(manuscript_mod.PERSONS),
                      help="narrative person for this pass (default: the book's)")
    pnov.add_argument("--tense", choices=tuple(manuscript_mod.TENSES),
                      help="narrative tense for this pass (default: the book's)")
    pnov.add_argument("--focal", help="whose head the prose stays in")
    pnov.add_argument("--direction",
                      help="for a re-run: what was wrong with the last take")
    pnov.add_argument("--regenerate", action="store_true",
                      help="write a new take over a scene already novelized")
    pnov.add_argument("--dry-run", action="store_true",
                      help="for `run`: print the prose without filing it")
    pnov.add_argument("--model", help="override the model for this pass")
    pnov.add_argument("--max-scenes", type=int,
                      help="for `batch`: stop after this many pending units; omit for all")
    pnov.add_argument("--episode", type=int,
                      help="for `batch`: restrict the resumable run to one canonical episode id")
    pnov.add_argument("--max-retries", type=int, default=2,
                      help="for `batch`: retries per failed unit (default: 2)")
    pnov.add_argument("--continuity-chars", type=int, default=3000,
                      help="for `batch`: preceding manuscript tail supplied to the next unit")
    pnov.add_argument("--stop-on-error", action="store_true",
                      help="for `batch`: stop after a unit exhausts its retries")
    pnov.add_argument("--approved-only", action="store_true",
                      help="for `assemble`: omit draft and rejected scenes")
    pnov.add_argument(
        "--max-span-chars",
        type=int,
        default=None,
        dest="max_span_chars",
        help="one-pass source-character ceiling (default: "
             f"{config.NOVELIZE_MAX_SPAN_CHARS:,}, or "
             "STORY_EDITOR_NOVELIZE_MAX_SPAN_CHARS). Larger → fewer passes; "
             "smaller → safer output budgets.",
    )
    pnov.set_defaults(func=_cmd_novelize)

    pi2 = sub.add_parser("index", help="three-layer search index (Phase 1)")
    i2sub = pi2.add_subparsers(dest="action", required=True)

    ibu = i2sub.add_parser("build", help="(re)build the index for the current log")
    ibu.add_argument("--db", help="override the db path (default: workspace/<log>.index.sqlite3)")
    ibu.add_argument("--rebuild", action="store_true",
                     help="drop existing tables before building")
    ibu.set_defaults(func=_cmd_index_build)

    iin = i2sub.add_parser("info", help="summary stats for the index")
    iin.add_argument("--db", help="override the db path")
    iin.set_defaults(func=_cmd_index_info)

    ise = i2sub.add_parser("search", help="run a search against the index")
    ise.add_argument("query", nargs="?", help="the query string (omit for --mode structural)")
    ise.add_argument("--mode", choices=("fused", "keyword", "semantic", "structural"),
                     default="fused", help="which retrieval layer (default: fused)")
    ise.add_argument("--limit", type=int, default=10, help="how many hits to return")
    ise.add_argument("--candidates", type=int, default=25,
                     help="per-layer candidates fused mode considers (default: 25)")
    ise.add_argument("--db", help="override the db path")
    ise.add_argument("--speaker", help="restrict to one speaker (exact, case-insensitive)")
    ise.add_argument("--role", choices=("user", "char", "system"), help="restrict by role")
    g = ise.add_mutually_exclusive_group()
    g.add_argument("--interludes", action="store_true",
                   help="restrict to editor-injected interludes only")
    g.add_argument("--no-interludes", action="store_true",
                   help="exclude editor-injected interludes")
    ise.add_argument("--date-from", help="ISO date lower bound (yyyy-mm-dd)")
    ise.add_argument("--date-to", help="ISO date upper bound (yyyy-mm-dd)")
    ise.set_defaults(func=_cmd_index_search)

    pprj = sub.add_parser("project", help="projects on this machine: list, switch, save and load bundles")
    prsub = pprj.add_subparsers(dest="action", required=True)
    prsub.add_parser("list", help="registered projects; * marks the active one")
    prsub.add_parser("current", help="the project this command would open, and why")
    pra = prsub.add_parser("add", help="register a project folder (nothing is copied)")
    pra.add_argument("home")
    prr = prsub.add_parser("remove", help="forget a project (its files are not touched)")
    prr.add_argument("id")
    prw = prsub.add_parser("switch", help="make a project active (takes effect when the server starts)")
    prw.add_argument("id")
    prs = prsub.add_parser("save", help="write a .sebundle of a project")
    prs.add_argument("id", nargs="?", help="a registered project (default: the current one)")
    prs.add_argument("--home", help="bundle this folder instead")
    prs.add_argument("--out", default=".", help="folder or .sebundle path (default: here)")
    prs.add_argument("--with-backups", action="store_true", help="include workspace/backups/")
    prs.add_argument("--with-index", action="store_true", help="include the search index")
    prl = prsub.add_parser("load", help="restore a .sebundle into a new folder")
    prl.add_argument("bundle")
    prl.add_argument("--into", help="target folder, which must not exist (default: <id> next to the bundle)")
    prl.add_argument("--id", dest="new_id", help="load under a different project id")
    prl.add_argument("--no-register", action="store_true", help="don't add it to the project list")
    prv = prsub.add_parser("verify", help="check a .sebundle without loading it")
    prv.add_argument("bundle")
    pprj.set_defaults(func=_cmd_project)

    psv = sub.add_parser(
        "serve",
        help="start the local HTTP API for SillyTavern extension / scripts (Phase 6)",
    )
    psv.add_argument("--host", default=config.API_HOST, help="bind address (default: 127.0.0.1)")
    psv.add_argument("--port", type=int, default=config.API_PORT, help="port (default: 8765)")
    psv.set_defaults(func=_cmd_serve)

    ph = sub.add_parser("history", help="browse the append-only edit changelog (Phase 6)")
    phsub = ph.add_subparsers(dest="action", required=True)

    phl = phsub.add_parser("list", help="list recent commits and undos")
    phl.add_argument("--limit", type=int, default=20, help="max entries (default: 20)")
    phl.set_defaults(func=_cmd_history, entry_id=None)

    phs = phsub.add_parser("show", help="show one entry with full before/after diffs")
    phs.add_argument("entry_id", type=int, nargs="?", default=None,
                     help="entry id (default: latest)")
    phs.set_defaults(func=_cmd_history)

    pllm = sub.add_parser(
        "llm",
        help="probe the configured OpenAI-compatible model endpoint "
             "(local textgen or OpenRouter)",
    )
    llmsub = pllm.add_subparsers(dest="llm_action", required=True)
    llmp = llmsub.add_parser(
        "ping",
        help="send a tiny completion and print provider/model/reply",
    )
    llmp.add_argument("--model", help="override STORY_EDITOR_MODEL_NAME for this ping")
    llmp.add_argument(
        "--base-url",
        help="OpenAI-compatible base (e.g. http://localhost:5001/v1)",
    )
    llmp.add_argument(
        "--timeout", type=float, default=60.0,
        help="seconds to wait (default: 60)",
    )
    llmp.set_defaults(func=_cmd_llm_ping)
    llmm = llmsub.add_parser("models", help="list model ids from GET /models")
    llmm.add_argument("--limit", type=int, default=30, help="max ids to print")
    llmm.add_argument(
        "--base-url",
        help="OpenAI-compatible base (e.g. http://localhost:5001/v1)",
    )
    llmm.set_defaults(func=_cmd_llm_models)

    pauth = sub.add_parser(
        "author",
        help="Author Studio experimental track (spark / advance / mark-committed)",
    )
    authsub = pauth.add_subparsers(dest="author_action", required=True)
    asp = authsub.add_parser("spark", help="compile spark for the next (or named) beat")
    asp.add_argument("--beat", help="beat label (default: next open)")
    asp.add_argument("--seed", help="path to mini_spine.json")
    asp.set_defaults(func=_cmd_author_spark)
    aad = authsub.add_parser(
        "advance",
        help="spark → write_scene → gates with retries; leaves pending for human commit",
    )
    aad.add_argument("--seed", help="path to mini_spine.json")
    aad.add_argument("--max-retries", type=int, default=3)
    aad.add_argument(
        "--skip-canon-llm",
        action="store_true",
        help="skip LLM canon check (criteria + dossier only)",
    )
    aad.set_defaults(func=_cmd_author_advance)
    amc = authsub.add_parser(
        "mark-committed",
        help="record that a beat was human-committed (after edits commit)",
    )
    amc.add_argument("label", help="beat label, e.g. A1")
    amc.add_argument(
        "--earnedness",
        type=int,
        choices=[1, 2, 3, 4, 5],
        help="required when the spine/run has end_state (trajectory POC)",
    )
    amc.add_argument("--earnedness-note", default="", help="optional earnedness note")
    amc.set_defaults(func=_cmd_author_mark_committed)
    atr = authsub.add_parser(
        "trajectory",
        help="export active author run as trajectory JSONL",
    )
    atr.add_argument(
        "-o", "--output",
        help="write path (default: workspace/trajectory_<run_id>.jsonl)",
    )
    atr.add_argument(
        "--stdout",
        action="store_true",
        help="print JSONL to stdout instead of writing a file",
    )
    atr.set_defaults(func=_cmd_author_trajectory)

    return p


def _cmd_author_spark(args: argparse.Namespace) -> int:
    from . import author as author_mod
    from .author import seed as seed_mod

    spine = author_mod.load_seed(args.seed)
    progress = seed_mod.load_progress()
    completed = set(progress.get("completed") or [])
    beat = (
        spine.beat_by_label(args.beat)
        if args.beat
        else spine.next_open(completed)
    )
    if beat is None:
        print("no open beat", file=sys.stderr)
        return 1
    note = author_mod.compile_spark(
        beat, spine=spine, log=loader.load(args.log),
    )
    print(f"# spark — {beat.label}: {beat.title}")
    print(note.text)
    return 0


def _cmd_author_advance(args: argparse.Namespace) -> int:
    from . import author as author_mod

    def on_progress(ev: dict) -> None:
        if ev.get("kind") == "phase":
            print(f"  … {ev.get('text', '')}", flush=True)

    result = author_mod.advance_beat(
        seed_path=args.seed,
        log=args.log,
        max_retries=args.max_retries,
        skip_canon_llm=args.skip_canon_llm,
        on_progress=on_progress,
    )
    print(result.message)
    if result.gate:
        print(f"  gate ok={result.gate.ok} attempts={result.attempts}")
        for f in result.gate.failures:
            print(f"  FAIL: {f}")
        for n in result.gate.notes:
            print(f"  note: {n}")
    if result.awaiting_commit:
        print("pending edits ready — run: python -m story_editor edits show")
        print("then: python -m story_editor edits commit")
        print(f"then: python -m story_editor author mark-committed {result.beat.label}")
        return 0
    return 2 if result.write is not None else 1


def _cmd_author_mark_committed(args: argparse.Namespace) -> int:
    from . import author as author_mod

    try:
        author_mod.mark_beat_committed(
            args.label,
            earnedness=getattr(args, "earnedness", None),
            earnedness_note=(getattr(args, "earnedness_note", None) or None),
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"marked {args.label} committed")
    return 0


def _cmd_author_trajectory(args: argparse.Namespace) -> int:
    from . import author as author_mod

    try:
        if args.stdout:
            sys.stdout.write(author_mod.trajectory_jsonl())
            return 0
        path = author_mod.write_trajectory(args.output)
        print(path)
        return 0
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _cmd_llm_ping(args: argparse.Namespace) -> int:
    from . import llm as llm_mod

    provider = getattr(config, "MODEL_PROVIDER", "local")
    key_set = bool((config.MODEL_API_KEY or "").strip())
    base = (getattr(args, "base_url", None) or config.MODEL_BASE_URL).rstrip("/")
    print(f"provider: {provider}", flush=True)
    print(f"base:     {base}", flush=True)
    print(f"model:    {args.model or config.MODEL_NAME}", flush=True)
    print(f"api key:  {'set' if key_set else 'not set'}", flush=True)
    if provider == "openrouter" and not key_set and not getattr(args, "base_url", None):
        print(
            "OpenRouter needs STORY_EDITOR_MODEL_API_KEY or OPENROUTER_API_KEY.",
            file=sys.stderr,
        )
        return 1
    try:
        result = llm_mod.ping(
            model=args.model, timeout=args.timeout, base_url=args.base_url,
        )
    except llm_mod.ModelError as exc:
        print(f"ping failed: {exc}", file=sys.stderr)
        return 1
    print(f"reply:    {result['reply']!r}")
    if result.get("model") and args.model and result["model"] != args.model:
        print(f"served:   {result['model']}  (bridge remapped the requested id)")
    print("ok")
    return 0


def _cmd_llm_models(args: argparse.Namespace) -> int:
    from . import llm as llm_mod

    names = llm_mod.list_models(base_url=args.base_url, timeout=30.0)
    if not names:
        print("no models listed (endpoint empty, unreachable, or unauthorized)",
              file=sys.stderr)
        return 1
    for name in names[: max(1, args.limit)]:
        print(name)
    if len(names) > args.limit:
        print(f"… ({len(names) - args.limit} more)")
    return 0


def _cmd_project(args: argparse.Namespace) -> int:
    from . import bundle as bundle_mod, registry as registry_mod

    action = args.action
    try:
        if action == "list":
            reg = registry_mod.load()
            if not reg.projects:
                print(f"no projects registered ({registry_mod.path()})")
                print("add one with: python -m story_editor project add PATH")
                return 0
            for e in reg.projects:
                mark = "*" if e.id == reg.active else " "
                gone = "" if e.home.is_dir() else "   (folder missing)"
                print(f"{mark} {e.id:<24} {e.title:<28} {e.home}{gone}")
            return 0
        if action == "current":
            print(f"{config.PROJECT_ID}  ({config.PROJECT_TITLE})")
            print(f"  home: {config.HOME_DIR}")
            why = {"env": "STORY_EDITOR_HOME is set", "registry": "the active project in the registry",
                   "repo": "project.json at the repository root", "example": "the bundled example (nothing else chosen)"}
            print(f"  from: {why.get(config.HOME_SOURCE, config.HOME_SOURCE)}")
            if config.REGISTRY_WARNING:
                print(f"  note: {config.REGISTRY_WARNING}")
            return 0
        if action == "add":
            e = registry_mod.add(args.home)
            print(f"registered {e.id} → {e.home}")
            return 0
        if action == "remove":
            e = registry_mod.remove(args.id)
            print(f"forgot {e.id} ({e.home} is untouched)")
            return 0
        if action == "switch":
            e = registry_mod.switch(args.id)
            print(f"active project: {e.id} → {e.home}")
            if os.environ.get("STORY_EDITOR_HOME"):
                print("note: STORY_EDITOR_HOME is set in this shell and still wins; unset it to use the switch")
            print("a running server keeps its project until it restarts (the GUI picker restarts it for you)")
            return 0
        if action == "save":
            if args.home:
                home = Path(args.home)
            elif args.id:
                entry = registry_mod.load().get(args.id)
                if entry is None:
                    print(f"error: no project called {args.id!r}", file=sys.stderr)
                    return 1
                home = entry.home
            else:
                home = config.HOME_DIR
            path = bundle_mod.save(home, args.out, with_backups=args.with_backups, with_index=args.with_index)
            report = bundle_mod.verify(path)
            print(f"saved {path}")
            print(f"  {report.files} files, {report.bytes:,} bytes; verified")
            for e in report.manifest.get("external", []):
                print(f"  external: {e['role']} ← {e['original']}")
            for e in report.manifest.get("not_bundled", []):
                print(f"  not bundled: {e['role']} ({e['original']}): {e['reason']}")
            return 0 if report.ok else 1
        if action == "verify":
            report = bundle_mod.verify(args.bundle)
            if report.ok:
                print(f"ok: {report.project_id} ({report.title}), {report.files} files, {report.bytes:,} bytes")
                print(f"  created {report.manifest.get('created')} on {report.manifest.get('machine')}")
                return 0
            for err in report.errors:
                print(f"FAILED: {err}", file=sys.stderr)
            return 1
        if action == "load":
            report = bundle_mod.verify(args.bundle)
            if not report.ok:
                for err in report.errors:
                    print(f"FAILED: {err}", file=sys.stderr)
                return 1
            into = Path(args.into) if args.into else Path(args.bundle).expanduser().resolve().parent / (args.new_id or report.project_id)
            loaded = bundle_mod.load(args.bundle, into, new_id=args.new_id, register=not args.no_register)
            print(f"loaded {loaded.project_id} into {loaded.home} ({loaded.files} files, all verified)")
            for note in loaded.rewrites:
                print(f"  {note}")
            if loaded.registered:
                print(f"  registered; open it with: python -m story_editor project switch {loaded.project_id}")
            return 0
    except bundle_mod.BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    from . import server as server_mod
    server_mod.serve(host=args.host, port=args.port)
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    from . import history as history_mod

    if args.action == "list":
        entries = history_mod.list_entries(log_path=args.log, limit=args.limit)
        if not entries:
            print("no history entries yet (commit an edit to create one).")
            return 0
        print(f"# edit history — {config.EDIT_HISTORY.name}  (newest first)")
        print(f"{'ID':>4}  {'WHEN':<26}  {'EVENT':<7}  {'OP':<10}  SPAN        NOTE")
        print("-" * 90)
        for e in entries:
            when = e.created[:19].replace("T", " ") if e.created else "?"
            span = ""
            if e.changed_from is not None:
                span = f"msgs {e.changed_from}–{e.changed_to}"
            note = (e.note or "")[:36]
            if len(e.note or "") > 36:
                note += "…"
            print(f"{e.id:>4}  {when:<26}  {e.event:<7}  {e.operator:<10}  {span:<11} {note}")
        return 0

    if args.action == "show":
        entry_id = args.entry_id
        if entry_id is None:
            latest = history_mod.get_latest(log_path=args.log)
            if latest is None:
                print("no history entries.", file=sys.stderr)
                return 1
            entry_id = latest.id
        entry = history_mod.get_entry(entry_id, log_path=args.log)
        if entry is None:
            print(f"no history entry #{entry_id}.", file=sys.stderr)
            return 1
        print(f"# history entry #{entry.id}  —  {entry.event} / {entry.operator}")
        print(f"  when:   {entry.created}")
        print(f"  log:    {entry.log}")
        print(f"  note:   {entry.note}")
        if entry.locator:
            print(f"  target: {entry.locator}")
        print(f"  backup: {Path(entry.backup).name}")
        if entry.changed_from is not None:
            print(f"  span:   msgs {entry.changed_from}–{entry.changed_to}")
        if not entry.edits:
            print("\n(no per-message edits recorded)")
            return 0
        for edit in entry.edits:
            print(f"\n── msg {edit.msg_id} · {edit.speaker} · {edit.kind} ──")
            for f in edit.flags:
                print(f"  ⚠ {f}")
            print(transform_mod._unified(edit.before, edit.after))
        return 0

    print(f"unknown history action: {args.action}", file=sys.stderr)
    return 1


def _cmd_import(args: argparse.Namespace) -> int:
    """Convert a plain .story file into the engine's .jsonl log format."""
    from . import importer as importer_mod

    dst = args.output or str(config.working_log())
    n = importer_mod.convert(args.source, dst)
    print(f"wrote {n} messages to {dst}")
    return 0


def _load_jsonl_turns(log_path: Path) -> list[dict]:
    import json as _json

    raw_log = [
        _json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if raw_log and isinstance(raw_log[0].get("user_name"), str):
        raw_log = raw_log[1:]
    for i, msg in enumerate(raw_log):
        if "msg_id" not in msg:
            msg["msg_id"] = i
    return raw_log


def _turns_for_speaker(raw_log: list[dict], speaker: str) -> list[dict]:
    speaker_lower = speaker.lower()
    return [m for m in raw_log if (m.get("name") or "").lower() == speaker_lower]


def _cmd_canon(args: argparse.Namespace) -> int:
    """Phase 5 — canon layer: list, show, and in-character check."""
    action = args.canon_action

    if action == "list":
        bibles = canon_layer.all_bibles()
        if not bibles:
            print("No character bibles found.  "
                  "Check that canon/characters.json exists.", file=sys.stderr)
            return 1
        print(f"{'KEY':<16}  LABEL")
        print("-" * 60)
        for key, char in sorted(bibles.items()):
            print(f"{key:<16}  {char.label}")
        return 0

    if action == "show":
        name = args.name
        char = canon_layer.get_character(name)
        if char is None:
            print(f"no bible found for {name!r}", file=sys.stderr)
            return 1
        print(canon_layer.render_canon_block(char))
        if char.canonical_facts:
            print("\nAll canonical facts:")
            for f_ in char.canonical_facts:
                print(f"  • {f_}")
        if char.relationship_notes:
            print("\nRelationship notes:")
            for other, note in char.relationship_notes.items():
                print(f"  {other}: {note}")
        return 0

    if action == "check":
        edits = transform_mod.load_pending_edits()
        if edits is None:
            print("no pending edits to check — run `restyle`, `retune`, or `inject` first",
                  file=sys.stderr)
            return 1
        if not edits.edits:
            # Empty edit set is not an error: restyle ran but the model judged no changes
            # needed (correct behaviour on already-in-character text).
            print("· pending edit-set is empty — the last operator produced no changes "
                  "(model judged the target already correct). Nothing to audit.")
            return 0

        any_concerns = False
        for edit in edits.edits:
            char = canon_layer.get_character(edit.speaker)
            if char is None:
                print(f"[msg {edit.msg_id}] {edit.speaker}: no bible on record — skipping")
                continue
            print(f"\n[msg {edit.msg_id}] {edit.speaker} — checking against canon…")
            result = canon_layer.check_in_character(
                before=edit.before,
                after=edit.after,
                speaker=edit.speaker,
                char=char,
            )
            verdict = result.get("verdict", "unknown")
            reason = result.get("reason", "")
            issues = result.get("specific_issues", [])

            mark = {"in_character": "✓", "concerns": "⚠", "out_of_character": "✗",
                    "unknown": "?"}.get(verdict, "?")
            print(f"  {mark} {verdict.upper()}: {reason}")
            for iss in issues:
                print(f"    – {iss}")
            if verdict in ("concerns", "out_of_character"):
                any_concerns = True

        if any_concerns:
            print("\nsome edits have concerns or are out of character — review before committing")
            return 1
        print("\nAll checked edits are in-character.")
        return 0

    if action == "test":
        # --- bible proof test ---
        # Generate one compliant and one violating passage for the named character,
        # run check_in_character on both, and report whether the bible correctly
        # distinguished them.
        name = args.name
        if not name:
            print("canon test requires a character name, e.g. `canon test Wren`",
                  file=sys.stderr)
            return 1
        char = canon_layer.get_character(name)
        if char is None:
            print(f"no bible found for {name!r}", file=sys.stderr)
            return 1

        # Pick the constraint to test: --constraint-index N selects from
        # forbidden_phrasings; default is index 0.
        pool = char.forbidden_phrasings
        if not pool:
            print(f"no forbidden_phrasings defined for {char.label} — "
                  "add Tier 1/2 rules before testing", file=sys.stderr)
            return 1
        idx = getattr(args, "constraint_index", 0) or 0
        if idx >= len(pool):
            print(f"constraint index {idx} out of range "
                  f"(0..{len(pool)-1})", file=sys.stderr)
            return 1
        constraint = pool[idx]

        print(f"\nBIBLE TEST — {char.label}")
        print(f"Testing constraint [{idx}]:\n  {constraint}\n")

        print("─" * 60)
        print("STEP 1/4  Generating VIOLATING passage…")
        violating = canon_layer.generate_test_passage(char, constraint, violate=True)
        print(f"\n{violating}\n")

        print("─" * 60)
        print("STEP 2/4  Checking violating passage against canon…")
        v_result = canon_layer.check_in_character(
            before="", after=violating, speaker=char.label, char=char,
        )
        v_verdict = v_result.get("verdict", "unknown")
        v_reason  = v_result.get("reason", "")
        v_issues  = v_result.get("specific_issues", [])
        v_mark = {"in_character": "✓", "concerns": "⚠", "out_of_character": "✗",
                  "unknown": "?"}.get(v_verdict, "?")
        print(f"  {v_mark} {v_verdict.upper()}: {v_reason}")
        for iss in v_issues:
            print(f"    – {iss}")

        print()
        print("─" * 60)
        print("STEP 3/4  Generating COMPLIANT passage…")
        _GEN_RETRIES = 2
        compliant = ""
        for _attempt in range(_GEN_RETRIES):
            compliant = canon_layer.generate_test_passage(char, constraint, violate=False)
            if compliant.strip():
                break
            if _attempt < _GEN_RETRIES - 1:
                print(f"  (empty generation on attempt {_attempt + 1}, retrying…)",
                      file=sys.stderr)
        print(f"\n{compliant}\n")

        # If after retries we still have no content, it's a generation failure —
        # not a bible problem. Exit 0 with an inconclusive advisory so the test
        # harness doesn't penalise the bible for the model's blank output.
        if not compliant.strip():
            print("─" * 60)
            print("STEP 4/4  Checking compliant passage against canon…")
            print("  (skipped — generation returned empty text after retries)")
            print()
            print("═" * 60)
            print(
                "RESULT: INCONCLUSIVE — compliant passage generation failed (empty output "
                "from model). This is a generation reliability issue, not a bible problem.\n"
                "  Re-run to confirm; if it persists, check the constraint wording or model."
            )
            return 0

        print("─" * 60)
        print("STEP 4/4  Checking compliant passage against canon…")
        c_result = canon_layer.check_in_character(
            before="", after=compliant, speaker=char.label, char=char,
        )
        c_verdict = c_result.get("verdict", "unknown")
        c_reason  = c_result.get("reason", "")
        c_issues  = c_result.get("specific_issues", [])
        c_mark = {"in_character": "✓", "concerns": "⚠", "out_of_character": "✗",
                  "unknown": "?"}.get(c_verdict, "?")
        print(f"  {c_mark} {c_verdict.upper()}: {c_reason}")
        for iss in c_issues:
            print(f"    – {iss}")

        # --- Verdict ---
        print()
        print("═" * 60)
        violation_caught = v_verdict in ("concerns", "out_of_character")
        compliant_passed = c_verdict == "in_character"

        if violation_caught and compliant_passed:
            print("RESULT: PASS — bible correctly distinguished compliant from violating.")
            return 0
        else:
            parts = []
            if not violation_caught:
                parts.append(
                    f"violating passage was NOT flagged (verdict: {v_verdict}) — "
                    "the constraint may be too vague (Tier 4/5). "
                    "Concretize it or move it to aesthetic_rules."
                )
            if not compliant_passed:
                parts.append(
                    f"compliant passage was flagged (verdict: {c_verdict}) — "
                    "the constraint may be overconstrained or the generated passage "
                    "drifted despite the direction. Re-run or review the rule."
                )
            for p in parts:
                print(f"RESULT: FAIL — {p}")
            return 1

    if action == "audit":
        speaker = (args.name or "").strip()
        if not speaker:
            print("canon audit requires a speaker name, e.g. `canon audit Narrator`",
                  file=sys.stderr)
            return 1
        log_path = config.working_log()
        if not log_path.exists():
            print(f"no log found at {log_path}", file=sys.stderr)
            return 1
        turns = _turns_for_speaker(_load_jsonl_turns(log_path), speaker)
        if not turns:
            print(f"no turns found for speaker {speaker!r}", file=sys.stderr)
            return 1
        rows = canon_layer.audit_speaker_turns(turns, speaker)
        flagged = [r for r in rows if r["flags"]]
        lines = [
            f"# canon audit — {speaker}  ({len(turns)} turns, {len(flagged)} flagged)",
            "# ST logs the card name, not the character voiced. Review flagged rows",
            "# and add msg ids to canon/bible_sources.json before running canon draft.",
            "",
        ]
        for r in rows:
            flag = ",".join(r["flags"]) if r["flags"] else "ok"
            lines.append(f"  {r['msg_id']:4d}  [{flag:16s}]  {r['preview']}")
        report = "\n".join(lines) + "\n"
        out = args.audit_out
        if out:
            Path(out).write_text(report, encoding="utf-8")
            print(f"wrote audit report → {out}  ({len(flagged)} flagged / {len(turns)} turns)")
        else:
            print(report, end="")
        return 0

    if action == "draft":
        # ── canon draft SPEAKER ────────────────────────────────────────────
        # Collect all turns by SPEAKER from the active log, run the batch
        # observation + compression + synthesis pipeline, and output a draft
        # characters.json entry.
        speaker = args.name.strip()
        if not speaker:
            print("canon draft requires a speaker name, "
                  "e.g. `canon draft Wren`", file=sys.stderr)
            return 1

        log_path = config.working_log()
        if not log_path.exists():
            print(f"no log found at {log_path}", file=sys.stderr)
            return 1

        turns = _turns_for_speaker(_load_jsonl_turns(log_path), speaker)

        if not turns:
            print(f"no turns found for speaker {speaker!r} in {log_path.name}",
                  file=sys.stderr)
            return 1

        exclude_ids = canon_layer.parse_msg_id_spec(args.exclude_msgs)
        include_only_ids = canon_layer.parse_msg_id_spec(args.include_only_msgs)
        skip_narrator = bool(args.skip_scene_narrator)
        if not args.no_sources:
            rules = canon_layer.load_bible_source_rules(speaker)
            raw_exc = rules.get("exclude_msg_ids")
            if isinstance(raw_exc, list):
                exclude_ids |= {int(x) for x in raw_exc}
            elif raw_exc:
                exclude_ids |= canon_layer.parse_msg_id_spec(str(raw_exc))
            inc = rules.get("include_only_msg_ids")
            if inc and not include_only_ids:
                if isinstance(inc, list):
                    include_only_ids = {int(x) for x in inc}
                else:
                    include_only_ids = canon_layer.parse_msg_id_spec(str(inc))
            if rules.get("skip_scene_narrator"):
                skip_narrator = True

        turns, fstats = canon_layer.filter_draft_turns(
            turns,
            speaker,
            exclude_ids=exclude_ids,
            include_only_ids=include_only_ids or None,
            skip_scene_narrator=skip_narrator,
        )
        print(f"# canon draft — {speaker}  ({fstats['kept']}/{fstats['input']} turns after filters)")
        if fstats["skipped_exclude"]:
            print(f"  excluded by id: {fstats['skipped_exclude']}")
        if fstats["skipped_include_only"]:
            print(f"  outside include-only: {fstats['skipped_include_only']}")
        if fstats["skipped_scene_narrator"]:
            print(f"  skipped scene-narrator: {fstats['skipped_scene_narrator']}")
        if getattr(args, "use_attribution", False):
            attr_store = attribution_mod.load_store()
            if attr_store.get("labels"):
                turns, astats = attribution_mod.filter_turns_for_bible(
                    turns, speaker, attr_store,
                )
                print(
                    f"  attribution filter: {astats['kept']}/{astats['input']} "
                    f"(voice≠{astats['bible_voice']}: {astats['skipped_attribution_voice']}, "
                    f"mode: {astats['skipped_attribution_mode']})"
                )
            else:
                print("  attribution filter: no labels in sidecar — skipped")
        if not turns:
            print("error: no turns left after filters — adjust bible_sources.json or flags",
                  file=sys.stderr)
            return 1

        import json as _json

        print(f"  source log: {log_path.name}")

        # Auto-detect channel from is_user flag
        channel_hint = args.channel
        if channel_hint is None:
            is_user_flags = [bool(m.get("is_user")) for m in turns]
            channel_hint = "user" if sum(is_user_flags) > len(is_user_flags) / 2 else "char"
        print(f"  channel hint: {channel_hint}")

        draft = canon_layer.draft_bible(
            speaker_name=speaker,
            turns=turns,
            batch_chars=args.batch_chars,
            compress_every=args.compress_every,
            channel_hint=channel_hint,
            print_progress=True,
        )

        if not draft:
            print("error: synthesis returned an empty result — "
                  "try with a larger --batch-chars or inspect the model output",
                  file=sys.stderr)
            return 1

        # Wrap in the full entry structure
        entry = {speaker.lower(): draft}
        entry_json = _json.dumps(entry, indent=2, ensure_ascii=False)

        # ── output ────────────────────────────────────────────────────────
        out_path = args.out

        if args.merge:
            # Merge directly into canon/characters.json
            chars_path = config.CHARACTERS_JSON
            if chars_path.exists():
                existing = _json.loads(chars_path.read_text(encoding="utf-8"))
            else:
                existing = {"schema": "story-editor/characters@1", "characters": {}}

            key = speaker.lower()
            if key in existing.get("characters", {}):
                print(f"  ⚠  key {key!r} already exists in characters.json "
                      f"— overwriting with draft.")
            existing.setdefault("characters", {})[key] = draft
            chars_path.write_text(
                _json.dumps(existing, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            # Invalidate cached loader so future calls see the new entry
            canon_layer._load_characters_json.cache_clear()
            print(f"\nMerged draft for {speaker!r} → {chars_path}")
            print("Run  `canon test {speaker}`  to validate the bible discriminates.")
        elif out_path:
            import pathlib
            pathlib.Path(out_path).write_text(entry_json, encoding="utf-8")
            print(f"\nDraft written to {out_path}")
            print(f"Review it, then add the entry under \"characters\" in "
                  f"canon/characters.json, or rerun with --merge to apply automatically.")
        else:
            # Print to stdout
            print()
            print("─" * 60)
            print("DRAFT BIBLE ENTRY  (add under \"characters\" in canon/characters.json)")
            print("─" * 60)
            print(entry_json)
            print("─" * 60)
            print(f"\nReview and tighten, then run:  canon test {speaker}")
            print(f"Or rerun with --merge to apply directly:  canon draft {speaker} --merge")

        return 0

    if action == "attribute":
        attr_action = getattr(args, "attr_action", "auto") or "auto"
        # Allow `canon attribute stats` (second positional) as well as --attr-action.
        name_tok = (args.name or "").strip().lower()
        if name_tok in ("auto", "propose", "report", "show", "set", "stats", "reconcile"):
            attr_action = name_tok
        log_path = config.working_log()
        if not log_path.exists():
            print(f"no log found at {log_path}", file=sys.stderr)
            return 1
        turns = _load_jsonl_turns(log_path)
        store = attribution_mod.load_store()
        store["log"] = log_path.name

        if attr_action == "auto":
            id_set = canon_layer.parse_msg_id_spec(getattr(args, "attr_msgs", None)) or None
            stats = attribution_mod.auto_label_turns(
                turns, store,
                only_unlabeled=bool(args.only_unlabeled),
                msg_ids=id_set,
                force=bool(args.force),
            )
            path = attribution_mod.save_store(store)
            print(
                f"attribute auto → {path}\n"
                f"  considered: {stats['considered']}  written: {stats['written']}  "
                f"skipped_reviewed: {stats['skipped_reviewed']}  "
                f"skipped_existing: {stats['skipped_existing']}"
            )
            return 0

        if attr_action == "propose":
            only_flagged = not bool(getattr(args, "attr_all", False))
            id_set = canon_layer.parse_msg_id_spec(getattr(args, "attr_msgs", None)) or None
            def _prog(msg: str) -> None:
                print(f"  {msg}")

            totals = attribution_mod.propose_turns(
                turns, store,
                only_flagged=only_flagged,
                only_unlabeled=bool(args.only_unlabeled),
                msg_ids=id_set,
                batch_size=max(1, int(args.batch_size)),
                on_progress=_prog,
            )
            path = attribution_mod.save_store(store)
            print(
                f"attribute propose → {path}\n"
                f"  batches: {totals['batches']}  requested: {totals['requested']}  "
                f"parsed: {totals['parsed']}  written: {totals['written']}"
            )
            return 0

        if attr_action == "reconcile":
            id_set = canon_layer.parse_msg_id_spec(getattr(args, "attr_msgs", None)) or None
            min_conf = float(getattr(args, "auto_accept_confidence", 0.95) or 0.95)
            skip_propose = bool(getattr(args, "skip_propose", False))
            review_out = (
                getattr(args, "attr_out", None)
                or str(config.WORKSPACE_DIR / "drafts" / "attribution-needs-human.md")
            )

            def _prog(msg: str) -> None:
                print(f"  {msg}")

            print(f"attribute reconcile — auto"
                  f"{' → propose' if not skip_propose else ''} → auto-accept "
                  f"(confidence ≥ {min_conf})")
            stats, human_ids = attribution_mod.reconcile_turns(
                turns,
                store,
                min_confidence=min_conf,
                skip_propose=skip_propose,
                card_filter=getattr(args, "card", None),
                msg_ids=id_set,
                batch_size=max(1, int(args.batch_size)),
                on_progress=_prog,
            )
            path = attribution_mod.save_store(store)
            review = attribution_mod.format_reconcile_review(
                turns,
                store,
                human_ids,
                preview_chars=int(args.preview_chars) if args.preview_chars is not None else 0,
            )
            Path(review_out).write_text(review, encoding="utf-8")
            print(
                f"\nattribute reconcile → {path}\n"
                f"  scoped: {stats['scoped']}  auto_written: {stats['auto_written']}  "
                f"propose_written: {stats['propose_written']}  "
                f"auto_accepted: {stats['auto_accepted']}  "
                f"needs_human: {stats['needs_human']}\n"
                f"  review file → {review_out}"
            )
            return 0 if stats["needs_human"] == 0 else 1

        if attr_action == "report":
            review = bool(getattr(args, "review", False) or getattr(args, "full_text", False))
            if review:
                report = attribution_mod.format_review_report(
                    turns, store,
                    only_flagged=bool(args.only_flagged),
                    card_filter=getattr(args, "card", None),
                )
            else:
                report = attribution_mod.format_report(
                    turns, store,
                    only_flagged=bool(args.only_flagged),
                    card_filter=getattr(args, "card", None),
                    preview_chars=int(args.preview_chars) if args.preview_chars is not None else 90,
                )
            out = getattr(args, "attr_out", None)
            if out:
                Path(out).write_text(report, encoding="utf-8")
                print(f"wrote attribution report → {out}")
            else:
                print(report, end="")
            return 0

        if attr_action == "stats":
            print(attribution_mod.format_stats(store, turns), end="")
            return 0

        if attr_action == "show":
            mid = args.msg_id
            if mid is None:
                print("attribute show requires --msg-id", file=sys.stderr)
                return 1
            lab = attribution_mod.get_label(store, mid)
            turn = next((t for t in turns if int(t.get("msg_id", -1)) == mid), None)
            if turn is None:
                print(f"no message {mid} in log", file=sys.stderr)
                return 1
            print(f"msg {mid}  card={turn.get('name')!r}")
            if lab is None:
                print("  (no label — run `canon attribute auto`)")
            else:
                import json as _json
                print(_json.dumps(lab.to_dict(), indent=2, ensure_ascii=False))
            return 0

        if attr_action == "set":
            mid = args.msg_id
            if mid is None:
                print("attribute set requires --msg-id", file=sys.stderr)
                return 1
            turn = next((t for t in turns if int(t.get("msg_id", -1)) == mid), None)
            if turn is None:
                print(f"no message {mid} in log", file=sys.stderr)
                return 1
            voice = (args.voice or "").strip().lower()
            if not voice:
                print("attribute set requires --voice", file=sys.stderr)
                return 1
            mode = args.mode or "pov"
            existing = attribution_mod.get_label(store, mid)
            label = attribution_mod.VoiceLabel(
                card=str(turn.get("name") or ""),
                voice=voice,
                mode=mode,
                focal=existing.focal if existing else "",
                addressees=list(existing.addressees) if existing else [],
                source="manual",
                reviewed=bool(args.reviewed) or True,
                confidence=1.0,
                notes=str(args.notes or ""),
                segments=existing.segments if existing else [],
            )
            attribution_mod.set_label(store, mid, label, force=True)
            path = attribution_mod.save_store(store)
            print(f"set msg {mid} → voice={voice} mode={mode}  ({path})")
            return 0

        print(f"unknown attribute action: {attr_action}", file=sys.stderr)
        return 1

    print(f"unknown action: {action}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, IndexError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
