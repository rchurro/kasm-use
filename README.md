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
| `kasm_start` | Start a workspace (e.g. `Chrome`) — takes ~2 min, see [Limitations](#limitations) |
| `kasm_look` | Screenshot of the session, returned as an image |
| `kasm_click` | Click at x,y in the latest screenshot's coordinates |
| `kasm_type` | Type text into the focused field |
| `kasm_key` | Keys/shortcuts, e.g. `ctrl+l`, `Return`, `Tab` |
| `kasm_scroll` | Scroll, optionally at a point |
| `kasm_stop` | Destroy the session |

## Requirements

- Kasm Workspaces **1.19 or newer** (the screenshot API needs 1.19 images).
- A Kasm **API key**: Admin → Settings → Developers → API Keys → Add. Grant it only the session
  permissions (create/list/destroy sessions, screenshot, exec). It does not need user or admin rights.
- Your Kasm **user ID**, so sessions belong to you: Admin → Access Management → Users → open your user;
  the ID is in the page URL.
- A workspace image based on Debian/Ubuntu (all official `kasmweb/*` images are).

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

- **First start is slow (~90 s extra).** Stock images don't include `xdotool`, so `kasm_start`
  installs it in the session as root. Reusing a running session skips this.
- **Screenshots can lag** a few seconds behind actions; the agent is told to look again to confirm.
- **Kasm's exec API returns no output**, so actions report "sent", not "succeeded". The next
  screenshot is the confirmation.
- **CAPTCHAs, passwords, MFA and payments are handed to you.** The tool descriptions tell the agent
  never to type them; open the session in Kasm and take over.
- It's slow compared to a local browser tool: every step is a screenshot round trip.

## How it works

- **Eyes:** `POST /api/public/get_kasm_screenshot` (KasmVNC renders a JPEG).
- **Hands:** `POST /api/public/exec_command_kasm` running `xdotool` inside the session.
- Clicks are given in screenshot pixels and scaled to the real desktop size inside the session
  (`xdotool getdisplaygeometry`), using the JPEG's actual dimensions — Kasm keeps the desktop's
  aspect ratio, so the image is often not the size requested.

## License

MIT
