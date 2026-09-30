"""kasm-use: Kasm Workspaces desktop control over MCP.

  kasm-use                              stdio (the client launches it; the usual way)
  kasm-use --transport http [--host 0.0.0.0] [--port 8000]
                                        streamable HTTP at /mcp; requires KASM_USE_TOKEN,
                                        sent by clients as `Authorization: Bearer <token>`
"""
import argparse
import asyncio
import contextlib
import hmac
import json
import os
import sys

from mcp import types
from mcp.server.lowlevel import Server

from . import __version__, tools

HANDLERS = {name: fn for name, fn, _, _ in tools._TOOLS}

server = Server("kasm", version=__version__, instructions=(
    "Operate Kasm Workspaces desktops. Typical loop: kasm_start (or reuse a running session "
    "from kasm_list) -> kasm_look -> act (click/type/key/scroll) -> kasm_look to confirm. "
    "Never type passwords, MFA codes or payment details; stop and ask the owner to take over. "
    "Stop at CAPTCHAs and hand them to the owner. Call kasm_stop when done."))


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [types.Tool(name=n, description=d, inputSchema=p) for n, _, d, p in tools._TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name not in HANDLERS:
        raise ValueError(f"Unknown tool {name}")
    # The tools are blocking (urllib + sleeps); keep the event loop free.
    result = await asyncio.to_thread(HANDLERS[name], arguments or {})
    if isinstance(result, dict) and result.get("_multimodal"):
        out = []
        for part in result["content"]:
            if part["type"] == "text":
                out.append(types.TextContent(type="text", text=part["text"]))
            elif part["type"] == "image_url":
                header, b64 = part["image_url"]["url"].split(",", 1)
                out.append(types.ImageContent(type="image", data=b64, mimeType=header[5:].split(";")[0]))
        return out
    parsed = json.loads(result)
    if not parsed.get("ok", True):
        # Surface tool errors as MCP tool errors so clients show them as failures.
        raise RuntimeError(parsed["error"])
    return [types.TextContent(type="text", text=result)]


def _check_env() -> None:
    missing = [v for v in tools._REQUIRED_ENV if not os.environ.get(v)]
    if missing:
        sys.exit(f"kasm-use: missing environment variables: {', '.join(missing)}")


async def _stdio() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _http(host: str, port: int) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    token = os.environ.get("KASM_USE_TOKEN")
    if not token:
        sys.exit("kasm-use: KASM_USE_TOKEN is required for --transport http")
    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    async def mcp_app(scope, receive, send):
        given = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if not hmac.compare_digest(given, f"Bearer {token}"):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    async def health(_request):
        return JSONResponse({"ok": True, "version": __version__, "tools": len(HANDLERS)})

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with manager.run():
            yield

    app = Starlette(routes=[Route("/healthz", health), Mount("/mcp", app=mcp_app)], lifespan=lifespan)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main() -> None:
    ap = argparse.ArgumentParser(prog="kasm-use", description="Kasm Workspaces desktop control over MCP")
    ap.add_argument("--transport", choices=["stdio", "http"], default=os.environ.get("KASM_USE_TRANSPORT", "stdio"))
    ap.add_argument("--host", default=os.environ.get("KASM_USE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("KASM_USE_PORT", "8000")))
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args()
    _check_env()
    if args.transport == "stdio":
        asyncio.run(_stdio())
    else:
        _http(args.host, args.port)


if __name__ == "__main__":
    main()
