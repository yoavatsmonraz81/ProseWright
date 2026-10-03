"""Method-tagged condition evidence + structured gate failures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

EvidenceMethod = Literal["match", "llm", "human"]

GateCode = Literal[
    "END_STATE_EARLY",
    "END_STATE_INCOMPLETE",
    "CRITERIA_MISSING",
    "CHARACTER_OOC",
    "CHARACTER_CONTINUITY",
    "PLOT_CONTINUITY",
    "DOSSIER_OPEN",
    "INITIAL_STATE_CONTRADICTION",
]


@dataclass
class ConditionEvidence:
    condition: str
    satisfied: bool
    method: EvidenceMethod = "match"
    confidence: float = 1.0
    span_refs: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "satisfied": self.satisfied,
            "method": self.method,
            "confidence": float(self.confidence),
            "span_refs": list(self.span_refs),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> ConditionEvidence:
        method = str(d.get("method") or "match")
        if method not in ("match", "llm", "human"):
            method = "match"
        return cls(
            condition=str(d.get("condition") or ""),
            satisfied=bool(d.get("satisfied")),
            method=method,  # type: ignore[arg-type]
            confidence=float(d.get("confidence") or 0.0),
            span_refs=[str(x) for x in (d.get("span_refs") or [])],
        )


@dataclass
class GateFailure:
    code: str
    message: str
    condition: str = ""
    method: EvidenceMethod = "match"
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "condition": self.condition,
            "method": self.method,
            "detail": dict(self.detail),
            "message": self.message,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> GateFailure:
        method = str(d.get("method") or "match")
        if method not in ("match", "llm", "human"):
            method = "match"
        return cls(
            code=str(d.get("code") or "UNKNOWN"),
            message=str(d.get("message") or ""),
            condition=str(d.get("condition") or ""),
            method=method,  # type: ignore[arg-type]
            detail=dict(d.get("detail") or {}),
        )


def evidence_from_criteria_hits(
    text: str,
    conditions: list[str],
    *,
    span_refs: list[str] | None = None,
) -> list[ConditionEvidence]:
    """Score conditions with the existing match heuristic; tag method=match."""
    from .criteria import criteria_hits

    ok, missing = criteria_hits(text, conditions)
    ok_set = set(ok)
    refs = list(span_refs or [])
    out: list[ConditionEvidence] = []
    for c in conditions:
        raw = (c or "").strip()
        if not raw:
            continue
        out.append(
            ConditionEvidence(
                condition=raw,
                satisfied=raw in ok_set,
                method="match",
                confidence=1.0 if raw in ok_set else 0.0,
                span_refs=refs,
            )
        )
    # Preserve order of input; missing only listed for completeness in hits helper
    _ = missing
    return out


def all_satisfied(evidence: list[ConditionEvidence]) -> bool:
    return bool(evidence) and all(e.satisfied for e in evidence)


def satisfied_conditions(evidence: list[ConditionEvidence]) -> list[str]:
    return [e.condition for e in evidence if e.satisfied]


def missing_conditions(evidence: list[ConditionEvidence]) -> list[str]:
    return [e.condition for e in evidence if not e.satisfied]
