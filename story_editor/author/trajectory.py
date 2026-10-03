"""Export a lab-shaped trajectory JSONL from an author run."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .. import config
from .run import load_run


def _plan_hash(beat_plan: list[dict[str, Any]]) -> str:
    raw = json.dumps(beat_plan, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def build_trajectory(run: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return JSONL records: header, steps…, footer."""
    if run is None:
        run = load_run()
    if not run:
        raise ValueError("no active author run to export")

    plan = list(run.get("beat_plan") or [])
    N = int(run.get("N") or len(plan) or 0)
    header: dict[str, Any] = {
        "record": "run",
        "run_id": run.get("run_id"),
        "spine_title": run.get("spine_title"),
        "spine_path": run.get("spine_path"),
        "initial_state": list(run.get("initial_state") or []),
        "end_state": list(run.get("end_state") or []),
        "N": N,
        "beat_plan_hash": _plan_hash(plan),
        "beat_plan": plan,
        "provenance": {
            "workspace": str(config.WORKSPACE_DIR),
            "working_log": str(config.working_log()),
        },
        "status": run.get("status") or "open",
        "created_at": run.get("created_at"),
    }

    lines: list[dict[str, Any]] = [header]
    steps = list(run.get("steps") or [])
    earnedness_vals: list[float] = []
    for step in steps:
        rec = {"record": "step", **step}
        lines.append(rec)
        e = step.get("earnedness")
        if isinstance(e, (int, float)):
            earnedness_vals.append(float(e))

    evidenced = list(run.get("evidenced_end_state") or [])
    end_state = [str(x) for x in (run.get("end_state") or [])]
    by_cond = {str(e.get("condition") or ""): e for e in evidenced}
    final_evidence = []
    methods: set[str] = set()
    all_ok = True
    for cond in end_state:
        ev = by_cond.get(cond)
        if ev and ev.get("satisfied"):
            final_evidence.append(ev)
            methods.add(str(ev.get("method") or "match"))
        else:
            all_ok = False
            final_evidence.append(
                {
                    "condition": cond,
                    "satisfied": False,
                    "method": "match",
                    "confidence": 0.0,
                    "span_refs": [],
                }
            )

    # Prefer strongest method label for the rollup claim.
    if "human" in methods:
        ok_method = "human"
    elif "llm" in methods:
        ok_method = "llm"
    else:
        ok_method = "match"

    footer: dict[str, Any] = {
        "record": "footer",
        "run_id": run.get("run_id"),
        "end_state_ok": all_ok and bool(end_state),
        "end_state_ok_method": ok_method if end_state else None,
        "final_evidence": final_evidence,
        "residuals": list(run.get("residuals") or []),
        "steps_completed": len(steps),
        "N": N,
    }
    if earnedness_vals:
        footer["earnedness_mean"] = sum(earnedness_vals) / len(earnedness_vals)
    lines.append(footer)
    return lines


def trajectory_jsonl(run: dict[str, Any] | None = None) -> str:
    return "\n".join(
        json.dumps(rec, ensure_ascii=False) for rec in build_trajectory(run)
    ) + "\n"


def write_trajectory(
    path: str | Path | None = None,
    run: dict[str, Any] | None = None,
) -> Path:
    lines = trajectory_jsonl(run)
    if path is None:
        rid = (run or load_run() or {}).get("run_id") or "run"
        path = Path(config.WORKSPACE_DIR) / f"trajectory_{rid}.jsonl"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(lines, encoding="utf-8")
    return p
