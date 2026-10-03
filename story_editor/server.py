"""Phase 6 — local HTTP API for thin clients (SillyTavern extension, scripts).

Stdlib only (``ThreadingHTTPServer``). Long-running LLM calls run in request
threads; one client at a time is recommended for v1.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import ask as ask_mod, author as author_mod, canon_layer, code_sync, config, dossier as dossier_mod, drive_sync, fonts as fonts_mod, history as history_mod, layers as layers_mod, loader, manuscript as manuscript_mod, novelize as novelize_mod, proofread as proofread_mod, propagate as propagate_mod, prose_review as prose_review_mod, publication as publication_mod, st_sync, structure as structure_mod, transform as transform_mod, views as views_mod

API_VERSION = "0.7.0-author"

# Confidence floor below which a label still wants a human eye. Every
# attribution route shares it: if the review GET and the bulk-set POST disagree
# on the threshold, the drawer offers rows it then declines to act on.
REVIEW_MIN_CONFIDENCE = 0.95

# Fields of a bulk-set body that describe the label being written, not the rows
# to write it to.
_BULK_ASSIGNMENT_KEYS = frozenset({"voice", "mode", "reviewed", "notes", "msg_ids"})


class _StreamStopped(Exception):
    """Client disconnected during SSE."""


def _sse_begin(handler: BaseHTTPRequestHandler) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    # `close`, not `keep-alive`: these frames carry no length and no chunk
    # framing, so closing the socket is the only end-of-response a client can
    # detect. Announcing keep-alive also tells http.server to hold the
    # connection, which leaves the caller waiting long after the verdict.
    handler.send_header("Connection", "close")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.end_headers()


def _sse_emit(handler: BaseHTTPRequestHandler, event: dict) -> None:
    try:
        line = f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        handler.wfile.write(line.encode("utf-8"))
        handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        raise _StreamStopped


def _stream_propose(
    handler: BaseHTTPRequestHandler,
    work_fn,
    *,
    done_builder=None,
) -> None:
    from . import llm as llm_mod

    try:
        _sse_begin(handler)

        def emit(ev: dict) -> None:
            _sse_emit(handler, ev)

        result = work_fn(emit)
        if done_builder is None:
            payload: dict[str, Any] = {
                "kind": "done",
                "ok": True,
                "edit_set": result.to_json(),
            }
            if hasattr(result, "edits"):
                payload["advisory"] = result.edits == []
        else:
            payload = {"kind": "done", "ok": True, **done_builder(result)}
        _sse_emit(handler, payload)
    except _StreamStopped:
        return
    except llm_mod.ModelError as exc:
        try:
            _sse_emit(handler, {"kind": "error", "error": str(exc)})
        except _StreamStopped:
            pass
    except (ValueError, IndexError) as exc:
        try:
            _sse_emit(handler, {"kind": "error", "error": str(exc)})
        except _StreamStopped:
            pass


def _attribution():
    from . import attribution as attribution_mod

    return attribution_mod


def _log_turns(log_path: Path) -> list[dict[str, Any]]:
    log = loader.load(log_path)
    return [
        {
            "msg_id": m.msg_id,
            "name": m.speaker,
            "is_user": m.is_user,
            "mes": m.text,
        }
        for m in log.messages
    ]


def _attribution_review_payload(
    log_path: Path,
    *,
    min_confidence: float = REVIEW_MIN_CONFIDENCE,
    review_filter: Any | None = None,
) -> dict[str, Any]:
    attr = _attribution()
    turns = _log_turns(log_path)
    store = attr.load_store()
    if review_filter is None:
        review_filter = attr.ReviewFilter()
    elif isinstance(review_filter, dict):
        review_filter = attr.review_filter_from_mapping(review_filter)
    return attr.build_review_payload(
        turns,
        store,
        min_confidence=min_confidence,
        review_filter=review_filter,
    )


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        handler.send_header("Access-Control-Allow-Headers", "Content-Type")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        # Client closed (browser timeout, tab closed) before the body was sent.
        return


def _file_response(
    handler: BaseHTTPRequestHandler,
    path: Path,
    *,
    content_type: str,
    download_name: str | None = None,
) -> None:
    body = path.read_bytes()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        if download_name:
            handler.send_header(
                "Content-Disposition", f'attachment; filename="{download_name}"',
            )
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


def _read_json(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", 0))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode("utf-8"))


def _list_remote_models(*, timeout: float = 4.0) -> list[str]:
    from . import llm as llm_mod

    return llm_mod.list_models(timeout=timeout)


def _model_reachable() -> bool:
    from . import llm as llm_mod

    if _list_remote_models(timeout=4.0):
        return True
    # Some local servers answer /models with an empty or odd body; treat HTTP
    # success as reachable. Remote providers need a key + a real list.
    url = config.MODEL_BASE_URL.rstrip("/") + "/models"
    try:
        req = urllib.request.Request(
            url, headers=llm_mod.request_headers(), method="GET",
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _sync_and_list_models() -> tuple[list[str], dict[str, Any]]:
    """Fetch /models and, for local provider, adopt the loaded id when stale."""
    available = _list_remote_models(timeout=4.0)
    sync_info: dict[str, Any] = {"synced": False, "available": available}
    if getattr(config, "MODEL_PROVIDER", "local") == "local" and available:
        sync_info = config.sync_local_model_from_api(available)
    return available, sync_info


def _log_path() -> Path:
    return config.working_log()


def _edit_set_payload(edit_set: transform_mod.EditSet | None) -> dict[str, Any] | None:
    if edit_set is None:
        return None
    return edit_set.to_json()


def _body_log_path(body: dict) -> Path:
    return Path(body.get("log") or _log_path())


def _query_log_path(query: dict[str, list[str]]) -> Path:
    raw = query.get("log", [None])[0]
    return Path(raw) if raw else _log_path()


def _as_layer(raw: Any) -> str:
    """Validate a layer name. Defaults to the manuscript, because that is the
    layer a novel editor client is nearly always asking about — the log has its
    own long-standing routes."""
    layer = str(raw or layers_mod.MANUSCRIPT).strip().lower()
    if not layers_mod.is_layer(layer):
        raise layers_mod.LayerViolation(
            f"unknown layer {layer!r} — expected one of "
            f"{', '.join(layers_mod.LAYERS)}"
        )
    return layer


def _query_layer(query: dict[str, list[str]]) -> str:
    return _as_layer(query.get("layer", [None])[0])


def _voice_from(data: dict[str, Any]) -> dict[str, Any] | None:
    """The voice fields a request actually expressed, or None when it expressed
    none and the stored voice should stand. Deliberately partial: asking for
    present tense must not reset the person or the focal character."""
    patch: dict[str, Any] = {}
    person = str(data.get("person") or "").strip().lower()
    tense = str(data.get("tense") or "").strip().lower()
    if person:
        if person not in manuscript_mod.PERSONS:
            raise ValueError(
                f"unknown person {person!r} — expected one of "
                f"{', '.join(manuscript_mod.PERSONS)}"
            )
        patch["person"] = person
    if tense:
        if tense not in manuscript_mod.TENSES:
            raise ValueError(
                f"unknown tense {tense!r} — expected one of "
                f"{', '.join(manuscript_mod.TENSES)}"
            )
        patch["tense"] = tense
    if "focal" in data:
        patch["focal"] = str(data.get("focal") or "").strip()
    return patch or None


def _layer_for_op(body: dict, op: str) -> str:
    """The layer a transform request names, defaulting to the log because these
    operators are log-layer verbs and the CLI never had a layer to pass. The
    check is what keeps a client that offered the wrong verb from landing it:
    `LayerViolation` for never, `OpUnavailable` for not yet."""
    layer = _as_layer(body.get("layer") or layers_mod.LOG)
    layers_mod.check_runs(op, layer)
    return layer


def _ids_from_body(body: dict) -> list[int]:
    """A GUI selection arrives as from/to, or as an explicit id list."""
    ids: list[int] = []
    if body.get("msg_id") is not None:
        ids.append(int(body["msg_id"]))
    for raw in body.get("msg_ids") or []:
        ids.append(int(raw))
    lo, hi = body.get("from"), body.get("to")
    if lo is not None or hi is not None:
        if lo is None or hi is None:
            raise ValueError("range needs both from and to")
        if int(hi) < int(lo):
            raise ValueError(f"to {hi} is before from {lo}")
        ids.extend(range(int(lo), int(hi) + 1))
    return sorted(set(ids))


def _span_from_body(body: dict, log_path: Path) -> transform_mod.SpanResult:
    log = loader.load(log_path)
    return transform_mod.resolve_span(
        log,
        beat=body.get("beat"),
        scene=body.get("scene"),
        msg_from=body.get("from"),
        msg_to=body.get("to"),
        speaker=body.get("speaker"),
        role=body.get("role"),
    )


def _status_payload() -> dict[str, Any]:
    log_path = _log_path()
    msg_count = 0
    if log_path.exists():
        try:
            msg_count = len(loader.load(log_path))
        except (json.JSONDecodeError, OSError):
            pass
    pending_edits = transform_mod.load_pending_edits()
    pending_kind: str | None = None
    if pending_edits is not None and pending_edits.edits:
        pending_kind = "edits"
    hist_count = len(history_mod.load_all(log_path=log_path))
    try:
        sweep_ctx = propagate_mod.load_sweep_context()
    except (json.JSONDecodeError, OSError):
        sweep_ctx = None
    available, sync_info = _sync_and_list_models()
    model_pub = config.model_settings_public()
    model_pub["available"] = available
    model_pub["sync"] = {
        "synced": bool(sync_info.get("synced")),
        "reason": sync_info.get("reason"),
        "previous": sync_info.get("previous"),
    }
    return {
        "api_version": API_VERSION,
        "home_dir": str(config.HOME_DIR),
        "log": str(log_path.resolve()),
        "message_count": msg_count,
        "pending_edits": len(pending_edits.edits) if pending_edits else 0,
        "pending_operator": pending_edits.operator if pending_edits else None,
        "pending_kind": pending_kind,
        "pending_interlude": False,
        "edit_set": _edit_set_payload(pending_edits),
        "history_entries": hist_count,
        "history_path": str(config.EDIT_HISTORY),
        # Sweep reads the last commit rather than a selection, so a client has to
        # know whether there is a commit to read before it offers the verb.
        "sweep_ready": sweep_ctx is not None,
        "sweep_note": sweep_ctx.note if sweep_ctx else None,
        "sweep_from": (sweep_ctx.changed_to + 1) if sweep_ctx else None,
        "model_reachable": bool(available) or _model_reachable(),
        "model_url": config.MODEL_BASE_URL,
        "model_provider": getattr(config, "MODEL_PROVIDER", "local"),
        "model_api_key_set": bool((config.MODEL_API_KEY or "").strip()),
        "model_default": config.MODEL_NAME,
        "model_available": available,
        "model_presets": {
            "creative": config.MODEL_PRESET_CREATIVE,
            "analyst": config.MODEL_PRESET_ANALYST,
        },
        "model": model_pub,
        # Author Studio / alt-home: surface experiment banner + seed presence.
        "author_studio": bool(
            getattr(config, "IS_ALT_HOME", False)
            or (Path(config.HOME_DIR) / "mini_spine.json").exists()
        ),
        "mini_spine": str(getattr(config, "MINI_SPINE", Path(config.HOME_DIR) / "mini_spine.json")),
    }


def _proposal_payload() -> dict[str, Any]:
    """Whatever is waiting for a verdict, in one shape.

    Rendered through `transform.edit_diff`; the client dispatches on `kind`
    when it comes time to accept, which keeps the diff rules in one place.
    """
    edits = transform_mod.load_pending_edits()
    if edits is None:
        return {"kind": None, "edits": []}
    payload = transform_mod.edit_set_diff(edits)
    return {
        "kind": "edits",
        "layer": edits.layer,
        "op": transform_mod.operator_base(edits.operator),
        # An empty set is not a failure: the model read the direction and left the
        # prose alone. The pane says so rather than showing an empty diff.
        "advisory": not edits.edits,
        **payload,
    }


# --------------------------------------------------------------------------- #
# Derived layers (novel editor)
# --------------------------------------------------------------------------- #


def _scene_payload(
    scene: manuscript_mod.Scene,
    *,
    layer: str,
    log: loader.Log,
) -> dict[str, Any]:
    """One scene, plus the provenance a reviewer needs to judge it."""
    drift = manuscript_mod.scene_drift(scene, log)
    start, end = scene.anchor.resolve(log)
    voice = scene.voice
    return {
        "id": scene.id,
        "layer": layer,
        "status": scene.status,
        "title": scene.title,
        "voice": voice.to_json() if voice else None,
        "voice_label": voice.describe() if voice else "",
        "source": {
            "from_uid": scene.anchor.from_uid,
            "to_uid": scene.anchor.to_uid,
            "start": start,
            "end": end,
        },
        "drift": drift.to_json(),
        "pinned": bool(getattr(scene, "pinned", False)),
        "generated": scene.generated,
        "model": scene.model,
        "notes": scene.notes,
        "blocks": [b.to_json() for b in scene.blocks],
        "text": scene.text(),
        "words": len(scene.text().split()),
    }


def _log_scene_payload(
    scene: manuscript_mod.Scene, log: loader.Log
) -> dict[str, Any]:
    """The log span a derived scene came from — the left-hand side of the diff."""
    start, end = scene.anchor.resolve(log)
    messages = manuscript_mod.source_messages(log, scene.anchor)
    return {
        "id": scene.id,
        "layer": layers_mod.LOG,
        "source": {
            "from_uid": scene.anchor.from_uid,
            "to_uid": scene.anchor.to_uid,
            "start": start,
            "end": end,
        },
        "messages": [
            {
                "msg_id": m.msg_id,
                "uid": m.uid,
                "speaker": m.speaker,
                "role": m.role(),
                "text": m.text,
            }
            for m in messages
        ],
        "text": manuscript_mod.source_text(log, scene.anchor),
    }


def _manuscript_payload(layer: str, log: loader.Log) -> dict[str, Any]:
    doc = manuscript_mod.load(layer, log=log)
    drifts = manuscript_mod.drift(doc, log)
    orchestration_total = 0
    orchestration_pending = 0
    if layer == layers_mod.MANUSCRIPT:
        units = novelize_mod.orchestration_units(log)
        orchestration_total = len(units)
        orchestration_pending = sum(1 for unit in units if doc.covering(unit.start) is None)
    return {
        "layer": doc.layer,
        "log": doc.log,
        "updated": doc.updated,
        "stats": doc.stats(),
        "drifted": sum(1 for d in drifts if d.dirty),
        "orchestration_total": orchestration_total,
        "orchestration_pending": orchestration_pending,
        "voice": doc.voice.to_json(),
        "voice_label": doc.voice.describe(),
        "persons": manuscript_mod.PERSONS,
        "tenses": manuscript_mod.TENSES,
        "scenes": [
            {
                "id": s.id,
                "title": s.title,
                "status": s.status,
                "start": s.anchor.start,
                "end": s.anchor.end,
                "blocks": len(s.blocks),
                "words": len(s.text().split()),
                "drift": d.to_json(),
                "pinned": bool(getattr(s, "pinned", False)),
                "voice": (s.voice or doc.voice).to_json(),
            }
            for s, d in zip(doc.scenes, drifts)
        ],
    }


def _serve_font(handler: BaseHTTPRequestHandler, name: str) -> None:
    resolved = fonts_mod.font_path(name)
    if resolved is None:
        _json_response(handler, 404, {"error": f"no such font file: {name}"})
        return
    body = resolved.read_bytes()
    ctype = {
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
    }.get(resolved.suffix.lower(), "application/octet-stream")
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Cache-Control", "public, max-age=604800")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


# --------------------------------------------------------------------------- #
# Static bundle
# --------------------------------------------------------------------------- #

_STATIC_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
}


def _serve_static(handler: BaseHTTPRequestHandler, rel: str) -> None:
    """Serve the built UI from ``config.APP_DIR``.

    Unknown paths fall back to ``index.html`` so client-side routing survives a
    page reload — except for asset requests, which must 404 honestly rather than
    hand a stray script tag a page of HTML.
    """
    root = Path(config.APP_DIR).resolve()
    if not root.exists():
        _json_response(handler, 404, {
            "error": "UI bundle not built",
            "expected": str(root),
            "hint": "cd app && npm install && npm run build",
        })
        return

    target = (root / rel).resolve() if rel else root / "index.html"
    if not str(target).startswith(str(root)):
        _json_response(handler, 403, {"error": "path escapes the app directory"})
        return
    if target.is_dir():
        target = target / "index.html"
    if not target.is_file():
        if Path(rel).suffix:
            _json_response(handler, 404, {"error": f"no such asset: {rel}"})
            return
        target = root / "index.html"
        if not target.is_file():
            _json_response(handler, 404, {"error": "index.html missing from bundle"})
            return

    body = target.read_bytes()
    ctype = _STATIC_TYPES.get(
        target.suffix, mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    )
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(body)))
        # Hashed asset names make long caching safe; index.html must not stick.
        if target.name == "index.html":
            handler.send_header("Cache-Control", "no-store")
        else:
            handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        return


def _canon_check_pending() -> dict[str, Any]:
    edits = transform_mod.load_pending_edits()
    if edits is None:
        return {"ok": False, "error": "no pending edits"}
    if not edits.edits:
        return {"ok": True, "empty": True, "results": []}

    results: list[dict[str, Any]] = []
    any_concerns = False
    for edit in edits.edits:
        char = canon_layer.get_character(edit.speaker)
        if char is None:
            results.append({
                "msg_id": edit.msg_id,
                "speaker": edit.speaker,
                "verdict": "skipped",
                "reason": "no bible on record",
            })
            continue
        result = canon_layer.check_in_character(
            before=edit.before,
            after=edit.after,
            speaker=edit.speaker,
            char=char,
        )
        verdict = result.get("verdict", "unknown")
        if verdict in ("concerns", "out_of_character"):
            any_concerns = True
        results.append({
            "msg_id": edit.msg_id,
            "speaker": edit.speaker,
            "verdict": verdict,
            "reason": result.get("reason", ""),
            "specific_issues": result.get("specific_issues", []),
        })
    return {"ok": True, "any_concerns": any_concerns, "results": results}


def _proofread_summary() -> dict[str, Any]:
    log_path = _log_path()
    lore_paths = [p for p in config.LOREBOOK_PATHS if Path(p).exists()]
    reports = proofread_mod.proofread_pending(log_path, lore_paths=lore_paths)
    n_contradicts = sum(len(r.contradicts) for r in reports)
    n_plausible = sum(len(r.plausible) for r in reports)
    n_supported = sum(len(r.supported) for r in reports)
    n_clean = sum(1 for r in reports if r.verdict == "clean")
    return {
        "ok": True,
        "clean": n_clean,
        "supported": n_supported,
        "plausible": n_plausible,
        "contradicts": n_contradicts,
        "reports": [
            {
                "edit_msg_id": r.edit_msg_id,
                "edit_speaker": r.edit_speaker,
                "edit_kind": r.edit_kind,
                "verdict": r.verdict,
                "contradicts": [c.to_json() for c in r.contradicts],
                "plausible": [c.to_json() for c in r.plausible],
                "supported": [c.to_json() for c in r.supported],
            }
            for r in reports
        ],
    }


# --------------------------------------------------------------------------- #
# Projects: the registry, switching, bundles
# --------------------------------------------------------------------------- #

# Write requests (POST/PUT) being served right now. A project switch restarts
# the process, so it waits until it is the only one.
_INFLIGHT = 0
_INFLIGHT_LOCK = threading.Lock()


class _Inflight:
    def __enter__(self):
        global _INFLIGHT
        with _INFLIGHT_LOCK:
            _INFLIGHT += 1

    def __exit__(self, *exc):
        global _INFLIGHT
        with _INFLIGHT_LOCK:
            _INFLIGHT -= 1


def _switch_refusal() -> str | None:
    """Why the open project can't be switched away from right now, or None."""
    if drive_sync.job().get("running"):
        return "a Drive sync is running; switch when it finishes"
    with _INFLIGHT_LOCK:
        others = _INFLIGHT - 1  # the switch request itself
    if others > 0:
        return f"{others} other request(s) are still running (a proposal, novelize, export…); try again when they finish"
    return None


def _project_payload() -> dict[str, Any]:
    from . import registry as registry_mod

    current = {
        "id": config.PROJECT_ID, "title": config.PROJECT_TITLE,
        "home": str(config.HOME_DIR), "source": config.HOME_SOURCE,
        "warning": config.REGISTRY_WARNING,
    }
    try:
        reg = registry_mod.load()
        error = ""
    except registry_mod.RegistryError as exc:
        reg, error = registry_mod.Registry(), str(exc)
    projects = [{**e.to_json(), "exists": e.home.is_dir(), "open": e.home == config.HOME_DIR}
                for e in reg.projects]
    return {"current": current, "active": reg.active, "projects": projects,
            "registry": str(registry_mod.path()), "registry_error": error,
            "projects_dir": str(registry_mod.projects_dir()),
            "env_override": bool(os.environ.get("STORY_EDITOR_HOME"))}


def _restart_into_registry() -> None:
    """Restart so config resolves the registry's active project. An explicit
    STORY_EDITOR_HOME / _LOG would win over the switch, so the new process
    starts without them."""
    for var in ("STORY_EDITOR_HOME", "STORY_EDITOR_LOG"):
        os.environ.pop(var, None)
    code_sync.restart_soon()


class StoryEditorHandler(BaseHTTPRequestHandler):
    server_version = "StoryEditorAPI/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        # Quieter than default; override for debug.
        pass

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path == "/health":
                _json_response(self, 200, {"ok": True, "version": API_VERSION})
            elif path == "/status":
                _json_response(self, 200, _status_payload())
            elif path == "/projects":
                _json_response(self, 200, _project_payload())
            elif path == "/model":
                available, sync_info = _sync_and_list_models()
                _json_response(self, 200, {
                    **config.model_settings_public(),
                    "reachable": bool(available) or _model_reachable(),
                    "available": available,
                    "sync": {
                        "synced": bool(sync_info.get("synced")),
                        "reason": sync_info.get("reason"),
                        "previous": sync_info.get("previous"),
                    },
                })
            elif path == "/edits":
                _json_response(self, 200, {
                    "edit_set": _edit_set_payload(transform_mod.load_pending_edits()),
                })
            elif path == "/proposal":
                _json_response(self, 200, _proposal_payload())
            elif path == "/prose-review/summary":
                _json_response(self, 200, prose_review_mod.summary())
            elif path == "/prose-review":
                scene_id = query.get("scene_id", [""])[0]
                status = query.get("status", ["pending"])[0]
                if status not in (*prose_review_mod.STATUSES, "all"):
                    _json_response(self, 400, {
                        "error": "status must be pending, committed, rejected, stale, or all",
                    })
                    return
                proposals = prose_review_mod.list_view(
                    scene_id=scene_id, status=status,
                )
                _json_response(self, 200, {
                    "proposals": proposals,
                    "count": len(proposals),
                })
            elif path == "/history":
                limit = int(query.get("limit", ["20"])[0])
                entries = history_mod.list_entries(
                    log_path=_log_path(), limit=limit,
                )
                _json_response(self, 200, {
                    "entries": [e.to_json() for e in entries],
                    "path": str(config.EDIT_HISTORY),
                })
            elif path.startswith("/history/"):
                try:
                    entry_id = int(path.split("/")[-1])
                except ValueError:
                    _json_response(self, 400, {"error": "invalid history id"})
                    return
                entry = history_mod.get_entry(entry_id, log_path=_log_path())
                if entry is None:
                    _json_response(self, 404, {"error": f"no entry #{entry_id}"})
                    return
                _json_response(self, 200, entry.to_json())
            elif path == "/sync/export":
                log_path = _log_path()
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                _json_response(self, 200, st_sync.export_st_chat(log_path))
            elif path == "/sync/status":
                _json_response(self, 200, st_sync.chat_status(_log_path()))
            elif path == "/drive/status":
                _json_response(self, 200, drive_sync.status().to_json())
            elif path == "/drive/job":
                _json_response(self, 200, drive_sync.job())
            elif path == "/code/status":
                _json_response(self, 200, code_sync.status().to_json())
            elif path == "/sweep/last/report":
                report_path = config.LAST_SWEEP_REPORT
                if not report_path.exists():
                    _json_response(self, 404, {"error": "no sweep report yet"})
                    return
                _json_response(self, 200, {
                    "path": str(report_path),
                    "markdown": report_path.read_text(encoding="utf-8"),
                })
            elif path == "/attribution/stats":
                log_path = Path(query.get("log", [_log_path()])[0])
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                attr = _attribution()
                turns = _log_turns(log_path)
                store = attr.load_store()
                min_conf = float(query.get("min_confidence", [str(REVIEW_MIN_CONFIDENCE)])[0])
                needs = sum(
                    1 for t in turns
                    if attr.needs_human_review(
                        attr.get_label(store, int(t["msg_id"])),
                        min_confidence=min_conf,
                    )
                )
                _json_response(self, 200, {
                    "ok": True,
                    "needs_human": needs,
                    "labeled": len(store.get("labels") or {}),
                    "messages": len(turns),
                    "summary": attr.format_stats(store, turns),
                })
            elif path == "/attribution/review":
                log_path = Path(query.get("log", [_log_path()])[0])
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                min_conf = float(query.get("min_confidence", [str(REVIEW_MIN_CONFIDENCE)])[0])
                rf = _attribution().review_filter_from_query(query)
                _json_response(
                    self,
                    200,
                    _attribution_review_payload(
                        log_path, min_confidence=min_conf, review_filter=rf,
                    ),
                )
            elif path == "/structure/spine":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                log = loader.load(log_path)
                _json_response(self, 200, structure_mod.spine_navigation_payload(log))
            elif path == "/structure/locate":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                log = loader.load(log_path)
                speaker = (query.get("speaker", [""])[0] or "").strip() or None
                if query.get("msg_id"):
                    try:
                        msg_id = int(query["msg_id"][0])
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "invalid msg_id"})
                        return
                    loc = structure_mod.locate_message(
                        log, msg_id, speaker_filter=speaker,
                    )
                    _json_response(self, 200, {"ok": True, "location": loc.to_json()})
                    return
                if query.get("beat") and query.get("pos"):
                    try:
                        beat_id = int(query["beat"][0])
                        position = int(query["pos"][0])
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "invalid beat or pos"})
                        return
                    try:
                        loc = structure_mod.resolve_beat_position(
                            log, beat_id, position, speaker=speaker,
                        )
                    except ValueError as exc:
                        _json_response(self, 400, {"error": str(exc)})
                        return
                    _json_response(self, 200, {"ok": True, "location": loc.to_json()})
                    return
                _json_response(
                    self,
                    400,
                    {"error": "provide msg_id=N or beat=N&pos=P (1-based within beat)"},
                )
            elif path == "/layers":
                _json_response(self, 200, layers_mod.describe())
            elif path == "/weed/sources":
                from . import weed as weed_mod
                log_path = _query_log_path(query)
                log = loader.load(log_path)
                _json_response(self, 200, {
                    "ok": True,
                    "sources": weed_mod.source_catalog(log),
                    "selected_sources": weed_mod.load_selected_sources(log),
                })
            elif path == "/weed":
                from . import weed as weed_mod
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                try:
                    msg_from = int(query["from"][0]) if query.get("from") else None
                    msg_to = int(query["to"][0]) if query.get("to") else None
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "from and to must be integers"})
                    return
                speaker = (query.get("speaker") or [None])[0] or None
                sources = query.get("source") or None
                report = weed_mod.scan_log(
                    log_path, msg_from=msg_from, msg_to=msg_to, speaker=speaker,
                    source_keys=sources,
                )
                _json_response(self, 200, {"ok": True, **report.to_json()})
            elif path == "/stamps":
                from . import stamps as stamps_mod
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                try:
                    msg_from = int(query["from"][0]) if query.get("from") else None
                    msg_to = int(query["to"][0]) if query.get("to") else None
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "from and to must be integers"})
                    return
                persist = (query.get("persist") or ["1"])[0] not in ("0", "false", "no")
                report = stamps_mod.scan_log(
                    log_path, msg_from=msg_from, msg_to=msg_to, persist=persist,
                )
                _json_response(self, 200, {"ok": True, **report.to_json()})
            elif path == "/manuscript":
                layer = _query_layer(query)
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                _json_response(
                    self, 200, _manuscript_payload(layer, loader.load(log_path)),
                )
            elif path == "/export/chapters":
                _json_response(self, 200, {
                    "chapters": publication_mod.chapters(),
                })
            elif path.startswith("/scene/"):
                scene_id = path[len("/scene/"):]
                layer = _query_layer(query)
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                log = loader.load(log_path)
                # A scene id is a handle on all three layers: the derived scene
                # lives in one document, and `layer=log` returns the source it
                # was made from. Same id, three views — that is what the diff
                # pane compares.
                home = (
                    layers_mod.MANUSCRIPT if layer == layers_mod.LOG else layer
                )
                doc = manuscript_mod.load(home, log=log)
                scene = doc.by_id(scene_id)
                if scene is None:
                    _json_response(self, 404, {
                        "error": f"no scene {scene_id} in the {home} layer",
                    })
                    return
                if layer == layers_mod.LOG:
                    _json_response(self, 200, _log_scene_payload(scene, log))
                    return
                _json_response(self, 200, _scene_payload(
                    scene, layer=layer, log=log,
                ))
            elif path == "/spine":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                _json_response(self, 200, views_mod.spine_view(loader.load(log_path)))
            elif path == "/ask/options":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                _json_response(
                    self, 200, ask_mod.as_of_options(loader.load(log_path)),
                )
            elif path == "/scenes":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                _json_response(self, 200, views_mod.scenes_view(loader.load(log_path)))
            elif path == "/log/span":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                try:
                    start = int(query.get("from", ["0"])[0])
                    end = int(query.get("to", [str(start + 20)])[0])
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "from/to must be integers"})
                    return
                _json_response(
                    self, 200, views_mod.log_span_view(loader.load(log_path), start, end),
                )
            elif path == "/log/by-voice":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                catalog = _attribution().voice_catalog(loader.load(log_path))
                want = (query.get("voice", [""])[0] or "").strip().lower()
                if want:
                    match = next((row for row in catalog if row["voice"] == want), None)
                    ids = list(match["ids"]) if match else []
                    _json_response(self, 200, {
                        "ok": True, "voice": want, "ids": ids, "count": len(ids), "voices": catalog,
                    })
                    return
                _json_response(self, 200, {"ok": True, "voices": catalog})
            elif path == "/novelize/plan":
                log_path = _query_log_path(query)
                if not log_path.exists():
                    _json_response(self, 404, {"error": f"log not found: {log_path}"})
                    return
                try:
                    start = int(query.get("from", ["0"])[0])
                    end = int(query.get("to", [str(start)])[0])
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "from/to must be integers"})
                    return
                max_span = None
                if query.get("max_span_chars"):
                    try:
                        max_span = int(query["max_span_chars"][0])
                    except (TypeError, ValueError):
                        _json_response(self, 400, {
                            "error": "max_span_chars must be an integer",
                        })
                        return
                try:
                    payload = novelize_mod.plan(
                        loader.load(log_path), start, end,
                        override=_voice_from({k: v[0] for k, v in query.items()}),
                        max_span_chars=max_span,
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, payload)
            elif path == "/fonts":
                _json_response(self, 200, fonts_mod.registry())
            elif path.startswith("/fonts/file/"):
                _serve_font(self, path[len("/fonts/file/"):])
            elif path == "/play":
                from . import play as play_mod

                try:
                    _json_response(self, 200, play_mod.view())
                except play_mod.PlayError as exc:
                    _json_response(self, 400, {"error": str(exc)})
            elif path == "/cast":
                from . import cast as cast_mod

                _json_response(self, 200, cast_mod.roster())
            elif path.startswith("/cast/"):
                from . import cast as cast_mod

                key = unquote(path[len("/cast/"):].strip("/"))
                found = cast_mod.member(key)
                if found is None:
                    _json_response(self, 404, {"error": f"no character {key!r}"})
                    return
                _json_response(self, 200, found)
            elif path == "/dossiers":
                chars = dossier_mod.list_characters()
                _json_response(self, 200, {
                    "characters": chars,
                    "dossiers": [
                        (dossier_mod.load(c).to_json() if dossier_mod.load(c) else None)
                        for c in chars
                    ],
                })
            elif path.startswith("/dossier/"):
                name = unquote(path[len("/dossier/"):].strip("/"))
                if not name:
                    _json_response(self, 400, {"error": "character required"})
                    return
                d = dossier_mod.load(name)
                if d is None:
                    _json_response(self, 404, {"error": f"no dossier for {name}"})
                    return
                as_of_raw = query.get("as_of", [None])[0]
                payload = d.to_json()
                payload["has_portrait"] = dossier_mod.has_portrait(name)
                payload["portrait_selection"] = dossier_mod.portrait_selection(name)
                if as_of_raw is not None:
                    try:
                        mid = int(as_of_raw)
                    except ValueError:
                        _json_response(self, 400, {"error": "as_of must be int"})
                        return
                    payload["as_of"] = mid
                    payload["projection"] = [
                        e.to_json() for e in dossier_mod.as_of(d, mid)
                    ]
                    payload["brief"] = dossier_mod.project_brief(d, mid)
                _json_response(self, 200, payload)
            elif path == "/portraits":
                _json_response(self, 200, {
                    "portraits": dossier_mod.list_portrait_catalog(),
                })
            elif path.startswith("/portrait-asset"):
                # /portrait-asset?ref=st:Wren.png — preview any catalog face
                ref = (query.get("ref", [None])[0] or "").strip()
                if not ref and path.startswith("/portrait-asset/"):
                    ref = unquote(path[len("/portrait-asset/"):].strip("/"))
                p = dossier_mod.resolve_portrait_ref(ref)
                if p is None:
                    _json_response(self, 404, {"error": "no portrait", "ref": ref})
                    return
                data = p.read_bytes()
                ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                # Refs are immutable filenames; safe to cache. Character portraits
                # below must not be — the same URL swaps when a pin changes.
                self.send_header("Cache-Control", "private, max-age=3600")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            elif path.startswith("/portrait/"):
                name = unquote(path[len("/portrait/"):].strip("/"))
                p = dossier_mod.resolve_portrait_path(name)
                if p is None:
                    _json_response(self, 404, {"error": "no portrait", "character": name})
                    return
                data = p.read_bytes()
                ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            elif path == "/author/seed":
                try:
                    _json_response(self, 200, author_mod.seed_view())
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
            elif path == "/author/seed/template":
                try:
                    _json_response(self, 200, author_mod.seed_template())
                except ValueError as exc:
                    _json_response(self, 500, {"error": str(exc)})
                    return
            elif path == "/author/story":
                try:
                    payload = author_mod.build_author_story(
                        log=loader.load(_log_path()),
                    )
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                _json_response(self, 200, payload)
            elif path == "/author/spark":
                try:
                    spine = author_mod.load_seed()
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                from .author import seed as seed_mod
                progress = seed_mod.load_progress()
                completed = set(progress.get("completed") or [])
                label = query.get("beat", [None])[0]
                beat = spine.beat_by_label(label) if label else spine.next_open(completed)
                if beat is None:
                    _json_response(self, 400, {"error": "no open beat"})
                    return
                first = (query.get("first", [None])[0] or query.get("first_speaker", [None])[0])
                note = author_mod.compile_spark(
                    beat,
                    spine=spine,
                    log=loader.load(_log_path()),
                    first_speaker=first,
                )
                _json_response(self, 200, {"beat": beat.to_json(), "spark": note.to_json()})
            elif path == "/author/spark/template":
                try:
                    spine = author_mod.load_seed()
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                from .author import seed as seed_mod
                progress = seed_mod.load_progress()
                completed = set(progress.get("completed") or [])
                label = query.get("beat", [None])[0]
                beat = spine.beat_by_label(label) if label else spine.next_open(completed)
                if beat is None:
                    _json_response(self, 400, {"error": "no open beat"})
                    return
                first = (query.get("first", [None])[0] or query.get("first_speaker", [None])[0])
                log = loader.load(_log_path())
                as_of = len(log) - 1 if len(log) else 0
                note = author_mod.manual_spark_template(
                    beat, first_speaker=first, as_of_msg=as_of,
                )
                draft = author_mod.load_spark_draft()
                draft_for_beat = (
                    draft
                    if draft and str(draft.get("beat_label") or "") == beat.label
                    else None
                )
                _json_response(self, 200, {
                    "beat": beat.to_json(),
                    "spark": note.to_json(),
                    "draft": draft_for_beat,
                })
            elif path == "/author/spark/draft":
                draft = author_mod.load_spark_draft()
                _json_response(self, 200, {"draft": draft})
            elif path == "/author/run":
                run = author_mod.load_run()
                if run is None:
                    try:
                        run = author_mod.ensure_open_run()
                    except FileNotFoundError as exc:
                        _json_response(self, 404, {"error": str(exc)})
                        return
                _json_response(self, 200, {"run": run})
            elif path == "/author/trajectory":
                try:
                    records = author_mod.build_trajectory()
                except ValueError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                fmt = (query.get("format", ["json"])[0] or "json").lower()
                if fmt == "jsonl":
                    text = author_mod.trajectory_jsonl()
                    data = text.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                _json_response(self, 200, {
                    "records": records,
                    "jsonl": author_mod.trajectory_jsonl(),
                })
            elif path == "/app" or path.startswith("/app/"):
                _serve_static(self, path[len("/app/"):] if len(path) > 4 else "")
            else:
                _json_response(self, 404, {"error": f"unknown path: {path}"})
        except layers_mod.OpUnavailable as exc:
            _json_response(self, 501, {"error": str(exc)})
        except layers_mod.LayerViolation as exc:
            _json_response(self, 409, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                _json_response(self, 500, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_PUT(self) -> None:  # noqa: N802
        with _Inflight():
            self._handle_put()

    def do_POST(self) -> None:  # noqa: N802
        with _Inflight():
            self._handle_post()

    def _play_post(self, path: str) -> None:
        from . import play as play_mod

        body = _read_json(self) or {}
        try:
            if path == "/play/turn":
                move = str(body.get("move") or "").strip()
                if not move:
                    _json_response(self, 400, {"error": "write a move first"})
                    return
                result = play_mod.turn(move, debug=bool(body.get("debug")))
            elif path == "/play/begin":
                result = play_mod.turn(None, debug=bool(body.get("debug")))
            elif path == "/play/undo":
                result = play_mod.undo()
            elif path == "/play/as":
                result = play_mod.switch_pov(str(body.get("char") or ""))
            elif path == "/play/renarrate":
                result = play_mod.renarrate(debug=bool(body.get("debug")))
            elif path == "/play/reroll":
                result = play_mod.reroll(debug=bool(body.get("debug")))
            elif path == "/play/swipe":
                try:
                    index = int(body.get("index"))
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "index must be a number"})
                    return
                result = play_mod.choose_swipe(index)
            else:
                result = play_mod.sync()
        except play_mod.PlayBusy as exc:
            _json_response(self, 409, {"error": str(exc)})
            return
        except play_mod.PlayError as exc:
            _json_response(self, 400, {"error": str(exc)})
            return
        _json_response(self, 200, {"ok": True, **result, "view": play_mod.view()})

    def _project_post(self, path: str) -> None:
        from . import bundle as bundle_mod, registry as registry_mod

        if path == "/project/upload":  # the body is the .sebundle itself
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                _json_response(self, 400, {"error": "choose a .sebundle file to load"})
                return
            try:
                staged = bundle_mod.stage_upload(self.rfile, length, registry_mod.projects_dir())
            except bundle_mod.BundleError as exc:
                _json_response(self, 400, {"error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, **staged})
            return

        body = _read_json(self) or {}
        if path == "/project/load":
            try:
                loaded = bundle_mod.load_upload(str(body.get("token") or ""), str(body.get("id") or "").strip(),
                                                registry_mod.projects_dir())
            except (bundle_mod.BundleError, registry_mod.RegistryError, ValueError) as exc:
                _json_response(self, 400, {"error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "loaded": loaded.to_json(), **_project_payload()})
            return
        if path == "/project/upload/discard":
            bundle_mod.discard_upload(str(body.get("token") or ""), registry_mod.projects_dir())
            _json_response(self, 200, {"ok": True})
            return
        if path == "/project/add":
            try:
                entry = registry_mod.add(str(body.get("home") or ""))
            except (registry_mod.RegistryError, ValueError) as exc:
                _json_response(self, 400, {"error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "project": entry.to_json(), **_project_payload()})
            return
        if path == "/project/save":
            out = Path(tempfile.mkdtemp(prefix="story-editor-bundle-"))
            try:
                bundle = bundle_mod.save(config.HOME_DIR, out,
                                         with_backups=bool(body.get("with_backups")),
                                         with_index=bool(body.get("with_index")))
                report = bundle_mod.verify(bundle)
                if not report.ok:
                    _json_response(self, 500, {"error": "; ".join(report.errors)})
                    return
                _file_response(self, bundle, content_type="application/gzip", download_name=bundle.name)
            except bundle_mod.BundleError as exc:
                _json_response(self, 409, {"error": str(exc)})
            finally:
                shutil.rmtree(out, ignore_errors=True)
            return
        # /project/switch
        target = str(body.get("id") or "")
        refusal = _switch_refusal()
        if refusal:
            _json_response(self, 409, {"error": refusal})
            return
        try:
            # Keep a way back: the project being left joins the list if it isn't there.
            reg = registry_mod.load()
            if reg.by_home(config.HOME_DIR) is None and reg.get(config.PROJECT_ID) is None:
                registry_mod.add(config.HOME_DIR)
            entry = registry_mod.switch(target)
        except (registry_mod.RegistryError, ValueError) as exc:
            _json_response(self, 400, {"error": str(exc)})
            return
        _json_response(self, 200, {"ok": True, "restarting": True, "project": entry.to_json()})
        _restart_into_registry()

    def _handle_put(self) -> None:
        """Hand edits to a derived layer.

        There is no PUT for the log: roleplay turns are rewritten through the
        transform operators, which keep a reviewable edit set and a backup. The
        manuscript is prose the author owns outright, so it takes direct edits —
        `layers.check` is what keeps those two facts from blurring.
        """
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        try:
            if path.startswith("/scene/"):
                scene_id = path[len("/scene/"):]
                body = _read_json(self)
                layer = _as_layer(body.get("layer"))
                layers_mod.check("edit", layer)
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layer, log=log)
                scene = doc.by_id(scene_id)
                if scene is None:
                    _json_response(self, 404, {
                        "error": f"no scene {scene_id} in the {layer} layer",
                    })
                    return

                touched = 0
                before_blocks = scene.block_texts()
                if isinstance(body.get("blocks"), list):
                    for patch in body["blocks"]:
                        if not isinstance(patch, dict):
                            continue
                        block = scene.block(str(patch.get("id") or ""))
                        if block is None:
                            continue
                        if "text" in patch:
                            block.text = str(patch.get("text") or "")
                            block.edited = True
                            touched += 1
                        if patch.get("kind") in manuscript_mod.BLOCK_KINDS:
                            block.kind = str(patch["kind"])
                        if "note" in patch:
                            block.note = str(patch.get("note") or "")
                elif isinstance(body.get("text"), str):
                    # Whole-scene replacement: re-split into blocks, keeping the
                    # ids of untouched paragraphs so queued proposals still land,
                    # and the old provenance so the rewrite stays traceable.
                    src = scene.source_uids()
                    before = {b.id: b.text for b in scene.blocks}
                    scene.blocks = manuscript_mod.reconcile_blocks(
                        scene.blocks, body["text"], src=src,
                    )
                    touched = sum(
                        1 for b in scene.blocks if before.get(b.id) != b.text
                    )
                else:
                    _json_response(self, 400, {
                        "error": "provide blocks:[{id,text}] or text:'…'",
                    })
                    return

                if "title" in body:
                    scene.title = str(body.get("title") or "")
                if "notes" in body:
                    scene.notes = str(body.get("notes") or "")
                hand_edits = history_mod.block_edits(before_blocks, scene.block_texts())
                doc_path = manuscript_mod.path_for(layer)
                backup = (
                    prose_review_mod.backup_manuscript(doc_path, f"manual-{scene_id}")
                    if hand_edits and doc_path.exists() else None
                )
                manuscript_mod.save(doc)
                if backup is not None:
                    history_mod.record_manuscript_change(
                        log.path,
                        operator=history_mod.MANUALLY_EDITED,
                        layer=layer,
                        scene_id=scene.id,
                        scene_title=scene.title,
                        span=scene.log_span(),
                        note=f"{len(hand_edits)} paragraph(s) edited by hand",
                        locator=f"scene {scene.id}",
                        backup=backup,
                        edits=hand_edits,
                    )
                autosave = None
                if layer == layers_mod.MANUSCRIPT:
                    autosave = novelize_mod.write_private_text_autosave(doc, log)
                payload = _scene_payload(
                    scene, layer=layer, log=log,
                )
                payload["blocks_touched"] = touched
                if autosave is not None:
                    payload["autosave"] = str(autosave)
                _json_response(self, 200, payload)
            elif path == "/author/seed":
                body = _read_json(self) or {}
                # Accept either the spine object or { spine: {...} }.
                data = body.get("spine") if isinstance(body.get("spine"), dict) else body
                try:
                    view = author_mod.save_seed_and_sync_progress(data)
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, **view})
            else:
                _json_response(self, 404, {"error": f"unknown path: {path}"})
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "invalid JSON body"})
        except layers_mod.OpUnavailable as exc:
            _json_response(self, 501, {"error": str(exc)})
        except layers_mod.LayerViolation as exc:
            _json_response(self, 409, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                _json_response(self, 500, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                return

    def _handle_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path in ("/play/turn", "/play/begin", "/play/undo", "/play/as", "/play/sync",
                        "/play/renarrate", "/play/reroll", "/play/swipe"):
                self._play_post(path)
                return
            if path in ("/project/switch", "/project/add", "/project/save",
                        "/project/upload", "/project/load", "/project/upload/discard"):
                self._project_post(path)
                return
            if path == "/prose-review/compression":
                body = _read_json(self) or {}
                scene_id = body.get("scene_id")
                if not isinstance(scene_id, str) or not scene_id:
                    _json_response(self, 400, {"error": "scene_id is required"})
                    return
                from . import llm as llm_mod
                try:
                    if body.get("stream"):
                        log = loader.load(_body_log_path(body))
                        if manuscript_mod.load(log=log).by_id(scene_id) is None:
                            raise KeyError(scene_id)
                        _stream_propose(
                            self,
                            lambda emit: prose_review_mod.propose_compression(scene_id, log=log, on_stream=emit),
                            done_builder=lambda result: {"compression": result},
                        )
                        return
                    result = prose_review_mod.propose_compression(
                        scene_id, log=loader.load(_body_log_path(body)),
                    )
                except KeyError:
                    _json_response(self, 404, {"error": f"no manuscript scene {scene_id}"})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, **result})
                return

            if path == "/prose-review/rhythm":
                body = _read_json(self) or {}
                scene_id = body.get("scene_id")
                if not isinstance(scene_id, str) or not scene_id:
                    _json_response(self, 400, {"error": "scene_id is required"})
                    return
                from . import llm as llm_mod
                try:
                    if body.get("stream"):
                        log = loader.load(_body_log_path(body))
                        if manuscript_mod.load(log=log).by_id(scene_id) is None:
                            raise KeyError(scene_id)
                        _stream_propose(
                            self,
                            lambda emit: prose_review_mod.propose_rhythm(scene_id, log=log, on_stream=emit),
                            done_builder=lambda result: {"rhythm": result},
                        )
                        return
                    result = prose_review_mod.propose_rhythm(
                        scene_id, log=loader.load(_body_log_path(body)),
                    )
                except KeyError:
                    _json_response(self, 404, {"error": f"no manuscript scene {scene_id}"})
                    return
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, **result})
                return

            if path.startswith("/prose-review/"):
                parts = path.split("/")
                if len(parts) != 4 or parts[3] not in ("commit", "reject"):
                    _json_response(self, 404, {"error": f"unknown path: {path}"})
                    return
                proposal_id, action = parts[2], parts[3]
                if action == "reject":
                    _read_json(self)  # consume the POST body before responding
                    try:
                        proposal = prose_review_mod.reject(proposal_id)
                    except KeyError:
                        _json_response(self, 404, {"error": f"no prose proposal {proposal_id}"})
                        return
                    _json_response(self, 200, {
                        "ok": True,
                        "proposal": proposal.view(),
                    })
                    return

                body = _read_json(self) or {}
                log = loader.load(_body_log_path(body))
                try:
                    proposal, scene, backup, autosave = prose_review_mod.commit(
                        proposal_id, log=log, replacements=body.get("replacements"),
                    )
                except KeyError:
                    _json_response(self, 404, {"error": f"no prose proposal {proposal_id}"})
                    return
                except prose_review_mod.StaleProposal as exc:
                    _json_response(self, 409, {"error": str(exc), "stale": True})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "proposal": proposal.view(),
                    "scene": _scene_payload(
                        scene, layer=layers_mod.MANUSCRIPT, log=log,
                    ),
                    "backup": str(backup),
                    "autosave": str(autosave),
                })
                return

            if path == "/export/pdf":
                body = _read_json(self) or {}
                raw_ids = body.get("chapter_ids")
                if not isinstance(raw_ids, list):
                    _json_response(self, 400, {"error": "chapter_ids must be a list"})
                    return
                try:
                    chapter_ids = {int(value) for value in raw_ids}
                    pdf = publication_mod.export_pdf(chapter_ids)
                except (TypeError, ValueError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _file_response(
                    self, pdf, content_type="application/pdf", download_name=pdf.name,
                )
                return

            if path in ("/export/txt", "/export/ao3"):
                body = _read_json(self) or {}
                raw_ids = body.get("chapter_ids")
                if not isinstance(raw_ids, list):
                    _json_response(self, 400, {"error": "chapter_ids must be a list"})
                    return
                try:
                    chapter_ids = {int(value) for value in raw_ids}
                    exporter = (
                        publication_mod.export_ao3 if path == "/export/ao3"
                        else publication_mod.export_txt
                    )
                    exported = exporter(chapter_ids)
                except (TypeError, ValueError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _file_response(
                    self,
                    exported,
                    content_type={
                        ".zip": "application/zip",
                        ".html": "text/html; charset=utf-8",
                    }.get(exported.suffix, "text/plain; charset=utf-8"),
                    download_name=exported.name,
                )
                return

            if path == "/sync/import":
                body = _read_json(self)
                messages = body.get("messages")
                if not isinstance(messages, list):
                    _json_response(self, 400, {"error": "body.messages must be a list"})
                    return
                log_path = Path(body.get("log") or _log_path())
                try:
                    count = st_sync.import_st_chat(
                        messages, log_path, force=bool(body.get("force"))
                    )
                except st_sync.WorkingLogAhead as exc:
                    _json_response(self, 409, {
                        "error": str(exc),
                        "working_count": exc.working,
                        "incoming_count": exc.incoming,
                    })
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "message_count": count,
                    "log": str(log_path.resolve()),
                })

            elif path in ("/code/update", "/code/push", "/code/restart"):
                body = _read_json(self) or {}
                try:
                    if path == "/code/update":
                        result = code_sync.update()
                    elif path == "/code/push":
                        result = code_sync.push()
                    else:
                        result = {}
                except code_sync.UpdateRefused as exc:
                    _json_response(self, 409, {
                        "error": str(exc), "status": code_sync.status(fetch=False).to_json(),
                    })
                    return
                except code_sync.CodeSyncError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                restart = path == "/code/restart" or (
                    bool(result.get("updated"))
                    and not result.get("dependencies_changed")
                    and body.get("restart", True)
                )
                _json_response(self, 200, {"ok": True, **result, "restarting": restart})
                if restart:
                    code_sync.restart_soon()
            elif path in ("/drive/push", "/drive/pull"):
                body = _read_json(self) or {}
                direction = path.rsplit("/", 1)[1]
                try:
                    started = drive_sync.start(direction, force=bool(body.get("force")))
                except drive_sync.SyncRefused as exc:
                    _json_response(self, 409, {
                        "error": str(exc), "status": drive_sync.status().to_json(),
                    })
                    return
                except drive_sync.DriveError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 202, {"ok": True, "job": started})
            elif path == "/sync/push":
                body = _read_json(self)
                layers_mod.check_syncable(layers_mod.LOG)
                src = Path(body.get("log") or _log_path())
                try:
                    dest = Path(body["to"]) if body.get("to") else st_sync.project_chat()
                except st_sync.NoChatConfigured as exc:
                    _json_response(self, 409, {"error": str(exc)})
                    return
                if not src.exists():
                    _json_response(self, 404, {"error": f"log not found: {src}"})
                    return
                result = st_sync.write_st_chat(src, dest)
                _json_response(self, 200, {"ok": True, **result})

            elif path == "/model":
                body = _read_json(self)
                try:
                    snap = config.apply_model_settings(
                        provider=body.get("provider"),
                        model_name=body.get("model") or body.get("model_name"),
                        api_key=body.get("api_key"),
                        base_url=body.get("base_url") or body.get("url"),
                        local_port=body.get("local_port"),
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    **snap,
                    "reachable": _model_reachable(),
                })

            elif path == "/restyle":
                body = _read_json(self)
                _layer_for_op(body, "restyle")
                note = (body.get("note") or "").strip()
                if not note:
                    _json_response(self, 400, {"error": "note is required"})
                    return
                log_path = _body_log_path(body)
                span = _span_from_body(body, log_path)
                if not span.messages:
                    _json_response(self, 400, {
                        "error": "no messages in scope",
                        "locator": span.locator,
                    })
                    return
                from . import llm as llm_mod
                if body.get("stream"):
                    _stream_propose(
                        self,
                        lambda emit: transform_mod.restyle_span(
                            log_path, span, note,
                            chain=not body.get("independent", False),
                            on_stream=emit,
                        ),
                    )
                    return
                try:
                    edit_set = transform_mod.restyle_span(
                        log_path, span, note,
                        chain=not body.get("independent", False),
                    )
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/retune":
                body = _read_json(self)
                _layer_for_op(body, "retune")
                mode = (body.get("mode") or "").strip().lower()
                if mode not in ("tighten", "expand"):
                    _json_response(self, 400, {"error": "mode must be 'tighten' or 'expand'"})
                    return
                log_path = _body_log_path(body)
                span = _span_from_body(body, log_path)
                if not span.messages:
                    _json_response(self, 400, {
                        "error": "no messages in scope",
                        "locator": span.locator,
                    })
                    return
                from . import llm as llm_mod
                if body.get("stream"):
                    _stream_propose(
                        self,
                        lambda emit: transform_mod.retune_span(
                            log_path, span, mode,
                            note=body.get("note"),
                            chain=not body.get("independent", False),
                            on_stream=emit,
                        ),
                    )
                    return
                try:
                    edit_set = transform_mod.retune_span(
                        log_path, span, mode,
                        note=body.get("note"),
                        chain=not body.get("independent", False),
                    )
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/weed/sources":
                body = _read_json(self)
                from . import weed as weed_mod
                log_path = _body_log_path(body)
                selected = body.get("selected_sources")
                if not isinstance(selected, list):
                    _json_response(self, 400, {"error": "selected_sources must be a list"})
                    return
                log = loader.load(log_path)
                chosen = weed_mod.save_selected_sources(log, selected)
                _json_response(self, 200, {
                    "ok": True,
                    "sources": weed_mod.source_catalog(log),
                    "selected_sources": chosen,
                })
            elif path == "/weed":
                body = _read_json(self)
                _layer_for_op(body, "weed")
                from . import llm as llm_mod
                from . import weed as weed_mod
                log_path = _body_log_path(body)
                sources = body.get("sources")
                if sources is not None and not isinstance(sources, list):
                    _json_response(self, 400, {"error": "sources must be a list"})
                    return
                try:
                    span = _span_from_body(body, log_path)
                except (ValueError, IndexError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                if body.get("stream"):
                    _stream_propose(
                        self,
                        lambda emit: weed_mod.apply_span(
                            log_path, span, on_stream=emit, source_keys=sources,
                        ),
                    )
                    return
                try:
                    edit_set = weed_mod.apply_span(log_path, span, source_keys=sources)
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/copyedit":
                body = _read_json(self)
                _layer_for_op(body, "copyedit")
                from . import llm as llm_mod
                from . import copyedit as copyedit_mod
                log_path = _body_log_path(body)
                try:
                    span = _span_from_body(body, log_path)
                except (ValueError, IndexError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                base_url = body.get("base_url") or None
                if body.get("stream"):
                    _stream_propose(
                        self,
                        lambda emit: copyedit_mod.apply_span(
                            log_path, span, on_stream=emit, base_url=base_url,
                        ),
                    )
                    return
                try:
                    edit_set = copyedit_mod.apply_span(
                        log_path, span, base_url=base_url,
                    )
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/stamps":
                body = _read_json(self)
                _layer_for_op(body, "stamps")
                from . import stamps as stamps_mod
                log_path = _body_log_path(body)
                try:
                    span = _span_from_body(body, log_path)
                except (ValueError, IndexError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                if body.get("stream"):
                    _stream_propose(
                        self,
                        lambda emit: stamps_mod.propose_span(
                            log_path, span, on_stream=emit,
                        ),
                    )
                    return
                edit_set = stamps_mod.propose_span(log_path, span)
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/remove":
                body = _read_json(self)
                _layer_for_op(body, "remove")
                log_path = _body_log_path(body)
                msg_from = body.get("from")
                if msg_from is None:
                    _json_response(self, 400, {"error": "from (msg id) is required"})
                    return
                try:
                    edit_set = transform_mod.propose_remove(
                        log_path,
                        int(msg_from),
                        body.get("to"),
                        sweep=bool(body.get("sweep")),
                    )
                except (ValueError, IndexError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/inject":
                body = _read_json(self)
                _layer_for_op(body, "inject")
                note = (body.get("note") or "").strip()
                speaker = (body.get("speaker") or "").strip()
                if not note:
                    _json_response(self, 400, {"error": "note is required"})
                    return
                if not speaker:
                    _json_response(self, 400, {"error": "speaker is required"})
                    return
                after = body.get("after")
                if after is None:
                    _json_response(self, 400, {"error": "after (msg id) is required"})
                    return
                log_path = _body_log_path(body)
                from . import llm as llm_mod
                if body.get("stream"):
                    _stream_propose(
                        self,
                        lambda emit: transform_mod.inject_at(
                            log_path,
                            int(after),
                            speaker,
                            note,
                            locator=body.get("locator") or f"after msg {after}",
                            attribution_voice=body.get("voice") or body.get("attribution_voice"),
                            attribution_mode=body.get("mode") or body.get("attribution_mode"),
                            on_stream=emit,
                        ),
                    )
                    return
                try:
                    edit_set = transform_mod.inject_at(
                        log_path,
                        int(after),
                        speaker,
                        note,
                        locator=body.get("locator") or f"after msg {after}",
                        attribution_voice=body.get("voice") or body.get("attribution_voice"),
                        attribution_mode=body.get("mode") or body.get("attribution_mode"),
                    )
                except (ValueError, IndexError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/sweep/last":
                body = _read_json(self)
                _layer_for_op(body, "sweep")
                log_path = _body_log_path(body)
                ctx = propagate_mod.load_sweep_context()
                if ctx is None:
                    _json_response(self, 400, {
                        "error": "no sweep context (commit an edit first, or sweep manually via CLI)",
                    })
                    return
                edits = [transform_mod.Edit.from_json(e) for e in ctx.edits]
                from . import llm as llm_mod
                try:
                    report = propagate_mod.sweep(
                        log_path,
                        edits,
                        ctx.operator,
                        ctx.note,
                        sweep_from=ctx.changed_to + 1,
                    )
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "report": report.to_json(),
                    "report_path": str(config.LAST_SWEEP_REPORT),
                })

            elif path == "/log/patch":
                body = _read_json(self)
                _layer_for_op(body, "patch")
                patches = body.get("patches")
                if not isinstance(patches, list) or not patches:
                    _json_response(self, 400, {"error": "patches:[{msg_id, body}] is required"})
                    return
                try:
                    edit_set = transform_mod.propose_hand_edits(
                        _body_log_path(body), patches,
                    )
                except (ValueError, IndexError) as exc:
                    _json_response(self, 409, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "advisory": edit_set.edits == [],
                    "edit_set": edit_set.to_json(),
                })

            elif path == "/edits/discard":
                transform_mod.discard_pending_edits()
                # Clear Author/trajectory "awaiting Accept" so the rail cannot
                # stay stuck with no proposal (and no earnedness UI).
                try:
                    from .author import seed as seed_mod
                    prog = seed_mod.load_progress()
                    changed = False
                    if prog.get("awaiting") or prog.get("pending_gate"):
                        prog.pop("awaiting", None)
                        prog.pop("pending_gate", None)
                        seed_mod.save_progress(prog)
                        changed = True
                    _ = changed
                except Exception:  # noqa: BLE001
                    pass
                _json_response(self, 200, {"ok": True})

            elif path == "/edits/drop":
                body = _read_json(self)
                idx = body.get("index")
                msg_id = body.get("msg_id")
                if idx is not None:
                    dropped = transform_mod.drop_edit(0, index=int(idx))
                    _json_response(self, 200, {"ok": dropped, "index": int(idx)})
                    return
                if msg_id is None:
                    _json_response(self, 400, {"error": "msg_id or index is required"})
                    return
                dropped = transform_mod.drop_edit(int(msg_id))
                _json_response(self, 200, {"ok": dropped, "msg_id": msg_id})

            elif path == "/edits/commit-one":
                body = _read_json(self)
                log_path = _body_log_path(body)
                try:
                    backup_path, applied, remaining = transform_mod.commit_one_edit(
                        log_path,
                        index=int(body.get("index", 0)),
                        allow_flagged=bool(body.get("allow_flagged", False)),
                    )
                except (FileNotFoundError, ValueError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "backup": str(backup_path),
                    "applied": applied,
                    "remaining": remaining,
                })

            elif path == "/edits/commit":
                body = _read_json(self)
                allow_flagged = bool(body.get("allow_flagged", False))
                log_path = _body_log_path(body)
                earnedness = body.get("earnedness")
                if earnedness is not None:
                    try:
                        earnedness = int(earnedness)
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "earnedness must be 1–5"})
                        return
                    if earnedness < 1 or earnedness > 5:
                        _json_response(self, 400, {"error": "earnedness must be 1–5"})
                        return
                earnedness_note = body.get("earnedness_note")
                if earnedness_note is not None:
                    earnedness_note = str(earnedness_note)
                # Capture before commit clears pending — Author Studio needs
                # the beat label to advance the mini-spine cursor.
                pending_before = transform_mod.load_pending_edits()
                # Validate trajectory earnedness before mutating the log.
                if pending_before is not None:
                    try:
                        from .author.seed import load_progress, load_seed
                        from .author.run import load_run as _load_run
                        prog = load_progress()
                        pending_gate = prog.get("pending_gate") or {}
                        label_chk = None
                        if getattr(pending_before, "operator", "") == "author_write_scene":
                            label_chk = author_mod.beat_label_from_edit_set(pending_before)
                        if label_chk and str(pending_gate.get("label") or "") == label_chk:
                            run_chk = _load_run(str(pending_gate.get("run_id") or "") or None)
                            has_dest = bool((run_chk or {}).get("end_state"))
                            if not has_dest:
                                try:
                                    has_dest = bool(load_seed().end_state)
                                except Exception:  # noqa: BLE001
                                    has_dest = False
                            if has_dest and earnedness is None:
                                _json_response(self, 400, {
                                    "error": (
                                        "earnedness (1–5) is required when "
                                        "accepting a trajectory step"
                                    ),
                                })
                                return
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    backup_path, applied = transform_mod.commit_edits(
                        log_path, allow_flagged=allow_flagged,
                    )
                except (FileNotFoundError, ValueError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                beat_committed = None
                beat_commit_error = None
                if pending_before is not None:
                    try:
                        beat_committed = author_mod.on_author_edits_committed(
                            pending_before,
                            backup=backup_path,
                            earnedness=earnedness,
                            earnedness_note=earnedness_note,
                        )
                    except Exception as exc:  # noqa: BLE001 — never block a commit
                        beat_commit_error = str(exc)
                _json_response(self, 200, {
                    "ok": True,
                    "backup": str(backup_path),
                    "applied": applied,
                    "beat_committed": beat_committed,
                    "beat_commit_error": beat_commit_error,
                })

            elif path == "/edits/undo":
                log_path = Path(query.get("log", [_log_path()])[0])
                body = _read_json(self)
                history_id = body.get("history_id")
                from_backup = None
                if history_id is not None:
                    try:
                        requested_id = int(history_id)
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "history_id must be an integer"})
                        return
                    latest = history_mod.get_latest(log_path=log_path)
                    if latest is None or latest.id != requested_id or latest.event != "commit":
                        _json_response(self, 409, {
                            "error": "edit history changed; reopen History before undoing",
                        })
                        return
                    from_backup = latest.backup
                try:
                    restored = transform_mod.undo(log_path, from_backup=from_backup)
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, "restored": str(restored)})

            elif path == "/proofread":
                from . import llm as llm_mod
                try:
                    summary = _proofread_summary()
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, summary)

            elif path == "/canon/check":
                from . import llm as llm_mod
                try:
                    result = _canon_check_pending()
                except llm_mod.ModelError as exc:
                    _json_response(self, 502, {"error": str(exc)})
                    return
                _json_response(self, 200, result)

            elif path == "/attribution/set":
                body = _read_json(self)
                msg_id = body.get("msg_id")
                voice = (body.get("voice") or "").strip().lower()
                mode = (body.get("mode") or "pov").strip().lower()
                if msg_id is None:
                    _json_response(self, 400, {"error": "msg_id is required"})
                    return
                if not voice:
                    _json_response(self, 400, {"error": "voice is required"})
                    return
                attr = _attribution()
                if mode not in attr.VALID_MODES:
                    _json_response(self, 400, {"error": f"invalid mode: {mode}"})
                    return
                log_path = _body_log_path(body)
                turns = _log_turns(log_path)
                turn = next(
                    (t for t in turns if int(t["msg_id"]) == int(msg_id)), None,
                )
                if turn is None:
                    _json_response(self, 404, {"error": f"no message {msg_id} in log"})
                    return
                store = attr.load_store()
                existing = attr.get_label(store, int(msg_id))
                label = attr.VoiceLabel(
                    card=str(turn.get("name") or ""),
                    voice=voice,
                    mode=mode,
                    focal=(
                        str(body.get("focal") or "").strip().lower()
                        if "focal" in body
                        else (existing.focal if existing else "")
                    ),
                    addressees=(
                        [str(x).strip().lower() for x in body.get("addressees", []) if str(x).strip()]
                        if isinstance(body.get("addressees"), list)
                        else (list(existing.addressees) if existing else [])
                    ),
                    source="manual",
                    reviewed=bool(body.get("reviewed", True)),
                    confidence=1.0,
                    notes=str(body.get("notes") or ""),
                    segments=existing.segments if existing else [],
                )
                attr.set_label(store, int(msg_id), label, force=True)
                save_path = attr.save_store(store)
                _json_response(self, 200, {
                    "ok": True,
                    "msg_id": int(msg_id),
                    "label": label.to_dict(),
                    "store": str(save_path.resolve()),
                    "needs_human_remaining": _attribution_review_payload(
                        log_path,
                    )["count"],
                })

            elif path == "/attribution/bulk-set":
                body = _read_json(self)
                voice = (body.get("voice") or "").strip().lower()
                mode = (body.get("mode") or "pov").strip().lower()
                if not voice:
                    _json_response(self, 400, {"error": "voice is required"})
                    return
                attr = _attribution()
                if mode not in attr.VALID_MODES:
                    _json_response(self, 400, {"error": f"invalid mode: {mode}"})
                    return
                log_path = _body_log_path(body)
                turns = _log_turns(log_path)
                store = attr.load_store()
                min_conf = float(body.get("min_confidence", REVIEW_MIN_CONFIDENCE))
                # `voice` and `mode` here are the values being ASSIGNED, but the
                # filter builder reads those same keys as filters on a turn's
                # current voice/mode — which narrows the selection to rows that
                # already hold the target label, i.e. to nothing. Filter on the
                # plural `modes`/`card`/`preset` keys only.
                review_filter = attr.review_filter_from_mapping(
                    {k: v for k, v in body.items() if k not in _BULK_ASSIGNMENT_KEYS}
                )
                msg_ids = body.get("msg_ids")
                if msg_ids is not None:
                    target_ids = [int(x) for x in msg_ids]
                else:
                    payload = attr.build_review_payload(
                        turns,
                        store,
                        min_confidence=min_conf,
                        review_filter=review_filter,
                    )
                    target_ids = [int(i["msg_id"]) for i in payload["items"]]
                if not target_ids:
                    _json_response(self, 200, {
                        "ok": True,
                        "applied": 0,
                        "skipped": 0,
                        "requested": 0,
                        "needs_human_remaining": _attribution_review_payload(
                            log_path,
                            min_confidence=min_conf,
                            review_filter=review_filter,
                        )["count"],
                    })
                    return
                stats = attr.bulk_set_labels(
                    turns,
                    store,
                    target_ids,
                    voice=voice,
                    mode=mode,
                    reviewed=bool(body.get("reviewed", True)),
                    notes=str(body.get("notes") or ""),
                )
                save_path = attr.save_store(store)
                _json_response(self, 200, {
                    "ok": True,
                    **stats,
                    "voice": voice,
                    "mode": mode,
                    "store": str(save_path.resolve()),
                    "needs_human_remaining": _attribution_review_payload(
                        log_path,
                        min_confidence=min_conf,
                        review_filter=review_filter,
                    )["count"],
                })

            elif path == "/attribution/reconcile":
                body = _read_json(self)
                log_path = _body_log_path(body)
                attr = _attribution()
                turns = _log_turns(log_path)
                store = attr.load_store()
                min_conf = float(body.get("min_confidence", REVIEW_MIN_CONFIDENCE))
                skip_propose = bool(body.get("skip_propose", False))
                card = body.get("card")
                review_filter = _attribution().review_filter_from_mapping(body)
                try:
                    stats, human_ids = attr.reconcile_turns(
                        turns,
                        store,
                        min_confidence=min_conf,
                        skip_propose=skip_propose,
                        card_filter=card,
                    )
                except Exception as exc:
                    from . import llm as llm_mod
                    if isinstance(exc, llm_mod.ModelError):
                        _json_response(self, 502, {"error": str(exc)})
                        return
                    raise
                save_path = attr.save_store(store)
                _json_response(self, 200, {
                    "ok": True,
                    "stats": stats,
                    "needs_human": len(human_ids),
                    "store": str(save_path.resolve()),
                    "review": _attribution_review_payload(
                        log_path,
                        min_confidence=min_conf,
                        review_filter=review_filter,
                    ),
                })

            elif path == "/spine/validate":
                body = _read_json(self)
                log_path = _body_log_path(body)
                log = loader.load(log_path)
                derived = structure_mod.load_derived_spine(log)
                if derived is None:
                    _json_response(self, 400, {
                        "error": "no committed derived spine — press derive, "
                                 "then commit (or CLI: structure derive + commit)",
                    })
                    return
                stream = bool(body.get("stream"))

                def _run_audit(emit=None):
                    def on_progress(stage, detail, *, step=0, total=0):
                        if emit is None:
                            return
                        emit({
                            "kind": "phase",
                            "text": detail,
                            "stage": stage,
                            "step": step,
                            "total": total,
                        })

                    audit = structure_mod.audit_spine(
                        derived,
                        db_path=config.index_db_for(log_path),
                        on_progress=on_progress if emit else None,
                    )
                    saved = views_mod.save_alignment(
                        audit["result"], authored_count=len(audit["authored"]),
                    )
                    payload = views_mod.spine_view(log)
                    payload["checked"] = saved["checked"]
                    return payload

                if stream:
                    from . import llm as llm_mod
                    from . import jsonish as jsonish_mod

                    try:
                        _sse_begin(self)

                        def emit(ev: dict) -> None:
                            _sse_emit(self, ev)

                        emit({
                            "kind": "phase",
                            "text": "starting spine audit",
                            "stage": "start",
                            "step": 0,
                            "total": 0,
                        })
                        payload = _run_audit(emit)
                        _sse_emit(self, {
                            "kind": "done",
                            "ok": True,
                            "spine": payload,
                        })
                    except _StreamStopped:
                        return
                    except (ValueError, FileNotFoundError, RuntimeError) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    except (llm_mod.ModelError, jsonish_mod.UnparsableReply) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    return

                try:
                    payload = _run_audit(None)
                except (ValueError, FileNotFoundError, RuntimeError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except Exception as exc:
                    from . import llm as llm_mod
                    if isinstance(exc, llm_mod.ModelError):
                        _json_response(self, 502, {"error": str(exc)})
                        return
                    raise
                _json_response(self, 200, payload)

            elif path == "/spine/derive":
                # Propose a fresh derived spine (pending until /spine/commit).
                body = _read_json(self)
                log_path = _body_log_path(body)
                log = loader.load(log_path)
                stream = bool(body.get("stream"))
                max_beats = int(body.get("max_beats") or 14)

                def _run_derive(emit=None):
                    def on_progress(stage, detail, *, step=0, total=0):
                        if emit is None:
                            return
                        emit({
                            "kind": "phase",
                            "text": detail,
                            "stage": stage,
                            "step": step,
                            "total": total,
                        })

                    proposal = structure_mod.derive_beats(
                        log,
                        max_beats=max_beats,
                        on_progress=on_progress if emit else None,
                    )
                    structure_mod.write_pending_spine(proposal)
                    return views_mod.spine_view(log)

                if stream:
                    from . import llm as llm_mod
                    from . import jsonish as jsonish_mod

                    try:
                        _sse_begin(self)

                        def emit(ev: dict) -> None:
                            _sse_emit(self, ev)

                        emit({
                            "kind": "phase",
                            "text": "starting spine derive",
                            "stage": "start",
                            "step": 0,
                            "total": 0,
                        })
                        payload = _run_derive(emit)
                        _sse_emit(self, {
                            "kind": "done",
                            "ok": True,
                            "spine": payload,
                        })
                    except _StreamStopped:
                        return
                    except (ValueError, FileNotFoundError, RuntimeError) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    except (llm_mod.ModelError, jsonish_mod.UnparsableReply) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    return

                try:
                    payload = _run_derive(None)
                except (ValueError, FileNotFoundError, RuntimeError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except Exception as exc:
                    from . import llm as llm_mod
                    if isinstance(exc, llm_mod.ModelError):
                        _json_response(self, 502, {"error": str(exc)})
                        return
                    raise
                _json_response(self, 200, payload)

            elif path == "/spine/commit":
                body = _read_json(self)
                log_path = _body_log_path(body)
                log = loader.load(log_path)
                try:
                    structure_mod.commit_spine()
                except FileNotFoundError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, views_mod.spine_view(log))

            elif path == "/spine/discard":
                body = _read_json(self)
                log_path = _body_log_path(body)
                log = loader.load(log_path)
                structure_mod.discard_pending_spine()
                _json_response(self, 200, views_mod.spine_view(log))

            elif path == "/ask":
                body = _read_json(self)
                log_path = _body_log_path(body)
                log = loader.load(log_path)
                question = str(body.get("question") or "").strip()
                if not question:
                    _json_response(self, 400, {"error": "question is required"})
                    return
                as_of = body.get("as_of")
                mode = str(body.get("mode") or "fused")
                stream = bool(body.get("stream"))

                def _run_ask(emit=None):
                    def on_progress(stage, detail):
                        if emit is None:
                            return
                        emit({
                            "kind": "phase",
                            "text": detail,
                            "stage": stage,
                        })

                    return ask_mod.ask(
                        question,
                        log=log,
                        log_path=log_path,
                        as_of=as_of,
                        mode=mode,
                        on_progress=on_progress if emit else None,
                    ).to_json()

                if stream:
                    from . import llm as llm_mod
                    from . import jsonish as jsonish_mod

                    try:
                        _sse_begin(self)

                        def emit(ev: dict) -> None:
                            _sse_emit(self, ev)

                        emit({
                            "kind": "phase",
                            "text": "starting ask",
                            "stage": "start",
                        })
                        payload = _run_ask(emit)
                        _sse_emit(self, {
                            "kind": "done",
                            "ok": True,
                            "ask": payload,
                        })
                    except _StreamStopped:
                        return
                    except (ValueError, FileNotFoundError) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    except (llm_mod.ModelError, jsonish_mod.UnparsableReply) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    return

                try:
                    payload = _run_ask(None)
                except (ValueError, FileNotFoundError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except Exception as exc:
                    from . import llm as llm_mod
                    if isinstance(exc, llm_mod.ModelError):
                        _json_response(self, 502, {"error": str(exc)})
                        return
                    raise
                _json_response(self, 200, payload)

            elif path == "/fonts/role":
                body = _read_json(self)
                role = str(body.get("role") or "")
                try:
                    cfg = fonts_mod.set_role(role, body)
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, "fonts": fonts_mod.registry()})

            elif path == "/fonts/system":
                body = _read_json(self)
                try:
                    face = fonts_mod.add_system_face(
                        str(body.get("family") or ""),
                        licence=str(body.get("licence") or ""),
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True, "face": face.to_json(), "fonts": fonts_mod.registry(),
                })

            elif path == "/fonts/remove":
                body = _read_json(self)
                removed = fonts_mod.remove_face(str(body.get("family") or ""))
                _json_response(self, 200 if removed else 404, {
                    "ok": removed, "fonts": fonts_mod.registry(),
                })

            elif path == "/fonts/import":
                # Raw body, not JSON: the payload is a font file. Metadata rides
                # on the query string so the body stays exactly the bytes.
                name = query.get("name", [""])[0]
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length) if length > 0 else b""
                try:
                    face = fonts_mod.import_file(
                        name,
                        data,
                        family=query.get("family", [""])[0],
                        licence=query.get("licence", [""])[0],
                        style=query.get("style", ["normal"])[0],
                        weight=query.get("weight", ["400"])[0],
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True, "face": face.to_json(), "fonts": fonts_mod.registry(),
                })

            elif path.startswith("/scene/") and path.endswith("/status"):
                scene_id = path[len("/scene/"):-len("/status")]
                body = _read_json(self)
                layer = _as_layer(body.get("layer"))
                status = str(body.get("status") or "").strip().lower()
                if status not in manuscript_mod.STATUSES:
                    _json_response(self, 400, {
                        "error": f"status must be one of "
                                 f"{', '.join(manuscript_mod.STATUSES)}",
                    })
                    return
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layer, log=log)
                scene = doc.by_id(scene_id)
                if scene is None:
                    _json_response(self, 404, {"error": f"no scene {scene_id}"})
                    return
                scene.status = status
                manuscript_mod.save(doc)
                _json_response(self, 200, _scene_payload(
                    scene, layer=layer, log=log,
                ))

            elif path == "/novelize":
                body = _read_json(self)
                try:
                    start = int(body.get("from"))
                    end = int(body.get("to", body.get("from")))
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "from/to must be integers"})
                    return
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)
                existing = doc.covering(start)
                if existing is not None and not body.get("regenerate"):
                    # A second pass over a scene the author already has is almost
                    # always an accident; asking for it explicitly is cheap.
                    _json_response(self, 409, {
                        "error": f"messages {start}..{end} are already novelized as "
                                 f"{existing.id} ({existing.status}). Pass "
                                 f"regenerate=true to write a new take over it.",
                        "scene": existing.id,
                        "status": existing.status,
                    })
                    return
                stream = bool(body.get("stream"))
                dry_run = bool(body.get("dry_run"))
                max_span = body.get("max_span_chars")
                if max_span is not None and max_span != "":
                    try:
                        max_span = int(max_span)
                    except (TypeError, ValueError):
                        _json_response(self, 400, {
                            "error": "max_span_chars must be an integer",
                        })
                        return
                else:
                    max_span = None
                max_retries = body.get("max_retries", 2)
                try:
                    max_retries = max(0, min(10, int(max_retries)))
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "max_retries must be an integer"})
                    return
                continuity_chars = body.get("continuity_chars", 3000)
                try:
                    continuity_chars = max(0, min(12000, int(continuity_chars)))
                except (TypeError, ValueError):
                    _json_response(self, 400, {"error": "continuity_chars must be an integer"})
                    return

                def _run_novelize(emit=None):
                    def on_progress(stage, detail, *, step=0, total=0):
                        if emit is None:
                            return
                        emit({
                            "kind": "phase",
                            "text": detail,
                            "stage": stage,
                            "step": step,
                            "total": total,
                        })

                    proposal = novelize_mod.propose(
                        log, start, end,
                        doc=doc,
                        voice=_voice_from(body),
                        direction=str(body.get("direction") or ""),
                        model=body.get("model") or None,
                        on_progress=on_progress if emit else None,
                        max_span_chars=max_span,
                    )
                    if dry_run:
                        return {
                            "ok": True,
                            "saved": False,
                            **proposal.to_json(),
                        }
                    novelize_mod.commit(proposal, doc=doc)
                    return {
                        "ok": True,
                        "saved": True,
                        **proposal.to_json(),
                        "scene": _scene_payload(
                            proposal.scene, layer=layers_mod.MANUSCRIPT, log=log,
                        ),
                    }

                if stream:
                    from . import llm as llm_mod
                    from . import jsonish as jsonish_mod

                    try:
                        _sse_begin(self)

                        def emit(ev: dict) -> None:
                            _sse_emit(self, ev)

                        emit({
                            "kind": "phase",
                            "text": "starting novelize",
                            "stage": "start",
                            "step": 0,
                            "total": 0,
                        })
                        payload = _run_novelize(emit)
                        _sse_emit(self, {
                            "kind": "done",
                            "ok": True,
                            "novelize": payload,
                        })
                    except _StreamStopped:
                        return
                    except novelize_mod.TooLong as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    except (ValueError, FileNotFoundError) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    except (llm_mod.ModelError, jsonish_mod.UnparsableReply) as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    return

                try:
                    payload = _run_novelize(None)
                except novelize_mod.TooLong as exc:
                    _json_response(self, 413, {"error": str(exc)})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except Exception as exc:
                    from . import llm as llm_mod
                    if isinstance(exc, llm_mod.ModelError):
                        _json_response(self, 502, {"error": str(exc)})
                        return
                    raise
                _json_response(self, 200, payload)

            elif path == "/novelize/batch":
                body = _read_json(self)
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)
                stream = bool(body.get("stream", True))
                max_scenes = body.get("max_scenes")
                if max_scenes is not None and max_scenes != "":
                    try:
                        max_scenes = int(max_scenes)
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "max_scenes must be an integer"})
                        return
                else:
                    max_scenes = None
                max_span = body.get("max_span_chars")
                if max_span is not None and max_span != "":
                    try:
                        max_span = int(max_span)
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "max_span_chars must be an integer"})
                        return
                else:
                    max_span = None

                def _run_batch(emit=None):
                    def on_progress(stage, detail, *, step=0, total=0):
                        if emit is None:
                            return
                        emit({
                            "kind": "phase",
                            "text": detail,
                            "stage": stage,
                            "step": step,
                            "total": total,
                        })

                    batch = novelize_mod.run_batch(
                        log,
                        doc=doc,
                        voice=_voice_from(body),
                        model=body.get("model") or None,
                        max_scenes=max_scenes,
                        max_span_chars=max_span,
                        regenerate=bool(body.get("regenerate")),
                        stop_on_error=bool(body.get("stop_on_error")),
                        max_retries=max_retries,
                        continuity_chars=continuity_chars,
                        on_progress=on_progress if emit else None,
                    )
                    return {"ok": True, **batch.to_json()}

                if stream:
                    try:
                        _sse_begin(self)

                        def emit(ev: dict) -> None:
                            _sse_emit(self, ev)

                        emit({
                            "kind": "phase",
                            "text": "starting batch novelize",
                            "stage": "batch",
                            "step": 0,
                            "total": 0,
                        })
                        payload = _run_batch(emit)
                        _sse_emit(self, {"kind": "done", "ok": True, "batch": payload})
                    except _StreamStopped:
                        return
                    except Exception as exc:
                        try:
                            _sse_emit(self, {"kind": "error", "error": str(exc)})
                        except _StreamStopped:
                            pass
                    return

                try:
                    payload = _run_batch(None)
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                except Exception as exc:
                    from . import llm as llm_mod
                    if isinstance(exc, llm_mod.ModelError):
                        _json_response(self, 502, {"error": str(exc)})
                        return
                    raise
                _json_response(self, 200, payload)

            elif path == "/manuscript/voice":
                # The book's default voice. Scenes already written keep the voice
                # they were written in — changing the default is a decision about
                # what comes next, not a retroactive rewrite.
                body = _read_json(self)
                patch = _voice_from(body)
                if patch is None:
                    _json_response(self, 400, {
                        "error": "give at least one of person, tense, focal",
                    })
                    return
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)
                doc.voice = doc.voice.merged(patch)
                manuscript_mod.save(doc)
                _json_response(self, 200, {
                    "ok": True,
                    "voice": doc.voice.to_json(),
                    "voice_label": doc.voice.describe(),
                })

            elif path.startswith("/scene/") and path.endswith("/voice"):
                # One scene departing from the book's voice. Recorded, not
                # applied: the prose changes when the scene is regenerated.
                scene_id = path[len("/scene/"):-len("/voice")]
                body = _read_json(self)
                patch = _voice_from(body)
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layers_mod.MANUSCRIPT, log=log)
                scene = doc.by_id(scene_id)
                if scene is None:
                    _json_response(self, 404, {"error": f"no scene {scene_id}"})
                    return
                if patch is None and body.get("follow_book"):
                    scene.voice = None
                elif patch is None:
                    _json_response(self, 400, {
                        "error": "give at least one of person, tense, focal, "
                                 "or follow_book=true",
                    })
                    return
                else:
                    scene.voice = (scene.voice or doc.voice).merged(patch)
                manuscript_mod.save(doc)
                _json_response(self, 200, _scene_payload(
                    scene, layer=layers_mod.MANUSCRIPT, log=log,
                ))

            elif path.startswith("/scene/") and path.endswith("/rebase"):
                scene_id = path[len("/scene/"):-len("/rebase")]
                body = _read_json(self)
                layer = _as_layer(body.get("layer"))
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layer, log=log)
                scene = doc.by_id(scene_id)
                if scene is None:
                    _json_response(self, 404, {"error": f"no scene {scene_id}"})
                    return
                # Accept the source as it stands now, without touching the prose:
                # "I have read the change and this paragraph still holds."
                manuscript_mod.rebase(scene, log)
                manuscript_mod.save(doc)
                _json_response(self, 200, _scene_payload(scene, layer=layer, log=log))

            elif path.startswith("/scene/") and path.endswith("/pin"):
                scene_id = path[len("/scene/"):-len("/pin")]
                body = _read_json(self)
                layer = _as_layer(body.get("layer") or layers_mod.MANUSCRIPT)
                log = loader.load(_body_log_path(body))
                doc = manuscript_mod.load(layer, log=log)
                scene = doc.by_id(scene_id)
                if scene is None:
                    _json_response(self, 404, {"error": f"no scene {scene_id}"})
                    return
                if body.get("unpin"):
                    manuscript_mod.unpin(scene)
                else:
                    manuscript_mod.pin(scene)
                manuscript_mod.save(doc)
                _json_response(self, 200, _scene_payload(scene, layer=layer, log=log))

            elif path.startswith("/dossier/") and path.endswith("/portrait-upload"):
                name = unquote(path[len("/dossier/"):-len("/portrait-upload")].strip("/"))
                body = _read_json(self) or {}
                filename = str(body.get("filename") or "portrait.png")
                b64 = str(body.get("data_base64") or body.get("data") or "")
                if "," in b64 and b64.strip().startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                try:
                    raw = base64.b64decode(b64, validate=False)
                except Exception:
                    _json_response(self, 400, {"error": "data_base64 is not valid base64"})
                    return
                try:
                    d = dossier_mod.import_portrait_file(name, filename, raw)
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                payload = d.to_json()
                payload["has_portrait"] = dossier_mod.has_portrait(name)
                payload["portrait_selection"] = dossier_mod.portrait_selection(name)
                _json_response(self, 200, {
                    "ok": True,
                    "dossier": payload,
                    "portraits": dossier_mod.list_portrait_catalog(),
                })

            elif path.startswith("/dossier/") and path.endswith("/portrait"):
                name = unquote(path[len("/dossier/"):-len("/portrait")].strip("/"))
                body = _read_json(self) or {}
                ref = body.get("portrait", body.get("ref"))
                if ref is not None and not isinstance(ref, str):
                    _json_response(self, 400, {"error": "portrait must be a string or null"})
                    return
                try:
                    d = dossier_mod.set_portrait(name, ref)
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                payload = d.to_json()
                payload["has_portrait"] = dossier_mod.has_portrait(name)
                payload["portrait_selection"] = dossier_mod.portrait_selection(name)
                _json_response(self, 200, {"ok": True, "dossier": payload})

            elif path.startswith("/dossier/") and path.endswith("/entry"):
                name = unquote(path[len("/dossier/"):-len("/entry")].strip("/"))
                body = _read_json(self) or {}
                d = dossier_mod.load(name)
                if d is None:
                    d = dossier_mod.Dossier(character=name, label=name)
                try:
                    entry = dossier_mod.add_entry(
                        d,
                        kind=str(body.get("kind") or "state"),
                        text=str(body.get("text") or ""),
                        from_msg=body.get("from"),
                        to_msg=body.get("to"),
                        source=str(body.get("source") or "hand"),
                        log=loader.load(_log_path()),
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                dossier_mod.save(d)
                _json_response(self, 200, {"ok": True, "entry": entry.to_json(),
                                           "dossier": d.to_json()})

            elif path.startswith("/dossier/") and path.endswith("/close"):
                name = path[len("/dossier/"):-len("/close")].strip("/")
                body = _read_json(self) or {}
                d = dossier_mod.load(name)
                if d is None:
                    _json_response(self, 404, {"error": f"no dossier for {name}"})
                    return
                try:
                    item = dossier_mod.close_open_item(
                        d,
                        str(body.get("item_id") or ""),
                        resolution=str(body.get("resolution") or ""),
                    )
                except (KeyError, ValueError) as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                dossier_mod.save(d)
                _json_response(self, 200, {"ok": True, "item": item.to_json()})

            elif path.startswith("/dossier/") and path.endswith("/remove-entry"):
                name = path[len("/dossier/"):-len("/remove-entry")].strip("/")
                body = _read_json(self) or {}
                d = dossier_mod.load(name)
                if d is None:
                    _json_response(self, 404, {"error": f"no dossier for {name}"})
                    return
                entry_id = str(body.get("entry_id") or "").strip()
                if not entry_id:
                    _json_response(self, 400, {"error": "entry_id required"})
                    return
                try:
                    removed = dossier_mod.remove_entry(d, entry_id)
                except KeyError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                dossier_mod.save(d)
                _json_response(self, 200, {
                    "ok": True,
                    "removed": removed.to_json(),
                    "dossier": d.to_json(),
                })

            elif path == "/dossier/put-on-file":
                body = _read_json(self) or {}
                character = str(body.get("character") or "").strip()
                passage = str(body.get("passage") or "").strip()
                kind = str(body.get("kind") or "state")
                if not character or not passage:
                    _json_response(self, 400, {"error": "character and passage required"})
                    return
                text = dossier_mod.summarize_passage_to_entry(
                    character=character, passage=passage, kind=kind,
                )
                d = dossier_mod.load(character) or dossier_mod.Dossier(
                    character=character, label=character,
                )
                entry = dossier_mod.add_entry(
                    d,
                    kind=kind,
                    text=text,
                    from_msg=body.get("from"),
                    to_msg=body.get("to"),
                    source="put_on_file",
                    log=loader.load(_log_path()),
                )
                dossier_mod.save(d)
                _json_response(self, 200, {"ok": True, "entry": entry.to_json(),
                                           "dossier": d.to_json()})

            elif path == "/author/advance":
                body = _read_json(self) or {}
                max_retries = int(body.get("max_retries") or 3)
                skip_canon = bool(body.get("skip_canon_llm", False))
                spark_text = body.get("spark_text")
                if spark_text is not None:
                    spark_text = str(spark_text)
                use_draft = bool(body.get("use_draft", True))
                beat_label = str(body.get("beat") or body.get("beat_label") or "").strip() or None
                first_speaker = str(
                    body.get("first_speaker") or body.get("first") or ""
                ).strip() or None

                def work(emit):
                    return author_mod.advance_beat(
                        max_retries=max_retries,
                        skip_canon_llm=skip_canon,
                        spark_text=spark_text,
                        use_draft=use_draft,
                        beat_label=beat_label,
                        first_speaker=first_speaker,
                        on_progress=emit,
                    )

                _stream_propose(
                    self,
                    work,
                    done_builder=lambda r: {"result": r.to_json()},
                )

            elif path == "/author/spark/draft":
                body = _read_json(self) or {}
                label = str(body.get("beat_label") or body.get("beat") or "").strip()
                text = str(body.get("text") or "")
                if not label:
                    _json_response(self, 400, {"error": "beat_label required"})
                    return
                try:
                    path_out = author_mod.save_spark_draft(beat_label=label, text=text)
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "path": str(path_out),
                    "draft": author_mod.load_spark_draft(),
                })

            elif path == "/author/spark/draft/clear":
                cleared = author_mod.clear_spark_draft()
                _json_response(self, 200, {"ok": True, "cleared": cleared})

            elif path == "/author/mark-committed":
                body = _read_json(self) or {}
                label = str(body.get("label") or "").strip()
                if not label:
                    _json_response(self, 400, {"error": "label required"})
                    return
                backup = str(body.get("backup") or "").strip() or None
                earnedness = body.get("earnedness")
                if earnedness is not None:
                    try:
                        earnedness = int(earnedness)
                    except (TypeError, ValueError):
                        _json_response(self, 400, {"error": "earnedness must be 1–5"})
                        return
                earnedness_note = body.get("earnedness_note")
                if earnedness_note is not None:
                    earnedness_note = str(earnedness_note)
                try:
                    progress = author_mod.mark_beat_committed(
                        label,
                        backup=backup,
                        earnedness=earnedness,
                        earnedness_note=earnedness_note,
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {
                    "ok": True,
                    "label": label,
                    "progress": progress,
                })

            elif path == "/author/seed/load":
                body = _read_json(self) or {}
                keep = bool(body.get("keep_progress", False))
                json_body = body.get("json") if isinstance(body.get("json"), dict) else None
                # Allow posting the spine object at the top level.
                if json_body is None and isinstance(body.get("beats"), list):
                    json_body = body
                path_in = str(body.get("path") or "").strip() or None
                try:
                    view = author_mod.load_external_seed(
                        json_body=json_body,
                        path=path_in,
                        keep_progress=keep,
                    )
                except FileNotFoundError as exc:
                    _json_response(self, 404, {"error": str(exc)})
                    return
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, **view})

            elif path == "/author/undo":
                body = _read_json(self) or {}
                label = str(body.get("label") or body.get("beat") or "").strip() or None
                restore_log = bool(body.get("restore_log", True))
                try:
                    result = author_mod.undo_author_commit(
                        label, restore_log=restore_log,
                    )
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, result)

            elif path == "/author/redo":
                try:
                    result = author_mod.redo_author_commit()
                except ValueError as exc:
                    _json_response(self, 400, {"error": str(exc)})
                    return
                _json_response(self, 200, result)

            else:
                _json_response(self, 404, {"error": f"unknown path: {path}"})
        except json.JSONDecodeError:
            _json_response(self, 400, {"error": "invalid JSON body"})
        except layers_mod.OpUnavailable as exc:
            _json_response(self, 501, {"error": str(exc)})
        except layers_mod.LayerViolation as exc:
            _json_response(self, 409, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            try:
                _json_response(self, 500, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError):
                return


def _start_empty_play_log() -> None:
    """A project that plays from a play folder gets its working log from play.
    Until the first turn there is none, so start an empty one: the editor then
    opens a new played project like any other, with nothing in it yet."""
    log = config.working_log()
    if getattr(config, "PLAY_HOME", None) is None or log.exists():
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"chat_metadata": {"player_mode": True}}) + "\n", encoding="utf-8")


def serve(host: str | None = None, port: int | None = None) -> None:
    """Start the API server (blocks until interrupted)."""
    host = host or config.API_HOST
    port = port or config.API_PORT
    _start_empty_play_log()
    httpd = ThreadingHTTPServer((host, port), StoryEditorHandler)
    print(f"Story Editor API listening on http://{host}:{port}")
    print(f"  home: {config.HOME_DIR}")
    print(f"  log:  {_log_path().resolve()}")
    if (config.APP_DIR / "index.html").exists():
        print(f"  app:  http://{host}:{port}/app")
    else:
        print("  app:  not built — run `npm install && npm run build` in app/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
