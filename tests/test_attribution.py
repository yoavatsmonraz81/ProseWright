#!/usr/bin/env python3
"""Unit tests for voice attribution sidecar (no LLM)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_editor import attribution as attr


def test_auto_label_player_pov():
    turn = {"msg_id": 1, "name": "Wren", "is_user": True, "mes": "I looked at him."}
    lab = attr.auto_label_turn(turn)
    assert lab.voice == "wren"
    assert lab.mode == "pov"
    assert lab.source == "default"


def test_auto_label_narrator_scene_block():
    text = (
        'Varga said "Stay back." Wren whispered "Not here." '
        "The harbourmaster watched them both."
    )
    turn = {"msg_id": 2, "name": "Narrator", "is_user": False, "mes": text}
    lab = attr.auto_label_turn(turn)
    assert lab.voice == "ensemble"
    assert lab.mode == "scene_narrator"
    assert lab.source == "heuristic"
    assert 0.55 <= lab.confidence <= 0.95


def test_auto_label_player_card_crowded_scene():
    text = (
        "Varga stood by the door. Ilse watched from the corner. "
        "Varga said something under his breath."
    )
    turn = {"msg_id": 3, "name": "Wren", "is_user": True, "mes": text}
    lab = attr.auto_label_turn(turn)
    assert lab.voice == "wren"
    assert lab.mode == "mixed"
    assert lab.source == "heuristic"


def test_player_writing_about_herself_is_not_a_crowd():
    text = "Wren shifts the satchel. Wren squints at the harbour mouth."
    lab = attr.auto_label_turn({"msg_id": 4, "name": "Wren", "is_user": True, "mes": text})
    assert lab.voice == "wren"
    assert lab.mode == "pov"


def test_reviewed_not_clobbered():
    store = attr.empty_store()
    mid = 10
    manual = attr.VoiceLabel(
        card="Wren", voice="varga", mode="wrong_card",
        source="manual", reviewed=True,
    )
    attr.set_label(store, mid, manual)
    auto = attr.auto_label_turn({"msg_id": mid, "name": "Wren", "is_user": True, "mes": "hi"})
    ok = attr.set_label(store, mid, auto)
    assert ok is False
    kept = attr.get_label(store, mid)
    assert kept is not None
    assert kept.voice == "varga"
    assert kept.reviewed is True


def test_filter_turns_for_bible():
    store = attr.empty_store()
    turns = [
        {"msg_id": 1, "name": "Ilse", "mes": "Ilse only."},
        {"msg_id": 2, "name": "Ilse", "mes": 'Varga said "Hi." Wren said "No."'},
    ]
    attr.set_label(store, 1, attr.VoiceLabel(card="Ilse", voice="ilse", mode="pov"))
    attr.set_label(
        store, 2,
        attr.VoiceLabel(card="Ilse", voice="ilse", mode="scene_narrator", source="heuristic"),
    )
    kept, stats = attr.filter_turns_for_bible(turns, "Ilse", store)
    assert len(kept) == 1
    assert kept[0]["msg_id"] == 1
    assert stats["skipped_attribution_mode"] == 1


def test_save_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "voice_attribution.json"
        store = attr.empty_store(log_name="test.jsonl")
        attr.set_label(
            store, 5,
            attr.VoiceLabel(
                card="Wren", voice="varga", mode="wrong_card",
                focal="wren", addressees=["ilse"],
                segments=[{
                    "voice": "varga", "kind": "dialogue",
                    "addressees": ["ilse"], "hint": "warning",
                }],
                source="manual", reviewed=True,
            ),
        )
        attr.save_store(store, path)
        loaded = attr.load_store(path)
        lab = attr.get_label(loaded, 5)
        assert lab is not None
        assert lab.voice == "varga"
        assert lab.mode == "wrong_card"
        assert lab.focal == "wren"
        assert lab.addressees == ["ilse"]
        assert lab.segments[0]["addressees"] == ["ilse"]


def test_stats_do_not_count_orphan_labels_as_live():
    turns = [{"msg_id": 0, "name": "Wren", "mes": "a"}]
    store = attr.empty_store()
    attr.set_label(store, 0, attr.VoiceLabel(card="Wren", voice="wren"))
    store["labels"]["m-deleted"] = attr.VoiceLabel(
        card="Ilse", voice="ilse"
    ).to_dict()
    stats = attr.format_stats(store, turns)
    assert "labeled: 1  unlabeled: 0" in stats
    assert "orphaned labels (not in current log): 1" in stats


def test_legacy_label_loads_without_semantic_fields():
    lab = attr.VoiceLabel.from_dict({
        "card": "Wren", "voice": "wren", "mode": "pov",
    })
    assert lab.focal == ""
    assert lab.addressees == []


def test_auto_accept_eligible():
    lab = attr.VoiceLabel(
        card="Wren", voice="ilse", mode="wrong_card",
        source="llm_proposed", confidence=0.97,
    )
    assert attr.auto_accept_eligible(lab, min_confidence=0.95)
    low = attr.VoiceLabel(
        card="Wren", voice="ilse", mode="wrong_card",
        source="llm_proposed", confidence=0.80,
    )
    assert not attr.auto_accept_eligible(low, min_confidence=0.95)
    mixed = attr.VoiceLabel(
        card="Wren", voice="wren", mode="mixed",
        source="llm_proposed", confidence=0.99,
    )
    assert not attr.auto_accept_eligible(mixed, min_confidence=0.95)


def test_reconcile_auto_accept_no_llm():
    store = attr.empty_store()
    turns = [
        {"msg_id": 1, "name": "Wren", "is_user": True, "mes": "Ilse walked away."},
        {"msg_id": 2, "name": "Wren", "is_user": True, "mes": "I smiled."},
    ]
    attr.set_label(
        store, 1,
        attr.VoiceLabel(
            card="Wren", voice="ilse", mode="wrong_card",
            source="llm_proposed", confidence=0.98,
        ),
    )
    attr.set_label(
        store, 2,
        attr.VoiceLabel(card="Wren", voice="wren", mode="pov", source="default"),
    )
    stats, human_ids = attr.reconcile_turns(
        turns, store, skip_propose=True, min_confidence=0.95,
    )
    assert stats["auto_accepted"] == 1
    accepted = attr.get_label(store, 1)
    assert accepted is not None
    assert accepted.reviewed is True
    assert human_ids == []


def test_auto_accept_ensemble_scene_narrator():
    lab = attr.VoiceLabel(
        card="Ilse", voice="ensemble", mode="scene_narrator",
        source="llm_proposed", confidence=0.97,
    )
    assert attr.auto_accept_eligible(lab, min_confidence=0.95)


def test_needs_human_review_ensemble_scene_narrator():
    lab = attr.VoiceLabel(
        card="Ilse", voice="ensemble", mode="scene_narrator",
        source="heuristic", confidence=0.92,
    )
    assert not attr.needs_human_review(lab, min_confidence=0.95)
    legacy = attr.VoiceLabel(
        card="Ilse", voice="ilse", mode="scene_narrator",
        source="heuristic", confidence=0.92,
    )
    assert attr.needs_human_review(legacy, min_confidence=0.95)


def test_build_review_payload_filters():
    store = attr.empty_store()
    turns = [
        {"msg_id": 1, "name": "Wren", "mes": "a"},
        {"msg_id": 2, "name": "Ilse", "mes": "b"},
        {"msg_id": 3, "name": "Wren", "mes": "c"},
    ]
    attr.set_label(
        store, 1,
        attr.VoiceLabel(
            card="Wren", voice="wren", mode="mixed",
            source="heuristic", confidence=0.7,
        ),
    )
    attr.set_label(
        store, 2,
        attr.VoiceLabel(
            card="Ilse", voice="ilse", mode="scene_narrator",
            source="heuristic", confidence=0.92,
        ),
    )
    attr.set_label(
        store, 3,
        attr.VoiceLabel(
            card="Wren", voice="varga", mode="wrong_card",
            source="heuristic", confidence=0.6,
        ),
    )
    all_payload = attr.build_review_payload(turns, store)
    assert all_payload["total_needs_human"] == 3
    focus = attr.build_review_payload(
        turns, store,
        review_filter=attr.review_filter_from_mapping({"preset": "focus"}),
    )
    assert focus["count"] == 2
    assert focus["total_needs_human"] == 3
    low_conf = attr.build_review_payload(
        turns, store,
        review_filter=attr.review_filter_from_mapping({
            "label_confidence_max": 0.65,
        }),
    )
    assert low_conf["count"] == 1
    ids = {it["msg_id"] for it in low_conf["items"]}
    assert ids == {3}


def test_bulk_set_labels():
    store = attr.empty_store()
    turns = [
        {"msg_id": 1, "name": "Wren", "mes": "a"},
        {"msg_id": 2, "name": "Wren", "mes": "b"},
    ]
    attr.set_label(store, 1, attr.VoiceLabel(card="Wren", voice="wren", mode="pov", reviewed=False))
    attr.set_label(store, 2, attr.VoiceLabel(card="Wren", voice="wren", mode="mixed", reviewed=False))
    stats = attr.bulk_set_labels(turns, store, [1, 2], voice="varga", mode="wrong_card")
    assert stats["applied"] == 2
    assert attr.get_label(store, 1).voice == "varga"
    assert attr.get_label(store, 2).mode == "wrong_card"
    assert attr.get_label(store, 1).reviewed is True


def main() -> int:
    tests = [
        test_auto_label_player_pov,
        test_player_writing_about_herself_is_not_a_crowd,
        test_auto_label_narrator_scene_block,
        test_auto_label_player_card_crowded_scene,
        test_reviewed_not_clobbered,
        test_filter_turns_for_bible,
        test_save_load_roundtrip,
        test_legacy_label_loads_without_semantic_fields,
        test_auto_accept_eligible,
        test_auto_accept_ensemble_scene_narrator,
        test_needs_human_review_ensemble_scene_narrator,
        test_build_review_payload_filters,
        test_reconcile_auto_accept_no_llm,
        test_bulk_set_labels,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:
            print(f"FAIL  {fn.__name__} — {exc}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
