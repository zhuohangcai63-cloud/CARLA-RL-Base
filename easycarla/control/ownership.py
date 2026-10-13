"""One control writer per actor per physical tick."""
from __future__ import annotations

from .actions import validate_action, validate_applied_control
from easycarla.errors import ControlConflict


class ControlArbiter:
    """Bookkeeping for adapters; claiming does not disable autopilot itself.

    The adapter must stop the old source before release/claim. Runtime code must
    send all writes through submit, including scenario and TM hand-offs.
    """

    def __init__(self):
        self._owners = {}
        self._submitted = {}
        self._tick = None

    def claim(self, actor_id, owner: str):
        if not owner:
            raise ValueError("control owner must be nonempty")
        current = self._owners.get(actor_id)
        if current is not None and current != owner:
            raise ControlConflict(f"actor {actor_id!r} is controlled by {current!r}")
        self._owners[actor_id] = owner

    def release(self, actor_id, owner: str):
        if self._owners.get(actor_id) != owner:
            raise ControlConflict(f"{owner!r} does not own actor {actor_id!r}")
        del self._owners[actor_id]

    def owner(self, actor_id):
        return self._owners.get(actor_id)

    def begin_tick(self, tick: int):
        if self._tick is not None and tick <= self._tick:
            raise ControlConflict("physical ticks must increase monotonically")
        self._tick = tick
        self._submitted.clear()

    def submit(self, actor_id, owner: str, control, *, allow_combined=False):
        if self._tick is None:
            raise ControlConflict("begin_tick must precede a control submission")
        if self._owners.get(actor_id) != owner:
            raise ControlConflict(f"{owner!r} does not own actor {actor_id!r}")
        if actor_id in self._submitted:
            raise ControlConflict(f"actor {actor_id!r} already has a control at tick {self._tick}")
        result = (validate_applied_control if allow_combined else validate_action)(control)
        self._submitted[actor_id] = (owner, result.copy())
        return result

    def state_dict(self):
        return {"tick": self._tick, "owners": dict(self._owners)}

    def clear(self):
        self._owners.clear()
        self._submitted.clear()
        self._tick = None
