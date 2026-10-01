# kasm-use

Let an AI agent use a [Kasm Workspaces](https://kasmweb.com) desktop: start a session, look at the
screen, click, type, press keys, scroll, and stop it — in plain language, from any MCP client
(Claude Code, Claude Desktop, OpenCode, Cursor, …) or as a native [Hermes Agent](#hermes-agent) plugin.

Other Kasm MCP servers manage sessions (start, list, stop). **kasm-use actually uses them**: the agent
sees the desktop and operates it, like computer-use or browser-use, inside your Kasm.

- **Works with stock Kasm images.** Nothing is baked into the workspace; it drives the desktop
  through Kasm's own API.
- **You can watch and take over.** Sessions are created as *your* Kasm user, so they show up in
  your Kasm dashboard. Open one to watch the agent work, or to handle a login or CAPTCHA yourself.
- **Your Kasm, your key.** You run the server next to your own Kasm. Nothing goes through a third party.

## Tools

| Tool | What it does |
| --- | --- |
| `kasm_list` | Workspaces you can start, and your running sessions |
| `kasm_start` | Start a workspace (e.g. `Chrome`); returns once the session is running |
| `kasm_look` | Live screenshot of the session, returned as an image (~1 s) |
| `kasm_click` | Click at x,y in the latest screenshot's coordinates |
| `kasm_type` | Type text into the focused field |
| `kasm_key` | Keys/shortcuts, e.g. `ctrl+l`, `Return`, `Tab` |
| `kasm_scroll` | Scroll, optionally at a point |
| `kasm_stop` | Destroy the session |

## Requirements

- Kasm Workspaces **1.19 or newer** (tested on 1.19.0).
- A Kasm **API key**: Admin → Settings → Developers → API Keys → Add. Grant it only the session
  permissions (create/list/destroy sessions, screenshot, exec). It does not need user or admin rights.
- Your Kasm **user ID**, so sessions belong to you: Admin → Access Management → Users → open your user;
  the ID is in the page URL.
- Any workspace image. Nothing is installed in the session for sessions kasm-use starts.

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `KASM_API_URL` | yes | e.g. `https://kasm.example.com` |
| `KASM_API_KEY` / `KASM_API_KEY_SECRET` | yes | the API key pair |
| `KASM_USER_ID` | yes | the Kasm user sessions are created for |
| `KASM_VERIFY_TLS` | no | `false` to accept a self-signed Kasm certificate (default `true`) |
| `KASM_USE_TOKEN` | HTTP only | bearer token clients must send |

## Run it

### Locally (stdio) — the usual way

Your MCP client starts the server itself. With [uv](https://docs.astral.sh/uv/) installed:

```json
{
  "mcpServers": {
    "kasm": {
      "command": "uvx",
      "args": ["kasm-use"],
      "env": {
        "KASM_API_URL": "https://kasm.example.com",
        "KASM_API_KEY": "…",
        "KASM_API_KEY_SECRET": "…",
        "KASM_USER_ID": "…"
      }
    }
  }
}
```

Claude Code:

```bash
claude mcp add kasm -e KASM_API_URL=https://kasm.example.com -e KASM_API_KEY=… -e KASM_API_KEY_SECRET=… -e KASM_USER_ID=… -- uvx kasm-use
```

### As a service (streamable HTTP)

```bash
docker run -d -p 8000:8000 \
  -e KASM_API_URL=https://kasm.example.com -e KASM_API_KEY=… -e KASM_API_KEY_SECRET=… \
  -e KASM_USER_ID=… -e KASM_USE_TOKEN="$(openssl rand -hex 32)" \
  ghcr.io/rchurro/kasm-use:latest
```

Clients connect to `http://host:8000/mcp` with the header `Authorization: Bearer <KASM_USE_TOKEN>`.
Health check: `GET /healthz`. Run **one replica** per Kasm user: the server remembers each
session's last screenshot size in memory to map click coordinates.

Keep it on a private network (LAN, VPN, Tailscale). Anyone with the token can drive your desktops.

### Hermes Agent

```bash
pip install kasm-use                      # into Hermes's Python environment
cp -r hermes-plugin/kasm ~/.hermes/plugins/kasm
```

Enable `kasm` under `plugins.enabled`, set the environment variables above, and start a new
Hermes session (`/new`) so the tools load.

## Limitations

- **Full control needs sessions kasm-use started.** Kasm only hands out the per-session login
  token that VNC needs when a session is created, so kasm-use keeps it (in memory) for sessions
  it starts. Sessions started elsewhere fall back to Kasm's exec API + `xdotool`, which must
  already be in the image, and to Kasm's screenshot API, which is only fresh while someone is
  viewing the session. Restarting the server forgets the tokens.
- **Input reports "sent", not "succeeded".** The next screenshot is the confirmation.
- **CAPTCHAs, passwords, MFA and payments are handed to you.** The tool descriptions tell the agent
  never to type them; open the session in Kasm and take over.
- It's slow compared to a local browser tool: every step is a screenshot round trip.

## How it works

kasm-use talks to each session's KasmVNC server directly, through Kasm's own proxy
(`wss://<kasm>/desktop/<id>/vnc/websockify`), with the login token `request_kasm` returns.
The VNC client is built in and uses only the Python standard library.

- **Eyes:** a full framebuffer read over VNC (ZRLE, decoded with `zlib`), returned as PNG.
  Kasm's screenshot API isn't used for these sessions: it only refreshes while a viewer is
  connected, so with nobody watching it returns stale frames.
- **Hands:** VNC pointer and key events. KasmVNC's pointer message is 11 bytes (16-bit button
  mask plus scroll deltas), not standard RFB's 6.
- Clicks are given in screenshot pixels and scaled to the desktop's current size, which VNC reports
  on connect — it changes when someone opens the session and Kasm resizes it to their window.
- Each tool call opens a short VNC connection and does a round trip before closing, so no input
  is lost on disconnect. The owner can stay connected at the same time.

## License

MIT
