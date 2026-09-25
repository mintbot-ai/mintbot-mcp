# mintbot MCP server installed

The server listens on `http://127.0.0.1:8650/mcp` on this VPS and accepts only
requests that carry its bearer token. Nothing is exposed to the internet.

Get a ready-to-paste client configuration (Claude Desktop, Claude Code, Cursor):

```bash
/opt/mintbot-mcp/bin/mintbot-mcp client-config
```

Reach it from your own machine through an SSH tunnel:

```bash
ssh -N -L 8650:127.0.0.1:8650 root@<your agent host>
```

and point the MCP client at `http://127.0.0.1:8650/mcp` with the header
`Authorization: Bearer <token>` (`mintbot-mcp token` prints it; `--rotate`
replaces it).

Check the backends the server talks to: `/opt/mintbot-mcp/bin/mintbot-mcp check`.
