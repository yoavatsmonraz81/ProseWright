"""Player-mode engine: local model backends and kp bench, against a stand-in server."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "player-mode" / "engine"))

from kp import backend, prompts  # noqa: E402


class _Server:
    """An OpenAI-compatible chat endpoint that replays canned replies."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                text = outer.replies.pop(0) if outer.replies else "(no more replies)"
                data = json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}],
                                   "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                             "prompt_tokens_details": {"cached_tokens": 60}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def server():
    servers = []

    def make(replies):
        s = _Server(replies)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


PLAN = {k: "" for k in prompts.PLAN_SCHEMA["required"]}


def test_prose_from_a_local_server_with_thinking_stripped(server):
    s = server(["<|channel>thought\nlet me think…<channel|>The lamps come on."])
    r = backend.call({"url": s.url, "model": "local-test"}, "system", "write")
    assert r.text == "The lamps come on." and r.cost_usd == 0.0 and r.model == "local-test"
    assert r.cached == 60 and r.tokens_in == 100 and r.tokens_out == 20
    req = s.requests[0]
    assert req["messages"][0] == {"role": "system", "content": "system"}
    assert req["enable_thinking"] is False and "grammar" not in req


def test_a_plan_is_requested_with_a_grammar_and_parsed(server):
    s = server(["```json\n" + json.dumps(PLAN) + "\n```"])
    r = backend.call({"url": s.url}, "plan", "move", schema=prompts.PLAN_SCHEMA)
    assert r.data == PLAN
    req = s.requests[0]
    assert req["grammar"] == req["grammar_string"] and req["grammar"].startswith("root ::= ")
    assert "JSON Schema" in req["messages"][0]["content"]


def test_a_broken_plan_is_retried_once_then_refused(server):
    s = server(["not json at all", json.dumps(PLAN)])
    r = backend.call({"url": s.url, "json": "prompt"}, "plan", "move", schema=prompts.PLAN_SCHEMA)
    assert r.data == PLAN and len(s.requests) == 2
    assert "not valid JSON" in s.requests[1]["messages"][-1]["content"]

    s = server(['{"read": "only one key"}', '{"read": "still one"}'])
    with pytest.raises(backend.ModelError, match="usable plan"):
        backend.call({"url": s.url, "json": "prompt"}, "plan", "move", schema=prompts.PLAN_SCHEMA)


def test_schema_mode_and_slot_pinning(server):
    s = server([json.dumps(PLAN)])
    backend.call({"url": s.url, "json": "schema", "slot": 1, "extra": {"top_k": 20}}, "p", "m",
                 schema=prompts.PLAN_SCHEMA)
    req = s.requests[0]
    assert req["response_format"]["type"] == "json_schema" and "grammar" not in req
    assert req["id_slot"] == 1 and req["cache_prompt"] is True and req["top_k"] == 20


def test_an_unreachable_server_says_so():
    with pytest.raises(backend.ModelError, match="not reachable"):
        backend.call({"url": "http://127.0.0.1:9/v1"}, "s", "p", timeout=3)
    with pytest.raises(backend.ModelError, match="needs a"):
        backend.call({"model": "x"}, "s", "p")


def test_the_grammar_covers_the_whole_plan():
    g = backend.schema_to_gbnf(prompts.PLAN_SCHEMA)
    names = {line.split(" ::= ")[0] for line in g.splitlines()}
    assert {"root", "ws", "string", "integer", "boolean"} <= names
    root = next(line for line in g.splitlines() if line.startswith("root ::= "))
    for key in prompts.PLAN_SCHEMA["required"]:
        assert json.dumps(json.dumps(key) + ":") in root
    for line in g.splitlines():  # every rule a line references is defined
        for ref in __import__("re").findall(r"\b(root-[a-z0-9-]+)\b", line.split(" ::= ", 1)[1]):
            assert ref in names, ref
