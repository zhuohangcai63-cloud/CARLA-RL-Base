"""Public standalone EasyCARLA client API."""
from .client import CarlaEnvClient
from .errors import (CarlaClientError, ClientBusy, InstanceBusy, ProtocolError,
                     RemoteError, ServiceUnavailable, UnknownOutcomeError)
from .protocol import VERSION as PROTOCOL_VERSION, copy_observation
from .pool import ClientPool, Completion


def __getattr__(name):
    # Optional Gymnasium stays out of the NumPy/POSIX-only core import path.
    if name == "CarlaGymEnv":
        from .gym_env import CarlaGymEnv
        return CarlaGymEnv
    if name in ("CarlaVectorEnv", "BatchError"):
        from . import vector_env
        return getattr(vector_env, name)
    raise AttributeError(name)

__all__ = ["CarlaEnvClient", "copy_observation", "CarlaClientError", "ClientBusy",
           "InstanceBusy", "ProtocolError", "RemoteError", "ServiceUnavailable",
           "UnknownOutcomeError", "PROTOCOL_VERSION", "ClientPool", "Completion"]
