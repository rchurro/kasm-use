"""Kasm Workspaces desktop control tools — work with any stock Kasm image.

Shared by the MCP server (kasm_use.server) and the Hermes Agent plugin (register() below).

Eyes  : POST /api/public/get_kasm_screenshot            (KasmVNC in the session renders a JPEG)
Hands : POST /api/public/exec_command_kasm + xdotool     (installed into the session at start;
                                                          stock images don't ship it)

Configuration comes from the environment only:
  KASM_API_URL, KASM_API_KEY, KASM_API_KEY_SECRET  - a Kasm API key (session permissions only)
  KASM_USER_ID       - the Kasm user sessions are created for, so the owner can watch/take over
  KASM_VERIFY_TLS    - "false" to accept a self-signed Kasm certificate (default: verify)

Coordinates: every click/scroll uses the coordinate space of the most recent kasm_look image for
that session. The real desktop is usually much larger (and changes when the owner connects and
KasmVNC resizes it), so we scale inside the session using `xdotool getdisplaygeometry`.

Lessons baked in:
- exec_command_kasm is fire-and-forget: it never returns command output.
- The screenshot keeps the desktop's aspect ratio, so the returned JPEG is NOT the requested
  size — always read its real dimensions (a 1280x720 request on a 3840x2008 desktop gives
  1280x669; scaling Y by 720 put clicks ~35 real px too high).
- Screenshots right after an action can be stale for a few seconds.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
import struct
import time
import urllib.error
import urllib.request

TOOLSET = "kasm"
_REQUIRED_ENV = ["KASM_API_URL", "KASM_API_KEY", "KASM_API_KEY_SECRET", "KASM_USER_ID"]
_SHOT_W, _SHOT_H = 1280, 800
_INSTALL_WAIT_S = 90
_CTX = (ssl.create_default_context() if os.environ.get("KASM_VERIFY_TLS", "true").lower() not in ("0", "false", "no")
        else ssl._create_unverified_context())

# kasm_id -> (image_width, image_height) of the last screenshot shown to the model.
# Per process: one server instance serves one Kasm user.
_LAST_SHOT: dict[str, tuple[int, int]] = {}


# ----------------------------------------------------------------------------------- API
def _call(endpoint: str, raw: bool = False, **payload):
    body = dict(api_key=os.environ["KASM_API_KEY"], api_key_secret=os.environ["KASM_API_KEY_SECRET"], **payload)
    req = urllib.request.Request(
        f"{os.environ['KASM_API_URL'].rstrip('/')}/api/public/{endpoint}",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=120, context=_CTX)
        data = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Kasm {endpoint} HTTP {e.code}: {e.read()[:200]!r}") from None
    if raw:
        return data
    parsed = json.loads(data or b"{}")
    if isinstance(parsed, dict) and parsed.get("error_message"):
        raise RuntimeError(f"Kasm {endpoint}: {parsed['error_message']}")
    return parsed


def _user() -> str:
    return os.environ["KASM_USER_ID"]


def _exec(kasm_id: str, cmd: str, env: dict | None = None, root: bool = False) -> None:
    cfg = {"cmd": cmd, "environment": {"DISPLAY": ":1", **(env or {})}}
    if root:
        cfg["user"] = "root"
    _call("exec_command_kasm", kasm_id=kasm_id, user_id=_user(), exec_config=cfg)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            h, w = struct.unpack(">HH", data[i + 5:i + 9])
            return w, h
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return None


def _ok(**fields) -> str:
    return json.dumps({"ok": True, **fields})


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg})


def _resolve_session(args: dict) -> str:
    kid = (args.get("session_id") or "").strip()
    if kid:
        return kid
    mine = [k for k in _call("get_kasms").get("kasms", [])
            if str(k.get("user_id", "")).replace("-", "") == _user().replace("-", "")]
    if not mine:
        raise RuntimeError("No running Kasm session. Start one with kasm_start first.")
    return mine[-1]["kasm_id"]


# --------------------------------------------------------------------------------- tools
def kasm_list(args, **kw):
    try:
        images = [{"workspace": i.get("friendly_name"), "image": i.get("name")}
                  for i in _call("get_images").get("images", []) if i.get("enabled")]
        sessions = [{"session_id": k["kasm_id"], "workspace": (k.get("image") or {}).get("friendly_name"),
                     "status": k.get("operational_status")}
                    for k in _call("get_kasms").get("kasms", [])
                    if str(k.get("user_id", "")).replace("-", "") == _user().replace("-", "")]
        return _ok(workspaces=images, sessions=sessions)
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
        kid = started["kasm_id"]
        for _ in range(60):
            status = (_call("get_kasm_status", kasm_id=kid, user_id=_user()).get("kasm") or {}).get("operational_status")
            if status == "running":
                break
            time.sleep(5)
        else:
            return _err(f"Session {kid} did not reach 'running'.")
        # Hands: stock images lack xdotool; install it (Debian/Ubuntu images). Fire-and-forget,
        # so wait for apt to finish.
        _exec(kid, "sh -c 'command -v xdotool >/dev/null || { apt-get update -qq && "
                   "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq xdotool; } >/tmp/.kasm-use-xdotool.log 2>&1'",
              root=True)
        time.sleep(_INSTALL_WAIT_S)
        return _ok(session_id=kid, workspace=img.get("friendly_name"), image=img.get("name"),
                   note="Session is running and visible in the owner's Kasm dashboard. Call kasm_look next.")
    except Exception as e:
        return _err(str(e))


def kasm_look(args, **kw):
    try:
        kid = _resolve_session(args)
        time.sleep(float(args.get("wait_seconds", 2)))
        img = b""
        for _ in range(8):
            img = _call("get_kasm_screenshot", raw=True, kasm_id=kid, user_id=_user(), width=_SHOT_W, height=_SHOT_H)
            if img[:2] == b"\xff\xd8":
                break
            time.sleep(3)
        if img[:2] != b"\xff\xd8":
            return _err("Screenshot not available yet (the desktop may still be starting). Try again.")
        size = _jpeg_size(img) or (_SHOT_W, _SHOT_H)
        _LAST_SHOT[kid] = size
        summary = (f"Screenshot of Kasm session {kid[:8]} ({size[0]}x{size[1]}). Give kasm_click / kasm_scroll "
                   f"coordinates in THIS image's pixels. The view can lag a few seconds behind actions.")
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": summary},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(img).decode()}},
            ],
            "text_summary": summary,
        }
    except Exception as e:
        return _err(str(e))


def _scaled_xy(kid: str, x: int, y: int) -> tuple[str, str]:
    w, h = _LAST_SHOT.get(kid, (0, 0))
    if not w:
        raise RuntimeError("Call kasm_look first so coordinates can be mapped to the screen.")
    x, y = max(0, min(int(x), w - 1)), max(0, min(int(y), h - 1))
    return f"$(( {x} * WIDTH / {w} ))", f"$(( {y} * HEIGHT / {h} ))"


def kasm_click(args, **kw):
    try:
        kid = _resolve_session(args)
        sx, sy = _scaled_xy(kid, args["x"], args["y"])
        button = {"left": 1, "middle": 2, "right": 3}.get(args.get("button", "left"), 1)
        repeat = "--repeat 2 " if args.get("double") else ""
        _exec(kid, f"sh -c 'eval $(xdotool getdisplaygeometry --shell); "
                   f"xdotool mousemove {sx} {sy} sleep 0.1 click {repeat}{button}'")
        return _ok(session_id=kid, clicked=[args["x"], args["y"]])
    except Exception as e:
        return _err(str(e))


def kasm_type(args, **kw):
    text = args.get("text", "")
    if not text:
        return _err("text is required")
    try:
        kid = _resolve_session(args)
        # Text travels as an env var, never inside the shell string: no quoting/injection issues.
        _exec(kid, "sh -c 'xdotool type --delay 35 -- \"$KASM_USE_TEXT\"'", env={"KASM_USE_TEXT": text})
        if args.get("press_enter"):
            time.sleep(min(0.05 * len(text) + 0.5, 15))
            _exec(kid, "sh -c 'xdotool key Return'")
        return _ok(session_id=kid, typed_chars=len(text), pressed_enter=bool(args.get("press_enter")))
    except Exception as e:
        return _err(str(e))


_KEY_RE = re.compile(r"^[A-Za-z0-9_+]+$")


def kasm_key(args, **kw):
    keys = [k for k in re.split(r"[\s,]+", args.get("keys", "")) if k]
    bad = [k for k in keys if not _KEY_RE.match(k)]
    if not keys or bad:
        return _err(f"keys must be xdotool key names like ctrl+l, Return, Tab, Escape (invalid: {bad})")
    try:
        kid = _resolve_session(args)
        _exec(kid, "sh -c 'xdotool key " + " ".join(keys) + "'")
        return _ok(session_id=kid, keys=keys)
    except Exception as e:
        return _err(str(e))


def kasm_scroll(args, **kw):
    try:
        kid = _resolve_session(args)
        button = {"up": 4, "down": 5, "left": 6, "right": 7}.get(args.get("direction", "down"), 5)
        amount = max(1, min(int(args.get("amount", 5)), 30))
        move = ""
        if "x" in args and "y" in args:
            sx, sy = _scaled_xy(kid, args["x"], args["y"])
            move = f"eval $(xdotool getdisplaygeometry --shell); xdotool mousemove {sx} {sy}; "
        _exec(kid, f"sh -c '{move}xdotool click --repeat {amount} {button}'")
        return _ok(session_id=kid, direction=args.get("direction", "down"), amount=amount)
    except Exception as e:
        return _err(str(e))


def kasm_stop(args, **kw):
    try:
        kid = _resolve_session(args)
        _call("destroy_kasm", kasm_id=kid, user_id=_user())
        _LAST_SHOT.pop(kid, None)
        return _ok(session_id=kid, stopped=True)
    except Exception as e:
        return _err(str(e))


# ------------------------------------------------------------------------------ schemas
_SID = {"session_id": {"type": "string", "description": "Kasm session id. Omit to use your newest running session."}}

_TOOLS = [
    ("kasm_list", kasm_list, "List Kasm workspaces you can start and your running sessions.",
     {"type": "object", "properties": {}}),
    ("kasm_start", kasm_start,
     "Start a Kasm workspace session (e.g. 'Chrome', 'Terminal') as the owner, so they can watch it. "
     "Takes ~2 minutes (it installs the input tool). Then call kasm_look.",
     {"type": "object", "properties": {"workspace": {"type": "string", "description": "Workspace name, e.g. Chrome"}}}),
    ("kasm_look", kasm_look,
     "Screenshot the Kasm session so you can see it. Always look before clicking, and look again after "
     "each action to confirm it worked.",
     {"type": "object", "properties": {**_SID, "wait_seconds": {"type": "number", "description": "Pause before capturing (default 2)."}}}),
    ("kasm_click", kasm_click, "Click at x,y in the coordinate space of the latest kasm_look image.",
     {"type": "object", "required": ["x", "y"], "properties": {
         **_SID, "x": {"type": "integer"}, "y": {"type": "integer"},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "double": {"type": "boolean"}}}),
    ("kasm_type", kasm_type,
     "Type text into the focused field of the Kasm session. NEVER type passwords, MFA codes, or payment "
     "details — stop and ask the owner to take over the session for those.",
     {"type": "object", "required": ["text"], "properties": {
         **_SID, "text": {"type": "string"}, "press_enter": {"type": "boolean"}}}),
    ("kasm_key", kasm_key, "Press keys/shortcuts in the Kasm session, e.g. 'ctrl+l', 'Return', 'Tab', 'Escape'.",
     {"type": "object", "required": ["keys"], "properties": {**_SID, "keys": {"type": "string"}}}),
    ("kasm_scroll", kasm_scroll, "Scroll the Kasm session, optionally at x,y from the latest kasm_look image.",
     {"type": "object", "properties": {
         **_SID, "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
         "amount": {"type": "integer", "description": "Wheel clicks, 1-30 (default 5)"},
         "x": {"type": "integer"}, "y": {"type": "integer"}}}),
    ("kasm_stop", kasm_stop, "Stop (destroy) a Kasm session when the task is finished or the owner asks.",
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
