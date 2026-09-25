"""The ``mintbot-mcp`` command: serve (HTTP), stdio, token, client-config, check."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .config import ConfigError, Settings, ensure_token, load_settings, token_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mintbot-mcp",
        description="MCP server for this mintbot agent: chat with the agent and manage its settings from any MCP client.",
    )
    parser.add_argument("--version", action="version", version=f"mintbot-mcp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the MCP server over streamable HTTP (bearer-authenticated)")
    serve.add_argument("--host", help="listen address (default 127.0.0.1 or MINTBOT_MCP_HOST)")
    serve.add_argument("--port", type=int, help="listen port (default 8650 or MINTBOT_MCP_PORT)")

    sub.add_parser("stdio", help="run the MCP server over stdio for a local MCP client")

    token = sub.add_parser("token", help="print the bearer token MCP clients must present (created on first use)")
    token.add_argument("--rotate", action="store_true", help="replace the token; every client must be reconfigured")

    config = sub.add_parser("client-config", help="print a ready-to-paste MCP client configuration")
    config.add_argument("--url", help="the URL clients reach the server at (default: the local listener)")

    sub.add_parser("check", help="verify the agent backends are reachable (exit 1 when not)")

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ConfigError as exc:
        print(f"mintbot-mcp: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "token":
        path = token_file()
        if args.rotate and path.exists():
            path.unlink()
        print(ensure_token(path))
        return 0
    settings = load_settings()
    if args.command == "serve":
        _serve(settings.with_listener(args.host, args.port))
        return 0
    if args.command == "stdio":
        _stdio(settings)
        return 0
    if args.command == "client-config":
        url = args.url or f"http://{settings.host}:{settings.port}/mcp"
        print(json.dumps(client_config(url, ensure_token(settings.token_file)), indent=2))
        return 0
    if args.command == "check":
        return _check(settings)
    raise AssertionError(args.command)


def client_config(url: str, token: str) -> dict:
    """The ``mcpServers`` block Claude Desktop, Claude Code and Cursor understand."""
    return {"mcpServers": {"mintbot": {"type": "http", "url": url,
                                        "headers": {"Authorization": f"Bearer {token}"}}}}


def _serve(settings: Settings) -> None:
    import uvicorn

    from .app import build_http_app
    from .server import build_server, make_backends

    backends = make_backends(settings)
    server = build_server(settings, backends)
    app = build_http_app(server, ensure_token(settings.token_file))
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


def _stdio(settings: Settings) -> None:
    from .server import build_server, make_backends

    backends = make_backends(settings)
    build_server(settings, backends).run("stdio")


def _check(settings: Settings) -> int:
    results = asyncio.run(probe(settings))
    for name, verdict in results.items():
        print(f"{name}: {verdict}")
    return 0 if results["hermes"] == "ok" and results["local_api"] == "ok" else 1


async def probe(settings: Settings) -> dict[str, str]:
    from .server import make_backends

    backends = make_backends(settings)
    results: dict[str, str] = {}
    try:
        for name, call in (("hermes", backends.hermes.health), ("local_api", backends.local.health),
                           ("central", backends.central.get_name)):
            try:
                await call()
                results[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                results[name] = f"error: {exc}"
    finally:
        await backends.aclose()
    return results
