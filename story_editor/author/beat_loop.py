"""Propose → gate → retry loop for one or more beats (human commits)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import canon_layer, config, dossier as dossier_mod, loader, transform as transform_mod
from ..loader import Log
from .evidence import (
    ConditionEvidence,
    GateFailure,
    evidence_from_criteria_hits,
    missing_conditions,
)
from .run import (
    ensure_open_run,
    load_run,
    save_run,
    step_index_for_label,
    update_residuals,
)
from .seed import MiniBeat, MiniSpine, load_progress, load_seed, save_progress
from .spark import (
    AuthorNote,
    clear_spark_draft,
    compile_spark,
    load_spark_draft,
    spark_from_text,
)
from .write_scene import WriteResult, write_scene

OnAwaitCommit = Callable[["BeatLoopResult"], bool]


@dataclass
class GateResult:
    ok: bool
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    gate_failures: list[GateFailure] = field(default_factory=list)
    evidence: list[ConditionEvidence] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": list(self.failures),
            "notes": list(self.notes),
            "details": dict(self.details),
            "gate_failures": [g.to_json() for g in self.gate_failures],
            "evidence": [e.to_json() for e in self.evidence],
        }


@dataclass
class BeatLoopResult:
    beat: MiniBeat | None
    ok: bool
    attempts: int
    message: str = ""
    spark: AuthorNote | None = None
    write: WriteResult | None = None
    gate: GateResult | None = None
    awaiting_commit: bool = False
    error: str = ""

    @property
    def beat_label(self) -> str:
        return self.beat.label if self.beat else ""

    @property
    def beat_id(self) -> int:
        return self.beat.id if self.beat else -1

    def to_json(self) -> dict[str, Any]:
        return {
            "beat": self.beat.to_json() if self.beat else None,
            "beat_label": self.beat_label,
            "beat_id": self.beat_id,
            "ok": self.ok,
            "attempts": self.attempts,
            "message": self.message,
            "spark": self.spark.to_json() if self.spark else None,
            "write": self.write.to_json() if self.write else None,
            "gate": self.gate.to_json() if self.gate else None,
            "gates": self.gate.to_json() if self.gate else None,
            "awaiting_commit": self.awaiting_commit,
            "error": self.error,
        }


def _criteria_hits(text: str, criteria: list[str]) -> tuple[list[str], list[str]]:
    from .criteria import criteria_hits

    return criteria_hits(text, criteria)


def _proposal_body(write: WriteResult) -> str:
    # edit_set stores injects reversed for commit ordering — flip back so the
    # gate reads narrative order (and never drops a turn).
    parts = [
        f"{e.speaker}: {e.after}"
        for e in write.edit_set.edits
        if e.kind == "inject" and e.after
    ]
    return "\n".join(reversed(parts))


def _span_refs_for_write(write: WriteResult) -> list[str]:
    n_injects = sum(1 for e in write.edit_set.edits if e.kind == "inject")
    if n_injects <= 0:
        return []
    start = write.after_msg_id + 1
    end = write.after_msg_id + n_injects
    return [f"msg:{i}" for i in range(start, end + 1)]


def _add_failure(
    failures: list[str],
    structured: list[GateFailure],
    gf: GateFailure,
) -> None:
    structured.append(gf)
    failures.append(gf.message)


def gate_proposal(
    log: Log,
    beat: MiniBeat,
    write: WriteResult,
    *,
    skip_canon_llm: bool = False,
    spine: MiniSpine | None = None,
    run: dict[str, Any] | None = None,
    step_k: int | None = None,
) -> GateResult:
    failures: list[str] = []
    structured: list[GateFailure] = []
    notes: list[str] = []
    details: dict[str, Any] = {}
    evidence: list[ConditionEvidence] = []
    body = _proposal_body(write)
    span_refs = _span_refs_for_write(write)

    crit_ev = evidence_from_criteria_hits(
        body, beat.criteria, span_refs=span_refs,
    )
    evidence.extend(crit_ev)
    satisfied = [e.condition for e in crit_ev if e.satisfied]
    missing = missing_conditions(crit_ev)
    details["criteria_ok"] = satisfied
    details["criteria_missing"] = missing
    details["criteria_evidence"] = [e.to_json() for e in crit_ev]
    if missing:
        _add_failure(
            failures,
            structured,
            GateFailure(
                code="CRITERIA_MISSING",
                message="criteria missing: " + "; ".join(missing),
                condition=missing[0],
                method="match",
                detail={"missing": missing},
            ),
        )

    canon_notes: list[dict[str, Any]] = []
    if not skip_canon_llm:
        for e in write.edit_set.edits:
            if e.kind != "inject":
                continue
            char = canon_layer.get_character(e.speaker)
            if char is None:
                continue
            verdict = canon_layer.check_in_character(
                before="(new inject)",
                after=e.after,
                speaker=e.speaker,
                char=char,
            )
            canon_notes.append({"speaker": e.speaker, **verdict})
            if verdict.get("verdict") == "out_of_character":
                reason = str(verdict.get("reason") or "out_of_character")
                _add_failure(
                    failures,
                    structured,
                    GateFailure(
                        code="CHARACTER_OOC",
                        message=f"canon/{e.speaker}: {reason}",
                        condition=e.speaker,
                        method="llm",
                        detail={"speaker": e.speaker, "verdict": verdict},
                    ),
                )
    else:
        notes.append("canon LLM check skipped")
        details["canon_skipped"] = True
    details["canon"] = canon_notes

    n_injects = sum(1 for e in write.edit_set.edits if e.kind == "inject")
    end = write.after_msg_id + max(1, n_injects)
    span = dossier_mod.check_span(
        log,
        write.after_msg_id,
        end,
        characters=beat.speakers or None,
    )
    details["dossier"] = span.to_json()
    notes.extend(span.notes)
    if not span.ok:
        _add_failure(
            failures,
            structured,
            GateFailure(
                code="DOSSIER_OPEN",
                message=f"dossier open items: {len(span.open_items)}",
                method="match",
                detail={"open_count": len(span.open_items)},
            ),
        )

    # Trajectory gates (optional when no run / destination configured).
    N = int((run or {}).get("N") or 0) if run else 0
    k = int(step_k) if step_k else 0
    end_state = list((run or {}).get("end_state") or [])
    if not end_state and spine is not None:
        end_state = list(spine.end_state or [])
    initial_state = list((run or {}).get("initial_state") or [])
    if not initial_state and spine is not None:
        initial_state = list(spine.initial_state or [])

    if end_state:
        end_ev = evidence_from_criteria_hits(body, end_state, span_refs=span_refs)
        details["end_state_evidence_this_step"] = [e.to_json() for e in end_ev]
        evidence.extend(end_ev)
        all_end_here = bool(end_ev) and all(e.satisfied for e in end_ev)
        if k and N and k < N and all_end_here:
            _add_failure(
                failures,
                structured,
                GateFailure(
                    code="END_STATE_EARLY",
                    message=(
                        f"end_state fully evidenced at step {k}/{N} — "
                        "too early; leave destination for the final step"
                    ),
                    method="match",
                    detail={"k": k, "N": N, "end_state": end_state},
                ),
            )
        if k and N and k == N:
            prior = list((run or {}).get("evidenced_end_state") or [])
            by_cond = {
                str(e.get("condition") or ""): e
                for e in prior
                if e.get("satisfied")
            }
            for e in end_ev:
                if e.satisfied:
                    by_cond[e.condition] = e.to_json()
            still_missing = [c for c in end_state if c not in by_cond]
            details["end_state_cumulative_missing"] = still_missing
            if still_missing:
                _add_failure(
                    failures,
                    structured,
                    GateFailure(
                        code="END_STATE_INCOMPLETE",
                        message=(
                            "end_state incomplete at final step: "
                            + "; ".join(still_missing)
                        ),
                        condition=still_missing[0],
                        method="match",
                        detail={"missing": still_missing, "k": k, "N": N},
                    ),
                )

    if k == 1 and initial_state:
        # Soft→hard: proposal must not contradict declared initial facts
        # via explicit negation phrases (POC heuristic).
        body_l = body.lower()
        for cond in initial_state:
            neg = f"not {cond.lower()}"
            if neg in body_l or f"never {cond.lower()}" in body_l:
                _add_failure(
                    failures,
                    structured,
                    GateFailure(
                        code="INITIAL_STATE_CONTRADICTION",
                        message=f"contradicts initial_state: {cond}",
                        condition=cond,
                        method="match",
                        detail={"negation_hit": True},
                    ),
                )

    if k >= 2 and run:
        # Plot continuity: prior accepted landings still hold in cumulative log.
        prior_landings: list[str] = []
        for step in run.get("steps") or []:
            for ev in step.get("evidence") or []:
                if ev.get("satisfied") and str(ev.get("condition") or "").strip():
                    # Only beat criteria landings, not end_state (checked separately).
                    if str(ev.get("kind") or "") == "end_state":
                        continue
                    prior_landings.append(str(ev["condition"]))
        # Also carry_forward obligations.
        prior_landings.extend(str(x) for x in (run.get("carry_forward") or []))
        # Dedup preserve order
        seen: set[str] = set()
        unique_prior: list[str] = []
        for c in prior_landings:
            if c not in seen:
                seen.add(c)
                unique_prior.append(c)
        if unique_prior:
            # Check against full log text up to proposal (proposal body alone
            # may not repeat earlier facts — use log + proposal).
            log_text = "\n".join(
                f"{m.speaker}: {m.mes}" for m in log.messages
            )
            combined = log_text + "\n" + body
            plot_ev = evidence_from_criteria_hits(
                combined, unique_prior, span_refs=span_refs,
            )
            details["plot_continuity_evidence"] = [e.to_json() for e in plot_ev]
            lost = missing_conditions(plot_ev)
            if lost:
                _add_failure(
                    failures,
                    structured,
                    GateFailure(
                        code="PLOT_CONTINUITY",
                        message="prior landing no longer holds: " + "; ".join(lost),
                        condition=lost[0],
                        method="match",
                        detail={"lost": lost},
                    ),
                )

        # Character continuity vs dossier brief as_of frontier (match POC).
        as_of = len(log) - 1 if len(log) else 0
        for name in beat.speakers or []:
            d = dossier_mod.load(name)
            if d is None:
                continue
            brief = dossier_mod.project_brief(d, as_of) or ""
            # Extract short fact lines that look like constraints.
            facts = [
                ln.strip(" -•\t")
                for ln in brief.splitlines()
                if ln.strip().startswith(("-", "•", "["))
            ][:6]
            facts = [f for f in facts if len(f) > 12]
            if not facts:
                continue
            # Soft: if proposal explicitly negates a dossier fact phrase.
            body_l = body.lower()
            for fact in facts:
                key = fact.lower()[:40]
                if key and f"not {key}" in body_l:
                    _add_failure(
                        failures,
                        structured,
                        GateFailure(
                            code="CHARACTER_CONTINUITY",
                            message=f"continuity/{name}: contradicts dossier fact",
                            condition=fact,
                            method="match",
                            detail={"speaker": name, "as_of": as_of},
                        ),
                    )

    details["step_k"] = k
    details["N"] = N
    return GateResult(
        ok=not failures,
        failures=failures,
        notes=notes,
        details=details,
        gate_failures=structured,
        evidence=evidence,
    )


def advance_beat(
    *,
    spine: MiniSpine | None = None,
    seed_path: str | Path | None = None,
    log: Log | str | Path | None = None,
    beat_label: str | None = None,
    max_retries: int = 3,
    dry_spark_only: bool = False,
    skip_canon_llm: bool = False,
    spark_text: str | None = None,
    use_draft: bool = True,
    first_speaker: str | None = None,
    on_progress=None,
) -> BeatLoopResult:
    """Spark → write → gate → retry. Stops with awaiting_commit when green.

    If ``spark_text`` is set, or a saved draft matches this beat (when
    ``use_draft``), that hand-prepared note drives write_scene instead of the
    auto-compiler. ``first_speaker`` sets who writes the opening turn.
    """
    if spine is None:
        spine = load_seed(seed_path)
    if log is None:
        log = loader.load(config.working_log())
    elif not isinstance(log, Log):
        log = loader.load(log)

    progress = load_progress()
    completed = {str(x) for x in (progress.get("completed") or [])}
    run = ensure_open_run(spine)

    if beat_label:
        beat = spine.beat_by_label(beat_label)
    else:
        beat = spine.next_open(completed)
    if beat is None:
        return BeatLoopResult(
            beat=None,
            ok=False,
            attempts=0,
            message="no open beat",
            error="no open beat",
        )

    step_k = step_index_for_label(run, beat.label)
    if step_k is None:
        # Beat on spine but not in this run's plan — treat as sequential open index.
        step_k = len(completed) + 1

    hand_text = (spark_text or "").strip() or None
    if hand_text is None and use_draft:
        draft = load_spark_draft()
        if draft and str(draft.get("beat_label") or "") == beat.label:
            hand_text = str(draft.get("text") or "").strip() or None

    failures: list[str] = []
    last_spark: AuthorNote | None = None
    last_write: WriteResult | None = None
    last_gate: GateResult | None = None
    attempt_records: list[dict[str, Any]] = []

    as_of_msg = len(log) - 1 if len(log) else 0

    for attempt in range(1, max_retries + 1):
        if hand_text:
            spark = spark_from_text(
                beat,
                hand_text,
                failure_notes=failures or None,
                first_speaker=first_speaker,
                as_of_msg=as_of_msg,
            )
        else:
            spark = compile_spark(
                beat,
                spine=spine,
                log=log,
                failure_notes=failures or None,
                first_speaker=first_speaker,
                run=run,
                step_k=step_k,
            )
        last_spark = spark
        if dry_spark_only:
            return BeatLoopResult(
                beat=beat,
                ok=True,
                attempts=attempt,
                message=f"spark only — {beat.label}",
                spark=spark,
            )

        try:
            write = write_scene(log, spark, on_progress=on_progress)
        except Exception as exc:  # noqa: BLE001
            failures = [str(exc)]
            transform_mod.discard_pending_edits()
            last_write = None
            attempt_records.append(
                {
                    "attempt": attempt,
                    "ok": False,
                    "error": str(exc),
                    "gate_results": [],
                }
            )
            continue
        last_write = write

        gate = gate_proposal(
            log,
            beat,
            write,
            skip_canon_llm=skip_canon_llm,
            spine=spine,
            run=run,
            step_k=step_k,
        )
        last_gate = gate
        attempt_records.append(
            {
                "attempt": attempt,
                "ok": gate.ok,
                "gate_results": [g.to_json() for g in gate.gate_failures],
                "evidence": [e.to_json() for e in gate.evidence],
                "failures": list(gate.failures),
            }
        )
        if gate.ok:
            attempts_map = dict(progress.get("attempts") or {})
            attempts_map[beat.label] = attempt
            progress["attempts"] = attempts_map
            # Cursor for /edits/commit — Accept in the Fork pane must advance
            # the mini-spine without a separate "mark committed" click.
            progress["awaiting"] = beat.label
            progress["pending_gate"] = {
                "label": beat.label,
                "step_k": step_k,
                "N": int(run.get("N") or 0),
                "run_id": run.get("run_id"),
                "evidence": [e.to_json() for e in gate.evidence],
                "gate": gate.to_json(),
                "attempts": attempt_records,
                "spark_text": (spark.text or "")[:4000],
            }
            attempt_hist = dict(progress.get("attempt_history") or {})
            attempt_hist[beat.label] = list(attempt_records)
            progress["attempt_history"] = attempt_hist
            save_progress(progress)
            if hand_text:
                clear_spark_draft()
            return BeatLoopResult(
                beat=beat,
                ok=True,
                attempts=attempt,
                message=(
                    f"beat {beat.label} gated green after {attempt} attempt(s) — "
                    "awaiting human commit"
                    + (" (hand spark)" if hand_text else "")
                ),
                spark=spark,
                write=write,
                gate=gate,
                awaiting_commit=True,
            )
        failures = list(gate.failures)
        transform_mod.discard_pending_edits()

    # Persist failed attempt history for export / retry UI.
    attempt_hist = dict(progress.get("attempt_history") or {})
    attempt_hist[beat.label] = list(attempt_records)
    progress["attempt_history"] = attempt_hist
    attempts_map = dict(progress.get("attempts") or {})
    attempts_map[beat.label] = max_retries
    progress["attempts"] = attempts_map
    save_progress(progress)

    err = "; ".join(failures) or "retries exhausted"
    return BeatLoopResult(
        beat=beat,
        ok=False,
        attempts=max_retries,
        message=f"beat {beat.label} failed: {err}",
        spark=last_spark,
        write=last_write,
        gate=last_gate,
        error=err,
    )


def mark_beat_committed(
    beat_label: str,
    *,
    backup: str | Path | None = None,
    msg_range: tuple[int, int] | list[int] | None = None,
    earnedness: int | None = None,
    earnedness_note: str | None = None,
) -> dict[str, Any]:
    """Mark a beat complete and record its pre-commit log backup for undo.

    When trajectory fields are present (pending_gate / run), also append a
    step record with earnedness and update residuals.
    """
    progress = load_progress()
    done = list(progress.get("completed") or [])
    if beat_label not in done:
        done.append(beat_label)
    progress["completed"] = done
    if progress.get("awaiting") == beat_label:
        progress.pop("awaiting", None)
    pending = progress.get("pending_gate") or {}
    if str(pending.get("label") or "") != beat_label:
        pending = {}

    commits = list(progress.get("commits") or [])
    # Replace a prior record for the same label (re-advance after reopen).
    commits = [c for c in commits if str(c.get("label") or "") != beat_label]
    entry: dict[str, Any] = {"label": beat_label}
    if backup:
        entry["backup"] = str(backup)
    if msg_range is not None and len(msg_range) == 2:
        entry["msg_range"] = [int(msg_range[0]), int(msg_range[1])]
    if earnedness is not None:
        e = int(earnedness)
        if e < 1 or e > 5:
            raise ValueError("earnedness must be an integer 1–5")
        entry["earnedness"] = e
    if earnedness_note:
        entry["earnedness_note"] = str(earnedness_note)
    commits.append(entry)
    progress["commits"] = commits
    # A new accept invalidates any forward redo stack.
    progress.pop("redo", None)

    # Trajectory run step + residuals (backward-compatible no-op if no run).
    run = load_run(str(pending.get("run_id") or "") or None)
    if run is None:
        try:
            run = ensure_open_run()
        except Exception:  # noqa: BLE001
            run = None
    if run is not None:
        step_k = int(pending.get("step_k") or step_index_for_label(run, beat_label) or 0)
        N = int(run.get("N") or pending.get("N") or 0)
        evidence = list(pending.get("evidence") or [])
        end_set = {str(x) for x in (run.get("end_state") or [])}
        tagged: list[dict[str, Any]] = []
        for ev in evidence:
            row = dict(ev)
            cond = str(row.get("condition") or "")
            row["kind"] = "end_state" if cond in end_set else "criteria"
            tagged.append(row)
        end_hits = [
            e for e in tagged if e.get("kind") == "end_state" and e.get("satisfied")
        ]
        update_residuals(run, new_evidence=end_hits)

        criteria: list[str] = []
        title = ""
        try:
            b = load_seed().beat_by_label(beat_label)
            if b:
                criteria = list(b.criteria)
                title = b.title
        except Exception:  # noqa: BLE001
            pass

        step_rec: dict[str, Any] = {
            "beat_label": beat_label,
            "title": title,
            "k": step_k,
            "N": N,
            "criteria": criteria,
            "attempts": list(
                pending.get("attempts")
                or (progress.get("attempt_history") or {}).get(beat_label)
                or []
            ),
            "human_verdict": "accept",
            "evidence": tagged,
            "residuals_after": list(run.get("residuals") or []),
            "msg_range": (
                [int(msg_range[0]), int(msg_range[1])]
                if msg_range is not None and len(msg_range) == 2
                else None
            ),
            "spark_ref": (pending.get("spark_text") or "")[:500],
        }
        if earnedness is not None:
            step_rec["earnedness"] = int(earnedness)
        if earnedness_note:
            step_rec["earnedness_note"] = str(earnedness_note)
        steps = [
            s for s in (run.get("steps") or [])
            if str(s.get("beat_label")) != beat_label
        ]
        steps.append(step_rec)
        run["steps"] = steps
        if N and len(steps) >= N and not (run.get("residuals") or []):
            run["status"] = "complete"
        save_run(run)
        progress["active_run_id"] = run.get("run_id")

    progress.pop("pending_gate", None)
    save_progress(progress)
    return progress


def _restore_log(backup: str | Path) -> Path:
    from .. import backup as backup_mod

    log_path = Path(config.working_log())
    return backup_mod.restore(log_path, from_backup=backup)


def undo_author_commit(
    beat_label: str | None = None,
    *,
    restore_log: bool = True,
) -> dict[str, Any]:
    """Reopen a committed beat (and every beat after it).

    Restores the log to the pre-accept backup for that beat when available,
    snapshots the current log onto a redo stack, and clears completed marks
    from the target beat forward.
    """
    from .. import backup as backup_mod

    progress = load_progress()
    done = [str(x) for x in (progress.get("completed") or [])]
    commits = list(progress.get("commits") or [])
    if not done:
        raise ValueError("no accepted beats to undo")

    label = (beat_label or "").strip() or done[-1]
    if label not in done:
        raise ValueError(f"beat {label} is not in the completed list")

    idx = done.index(label)
    removed_labels = done[idx:]
    kept_done = done[:idx]

    # Prefer the recorded commit backup for the target beat.
    commit_by_label = {
        str(c.get("label") or ""): c for c in commits if isinstance(c, dict)
    }
    target_commit = commit_by_label.get(label) or {}
    backup = str(target_commit.get("backup") or "").strip()

    removed_commits = [
        c for c in commits
        if str(c.get("label") or "") in set(removed_labels)
    ]
    kept_commits = [
        c for c in commits
        if str(c.get("label") or "") in set(kept_done)
    ]

    log_path = Path(config.working_log())
    restored: str | None = None
    snapshot: str | None = None
    if restore_log and log_path.exists():
        snapshot_path = backup_mod.backup(
            log_path, label=f"author-redo-{'-'.join(removed_labels)}",
        )
        snapshot = str(snapshot_path)
        if backup and Path(backup).exists():
            restored = str(_restore_log(backup))
        elif label == done[-1]:
            # Legacy progress without a commits stack — undo latest transform backup.
            try:
                restored = str(transform_mod.undo(log_path))
            except FileNotFoundError:
                restored = None
        # If we couldn't restore, keep the snapshot for redo of a soft reopen.
        transform_mod.discard_pending_edits()

    redo = list(progress.get("redo") or [])
    redo.append({
        "labels": removed_labels,
        "commits": removed_commits,
        "snapshot": snapshot,
    })
    progress["redo"] = redo[-12:]  # cap stack depth
    progress["completed"] = kept_done
    progress["commits"] = kept_commits
    if progress.get("awaiting") in removed_labels:
        progress.pop("awaiting", None)
    # Soft-clear awaiting whenever we reopen — cursor returns to the undone beat.
    progress.pop("awaiting", None)
    clear_spark_draft()
    save_progress(progress)
    return {
        "ok": True,
        "undone": removed_labels,
        "completed": kept_done,
        "restored": restored,
        "log_restored": bool(restored),
        "progress": progress,
    }


def redo_author_commit() -> dict[str, Any]:
    """Re-apply the most recent author undo (log snapshot + completed marks)."""
    from .. import backup as backup_mod

    progress = load_progress()
    redo = list(progress.get("redo") or [])
    if not redo:
        raise ValueError("nothing to redo")
    frame = redo.pop()
    labels = [str(x) for x in (frame.get("labels") or [])]
    commits = list(frame.get("commits") or [])
    snapshot = str(frame.get("snapshot") or "").strip()

    log_restored = False
    restored: str | None = None
    if snapshot and Path(snapshot).exists():
        restored = str(backup_mod.restore(config.working_log(), from_backup=snapshot))
        log_restored = True
        transform_mod.discard_pending_edits()

    done = [str(x) for x in (progress.get("completed") or [])]
    for lab in labels:
        if lab not in done:
            done.append(lab)
    existing = {str(c.get("label") or "") for c in (progress.get("commits") or [])}
    merged_commits = list(progress.get("commits") or [])
    for c in commits:
        lab = str(c.get("label") or "")
        if lab and lab not in existing:
            merged_commits.append(c)
            existing.add(lab)

    progress["completed"] = done
    progress["commits"] = merged_commits
    progress["redo"] = redo
    progress.pop("awaiting", None)
    save_progress(progress)
    return {
        "ok": True,
        "redone": labels,
        "completed": done,
        "restored": restored,
        "log_restored": log_restored,
        "progress": progress,
    }


def beat_label_from_edit_set(edit_set) -> str | None:
    """Parse ``spark A1`` notes / locators written by write_scene."""
    note = getattr(edit_set, "note", "") or ""
    m = re.search(r"\bspark\s+([A-Za-z0-9_-]+)\b", note, re.I)
    if m:
        return m.group(1)
    locator = getattr(edit_set, "locator", "") or ""
    m = re.search(r"·\s*([A-Za-z0-9_-]+)\s*$", locator)
    if m:
        return m.group(1)
    progress = load_progress()
    awaiting = progress.get("awaiting")
    return str(awaiting) if awaiting else None


def on_author_edits_committed(
    edit_set,
    *,
    backup: str | Path | None = None,
    earnedness: int | None = None,
    earnedness_note: str | None = None,
) -> str | None:
    """If this commit was an Author Studio scene, advance the beat cursor.

    Returns the label marked complete, or None. ``backup`` is the pre-commit
    log snapshot from ``commit_edits`` so Accept can later be undone.
    """
    if getattr(edit_set, "operator", "") != "author_write_scene":
        return None
    label = beat_label_from_edit_set(edit_set)
    if not label:
        return None
    injects = [e for e in (edit_set.edits or []) if getattr(e, "kind", "") == "inject"]
    msg_range = None
    if injects:
        after = int(injects[0].msg_id)
        n_inj = len(injects)
        msg_range = (after + 1, after + n_inj)
    # Trajectory POC: require earnedness when the run has a destination.
    progress = load_progress()
    pending = progress.get("pending_gate") or {}
    run_for_req = load_run(str(pending.get("run_id") or "") or None)
    has_destination = bool(
        (run_for_req or {}).get("end_state")
        or (pending.get("evidence") and (run_for_req or {}).get("end_state"))
    )
    if not has_destination:
        try:
            has_destination = bool(load_seed().end_state)
        except Exception:  # noqa: BLE001
            has_destination = False
    if (
        has_destination
        and str(pending.get("label") or "") == label
        and earnedness is None
    ):
        raise ValueError(
            "earnedness (1–5) is required when accepting a trajectory step"
        )
    mark_beat_committed(
        label,
        backup=backup,
        msg_range=msg_range,
        earnedness=earnedness,
        earnedness_note=earnedness_note,
    )
    return label


def _commit_msg_range(progress: dict[str, Any], label: str) -> tuple[int, int] | None:
    """Optional ``msg_range`` recorded on Accept (newer progress files)."""
    for c in progress.get("commits") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("label") or "") != label:
            continue
        rng = c.get("msg_range")
        if (
            isinstance(rng, (list, tuple))
            and len(rng) == 2
            and all(isinstance(x, int) for x in rng)
        ):
            return int(rng[0]), int(rng[1])
    return None


def _msgs_for_range(
    log: Log, start: int, end: int, covered: set[int],
) -> tuple[list[int], list[dict[str, Any]]]:
    n = len(log)
    start = max(0, start)
    end = min(n - 1, end)
    if end < start:
        return [], []
    ids: list[int] = []
    messages: list[dict[str, Any]] = []
    for mid in range(start, end + 1):
        m = log.get(mid)
        ids.append(mid)
        covered.add(mid)
        messages.append({
            "msg_id": mid,
            "speaker": m.speaker,
            "text": m.text,
        })
    return ids, messages


def _infer_span_for_beat(
    beat: MiniBeat,
    log: Log,
    unclaimed: list[int],
) -> tuple[int, int] | None:
    """Guess a span from location / criteria when no commit range exists."""
    if not unclaimed:
        return None
    loc = (beat.location or "").strip().lower()
    criteria = [c.strip().lower() for c in (beat.criteria or []) if c.strip()]
    stop = {
        "the", "and", "from", "with", "that", "this", "her", "his", "she",
        "him", "they", "them", "for", "into", "onto", "over", "under",
        "offers", "accepts", "takes", "reads", "arrives", "mentioned",
    }
    speakers = {s.strip().lower() for s in (beat.speakers or []) if s.strip()}
    stop |= speakers
    title_bits = [
        w for w in re.split(r"\W+", (beat.title or "").lower())
        if len(w) > 2 and w not in stop
    ]
    # Short distinctive words from criteria ("tea", "cup", "letter", …).
    crit_bits: list[str] = []
    for c in criteria:
        for w in re.split(r"\W+", c):
            if len(w) >= 3 and w not in stop:
                crit_bits.append(w)

    def score(mid: int) -> int:
        text = (log.get(mid).text or "").lower()
        head = text[:200]
        s = 0
        if loc and loc in head:
            s += 5
        elif loc and loc in text:
            s += 2
        for c in criteria:
            token = max(re.split(r"\W+", c), key=len, default="")
            if token and len(token) > 3 and token in text:
                s += 3
        for w in crit_bits:
            if w in text:
                s += 1
        for w in title_bits:
            if w in text:
                s += 1
        return s

    scored = {mid: score(mid) for mid in unclaimed}
    best = max(scored.values(), default=0)
    if best <= 0:
        return None
    # Strong hits near the best score; require at least a location/title signal.
    thresh = max(2, best - 2)
    hits = sorted(mid for mid, s in scored.items() if s >= thresh)
    if not hits:
        return None
    # Prefer one contiguous cluster (seed logs revisit locations later).
    clusters: list[list[int]] = [[hits[0]]]
    for mid in hits[1:]:
        if mid == clusters[-1][-1] + 1:
            clusters[-1].append(mid)
        else:
            clusters.append([mid])

    def cluster_score(c: list[int]) -> tuple[int, int]:
        return (sum(scored[m] for m in c), len(c))

    best_cluster = max(clusters, key=cluster_score)
    u_set = set(unclaimed)
    start, end = best_cluster[0], best_cluster[-1]
    # Grow through weakly matching neighbors so tea/study isn't split mid-scene.
    while start - 1 in u_set and scored.get(start - 1, 0) > 0:
        start -= 1
    while end + 1 in u_set and scored.get(end + 1, 0) > 0:
        end += 1
    block = [m for m in unclaimed if start <= m <= end]
    if not block:
        return None
    return block[0], block[-1]


def build_author_story(
    *,
    spine: MiniSpine | None = None,
    seed_path: str | Path | None = None,
    log: Log | str | Path | None = None,
) -> dict[str, Any]:
    """Linear A1→An view of the *current* working log, grouped by mini-spine beat.

    Never reads backups/redo snapshots. Spans come from the ``msg_range``
    recorded on Accept, then location/criteria inference for completed beats
    that have none (e.g. A1).
    """

    if spine is None:
        spine = load_seed(seed_path)
    if log is None:
        log = loader.load(config.working_log())
    elif not isinstance(log, Log):
        log = loader.load(log)

    progress = load_progress()
    completed = {str(x) for x in (progress.get("completed") or [])}
    awaiting = str(progress.get("awaiting") or "") or None

    pending = None
    try:
        pending = transform_mod.load_pending_edits()
    except Exception:
        pending = None
    pending_label = beat_label_from_edit_set(pending) if pending else None

    n = len(log)
    covered: set[int] = set()
    beats_out: list[dict[str, Any]] = []

    # Pass 1 — recorded commit ranges.
    for beat in spine.beats:
        status = "missing"
        msg_range: list[int] | None = None
        messages: list[dict[str, Any]] = []
        span = _commit_msg_range(progress, beat.label)

        if span is not None:
            _ids, messages = _msgs_for_range(log, span[0], span[1], covered)
            if messages:
                msg_range = [messages[0]["msg_id"], messages[-1]["msg_id"]]
                status = "on_log"

        if (
            not messages
            and awaiting
            and beat.label == awaiting
            and pending is not None
            and pending_label == beat.label
        ):
            status = "awaiting"
            injects = [
                e for e in (pending.edits or [])
                if getattr(e, "kind", "") == "inject"
            ]
            for e in reversed(injects):
                messages.append({
                    "msg_id": int(e.msg_id),
                    "speaker": e.speaker,
                    "text": e.after,
                    "pending": True,
                })
        elif beat.label == awaiting and not messages:
            status = "awaiting"
        elif beat.label in completed and messages:
            status = "on_log"
        elif beat.label not in completed and not messages:
            status = "missing"

        beats_out.append({
            "label": beat.label,
            "title": beat.title,
            "status": status,
            "msg_range": msg_range,
            "criteria": list(beat.criteria or []),
            "messages": messages,
            "_beat": beat,  # stripped before return
        })

    # Pass 2 — completed beats still empty: infer from location / criteria.
    unclaimed = [i for i in range(n) if i not in covered]
    for row in beats_out:
        if row["messages"] or row["label"] not in completed:
            continue
        beat = row.pop("_beat", None) or next(
            (b for b in spine.beats if b.label == row["label"]), None
        )
        if beat is None:
            continue
        inferred = _infer_span_for_beat(beat, log, unclaimed)
        if inferred is None:
            continue
        _ids, messages = _msgs_for_range(log, inferred[0], inferred[1], covered)
        unclaimed = [i for i in unclaimed if i not in covered]
        if not messages:
            continue
        row["messages"] = messages
        row["msg_range"] = [messages[0]["msg_id"], messages[-1]["msg_id"]]
        row["status"] = "on_log"
        # Display-only inference — nothing is written on GET.

    for row in beats_out:
        row.pop("_beat", None)

    prologue: list[dict[str, Any]] = []
    for mid in range(n):
        if mid in covered:
            continue
        m = log.get(mid)
        prologue.append({
            "msg_id": mid,
            "speaker": m.speaker,
            "text": m.text,
        })

    return {
        "title": spine.title,
        "beats": beats_out,
        "prologue": prologue,
        "message_count": n,
        "progress": {
            "completed": list(progress.get("completed") or []),
            "awaiting": awaiting,
        },
    }


def walk_beats(
    *,
    spine: MiniSpine | None = None,
    seed_path: str | Path | None = None,
    log: Log | str | Path | None = None,
    max_beats: int = 3,
    max_retries: int = 3,
    skip_canon_llm: bool = False,
    on_awaiting_commit: OnAwaitCommit | None = None,
    on_progress=None,
) -> list[BeatLoopResult]:
    if spine is None:
        spine = load_seed(seed_path)
    if log is None:
        log = loader.load(config.working_log())
    elif not isinstance(log, Log):
        log = loader.load(log)

    results: list[BeatLoopResult] = []
    for _ in range(max_beats):
        log = loader.load(log.path)
        result = advance_beat(
            spine=spine,
            log=log,
            max_retries=max_retries,
            skip_canon_llm=skip_canon_llm,
            on_progress=on_progress,
        )
        results.append(result)
        if not result.ok or not result.awaiting_commit:
            break
        if on_awaiting_commit is None:
            break
        if not bool(on_awaiting_commit(result)):
            break
        if result.beat:
            mark_beat_committed(result.beat.label)
    return results
