"""Kasm Workspaces desktop control tools — work with any stock Kasm image.

Shared by the MCP server (kasm_use.server) and the Hermes Agent plugin (register() below).

Eyes and hands are the session's own KasmVNC server, reached through Kasm's proxy (see vnc.py):
live framebuffer reads for screenshots, VNC pointer/key events for input.

Scope: every tool acts ONLY on sessions this process started with kasm_start. Two Kasm facts make
that the important boundary:
- A Kasm API key is server-wide. With the permissions kasm-use needs it can list, view and stop any
  user's sessions, and "Users Auth Session" lets it log in on behalf of any user.
- The session_token request_kasm returns is a login token for KASM_USER_ID, not for one session:
  it opens every session that user has.
So kasm-use never accepts a session it didn't start, never returns tokens or keys to the model, and
doesn't use Kasm's exec or screenshot APIs at all.

Configuration comes from the environment only:
  KASM_API_URL, KASM_API_KEY, KASM_API_KEY_SECRET  - a Kasm API key (see README for permissions)
  KASM_USER_ID       - the Kasm user sessions are created for, so the owner can watch/take over
  KASM_VERIFY_TLS    - "false" to accept a self-signed Kasm certificate (default: verify)

Coordinates: every click/scroll uses the coordinate space of the most recent kasm_look image for
that session. The desktop resizes when someone opens the session in their browser, so clicks are
scaled to the size VNC reports on each connection.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

from . import vnc

TOOLSET = "kasm"
_REQUIRED_ENV = ["KASM_API_URL", "KASM_API_KEY", "KASM_API_KEY_SECRET", "KASM_USER_ID"]
_CTX = (ssl.create_default_context() if os.environ.get("KASM_VERIFY_TLS", "true").lower() not in ("0", "false", "no")
        else ssl._create_unverified_context())

# Sessions this process started: kasm_id (no dashes) -> {"username", "session_token", "path",
# "workspace", "started"}. The allowlist for every tool. In memory only — never logged or returned
# to the model; a restart forgets them (the sessions keep running and can be stopped in Kasm).
_SESSIONS: dict[str, dict] = {}
_LOCK = threading.Lock()

# kasm_id -> (image_width, image_height) of the last screenshot shown to the model.
_LAST_SHOT: dict[str, tuple[int, int]] = {}


# ----------------------------------------------------------------------------------- API
def _call(endpoint: str, **payload):
    body = dict(api_key=os.environ["KASM_API_KEY"], api_key_secret=os.environ["KASM_API_KEY_SECRET"], **payload)
    req = urllib.request.Request(
        f"{os.environ['KASM_API_URL'].rstrip('/')}/api/public/{endpoint}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        data = urllib.request.urlopen(req, timeout=120, context=_CTX).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Kasm {endpoint} HTTP {e.code}: {e.read()[:200]!r}") from None
    parsed = json.loads(data or b"{}")
    if isinstance(parsed, dict) and parsed.get("error_message"):
        raise RuntimeError(f"Kasm {endpoint}: {parsed['error_message']}")
    return parsed


def _user() -> str:
    return os.environ["KASM_USER_ID"]


def _kid(kasm_id: str) -> str:
    return str(kasm_id).replace("-", "").strip().lower()


def _ok(**fields) -> str:
    return json.dumps({"ok": True, **fields})


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


def _resolve_session(args: dict) -> str:
    """The session to act on — always one this process started."""
    with _LOCK:
        mine = list(_SESSIONS)
    requested = _kid(args.get("session_id") or "")
    if requested:
        if requested not in mine:
            raise PermissionError(
                "kasm-use only controls sessions it started with kasm_start in this run. "
                "That session wasn't started here (or kasm-use restarted since); start a new one.")
        return requested
    if not mine:
        raise RuntimeError("No session started yet. Start one with kasm_start first.")
    return max(mine, key=lambda k: _SESSIONS[k]["started"])


def _session(kid: str) -> vnc.Session:
    creds = _SESSIONS[kid]
    if not creds.get("path"):
        status = _call("get_kasm_status", kasm_id=kid, user_id=_user()).get("kasm") or {}
        creds["path"] = ((status.get("port_map") or {}).get("vnc") or {}).get("path")
        if not creds["path"]:
            raise RuntimeError("Kasm did not report a VNC path for this session.")
    return vnc.Session(os.environ["KASM_API_URL"], creds["path"], creds["username"], creds["session_token"], _CTX)


def _to_desktop(kid: str, x, y, width: int, height: int) -> tuple[int, int]:
    w, h = _LAST_SHOT.get(kid, (0, 0))
    if not w:
        raise RuntimeError("Call kasm_look first so coordinates can be mapped to the screen.")
    x, y = max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))
    return x * width // w, y * height // h


# --------------------------------------------------------------------------------- tools
def kasm_list(args, **kw):
    try:
        images = [{"workspace": i.get("friendly_name"), "image": i.get("name")}
                  for i in _call("get_images").get("images", []) if i.get("enabled")]
        running = {_kid(k["kasm_id"]): k.get("operational_status") for k in _call("get_kasms").get("kasms", [])}
        with _LOCK:
            for kid in [k for k in _SESSIONS if k not in running]:  # stopped elsewhere (e.g. dashboard)
                _SESSIONS.pop(kid, None)
                _LAST_SHOT.pop(kid, None)
            sessions = [{"session_id": kid, "workspace": s["workspace"], "status": running.get(kid)}
                        for kid, s in _SESSIONS.items()]
        return _ok(workspaces=images, sessions=sessions,
                   note="Only sessions started by kasm-use in this run are listed or controllable.")
    except Exception as e:
        return _err(str(e))


def kasm_start(args, **kw):
    workspace = (args.get("workspace") or "Chrome").strip().lower()
    try:
        images = [i for i in _call("get_images").get("images", []) if i.get("enabled")]
        matches = [i for i in images if workspace in (i.get("friendly_name") or "").lower()]
        if not matches:
            return _err(f"No workspace matching {workspace!r}. Available: "
                        + ", ".join(sorted({i.get('friendly_name') for i in images})))
        # Prefer the newest image tag when the same workspace exists more than once.
        img = sorted(matches, key=lambda i: i.get("name") or "")[-1]
        started = _call("request_kasm", image_id=img["image_id"], user_id=_user(), enable_sharing=False)
        kid = _kid(started["kasm_id"])
        if not (started.get("session_token") and started.get("username")):
            try:  # don't leave an uncontrollable session running
                _call("destroy_kasm", kasm_id=kid, user_id=_user())
            except Exception:
                pass
            return _err("Kasm did not return a session login token (needs Kasm 1.19+ and the API key's "
                        "'Users Auth Session' permission), so kasm-use can't control the session.")
        with _LOCK:
            _SESSIONS[kid] = {"username": started["username"], "session_token": started["session_token"],
                              "workspace": img.get("friendly_name"), "started": time.time()}
        for _ in range(60):
            status = (_call("get_kasm_status", kasm_id=kid, user_id=_user()).get("kasm") or {}).get("operational_status")
            if status == "running":
                break
            time.sleep(5)
        else:
            return _err(f"Session {kid} did not reach 'running'.")
        return _ok(session_id=kid, workspace=img.get("friendly_name"), image=img.get("name"),
                   note="Session is running and visible in the owner's Kasm dashboard. Call kasm_look next.")
    except Exception as e:
        return _err(str(e))


def kasm_look(args, **kw):
    try:
        kid = _resolve_session(args)
        time.sleep(float(args.get("wait_seconds", 2)))
        with _session(kid) as s:
            png, size = s.screenshot(), (s.width, s.height)
        _LAST_SHOT[kid] = size
        summary = (f"Screenshot of Kasm session {kid[:8]} ({size[0]}x{size[1]}, live). Give kasm_click / "
                   f"kasm_scroll coordinates in THIS image's pixels.")
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ],
            "text_summary": summary,
        }
    except Exception as e:
        return _err(str(e))


def kasm_click(args, **kw):
    try:
        kid = _resolve_session(args)
        button = {"left": 1, "middle": 2, "right": 3}.get(args.get("button", "left"), 1)
        with _session(kid) as s:
            s.click(*_to_desktop(kid, args["x"], args["y"], s.width, s.height), button,
                    2 if args.get("double") else 1)
        return _ok(session_id=kid, clicked=[args["x"], args["y"]])
    except Exception as e:
        return _err(str(e))


def kasm_type(args, **kw):
    text = args.get("text", "")
    if not text:
        return _err("text is required")
    try:
        kid = _resolve_session(args)
        with _session(kid) as s:
            s.type(text)
            if args.get("press_enter"):
                s.combo([vnc.keysym("Return")])
        return _ok(session_id=kid, typed_chars=len(text), pressed_enter=bool(args.get("press_enter")))
    except Exception as e:
        return _err(str(e))


_KEY_RE = re.compile(r"^[A-Za-z0-9_+]+$")


def kasm_key(args, **kw):
    keys = [k for k in re.split(r"[\s,]+", args.get("keys", "")) if k]
    bad = [k for k in keys if not _KEY_RE.match(k)]
    if not keys or bad:
        return _err(f"keys must be key names like ctrl+l, Return, Tab, Escape (invalid: {bad})")
    try:
        combos = [[vnc.keysym(part) for part in k.split("+") if part] for k in keys]
        kid = _resolve_session(args)
        with _session(kid) as s:
            for combo in combos:
                s.combo(combo)
        return _ok(session_id=kid, keys=keys)
    except Exception as e:
        return _err(str(e))


def kasm_scroll(args, **kw):
    try:
        kid = _resolve_session(args)
        amount = max(1, min(int(args.get("amount", 5)), 30))
        direction = args.get("direction", "down")
        if direction not in ("up", "down", "left", "right"):
            return _err("direction must be up, down, left or right")
        with _session(kid) as s:
            if "x" in args and "y" in args:
                x, y = _to_desktop(kid, args["x"], args["y"], s.width, s.height)
            else:
                x, y = s.width // 2, s.height // 2
            s.scroll(x, y, direction, amount)
        return _ok(session_id=kid, direction=direction, amount=amount)
    except Exception as e:
        return _err(str(e))


def kasm_stop(args, **kw):
    try:
        kid = _resolve_session(args)
        _call("destroy_kasm", kasm_id=kid, user_id=_user())
        with _LOCK:
            _SESSIONS.pop(kid, None)
            _LAST_SHOT.pop(kid, None)
        return _ok(session_id=kid, stopped=True)
    except Exception as e:
        return _err(str(e))


# ------------------------------------------------------------------------------ schemas
_SID = {"session_id": {"type": "string",
                       "description": "A session started by kasm_start. Omit to use the most recent one."}}

_TOOLS = [
    ("kasm_list", kasm_list, "List Kasm workspaces you can start, and the sessions you started.",
     {"type": "object", "properties": {}}),
    ("kasm_start", kasm_start,
     "Start a Kasm workspace session (e.g. 'Chrome', 'Terminal') as the owner, so they can watch it. "
     "Returns once the session is running; then call kasm_look. Only sessions you start can be controlled.",
     {"type": "object", "properties": {"workspace": {"type": "string", "description": "Workspace name, e.g. Chrome"}}}),
    ("kasm_look", kasm_look,
     "Live screenshot of your Kasm session. Always look before clicking, and look again after "
     "each action to confirm it worked.",
     {"type": "object", "properties": {**_SID, "wait_seconds": {"type": "number", "description": "Pause before capturing (default 2)."}}}),
    ("kasm_click", kasm_click, "Click at x,y in the coordinate space of the latest kasm_look image.",
     {"type": "object", "required": ["x", "y"], "properties": {
         **_SID, "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean"}}}),
    ("kasm_type", kasm_type,
     "Type text into the focused field of the Kasm session (click the field first). NEVER type passwords, "
     "MFA codes, or payment details — stop and ask the owner to take over the session for those.",
     {"type": "object", "required": ["text"], "properties": {
         **_SID, "text": {"type": "string"}, "press_enter": {"type": "boolean"}}}),
    ("kasm_key", kasm_key, "Press keys/shortcuts in the Kasm session, e.g. 'ctrl+l', 'Return', 'Tab', 'Escape'.",
     {"type": "object", "required": ["keys"], "properties": {**_SID, "keys": {"type": "string"}}}),
    ("kasm_scroll", kasm_scroll, "Scroll the Kasm session, optionally at x,y from the latest kasm_look image.",
     {"type": "object", "properties": {
         **_SID, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "amount": {"type": "integer", "description": "Wheel clicks, 1-30 (default 5)"},
         "x": {"type": "integer"}, "y": {"type": "integer"}}}),
    ("kasm_stop", kasm_stop, "Stop (destroy) a session you started, when the task is finished or the owner asks.",
     {"type": "object", "properties": {**_SID}}),
]


def _available() -> bool:
    return all(os.environ.get(v) for v in _REQUIRED_ENV)


def register(ctx) -> None:
    """Hermes Agent plugin entry point (see hermes-plugin/)."""
    for name, handler, description, params in _TOOLS:
        ctx.register_tool(
            name=name, toolset=TOOLSET,
            schema={"name": name, "description": description, "parameters": params},
            handler=handler, check_fn=_available, requires_env=_REQUIRED_ENV,
            description=description, emoji="🖥️",
        )
