"""Switching projects from the server: refused while work is in flight."""

from __future__ import annotations

from story_editor import drive_sync, server


def test_switch_is_allowed_when_idle():
    with server._Inflight():  # the switch request itself
        assert server._switch_refusal() is None


def test_switch_waits_for_other_requests():
    with server._Inflight(), server._Inflight():
        refusal = server._switch_refusal()
    assert refusal and "1 other request" in refusal


def test_switch_waits_for_a_drive_sync(monkeypatch):
    monkeypatch.setitem(drive_sync._JOB, "running", True)
    with server._Inflight():
        refusal = server._switch_refusal()
    assert refusal and "Drive sync" in refusal


def test_the_project_payload_describes_the_open_project():
    view = server._project_payload()
    assert view["current"]["id"] == "lantern-quay"
    assert view["current"]["source"] == "env"
    assert view["projects"] == [] and view["registry_error"] == ""
