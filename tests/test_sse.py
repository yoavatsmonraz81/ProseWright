"""How a streaming response ends.

An SSE reply carries no Content-Length and no chunk framing, so the closed
socket is the only end-of-response a client can see. Announcing keep-alive
instead left the browser waiting minutes after the prose had arrived, with the
operator still apparently running — these tests pin the framing that fixed it.
"""

from __future__ import annotations

import io
import json

from story_editor import server


class Recorder:
    """Enough of BaseHTTPRequestHandler for the SSE helpers to write into."""

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.ended = False
        self.wfile = io.BytesIO()

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, keyword: str, value: str) -> None:
        self.headers.append((keyword, value))

    def end_headers(self) -> None:
        self.ended = True

    def header(self, keyword: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == keyword.lower():
                return value
        return None

    def frames(self) -> list[dict]:
        out = []
        for block in self.wfile.getvalue().decode("utf-8").split("\n\n"):
            for line in block.splitlines():
                if line.startswith("data:"):
                    out.append(json.loads(line[5:].strip()))
        return out


class Result:
    """Stand-in for an edit set: the shape `_stream_propose` reports."""

    def __init__(self, edits: list[dict] | None = None) -> None:
        self.edits = edits if edits is not None else [{"msg_id": 2}]

    def to_json(self) -> dict:
        return {"edits": self.edits}


def test_sse_headers_close_the_response():
    rec = Recorder()
    server._sse_begin(rec)

    assert rec.status == 200
    assert rec.header("Content-Type") == "text/event-stream; charset=utf-8"
    # The whole bug in one assertion: keep-alive gives the client no way to know
    # the stream is over, because nothing else in the response marks its end.
    assert rec.header("Connection") == "close"
    assert rec.header("Content-Length") is None
    assert rec.ended


def test_stream_propose_ends_on_a_verdict():
    rec = Recorder()
    server._stream_propose(
        rec,
        lambda emit: (emit({"kind": "phase", "text": "working"}), Result())[1],
    )

    frames = rec.frames()
    assert [f["kind"] for f in frames] == ["phase", "done"]
    assert frames[-1]["ok"] is True
    assert frames[-1]["edit_set"] == {"edits": [{"msg_id": 2}]}
    assert frames[-1]["advisory"] is False


def test_a_proposal_that_changes_nothing_is_advisory():
    rec = Recorder()
    server._stream_propose(rec, lambda emit: Result(edits=[]))

    verdict = rec.frames()[-1]
    assert verdict["kind"] == "done"
    assert verdict["advisory"] is True


def test_model_failure_arrives_as_a_verdict_not_a_silence():
    from story_editor import llm

    def work(emit):
        emit({"kind": "phase", "text": "working"})
        raise llm.ModelError("could not reach model")

    rec = Recorder()
    server._stream_propose(rec, work)

    frames = rec.frames()
    assert [f["kind"] for f in frames] == ["phase", "error"]
    assert "could not reach model" in frames[-1]["error"]


def test_spine_validate_stream_emits_phase_then_done_spine():
    """Validate uses the same SSE framing as propose; done carries the spine."""
    rec = Recorder()
    spine_payload = {"authored": [], "derived": [], "counts": {}}

    def work(emit):
        emit({
            "kind": "phase",
            "text": "batch 1/2 — A1, A2, A3",
            "stage": "model",
            "step": 4,
            "total": 5,
        })
        return spine_payload

    server._stream_propose(
        rec,
        work,
        done_builder=lambda payload: {"spine": payload},
    )

    frames = rec.frames()
    assert frames[0]["kind"] == "phase"
    assert frames[0]["step"] == 4
    assert frames[0]["total"] == 5
    assert frames[-1] == {"kind": "done", "ok": True, "spine": spine_payload}
    assert rec.header("Connection") == "close"


def test_spine_derive_stream_uses_same_done_spine_shape():
    """Derive is propose-then-commit; the stream still ends with a spine view."""
    rec = Recorder()
    spine_payload = {
        "authored": [],
        "derived": [],
        "pending": {"n_beats": 3, "beats": []},
        "counts": {},
    }

    def work(emit):
        emit({
            "kind": "phase",
            "text": "proposing up to 14 beats",
            "stage": "model",
            "step": 0,
            "total": 0,
        })
        return spine_payload

    server._stream_propose(
        rec,
        work,
        done_builder=lambda payload: {"spine": payload},
    )

    frames = rec.frames()
    assert frames[0]["stage"] == "model"
    assert frames[-1]["spine"]["pending"]["n_beats"] == 3
