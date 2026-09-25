"""The tool layer: thin wrappers whose failures are structured replies."""
from __future__ import annotations

import httpx
import pytest

from tests.conftest import Rig, make_settings
from mintbot_mcp.server import build_server, make_backends


async def test_every_tool_declares_structured_output(rig: Rig):
    tools = await rig.server.list_tools()
    names = {t.name for t in tools}
    expected = {
        "chat_send", "chat_wait", "chat_cancel", "chat_sessions", "chat_session_messages",
        "chat_session_delete", "chat_session_rename", "settings_overview", "model_get", "models_catalog",
        "model_set", "persona_get", "persona_set", "agent_name_get", "agent_name_set", "usage_get",
        "jobs_list", "jobs_create", "jobs_edit", "jobs_pause", "jobs_resume", "jobs_remove",
        "notifications_list", "notifications_mark_seen", "extensions_list", "extensions_settings_get",
        "extensions_settings_set", "extensions_install", "extensions_uninstall", "extensions_policy_set",
        "extensions_check_now", "extensions_update_now", "telegram_bot_get", "telegram_bot_apply",
        "telegram_bot_remove", "updates_get", "updates_set", "updates_run", "ssh_keys_list", "ssh_keys_add",
        "ssh_keys_remove", "security_state", "security_unlock", "security_lock", "server_reboot",
    }
    assert expected <= names, sorted(expected - names)
    for tool in tools:
        assert tool.output_schema, f"{tool.name} has no output schema; clients would get plain text"
        assert tool.description, f"{tool.name} has no description"


async def test_reboot_requires_explicit_confirmation(rig: Rig):
    rig.local.on("POST", "/server/reboot", {"status": "rebooting"})
    assert await rig.call("server_reboot") == {
        "ok": False, "error": "pass confirm=true to reboot the server", "code": "confirmation_required",
    }
    assert rig.local.sent("POST", "/server/reboot") == []
    assert await rig.call("server_reboot", confirm=True) == {"ok": True, "status": "rebooting"}


async def test_backend_http_error_becomes_a_structured_reply(rig: Rig):
    rig.local.on("GET", "/config/state", {"detail": "config daemon exploded"}, status=500)
    reply = await rig.call("model_get")
    assert reply["ok"] is False and reply["status"] == 500
    assert "config daemon exploded" in reply["error"]


async def test_unreachable_backend_is_reported_not_raised(rig: Rig):
    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")
    rig.local.handle("GET", "/config/state", boom)
    reply = await rig.call("model_get")
    assert reply["ok"] is False and reply["code"] == "unreachable"
    assert "connection refused" in reply["error"]


async def test_central_tools_without_central_config(tmp_path):
    settings = make_settings(tmp_path, central_url=None, panel_token=None, agent_id=None)
    backends = make_backends(settings)
    rig = Rig(build_server(settings, backends), backends, None, None, None, settings)  # type: ignore[arg-type]
    try:
        for tool in ("agent_name_get", "models_catalog", "usage_get"):
            reply = await rig.call(tool)
            assert reply["ok"] is False and reply["code"] == "central_unavailable", tool
    finally:
        await backends.aclose()


@pytest.mark.parametrize("tool, arguments", [
    ("chat_session_messages", {"session_id": "../etc/passwd"}),
    ("chat_session_rename", {"session_id": "abc", "title": "   "}),
    ("chat_send", {"message": "   "}),
    ("model_set", {"provider": "bad provider!", "model": "x"}),
    ("model_set", {"provider": "ok", "model": ""}),
    ("persona_set", {"content": "x", "mode": "prepend"}),
    ("updates_set", {}),
    ("updates_set", {"channels": {"nope": True}}),
    ("updates_run", {"channel": "firmware"}),
    ("extensions_policy_set", {"name": "x", "policy": "sometimes"}),
    ("extensions_uninstall", {"name": "../x"}),
    ("extensions_install", {"domain_or_url": ""}),
    ("ssh_keys_add", {"key": "ssh-ed25519 AAA\nssh-rsa BBB"}),
    ("ssh_keys_remove", {"fingerprint": ""}),
    ("security_unlock", {"code": "12"}),
    ("jobs_pause", {"job_id": "a/b"}),
])
async def test_argument_validation_never_reaches_a_backend(rig: Rig, tool, arguments):
    reply = await rig.call(tool, **arguments)
    assert reply["ok"] is False and reply["code"] == "invalid_argument", reply
    assert rig.local.calls == [] and rig.hermes.calls == [] and rig.central.calls == []


async def test_second_factor_gate_unlock_and_lock(rig: Rig):
    rig.local.on("POST", "/persona/local", {"detail": "second_factor_required"}, status=401)
    reply = await rig.call("persona_set", content="Be brief.")
    assert reply["ok"] is False and reply["code"] == "second_factor_required"
    rig.local.on("GET", "/security/state", {"totp_enabled": True})
    assert (await rig.call("security_state"))["two_factor_unlocked"] is False

    rig.local.on("POST", "/auth/totp_verify", {"ok": True},
                 headers={"set-cookie": "mb_sess=session-cookie; Path=/; Secure; HttpOnly"})
    assert await rig.call("security_unlock", code="123 456") == {"ok": True, "unlocked": True}
    assert json_body(rig, "POST", "/auth/totp_verify") == {"code": "123456"}

    rig.local.on("POST", "/persona/local", {"saved": True})
    assert (await rig.call("persona_set", content="Be brief."))["ok"] is True
    assert rig.local.sent("POST", "/persona/local")[-1].headers["cookie"] == "mb_sess=session-cookie"
    state = await rig.call("security_state")
    assert state["two_factor_unlocked"] is True and state["totp_enabled"] is True

    rig.local.on("POST", "/auth/logout", {})
    assert await rig.call("security_lock") == {"ok": True, "unlocked": False}
    assert rig.local.sent("POST", "/auth/logout")[-1].headers["cookie"] == "mb_sess=session-cookie"


def json_body(rig: Rig, method: str, path: str):
    return rig.local.last_json(method, path)


async def test_settings_overview_collects_every_section_and_keeps_errors_in_place(rig: Rig):
    rig.hermes.on("GET", "/health", {"status": "ok"})
    rig.local.on("GET", "/config/state", {"active": {"provider": "mintbot", "model": "claude-opus-5"}})
    rig.central.on("GET", "/v1/agent-panel/panel-token/name", {"detail": "central down"}, status=503)
    rig.local.on("GET", "/persona/local", {"content": "I am terse.", "max_chars": 4000})
    for path in ("/telegram-bot", "/updates/preferences", "/extensions/settings", "/security/state",
                 "/background-jobs/jobs"):
        rig.local.on("GET", path, {"path": path})
    overview = await rig.call("settings_overview")
    assert overview["ok"] is True and overview["agent_id"] == 1091 and overview["two_factor_unlocked"] is False
    assert overview["model"]["active"]["model"] == "claude-opus-5"
    assert overview["name"]["ok"] is False and overview["name"]["status"] == 503
    assert overview["persona"] == {"chars": 11, "max_chars": 4000, "has_overlay": True}
    assert overview["jobs"] == {"path": "/background-jobs/jobs"}


async def test_persona_append_keeps_the_existing_overlay(rig: Rig):
    rig.local.on("GET", "/persona/local", {"content": "Rule one.\n"})
    rig.local.on("POST", "/persona/local", {"saved": True})
    await rig.call("persona_set", content="  Rule two.  ", mode="append")
    assert rig.local.last_json("POST", "/persona/local") == {"content": "Rule one.\n\nRule two."}
    await rig.call("persona_set", content="", mode="replace")
    assert rig.local.last_json("POST", "/persona/local") == {"content": ""}


async def test_updates_set_shapes_the_channel_toggles(rig: Rig):
    rig.local.on("POST", "/updates/preferences", {"saved": True})
    await rig.call("updates_set", at=" 03:30 ", channels={"apt": False, "extensions": True})
    assert rig.local.last_json("POST", "/updates/preferences") == {
        "at": "03:30", "channels": {"apt": {"enabled": False}, "extensions": {"enabled": True}},
    }


async def test_jobs_create_sends_only_given_fields(rig: Rig):
    rig.local.on("POST", "/background-jobs/jobs", {"job_id": "j1"})
    reply = await rig.call("jobs_create", schedule=" every day at 9am ", prompt="Summarise the inbox.")
    assert reply == {"ok": True, "job_id": "j1"}
    assert rig.local.last_json("POST", "/background-jobs/jobs") == {
        "schedule": "every day at 9am", "prompt": "Summarise the inbox.", "no_agent": False,
    }


async def test_agent_name_get_normalises_the_central_reply(rig: Rig):
    rig.central.on("GET", "/v1/agent-panel/panel-token/name", {"name": "  Murr "})
    assert await rig.call("agent_name_get") == {"ok": True, "name": "Murr", "has_name": True}
    rig.central.on("GET", "/v1/agent-panel/panel-token/name", {"name": None})
    assert await rig.call("agent_name_get") == {"ok": True, "name": None, "has_name": False}


async def test_usage_get_authenticates_with_the_agent_key(rig: Rig):
    rig.central.on("GET", "/internal/agent-usage/1091", {"balance_usd": 12.5})
    assert await rig.call("usage_get") == {"ok": True, "balance_usd": 12.5}
    assert rig.central.calls[-1].headers["authorization"] == "Bearer hermes-key"


async def test_chat_session_messages_keeps_only_the_visible_turns(rig: Rig):
    rig.hermes.on("GET", "/api/sessions/s1/messages", {
        "title": "Inbox",
        "messages": [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "{}"},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "hello"},
        ],
    })
    reply = await rig.call("chat_session_messages", session_id="s1", limit=1)
    assert reply["title"] == "Inbox" and reply["total_visible"] == 2
    assert reply["messages"] == [{"role": "assistant", "content": "hello"}]


async def test_extensions_install_preflights_before_consent(rig: Rig):
    rig.local.on("POST", "/extensions/preflight", {"error_code": "consent_required"})
    rig.local.on("POST", "/extensions/install", {"installed": "graph-memory"})
    first = await rig.call("extensions_install", domain_or_url="mintbot.ai/graph-memory")
    assert first["error_code"] == "consent_required" and rig.local.sent("POST", "/extensions/install") == []
    second = await rig.call("extensions_install", domain_or_url="mintbot.ai/graph-memory", consented=True)
    assert second == {"ok": True, "installed": "graph-memory"}
    assert rig.local.last_json("POST", "/extensions/install")["consented"] is True


async def test_local_api_requests_carry_the_central_auth_header(rig: Rig):
    rig.local.on("GET", "/background-jobs/jobs", [])
    assert await rig.call("jobs_list") == {"ok": True, "result": []}
    assert rig.local.calls[-1].headers["x-mintbot-central-auth"] == "hermes-key"


async def test_agent_overview_resource_mirrors_the_tool(rig: Rig):
    rig.hermes.on("GET", "/health", {"status": "ok"})
    contents = await rig.server.read_resource("mintbot://agent")
    text = list(contents)[0].content
    assert '"agent_id": 1091' in text and '"runtime"' in text
