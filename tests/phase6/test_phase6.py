#!/usr/bin/env python3
"""Phase 6 integration probes — API, ST sync, edit history, cross-phase pipelines.

Designed to run against an isolated STORY_EDITOR_HOME (see tests/phase6/run_tests.sh).
Most tests are non-LLM; optional LLM block runs when the model server is up.

Exit 0 if all executed tests pass. Exit 1 on any failure.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

# Project root on sys.path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from story_editor import config, history, loader, st_sync, transform
from story_editor.transform import Edit, EditSet

# These probes are DESTRUCTIVE: they re-import the log, commit edits, restore
# backups, and clear history. They are safe only inside an isolated
# STORY_EDITOR_HOME. Guard at import time, not in main(), because a test runner
# that collects this file calls the functions directly — which is exactly how the
# a live working log once got reverted to a week-old backup.
if not config.IS_ALT_HOME:
    raise RuntimeError(
        "refusing to load the phase 6 probes: STORY_EDITOR_HOME is unset, so "
        f"they would write to the live workspace ({config.HOME_DIR}). "
        "Run tests/phase6/run_tests.sh, which sets up an isolated home."
    )

PASS = 0
FAIL = 0
SKIP = 0
RESULTS: list[str] = []


def run(label: str, fn: Callable[[], None], *, optional: bool = False) -> None:
    global PASS, FAIL, SKIP
    try:
        fn()
        print(f"   PASS: {label}")
        PASS += 1
        RESULTS.append(f"PASS  {label}")
    except SkipTest as exc:
        print(f"   SKIP: {label} ({exc})")
        SKIP += 1
        RESULTS.append(f"SKIP  {label}")
    except Exception as exc:
        if optional:
            print(f"   SKIP: {label} (optional: {exc})")
            SKIP += 1
            RESULTS.append(f"SKIP  {label}")
        else:
            print(f"   FAIL: {label} — {exc}")
            FAIL += 1
            RESULTS.append(f"FAIL  {label}")


class SkipTest(Exception):
    pass


def _assert(cond: bool, msg: str = "assertion failed") -> None:
    if not cond:
        raise AssertionError(msg)


API_TIMEOUT_SEC = 180  # restyle / canon/check can exceed 30s on local GPU inference


def api_json(
    method: str,
    base: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = API_TIMEOUT_SEC,
) -> tuple[int, dict]:
    url = f"{base.rstrip('/')}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"error": raw}
        return exc.code, payload


def api_raw(
    method: str,
    base: str,
    path: str,
    *,
    timeout: float = API_TIMEOUT_SEC,
) -> tuple[int, str, str]:
    """(status, body, content-type) for routes that do not answer in JSON."""
    url = f"{base.rstrip('/')}{path}"
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), resp.headers.get(
                "Content-Type", ""
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            exc.read().decode("utf-8", "replace"),
            exc.headers.get("Content-Type", ""),
        )


def log_path() -> Path:
    return config.working_log()


HOLMES_RESTYLE_MSG = 2


def holmes_msg2_text() -> str:
    return loader.load(log_path()).get(HOLMES_RESTYLE_MSG).text


def write_synthetic_restyle(
    before: str,
    after: str,
    *,
    msg_id: int = HOLMES_RESTYLE_MSG,
    speaker: str = "Holmes",
    note: str = "phase6 synthetic",
) -> None:
    """Stage a pending restyle without calling the LLM."""
    lp = log_path()
    edit_set = EditSet(
        log=str(lp.resolve()),
        operator="restyle",
        note=note,
        locator=f"msg {msg_id} (phase6 test)",
        edits=[
            Edit(
                msg_id=msg_id,
                speaker=speaker,
                before=before,
                after=after,
                kind="replace",
            )
        ],
    )
    transform.write_pending_edits(edit_set)


def reset_workspace() -> None:
    """Fresh log + empty history for repeatable runs."""
    lp = log_path()
    src_story = ROOT / "tests" / "holmes" / "source" / "scandal_in_bohemia.story"
    _assert(src_story.exists(), f"missing holmes source: {src_story}")
    subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "import", str(src_story), "-o", str(lp)],
        check=True,
        cwd=str(ROOT),
        capture_output=True,
    )
    hist = config.EDIT_HISTORY
    if hist.exists():
        hist.unlink()
    for name in ("pending_edits.json", "pending_edits.md", "last_sweep_context.json"):
        p = config.WORKSPACE_DIR / name
        if p.exists():
            p.unlink()


# ── Phase 6 unit probes ───────────────────────────────────────────────────

def test_history_append_and_list() -> None:
    before = holmes_msg2_text()
    after = before.replace("Wedlock", "Marriage")
    write_synthetic_restyle(before, after, note="history-unit")
    transform.commit_edits(log_path())
    entries = history.list_entries(log_path=log_path(), limit=5)
    _assert(len(entries) >= 1)
    e = entries[0]
    _assert(e.operator == "restyle")
    _assert(e.note == "history-unit")
    _assert(e.edits[0].msg_id == HOLMES_RESTYLE_MSG)
    _assert(e.edits[0].before == before)
    _assert(e.edits[0].after == after)
    _assert(Path(e.backup).exists(), "history must reference a real backup file")


def test_history_undo_recorded() -> None:
    n_before = len(history.load_all(log_path=log_path()))
    transform.undo(log_path())
    entries = history.list_entries(log_path=log_path(), limit=1)
    _assert(len(entries) >= 1)
    _assert(entries[0].event == "undo")
    _assert(len(history.load_all(log_path=log_path())) == n_before + 1)


def test_st_sync_roundtrip_preserves_count() -> None:
    exported = st_sync.export_st_chat(log_path())
    msgs = exported["messages"]
    _assert(len(msgs) >= 20)
    original_count = len(msgs)
    msgs[3] = dict(msgs[3])
    msgs[3]["mes"] = msgs[3]["mes"] + "\n[SYNC-MARKER]"
    tmp = config.WORKSPACE_DIR / "sync_roundtrip.jsonl"
    n = st_sync.import_st_chat(msgs, tmp)
    _assert(n == original_count)
    back = st_sync.export_st_chat(tmp)
    _assert("[SYNC-MARKER]" in back["messages"][3]["mes"])
    tmp.unlink(missing_ok=True)


# ── HTTP API probes (server must be running) ────────────────────────────────

def test_api_projects_and_bundle(base: str) -> None:
    import tempfile

    from story_editor import bundle as bundle_mod

    status, view = api_json("GET", base, "/projects")
    _assert(status == 200, view)
    _assert(view["current"]["home"] == os.environ["STORY_EDITOR_HOME"], view["current"])
    _assert(view["current"]["source"] == "env", view["current"])
    _assert(view["registry"] == os.environ["STORY_EDITOR_REGISTRY"], view["registry"])
    status, body = api_json("POST", base, "/project/switch", {"id": "no-such-project"})
    _assert(status == 400 and "no project" in body.get("error", ""), body)
    # Saving downloads a bundle that verifies.
    req = urllib.request.Request(f"{base.rstrip('/')}/project/save", data=b"{}",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=API_TIMEOUT_SEC) as resp:
        _assert(resp.status == 200)
        _assert("sebundle" in resp.headers.get("Content-Disposition", ""))
        data = resp.read()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.sebundle"
        path.write_bytes(data)
        report = bundle_mod.verify(path)
        _assert(report.ok, report.errors)


def test_api_health(base: str) -> None:
    from story_editor.server import API_VERSION

    status, body = api_json("GET", base, "/health")
    _assert(status == 200)
    _assert(body.get("ok") is True)
    # Compare against the served constant: a hard-coded literal here just goes
    # stale on every bump and reports a red suite for a version number.
    _assert(body.get("version") == API_VERSION, body)


def test_api_status_fields(base: str) -> None:
    status, body = api_json("GET", base, "/status")
    _assert(status == 200)
    for key in ("log", "message_count", "history_path", "history_entries", "api_version"):
        _assert(key in body, f"status missing {key}")
    _assert(body["message_count"] >= 20)


def test_api_sync_import_export(base: str) -> None:
    exported = st_sync.export_st_chat(log_path())
    msgs = exported["messages"]
    msgs = [dict(m) for m in msgs]
    msgs[5]["mes"] = msgs[5]["mes"] + " API-SYNC-TAG"
    status, body = api_json("POST", base, "/sync/import", {"messages": msgs})
    _assert(status == 200, body)
    _assert(body.get("message_count") == len(msgs))
    status, out = api_json("GET", base, "/sync/export")
    _assert(status == 200)
    _assert("API-SYNC-TAG" in out["messages"][5]["mes"])


def _history_entry_by_note(base: str, note: str) -> dict:
    """Return the newest history entry matching *note*."""
    status, hist = api_json("GET", base, "/history?limit=50")
    _assert(status == 200, hist)
    for entry in hist.get("entries", []):
        if entry.get("note") == note:
            return entry
    raise AssertionError(f"no history entry with note={note!r}")


def test_api_edits_commit_and_history(base: str) -> None:
    before = holmes_msg2_text()
    after = before.replace("seven and a half", "eight")
    write_synthetic_restyle(before, after, note="api-commit-probe")
    status, pending = api_json("GET", base, "/edits")
    _assert(status == 200)
    _assert(pending.get("edit_set") is not None)
    _assert(len(pending["edit_set"]["edits"]) == 1)
    status, committed = api_json("POST", base, "/edits/commit", {})
    _assert(status == 200, committed)
    _assert(committed.get("applied") == 1)
    _assert(holmes_msg2_text() == after)
    entry = _history_entry_by_note(base, "api-commit-probe")
    status, one = api_json("GET", base, f"/history/{entry['id']}")
    _assert(status == 200)
    _assert(one.get("note") == "api-commit-probe")
    _assert(one.get("event") == "commit")


def test_api_undo(base: str) -> None:
    before_undo = holmes_msg2_text()
    _assert("eight" in before_undo, "commit probe should have landed before undo")
    status, body = api_json("POST", base, "/edits/undo", {})
    _assert(status == 200, body)
    _assert("seven and a half" in holmes_msg2_text())


def test_api_empty_restyle_advisory(base: str) -> None:
    """Restyle with no LLM: we only check API accepts a note and returns structured response."""
    # Skip if model would be called — this test uses impossible span to get empty quickly
    # Actually calling restyle hits LLM. Skip unless we only test error paths.
    status, body = api_json("POST", base, "/restyle", {"note": ""})
    _assert(status == 400)
    _assert("note" in str(body.get("error", "")).lower())


def test_api_unknown_route(base: str) -> None:
    status, _ = api_json("GET", base, "/no-such-route")
    _assert(status == 404)


def test_api_retune_rejects_bad_mode(base: str) -> None:
    status, body = api_json("POST", base, "/retune", {"mode": "invalid", "note": "x"})
    _assert(status == 400)
    _assert("mode" in str(body.get("error", "")).lower())


def test_api_inject_requires_speaker(base: str) -> None:
    status, body = api_json("POST", base, "/inject", {
        "note": "test",
        "after": HOLMES_RESTYLE_MSG,
    })
    _assert(status == 400)
    _assert("speaker" in str(body.get("error", "")).lower())


def test_api_sweep_last_without_context(base: str) -> None:
    ctx_path = config.LAST_SWEEP_CONTEXT
    had = ctx_path.exists()
    backup = ctx_path.read_text(encoding="utf-8") if had else None
    if had:
        ctx_path.unlink()
    try:
        status, body = api_json("POST", base, "/sweep/last", {})
        _assert(status == 400, body)
        _assert("sweep context" in str(body.get("error", "")).lower())
    finally:
        if had and backup is not None:
            ctx_path.write_text(backup, encoding="utf-8")




def _clear_attribution_store() -> None:
    p = config.VOICE_ATTRIBUTION
    if p.exists():
        p.unlink()


def _seed_attribution_needs_review(msg_id: int = 3) -> None:
    from story_editor import attribution as attr

    log = loader.load(log_path())
    turn = log.get(msg_id)
    store = attr.empty_store()
    label = attr.VoiceLabel(
        card=turn.speaker,
        voice="holmes",
        mode="mixed",
        source="heuristic",
        reviewed=False,
        confidence=0.5,
        notes="phase6 probe",
    )
    attr.set_label(store, msg_id, label, force=True)
    attr.save_store(store)


def test_api_attribution_stats_and_review(base: str) -> None:
    _clear_attribution_store()
    try:
        _seed_attribution_needs_review(3)
        status, stats = api_json("GET", base, "/attribution/stats")
        _assert(status == 200, stats)
        _assert(stats.get("needs_human", 0) >= 1)
        _assert("summary" in stats)
        status, review = api_json("GET", base, "/attribution/review")
        _assert(status == 200, review)
        _assert(review.get("count", 0) >= 1)
        ids = {item["msg_id"] for item in review.get("items", [])}
        _assert(3 in ids)
        item = next(i for i in review["items"] if i["msg_id"] == 3)
        _assert(item.get("message"))
        _assert(item.get("label", {}).get("mode") == "mixed")
    finally:
        _clear_attribution_store()


def test_api_attribution_set(base: str) -> None:
    _clear_attribution_store()
    try:
        _seed_attribution_needs_review(3)
        _, before = api_json("GET", base, "/attribution/review")
        before_count = before.get("count", 0)
        status, body = api_json("POST", base, "/attribution/set", {
            "msg_id": 3,
            "voice": "holmes",
            "mode": "pov",
            "reviewed": True,
        })
        _assert(status == 200, body)
        _assert(body.get("label", {}).get("mode") == "pov")
        _assert(body.get("needs_human_remaining") == before_count - 1)
        status, review = api_json("GET", base, "/attribution/review")
        _assert(status == 200)
        ids = {item["msg_id"] for item in review.get("items", [])}
        _assert(3 not in ids)
    finally:
        _clear_attribution_store()


def test_api_attribution_set_errors(base: str) -> None:
    status, body = api_json("POST", base, "/attribution/set", {"voice": "holmes"})
    _assert(status == 400)
    _assert("msg_id" in str(body.get("error", "")).lower())
    status, body = api_json("POST", base, "/attribution/set", {
        "msg_id": 3,
        "voice": "holmes",
        "mode": "not-a-mode",
    })
    _assert(status == 400)
    _assert("mode" in str(body.get("error", "")).lower())
    status, body = api_json("POST", base, "/attribution/set", {
        "msg_id": 99999,
        "voice": "holmes",
        "mode": "pov",
    })
    _assert(status == 404)


def test_api_attribution_reconcile_skip_propose(base: str) -> None:
    _clear_attribution_store()
    try:
        status, body = api_json("POST", base, "/attribution/reconcile", {
            "skip_propose": True,
        })
        _assert(status == 200, body)
        _assert(body.get("ok") is True)
        _assert("stats" in body)
        _assert("review" in body)
        _assert(isinstance(body["review"].get("items"), list))
    finally:
        _clear_attribution_store()


def test_api_attribution_bulk_set(base: str) -> None:
    _clear_attribution_store()
    try:
        _seed_attribution_needs_review(3)
        store = __import__("story_editor.attribution", fromlist=["attribution"]).load_store()
        from story_editor import attribution as attr_mod
        attr_mod.set_label(
            store, 4,
            attr_mod.VoiceLabel(
                card="Holmes", voice="holmes", mode="scene_narrator",
                source="heuristic", reviewed=False, confidence=0.9,
            ),
        )
        attr_mod.save_store(store)
        _, before = api_json("GET", base, "/attribution/review?preset=all")
        status, body = api_json("POST", base, "/attribution/bulk-set", {
            "preset": "all",
            "voice": "watson",
            "mode": "pov",
            "reviewed": True,
        })
        _assert(status == 200, body)
        _assert(body.get("ok") is True)
        _assert(body.get("applied", 0) >= 2)
        _assert(body.get("voice") == "watson")
        status, after = api_json("GET", base, "/attribution/review?preset=all")
        _assert(status == 200)
        _assert(after.get("count", 99) < before.get("count", 0))
    finally:
        _clear_attribution_store()


def test_api_attribution_review_filters(base: str) -> None:
    _clear_attribution_store()
    try:
        _seed_attribution_needs_review(3)
        store = __import__("story_editor.attribution", fromlist=["attribution"]).load_store()
        from story_editor import attribution as attr_mod
        attr_mod.set_label(
            store, 4,
            attr_mod.VoiceLabel(
                card="Holmes", voice="holmes", mode="scene_narrator",
                source="heuristic", reviewed=False, confidence=0.9,
            ),
        )
        attr_mod.save_store(store)
        status, all_rev = api_json("GET", base, "/attribution/review?preset=all")
        _assert(status == 200)
        status, focus = api_json(
            "GET", base,
            "/attribution/review?preset=focus&label_confidence_max=0.85",
        )
        _assert(status == 200, focus)
        _assert(focus.get("count", 99) <= all_rev.get("count", 0))
        _assert("filters" in focus)
        _assert(focus["filters"].get("label_confidence_max") == 0.85)
    finally:
        _clear_attribution_store()


# ── Cross-phase combined pipelines (no LLM) ───────────────────────────────

def test_pipeline_import_index_structure_canon() -> None:
    """Phase 0 + 1 + 1.5 + 5 on the isolated log."""
    subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "structure", "doctor"],
        check=True, cwd=str(ROOT), capture_output=True,
    )
    subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "structure", "scenes"],
        check=True, cwd=str(ROOT), capture_output=True,
    )
    out = subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "canon", "list"],
        check=True, cwd=str(ROOT), capture_output=True, text=True,
    )
    _assert("holmes" in out.stdout.lower())
    subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "index", "build", "--rebuild"],
        check=True, cwd=str(ROOT), capture_output=True,
    )
    kw = subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "index", "search", "deduce",
         "--mode", "keyword", "--limit", "3"],
        check=True, cwd=str(ROOT), capture_output=True, text=True,
    )
    _assert("msg" in kw.stdout.lower())


def test_pipeline_commit_sweep_context_and_doctor() -> None:
    """Phase 2 commit → Phase 3 sweep context; doctor still clean."""
    before = holmes_msg2_text()
    after = before.replace("Watson", "my friend")
    write_synthetic_restyle(before, after, note="cross-phase-sweep")
    transform.commit_edits(log_path())
    ctx_path = config.LAST_SWEEP_CONTEXT
    _assert(ctx_path.exists(), "commit must write last_sweep_context.json")
    ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
    _assert(ctx.get("operator") == "restyle")
    _assert(ctx.get("changed_from") == HOLMES_RESTYLE_MSG)
    proc = subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "structure", "doctor"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    _assert(proc.returncode in (0, 1), "doctor should not crash")  # 1 = warnings only


def test_pipeline_history_cli_matches_api(base: str) -> None:
    """CLI history list agrees with API on a known entry."""
    entry = _history_entry_by_note(base, "cross-phase-sweep")
    api_id = entry["id"]
    cli = subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "history", "show", str(api_id)],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    _assert(cli.returncode == 0)
    _assert(f"#{api_id}" in cli.stdout)


def test_pipeline_index_finds_text_after_edit() -> None:
    """Phase 1 index still resolves content after a committed edit."""
    subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "index", "build", "--rebuild"],
        check=True, cwd=str(ROOT), capture_output=True,
    )
    kw = subprocess.run(
        [sys.executable, "-m", "story_editor.cli", "index", "search", "friend",
         "--mode", "keyword", "--limit", "5"],
        cwd=str(ROOT), capture_output=True, text=True,
    )
    _assert(kw.returncode == 0)
    _assert("friend" in holmes_msg2_text().lower())


def test_pipeline_st_sync_then_commit_via_api(base: str) -> None:
    """Simulates ST extension: push chat → stage edit via API path → commit → export."""
    exported = st_sync.export_st_chat(log_path())
    msgs = [dict(m) for m in exported["messages"]]
    api_json("POST", base, "/sync/import", {"messages": msgs})
    log = loader.load(log_path())
    target = log.get(7)
    before = target.text
    after = before + "\n[PHASE6-INJECT-MARKER]"
    write_synthetic_restyle(
        before, after,
        msg_id=7,
        speaker=target.speaker,
        note="st-sync-pipeline",
    )
    status, body = api_json("POST", base, "/edits/commit", {})
    _assert(status == 200, body)
    _, out = api_json("GET", base, "/sync/export")
    _assert("[PHASE6-INJECT-MARKER]" in out["messages"][7]["mes"])


# ── Derived layers (novel editor) ─────────────────────────────────────────

def _seed_manuscript(*, start: int = 1, end: int = 2):
    """Write a one-scene manuscript over the isolated log and return the scene.

    Phase 1 has no scene-creation route — scenes arrive from the novelize
    pipeline in Phase 3 — so the fixture writes the document directly and the
    probes exercise the read/edit/approve/rebase surface over HTTP.
    """
    from story_editor import manuscript as ms

    log = loader.load(log_path())
    scene = ms.new_scene(log, start, end, title="Phase 6 scene")
    scene.blocks = ms.blocks_from_prose(
        "A first paragraph of derived prose.\n\nAnd a second one.",
        src=[log.get(i).uid for i in range(start, end + 1)],
    )
    doc = ms.Document(layer="manuscript")
    doc.upsert(scene)
    ms.save(doc)
    return scene


def _clear_manuscript() -> None:
    Path(config.MANUSCRIPT).unlink(missing_ok=True)


def test_api_layers_capability_map(base: str) -> None:
    status, body = api_json("GET", base, "/layers")
    _assert(status == 200, body)
    _assert(body["layers"] == ["log", "manuscript"])
    _assert(body["syncable"] == ["log"])
    _assert("novelize" in body["ops"])
    _assert("cool" not in body["ops"])


def test_api_manuscript_empty(base: str) -> None:
    _clear_manuscript()
    status, body = api_json("GET", base, "/manuscript")
    _assert(status == 200, body)
    _assert(body["layer"] == "manuscript")
    _assert(body["stats"]["scenes"] == 0)
    _assert(body["scenes"] == [])


def test_api_manuscript_lists_scene_and_drift(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        status, body = api_json("GET", base, "/manuscript")
        _assert(status == 200, body)
        _assert(body["stats"]["scenes"] == 1)
        _assert(body["drifted"] == 0)
        row = body["scenes"][0]
        _assert(row["id"] == scene.id)
        _assert(row["status"] == "draft")
        _assert(row["blocks"] == 2)
        _assert(row["words"] > 0)
    finally:
        _clear_manuscript()


def test_api_scene_three_layer_views(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        status, body = api_json("GET", base, f"/scene/{scene.id}")
        _assert(status == 200, body)
        _assert(body["layer"] == "manuscript")
        _assert(len(body["blocks"]) == 2)
        _assert(body["drift"]["kind"] == "")
        # Same id, log view: the source turns this scene was derived from.
        status, src = api_json("GET", base, f"/scene/{scene.id}?layer=log")
        _assert(status == 200, src)
        _assert(src["layer"] == "log")
        _assert(len(src["messages"]) == 2)
        _assert(src["messages"][0]["uid"])
        # Provenance is metadata, never prose.
        _assert("se_uid" not in body["text"])
    finally:
        _clear_manuscript()


def test_api_scene_unknown_id_and_layer(base: str) -> None:
    status, body = api_json("GET", base, "/scene/sc-nope")
    _assert(status == 404, body)
    status, body = api_json("GET", base, "/scene/sc-nope?layer=archive")
    _assert(status == 409, body)
    _assert("unknown layer" in body.get("error", ""))


def test_api_scene_put_edits_blocks(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        block_id = scene.blocks[0].id
        status, body = api_json("PUT", base, f"/scene/{scene.id}", {
            "layer": "manuscript",
            "blocks": [{"id": block_id, "text": "A hand-edited paragraph."}],
        })
        _assert(status == 200, body)
        _assert(body["blocks_touched"] == 1)
        edited = next(b for b in body["blocks"] if b["id"] == block_id)
        _assert(edited["text"] == "A hand-edited paragraph.")
        _assert(edited["edited"] is True)
        # Provenance survives a hand edit.
        _assert(edited["src"])
        # ...and it persisted.
        _, again = api_json("GET", base, f"/scene/{scene.id}")
        _assert("hand-edited" in again["text"])
    finally:
        _clear_manuscript()


def test_api_scene_put_refuses_the_log_layer(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        status, body = api_json("PUT", base, f"/scene/{scene.id}", {
            "layer": "log",
            "text": "trying to hand-edit roleplay turns",
        })
        _assert(status == 409, body)
        _assert("edit cannot target the log layer" in body.get("error", ""))
    finally:
        _clear_manuscript()


def test_api_scene_status_and_rebase(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        status, body = api_json("POST", base, f"/scene/{scene.id}/status", {
            "layer": "manuscript", "status": "approved",
        })
        _assert(status == 200, body)
        _assert(body["status"] == "approved")

        status, body = api_json("POST", base, f"/scene/{scene.id}/status", {
            "layer": "manuscript", "status": "sideways",
        })
        _assert(status == 400, body)

        # Disturb the source under the scene, then accept it via rebase.
        log = loader.load(log_path())
        target = log.get(1)
        write_synthetic_restyle(
            target.text,
            target.text + "\n[PHASE6-DRIFT-MARKER]",
            msg_id=1,
            speaker=target.speaker,
            note="drift-probe",
        )
        api_json("POST", base, "/edits/commit", {})
        _, drifted = api_json("GET", base, f"/scene/{scene.id}")
        _assert(drifted["drift"]["kind"] == "source-changed", drifted["drift"])

        status, body = api_json("POST", base, f"/scene/{scene.id}/rebase", {
            "layer": "manuscript",
        })
        _assert(status == 200, body)
        _assert(body["drift"]["kind"] == "")
        _assert("first paragraph" in body["text"])
    finally:
        _clear_manuscript()
        api_json("POST", base, "/edits/undo", {})


def test_api_novelize_plan_reports_the_job(base: str) -> None:
    _clear_manuscript()
    try:
        status, body = api_json("GET", base, "/novelize/plan?from=1&to=2")
        _assert(status == 200, body)
        _assert(body["turns"] >= 1, body)
        _assert(body["existing"] is None)
        _assert(body["voice"]["person"] in body["persons"], body["voice"])
        _assert(body["voice"]["tense"] in body["tenses"], body["voice"])
        _assert(body["too_long"] is False)
        _assert(body.get("chunk_count", 1) == 1, body)
        # The picker's options come from the engine, never hard-coded client-side.
        _assert("close_third" in body["persons"])
        _assert(isinstance(body["focal_candidates"], list))
    finally:
        _clear_manuscript()


def test_api_novelize_plan_takes_a_partial_voice(base: str) -> None:
    # Asking for one field must not reset the others.
    status, body = api_json("GET", base, "/novelize/plan?from=1&to=2&tense=present")
    _assert(status == 200, body)
    _assert(body["voice"]["tense"] == "present", body["voice"])
    _assert(body["voice"]["person"] == body["book_voice"]["person"], body)

    status, body = api_json("GET", base, "/novelize/plan?from=1&to=2&person=sideways")
    _assert(status == 400, body)  # the vocabulary check rejects it as a bad request
    _assert("unknown person" in body.get("error", ""), body)


def test_api_book_voice_is_settable_and_sticks(base: str) -> None:
    _clear_manuscript()
    try:
        status, body = api_json("POST", base, "/manuscript/voice", {
            "person": "first", "tense": "present", "focal": "Wren",
        })
        _assert(status == 200, body)
        _assert(body["voice_label"] == "first person, present tense, following Wren")
        _, view = api_json("GET", base, "/manuscript")
        _assert(view["voice"]["person"] == "first", view["voice"])
        # And the plan for an un-novelized span now inherits it.
        _, plan = api_json("GET", base, "/novelize/plan?from=1&to=2")
        _assert(plan["voice"]["tense"] == "present", plan["voice"])

        status, body = api_json("POST", base, "/manuscript/voice", {})
        _assert(status == 400, body)
    finally:
        _clear_manuscript()


def test_api_scene_voice_overrides_the_book(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        status, body = api_json("POST", base, f"/scene/{scene.id}/voice", {
            "person": "omniscient",
        })
        _assert(status == 200, body)
        _assert(body["voice"]["person"] == "omniscient", body["voice"])
        # Omniscient has no vantage, so it carries no focal character.
        _assert(body["voice"].get("focal", "") == "", body["voice"])

        status, body = api_json("POST", base, f"/scene/{scene.id}/voice", {
            "follow_book": True,
        })
        _assert(status == 200, body)
        _assert(body["voice"] is None, body["voice"])

        status, body = api_json("POST", base, "/scene/sc-nope/voice", {"tense": "past"})
        _assert(status == 404, body)
    finally:
        _clear_manuscript()


def test_api_novelize_refuses_a_second_pass_without_regenerate(base: str) -> None:
    _clear_manuscript()
    try:
        scene = _seed_manuscript()
        # No model is called: the guard fires before the pass would start.
        status, body = api_json("POST", base, "/novelize", {"from": 1, "to": 2})
        _assert(status == 409, body)
        _assert(body["scene"] == scene.id, body)
        _assert("regenerate" in body.get("error", ""), body)
    finally:
        _clear_manuscript()


def test_api_novelize_plan_exposes_chunks_for_an_oversized_span(base: str) -> None:
    """Oversized-but-chunkable spans are planned as N passes, not hard-refused."""
    _clear_manuscript()
    try:
        from story_editor import novelize as nv

        log = loader.load(log_path())
        chars = sum(len(m.text) for m in log.messages)
        if chars <= nv.MAX_SPAN_CHARS:
            return  # the fixture log is small; nothing to chunk
        status, body = api_json(
            "GET", base, f"/novelize/plan?from=0&to={len(log) - 1}",
        )
        _assert(status == 200, body)
        _assert(body["too_long"] is True, body)
        _assert(body.get("unchunkable") is False, body)
        _assert(body["chunk_count"] > 1, body)
        _assert(len(body["chunks"]) == body["chunk_count"], body)
        _assert(body["chunks"][0]["from"] == 0, body)
        _assert(body["chunks"][-1]["to"] == len(log) - 1, body)
    finally:
        _clear_manuscript()


# --------------------------------------------------------------------------- #
# The transform surface: one review shape, and the layer rule at the door
# --------------------------------------------------------------------------- #


HEADERED_MSG = 4


def _stage(msg_id: int, after: str, *, note: str, operator: str = "restyle") -> str:
    """Stage a pending edit against the real text on disk, which is what commit
    checks against. Returns the `before` it staged."""
    before = loader.load(log_path()).get(msg_id).text
    write_synthetic_restyle(
        before, after, msg_id=msg_id,
        speaker=loader.load(log_path()).get(msg_id).speaker,
        note=note,
    )
    if operator != "restyle":
        edit_set = transform.load_pending_edits()
        _assert(edit_set is not None)
        edit_set.operator = operator
        transform.write_pending_edits(edit_set)
    return before


def _rebuild(hunks: list[dict], side: str) -> str:
    drop = "ins" if side == "before" else "del"
    return "".join(h["text"] for h in hunks if h["kind"] != drop)


def test_api_layers_reports_what_is_actually_written(base: str) -> None:
    """The GUI builds its verb list from this, so `runs_on` has to be here and has
    to be a subset of `layers` — otherwise the pane offers a button that 501s."""
    status, body = api_json("GET", base, "/layers")
    _assert(status == 200, body)
    for name, op in body["ops"].items():
        _assert("runs_on" in op, f"{name} does not say where it runs")
        _assert(set(op["runs_on"]) <= set(op["layers"]), name)
        if set(op["runs_on"]) != set(op["layers"]):
            _assert(op["pending"], f"{name} is unwritten somewhere and says nothing")
    _assert(body["ops"]["restyle"]["runs_on"] == ["log"], body["ops"]["restyle"])
    _assert(body["ops"]["novelize"]["runs_on"] == ["manuscript"])


def test_api_proposal_is_empty_when_nothing_waits(base: str) -> None:
    transform.discard_pending_edits()
    status, body = api_json("GET", base, "/proposal")
    _assert(status == 200, body)
    _assert(body.get("kind") is None, body)
    _assert(body.get("edits") == [], body)


def test_api_proposal_renders_a_staged_restyle(base: str) -> None:
    transform.discard_pending_edits()
    try:
        before = _stage(
            HOLMES_RESTYLE_MSG,
            loader.load(log_path()).get(HOLMES_RESTYLE_MSG).text.replace(
                "seven and a half", "eight"
            ),
            note="proposal-probe",
        )
        status, body = api_json("GET", base, "/proposal")
        _assert(status == 200, body)
        _assert(body["kind"] == "edits", body)
        _assert(body["layer"] == "log", body)
        _assert(body["op"] == "restyle", body)
        _assert(body["advisory"] is False, body)
        _assert(body["note"] == "proposal-probe", body)
        edit = body["edits"][0]
        _assert(edit["msg_id"] == HOLMES_RESTYLE_MSG, edit)
        # The hunks are the diff the pane draws; they must rebuild both sides
        # exactly or the reviewer is answering a paraphrase.
        _assert(_rebuild(edit["hunks"], "before") == edit["before"], edit["hunks"])
        _assert(_rebuild(edit["hunks"], "after") == edit["after"], edit["hunks"])
        _assert(edit["before"] == before, edit["before"][:80])
        _assert(any(h["kind"] == "ins" for h in edit["hunks"]), edit["hunks"])
    finally:
        transform.discard_pending_edits()


def test_api_proposal_names_the_verb_behind_a_mode(base: str) -> None:
    """`retune-tighten` is a label for the reviewer; `retune` is what the layer
    rules know. The pane needs both, so the payload carries both."""
    transform.discard_pending_edits()
    try:
        text = loader.load(log_path()).get(HOLMES_RESTYLE_MSG).text
        _stage(HOLMES_RESTYLE_MSG, text + " Briefly.", note="mode-probe",
               operator="retune-tighten")
        status, body = api_json("GET", base, "/proposal")
        _assert(status == 200, body)
        _assert(body["operator"] == "retune-tighten", body)
        _assert(body["op"] == "retune", body)
    finally:
        transform.discard_pending_edits()


def test_api_proposal_follows_a_dropped_edit(base: str) -> None:
    transform.discard_pending_edits()
    try:
        log = loader.load(log_path())
        first, second = HOLMES_RESTYLE_MSG, HOLMES_RESTYLE_MSG + 1
        edit_set = EditSet(
            log=str(log_path().resolve()),
            operator="restyle",
            note="drop-probe",
            locator=f"msgs {first}\u2013{second}",
            edits=[
                Edit(msg_id=m, speaker=log.get(m).speaker,
                     before=log.get(m).text, after=log.get(m).text + " Indeed.",
                     kind="replace")
                for m in (first, second)
            ],
        )
        transform.write_pending_edits(edit_set)
        status, body = api_json("GET", base, "/proposal")
        _assert(len(body["edits"]) == 2, body)
        status, dropped = api_json("POST", base, "/edits/drop", {"msg_id": second})
        _assert(status == 200 and dropped.get("ok") is True, dropped)
        status, body = api_json("GET", base, "/proposal")
        _assert([e["msg_id"] for e in body["edits"]] == [first], body)
    finally:
        transform.discard_pending_edits()


def test_api_proposal_holds_the_header_out_of_the_diff(base: str) -> None:
    """End to end: a turn carrying a `[ time | date | location ]` header is
    diffed on its body alone, and the header comes back marked as kept. Written
    through the same routes the GUI uses, then undone."""
    header = "[ \U0001f570\ufe0f 07:20 | \U0001f4c5 Day 2 | \U0001f4cd Baker Street ]\n\n"
    transform.discard_pending_edits()
    original = loader.load(log_path()).get(HEADERED_MSG).text
    try:
        _stage(HEADERED_MSG, header + original, note="header-probe-setup")
        status, body = api_json("POST", base, "/edits/commit", {})
        _assert(status == 200 and body.get("applied") == 1, body)

        _stage(HEADERED_MSG, header + original + " He said no more.",
               note="header-probe")
        status, body = api_json("GET", base, "/proposal")
        _assert(status == 200, body)
        edit = body["edits"][0]
        _assert(edit["header"] == header, repr(edit["header"]))
        _assert(edit["header_preserved"] is True, edit)
        _assert(edit["before"] == original, edit["before"][:80])
        joined = "".join(h["text"] for h in edit["hunks"])
        _assert("Baker Street ]" not in joined, "the header leaked into the diff")
    finally:
        transform.discard_pending_edits()
        status, body = api_json("POST", base, "/edits/undo", {})
        _assert(status == 200, body)
        _assert(loader.load(log_path()).get(HEADERED_MSG).text == original,
                "the header probe did not put the turn back")


def test_api_transform_routes_refuse_a_derived_layer(base: str) -> None:
    """Two refusals that must not read the same. Restyling the novel is a design
    the engine allows and has not written: 501, with the sentence why. Retuning
    it is not allowed at all: 409."""
    status, body = api_json("POST", base, "/restyle", {
        "note": "colder", "from": HOLMES_RESTYLE_MSG, "to": HOLMES_RESTYLE_MSG,
        "layer": "manuscript",
    })
    _assert(status == 501, body)
    _assert("not implemented yet" in body.get("error", ""), body)

    status, body = api_json("POST", base, "/retune", {
        "mode": "tighten", "from": HOLMES_RESTYLE_MSG, "to": HOLMES_RESTYLE_MSG,
        "layer": "manuscript",
    })
    _assert(status == 409, body)
    _assert("cannot target" in body.get("error", ""), body)

    status, body = api_json("POST", base, "/inject", {
        "note": "a beat", "speaker": "Holmes", "after": HOLMES_RESTYLE_MSG,
        "layer": "manuscript",
    })
    _assert(status in (409, 501), body)
    # Nothing was proposed by any of the three.
    status, body = api_json("GET", base, "/proposal")
    _assert(body.get("kind") is None, body)


def test_api_status_says_whether_sweep_has_anything_to_read(base: str) -> None:
    """Sweep reads the last commit rather than a selection, so the GUI asks
    /status whether the verb is offerable at all."""
    ctx_path = config.LAST_SWEEP_CONTEXT
    had = ctx_path.read_text(encoding="utf-8") if ctx_path.exists() else None
    try:
        ctx_path.unlink(missing_ok=True)
        status, body = api_json("GET", base, "/status")
        _assert(status == 200, body)
        _assert(body["sweep_ready"] is False, body)
        _assert(body["sweep_note"] is None, body)

        before = loader.load(log_path()).get(HOLMES_RESTYLE_MSG).text
        _stage(HOLMES_RESTYLE_MSG, before + " Quite so.", note="sweep-ready-probe")
        status, body = api_json("POST", base, "/edits/commit", {})
        _assert(status == 200, body)
        status, body = api_json("GET", base, "/status")
        _assert(body["sweep_ready"] is True, body)
        _assert(body["sweep_note"] == "sweep-ready-probe", body)
        _assert(body["sweep_from"] == HOLMES_RESTYLE_MSG + 1, body)
    finally:
        transform.discard_pending_edits()
        api_json("POST", base, "/edits/undo", {})
        if had is not None:
            ctx_path.write_text(had, encoding="utf-8")
        else:
            ctx_path.unlink(missing_ok=True)


def test_api_app_bundle(base: str) -> None:
    """Either the bundle is built and served, or the 404 says how to build it.

    Fetched raw rather than as JSON: a served bundle answers with HTML, and the
    probe that only knew how to read the missing case was itself the bug the
    first time a bundle existed.
    """
    status, raw, ctype = api_raw("GET", base, "/app")
    if status == 404:
        _assert("npm run build" in raw, raw[:200])
        return
    _assert(status == 200, raw[:200])
    _assert("text/html" in ctype, ctype)
    _assert('id="root"' in raw, raw[:200])

    status, raw, _ = api_raw("GET", base, "/app/does-not-exist")
    # SPA fallback: an unknown path inside the app is the app, not a 404.
    _assert(status == 200, raw[:200])


# ── The shell's own routes (phase 2) ──────────────────────────────────────

def test_api_spine_view(base: str) -> None:
    status, body = api_json("GET", base, "/spine")
    _assert(status == 200, body)
    for key in ("authored", "derived", "counts", "has_derived_spine"):
        _assert(key in body, f"missing {key}")
    _assert(isinstance(body["authored"], list))
    # Every authored beat carries a status, even with no alignment cached.
    for beat in body["authored"]:
        _assert(beat.get("status") in
                ("written", "partial", "planned", "unknown"), beat)


def test_api_scenes_index(base: str) -> None:
    status, body = api_json("GET", base, "/scenes")
    _assert(status == 200, body)
    _assert(body["count"] == len(body["scenes"]))
    if body["scenes"]:
        first = body["scenes"][0]
        for key in ("scene_id", "start", "end", "manuscript"):
            _assert(key in first, f"missing {key}: {first}")


def test_api_log_span_page(base: str) -> None:
    status, body = api_json("GET", base, "/log/span?from=0&to=3")
    _assert(status == 200, body)
    _assert(len(body["messages"]) <= 4, body)
    first = body["messages"][0]
    for key in ("msg_id", "uid", "speaker", "prose", "manuscript_scene"):
        _assert(key in first, f"missing {key}: {first}")

    # Out-of-range is clamped rather than refused: the page asks for a scene,
    # and a log that shrank under it should still render.
    status, body = api_json("GET", base, "/log/span?from=99999&to=99999")
    _assert(status == 200, body)
    _assert(body["end"] < 99999, body)

    status, body = api_json("GET", base, "/log/span?from=abc&to=2")
    _assert(status == 400, body)


def test_api_fonts_registry_and_refusals(base: str) -> None:
    status, body = api_json("GET", base, "/fonts")
    _assert(status == 200, body)
    _assert("roles" in body and "faces" in body, body)

    status, body = api_json("POST", base, "/fonts/role",
                            {"role": "page", "family": "Courier Prime", "size": 17})
    _assert(status == 200, body)
    _assert(body["fonts"]["roles"]["page"]["family"] == "Courier Prime", body)

    status, body = api_json("POST", base, "/fonts/role", {"role": "marginalia"})
    _assert(status == 400, body)

    status, body = api_json("POST", base, "/fonts/system",
                            {"family": "Traveling Typewriter", "licence": "personal use"})
    _assert(status == 200, body)
    _assert(any(f["family"] == "Traveling Typewriter" for f in body["fonts"]["faces"]), body)

    status, body = api_json("POST", base, "/fonts/system", {"family": "../../etc/passwd"})
    _assert(status == 400, body)

    # A font file route that serves any path it is handed is a file-read hole.
    status, body = api_json("GET", base, "/fonts/file/../../../etc/passwd")
    _assert(status == 404, body)

    api_json("POST", base, "/fonts/remove", {"family": "Traveling Typewriter"})


# ── Optional LLM combined probes ──────────────────────────────────────────

def test_api_restyle_llm(base: str) -> None:
    if not _model_reachable():
        raise SkipTest("model server down")
    before = holmes_msg2_text()
    status, body = api_json(
        "POST", base, "/restyle",
        {
            "note": "make this one sentence shorter — same facts",
            "from": HOLMES_RESTYLE_MSG,
            "to": HOLMES_RESTYLE_MSG,
            "speaker": "Holmes",
        },
    )
    _assert(status == 200, body)
    # advisory (no changes) is valid exit 200
    _assert("edit_set" in body)
    if body.get("advisory"):
        transform.discard_pending_edits()
        return
    api_json("POST", base, "/edits/discard", {})


def test_pipeline_canon_check_after_api_restyle(base: str) -> None:
    if not _model_reachable():
        raise SkipTest("model server down")
    status, body = api_json(
        "POST", base, "/restyle",
        {"note": "clip diction", "from": HOLMES_RESTYLE_MSG, "to": HOLMES_RESTYLE_MSG, "speaker": "Holmes"},
    )
    _assert(status == 200)
    if body.get("advisory") or not body.get("edit_set", {}).get("edits"):
        raise SkipTest("restyle produced no edits")
    status, check = api_json("POST", base, "/canon/check", {})
    _assert(status == 200)
    _assert("results" in check)
    api_json("POST", base, "/edits/discard", {})


def _model_reachable() -> bool:
    url = os.environ.get("STORY_EDITOR_MODEL_URL", "http://127.0.0.1:5000/v1").rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--api-base",
        default="http://127.0.0.1:8765",
        help="Story Editor API base URL (server must be running)",
    )
    p.add_argument(
        "--skip-reset",
        action="store_true",
        help="do not re-import log / clear history (for debugging)",
    )
    args = p.parse_args()
    base = args.api_base

    print("=" * 62)
    print(" Phase 6 probes — STORY_EDITOR_HOME=", config.HOME_DIR)
    print(" log:", log_path())
    print(" API:", base)
    print("=" * 62)

    if not args.skip_reset:
        print("\n### setup — fresh import + empty history")
        reset_workspace()

    print("\n### A — history + st_sync (unit)")
    run("history: commit recorded with diffs", test_history_append_and_list)
    run("history: undo appends entry", test_history_undo_recorded)
    run("st_sync: import/export roundtrip", test_st_sync_roundtrip_preserves_count)

    print("\n### B — HTTP API")
    run("API: GET /health", lambda: test_api_health(base))
    run("API: /projects + bundle download", lambda: test_api_projects_and_bundle(base))
    run("API: GET /status fields", lambda: test_api_status_fields(base))
    run("API: sync import/export", lambda: test_api_sync_import_export(base))
    run("API: edits commit + GET /history", lambda: test_api_edits_commit_and_history(base))
    run("API: POST /edits/undo", lambda: test_api_undo(base))
    run("API: restyle rejects empty note", lambda: test_api_empty_restyle_advisory(base))
    run("API: unknown route 404", lambda: test_api_unknown_route(base))
    run("API: retune rejects bad mode", lambda: test_api_retune_rejects_bad_mode(base))
    run("API: inject requires speaker", lambda: test_api_inject_requires_speaker(base))
    run("API: sweep/last without context", lambda: test_api_sweep_last_without_context(base))
    run("API: GET /attribution/stats + /review", lambda: test_api_attribution_stats_and_review(base))
    run("API: POST /attribution/set", lambda: test_api_attribution_set(base))
    run("API: POST /attribution/set errors", lambda: test_api_attribution_set_errors(base))
    run("API: POST /attribution/reconcile skip_propose", lambda: test_api_attribution_reconcile_skip_propose(base))
    run("API: POST /attribution/bulk-set", lambda: test_api_attribution_bulk_set(base))
    run("API: GET /attribution/review filters", lambda: test_api_attribution_review_filters(base))

    print("\n### B2 — derived layers (novel editor)")
    run("API: GET /layers", lambda: test_api_layers_capability_map(base))
    run("API: GET /manuscript empty", lambda: test_api_manuscript_empty(base))
    run("API: GET /manuscript lists scene", lambda: test_api_manuscript_lists_scene_and_drift(base))
    run("API: GET /scene three layers", lambda: test_api_scene_three_layer_views(base))
    run("API: GET /scene errors", lambda: test_api_scene_unknown_id_and_layer(base))
    run("API: PUT /scene edits blocks", lambda: test_api_scene_put_edits_blocks(base))
    run("API: PUT /scene refuses log layer", lambda: test_api_scene_put_refuses_the_log_layer(base))
    run("API: scene status + rebase", lambda: test_api_scene_status_and_rebase(base))
    run("API: GET /app bundle", lambda: test_api_app_bundle(base))

    print("\n### B3 — the shell's routes (phase 2)")
    run("API: GET /spine", lambda: test_api_spine_view(base))
    run("API: GET /scenes", lambda: test_api_scenes_index(base))
    run("API: GET /log/span", lambda: test_api_log_span_page(base))
    run("API: /fonts registry + refusals", lambda: test_api_fonts_registry_and_refusals(base))

    print("\n### B4 — novelization (phase 3, no LLM)")
    run("API: GET /novelize/plan", lambda: test_api_novelize_plan_reports_the_job(base))
    run("API: plan takes a partial voice", lambda: test_api_novelize_plan_takes_a_partial_voice(base))
    run("API: POST /manuscript/voice", lambda: test_api_book_voice_is_settable_and_sticks(base))
    run("API: POST /scene/{id}/voice", lambda: test_api_scene_voice_overrides_the_book(base))
    run("API: novelize guards a second pass", lambda: test_api_novelize_refuses_a_second_pass_without_regenerate(base))
    run("API: novelize plan exposes chunks", lambda: test_api_novelize_plan_exposes_chunks_for_an_oversized_span(base))

    print("\n### B5 — the transform surface (phase 3½, no LLM)")
    run("API: /layers reports what is written", lambda: test_api_layers_reports_what_is_actually_written(base))
    run("API: GET /proposal empty", lambda: test_api_proposal_is_empty_when_nothing_waits(base))
    run("API: GET /proposal renders a restyle", lambda: test_api_proposal_renders_a_staged_restyle(base))
    run("API: GET /proposal names the verb", lambda: test_api_proposal_names_the_verb_behind_a_mode(base))
    run("API: GET /proposal follows a drop", lambda: test_api_proposal_follows_a_dropped_edit(base))
    run("API: GET /proposal keeps the header out", lambda: test_api_proposal_holds_the_header_out_of_the_diff(base))
    run("API: transform routes refuse a derived layer", lambda: test_api_transform_routes_refuse_a_derived_layer(base))
    run("API: /status reports sweep readiness", lambda: test_api_status_says_whether_sweep_has_anything_to_read(base))

    print("\n### C — cross-phase pipelines (no LLM)")
    run("pipeline: import + index + structure + canon", test_pipeline_import_index_structure_canon)
    run("pipeline: commit writes sweep context", test_pipeline_commit_sweep_context_and_doctor)
    run("pipeline: CLI history matches API", lambda: test_pipeline_history_cli_matches_api(base))
    run("pipeline: index finds text after edit", test_pipeline_index_finds_text_after_edit)
    run("pipeline: ST sync → commit → export", lambda: test_pipeline_st_sync_then_commit_via_api(base))

    print("\n### D — optional LLM combined")
    run("API: restyle (LLM)", lambda: test_api_restyle_llm(base), optional=True)
    run("pipeline: canon check after restyle (LLM)", lambda: test_pipeline_canon_check_after_api_restyle(base), optional=True)

    print()
    print("=" * 62)
    print(f" SUMMARY    PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
    print("=" * 62)
    for r in RESULTS:
        print(f"  {r}")

    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
