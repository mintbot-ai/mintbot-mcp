# Changelog

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
