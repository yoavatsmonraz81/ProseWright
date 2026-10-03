"""The spine audit against a model that fumbles its JSON.

The audit is one slow call — minutes on a local model — and it used to be thrown
away whole by a single dropped comma, which reached the browser as a decoder
offset. What matters here is that the shapes a local model actually emits are
read back correctly, that a genuinely broken reply costs a retry rather than the
run, and that giving up says so in a sentence and leaves the last good alignment
where it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, jsonish, llm, structure, views


GOOD_RESULT = {
    "beats": [
        {"authored": "1", "verdict": "absent", "evidence": [], "note": "pre-log"},
        {"authored": "2", "verdict": "absent", "evidence": [], "note": "not yet"},
        {
            "authored": "3",
            "verdict": "present",
            "evidence": [],
            "note": "the pond",
            "derived": [0],
        },
    ],
    "drift": [],
    "coverage": "early slice",
}
# Legacy derived→authored shape, still accepted when every label is named.
GOOD_RESULT_LEGACY = {
    "alignment": [{"derived": 0, "authored": "3", "note": "the pond"}],
    "absent": ["1", "2"],
    "drift": [],
    "coverage": "early slice",
}

AUTHORED_MD = """# Spine

## Chronological beats

1. **Inciting event:** the compass, the harbour office.
2. **The design:** unspeakable, and a detonator.
3. **Recovery → confession:** the harbour wall.
"""

# The shape the real audit returns, with the comma the model dropped.
MISSING_COMMA = """{
  "beats": [
    {
      "authored": "1",
      "verdict": "present",
      "evidence": [],
      "note": "the compass lands first"
    },
    {
      "authored": "2",
      "verdict": "present",
      "evidence": [],
      "note": "the design, and its detonator"
    }
    {
      "authored": "3",
      "verdict": "absent",
      "evidence": [],
      "note": "not yet"
    }
  ],
  "drift": [],
  "coverage": "prose"
}"""


# --------------------------------------------------------------------------- #
# Tolerant extraction
# --------------------------------------------------------------------------- #


def test_clean_json_parses() -> None:
    assert jsonish.extract_object(json.dumps(GOOD_RESULT)) == GOOD_RESULT


def test_fenced_json_parses() -> None:
    raw = "```json\n" + json.dumps(GOOD_RESULT) + "\n```"
    assert jsonish.extract_object(raw) == GOOD_RESULT


def test_prose_around_the_object_is_ignored() -> None:
    raw = (
        "Sure — here is the alignment you asked for.\n"
        + json.dumps(GOOD_RESULT)
        + "\nLet me know if you want it broken down further."
    )
    assert jsonish.extract_object(raw) == GOOD_RESULT


def test_trailing_commas_parse() -> None:
    raw = """{
      "beats": [
        {"authored": "1", "verdict": "absent", "evidence": [], "note": "pre-log"},
        {"authored": "2", "verdict": "absent", "evidence": [], "note": "not yet"},
        {"authored": "3", "verdict": "present", "evidence": [], "note": "the pond", "derived": [0],},
      ],
      "drift": [],
      "coverage": "early slice",
    }"""
    assert jsonish.extract_object(raw) == GOOD_RESULT


def test_missing_member_comma_is_repaired() -> None:
    obj = jsonish.extract_object(MISSING_COMMA)
    assert obj is not None
    assert [row["authored"] for row in obj["beats"]] == ["1", "2", "3"]
    assert obj["beats"][1]["note"] == "the design, and its detonator"


def test_missing_comma_between_array_members_is_repaired() -> None:
    raw = """{"absent": [
      "4"
      "5"
    ], "drift": [], "coverage": "x", "alignment": []}"""
    assert jsonish.extract_object(raw)["absent"] == ["4", "5"]


def test_single_quoted_object_parses_when_unambiguous() -> None:
    assert jsonish.extract_object("{'coverage': 'prose'}") == {"coverage": "prose"}


def test_repair_leaves_valid_documents_alone() -> None:
    """Nothing in the repair may change what a parseable document says."""
    doc = json.dumps(
        {
            "alignment": [{"derived": 0, "authored": "1", "note": "a, b {c} [d]"}],
            "absent": [],
            "drift": ["quoted \"drift\" note", "trailing comma, inside prose"],
            "coverage": "true false null 12",
            "flags": [True, False, None, 12],
        },
        indent=2,
    )
    assert json.loads(jsonish._repair(doc)) == json.loads(doc)


def test_truncated_reply_is_not_silently_accepted() -> None:
    raw = '{"alignment": [{"derived": 0, "authored": "1"'
    assert jsonish.extract_object(raw) is None


def test_reply_without_an_object_is_not_accepted() -> None:
    assert jsonish.extract_object("I could not complete this audit.") is None
    assert jsonish.extract_object("") is None


# --------------------------------------------------------------------------- #
# Bounded retry
# --------------------------------------------------------------------------- #


def _fake_chat(replies: list[str], calls: list[int]):
    def chat(messages, **kwargs):
        calls.append(len(calls))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    return chat


def test_request_object_retries_then_succeeds(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad")
    calls: list[int] = []
    replies = ["not json at all", json.dumps(GOOD_RESULT)]

    def call() -> str:
        calls.append(0)
        return replies[len(calls) - 1]

    assert jsonish.request_object(call, what="spine audit", slug="s") == GOOD_RESULT
    assert len(calls) == 2
    assert not (tmp_path / "bad").exists()


def test_request_object_gives_up_with_a_friendly_error(monkeypatch, tmp_path) -> None:
    bad_dir = tmp_path / "bad"
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", bad_dir)
    calls: list[int] = []

    def call() -> str:
        calls.append(0)
        return "the model rambled and never closed a brace {"

    with pytest.raises(jsonish.UnparsableReply) as exc:
        jsonish.request_object(call, what="spine audit", slug="spine-audit", attempts=3)

    assert len(calls) == 3
    message = str(exc.value)
    assert "unparsable JSON for the spine audit after 3 attempts" in message
    # Not a decoder offset: the old error was "Expecting ',' delimiter: line 42".
    assert "Expecting" not in message and "char " not in message
    dumped = list(bad_dir.glob("spine-audit-*.txt"))
    assert len(dumped) == 1
    assert dumped[0].read_text(encoding="utf-8").startswith("the model rambled")
    assert str(dumped[0]) in message
    # The routes and commands that already answer for a broken model answer here.
    assert isinstance(exc.value, llm.ModelError)


# --------------------------------------------------------------------------- #
# The audit itself
# --------------------------------------------------------------------------- #


def _derived() -> structure.SpineProposal:
    beat = structure.Beat(
        beat_id=0,
        title="Recovery",
        justification="the pond",
        first_scene_id=0,
        last_scene_id=0,
        start_msg_id=0,
        end_msg_id=3,
    )
    return structure.SpineProposal(log="log.jsonl", beats=[beat], n_scenes=1, n_episodes=1)


def _spine_md(tmp_path: Path) -> Path:
    md = tmp_path / "spine.md"
    md.write_text(AUTHORED_MD, encoding="utf-8")
    return md


def _empty_evidence(labels=("1", "2", "3")) -> dict[str, list[structure.AuditExcerpt]]:
    """Skip the live index in unit tests; pass an explicit empty pack."""
    return {label: [] for label in labels}


def test_audit_keeps_its_payload_shape(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad")
    monkeypatch.setattr(llm, "chat", _fake_chat([json.dumps(GOOD_RESULT)], []))

    audit = structure.audit_spine(
        _derived(), md_path=_spine_md(tmp_path), evidence=_empty_evidence(),
    )

    assert set(audit) >= {"authored", "result", "evidence"}
    assert [b.label for b in audit["authored"]] == ["1", "2", "3"]
    by = {r["authored"]: r for r in audit["result"]["beats"]}
    assert by["3"]["verdict"] == "present"
    assert by["1"]["verdict"] == "absent"
    assert by["2"]["verdict"] == "absent"
    assert any(a["authored"] == "3" and a["derived"] == 0 for a in audit["result"]["alignment"])


def test_audit_survives_one_malformed_reply(monkeypatch, tmp_path) -> None:
    """The reproduced failure: attempt one is unrepairable, attempt two is fine."""
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad")
    calls: list[int] = []
    monkeypatch.setattr(
        llm, "chat", _fake_chat(["{ oh dear", json.dumps(GOOD_RESULT)], calls)
    )

    audit = structure.audit_spine(
        _derived(), md_path=_spine_md(tmp_path), evidence=_empty_evidence(),
    )

    assert len(calls) == 2
    assert any(a["authored"] == "3" for a in audit["result"]["alignment"])


def test_audit_repairs_the_missing_comma_without_a_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad")
    calls: list[int] = []
    monkeypatch.setattr(llm, "chat", _fake_chat([MISSING_COMMA], calls))

    audit = structure.audit_spine(
        _derived(), md_path=_spine_md(tmp_path), evidence=_empty_evidence(),
    )

    assert len(calls) == 1
    by = {r["authored"]: r["verdict"] for r in audit["result"]["beats"]}
    assert by == {"1": "present", "2": "present", "3": "absent"}


def test_failed_audit_leaves_the_cached_alignment_untouched(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "spine_alignment.json"
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", cache)
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad")
    saved = views.save_alignment(GOOD_RESULT, authored_count=21)
    before = cache.read_bytes()

    monkeypatch.setattr(llm, "chat", _fake_chat(["}{ not json"], []))

    with pytest.raises(jsonish.UnparsableReply):
        structure.audit_spine(
            _derived(), md_path=_spine_md(tmp_path), evidence=_empty_evidence(),
        )

    assert cache.read_bytes() == before
    still = views.load_alignment()
    assert still["result"] == GOOD_RESULT
    assert still["checked"] == saved["checked"]
    assert still["authored_count"] == 21


def test_normalize_drops_uncited_present_claims() -> None:
    """A 'present' verdict with no msg_id from the evidence pack is absent."""
    authored = [
        structure.AuthoredBeat(number=8, title="Kettering accident", text="Tobias dies"),
        structure.AuthoredBeat(number=14, title="War", text="not yet"),
    ]
    derived = structure.SpineProposal(
        log="x",
        beats=[
            structure.Beat(
                beat_id=8,
                title="Kettering",
                justification="clash",
                first_scene_id=0,
                last_scene_id=0,
                start_msg_id=600,
                end_msg_id=630,
            )
        ],
        n_scenes=1,
        n_episodes=1,
    )
    evidence = {
        "8": [
            structure.AuditExcerpt(620, "Wren", "the boy is gone"),
            structure.AuditExcerpt(621, "Ilse", "silence where he was"),
        ],
        # Non-empty pack with no cited overlap → present is forced absent.
        "14": [structure.AuditExcerpt(10, "Wren", "unrelated war talk")],
    }
    raw = {
        "beats": [
            {
                "authored": "8",
                "verdict": "present",
                "evidence": [620, 999],  # 999 is not in the pack — dropped
                "note": "Tobias is killed",
            },
            {
                "authored": "14",
                "verdict": "present",
                "evidence": [],  # uncitable against a non-empty pack → absent
                "note": "invented",
            },
        ],
        "drift": [
            {
                "authored": "8",
                "note": "replaced by masked attackers",
                "evidence": [620],
            }
        ],
        "coverage": "ok",
    }
    result = structure.normalize_audit_result(
        raw, authored=authored, derived=derived, evidence=evidence,
    )
    by = {r["authored"]: r for r in result["beats"]}
    assert by["8"]["verdict"] == "present"
    assert by["8"]["evidence"] == [620]
    assert by["8"]["derived"] == [8]
    assert by["14"]["verdict"] == "absent"
    assert any(a["authored"] == "8" and a["derived"] == 8 for a in result["alignment"])
    assert "14" in result["absent"]
    assert any("A8:" in d and "620" in d for d in result["drift"])


def test_coerce_wraps_a_bare_beat_object() -> None:
    raw = structure._coerce_audit_raw({
        "authored": "A8",
        "verdict": "present",
        "evidence": [620],
        "note": "Tobias",
    })
    assert raw["beats"][0]["authored"] == "A8"
    assert structure._audit_batch_usable(raw, {"8", "9"})


def test_audit_rejects_empty_batch_without_writing_cache(monkeypatch, tmp_path) -> None:
    """A parseable but verdict-less object must not become an all-absent cache."""
    cache = tmp_path / "spine_alignment.json"
    monkeypatch.setattr(config, "SPINE_ALIGNMENT", cache)
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad")
    views.save_alignment(GOOD_RESULT, authored_count=3)
    before = cache.read_bytes()
    monkeypatch.setattr(
        llm, "chat", _fake_chat([json.dumps({"coverage": "oops", "beats": []})], [])
    )

    with pytest.raises(jsonish.UnparsableReply):
        structure.audit_spine(
            _derived(), md_path=_spine_md(tmp_path), evidence=_empty_evidence(),
        )

    assert cache.read_bytes() == before


def test_msg_ids_cited_accepts_paren_tilde() -> None:
    beat = structure.AuthoredBeat(
        number=7,
        title="Hobb caught locally",
        text='Wren orders Ilse to Kettering (~507): "a nephew in Kettering."',
    )
    assert 507 in structure._msg_ids_cited_in_beat(beat)


def test_msg_ids_cited_accepts_paren_range() -> None:
    beat = structure.AuthoredBeat(
        number=13,
        letter="b",
        title="Reunion",
        text="Ilse returns (msgs ~697–702, in progress).",
    )
    ids = structure._msg_ids_cited_in_beat(beat)
    assert 697 in ids and 702 in ids


def test_chronology_guard_rejects_early_echo_for_late_beats() -> None:
    """A late beat citing only an early motif is not that beat."""
    authored = [
        structure.AuthoredBeat(number=13, letter="a", title="Harbour", text="montage"),
        structure.AuthoredBeat(number=15, title="The bell", text="the bell is rung"),
    ]
    derived = structure.SpineProposal(
        log="x",
        beats=[
            structure.Beat(
                beat_id=11,
                title="Harbour",
                justification="harbour",
                first_scene_id=0,
                last_scene_id=0,
                start_msg_id=680,
                end_msg_id=697,
            )
        ],
        n_scenes=1,
        n_episodes=1,
    )
    evidence = {
        "13a": [structure.AuditExcerpt(690, "Wren", "the casks on the quay")],
        "15": [
            structure.AuditExcerpt(176, "Ilse", "a bell somewhere across the water"),
            structure.AuditExcerpt(356, "Ilse", "the lighthouse bell rope"),
        ],
    }
    raw = {
        "beats": [
            {
                "authored": "13a",
                "verdict": "present",
                "evidence": [690],
                "note": "harbour montage",
            },
            {
                "authored": "15",
                "verdict": "partial",
                "evidence": [176, 356],
                "note": "bell echo",
            },
        ],
        "drift": [],
        "coverage": "x",
    }
    result = structure.normalize_audit_result(
        raw, authored=authored, derived=derived, evidence=evidence, log_tip=702,
    )
    by = {r["authored"]: r for r in result["beats"]}
    assert by["13a"]["verdict"] == "present"
    assert by["15"]["verdict"] == "absent"
    assert by["15"]["evidence"] == []
    assert "chronology guard" in by["15"]["note"]


def test_normalize_accepts_thematic_present_with_citations() -> None:
    authored = [
        structure.AuthoredBeat(number=2, title="The design", text="monster framing"),
    ]
    derived = structure.SpineProposal(
        log="x",
        beats=[
            structure.Beat(
                beat_id=0,
                title="Recovery",
                justification="pond",
                first_scene_id=0,
                last_scene_id=0,
                start_msg_id=90,
                end_msg_id=120,
            )
        ],
        n_scenes=1,
        n_episodes=1,
    )
    evidence = {
        "2": [structure.AuditExcerpt(115, "Ilse", "designed to be witnessed")],
    }
    raw = {
        "beats": [
            {
                "authored": "A2",
                "verdict": "present",
                "evidence": [115],
                "note": "the design thread",
            }
        ],
        "drift": [],
        "coverage": "design is on the page",
    }
    result = structure.normalize_audit_result(
        raw, authored=authored, derived=derived, evidence=evidence,
    )
    assert result["beats"][0]["verdict"] == "present"
    assert result["alignment"][0]["authored"] == "2"
    assert result["absent"] == []
