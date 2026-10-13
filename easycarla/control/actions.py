"""Control values at the public boundary (CARLA steering sign)."""
from __future__ import annotations

import numpy as np


def validate_action(action) -> np.ndarray:
    """Return [steer, throttle, brake], without clipping invalid requests.

    Positive steer is right. Throttle and brake are separate fractions, and
    simultaneous positive inputs are rejected rather than silently prioritized.
    """
    return _validate(action, allow_combined=False)


def validate_applied_control(action) -> np.ndarray:
    """Validate native control after explicit scenario transformations.

    Native disturbance scenarios can legitimately combine throttle and brake.
    This is never the public client-action validator: the runtime must select it
    explicitly for a native applied-control path and log requested/applied values.
    """
    return _validate(action, allow_combined=True)


def _validate(action, *, allow_combined) -> np.ndarray:
    try:
        values = np.asarray(action, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("action must contain three finite numeric values") from exc
    if values.shape != (3,) or not np.isfinite(values).all():
        raise ValueError("action must be finite [steer, throttle, brake] of shape (3,)")
    if not (-1 <= values[0] <= 1 and 0 <= values[1] <= 1 and 0 <= values[2] <= 1):
        raise ValueError("steer must be in [-1,1]; throttle and brake in [0,1]")
    if not allow_combined and values[1] > 0 and values[2] > 0:
        raise ValueError("throttle and brake cannot both be positive")
    return values.astype(np.float32)


def legacy_action(action) -> np.ndarray:
    """Explicit migration adapter for the former [steer, longitudinal] API."""
    values = np.asarray(action, dtype=np.float64)
    if values.shape != (2,) or not np.isfinite(values).all() or np.any(np.abs(values) > 1):
        raise ValueError("legacy action must have two finite values in [-1,1]")
    return validate_action([values[0], max(values[1], 0), max(-values[1], 0)])
