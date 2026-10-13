from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable
import carla

@dataclass
class SideSelection:
    side: str
    waypoint: carla.Waypoint
    spawn_transform: carla.Transform | None = None


@runtime_checkable
class EventLifecycle(Protocol):
    """Prepared event interface used by a scenario's physical-tick lifecycle.

    Construction/preparation accepts family-specific configuration. Once prepared,
    every family has the same lifecycle and versioned execution-state contract.
    """

    def before_tick(self) -> None: ...
    def after_tick(self) -> None: ...
    def close(self) -> None: ...
    def info(self) -> dict[str, Any]: ...
    def export_state(self, actor_roles: Mapping[int, str]) -> dict[str, Any]: ...
