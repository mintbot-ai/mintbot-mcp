"""The MCP surface: every tool an MCP client can call against this agent.

Tools are thin: validate the argument shape, call one backend, return the
backend's JSON. A backend failure never raises out of a tool - the client gets
``{"ok": false, "error": ..., "code": ...}`` it can show, exactly as the
agent's own mintbot tools behave. The one stateful piece is the chat turn
registry (:mod:`chat`), which lets a client follow a long agent turn in slices.
"""
from __future__ import annotations

import asyncio
import functools
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from .backends import BackendError, CentralClient, HermesClient, LocalApiClient
from .chat import TurnRegistry
from .config import Settings

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_POLICIES = ("auto", "notify", "pin", "off")
_UPDATE_CHANNELS = ("apt", "hermes", "mintbot", "extensions")
_DEFAULT_WAIT_SECONDS = 120
_MAX_NAME_LENGTH = 50

INSTRUCTIONS = """This server is a mintbot agent's own control surface, running on the agent's VPS.

Chat: chat_send sends one message and waits up to wait_seconds for the reply. A turn that is
still running comes back with status "running" and a turn_id - call chat_wait with it to keep
collecting the reply, or chat_cancel to stop the agent. Pass the returned session_id to the
next chat_send to continue the same conversation; omit it to start a new one.

Settings: every mintbot setting has a *_get / *_list reader and a matching writer. Writers
change the live agent at once (model_set restarts the agent runtime for about 20 seconds).
When the owner has two-factor login on, the browser-gated settings (persona, Telegram bot,
extension installs) answer with code "second_factor_required": call security_unlock with a
current authenticator code once, then retry.

Start with settings_overview for the whole picture in one call."""


@dataclass
class Backends:
    hermes: HermesClient
    local: LocalApiClient
    central: CentralClient
    turns: TurnRegistry

    async def aclose(self) -> None:
        for client in (self.hermes, self.local, self.central):
            await client.aclose()


def make_backends(
    settings: Settings,
    *,
    hermes_transport: httpx.AsyncBaseTransport | None = None,
    local_transport: httpx.AsyncBaseTransport | None = None,
    central_transport: httpx.AsyncBaseTransport | None = None,
) -> Backends:
    hermes = HermesClient(settings, transport=hermes_transport)
    return Backends(
        hermes=hermes,
        local=LocalApiClient(settings, transport=local_transport),
        central=CentralClient(settings, transport=central_transport),
        turns=TurnRegistry(hermes),
    )


def _guarded(fn):
    """Backend failures become a structured reply instead of an MCP protocol error."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except BackendError as exc:
            return exc.as_dict()
        except httpx.HTTPError as exc:
            return {"ok": False, "code": "unreachable", "error": f"backend unreachable: {exc}"}

    return wrapper


async def _part(coro) -> Any:
    """One section of an aggregate reply: the value, or the error in its place."""
    try:
        return await coro
    except BackendError as exc:
        return exc.as_dict()
    except httpx.HTTPError as exc:
        return {"ok": False, "code": "unreachable", "error": str(exc)}


def _ok(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload if "ok" in payload else {"ok": True, **payload}
    return {"ok": True, "result": payload}


def _checked(pattern: re.Pattern[str], value: str | None, what: str) -> str:
    text = (value or "").strip()
    if not pattern.match(text):
        raise BackendError(f"invalid {what}: {text!r}", code="invalid_argument")
    return text


def _message_list(data: Any) -> list[dict[str, Any]]:
    items = data
    if isinstance(data, dict):
        items = data.get("messages", data.get("data"))
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def _compact(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def build_server(settings: Settings, backends: Backends) -> MCPServer:  # noqa: C901 - one registry, many small tools
    b = backends
    mcp = MCPServer(
        name="mintbot",
        title="mintbot agent",
        version=__version__,
        instructions=INSTRUCTIONS,
        website_url="https://github.com/mintbot-ai/mintbot-mcp",
    )

    # ------------------------------------------------------------------ chat

    @mcp.tool()
    @_guarded
    async def chat_send(message: str, session_id: str | None = None,
                        wait_seconds: int = _DEFAULT_WAIT_SECONDS) -> dict[str, Any]:
        """Send one message to the agent and wait up to wait_seconds for its reply.

        Returns the reply with status "done", or status "running" plus a turn_id when the
        agent is still working (continue with chat_wait). Pass session_id to continue an
        existing conversation; omit it to start a new one - the reply carries the id.
        """
        if not message.strip():
            raise BackendError("message is empty", code="invalid_argument")
        sid = _checked(_SESSION_ID_RE, session_id, "session_id") if session_id else None
        turn = await b.turns.start(message, session_id=sid)
        return await b.turns.wait(turn, wait_seconds)

    @mcp.tool()
    @_guarded
    async def chat_wait(turn_id: str, wait_seconds: int = 60) -> dict[str, Any]:
        """Keep waiting on a running turn from chat_send; returns the reply collected so far."""
        turn = b.turns.get(turn_id.strip())
        if turn is None:
            raise BackendError(f"unknown turn_id {turn_id!r} (finished turns are kept for 15 minutes)",
                               code="unknown_turn")
        return await b.turns.wait(turn, wait_seconds)

    @mcp.tool()
    @_guarded
    async def chat_cancel(turn_id: str) -> dict[str, Any]:
        """Stop a running turn: the agent is interrupted, like the panel's Stop button."""
        return b.turns.cancel(turn_id.strip())

    @mcp.tool()
    @_guarded
    async def chat_sessions(limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """List recent conversations across every channel (MCP, web panel, Telegram, CLI), newest first."""
        return _ok(await b.hermes.list_sessions(limit=max(1, min(limit, 200)), offset=max(0, offset)))

    @mcp.tool()
    @_guarded
    async def chat_session_messages(session_id: str, limit: int = 100) -> dict[str, Any]:
        """Read one conversation: the last `limit` user/assistant messages."""
        sid = _checked(_SESSION_ID_RE, session_id, "session_id")
        data = await b.hermes.session_messages(sid)
        visible = [m for m in _message_list(data)
                   if m.get("role") in ("user", "assistant") and m.get("content")]
        meta = {k: v for k, v in data.items() if k not in ("messages", "data")} if isinstance(data, dict) else {}
        return {"ok": True, "session_id": sid, **meta,
                "messages": visible[-max(1, limit):], "total_visible": len(visible)}

    @mcp.tool()
    @_guarded
    async def chat_session_delete(session_id: str) -> dict[str, Any]:
        """Delete one conversation permanently."""
        return _ok(await b.hermes.delete_session(_checked(_SESSION_ID_RE, session_id, "session_id")))

    @mcp.tool()
    @_guarded
    async def chat_session_rename(session_id: str, title: str) -> dict[str, Any]:
        """Give a conversation a title (shown in the panel's session picker)."""
        clean = title.strip()
        if not clean or len(clean) > 200:
            raise BackendError("title must be 1-200 characters", code="invalid_argument")
        return _ok(await b.hermes.rename_session(_checked(_SESSION_ID_RE, session_id, "session_id"), clean))

    # -------------------------------------------------------------- overview

    @mcp.tool()
    @_guarded
    async def settings_overview() -> dict[str, Any]:
        """Everything about this agent in one call: runtime health, active model and providers,
        display name, persona overlay size, Telegram bot, nightly updates, extensions switch,
        two-factor state and the scheduled jobs. A section that cannot be read carries its error.
        """
        keys = ("runtime", "model", "name", "persona", "telegram_bot", "updates", "extensions",
                "security", "jobs")
        values = await asyncio.gather(
            _part(b.hermes.health()),
            _part(b.local.request("GET", "/config/state")),
            _part(b.central.get_name()),
            _part(b.local.request("GET", "/persona/local")),
            _part(b.local.request("GET", "/telegram-bot")),
            _part(b.local.request("GET", "/updates/preferences")),
            _part(b.local.request("GET", "/extensions/settings")),
            _part(b.local.request("GET", "/security/state")),
            _part(b.local.request("GET", "/background-jobs/jobs")),
        )
        overview = dict(zip(keys, values))
        persona = overview.get("persona")
        if isinstance(persona, dict) and "content" in persona:
            content = persona.get("content") or ""
            overview["persona"] = {"chars": len(content), "max_chars": persona.get("max_chars"),
                                   "has_overlay": bool(content.strip())}
        return {"ok": True, "agent_id": settings.agent_id, "two_factor_unlocked": b.local.unlocked, **overview}

    # ----------------------------------------------------------------- model

    @mcp.tool()
    @_guarded
    async def model_get() -> dict[str, Any]:
        """The active LLM provider + model, and every provider configured on this agent."""
        return _ok(await b.local.request("GET", "/config/state"))

    @mcp.tool()
    @_guarded
    async def models_catalog() -> dict[str, Any]:
        """The model catalog the panel's model picker offers (from mintbot central)."""
        return _ok(await b.central.models())

    @mcp.tool()
    @_guarded
    async def model_set(provider: str, model: str) -> dict[str, Any]:
        """Switch the active provider + model (a provider name from model_get, a model id from
        models_catalog). The agent runtime restarts about 20 s later; chat_send fails briefly.
        """
        clean_provider = _checked(_SLUG_RE, provider, "provider")
        clean_model = model.strip()
        if not clean_model or len(clean_model) > 256:
            raise BackendError("model must be 1-256 characters", code="invalid_argument")
        return _ok(await b.local.request("POST", "/config/active",
                                         body={"provider": clean_provider, "model": clean_model}))

    # --------------------------------------------------------------- persona

    @mcp.tool()
    @_guarded
    async def persona_get() -> dict[str, Any]:
        """The editable persona overlay (the panel's "Your agent" text) and its size limit."""
        return _ok(await b.local.request("GET", "/persona/local"))

    @mcp.tool()
    @_guarded
    async def persona_set(content: str, mode: str = "replace") -> dict[str, Any]:
        """Write the persona overlay. mode "replace" overwrites it (empty content clears it);
        mode "append" adds a paragraph at the end. Takes effect on the agent's next turn.
        """
        if mode not in ("replace", "append"):
            raise BackendError('mode must be "replace" or "append"', code="invalid_argument")
        text = content
        if mode == "append":
            current = await b.local.request("GET", "/persona/local")
            existing = (current.get("content") or "").rstrip() if isinstance(current, dict) else ""
            addition = content.strip()
            text = f"{existing}\n\n{addition}" if existing else addition
        return _ok(await b.local.request("POST", "/persona/local", body={"content": text}))

    # ------------------------------------------------------- name and credit

    @mcp.tool()
    @_guarded
    async def agent_name_get() -> dict[str, Any]:
        """The agent's display name (panel topbar); null when the owner has not named it."""
        data = await b.central.get_name()
        name = (data.get("name") or "").strip() if isinstance(data, dict) else ""
        return {"ok": True, "name": name or None, "has_name": bool(name)}

    @mcp.tool()
    @_guarded
    async def agent_name_set(name: str) -> dict[str, Any]:
        """Rename the agent (max 50 characters; an empty name clears it)."""
        return _ok(await b.central.set_name(name.strip()[:_MAX_NAME_LENGTH]))

    @mcp.tool()
    @_guarded
    async def usage_get() -> dict[str, Any]:
        """Current mintbot credit balance and the last 7 days' spend. The balance says nothing
        about which model is active: BYOK keys and connected subscriptions bypass credit.
        """
        return _ok(await b.central.usage())

    # ------------------------------------------------------------------ jobs

    @mcp.tool()
    @_guarded
    async def jobs_list() -> dict[str, Any]:
        """Every scheduled / background job (the panel's Background jobs list)."""
        return _ok(await b.local.request("GET", "/background-jobs/jobs"))

    @mcp.tool()
    @_guarded
    async def jobs_create(schedule: str, prompt: str | None = None, name: str | None = None,
                          deliver: str | None = None, repeat: int | None = None,
                          skills: list[str] | None = None, script: str | None = None,
                          no_agent: bool = False, workdir: str | None = None) -> dict[str, Any]:
        """Create a scheduled job. schedule: "30m", "every day at 9am", "weekdays at 9am", cron
        syntax, "in 2h" (one-shot) or an ISO timestamp. The prompt must be self-contained (the
        job runs in a fresh session). Finished runs are relayed to the panel and Telegram.
        """
        body = _compact({"schedule": schedule.strip(), "prompt": prompt, "name": name, "deliver": deliver,
                         "repeat": repeat, "skills": skills, "script": script, "workdir": workdir})
        body["no_agent"] = bool(no_agent)
        return _ok(await b.local.request("POST", "/background-jobs/jobs", body=body, timeout=120))

    @mcp.tool()
    @_guarded
    async def jobs_edit(job_id: str, schedule: str | None = None, prompt: str | None = None,
                        name: str | None = None, deliver: str | None = None, repeat: int | None = None,
                        skills: list[str] | None = None, script: str | None = None,
                        no_agent: bool | None = None, workdir: str | None = None,
                        clear_skills: bool = False) -> dict[str, Any]:
        """Edit one job; only the given fields change. skills replaces the job's skill list,
        clear_skills removes them all.
        """
        jid = _checked(_SLUG_RE, job_id, "job_id")
        body = _compact({"schedule": schedule, "prompt": prompt, "name": name, "deliver": deliver,
                         "repeat": repeat, "skills": skills, "script": script, "no_agent": no_agent,
                         "workdir": workdir})
        body["clear_skills"] = bool(clear_skills)
        return _ok(await b.local.request("PATCH", f"/background-jobs/jobs/{jid}", body=body, timeout=120))

    async def _job_action(job_id: str, action: str) -> dict[str, Any]:
        jid = _checked(_SLUG_RE, job_id, "job_id")
        return _ok(await b.local.request("POST", f"/background-jobs/job/{jid}/{action}", timeout=60))

    @mcp.tool()
    @_guarded
    async def jobs_pause(job_id: str) -> dict[str, Any]:
        """Pause a recurring job (keeps it, stops future runs)."""
        return await _job_action(job_id, "pause")

    @mcp.tool()
    @_guarded
    async def jobs_resume(job_id: str) -> dict[str, Any]:
        """Resume a paused job."""
        return await _job_action(job_id, "resume")

    @mcp.tool()
    @_guarded
    async def jobs_remove(job_id: str) -> dict[str, Any]:
        """Delete a job entirely."""
        return await _job_action(job_id, "remove")

    @mcp.tool()
    @_guarded
    async def notifications_list(limit: int = 20, offset: int = 0) -> dict[str, Any]:
        """Unread finished job runs and inbox notices (the panel's Notifications tab)."""
        return _ok(await b.local.request("GET", "/background-jobs",
                                         params={"limit": max(1, min(limit, 200)), "offset": max(0, offset)}))

    @mcp.tool()
    @_guarded
    async def notifications_mark_seen(run_id: str | None = None) -> dict[str, Any]:
        """Acknowledge one notification by run_id, or every current one when run_id is omitted."""
        if run_id and run_id.strip():
            return _ok(await b.local.request("POST", "/background-jobs/seen", body={"run_id": run_id.strip()}))
        return _ok(await b.local.request("POST", "/background-jobs/seen-all"))

    # ------------------------------------------------------------ extensions

    @mcp.tool()
    @_guarded
    async def extensions_list() -> dict[str, Any]:
        """Installed extensions (AXP packages) with version, trust, health and update policy."""
        return _ok(await b.local.request("GET", "/extensions"))

    @mcp.tool()
    @_guarded
    async def extensions_settings_get() -> dict[str, Any]:
        """Whether third-party extensions are allowed on this agent (off by default)."""
        return _ok(await b.local.request("GET", "/extensions/settings"))

    @mcp.tool()
    @_guarded
    async def extensions_settings_set(enabled: bool) -> dict[str, Any]:
        """Allow or refuse extension installs and update checks on this agent."""
        return _ok(await b.local.request("PUT", "/extensions/settings", body={"enabled": bool(enabled)}))

    @mcp.tool()
    @_guarded
    async def extensions_install(domain_or_url: str, consented: bool = False) -> dict[str, Any]:
        """Install an extension by publisher domain, manifest URL or GitHub repository URL.
        The first call (consented=false) only runs the preflight and returns the permissions
        the user must agree to; call again with consented=true after they explicitly did.
        """
        target = domain_or_url.strip()
        if not target or len(target) > 2048:
            raise BackendError("domain_or_url must be 1-2048 characters", code="invalid_argument")
        path = "/extensions/install" if consented else "/extensions/preflight"
        return _ok(await b.local.request("POST", path, body={"domain_or_url": target, "consented": bool(consented)},
                                         timeout=900))

    @mcp.tool()
    @_guarded
    async def extensions_uninstall(name: str, purge: bool = False) -> dict[str, Any]:
        """Uninstall an extension by name; purge=true also deletes its stored data."""
        clean = _checked(_SLUG_RE, name, "name")
        return _ok(await b.local.request("DELETE", f"/extensions/{clean}", params={"purge": str(bool(purge)).lower()},
                                         timeout=600))

    @mcp.tool()
    @_guarded
    async def extensions_policy_set(name: str, policy: str) -> dict[str, Any]:
        """Per-extension update policy: auto (install updates), notify, pin (this version) or off."""
        clean = _checked(_SLUG_RE, name, "name")
        if policy not in _POLICIES:
            raise BackendError(f"policy must be one of {', '.join(_POLICIES)}", code="invalid_argument")
        return _ok(await b.local.request("PUT", f"/extensions/{clean}/policy", body={"policy": policy}))

    @mcp.tool()
    @_guarded
    async def extensions_check_now() -> dict[str, Any]:
        """Check every installed extension for updates now (read-only; applies nothing)."""
        return _ok(await b.local.request("POST", "/extensions/check-now", timeout=300))

    @mcp.tool()
    @_guarded
    async def extensions_update_now(name: str) -> dict[str, Any]:
        """Apply the available update of one extension now."""
        clean = _checked(_SLUG_RE, name, "name")
        return _ok(await b.local.request("POST", f"/extensions/{clean}/update-now", timeout=900))

    # --------------------------------------------------------------- telegram

    @mcp.tool()
    @_guarded
    async def telegram_bot_get() -> dict[str, Any]:
        """State of the owner's custom Telegram bot connector (token never included)."""
        return _ok(await b.local.request("GET", "/telegram-bot"))

    @mcp.tool()
    @_guarded
    async def telegram_bot_apply(token: str | None = None) -> dict[str, Any]:
        """Connect a custom Telegram bot with its BotFather token (stored only on this VPS),
        or re-run the activation when token is omitted.
        """
        body = {"token": token.strip()} if token and token.strip() else {}
        return _ok(await b.local.request("POST", "/telegram-bot/apply", body=body, timeout=600))

    @mcp.tool()
    @_guarded
    async def telegram_bot_remove() -> dict[str, Any]:
        """Disconnect the custom Telegram bot and delete its token from this VPS."""
        return _ok(await b.local.request("DELETE", "/telegram-bot", timeout=120))

    # ---------------------------------------------------------------- updates

    @mcp.tool()
    @_guarded
    async def updates_get() -> dict[str, Any]:
        """Nightly maintenance: the schedule, which channels run (apt, hermes, mintbot,
        extensions) and when each last succeeded.
        """
        preferences, state = await asyncio.gather(
            _part(b.local.request("GET", "/updates/preferences")),
            _part(b.local.request("GET", "/updates/state")),
        )
        return {"ok": True, "preferences": preferences, "state": state}

    @mcp.tool()
    @_guarded
    async def updates_set(at: str | None = None, channels: dict[str, bool] | None = None) -> dict[str, Any]:
        """Change the nightly maintenance: `at` is the local time ("03:30"); `channels` maps
        apt / hermes / mintbot / extensions to on/off. Only the given fields change.
        """
        body: dict[str, Any] = {}
        if at is not None:
            body["at"] = at.strip()
        if channels is not None:
            unknown = sorted(set(channels) - set(_UPDATE_CHANNELS))
            if unknown:
                raise BackendError(f"unknown update channels: {', '.join(unknown)}", code="invalid_argument")
            body["channels"] = {key: {"enabled": bool(value)} for key, value in channels.items()}
        if not body:
            raise BackendError("nothing to change: give at and/or channels", code="invalid_argument")
        return _ok(await b.local.request("POST", "/updates/preferences", body=body))

    @mcp.tool()
    @_guarded
    async def updates_run(channel: str = "all") -> dict[str, Any]:
        """Run the maintenance now: one channel (apt, hermes, mintbot, extensions) or "all".
        Returns when the run has been started or finished, depending on the channel.
        """
        if channel == "all":
            return _ok(await b.local.request("POST", "/updates/run-all", timeout=1800))
        if channel not in _UPDATE_CHANNELS:
            raise BackendError(f"channel must be all or one of {', '.join(_UPDATE_CHANNELS)}", code="invalid_argument")
        return _ok(await b.local.request("POST", f"/updates/run/{channel}", timeout=1200))

    # ------------------------------------------------------------------- ssh

    @mcp.tool()
    @_guarded
    async def ssh_keys_list() -> dict[str, Any]:
        """SSH public keys allowed to log in as root (the mintbot support key is protected)."""
        return _ok(await b.local.request("GET", "/server/ssh-keys"))

    @mcp.tool()
    @_guarded
    async def ssh_keys_add(key: str) -> dict[str, Any]:
        """Add one SSH public key line ("ssh-ed25519 AAAA... comment")."""
        clean = key.strip()
        if not clean or "\n" in clean:
            raise BackendError("key must be a single non-empty line", code="invalid_argument")
        return _ok(await b.local.request("POST", "/server/ssh-keys", body={"key": clean}))

    @mcp.tool()
    @_guarded
    async def ssh_keys_remove(fingerprint: str) -> dict[str, Any]:
        """Remove an SSH key by the fingerprint shown in ssh_keys_list."""
        clean = fingerprint.strip()
        if not clean or len(clean) > 200:
            raise BackendError("fingerprint is required", code="invalid_argument")
        return _ok(await b.local.request("DELETE", f"/server/ssh-keys/{quote(clean, safe='/:')}"))

    # -------------------------------------------------------------- security

    @mcp.tool()
    @_guarded
    async def security_state() -> dict[str, Any]:
        """Two-factor login state of the panel, and whether this server currently holds an
        unlocked two-factor session for the browser-gated settings.
        """
        data = await b.local.request("GET", "/security/state")
        return {"ok": True, "two_factor_unlocked": b.local.unlocked, **(data if isinstance(data, dict) else {})}

    @mcp.tool()
    @_guarded
    async def security_unlock(code: str) -> dict[str, Any]:
        """Present a current authenticator (TOTP) or backup code so the browser-gated settings
        become writable through this server (session kept in memory until security_lock or
        a restart). Only needed while two-factor login is enabled.
        """
        clean = code.strip().replace(" ", "").replace("-", "")
        if not 4 <= len(clean) <= 16:
            raise BackendError("code must be 4-16 characters", code="invalid_argument")
        return await b.local.unlock(clean)

    @mcp.tool()
    @_guarded
    async def security_lock() -> dict[str, Any]:
        """Drop the unlocked two-factor session held by this server."""
        return await b.local.lock()

    # ---------------------------------------------------------------- server

    @mcp.tool()
    @_guarded
    async def server_reboot(confirm: bool = False) -> dict[str, Any]:
        """Reboot the agent's VPS. Everything, including this MCP server, is down for about a
        minute. Requires confirm=true.
        """
        if not confirm:
            raise BackendError("pass confirm=true to reboot the server", code="confirmation_required")
        return _ok(await b.local.request("POST", "/server/reboot"))

    # ------------------------------------------------------------- resources

    @mcp.resource("mintbot://agent", name="agent-overview", mime_type="application/json",
                  description="The same picture settings_overview returns, as a readable resource.")
    async def agent_overview() -> str:
        return json.dumps(await settings_overview(), ensure_ascii=False, indent=2, default=str)

    @mcp.resource("mintbot://sessions/{session_id}", name="conversation", mime_type="application/json",
                  description="One conversation's messages by session id.")
    async def conversation(session_id: str) -> str:
        return json.dumps(await chat_session_messages(session_id), ensure_ascii=False, indent=2, default=str)

    return mcp
