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
| projects | `projects_list`, `project_get` |
| overview | `settings_overview` (everything in one call; also the `mintbot://agent` resource) |
| model | `model_get`, `models_catalog`, `model_set` |
| persona | `persona_get`, `persona_set` (replace / append) |
| name and credit | `agent_name_get`, `agent_name_set`, `usage_get` |
| jobs | `jobs_list`, `jobs_create`, `jobs_edit`, `jobs_pause`, `jobs_resume`, `jobs_remove`, `notifications_list`, `notifications_mark_seen` |
| extensions | `extensions_list`, `extensions_settings_get`, `extensions_settings_set`, `extensions_install` (preflight, then consented), `extensions_uninstall`, `extensions_policy_set`, `extensions_check_now`, `extensions_update_now` |
| telegram | `telegram_bot_get`, `telegram_bot_apply`, `telegram_bot_remove` |
| updates | `updates_get`, `updates_set`, `updates_run` |
| ssh | `ssh_keys_list`, `ssh_keys_add`, `ssh_keys_remove` |
| security | `security_state` |
| server | `server_reboot` (requires `confirm=true`) |

Deliberately **not** exposed: entering BYOK API keys, setting up two-factor
login, the root password. Those stay in the panel, where the browser-only
two-factor gate protects them.

**Machine identity.** The daemon calls panel-local as the agent's own
machine identity: the agent's current local key as a bearer token on a direct
loopback connection with no browser or proxy headers. panel-local lets that
identity through every gate the owner's panel session clears, two-factor login
included, so an MCP client may change and run everything on its agent and is
never asked for an authenticator code. The daemon holds no central credential
and no panel cookie.

## Project-aware chat and safe retries

Call `projects_list` to discover canonical project IDs. Supply `project_id` to
`chat_send` when starting a conversation; omitted projects default to `main`,
never the panel's currently selected project. Continue with the returned
`session_id`. A conflicting project is rejected rather than moving the session.
These tools require the updated mintbot platform MCP companion; older platforms
fail explicitly instead of silently running in the wrong directory.

`chat_sessions` returns compact project-aware rows. Set `include_details=true`
for backend metadata, or `project_id` to narrow the list. Pagination is bounded
to the newest 500 sessions per project and refuses offsets outside that window.
Modern clients receive the full result in `structuredContent` and a short text
summary instead of a duplicate JSON dump. Older negotiated MCP versions retain
the SDK's JSON-text compatibility fallback. SSE explicitly declares UTF-8.

For retries, provide a unique `idempotency_key` with `chat_send`. Repeating the
same key and payload under the same credential cannot start a second agent turn,
including across concurrent requests and daemon restarts. A changed payload is
a conflict. After an uncertain restart the outcome is reported as unknown, never
rerun automatically. Use the returned session/turn IDs to investigate; do not
retry uncertain work with a fresh key. Dedupe tombstones last as long as the
private state database. Completed reply bodies expire after 15 minutes, with at
most 32 persisted replies of at most 256 Ki characters each.

For temporary collaboration, issue a named, expiring grant with explicit tools
and optional project IDs rather than sharing the owner token. See
[restricted access and connection notices](docs/access.md). Project selection is
not a filesystem or LLM-tool sandbox.

Authenticated MCP initialization sends a private owner notice through the
platform companion to the panel and the owner's Telegram DM, never a group.
Notices are limited to one per credential per 24 hours and five per agent per
10 minutes, with deduplication surviving restarts. Client name/version are
unverified claims; behind nginx the observed peer is normally the proxy, not
the external user's public IP. A failed notice does not block the MCP request.

## Install

### On a mintbot agent (AXP host)

In the agent panel: Settings -> **MCP server**, one switch. Its first
switch-on installs the server through the consent card (turning agent
extensions on first when they are off); the card then shows the connection
settings - endpoint, bearer token, SSH tunnel and a ready-to-paste client
configuration. Or in chat: *"install mintbot-mcp"* (or the GitHub URL), or
the Install box in Settings -> Extensions. The host shows the consent card, verifies the artifact digest
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
| `settings_section` "MCP server" | AXP 0.5: the host's own settings page for the extension takes its title and summary (Estonian shipped) from here; the server has no panel view |
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

### Transactional upgrades

`install.sh` and `upgrade.sh` build a complete candidate under
`$AXP_PREFIX/releases/release.<unique-id>/`: package, lifecycle hooks, and a
**new private virtualenv**. Pip never modifies the active environment. The
directory is already at its final path before venv creation and pip run, so
absolute interpreter paths in console-script shebangs remain valid.

The candidate's `healthcheck.sh --offline` imports the server, HTTP app and
runtime dependencies, and checks its CLI before activation. It needs no running
daemon, backend connection or token. The ordinary health hook still checks the
running daemon. A successful offline check is not a claim that the agent's
backends or the post-restart service are healthy.

Activation atomically replaces the `current` symlink. The stable
`bin/mintbot-mcp` launcher and root lifecycle-hook paths stay compatible; each
release's launcher pins its code and interpreter to the same release. Installers
are serialized with `flock`. Failed staging removes only the candidate; errors
during activation (including standalone service operations) restore the previous
entrypoints and unit file. Catchable termination signals also roll back. If
rollback itself fails, the hook fails loudly and retains the candidate and
transaction backups for operator recovery. This is not power-loss/SIGKILL
recovery, and does not undo a host's later post-hook actions.

Existing flat installs migrate without moving or modifying their old `lib/` and
`venv/` directories; this preserves old absolute shebangs and running processes.
The launcher and health hook use `current` after migration. Fresh installs also
expose compatibility `lib` and `venv` symlinks to `current`. Old releases and legacy
directories are deliberately retained, not automatically pruned while a process
could still use them. Remove an obsolete release only after verifying nothing
uses it; `uninstall.sh` removes the entire prefix as before. Re-running the
installed root hooks stages from the selected release, not the legacy files.

Upgrades preserve the existing token and other state data. Under `AXP_HOST`,
the installer never starts/enables a service or edits host enablement/policy:
a disabled extension stays under the host's control. Standalone upgrades restart
only a previously active unit and preserve existing enablement/stopped state;
only a first standalone installation enables and starts its new unit.
The shell lifecycle tools target Linux (Bash, GNU coreutils and `flock`).

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

The server listens on loopback only. The agent's public site relays it: from
anywhere, point the client at

```
https://www.agent1234.mintbot.ai/mcp/
```

with the same `Authorization: Bearer <token>` header (the mintbot panel's
Settings -> MCP server shows this address once the agent's certificate
carries the `www.` name). The bare host's `/ext/mintbot-mcp/mcp` is the
legacy address, kept for clients already configured with it. Without the
relay, open an SSH tunnel and keep the URL as printed:

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
`MINTBOT_MCP_HERMES_API_KEY`, `MINTBOT_MCP_LOCAL_CREDENTIALS_FILE`, `HERMES_HOME`.
The machine companion and Hermes calls read the active rotating local identity
on every request; a missing or malformed identity file fails closed.

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
`agent-extension-keys.json`. A stable release is never a GitHub pre-release:
it becomes the repository's "latest", which a bare repository URL installs.

Dev builds publish themselves: `.github/workflows/dev-build.yml` runs
`scripts/release-dev.sh` on every push to `main` (and right after each stable
release). It publishes the commit as `<next patch>-dev.<UTC timestamp>` on the
`dev` channel, a GitHub pre-release, and keeps only the newest two dev builds.

Dev builds are signed by a separate dev key (repository secret
`AXP_DEV_SIGNING_KEY`). Only `agent-extension-keys.dev.json`, the key directory
mintbot.dev serves to DEV agents, lists it; mintbot.ai never does, so a LIVE
agent refuses every dev build. An agent pins the key of its first install:
install a dev build's `agent-extension.json` and it updates to every later dev
build; install a stable release and it never takes a dev build. Moving an
agent between the two lines is a reinstall with an explicit re-trust. By hand,
with the dev key:

```bash
AXP_SIGNING_KEY=/path/to/mintbot.ai-mintbot-mcp-dev.key scripts/release-dev.sh [<commit>]
```

## License

MIT, (c) 2026 mintbot.ai
