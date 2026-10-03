"""Model calls: Claude (through Claude Code or the Anthropic API), or a local server.

A role's model in the play folder's config is either

* a Claude model name (the default: "claude-sonnet-5-5"), reached by one of
  two routes, always on the credentials of whoever runs the engine:

    "code"  headless Claude Code (`claude -p`) on the user's own `claude`
            login — a Claude subscription or an API key, whichever Claude
            Code is signed in with. Each call replaces the system prompt,
            disables every tool and skips settings, MCP and slash commands,
            so the request carries only what the engine compiled.
    "api"   the Anthropic API through the official SDK (`pip install
            anthropic`), which finds its credentials itself:
            ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an `ant auth login`
            profile. Nothing is stored by the engine.

  The route is "auto" unless set: Claude Code when `claude` is installed,
  the API otherwise. Set it with "claude": "code" | "api" in the play
  folder's config, or the PLAYER_MODE_CLAUDE environment variable; or


* a local server, as an object:

      {"url": "http://127.0.0.1:5000/v1", "model": "gemma-4-31b",
       "json": "grammar", "temperature": 0.8, "max_tokens": 1500,
       "slot": 0, "thinking": false, "api_key_env": "LOCAL_LLM_KEY", "extra": {...}}

  called through the OpenAI-compatible chat API (text-generation-webui,
  llama.cpp's llama-server, vLLM, LM Studio, Ollama...). "url" is the only
  required key. For structured replies (the planner's plan), "json" chooses
  how valid JSON is obtained:
      "grammar"  a GBNF grammar generated from the schema, enforced while
                 generating (text-generation-webui, llama.cpp) — the default;
      "schema"   OpenAI-style response_format json_schema (llama.cpp, vLLM,
                 LM Studio);
      "prompt"   instructions only, for servers with neither.
  Whatever the mode, the reply is parsed leniently and retried once if it is
  not valid. "slot" pins the role to a llama.cpp server slot so its prompt
  cache stays warm. Local calls cost nothing; tokens are still counted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUN_DIR = Path.home() / ".cache" / "player-mode" / "run"  # empty cwd: no CLAUDE.md discovery

# Haiku 4.5 rejects the effort setting; everything newer takes it.
_NO_EFFORT = ("haiku",)

# How Claude models are reached: "auto", "code" or "api" (see the module doc).
_CLAUDE_ROUTE: str | None = None


def configure(route: str | None) -> None:
    """Set the route for Claude models (from the play folder's "claude" key)."""
    global _CLAUDE_ROUTE
    if route not in (None, "", "auto", "code", "api"):
        raise ModelError(f'"claude" must be "auto", "code" or "api", not {route!r}')
    _CLAUDE_ROUTE = route or None


def claude_route() -> str:
    route = _CLAUDE_ROUTE or os.environ.get("PLAYER_MODE_CLAUDE") or "auto"
    if route == "auto":
        return "code" if shutil.which("claude") else "api"
    return route


class ModelError(RuntimeError):
    pass


@dataclass
class Result:
    text: str
    data: Any = None
    model: str = ""
    cost_usd: float = 0.0
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def cached(self) -> int:
        return int(self.usage.get("cache_read_input_tokens", 0))

    @property
    def tokens_in(self) -> int:
        u = self.usage
        return int(u.get("input_tokens", 0)) + self.cached + int(u.get("cache_creation_input_tokens", 0))

    @property
    def tokens_out(self) -> int:
        return int(self.usage.get("output_tokens", 0))


def call(
    model: str | dict,
    system: str,
    prompt: str,
    *,
    effort: str | None = None,
    schema: dict | None = None,
    timeout: int = 300,
) -> Result:
    if isinstance(model, dict):
        return _call_local(model, system, prompt, schema=schema, timeout=timeout)
    if claude_route() == "api":
        return _call_api(model, system, prompt, effort=effort, schema=schema, timeout=timeout)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        "claude", "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", "",
        "--no-session-persistence",
        "--output-format", "json",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
    ]
    if effort and not any(tag in model for tag in _NO_EFFORT):
        cmd += ["--effort", effort]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=RUN_DIR
        )
    except subprocess.TimeoutExpired as exc:
        raise ModelError(f"{model} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ModelError("Claude Code (`claude`) is not installed. Install it and sign in with `claude auth "
                         "login`, or use the Anthropic API instead: pip install anthropic, set "
                         "ANTHROPIC_API_KEY, and set \"claude\": \"api\" in the play folder's config.") from exc
    if not proc.stdout.strip():
        raise ModelError(f"{model} returned nothing: {proc.stderr.strip()[:400]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ModelError(f"{model} returned non-JSON: {proc.stdout[:400]}") from exc
    if out.get("is_error"):
        detail = str(out.get("result") or out.get("subtype"))
        hint = ""
        if "subscription access" in detail or "API key" in detail:
            hint = (" — Claude Code can't use the signed-in subscription. Check that your Claude subscription is "
                    "active (a lapsed one gives exactly this), then run `claude auth login` again; or "
                    "use an Anthropic API key: set ANTHROPIC_API_KEY (and, for the direct route, "
                    "\"claude\": \"api\" with `pip install anthropic`).")
        raise ModelError(f"{model} error: {detail}{hint}")
    if out.get("stop_reason") == "refusal":
        raise ModelError(f"{model} refused this turn")
    return Result(
        text=(out.get("result") or "").strip(),
        data=out.get("structured_output"),
        model=model,
        cost_usd=float(out.get("total_cost_usd") or 0.0),
        usage=out.get("usage") or {},
        seconds=round(time.monotonic() - started, 1),
    )


# --- local servers (OpenAI-compatible chat API) ------------------------------------
def schema_to_gbnf(schema: dict) -> str:
    """A GBNF grammar for the JSON Schema subset the engine uses: objects with
    named properties, strings (with enums), integers, numbers, booleans and
    arrays. Every listed property is emitted, in schema order."""
    rules: dict[str, str] = {}

    def lit(text: str) -> str:
        return json.dumps(json.dumps(text))  # a GBNF string literal of the JSON string

    def rule(node: dict, name: str) -> str:
        kind = node.get("type")
        if "enum" in node:
            rules[name] = " | ".join(lit(v) for v in node["enum"])
        elif kind == "object":
            props = node.get("properties", {})
            parts = []
            for i, (key, sub) in enumerate(props.items()):
                child = rule(sub, f"{name}-{re.sub(r'[^a-z0-9]+', '-', key.lower())}")
                sep = '"," ws ' if i else ""
                parts.append(f"{sep}{json.dumps(json.dumps(key) + ':')} ws {child} ws")
            rules[name] = '"{" ws ' + " ".join(parts) + ' "}"' if parts else '"{" ws "}"'
        elif kind == "array":
            item = rule(node.get("items", {"type": "string"}), f"{name}-item")
            rules[name] = f'"[" ws ( {item} ( ws "," ws {item} )* )? ws "]"'
        elif kind == "integer":
            return "integer"
        elif kind == "number":
            return "number"
        elif kind == "boolean":
            return "boolean"
        else:
            return "string"
        return name

    root = rule(schema, "root")
    base = {
        "ws": r"[ \t\n]{0,20}",
        "string": r'"\"" ( [^"\\\x00-\x1f] | "\\" ( ["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] ) )* "\""',
        "integer": r'"-"? ( "0" | [1-9] [0-9]* )',
        "number": r'"-"? ( "0" | [1-9] [0-9]* ) ( "." [0-9]+ )?',
        "boolean": r'"true" | "false"',
    }
    if root != "root":
        rules["root"] = root
    ordered = {"root": rules.pop("root"), **rules, **base}  # root first, as is customary
    return "\n".join(f"{k} ::= {v}" for k, v in ordered.items())


_THOUGHT = re.compile(r"<\|channel>thought.*?(?:<channel\|>|$)|<think>.*?(?:</think>|$)", re.DOTALL)


def _strip_thinking(text: str) -> str:
    """A reply without any reasoning block a thinking model left in it."""
    return _THOUGHT.sub("", text).strip()


def _parse_json(text: str) -> Any:
    """The JSON object in a reply, tolerating code fences and stray prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(t[start:end + 1])


def _local_request(spec: dict, messages: list[dict], *, schema: dict | None, timeout: int) -> dict:
    url = spec["url"].rstrip("/") + "/chat/completions"
    body: dict[str, Any] = {
        "model": spec.get("model", ""),
        "messages": messages,
        "temperature": spec.get("temperature", 0.4 if schema else 0.8),
        "max_tokens": spec.get("max_tokens", 2500 if schema else 1500),
        "stream": False,
        # Thinking models (Gemma 4, Qwen 3…) would put their reasoning in the reply;
        # off unless the role asks for it ("thinking": true).
        "enable_thinking": bool(spec.get("thinking", False)),
        "chat_template_kwargs": {"enable_thinking": bool(spec.get("thinking", False))},
    }
    if spec.get("slot") is not None:  # llama.cpp: keep this role's prompt cache warm
        body["id_slot"] = spec["slot"]
        body["cache_prompt"] = True
    if schema:
        mode = spec.get("json", "grammar")
        if mode == "grammar":
            grammar = schema_to_gbnf(schema)
            body["grammar_string"] = grammar  # text-generation-webui
            body["grammar"] = grammar         # llama.cpp's llama-server
        elif mode == "schema":
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "reply", "schema": schema, "strict": True}}
    body.update(spec.get("extra") or {})
    headers = {"Content-Type": "application/json"}
    key = spec.get("api_key") or (os.environ.get(spec["api_key_env"]) if spec.get("api_key_env") else None)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ModelError(f"local model at {url} answered {exc.code}: {exc.read()[:300]!r}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ModelError(f"local model at {url} is not reachable ({exc}); is the server running?") from exc
    except json.JSONDecodeError as exc:
        raise ModelError(f"local model at {url} returned something that isn't JSON") from exc


def _call_local(spec: dict, system: str, prompt: str, *, schema: dict | None, timeout: int) -> Result:
    if not spec.get("url"):
        raise ModelError("a local model needs a \"url\" (e.g. http://127.0.0.1:5000/v1)")
    name = spec.get("model") or spec["url"]
    if schema:
        system = (system + "\n\nReply with a single JSON object and nothing else. It must match this "
                  "JSON Schema exactly, with every listed property:\n" + json.dumps(schema))
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    started = time.monotonic()
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    data, text = None, ""
    for attempt in range(2 if schema else 1):
        out = _local_request(spec, messages, schema=schema, timeout=timeout)
        u = out.get("usage") or {}
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        usage["input_tokens"] += int(u.get("prompt_tokens") or 0) - cached
        usage["cache_read_input_tokens"] += cached
        usage["output_tokens"] += int(u.get("completion_tokens") or 0)
        try:
            text = _strip_thinking(out["choices"][0]["message"].get("content") or "")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError(f"{name} returned no message") from exc
        if not schema:
            break
        try:
            data = _parse_json(text)
            missing = [k for k in schema.get("required", []) if k not in data]
            if not missing:
                break
            problem = f"it is missing {', '.join(missing)}"
        except json.JSONDecodeError as exc:
            problem = f"it is not valid JSON ({exc.msg})"
        data = None
        messages = messages + [{"role": "assistant", "content": text},
                               {"role": "user", "content": f"That reply can't be used: {problem}. "
                                                           "Reply again with only the JSON object."}]
    if schema and data is None:
        raise ModelError(f"{name} did not return a usable plan after a retry: {text[:300]!r}")
    if not schema and not text:
        raise ModelError(f"{name} returned an empty reply")
    return Result(text=text, data=data, model=name, cost_usd=0.0, usage=usage,
                  seconds=round(time.monotonic() - started, 1))


# --- the Anthropic API, through the official SDK -----------------------------------
# List prices per million tokens (input, output), for the cost the engine shows;
# cache reads cost a tenth of input, cache writes 1.25 times input.
_PRICES = {"claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0), "claude-opus-5-5": (4.0, 20.0)}
# Models that take the server-side refusal fallback ("fallbacks": "default").
_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1")
_client = None


def _api_client(timeout: int):
    global _client
    try:
        import anthropic
    except ImportError as exc:
        raise ModelError("the Anthropic API route needs the SDK: pip install anthropic") from exc
    if _client is None:
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile
    return anthropic, _client.with_options(timeout=float(timeout))


def _call_api(model: str, system: str, prompt: str, *, effort: str | None, schema: dict | None,
              timeout: int) -> Result:
    anthropic, client = _api_client(timeout)
    params: dict[str, Any] = {
        "model": model,
        "max_tokens": 16000,
        # The system prompt (contract, canon, rules) repeats from turn to turn: cache it.
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": prompt}],
    }
    output_config: dict[str, Any] = {}
    if effort and not any(tag in model for tag in _NO_EFFORT):
        output_config["effort"] = effort
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if output_config:
        params["output_config"] = output_config
    started = time.monotonic()
    try:
        if model in _FALLBACK_MODELS:
            # On a safety decline the API re-runs the request on a fallback model it picks.
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
        else:
            response = client.messages.create(**params)
    except anthropic.AuthenticationError as exc:
        raise ModelError("the Anthropic API rejected the credentials: set ANTHROPIC_API_KEY "
                         "(or run `ant auth login`)") from exc
    except anthropic.PermissionDeniedError as exc:
        raise ModelError(f"the Anthropic API key can't use {model}: {exc.message}") from exc
    except anthropic.NotFoundError as exc:
        raise ModelError(f"the Anthropic API doesn't know the model {model!r}") from exc
    except anthropic.RateLimitError as exc:
        raise ModelError(f"the Anthropic API is rate-limiting this key; try again shortly ({exc.message})") from exc
    except anthropic.APIStatusError as exc:
        raise ModelError(f"the Anthropic API answered {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise ModelError(f"can't reach the Anthropic API ({exc})") from exc
    if response.stop_reason == "refusal":
        category = getattr(response.stop_details, "category", None) if response.stop_details else None
        raise ModelError(f"{model} declined this turn{f' ({category})' if category else ''}")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    data = None
    if schema:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelError(f"{model} returned a plan that isn't JSON: {text[:200]!r}") from exc
    u = response.usage
    usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_input_tokens": u.cache_read_input_tokens or 0,
        "cache_creation_input_tokens": u.cache_creation_input_tokens or 0,
    }
    price_in, price_out = _PRICES.get(model, (0.0, 0.0))
    cost = (usage["input_tokens"] * price_in + usage["cache_read_input_tokens"] * price_in * 0.1
            + usage["cache_creation_input_tokens"] * price_in * 1.25 + usage["output_tokens"] * price_out) / 1e6
    return Result(text=text, data=data, model=response.model or model, cost_usd=round(cost, 6), usage=usage,
                  seconds=round(time.monotonic() - started, 1))
