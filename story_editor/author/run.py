"""Author run records — N + beat_plan instance for a trajectory walk."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import config
from .seed import MiniSpine, load_seed


def runs_dir() -> Path:
    d = Path(config.WORKSPACE_DIR) / "author_runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_run_path() -> Path:
    return Path(config.WORKSPACE_DIR) / "author_active_run.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def beat_plan_from_spine(spine: MiniSpine) -> list[dict[str, Any]]:
    return [
        {
            "label": b.label,
            "title": b.title,
            "criteria": list(b.criteria),
            "location": b.location,
            "speakers": list(b.speakers),
            "notes": b.notes,
        }
        for b in spine.beats
    ]


def new_run(
    spine: MiniSpine | None = None,
    *,
    N: int | None = None,
    beat_plan: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if spine is None:
        spine = load_seed()
    plan = beat_plan if beat_plan is not None else beat_plan_from_spine(spine)
    n = int(N) if N is not None else len(plan)
    if n < 1:
        raise ValueError("run N must be >= 1")
    if len(plan) != n:
        # Allow explicit N with a matching plan; truncate/pad is caller's job.
        if beat_plan is None and n != len(spine.beats):
            raise ValueError(
                f"N={n} does not match spine beat count {len(spine.beats)}; "
                "pass an explicit beat_plan"
            )
        if beat_plan is not None and len(plan) != n:
            raise ValueError(f"beat_plan length {len(plan)} != N={n}")
    rid = (run_id or "").strip() or uuid.uuid4().hex[:12]
    return {
        "run_id": rid,
        "created_at": _utc_now(),
        "spine_title": spine.title,
        "spine_path": str(spine.path or ""),
        "initial_state": list(spine.initial_state),
        "end_state": list(spine.end_state),
        "N": n,
        "beat_plan": plan,
        "steps": [],
        "evidenced_end_state": [],
        "residuals": list(spine.end_state),
        "carry_forward": [],
        "status": "open",
    }


def save_run(run: dict[str, Any]) -> Path:
    rid = str(run.get("run_id") or "").strip()
    if not rid:
        raise ValueError("run_id required")
    p = runs_dir() / f"{rid}.json"
    p.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    active_run_path().write_text(
        json.dumps({"run_id": rid}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return p


def load_run(run_id: str | None = None) -> dict[str, Any] | None:
    rid = (run_id or "").strip()
    if not rid:
        active = active_run_path()
        if not active.exists():
            return None
        try:
            meta = json.loads(active.read_text(encoding="utf-8"))
            rid = str(meta.get("run_id") or "").strip()
        except (OSError, json.JSONDecodeError):
            return None
    if not rid:
        return None
    p = runs_dir() / f"{rid}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def ensure_open_run(spine: MiniSpine | None = None) -> dict[str, Any]:
    """Return active open run, or create one from current spine beats (N=len)."""
    run = load_run()
    if run and str(run.get("status") or "open") == "open":
        return run
    if spine is None:
        spine = load_seed()
    run = new_run(spine)
    save_run(run)
    return run


def step_index_for_label(run: dict[str, Any], label: str) -> int | None:
    """1-based step index k for a beat label in the run plan."""
    for i, b in enumerate(run.get("beat_plan") or []):
        if str(b.get("label") or "") == label:
            return i + 1
    return None


def update_residuals(
    run: dict[str, Any],
    *,
    new_evidence: list[dict[str, Any]],
    carry_forward: list[str] | None = None,
) -> dict[str, Any]:
    """Merge method-tagged end_state evidence; recompute residuals."""
    end_state = [str(x) for x in (run.get("end_state") or [])]
    evidenced = list(run.get("evidenced_end_state") or [])
    by_cond = {str(e.get("condition") or ""): e for e in evidenced if e.get("satisfied")}
    for e in new_evidence:
        if not e.get("satisfied"):
            continue
        cond = str(e.get("condition") or "")
        if cond:
            by_cond[cond] = e
    run["evidenced_end_state"] = list(by_cond.values())
    satisfied = set(by_cond.keys())
    run["residuals"] = [c for c in end_state if c not in satisfied]
    if carry_forward is not None:
        run["carry_forward"] = [str(x) for x in carry_forward if str(x).strip()]
    return run
