from pathlib import Path
from types import SimpleNamespace

from story_editor import server


def _handler(path: str = "/edits/undo"):
    handler = object.__new__(server.StoryEditorHandler)
    handler.path = path
    return handler


def test_history_bound_undo_restores_the_entry_backup(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "story.jsonl"
    log.write_text("{}\n", encoding="utf-8")
    backup = tmp_path / "before.jsonl"
    backup.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(server, "_read_json", lambda handler: {"history_id": 7})
    monkeypatch.setattr(server, "_log_path", lambda: log)
    monkeypatch.setattr(
        server.history_mod,
        "get_latest",
        lambda **kwargs: SimpleNamespace(id=7, event="commit", backup=str(backup)),
    )
    called = {}
    monkeypatch.setattr(
        server.transform_mod,
        "undo",
        lambda path, *, from_backup=None: called.update(
            path=path, from_backup=from_backup,
        ) or backup,
    )
    responses = []
    monkeypatch.setattr(
        server,
        "_json_response",
        lambda handler, status, payload: responses.append((status, payload)),
    )

    _handler().do_POST()

    assert responses == [(200, {"ok": True, "restored": str(backup)})]
    assert called == {"path": log, "from_backup": str(backup)}


def test_history_bound_undo_refuses_a_stale_drawer(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "story.jsonl"
    log.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(server, "_read_json", lambda handler: {"history_id": 6})
    monkeypatch.setattr(server, "_log_path", lambda: log)
    monkeypatch.setattr(
        server.history_mod,
        "get_latest",
        lambda **kwargs: SimpleNamespace(id=7, event="commit", backup="unused"),
    )
    monkeypatch.setattr(
        server.transform_mod,
        "undo",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not restore")),
    )
    responses = []
    monkeypatch.setattr(
        server,
        "_json_response",
        lambda handler, status, payload: responses.append((status, payload)),
    )

    _handler().do_POST()

    assert responses[0][0] == 409
    assert "history changed" in responses[0][1]["error"]
