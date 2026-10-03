"""Which operations are legal on which layer, and which layer may reach ST.

The two layers are not two views of one text; they are two documents with a
one-way arrow between them:

    log  ──novelize──▶  manuscript

Most of the damage this tool could do is a transform applied to the wrong one.
Syncing a novelized scene toward SillyTavern would overwrite roleplay turns with
prose that can never be turned back into turns. It is one wrong dropdown away,
and it does not announce itself afterwards — the file just quietly stops being
what you thought it was.

So the rule is a refusal, not a warning: an operation names its target layer, the
target is checked here, and an illegal pair raises. The GUI's job is then only to
avoid offering the impossible; correctness does not depend on it managing to.

Legality and existence are two different questions, and this module answers both,
because a client that asks one place and guesses the other ends up offering a
button that 500s. ``layers`` is what the design permits; ``runs_on`` is the
subset of that an operator has actually been written for, with ``pending``
saying in a sentence why the rest are not on offer yet.
"""

from __future__ import annotations

from dataclasses import dataclass

LOG = "log"
MANUSCRIPT = "manuscript"

# Ordered: each layer is derived from its predecessor.
LAYERS: tuple[str, ...] = (LOG, MANUSCRIPT)

# Only the log may travel back to SillyTavern, and only by explicit command.
SYNCABLE: frozenset[str] = frozenset({LOG})


class LayerViolation(Exception):
    """An operation was aimed at a layer it must never touch."""


class OpUnavailable(Exception):
    """The pair is legal, but no operator has been written for it yet."""


@dataclass(frozen=True)
class Op:
    name: str
    layers: frozenset[str]
    what: str
    runs_on: frozenset[str]
    pending: str = ""

    def allows(self, layer: str) -> bool:
        return layer in self.layers

    def runs(self, layer: str) -> bool:
        return layer in self.runs_on


def _op(
    name: str,
    layers: tuple[str, ...],
    what: str,
    *,
    runs_on: tuple[str, ...] | None = None,
    pending: str = "",
) -> Op:
    """``runs_on`` defaults to every legal layer — the ordinary case, where the
    operator exists everywhere the design allows it."""
    implemented = layers if runs_on is None else runs_on
    return Op(
        name=name,
        layers=frozenset(layers),
        what=what,
        runs_on=frozenset(implemented),
        pending=pending,
    )


OPS: dict[str, Op] = {
    # Log-layer operators — the existing engine. These rewrite roleplay turns,
    # and their results are the only thing allowed to sync back to ST.
    "restyle": _op(
        "restyle", (LOG, MANUSCRIPT), "rewrite prose to a note",
        runs_on=(LOG,),
        pending="Only the log operator is written. A restyle of the manuscript "
                "would have to rewrite prose blocks rather than turns, and it "
                "would owe the scene's drift record an answer afterwards.",
    ),
    "retune": _op("retune", (LOG,), "shift a character's register in place"),
    "weed": _op(
        "weed", (LOG,), "pull machine phrases out of sentences",
        runs_on=(LOG,),
    ),
    "copyedit": _op(
        "copyedit", (LOG,), "find and fix typos without rewriting",
        runs_on=(LOG,),
    ),
    "stamps": _op(
        "stamps", (LOG,), "infer missing chronicle / location headers",
        runs_on=(LOG,),
    ),
    "inject": _op("inject", (LOG,), "add a new turn"),
    "remove": _op(
        "remove", (LOG,), "take a turn out of the log",
        runs_on=(LOG,),
    ),
    "sweep": _op("sweep", (LOG,), "propagate a change downstream"),
    # Derivation pipelines. Each reads the layer below and writes the one named.
    "novelize": _op("novelize", (MANUSCRIPT,), "turn log turns into prose"),
    # Manuscript-and-below editing verbs. Log deletion is `remove` above —
    # propose, then accept — because a hard delete shifts every later id.
    "shorten": _op(
        "shorten", (MANUSCRIPT,), "compress without losing load",
        runs_on=(),
        pending="Not written yet. Compress scene, under the novel page, prunes "
                "repetition; shortening a passage that carries load is harder.",
    ),
    "extend": _op(
        "extend", (MANUSCRIPT,), "open a compressed passage out",
        runs_on=(),
        pending="Not written yet. Retune does this on the log; the manuscript "
                "equivalent waits on the same block-level rewrite restyle does.",
    ),
    "edit": _op("edit", (MANUSCRIPT,), "hand edit"),
    "annotate": _op(
        "annotate", (LOG, MANUSCRIPT), "label who spoke",
        runs_on=(LOG,),
        pending="Prose has no turns to label; who spoke is marked on the log.",
    ),
    "patch": _op(
        "patch", (LOG,), "copy a scene out, edit, approve back",
        runs_on=(LOG,),
    ),
}


def is_layer(layer: str) -> bool:
    return layer in LAYERS


def parent_of(layer: str) -> str | None:
    """The layer this one is derived from. None for the log, which is imported."""
    if layer not in LAYERS:
        raise LayerViolation(f"unknown layer: {layer!r}")
    index = LAYERS.index(layer)
    return LAYERS[index - 1] if index else None


def check(op: str, layer: str) -> Op:
    """Raise unless ``op`` is legal on ``layer``. Returns the op descriptor."""
    if layer not in LAYERS:
        raise LayerViolation(
            f"unknown layer {layer!r} — expected one of {', '.join(LAYERS)}"
        )
    known = OPS.get(op)
    if known is None:
        raise LayerViolation(
            f"unknown operation {op!r} — expected one of {', '.join(sorted(OPS))}"
        )
    if not known.allows(layer):
        allowed = ", ".join(sorted(known.layers))
        raise LayerViolation(
            f"{op} cannot target the {layer} layer (allowed: {allowed}) — "
            f"{known.what}"
        )
    return known


def check_syncable(layer: str) -> None:
    """Raise unless this layer is allowed to reach the SillyTavern chat file."""
    if layer not in LAYERS:
        raise LayerViolation(f"unknown layer: {layer!r}")
    if layer not in SYNCABLE:
        raise LayerViolation(
            f"the {layer} layer never syncs to SillyTavern — novelized prose "
            "cannot be turned back into roleplay turns, so a push would "
            "destroy the source it came from"
        )


def runs(op: str, layer: str) -> bool:
    """Whether an operator exists for this pair, legality aside."""
    known = OPS.get(op)
    return bool(known and known.runs(layer))


def check_runs(op: str, layer: str) -> Op:
    """Raise unless the pair is both legal and implemented. ``LayerViolation``
    means never; ``OpUnavailable`` means not yet, and carries the sentence the
    GUI shows beside the disabled verb."""
    known = check(op, layer)
    if not known.runs(layer):
        raise OpUnavailable(
            f"{op} on the {layer} layer is not implemented yet — {known.pending}"
        )
    return known


def ops_for(layer: str) -> list[str]:
    """Every op the design permits here — including ones with no operator yet,
    which a client shows greyed rather than hiding."""
    return sorted(name for name, op in OPS.items() if op.allows(layer))


def runnable_for(layer: str) -> list[str]:
    """The subset of ``ops_for`` this engine can actually run today."""
    return sorted(name for name, op in OPS.items() if op.runs(layer))


def describe() -> dict[str, object]:
    """Machine-readable capability map, for a client that would rather ask than
    hard-code these rules in a second place."""
    return {
        "layers": list(LAYERS),
        "syncable": sorted(SYNCABLE),
        "ops": {
            name: {
                "layers": sorted(op.layers),
                "what": op.what,
                "runs_on": sorted(op.runs_on),
                "pending": op.pending,
            }
            for name, op in OPS.items()
        },
    }
