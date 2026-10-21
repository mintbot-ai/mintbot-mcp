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

    access = sub.add_parser('access', help='issue, list or revoke temporary restricted credentials')
    actions = access.add_subparsers(dest='access_action', required=True)
    issue = actions.add_parser('issue', help='print a new grant and its token (shown once)')
    issue.add_argument('--name', required=True)
    issue.add_argument('--ttl', required=True, help='lifetime, e.g. 30m, 2h, 7d (maximum 365d)')
    issue.add_argument('--tool', action='append', default=[], help='allowed tool name; repeatable')
    issue.add_argument('--group', action='append', default=[], choices=['chat', 'projects'])
    issue.add_argument('--project', action='append', default=None, help='allowed project ID; repeatable')
    actions.add_parser('list', help='list grant metadata, never secrets')
    revoke = actions.add_parser('revoke', help='immediately revoke a grant by ID')
    revoke.add_argument('credential_id')

    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ConfigError as exc:
        print(f"mintbot-mcp: {exc}", file=sys.stderr)
        return 2


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == 'access':
        from .access import GrantStore
        from .config import grants_file
        import re

        store = GrantStore(grants_file())
        try:
            if args.access_action == 'issue':
                match = re.fullmatch(r'([0-9]+)([smhd]?)', args.ttl)
                if not match:
                    raise ValueError('ttl must be seconds or an integer followed by s, m, h or d')
                ttl = int(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
                grant, secret = store.issue(args.name, tools=args.tool, groups=args.group,
                                            project_ids=args.project, ttl=ttl)
                print(json.dumps({**grant, 'token': secret}, indent=2))
            elif args.access_action == 'list':
                print(json.dumps(store.list(), indent=2))
            elif not store.revoke(args.credential_id):
                raise ValueError('grant ID not found')
            else:
                print(json.dumps({'revoked': args.credential_id}))
        except (ValueError, OSError, KeyError, TypeError):
            print('mintbot-mcp: invalid grant options or inaccessible grant state', file=sys.stderr)
            return 2
        return 0
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
    from .access import GrantStore
    from .config import grants_file
    from .notifications import ConnectionNotifier

    app = build_http_app(server, ensure_token(settings.token_file),
                         grant_store=GrantStore(grants_file()), notifier=ConnectionNotifier(settings))
    # Peer notices report the observed socket peer, not caller-controlled X-Forwarded-For.
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", proxy_headers=False)


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
