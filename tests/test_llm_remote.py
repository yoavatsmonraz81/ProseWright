"""OpenAI-compatible client auth + OpenRouter-style headers (no live network)."""

from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from story_editor import config, llm


def test_request_headers_use_api_key(monkeypatch):
    monkeypatch.setattr(config, "MODEL_API_KEY", "sk-test-key")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "local")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "http://127.0.0.1:5000/v1")
    h = llm.request_headers()
    assert h["Authorization"] == "Bearer sk-test-key"
    assert "HTTP-Referer" not in h


def test_openrouter_headers_include_attribution(monkeypatch):
    monkeypatch.setattr(config, "MODEL_API_KEY", "sk-or-v1-test")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(config, "MODEL_HTTP_REFERER", "https://example.test")
    monkeypatch.setattr(config, "MODEL_APP_TITLE", "Story Editor Test")
    h = llm.request_headers()
    assert h["Authorization"] == "Bearer sk-or-v1-test"
    assert h["HTTP-Referer"] == "https://example.test"
    assert h["X-Title"] == "Story Editor Test"


def test_complete_sends_bearer_and_parses_reply(monkeypatch):
    monkeypatch.setattr(config, "MODEL_API_KEY", "sk-or-v1-test")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(config, "MODEL_NAME", "anthropic/claude-sonnet-4")
    monkeypatch.setattr(config, "MODEL_HTTP_REFERER", "https://example.test")
    monkeypatch.setattr(config, "MODEL_APP_TITLE", "SE")

    captured: dict = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {"content": "pong"},
                    "finish_reason": "stop",
                }],
            }).encode()

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode())
        return FakeResp()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    reply = llm.complete(
        [{"role": "user", "content": "ping"}],
        model="anthropic/claude-sonnet-4",
        max_tokens=8,
        _allow_length_retry=False,
    )
    assert reply.text == "pong"
    assert captured["url"].endswith("/chat/completions")
    # urllib Request normalizes header names
    auth = captured["headers"].get("Authorization") or captured["headers"].get("authorization")
    assert auth == "Bearer sk-or-v1-test"
    assert captured["body"]["model"] == "anthropic/claude-sonnet-4"
    assert 'enable_thinking' not in captured['body']


def test_complete_can_disable_local_thinking_per_request(monkeypatch):
    captured = {}
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps({'choices': [{'message': {'content': '{"edits": []}'},
                                           'finish_reason': 'stop'}]}).encode()
    def request(req, timeout=0):
        captured.update(json.loads(req.data))
        return Resp()
    monkeypatch.setattr(llm.urllib.request, 'urlopen', request)
    llm.complete([{'role': 'user', 'content': 'Repair rhythm.'}], enable_thinking=False)
    assert captured['enable_thinking'] is False
    assert captured['chat_template_kwargs']['enable_thinking'] is False


def test_http_error_surfaces_status(monkeypatch):
    monkeypatch.setattr(config, "MODEL_API_KEY", "bad")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://openrouter.ai/api/v1")

    def boom(req, timeout=0):
        raise HTTPError(
            req.full_url, 401, "Unauthorized", hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"No auth credentials found"}}'),
        )

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(llm.ModelError) as exc:
        llm.ping()
    assert "401" in str(exc.value)
    assert "OPENROUTER" in str(exc.value) or "API_KEY" in str(exc.value)


def test_list_models_parses_ids(monkeypatch):
    monkeypatch.setattr(config, "MODEL_API_KEY", "sk")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openrouter")

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({
                "data": [{"id": "anthropic/claude-sonnet-4"}, {"id": "openai/gpt-4o"}],
            }).encode()

    monkeypatch.setattr(
        llm.urllib.request, "urlopen", lambda req, timeout=0: FakeResp(),
    )
    assert llm.list_models() == [
        "anthropic/claude-sonnet-4",
        "openai/gpt-4o",
    ]


def test_apply_model_settings_roundtrip(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "local")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "http://127.0.0.1:5000/v1")
    monkeypatch.setattr(config, "MODEL_NAME", "local-a.gguf")
    monkeypatch.setattr(config, "MODEL_API_KEY", "")
    monkeypatch.setattr(config, "MODEL_PRESET_CREATIVE", "local-a.gguf")
    monkeypatch.setattr(config, "MODEL_PRESET_ANALYST", "local-a.gguf")
    monkeypatch.setattr(config, "_ENV_CREATIVE", None)
    monkeypatch.setattr(config, "_ENV_ANALYST", None)
    monkeypatch.setattr(
        config, "_LAST_MODEL_BY_PROVIDER",
        {"local": "local-a.gguf", "openrouter": "anthropic/claude-sonnet-4"},
    )
    monkeypatch.setattr(
        config, "_LAST_URL_BY_PROVIDER",
        {
            "local": "http://127.0.0.1:5000/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        },
    )

    with pytest.raises(ValueError, match="API key"):
        config.apply_model_settings(provider="openrouter")

    snap = config.apply_model_settings(
        provider="openrouter",
        api_key="sk-test",
        model_name="anthropic/claude-sonnet-4",
    )
    assert snap["provider"] == "openrouter"
    assert snap["api_key_set"] is True
    assert "openrouter.ai" in snap["base_url"]
    assert "api_key" not in snap

    back = config.apply_model_settings(provider="local")
    assert back["provider"] == "local"
    assert back["model"] == "local-a.gguf"


def test_model_settings_public_omits_secret(monkeypatch):
    monkeypatch.setattr(config, "MODEL_API_KEY", "sk-secret")
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openrouter")
    monkeypatch.setattr(config, "MODEL_NAME", "x")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    pub = config.model_settings_public()
    assert pub["api_key_set"] is True
    assert "sk-secret" not in str(pub)


def test_apply_local_port_rewrites_only_the_port(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "local")
    monkeypatch.setattr(config, "MODEL_BASE_URL", "http://127.0.0.1:5000/v1")
    monkeypatch.setattr(config, "MODEL_NAME", "local.gguf")
    monkeypatch.setattr(config, "MODEL_PRESET_CREATIVE", "local.gguf")
    monkeypatch.setattr(config, "MODEL_PRESET_ANALYST", "local.gguf")
    monkeypatch.setattr(config, "_ENV_CREATIVE", None)
    monkeypatch.setattr(config, "_ENV_ANALYST", None)

    snap = config.apply_model_settings(local_port=5007)
    assert snap["base_url"] == "http://127.0.0.1:5007/v1"
    assert snap["local_port"] == 5007


@pytest.mark.parametrize("port", [0, 65536, "wrong"])
def test_apply_local_port_rejects_invalid_values(monkeypatch, port):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "local")
    with pytest.raises(ValueError, match="1 to 65535"):
        config.apply_model_settings(local_port=port)


def test_sync_local_model_adopts_loaded_id(monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "local")
    monkeypatch.setattr(config, "MODEL_NAME", "gemma-4-stale.gguf")
    monkeypatch.setattr(config, "MODEL_PRESET_CREATIVE", "gemma-4-stale.gguf")
    monkeypatch.setattr(config, "MODEL_PRESET_ANALYST", "gemma-4-stale.gguf")
    monkeypatch.setattr(config, "_ENV_MODEL_NAME", None)
    monkeypatch.setattr(config, "_ENV_CREATIVE", None)
    monkeypatch.setattr(config, "_ENV_ANALYST", None)
    monkeypatch.setattr(
        config, "_LAST_MODEL_BY_PROVIDER",
        {"local": "gemma-4-stale.gguf", "openrouter": "anthropic/claude-sonnet-4"},
    )

    info = config.sync_local_model_from_api(["G4-MeroMero-v2-31B-Q8_0.gguf"])
    assert info["synced"] is True
    assert config.MODEL_NAME == "G4-MeroMero-v2-31B-Q8_0.gguf"
    # Already current — no churn.
    again = config.sync_local_model_from_api(["G4-MeroMero-v2-31B-Q8_0.gguf"])
    assert again["synced"] is False
    assert again["reason"] == "already_current"
