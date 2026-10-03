from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_editor import config, loader, manuscript as ms, prose_review as review


def _fixture(tmp_path: Path, monkeypatch) -> tuple[loader.Log, ms.Document, Path, Path]:
    log_path = tmp_path / "story.jsonl"
    log_path.write_text(
        json.dumps({"chat_metadata": {}}) + "\n" +
        json.dumps({
            "name": "Ilse",
            "mes": "Ilse crossed the room.",
            "extra": {"se_uid": "m-one"},
        }) + "\n",
        encoding="utf-8",
    )
    log = loader.load(log_path)
    scene = ms.new_scene(log, 0, 0)
    scene.blocks = ms.blocks_from_prose(
        "She crossed the room, she reached the window.\n\n"
        "She looked out at the snow, registering its silence.",
        src=["m-one"],
    )
    doc = ms.Document(layer="manuscript", log=str(log_path), scenes=[scene])
    manuscript_path = tmp_path / "manuscript.json"
    queue_path = tmp_path / "prose_review_queue.json"
    monkeypatch.setattr(config, "MANUSCRIPT", manuscript_path)
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(config, "BAD_REPLIES_DIR", tmp_path / "bad_replies")
    ms.save(doc, manuscript_path)
    return log, doc, manuscript_path, queue_path


def test_summary_counts_every_status_and_pending_verify(tmp_path: Path) -> None:
    queue = tmp_path / "prose_review_queue.json"
    proposals = [review.Proposal(
        id=f"pr-{index}", kind="syntax", scene_id=scene,
        changes=[review.Change(f"b-{index}", "before", "after")],
        reason=reason, status=status,
    ) for index, (scene, status, reason) in enumerate([
        ("scene-a", "pending", "Claude pass · VERIFY timeline"),
        ("scene-a", "committed", "Earlier edit"),
        ("scene-b", "stale", "VERIFY old text"),
        ("scene-b", "rejected", "Earlier edit"),
    ])]
    review.save(proposals, queue)

    result = review.summary(queue)
    assert result["total"] == 4
    assert result["totals"] == {
        "pending": 1, "committed": 1, "rejected": 1, "stale": 1, "verify": 1,
    }
    assert result["scenes"]["scene-a"]["verify"] == 1
    assert result["scenes"]["scene-b"]["verify"] == 0


def test_commit_is_manuscript_only_and_refreshes_autosave(tmp_path: Path, monkeypatch) -> None:
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    proposal = review.create(
        kind="syntax",
        scene_id=doc.scenes[0].id,
        changes=[review.Change(block.id, block.text, "She crossed the room and reached the window.")],
        reason="Repair a comma splice.",
    )
    review.enqueue(proposal, queue_path)
    source_before = log.path.read_bytes()

    accepted, scene, backup, autosave = review.commit(
        proposal.id,
        log=log,
        queue_path=queue_path,
        manuscript_path=manuscript_path,
    )

    assert accepted.status == "committed"
    assert scene.blocks[0].text == "She crossed the room and reached the window."
    assert scene.blocks[0].edited is True
    assert backup.exists()
    assert autosave.exists()
    assert "reached the window" in autosave.read_text(encoding="utf-8")
    assert log.path.read_bytes() == source_before


def test_commit_accepts_manual_replacement_and_keeps_original(tmp_path: Path, monkeypatch) -> None:
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    proposal = review.create(
        kind="syntax", scene_id=doc.scenes[0].id,
        changes=[review.Change(block.id, block.text, "An automated suggestion.")],
        reason="Repair syntax.",
    )
    review.enqueue(proposal, queue_path)
    accepted, scene, backup, _autosave = review.commit(
        proposal.id, log=log, queue_path=queue_path, manuscript_path=manuscript_path,
        replacements={block.id: "She paused beside the *window*."},
    )
    assert scene.blocks[0].text == "She paused beside the *window*."
    assert accepted.changes[0].before == block.text
    assert review.load(queue_path)[0].changes[0].after == scene.blocks[0].text
    assert backup.exists()


def test_manual_replacement_cannot_target_other_blocks(tmp_path: Path, monkeypatch) -> None:
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    proposal = review.create(
        kind="syntax", scene_id=doc.scenes[0].id,
        changes=[review.Change(block.id, block.text, "A suggestion.")], reason="Syntax.",
    )
    review.enqueue(proposal, queue_path)
    original = manuscript_path.read_bytes()
    with pytest.raises(ValueError, match="exactly"):
        review.commit(
            proposal.id, log=log, queue_path=queue_path, manuscript_path=manuscript_path,
            replacements={"unrelated-block": "Do not write this."},
        )
    assert manuscript_path.read_bytes() == original
    assert review.load(queue_path)[0].status == "pending"


def test_author_edit_marks_proposal_stale_instead_of_overwriting(tmp_path: Path, monkeypatch) -> None:
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    proposal = review.create(
        kind="compression",
        scene_id=doc.scenes[0].id,
        changes=[review.Change(block.id, block.text, "She reached the window.")],
        reason="Remove redundant movement.",
    )
    review.enqueue(proposal, queue_path)
    live = ms.load(path=manuscript_path, log=log)
    live.scenes[0].blocks[0].text = "The author wrote something better."
    ms.save(live, manuscript_path)

    with pytest.raises(review.StaleProposal):
        review.commit(
            proposal.id,
            log=log,
            queue_path=queue_path,
            manuscript_path=manuscript_path,
        )

    assert ms.load(path=manuscript_path, log=log).scenes[0].blocks[0].text == "The author wrote something better."
    assert review.load(queue_path)[0].status == "stale"


def test_reject_records_decision_without_changing_manuscript(tmp_path: Path, monkeypatch) -> None:
    _log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[1]
    proposal = review.create(
        kind="compression",
        scene_id=doc.scenes[0].id,
        changes=[review.Change(block.id, block.text, "She looked out at the silent snow.")],
        reason="Compress interpretation.",
    )
    review.enqueue(proposal, queue_path)
    before = manuscript_path.read_bytes()

    rejected = review.reject(proposal.id, path=queue_path)

    assert rejected.status == "rejected"
    assert rejected.decided
    assert manuscript_path.read_bytes() == before
    assert review.list_view(scene_id=doc.scenes[0].id, path=queue_path) == []


def test_grouped_commit_rewrites_and_removes_blocks_together(tmp_path: Path, monkeypatch) -> None:
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    first, second = doc.scenes[0].blocks
    proposal = review.create(
        kind="compression",
        scene_id=doc.scenes[0].id,
        changes=[
            review.Change(first.id, first.text, "She crossed to the window and looked out."),
            review.Change(second.id, second.text, ""),
        ],
        reason="Collapse duplicated movement and observation.",
    )
    review.enqueue(proposal, queue_path)

    review.commit(
        proposal.id,
        log=log,
        queue_path=queue_path,
        manuscript_path=manuscript_path,
    )

    live = ms.load(path=manuscript_path, log=log).scenes[0]
    assert [block.text for block in live.blocks] == ["She crossed to the window and looked out."]


def _rhythm_reply(monkeypatch, edits, *, truncated=False):
    monkeypatch.setattr(review.llm, "complete", lambda *args, **kwargs:
        review.llm.Completion(json.dumps({"edits": edits}), "length" if truncated else "stop"))


def test_rhythm_proposes_only_and_deduplicates(tmp_path: Path, monkeypatch) -> None:
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    _rhythm_reply(monkeypatch, [{"block_id": block.id, "find": block.text,
        "replace": "She crossed the room and reached the window.", "reason": "Reconnect action."}])
    before, source = manuscript_path.read_bytes(), log.path.read_bytes()
    result = review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result["count"] == 1
    assert result["proposals"][0]["kind"] == "rhythm"
    assert manuscript_path.read_bytes() == before and log.path.read_bytes() == source
    assert review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)["count"] == 0
    review.reject(result["proposals"][0]["id"], path=queue_path)
    assert review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)["count"] == 0


def test_rhythm_prompt_covers_fragments_and_em_dash_overuse(tmp_path: Path, monkeypatch) -> None:
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    seen = {}

    def complete(messages, **kwargs):
        seen["system"] = messages[0]["content"]
        return review.llm.Completion('{"edits": []}', "stop")

    monkeypatch.setattr(review.llm, "complete", complete)
    review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert "staccato" in seen["system"]
    assert "em-dash chaining" in seen["system"]
    assert "not a blanket punctuation-normalization pass" in seen["system"]


@pytest.mark.parametrize("find,replacement", [
    ('"Wait. Stay."', '"Wait and stay."'),
    ('Wait.', 'Hurry.'),
    ('*cold*', '*warm*'),
    ('She paused.', 'She paused.\n\nShe left.'),
    ('She paused.', 'She said "Go."'),
])
def test_rhythm_protects_dialogue_emphasis_and_paragraphs(tmp_path, monkeypatch, find, replacement):
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    block.text = '"Wait. Stay." She paused. The *cold* remained.'
    ms.save(doc, manuscript_path)
    _rhythm_reply(monkeypatch, [{"block_id": block.id, "find": find, "replace": replacement}])
    result = review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result["count"] == 0 and result["warnings"]
    assert not queue_path.exists()


def test_rhythm_rejects_truncated_reply_without_queueing(tmp_path, monkeypatch):
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    _rhythm_reply(monkeypatch, [], truncated=True)
    with pytest.raises(review.llm.ModelError, match="output-token limit"):
        review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert not queue_path.exists()


def test_rhythm_cannot_target_another_scene(tmp_path, monkeypatch):
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    _rhythm_reply(monkeypatch, [{"block_id": "elsewhere", "find": "She paused.", "replace": "She waited."}])
    result = review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result["count"] == 0 and result["warnings"]


def test_rhythm_second_batch_failure_does_not_queue_first(tmp_path, monkeypatch):
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    first, second = doc.scenes[0].blocks
    second.text = 'Long context. ' * 600
    ms.save(doc, manuscript_path)
    replies = iter([
        review.llm.Completion(json.dumps({"edits": [{"block_id": first.id,
            "find": first.text, "replace": "She crossed the room and reached the window."}]})),
        review.llm.Completion('not JSON'),
        review.llm.Completion('not JSON'),
    ])
    monkeypatch.setattr(review.llm, 'complete', lambda *args, **kwargs: next(replies))
    with pytest.raises(review.llm.ModelError):
        review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert not queue_path.exists()


@pytest.mark.parametrize('first', [review.llm.Completion('not JSON'),
    review.llm.Completion('{"changes": []}'), review.llm.Completion('{"edits": []}', 'length')])
def test_rhythm_recovers_after_one_retry(tmp_path, monkeypatch, first):
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    calls = []
    replies = iter([first, review.llm.Completion('{"edits": []}', 'stop')])
    def complete(messages, **kwargs):
        calls.append((list(messages), kwargs))
        return next(replies)
    monkeypatch.setattr(review.llm, 'complete', complete)
    result = review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result['count'] == 0 and len(calls) == 2
    assert 'previous response failed' in calls[1][0][-1]['content']
    assert calls[1][1]['max_tokens'] == (12000 if first.truncated else 6000)
    assert list(config.BAD_REPLIES_DIR.glob('rhythm-*.txt'))


def test_rhythm_empty_reasoning_budget_gets_only_one_explicit_retry(tmp_path, monkeypatch):
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    calls = []
    def complete(messages, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise review.llm.OutputLimitError('reasoning used the budget')
        return review.llm.Completion('{"edits": []}', 'stop')
    monkeypatch.setattr(review.llm, 'complete', complete)
    assert review.propose_rhythm(doc.scenes[0].id, log=log, queue_path=queue_path)['count'] == 0
    assert [c['max_tokens'] for c in calls] == [6000, 12000]
    assert all(c['_allow_length_retry'] is False for c in calls)


@pytest.mark.parametrize('mode,status', [('ok', 200), ('missing', 400), ('unknown', 404), ('model', 502)])
def test_rhythm_http_route(tmp_path, monkeypatch, mode, status):
    from story_editor import server
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(config, 'PROSE_REVIEW_QUEUE', queue_path)
    body = {} if mode == 'missing' else {'scene_id': 'absent' if mode == 'unknown' else doc.scenes[0].id}
    monkeypatch.setattr(server, '_read_json', lambda handler: body)
    monkeypatch.setattr(server, '_body_log_path', lambda body: log.path)
    responses = []
    monkeypatch.setattr(server, '_json_response', lambda handler, code, payload: responses.append((code, payload)))
    _rhythm_reply(monkeypatch, [])
    if mode == 'model':
        def failure(*args, **kwargs):
            raise review.llm.ModelError('offline')
        monkeypatch.setattr(review.llm, 'complete', failure)
    handler = object.__new__(server.StoryEditorHandler)
    handler.path = '/prose-review/rhythm'
    handler.do_POST()
    assert responses[0][0] == status
    assert not queue_path.exists()


def test_rhythm_http_stream_returns_progress_and_result(tmp_path, monkeypatch):
    from story_editor import server
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(config, 'PROSE_REVIEW_QUEUE', queue_path)
    monkeypatch.setattr(server, '_read_json', lambda handler: {'scene_id': doc.scenes[0].id, 'stream': True})
    monkeypatch.setattr(server, '_body_log_path', lambda body: log.path)
    _rhythm_reply(monkeypatch, [])
    events = []
    def stream(handler, work, *, done_builder):
        result = work(events.append)
        events.append({'kind': 'done', **done_builder(result)})
    monkeypatch.setattr(server, '_stream_propose', stream)
    handler = object.__new__(server.StoryEditorHandler)
    handler.path = '/prose-review/rhythm'
    handler.do_POST()
    assert events[0]['kind'] == 'phase'
    assert events[-1]['rhythm']['count'] == 0


def test_compression_proposes_reduction_without_writing(tmp_path, monkeypatch):
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[1]
    monkeypatch.setattr(review, '_compression_object', lambda *args, **kwargs: {'edits': [{
        'block_id': block.id,
        'after': 'She looked out at the silent snow.',
        'reason': 'Remove repeated interpretation.',
        'risks': ['Check the held pause.'],
    }]})
    before, source = manuscript_path.read_bytes(), log.path.read_bytes()
    result = review.propose_compression(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result['count'] == 1
    assert result['proposals'][0]['kind'] == 'compression'
    assert result['proposals'][0]['word_delta'] < 0
    assert manuscript_path.read_bytes() == before and log.path.read_bytes() == source


@pytest.mark.parametrize('before,after', [
    ('She waited. "Stay here."', 'She waited. "Leave now."'),
    ('The *cold* silence lingered far too long.', 'The silence lingered.'),
    ('She waited quietly.', 'She waited very quietly beside the door.'),
    ('She waited quietly.', 'She waited quietly.'),
    ('-------------------', ''),
])
def test_compression_rejects_unsafe_or_nonreducing_changes(tmp_path, monkeypatch, before, after):
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    block.text = before
    ms.save(doc, manuscript_path)
    monkeypatch.setattr(review, '_compression_object', lambda *args, **kwargs: {
        'edits': [{'block_id': block.id, 'after': after}],
    })
    result = review.propose_compression(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result['count'] == 0
    assert not queue_path.exists()


def test_compression_allows_deleting_plain_redundant_paragraph(tmp_path, monkeypatch):
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[1]
    monkeypatch.setattr(review, '_compression_object', lambda *args, **kwargs: {
        'edits': [{'block_id': block.id, 'after': '', 'reason': 'Entirely repeats the prior beat.'}],
    })
    result = review.propose_compression(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert result['count'] == 1 and result['proposals'][0]['after'] == ''


def test_compression_is_atomic_across_batches(tmp_path, monkeypatch):
    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    doc.scenes[0].blocks[1].text = 'Long repeated context. ' * 700
    ms.save(doc, manuscript_path)
    calls = 0
    def response(batch, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {'edits': [{'block_id': batch[0].id, 'after': 'She reached the window.'}]}
        raise review.llm.ModelError('second batch failed')
    monkeypatch.setattr(review, '_compression_object', response)
    with pytest.raises(review.llm.ModelError):
        review.propose_compression(doc.scenes[0].id, log=log, queue_path=queue_path)
    assert not queue_path.exists()


def test_compression_http_stream_returns_progress_and_result(tmp_path, monkeypatch):
    from story_editor import server
    log, doc, _path, queue_path = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(config, 'PROSE_REVIEW_QUEUE', queue_path)
    monkeypatch.setattr(server, '_read_json', lambda handler: {'scene_id': doc.scenes[0].id, 'stream': True})
    monkeypatch.setattr(server, '_body_log_path', lambda body: log.path)
    monkeypatch.setattr(review, '_compression_object', lambda *args, **kwargs: {'edits': []})
    events = []
    def stream(handler, work, *, done_builder):
        result = work(events.append)
        events.append({'kind': 'done', **done_builder(result)})
    monkeypatch.setattr(server, '_stream_propose', stream)
    handler = object.__new__(server.StoryEditorHandler)
    handler.path = '/prose-review/compression'
    handler.do_POST()
    assert events[0]['kind'] == 'phase'
    assert events[-1]['compression']['count'] == 0


def test_commit_lands_in_history_as_committed(tmp_path: Path, monkeypatch) -> None:
    from story_editor import history

    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    block = doc.scenes[0].blocks[0]
    proposal = review.create(
        kind="syntax",
        scene_id=doc.scenes[0].id,
        changes=[review.Change(block.id, block.text, "She crossed the room and reached the window.")],
        reason="Repair a comma splice.",
    )
    review.enqueue(proposal, queue_path)
    review.commit(proposal.id, log=log, queue_path=queue_path, manuscript_path=manuscript_path)

    [entry] = history.list_entries(log_path=log.path)
    assert entry.layer == "manuscript" and entry.operator == history.COMMITTED
    assert entry.scene_id == doc.scenes[0].id
    assert (entry.changed_from, entry.changed_to) == (0, 0)
    assert [(e.block_id, e.before, e.after) for e in entry.edits] == [
        (block.id, block.text, "She crossed the room and reached the window."),
    ]
    # A manuscript entry is never what `edits undo` restores the log from.
    assert history.get_latest(log_path=log.path) is None


def test_manual_scene_save_keeps_block_ids_and_lands_in_history(tmp_path: Path, monkeypatch) -> None:
    from story_editor import history, server

    log, doc, manuscript_path, queue_path = _fixture(tmp_path, monkeypatch)
    scene = doc.scenes[0]
    first, second = scene.blocks
    body = {
        "layer": "manuscript",
        "text": f"{first.text}\n\nShe looked out at the snow.",
    }
    responses = []
    monkeypatch.setattr(server, '_read_json', lambda handler: body)
    monkeypatch.setattr(server, '_body_log_path', lambda body: log.path)
    monkeypatch.setattr(server, '_json_response', lambda handler, code, payload: responses.append((code, payload)))
    handler = object.__new__(server.StoryEditorHandler)
    handler.path = f"/scene/{scene.id}"

    handler.do_PUT()

    assert responses[0][0] == 200
    assert responses[0][1]["blocks_touched"] == 1
    saved = ms.load(path=manuscript_path, log=log).by_id(scene.id)
    assert [b.id for b in saved.blocks] == [first.id, second.id]
    assert saved.blocks[0].edited is False
    [entry] = history.list_entries(log_path=log.path)
    assert entry.operator == history.MANUALLY_EDITED and entry.layer == "manuscript"
    assert Path(entry.backup).exists()
    assert [(e.block_id, e.before, e.after) for e in entry.edits] == [
        (second.id, second.text, "She looked out at the snow."),
    ]
