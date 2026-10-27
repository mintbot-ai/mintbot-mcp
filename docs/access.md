# Temporary restricted MCP credentials

The existing installation bearer remains the **full owner credential**. Never share
it for temporary work. Root on the agent can issue named, expiring grants instead:

```sh
mintbot-mcp access issue --name 'Review session' --ttl 2h \
  --group chat --group projects --project PROJECT_ID
mintbot-mcp access list
mintbot-mcp access revoke GRANT_ID
```

`issue` prints JSON containing the grant ID, expiry (Unix timestamp), policy and a
`token`. The token is shown only once. Put that token, not the owner token, in the
client's `Authorization` header with the `Bearer` scheme. Remotely, use the
public address `https://www.<agent host>/mcp/` (TLS, relayed by the agent's
public site) or an SSH tunnel; never plain HTTP across the network.
Revoking an unknown ID exits with status 2. Lifetime accepts an integer number of
seconds or an integer followed by `s`, `m`, `h`, `d`; it is required and capped at
365 days. Reissue rather than extending or silently refreshing a grant.

## Explicit permissions

* Repeat `--tool TOOL_NAME` for an explicit tool allowlist.
* `--group chat` expands to `chat_send`, `chat_wait`, `chat_cancel`, `chat_sessions`,
  `chat_session_messages`, `chat_session_delete`, `chat_session_rename`. This group
  includes deletion/cancellation; use individual tools for read-only access.
* `--group projects` expands to `projects_list`, `project_get`.
* Repeat `--project PROJECT_ID` to limit project access. IDs are canonical platform
  project identifiers, not directory names. An explicit list cannot be empty.
* Omitting `--project` allows the chosen tools across projects. It does **not**
  imply read-only access or otherwise narrow a chosen tool's effects.
* Project-scoped credentials may use only project-aware chat/project tools, never
  global settings. An allowlist with unrelated global tools is rejected.
* TOTP unlock/lock/state and owner-cookie-sensitive writes (`persona_set`,
  `telegram_bot_apply`, `telegram_bot_remove`, `extensions_install`) are never
  delegated. Broader delegation must not reuse the owner's unlocked cookie.

The server filters `tools/list`, resource lists/templates and checks actual
`tools/call` / `resources/read` dispatch. Unknown resource types, prompts and
unrecognized protocol methods fail closed for restricted credentials. Session
and turn operations must verify their canonical project, not trust a caller's
project claim. Existing MCP transport sessions cannot switch bearer identity.
Expired/revoked credentials are rejected even on an already initialized session.
A revocation prevents new HTTP requests; it does not terminate an LLM turn that
was already started. The owner can cancel that turn separately.

**A project grant selects and authorizes conversation context. It is not an OS,
filesystem, network or LLM-tool sandbox.** The underlying agent may still have
powerful host tools. Do not use a project grant as isolation for untrusted users.

## Local state and operator scope

Grant metadata and SHA-256 hashes of cryptographically random secrets are stored
at `$MINTBOT_MCP_STATE_DIR/access/grants.json` (or the extension state directory
when unset). There are no plaintext grant tokens on disk. On the production
root-run daemon the directory is root-owned `0700` and state is `0600`; local
non-root development uses the same owner-only permissions. Changes are locked
and atomically replaced. Existing state with unsafe ownership/permissions is
refused rather than used. Run management commands as the daemon user and with
the same state-directory environment as its service. No daemon restart is needed.
Grant issuance/list/revoke do not require loading agent backend configuration.
Do not manually edit the grant file. Keep backups private; restoring an old backup
can restore revoked grants. Owner token rotation and grant revocation are separate.
Local stdio is trusted owner access, not a way to apply an HTTP grant.

## Private connection notices

After successful authenticated MCP `initialize`, the daemon posts a bounded notice
to the panel-local companion's `POST /mcp/connections`, authenticated with the
active rotating local identity. The request carries a nonsecret credential ID,
client name (at most 128 printable characters), version (at most 80) and observed
socket peer (at most 64). The endpoint stores a credential digest and timestamps,
not bearer tokens or arbitrary client metadata. The platform endpoint owns
durable deduplication and private owner delivery. Notification
failure cannot block MCP access and logs only a fixed secret-free message.

The observed peer is the ASGI socket client, **not** an asserted public client IP.
The built-in CLI disables Uvicorn forwarded-header rewriting, and the application
never reads `X-Forwarded-For`. Behind nginx this will normally be loopback/the
proxy. Client metadata and peer information are not a verified personal identity.
No request body is consumed, replayed or buffered by auth/notices; initialization
metadata is observed at the SDK's parsed dispatch boundary, preserving streaming.
If the companion endpoint is unavailable/older, MCP still works but private
notices will not be delivered until the companion is upgraded.
