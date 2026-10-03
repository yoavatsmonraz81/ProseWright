"""Minimal OpenAI-compatible chat client over the standard library (no deps).

Points at the local Gemma endpoint by default (config.MODEL_BASE_URL). Set
``STORY_EDITOR_MODEL_PROVIDER=openrouter`` (plus an API key) to use OpenRouter
or any other OpenAI-compatible host — same request shape, real Bearer auth.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config


class ModelError(RuntimeError):
    pass


class OutputLimitError(ModelError):
    """The output budget ended before the model produced usable content."""


def request_headers() -> dict[str, str]:
    """Headers for an OpenAI-compatible chat/models request."""
    key = (config.MODEL_API_KEY or "").strip()
    if not key:
        # Local textgen-webui accepts any bearer; remotes need a real key.
        key = "local"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    }
    # OpenRouter (and some proxies) use these for attribution / routing.
    if getattr(config, "MODEL_PROVIDER", "local") == "openrouter" or "openrouter.ai" in (
        config.MODEL_BASE_URL or ""
    ):
        if config.MODEL_HTTP_REFERER:
            headers["HTTP-Referer"] = config.MODEL_HTTP_REFERER
        if config.MODEL_APP_TITLE:
            headers["X-Title"] = config.MODEL_APP_TITLE
    return headers


def _recover_content(msg: dict) -> str:
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    if content and reasoning and content[0].islower():
        tail = reasoning.rstrip()
        last_space = max(tail.rfind(" "), tail.rfind("\n"))
        if last_space != -1:
            word_fragment = tail[last_space + 1:]
            if word_fragment and word_fragment[0].isupper() is False and len(word_fragment) < 20:
                content = word_fragment + content
    return content.strip()


def _parse_sse_line(line: bytes) -> dict | None:
    text = line.decode("utf-8", errors="replace").strip()
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if payload == "[DONE]":
        return {"done": True}
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def _reach_hint(url: str) -> str:
    if "openrouter.ai" in url or getattr(config, "MODEL_PROVIDER", "") == "openrouter":
        return (
            "Check STORY_EDITOR_MODEL_API_KEY / OPENROUTER_API_KEY and "
            "STORY_EDITOR_MODEL_NAME (OpenRouter model id)."
        )
    return "Is the textgen-webui server running with the OpenAI API enabled?"


def chat_stream(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    task: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.9,
    max_tokens: int = 1200,
    timeout: float = 600.0,
):
    """Yield ``{"kind": "reasoning"|"content", "text": str}`` chunks from the model."""
    base = (base_url or config.MODEL_BASE_URL).rstrip("/")
    url = f"{base}/chat/completions"
    resolved_model = model or config.model_for_task(task)
    payload = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": True,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw_line in resp:
                chunk = _parse_sse_line(raw_line)
                if chunk is None:
                    continue
                if chunk.get("done"):
                    break
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if delta.get("reasoning_content"):
                    yield {"kind": "reasoning", "text": delta["reasoning_content"]}
                if delta.get("content"):
                    yield {"kind": "content", "text": delta["content"]}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ModelError(
            f"model HTTP {exc.code} at {url}: {detail or exc.reason}. {_reach_hint(url)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelError(
            f"could not reach model at {url}: {exc}. {_reach_hint(url)}"
        ) from exc


def stream_collect(
    messages: list[dict[str, str]],
    *,
    on_event=None,
    **kwargs,
) -> str:
    """Stream from the model, optionally forwarding events, return final prose."""
    max_tokens = int(kwargs.pop("max_tokens", 1200) or 1200)
    allow_retry = bool(kwargs.pop("_allow_length_retry", True))

    def _once(budget: int) -> str:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for ev in chat_stream(messages, max_tokens=budget, **kwargs):
            if on_event is not None:
                on_event(ev)
            if ev.get("kind") == "content":
                content_parts.append(ev.get("text") or "")
            elif ev.get("kind") == "reasoning":
                reasoning_parts.append(ev.get("text") or "")
        msg = {
            "content": "".join(content_parts),
            "reasoning_content": "".join(reasoning_parts),
        }
        return _recover_content(msg)

    content = _once(max_tokens)
    if not content and allow_retry and max_tokens < 24_000:
        # Same recovery as non-streaming complete(): reasoning ate the budget.
        bumped = min(24_000, max(12_288, max_tokens * 2, max_tokens + 4_000))
        if on_event is not None:
            on_event({
                "kind": "phase",
                "text": f"empty content at {max_tokens} tokens — retrying at {bumped}…",
            })
        content = _once(bumped)
    if not content:
        raise ModelError(
            "model produced no output text (reasoning may have consumed the budget). "
            "Try a shorter direction, or raise STORY_EDITOR_NOVELIZE_MAX_TOKENS / "
            "STORY_EDITOR_REWRITE_MAX_TOKENS."
        )
    return content


@dataclass
class Completion:
    """A reply plus why it ended. Most callers want the text and nothing else;
    operators that generate long prose need to know they were cut off, because a
    scene that stops mid-sentence must not be filed as if it were whole."""

    text: str
    finish: str = ""
    model: str = ""

    @property
    def truncated(self) -> bool:
        return self.finish == "length"


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    task: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.9,
    max_tokens: int = 1200,
    timeout: float = 600.0,
    _allow_length_retry: bool = True,
) -> str:
    return complete(
        messages,
        model=model,
        task=task,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        _allow_length_retry=_allow_length_retry,
    ).text


def complete(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    task: str | None = None,
    base_url: str | None = None,
    temperature: float = 0.9,
    max_tokens: int = 1200,
    timeout: float = 600.0,
    _allow_length_retry: bool = True,
    enable_thinking: bool | None = None,
) -> Completion:
    base = (base_url or config.MODEL_BASE_URL).rstrip("/")
    url = f"{base}/chat/completions"
    resolved_model = model or config.model_for_task(task)
    payload = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if enable_thinking is not None:
        # textgen-webui uses the top-level switch; llama-server uses the
        # template kwarg. Only explicitly opted-in local requests send these.
        payload["enable_thinking"] = enable_thinking
        payload["chat_template_kwargs"] = {"enable_thinking": enable_thinking}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ModelError(
            f"model HTTP {exc.code} at {url}: {detail or exc.reason}. {_reach_hint(url)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ModelError(
            f"could not reach model at {url}: {exc}. {_reach_hint(url)}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ModelError(f"model returned non-JSON response: {exc}") from exc

    try:
        choice = body["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason") or ""
        content = _recover_content(msg)
        if not content and finish == "length" and _allow_length_retry and max_tokens < 24_000:
            # Jump hard: doubling a small prose budget (e.g. 2.6k → 6.6k) still
            # loses to reasoning-heavy local models. Floor the retry at 12k.
            bumped = min(24_000, max(12_288, max_tokens * 2, max_tokens + 4_000))
            return complete(
                messages,
                model=model,
                task=task,
                base_url=base_url,
                temperature=temperature,
                max_tokens=bumped,
                timeout=timeout,
                _allow_length_retry=False,
                enable_thinking=enable_thinking,
            )
        if not content and finish == "length":
            raise OutputLimitError(
                "model hit max_tokens before producing output (reasoning models "
                "often spend the budget thinking). Try a shorter direction, or "
                "raise STORY_EDITOR_NOVELIZE_MAX_TOKENS / "
                "STORY_EDITOR_REWRITE_MAX_TOKENS."
            )
        return Completion(text=content, finish=finish, model=resolved_model)
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"unexpected response shape: {body}") from exc


def list_models(*, base_url: str | None = None, timeout: float = 15.0) -> list[str]:
    """GET /models — empty list if the endpoint is missing or refuses."""
    base = (base_url or config.MODEL_BASE_URL).rstrip("/")
    url = f"{base}/models"
    req = urllib.request.Request(url, headers=request_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for row in data:
        if isinstance(row, dict) and row.get("id"):
            names.append(str(row["id"]))
    return names


def ping(
    *,
    model: str | None = None,
    base_url: str | None = None,
    timeout: float = 60.0,
) -> dict:
    """Cheap live check: one short completion. Raises ``ModelError`` on failure."""
    resolved = model or config.MODEL_NAME
    reply = complete(
        [{"role": "user", "content": "Reply with exactly: pong"}],
        model=resolved,
        base_url=base_url,
        temperature=0,
        max_tokens=16,
        timeout=timeout,
        _allow_length_retry=False,
    )
    return {
        "ok": True,
        "provider": getattr(config, "MODEL_PROVIDER", "local"),
        "base_url": (base_url or config.MODEL_BASE_URL).rstrip("/"),
        "model": reply.model or resolved,
        "reply": reply.text,
        "api_key_set": bool((config.MODEL_API_KEY or "").strip()),
    }
