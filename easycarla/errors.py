"""Backend failures and preflight rejections, independent of the runtime."""


class EnvironmentFault(RuntimeError):
    code = "environment_fault"

    def __init__(self, message, cause=None):
        super().__init__(message)
        self.details = dict(getattr(cause, "details", {}))


class WorldInitializationTimeout(EnvironmentFault):
    """A requested world has not finished native initialization; do not continue."""


class RequestRejected(ValueError, RuntimeError):
    """Rejected before execution; the existing episode remains usable."""


class ControlConflict(EnvironmentFault):
    code = "control_conflict"
