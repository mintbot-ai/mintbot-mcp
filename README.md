# mintbot-mcp

An [MCP](https://modelcontextprotocol.io) server for a [mintbot](https://mintbot.ai)
agent, running on the agent's **own VPS**. Any MCP client - Claude Desktop,
Claude Code, Cursor, your own program - can chat with the agent and read or
change every mintbot setting the web panel offers. No central server is
involved: the daemon talks to the services that already run on the VPS.

Packaged as a signed [AXP](https://github.com/mintbot-ai/agent-extension)
extension, `mintbot.ai/mintbot-mcp`.

## How it fits

```text
MCP client (Claude Desktop, Claude Code, Cursor, ...)
        |  streamable HTTP + bearer token
        v
  mintbot-mcp  127.0.0.1:8650            <- this package, on the agent VPS
     |          |               \
     v          v                v
  Hermes API   panel-local     mintbot API (central; optional)
  :8642        :8643           display name, credit, model catalog
  chat turns,  every setting
  sessions     the panel has
```

- **Chat** goes through the stock Hermes API server on loopback and runs as
  the agent's own turns: same model, same credit, same session store as the
  panel and Telegram. Long turns are sliced (`chat_send` returns `running` +
  a `turn_id`, `chat_wait` collects the rest, `chat_cancel` interrupts).
- **Settings** go through `mintbot-panel-local`, the daemon the panel itself
  uses, as a trusted server-to-server caller. Model, persona, background jobs,
  notifications, extensions, Telegram bot, nightly updates, SSH keys, reboot.
- **Central** is reached only for the few values that live in the mintbot
  database (display name, credit balance, model catalog), exactly the way the
  agent's own tools reach them. Without central those tools answer
  `code: central_unavailable`; everything else keeps working.

Every tool returns structured JSON. A backend failure is never an MCP
protocol error: the client gets `{"ok": false, "error": ..., "code": ...}`.

## Tools

| group | tools |
|---|---|
| chat | `chat_send`, `chat_wait`, `chat_cancel`, `chat_sessions`, `chat_session_messages`, `chat_session_rename`, `chat_session_delete` |
| overview | `settings_overview` (everything in one call; also the `mintbot://agent` resource) |
| model | `model_get`, `models_catalog`, `model_set` |
| persona | `persona_get`, `persona_set` (replace / append) |
| name and credit | `agent_name_get`, `agent_name_set`, `usage_get` |
| jobs | `jobs_list`, `jobs_create`, `jobs_edit`, `jobs_pause`, `jobs_resume`, `jobs_remove`, `notifications_list`, `notifications_mark_seen` |
| extensions | `extensions_list`, `extensions_settings_get`, `extensions_settings_set`, `extensions_install` (preflight, then consented), `extensions_uninstall`, `extensions_policy_set`, `extensions_check_now`, `extensions_update_now` |
| telegram | `telegram_bot_get`, `telegram_bot_apply`, `telegram_bot_remove` |
| updates | `updates_get`, `updates_set`, `updates_run` |
| ssh | `ssh_keys_list`, `ssh_keys_add`, `ssh_keys_remove` |
| security | `security_state`, `security_unlock`, `security_lock` |
| server | `server_reboot` (requires `confirm=true`) |

Deliberately **not** exposed: entering BYOK API keys, setting up two-factor
login, the root password. Those stay in the panel, where the browser-only
two-factor gate protects them.

**Two-factor login.** When the owner has TOTP on, the browser-gated settings
(persona, Telegram bot, extension installs) answer `code:
"second_factor_required"`. `security_unlock` with a current authenticator
code mints a panel session that the daemon keeps in memory until
`security_lock` or a restart.

## Install

### On a mintbot agent (AXP host)

In chat: *"install mintbot-mcp"* (or the GitHub URL), or use the panel's
extensions card. The host shows the consent card, verifies the artifact digest
and the ed25519 signature, runs the lifecycle hooks in its sandbox, writes and
contains the systemd unit itself (the manifest binds the daemon with a
`command`), and keeps the extension updated from GitHub releases.

What the consent card shows, and why:

| declared | why |
|---|---|
| `network_egress` PyPI, `api.mintbot.ai`, `api.mintbot.dev` | `install.sh` pip-installs the pinned dependencies into a private virtualenv; at runtime only name / credit / model catalog are read from the mintbot API |
| `network_ingress` `127.0.0.1:8650/tcp` | the MCP endpoint itself, loopback only |
| `filesystem` `config:r`, `state:rw` | reads the Hermes home (API key, `mintbot:` block of `config.yaml`); writes only the bearer token into its state dir |
| `root: false` | the install prefix and state dir come from the host |
| signed by `mintbot.ai-mintbot-mcp-2026` | `ed25519:MGWTLv6h0j5d3mUnWZc0MzwZkhJQcmYVkJUMSLCXFno=`, listed in the publisher directory at `https://mintbot.ai/.well-known/agent-extension-keys.json` |

### Standalone (no AXP host)

```bash
git clone https://github.com/mintbot-ai/mintbot-mcp.git
cd mintbot-mcp
sudo ./install.sh
```

The installer is idempotent: private virtualenv + package under
`/opt/mintbot-mcp`, launcher `/opt/mintbot-mcp/bin/mintbot-mcp`, bearer token
in `/var/lib/axp/mintbot.ai/mintbot-mcp/token` (0600), and - as root on a
systemd machine - a plain `mintbot-mcp.service`. The scripts honour the AXP
host contract (`AXP_PREFIX`, `AXP_STATE_DIR`, `AXP_CACHE_DIR`, `AXP_PURGE`,
`AXP_FROM_VERSION`) and fall back to the spec defaults when the variables are
absent. `uninstall.sh` keeps the token (`AXP_PURGE=1` deletes it too).

The daemon expects a mintbot agent VPS: the Hermes API server on `:8642`
(`API_SERVER_KEY` in `$HERMES_HOME/env`) and `mintbot-panel-local` on `:8643`.
`mintbot-mcp check` tells you which backends it can reach.

## Connect a client

```bash
/opt/mintbot-mcp/bin/mintbot-mcp client-config
```

prints the `mcpServers` block Claude Desktop, Claude Code and Cursor accept:

```json
{
  "mcpServers": {
    "mintbot": {
      "type": "http",
      "url": "http://127.0.0.1:8650/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

The server listens on loopback only. From your own machine, open an SSH
tunnel and keep the URL as printed:

```bash
ssh -N -L 8650:127.0.0.1:8650 root@agent1234.mintbot.ai
```

A local MCP client on the VPS itself can also spawn `mintbot-mcp stdio`.

`mintbot-mcp token` prints the bearer token; `mintbot-mcp token --rotate`
replaces it (every client must be reconfigured). `/health` is the only
unauthenticated route.

## CLI

| command | what it does |
|---|---|
| `mintbot-mcp serve [--host] [--port]` | streamable-HTTP MCP server (default `127.0.0.1:8650`) |
| `mintbot-mcp stdio` | the same server over stdio |
| `mintbot-mcp token [--rotate]` | print (or replace) the bearer token |
| `mintbot-mcp client-config [--url]` | ready-to-paste client configuration |
| `mintbot-mcp check` | probe Hermes, panel-local and central; exit 1 when the loopback backends are down |

Environment overrides: `MINTBOT_MCP_HOST`, `MINTBOT_MCP_PORT`,
`MINTBOT_MCP_STATE_DIR` (or `AXP_STATE_DIR`), `MINTBOT_MCP_TOKEN_FILE`,
`MINTBOT_MCP_HERMES_URL`, `MINTBOT_MCP_LOCAL_API_URL`,
`MINTBOT_MCP_HERMES_API_KEY`, `HERMES_HOME`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .[dev] -e ../agent-extension   # axp: manifest + signature tests
pytest
```

The tests run the tool layer against fake backends (`httpx.MockTransport`),
the lifecycle scripts in a throwaway prefix with a fake `python3`, and check
the manifest against the AXP schema, the reference implementation and the
publisher key directory. No test touches the live agent, the network or the
signing key.

Release (maintainers):

```bash
AXP_SIGNING_KEY=/path/to/mintbot.ai-mintbot-mcp.key scripts/release.sh 0.1.0
git commit -am 'release v0.1.0' && git tag v0.1.0 && git push --tags
gh release create v0.1.0 dist/mintbot-mcp-0.1.0.tar.gz agent-extension.json --notes-file CHANGELOG.md
```

`scripts/build-artifact.sh` builds a deterministic tarball of the runtime
files; `scripts/release.sh` stamps the version into the package, fills the
digests and dates, signs the manifest and refuses a key that is not listed in
`agent-extension-keys.json`.

## License

MIT, (c) 2026 mintbot.ai
