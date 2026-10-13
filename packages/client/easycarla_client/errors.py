"""Public errors. UnknownOutcomeError must never trigger automatic action replay."""

class CarlaClientError(RuntimeError):
    pass


class ProtocolError(CarlaClientError):
    pass


class ServiceUnavailable(CarlaClientError):
    pass


class InstanceBusy(CarlaClientError):
    pass


class ClientBusy(CarlaClientError):
    pass


class UnknownOutcomeError(CarlaClientError):
    """A submitted request may have executed; reconnect and reset explicitly."""


class RemoteError(CarlaClientError):
    def __init__(self, code, message, *, details=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = details or {}


def is_request_rejection(error):
    """A server-confirmed rejection before mutation; safe to keep the episode."""
    return (isinstance(error, RemoteError) and error.code == "invalid_request"
            and error.details.get("request_rejected") is True
            and not error.details.get("episode_faulted")
            and not error.details.get("command_may_have_executed"))
