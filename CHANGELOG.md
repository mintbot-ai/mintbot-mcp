# Changelog

## Unreleased

- `.github/workflows/dev-build.yml` reports every published dev build to DEV
  CatOffice (`POST /beaver/dev-build`, repository secret
  `BEAVER_DEV_BUILD_TOKEN`), which queues Beaver's AXP package tests on it;
  `scripts/release-dev.sh` tells the workflow the version and commit it
  published (`GITHUB_OUTPUT`).
- `scripts/release.sh` gates a release on the package's own channel: it names
  the dev build it promotes (`AXP_PROMOTE_FROM`), refuses a dirty checkout,
  and the mintbot repository's promote check must confirm a green DEV Beaver
  package journey on that build before anything is signed. `AGENTS.md` points
  at the flow. The dev key directory lists the Desktop dev key as well.

## v0.1.4 (2026-10-27)

- The public address is `https://www.<agent>.mintbot.ai/mcp/`: the agent's
  public site relays it to the loopback endpoint behind the same bearer token
  (every public service of an agent lives on its public site). The bare host's
  `/ext/mintbot-mcp/mcp` stays as the legacy address for clients already
  configured with it; the daemon still listens on `127.0.0.1:8650` only. The
  README, the after-install note and the panel's Settings -> MCP server
  summary say so.
- Private connection notices: after every successful authenticated MCP
  `initialize` the daemon posts a bounded, secret-free notice to the
  panel-local companion (`POST /mcp/connections`, as the agent's machine
  identity); the platform deduplicates it and tells the owner (panel
  notification and Telegram) which client connected. A failed notice never
  blocks MCP access. Stable installs before this release sent none.
- The display name, the credit balance and the model catalog are read through
  the agent's own signing boundary on panel-local (`/plugin/central/...` on
  loopback), which signs each request with the agent's identity key - the way
  the agent's own tools reach central. `agent_name_get`, `agent_name_set`,
  `models_catalog` and `usage_get` work again where central answers only
  signed requests (HTTP 404 before). The daemon no longer reads the panel
  token or the central API base, and sends central no credential of its own.
- `scripts/release-dev.sh` publishes a dev build of main: the next patch plus
  a UTC timestamp (`0.1.4-dev.20261022170105`), signed on the `dev` channel
  as a GitHub pre-release; only the newest two dev builds are kept. Agents
  that installed a stable release never receive one. `scripts/release.sh`
  takes the tree to sign (`RELEASE_TREE`) and the channel (`AXP_CHANNEL`).
- Dev builds are published by `.github/workflows/dev-build.yml` on every push
  to main and signed by a dev key (`mintbot.ai-mintbot-mcp-dev-2026`) that only
  mintbot.dev's key directory lists (`agent-extension-keys.dev.json`), so LIVE
  agents refuse them and the stable key stays off GitHub. `scripts/release.sh`
  signs with any key of a key directory (`AXP_SIGNING_KEY_ID`, `AXP_KEYDIR`).
- Every settings tool calls panel-local as the agent's machine identity (the
  current local key as a bearer token on loopback), the way the `/mcp/`
  companion routes already did; panel-local lets that identity through every
  gate the owner's session clears, two-factor login included. The legacy
  `X-Mintbot-Central-Auth` header and the panel session cookie are gone, and
  with them `security_unlock`, `security_lock` and the
  `second_factor_required` reply. `security_state` reports the panel's
  two-factor state alone.

## v0.1.3 (2026-10-16)

- Stage the package and a separate dependency runtime at a permanent release
  path, validate them offline, then atomically switch the active release.
  Pip/copy/healthcheck failures leave the previous installation usable; activation
  and standalone service failures restore the previous entrypoints and unit.
- Preserve legacy runtime paths during migration, stable launcher/lifecycle hook
  paths, existing tokens and user data, and host-owned disabled state. Standalone
  upgrades no longer enable or start an intentionally stopped service.
- Add hermetic failure-injection, migration, absolute-shebang and extracted-release
  install/upgrade coverage. No service or real extension install is used in tests.

## v0.1.2 (2026-10-15)

- The manifest moves to AXP 0.5 and declares a `settings_section` ("MCP
  server"): a host with a settings page of its own per extension - the mintbot
  panel's Settings -> MCP server - takes its title and summary from here, in
  the user's language where a translation is shipped. The panel's switch there
  installs the server through the usual consent card and shows the connection
  settings (endpoint, token, SSH tunnel, ready-to-paste client configuration).
- The launcher bakes in the state dir the installer was given, so
  `mintbot-mcp token`, `client-config` and `token --rotate` run by hand on an
  AXP host read the daemon's token instead of the standalone default path.
- `release.changelog` points at the release of the version it ships with
  (v0.1.1 still pointed at v0.1.0); `scripts/release.sh` stamps it.
- The publisher key directory copy carries every mintbot.ai key (the
  Desktop's too): the same document mintbot.ai serves.
- A one-sentence description, which the panel translates.

## v0.1.1 (2026-09-26)

- The `mcp` service now declares its endpoint access in the manifest
  (`auth: {scheme: bearer, token_file: state:token}`, AXP v0.4 "Endpoint
  access", SPEC 4.2), so a host can register the server in its runtime MCP
  config with the bearer header filled in and show the owner a ready-made
  client entry. No runtime change: the token file and its location are the
  same as before.

## v0.1.0 (2026-09-25)

- First release: an MCP server (streamable HTTP + stdio) that runs on the
  agent's own VPS and exposes the agent to any MCP client - chat with turn
  slicing for long agent runs (`chat_send` / `chat_wait` / `chat_cancel`),
  conversation management, and every mintbot setting the panel offers
  (model, persona, name, credit, background jobs, notifications, extensions,
  Telegram bot, nightly updates, SSH keys, two-factor unlock, reboot).
- Backends are the daemons already on the VPS: the Hermes API server and the
  panel-local settings daemon on loopback; only the display name, credit and
  the model catalog go to mintbot central, the way the agent's own tools do.
- Bearer-authenticated HTTP transport on `127.0.0.1:8650`; the token lives in
  the extension's state dir (mode 0600) and is printed by `mintbot-mcp token`.
- Packaged as a signed AXP v0.4 extension published by `mintbot.ai`
  (`mintbot.ai/mintbot-mcp`): `archive` delivery from GitHub releases,
  `github` update source, publisher key `mintbot.ai-mintbot-mcp-2026`. The
  daemon is bound with a `command`, so an AXP host owns, contains and
  supervises the systemd unit; a standalone `install.sh` writes a plain
  `mintbot-mcp.service` instead.
- Permissions declared honestly for the consent card: no root, one loopback
  listener, egress limited to PyPI and the mintbot API, `config:r` (the Hermes
  home) and `state:rw` (the token).
