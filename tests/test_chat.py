"""Long agent turns, sliced: chat_send / chat_wait / chat_cancel."""
from __future__ import annotations

import asyncio

import httpx

from tests.conftest import Rig, sse

CHAT = "/v1/chat/completions"


async def test_chat_send_returns_the_reply_and_the_session(rig: Rig):
    rig.hermes.on("POST", CHAT, text=sse(["PO", "NG"]), headers={"X-Hermes-Session-Id": "sess-1"})
    reply = await rig.call("chat_send", message="ping")
    assert reply["ok"] is True and reply["status"] == "done"
    assert reply["reply"] == "PONG" and reply["session_id"] == "sess-1" and reply["turn_id"]
    request = rig.hermes.sent("POST", CHAT)[0]
    assert "x-hermes-session-id" not in request.headers
    body = rig.hermes.last_json("POST", CHAT)
    assert body["stream"] is True and body["messages"][-1] == {"role": "user", "content": "ping"}
    assert "MCP client" in body["messages"][0]["content"]

    await rig.call("chat_send", message="again", session_id="sess-1")
    assert rig.hermes.sent("POST", CHAT)[-1].headers["x-hermes-session-id"] == "sess-1"


async def test_running_turn_is_collected_with_chat_wait(rig: Rig):
    release = asyncio.Event()

    async def slow_stream():
        yield sse(["part1"], done=False).encode()
        await release.wait()
        yield sse(["part2"]).encode()

    rig.hermes.handle("POST", CHAT, lambda _r: httpx.Response(200, content=slow_stream()))
    first = await rig.call("chat_send", message="think hard", wait_seconds=0)
    assert first["status"] == "running" and "chat_wait" in first["hint"]
    turn_id = first["turn_id"]

    await asyncio.sleep(0.05)
    partial = await rig.call("chat_wait", turn_id=turn_id, wait_seconds=0)
    assert partial["status"] == "running" and partial["reply"] == "part1"

    release.set()
    final = await rig.call("chat_wait", turn_id=turn_id, wait_seconds=5)
    assert final["status"] == "done" and final["reply"] == "part1part2" and final["ok"] is True


async def test_chat_cancel_interrupts_the_turn(rig: Rig):
    async def endless():
        yield sse(["working"], done=False).encode()
        await asyncio.Event().wait()

    rig.hermes.handle("POST", CHAT, lambda _r: httpx.Response(200, content=endless()))
    started = await rig.call("chat_send", message="loop forever", wait_seconds=0)
    await asyncio.sleep(0.05)
    cancelled = await rig.call("chat_cancel", turn_id=started["turn_id"])
    assert cancelled["status"] == "cancelled" and cancelled["reply"] == "working"
    assert (await rig.call("chat_wait", turn_id=started["turn_id"]))["status"] == "cancelled"

    unknown = await rig.call("chat_cancel", turn_id="nope")
    assert unknown["ok"] is False and unknown["code"] == "unknown_turn"
    unknown = await rig.call("chat_wait", turn_id="nope")
    assert unknown["ok"] is False and unknown["code"] == "unknown_turn"


async def test_hermes_refusal_is_the_turns_error(rig: Rig):
    rig.hermes.on("POST", CHAT, {"detail": "bad key"}, status=401)
    reply = await rig.call("chat_send", message="hi")
    assert reply["ok"] is False and reply["status"] == "error"
    assert "401" in reply["error"] and "bad key" in reply["error"]
