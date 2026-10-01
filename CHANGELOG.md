# Changelog

## 0.2.0 — 2026-10-01

kasm-use now drives sessions through KasmVNC directly instead of `xdotool`.

- **Input over VNC.** Clicks, typing, keys and scrolling go to the session's KasmVNC server through
  Kasm's proxy. No more installing `xdotool` as root at session start, so `kasm_start` returns as
  soon as the session is running (about 90 s faster), and it works with any image.
- **Live screenshots over VNC.** `kasm_look` reads the framebuffer directly (~1 s, PNG). Kasm's
  screenshot API returned stale frames whenever nobody was viewing the session, which made agents
  retry actions that had already worked.
- **4–5x faster end to end.** A three-workspace demo (Chrome to a website, Terminal commands,
  Minetest world creation) went from 191–402 s per workspace to 39–77 s, with no retries.
- Sessions started outside kasm-use fall back to the 0.1.0 behaviour (exec + `xdotool`).
- Still standard-library only, so it installs with `--no-deps` alongside other agents.

## 0.1.0 — 2026-09-30

First release: eight tools for starting, viewing and driving Kasm sessions (screenshot API +
`xdotool` via exec); stdio and streamable-HTTP transports; Hermes Agent plugin.
