"""What may happen where, and what has actually been written.

`layers.py` answers two questions that are easy to conflate: whether an operation
is *allowed* on a layer, and whether an operator for it *exists*. A GUI that asks
one and assumes the other either hides a working verb or offers a button that
fails, so these tests pin both answers and the difference between them.
"""

from __future__ import annotations

import pytest

from story_editor import layers


# --------------------------------------------------------------------------- #
# Legality — the refusals
# --------------------------------------------------------------------------- #


def test_the_log_is_the_only_syncable_layer() -> None:
    layers.check_syncable(layers.LOG)
    for derived in (layers.MANUSCRIPT,):
        with pytest.raises(layers.LayerViolation):
            layers.check_syncable(derived)


def test_hand_editing_a_roleplay_turn_is_refused() -> None:
    layers.check("edit", layers.MANUSCRIPT)
    with pytest.raises(layers.LayerViolation):
        layers.check("edit", layers.LOG)


def test_unknown_layers_and_operations_raise() -> None:
    with pytest.raises(layers.LayerViolation):
        layers.check("restyle", "archive")
    with pytest.raises(layers.LayerViolation):
        layers.check("obliterate", layers.LOG)


def test_parent_of_walks_one_way() -> None:
    assert layers.parent_of(layers.LOG) is None
    assert layers.parent_of(layers.MANUSCRIPT) == layers.LOG


# --------------------------------------------------------------------------- #
# Existence — legal, but not written yet
# --------------------------------------------------------------------------- #


def test_every_op_runs_on_a_subset_of_its_legal_layers() -> None:
    for name, op in layers.OPS.items():
        assert op.runs_on <= op.layers, f"{name} claims to run where it is illegal"


def test_a_partly_written_op_says_why_in_a_sentence() -> None:
    for name, op in layers.OPS.items():
        if op.runs_on == op.layers:
            continue
        assert op.pending, f"{name} is not fully implemented and says nothing about it"


def test_restyle_is_legal_on_the_manuscript_but_not_written_for_it() -> None:
    layers.check("restyle", layers.MANUSCRIPT)  # legal
    assert not layers.runs("restyle", layers.MANUSCRIPT)
    with pytest.raises(layers.OpUnavailable) as raised:
        layers.check_runs("restyle", layers.MANUSCRIPT)
    assert "not implemented yet" in str(raised.value)
    assert layers.OPS["restyle"].pending in str(raised.value)


def test_never_and_not_yet_are_different_refusals() -> None:
    """A client shows one as a greyed verb and the other not at all, so they must
    not arrive as the same exception."""
    with pytest.raises(layers.LayerViolation):
        layers.check_runs("retune", layers.MANUSCRIPT)
    with pytest.raises(layers.OpUnavailable):
        layers.check_runs("shorten", layers.MANUSCRIPT)


def test_check_runs_returns_the_op_when_it_can_run() -> None:
    assert layers.check_runs("restyle", layers.LOG).name == "restyle"
    assert layers.check_runs("weed", layers.LOG).name == "weed"
    assert layers.check_runs("copyedit", layers.LOG).name == "copyedit"
    assert layers.check_runs("stamps", layers.LOG).name == "stamps"
    assert layers.check_runs("patch", layers.LOG).name == "patch"
    assert layers.check_runs("remove", layers.LOG).name == "remove"
    assert layers.check_runs("novelize", layers.MANUSCRIPT).name == "novelize"


def test_runnable_for_is_what_the_gui_may_offer() -> None:
    log_verbs = layers.runnable_for(layers.LOG)
    assert {"restyle", "retune", "weed", "copyedit", "stamps", "inject", "remove", "sweep", "patch"} <= set(log_verbs)
    assert "novelize" not in log_verbs
    assert layers.runnable_for(layers.MANUSCRIPT) == ["edit", "novelize"]


# --------------------------------------------------------------------------- #
# The capability map a client reads instead of hard-coding these rules
# --------------------------------------------------------------------------- #


def test_describe_carries_both_answers_for_every_op() -> None:
    described = layers.describe()
    assert described["layers"] == ["log", "manuscript"]
    assert described["syncable"] == ["log"]
    ops = described["ops"]
    assert set(ops) == set(layers.OPS)
    for name, entry in ops.items():
        assert entry["what"], f"{name} does not say what it does"
        assert set(entry["runs_on"]) <= set(entry["layers"])
        assert entry["layers"] == sorted(entry["layers"])


def test_describe_reports_the_unwritten_verbs_honestly() -> None:
    ops = layers.describe()["ops"]
    assert ops["restyle"]["runs_on"] == ["log"]
    assert ops["novelize"]["runs_on"] == ["manuscript"]
    assert ops["inject"]["pending"] == ""
    assert "voice" not in ops and "interlude" not in ops
    assert ops["weed"]["runs_on"] == ["log"]
    assert ops["weed"]["pending"] == ""
    assert ops["copyedit"]["runs_on"] == ["log"]
    assert ops["copyedit"]["pending"] == ""
    assert ops["stamps"]["runs_on"] == ["log"]
    assert ops["stamps"]["pending"] == ""
