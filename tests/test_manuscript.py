"""Derived layers: the document model, drift, and the rules about layers.

Drift is the whole point of this module, so most of these tests do the same
thing: build a manuscript scene over a log span, disturb the log underneath it in
one specific way, and assert the scene notices — or, just as important, that it
does not notice a change that left its own source alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from story_editor import layers, loader, manuscript as ms


def _write_log(path: Path, messages: list[tuple[str, str, str]]) -> loader.Log:
    """messages: (uid, speaker, text)."""
    lines = [json.dumps({"chat_metadata": {}})]
    for uid, speaker, text in messages:
        lines.append(json.dumps({
            "name": speaker,
            "mes": text,
            "is_user": speaker == "Wren",
            "extra": {"se_uid": uid},
        }))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return loader.load(path)


def _sample(path: Path) -> loader.Log:
    return _write_log(path, [
        ("m-aaa", "Wren", "Wren set the figurine down."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])


def _scene_over(log: loader.Log, start: int, end: int) -> ms.Scene:
    scene = ms.new_scene(log, start, end, title="The figurine")
    scene.blocks = ms.blocks_from_prose(
        "Wren's fingers lowered the pale king back into her backpack.\n\n"
        "Ilse said nothing at all.",
        src=[log.get(i).uid for i in range(start, end + 1)],
    )
    return scene


# --------------------------------------------------------------------------- #
# Model + persistence
# --------------------------------------------------------------------------- #


def test_new_scene_is_clean_against_its_source(tmp_path: Path) -> None:
    log = _sample(tmp_path / "log.jsonl")
    scene = _scene_over(log, 0, 1)
    assert scene.source_sig
    assert not ms.scene_drift(scene, log).dirty


def test_roundtrip_preserves_blocks_and_provenance(tmp_path: Path) -> None:
    log = _sample(tmp_path / "log.jsonl")
    doc = ms.Document(layer="manuscript")
    doc.upsert(_scene_over(log, 0, 1))
    out = tmp_path / "manuscript.json"
    ms.save(doc, out)

    back = ms.load("manuscript", path=out, log=log)
    assert len(back) == 1
    scene = back.scenes[0]
    assert scene.title == "The figurine"
    assert [b.text for b in scene.blocks] == [
        "Wren's fingers lowered the pale king back into her backpack.",
        "Ilse said nothing at all.",
    ]
    assert scene.blocks[0].src == ["m-aaa", "m-bbb"]
    assert scene.anchor.from_uid == "m-aaa"
    assert scene.anchor.to_uid == "m-bbb"


def test_export_emits_prose_without_metadata(tmp_path: Path) -> None:
    log = _sample(tmp_path / "log.jsonl")
    doc = ms.Document(layer="manuscript")
    approved = _scene_over(log, 0, 1)
    approved.status = "approved"
    draft = _scene_over(log, 2, 3)
    draft.title = "Not yet"
    doc.upsert(approved)
    doc.upsert(draft)

    text = ms.export_text(doc)
    assert "pale king" in text
    # Draft scenes stay out of a build, and no identifier reaches the page.
    assert "Not yet" not in text
    for leak in ("m-aaa", "sc-", "b-", "source_sig"):
        assert leak not in text
    assert "Not yet" in ms.export_text(doc, approved_only=False)


def test_stats_counts_words_and_statuses(tmp_path: Path) -> None:
    log = _sample(tmp_path / "log.jsonl")
    doc = ms.Document(layer="manuscript")
    first = _scene_over(log, 0, 1)
    first.status = "approved"
    doc.upsert(first)
    doc.upsert(_scene_over(log, 2, 3))

    stats = doc.stats()
    assert stats["scenes"] == 2
    assert stats["approved"] == 1
    assert stats["draft"] == 1
    assert stats["words"] > 0


def test_covering_finds_the_scene_for_a_message(tmp_path: Path) -> None:
    log = _sample(tmp_path / "log.jsonl")
    doc = ms.Document(layer="manuscript")
    doc.upsert(_scene_over(log, 0, 1))
    doc.upsert(_scene_over(log, 2, 3))

    assert doc.covering(1) is doc.scenes[0]
    assert doc.covering(3) is doc.scenes[1]


def test_front_matter_does_not_cover_or_derive_from_the_log(tmp_path: Path) -> None:
    log = _sample(tmp_path / "log.jsonl")
    front = ms.new_front_matter_scene(
        blocks=[ms.Block(id="b-front", kind="heading", text="COLLEGIUM", note="h2")],
        title="Prologue — A Letter from the Survey Office",
    )
    doc = ms.Document(layer="manuscript")
    doc.upsert(front)
    doc.upsert(_scene_over(log, 0, 1))

    assert ms.is_front_matter(front)
    assert ms.source_messages(log, front.anchor) == []
    assert front.source_sig == ms.signature_of([])
    assert doc.covering(0) is doc.scenes[1]
    assert doc.covering(-1) is front
    assert not ms.scene_drift(front, log).dirty

    out = tmp_path / "manuscript.json"
    ms.save(doc, out)
    back = ms.load("manuscript", path=out, log=log)
    restored = next(s for s in back.scenes if ms.is_front_matter(s))
    assert (restored.anchor.start, restored.anchor.end) == (-1, -1)
    assert restored.anchor.from_uid == ""
    assert restored.title.startswith("Prologue")


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #


def test_edited_source_message_drifts_the_scene(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 0, 1)

    _write_log(path, [
        ("m-aaa", "Wren", "Wren set the figurine down, hands unsteady."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    drift = ms.scene_drift(scene, loader.load(path))
    assert drift.kind == ms.SOURCE_CHANGED


def test_injection_inside_the_span_drifts_the_scene(tmp_path: Path) -> None:
    """The Phase 0 hazard: no existing message changed, but the span now covers
    prose the novelization never saw."""
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 0, 1)

    _write_log(path, [
        ("m-aaa", "Wren", "Wren set the figurine down."),
        ("m-new", "The Chorus", "The vault smelled of old beeswax."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    drift = ms.scene_drift(scene, loader.load(path))
    assert drift.kind == ms.SOURCE_CHANGED


def test_injection_before_the_span_only_moves_it(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 1, 2)
    assert (scene.anchor.start, scene.anchor.end) == (1, 2)

    _write_log(path, [
        ("m-new", "The Chorus", "A cutaway, inserted at the top."),
        ("m-aaa", "Wren", "Wren set the figurine down."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    moved = loader.load(path)
    scene.bind(moved)
    assert (scene.anchor.start, scene.anchor.end) == (2, 3)
    assert not ms.scene_drift(scene, moved).dirty


def test_deleted_boundary_message_loses_the_anchor(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 0, 1)

    _write_log(path, [
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    drift = ms.scene_drift(scene, loader.load(path))
    assert drift.kind == ms.ANCHOR_LOST
    assert "m-aaa" in drift.detail


def test_rebase_clears_anchor_lost_without_touching_prose(tmp_path: Path) -> None:
    """Gone boundary uids used to survive rebase via refreshed(), so the
    warning never cleared and authors thought the scene had been wiped."""
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 0, 1)
    before = scene.text()

    _write_log(path, [
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    moved = loader.load(path)
    assert ms.scene_drift(scene, moved).kind == ms.ANCHOR_LOST

    ms.rebase(scene, moved)
    assert not ms.scene_drift(scene, moved).dirty
    assert scene.text() == before
    assert moved.ordinal_of(scene.anchor.from_uid) is not None
    assert moved.ordinal_of(scene.anchor.to_uid) is not None


def test_rebase_accepts_the_source_without_touching_prose(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 0, 1)
    before = scene.text()

    _write_log(path, [
        ("m-aaa", "Wren", "Wren set the figurine down, hands unsteady."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    moved = loader.load(path)
    assert ms.scene_drift(scene, moved).dirty

    ms.rebase(scene, moved)
    assert not ms.scene_drift(scene, moved).dirty
    assert scene.text() == before


def test_pin_marks_divergence_without_clearing_sig(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    scene = _scene_over(log, 0, 1)
    before_sig = scene.source_sig

    _write_log(path, [
        ("m-aaa", "Wren", "Wren set the figurine down, hands unsteady."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    moved = loader.load(path)
    assert ms.scene_drift(scene, moved).dirty

    ms.pin(scene)
    drift = ms.scene_drift(scene, moved)
    assert drift.kind == ms.PINNED
    assert not drift.dirty
    assert scene.source_sig == before_sig
    assert scene.pinned is True


def test_document_drift_reports_every_scene(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    log = _sample(path)
    doc = ms.Document(layer="manuscript")
    doc.upsert(_scene_over(log, 0, 1))
    doc.upsert(_scene_over(log, 2, 3))

    _write_log(path, [
        ("m-aaa", "Wren", "Wren set the figurine down."),
        ("m-bbb", "Ilse", "Ilse watched the small pale king settle."),
        ("m-ccc", "Wren", "The door closed behind her, and the latch caught."),
        ("m-ddd", "Ilse", "Snow gathered on the sill."),
    ])
    reports = ms.drift(doc, loader.load(path))
    assert [r.dirty for r in reports] == [False, True]


# --------------------------------------------------------------------------- #
# Layer rules
# --------------------------------------------------------------------------- #


def test_novelize_targets_the_manuscript_only() -> None:
    layers.check("novelize", layers.MANUSCRIPT)
    for layer in (layers.LOG,):
        try:
            layers.check("novelize", layer)
        except layers.LayerViolation:
            continue
        raise AssertionError(f"novelize must not target {layer}")


def test_log_operators_stay_on_the_log() -> None:
    for op in ("retune", "inject", "sweep"):
        layers.check(op, layers.LOG)
        try:
            layers.check(op, layers.MANUSCRIPT)
        except layers.LayerViolation:
            continue
        raise AssertionError(f"{op} must not target the manuscript")


def test_only_the_log_may_sync_to_sillytavern() -> None:
    layers.check_syncable(layers.LOG)
    for layer in (layers.MANUSCRIPT,):
        try:
            layers.check_syncable(layer)
        except layers.LayerViolation as exc:
            assert "never syncs" in str(exc)
            continue
        raise AssertionError(f"{layer} must never sync to ST")


def test_unknown_op_or_layer_is_refused() -> None:
    for op, layer in (("frobnicate", layers.LOG), ("edit", "archive")):
        try:
            layers.check(op, layer)
        except layers.LayerViolation:
            continue
        raise AssertionError(f"{op}/{layer} must be refused")


def test_parent_of_walks_down_the_layers() -> None:
    assert layers.parent_of(layers.MANUSCRIPT) == layers.LOG
    assert layers.parent_of(layers.LOG) is None


def test_ops_for_a_layer_is_what_the_gui_may_offer() -> None:
    log_ops = layers.ops_for(layers.LOG)
    assert "inject" in log_ops
    assert "novelize" not in log_ops


def test_reconcile_blocks_keeps_ids_of_untouched_paragraphs():
    old = [
        ms.Block(id="b-1", text="One.", src=["m-a"]),
        ms.Block(id="b-2", text="Two.", kind="heading", note="n"),
        ms.Block(id="b-3", text="Three.", src=["m-c"]),
    ]
    out = ms.reconcile_blocks(old, "One.\n\nTwo, revised.\n\nThree.\n\nFour.", src=["m-z"])
    assert [b.id for b in out[:3]] == ["b-1", "b-2", "b-3"]
    assert not out[0].edited and not out[2].edited
    assert out[1].text == "Two, revised." and out[1].edited and out[1].kind == "heading"
    assert out[3].text == "Four." and out[3].edited and out[3].src == ["m-z"]
    assert out[3].id not in {"b-1", "b-2", "b-3"}


def test_block_edits_reports_changed_added_and_removed_paragraphs():
    from story_editor import history

    edits = history.block_edits(
        [("b-1", "One."), ("b-2", "Two."), ("b-3", "Three.")],
        [("b-1", "One."), ("b-2", "Two, revised."), ("b-9", "Nine.")],
    )
    assert [(e.kind, e.block_id, e.before, e.after) for e in edits] == [
        ("replace", "b-2", "Two.", "Two, revised."),
        ("inject", "b-9", "", "Nine."),
        ("remove", "b-3", "Three.", ""),
    ]
