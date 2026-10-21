"""Reply retention never discards live work."""
import time

from mintbot_mcp.chat import Turn, TurnRegistry, TTL_SECONDS, MAX_TURNS


def test_expired_finished_turn_is_not_retained_by_reading_it(rig):
    registry = rig.backends.turns
    turn = Turn('expired', 's1')
    turn.finish('done')
    turn.finished_at = time.monotonic() - TTL_SECONDS - 1
    registry._turns[turn.id] = turn
    assert registry.get(turn.id) is None


def test_capacity_never_evicts_running_turns(rig):
    registry = rig.backends.turns
    for n in range(MAX_TURNS + 5):
        registry._turns[str(n)] = Turn(str(n), 's1')
    registry._evict()
    assert len(registry._turns) == MAX_TURNS + 5
