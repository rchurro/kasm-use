"""Scope rules: tools act only on sessions this process started. No Kasm server needed."""
import json
import os

os.environ.setdefault("KASM_API_URL", "https://kasm.invalid")
os.environ.setdefault("KASM_API_KEY", "k")
os.environ.setdefault("KASM_API_KEY_SECRET", "s")
os.environ.setdefault("KASM_USER_ID", "u")

import pytest

from kasm_use import tools


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    tools._SESSIONS.clear()
    tools._LAST_SHOT.clear()
    calls = []
    monkeypatch.setattr(tools, "_call", lambda ep, **kw: calls.append((ep, kw)) or {})
    monkeypatch.setattr(tools, "_session", lambda kid: pytest.fail("must not open VNC for a foreign session"))
    yield calls


FOREIGN = "1b1a2009-c172-439f-9c0a-13110a5d9098"


@pytest.mark.parametrize("tool,args", [
    (tools.kasm_look, {"wait_seconds": 0}),
    (tools.kasm_click, {"x": 1, "y": 1}),
    (tools.kasm_type, {"text": "hi"}),
    (tools.kasm_key, {"keys": "Return"}),
    (tools.kasm_scroll, {}),
    (tools.kasm_stop, {}),
])
def test_foreign_session_refused(clean, tool, args):
    out = json.loads(tool({**args, "session_id": FOREIGN}))
    assert out["ok"] is False and "only controls sessions it started" in out["error"]
    assert not any(ep == "destroy_kasm" for ep, _ in clean)  # nothing was stopped


def test_no_sessions_means_nothing_to_act_on(clean):
    out = json.loads(tools.kasm_stop({}))
    assert out["ok"] is False and "kasm_start" in out["error"]
    assert not clean


def test_own_session_is_resolved_with_or_without_dashes():
    tools._SESSIONS["1b1a2009c172439f9c0a13110a5d9098"] = {"started": 1, "workspace": "Chrome"}
    assert tools._resolve_session({"session_id": FOREIGN}) == "1b1a2009c172439f9c0a13110a5d9098"
    assert tools._resolve_session({}) == "1b1a2009c172439f9c0a13110a5d9098"


def test_default_is_most_recent_own_session():
    tools._SESSIONS["aaa"] = {"started": 1, "workspace": "Chrome"}
    tools._SESSIONS["bbb"] = {"started": 2, "workspace": "Terminal"}
    assert tools._resolve_session({}) == "bbb"


def test_no_exec_or_screenshot_api_left():
    src = open(tools.__file__).read()
    for gone in ("exec_command_kasm", "get_kasm_screenshot", "xdotool getdisplaygeometry"):
        assert gone not in src
