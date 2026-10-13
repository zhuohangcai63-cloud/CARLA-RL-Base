"""Small, simulator-neutral contracts for executable scene sources.

Implement these methods rather than adding source-name checks to the runtime.
CARLA actor handles stay inside the service; serialized outputs contain data only.
"""
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class ResolvedControl:
    values: np.ndarray
    allow_combined: bool = False


@dataclass(frozen=True)
class ScenarioFacts:
    terminated: bool = False
    truncated: bool = False


@runtime_checkable
class EpisodeProtocol(Protocol):
    ego: Any
    route: Sequence[Any]
    metadata: dict
    controllers: dict
    task_profiles: frozenset[str]

    def before_tick(self) -> None: ...
    def after_tick(self, snapshot: Any) -> None: ...
    def resolve_control(self, requested: np.ndarray) -> ResolvedControl: ...
    def scenario_facts(self) -> ScenarioFacts: ...
    def info(self) -> dict: ...
    def capabilities(self) -> dict: ...
    def export_state(self, actor_roles: Mapping[int, str]) -> dict: ...
    def takeover(self, role: str) -> dict: ...
    def close(self) -> None: ...


class ScenarioAdapter(Protocol):
    def town(self, seed: int, options: dict) -> str: ...
    def prepare(self, session: Any, seed: int, options: dict) -> EpisodeProtocol: ...


def validate_episode(episode: EpisodeProtocol, task_profile: str) -> None:
    if not isinstance(episode, EpisodeProtocol):
        raise TypeError("Scenario episode does not implement the executable episode contract")
    if task_profile not in episode.task_profiles:
        raise ValueError(f"Scenario does not support task profile {task_profile!r}")
    if not isinstance(episode.controllers, dict):
        raise TypeError("Episode controllers must be a role-to-controller mapping")
    if not episode.route:
        raise ValueError("An executable episode must supply a nonempty route")
