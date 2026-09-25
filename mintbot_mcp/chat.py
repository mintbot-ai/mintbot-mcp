"""Long agent turns, sliced for MCP clients.

An agent turn can run for minutes (tools, subagents), longer than most MCP
clients wait for a tool result. So a turn is pumped by a background task into
a buffer, and the client collects the reply in slices: ``chat_send`` waits up
to its budget, a turn still running is handed back as ``running`` with a
``turn_id``, and ``chat_wait`` picks it up from there. Cancelling the task
closes the loopback stream, which is exactly how Hermes learns to interrupt
the agent (the panel's Stop button uses the same mechanism).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .backends import HermesClient

TTL_SECONDS = 15 * 60
MAX_TURNS = 32
MAX_WAIT_SECONDS = 600


@dataclass
class Turn:
    id: str
    session_id: str | None
    chunks: list[str] = field(default_factory=list)
    status: str = "running"  # running | done | error | cancelled
    error: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def reply(self) -> str:
        return "".join(self.chunks)

    @property
    def running(self) -> bool:
        return self.status == "running"

    def finish(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error
        self.finished_at = time.monotonic()
        self.changed.set()

    def snapshot(self) -> dict[str, Any]:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        out: dict[str, Any] = {
            "ok": self.status != "error",
            "turn_id": self.id,
            "session_id": self.session_id,
            "status": self.status,
            "reply": self.reply,
            "elapsed_seconds": round(end - self.started_at, 1),
        }
        if self.error:
            out["error"] = self.error
        if self.running:
            out["hint"] = "the agent is still working; call chat_wait with this turn_id to keep collecting the reply"
        return out


class TurnRegistry:
    def __init__(self, hermes: HermesClient) -> None:
        self._hermes = hermes
        self._turns: dict[str, Turn] = {}

    def get(self, turn_id: str) -> Turn | None:
        return self._turns.get(turn_id)

    async def start(self, message: str, *, session_id: str | None = None) -> Turn:
        self._evict()
        turn = Turn(id=uuid.uuid4().hex[:12], session_id=session_id)
        turn.task = asyncio.create_task(self._pump(turn, message))
        self._turns[turn.id] = turn
        return turn

    async def _pump(self, turn: Turn, message: str) -> None:
        try:
            async for kind, value in self._hermes.stream_turn(message, session_id=turn.session_id):
                if kind == "session":
                    turn.session_id = value
                else:
                    turn.chunks.append(value)
                turn.changed.set()
            turn.finish("done")
        except asyncio.CancelledError:
            turn.finish("cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - the error is the turn's result
            turn.finish("error", f"{type(exc).__name__}: {exc}")

    async def wait(self, turn: Turn, seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, min(float(seconds), MAX_WAIT_SECONDS))
        while turn.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            turn.changed.clear()
            try:
                await asyncio.wait_for(turn.changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
        return turn.snapshot()

    def cancel(self, turn_id: str) -> dict[str, Any]:
        turn = self._turns.get(turn_id)
        if turn is None:
            return {"ok": False, "code": "unknown_turn", "error": f"unknown turn_id {turn_id!r}"}
        if turn.running and turn.task is not None:
            turn.task.cancel()
            turn.finish("cancelled")
        return turn.snapshot()

    def _evict(self) -> None:
        now = time.monotonic()
        finished = sorted(
            (t for t in self._turns.values() if not t.running and t.finished_at is not None),
            key=lambda t: t.finished_at or 0.0,
        )
        for turn in finished:
            if now - (turn.finished_at or now) > TTL_SECONDS or len(self._turns) >= MAX_TURNS:
                self._turns.pop(turn.id, None)
