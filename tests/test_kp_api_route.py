"""Player-mode engine: reaching Claude through the Anthropic API (official SDK),
against a stand-in API server, and choosing between the Claude Code and API routes."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

anthropic = pytest.importorskip("anthropic")  # the API route is optional: pip install anthropic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "player-mode" / "engine"))

from kp import backend, prompts  # noqa: E402


class _API:
    """A stand-in for POST /v1/messages that records what the SDK sent."""

    def __init__(self, reply: dict, status: int = 200):
        self.calls: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.calls.append({"path": self.path, "headers": dict(self.headers), "body": body})
                data = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


def _message(text: str, *, stop: str = "end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": text}], "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 9000,
                      "cache_creation_input_tokens": 0}}


@pytest.fixture()
def api(monkeypatch):
    servers = []

    def make(reply, status=200):
        s = _API(reply, status)
        servers.append(s)
        monkeypatch.setenv("ANTHROPIC_BASE_URL", s.url)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(backend, "_client", None)
        backend.configure("api")
        return s

    yield make
    backend.configure(None)
    for s in servers:
        s.httpd.shutdown()
        s.httpd.server_close()


def test_a_plan_through_the_api(api):
    plan = {k: "" for k in prompts.PLAN_SCHEMA["required"]}
    s = api(_message(json.dumps(plan)))
    r = backend.call("claude-sonnet-5-5", "the rules", "the move", effort="low", schema=prompts.PLAN_SCHEMA)
    assert r.data == plan and r.cached == 9000 and r.tokens_out == 200
    assert r.cost_usd == pytest.approx((1000 * 2 + 9000 * 0.2 + 200 * 10) / 1e6)
    call = s.calls[0]
    body, headers = call["body"], {k.lower(): v for k, v in call["headers"].items()}
    assert headers["x-api-key"] == "test-key"  # the runner's own credentials
    assert "server-side-fallback-2026-07-01" in headers.get("anthropic-beta", "")
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "low",
                                     "format": {"type": "json_schema", "schema": prompts.PLAN_SCHEMA}}
    assert body["system"] == [{"type": "text", "text": "the rules", "cache_control": {"type": "ephemeral"}}]
    assert body["messages"] == [{"role": "user", "content": "the move"}]


def test_prose_on_haiku_without_effort_or_fallback(api):
    s = api(_message("Wren waves."))
    r = backend.call("claude-haiku-4-5", "system", "prompt", effort="low")
    assert r.text == "Wren waves." and r.data is None
    body = s.calls[0]["body"]
    assert "output_config" not in body and "fallbacks" not in body


def test_refusals_and_bad_credentials_are_explained(api):
    api(_message("", stop="refusal"))
    with pytest.raises(backend.ModelError, match="declined"):
        backend.call("claude-sonnet-5-5", "s", "p")
    api({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}, status=401)
    with pytest.raises(backend.ModelError, match="rejected the credentials"):
        backend.call("claude-sonnet-5-5", "s", "p")


def test_route_selection(monkeypatch):
    backend.configure(None)
    monkeypatch.delenv("PLAYER_MODE_CLAUDE", raising=False)
    monkeypatch.setattr(backend.shutil, "which", lambda name: "/usr/bin/claude")
    assert backend.claude_route() == "code"
    monkeypatch.setattr(backend.shutil, "which", lambda name: None)
    assert backend.claude_route() == "api"
    monkeypatch.setenv("PLAYER_MODE_CLAUDE", "code")
    assert backend.claude_route() == "code"
    backend.configure("api")  # the play folder's "claude" key wins
    assert backend.claude_route() == "api"
    backend.configure(None)
    with pytest.raises(backend.ModelError):
        backend.configure("sometimes")


def test_missing_claude_code_says_how_to_continue(monkeypatch):
    backend.configure("code")
    monkeypatch.setenv("PATH", "/nonexistent")
    try:
        with pytest.raises(backend.ModelError, match="not installed"):
            backend.call("claude-sonnet-5-5", "s", "p")
    finally:
        backend.configure(None)
