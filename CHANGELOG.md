# Changelog

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
