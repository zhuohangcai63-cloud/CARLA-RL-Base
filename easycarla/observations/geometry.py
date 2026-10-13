"""CARLA left-handed coordinates -> right-handed world and ego FLU, SI units."""
import math
import numpy as np


def xyz(vector):
    return np.array([vector.x, -vector.y, vector.z], dtype=np.float64)


def pose(transform):
    return xyz(transform.location), -math.radians(transform.rotation.yaw)


def local(points, origin, yaw, vector=False):
    values = np.asarray(points, dtype=np.float64)
    delta = values.copy() if vector else values - np.asarray(origin)
    c, s = math.cos(yaw), math.sin(yaw)
    x, y = delta[..., 0].copy(), delta[..., 1].copy()
    delta[..., 0] = c * x + s * y
    delta[..., 1] = -s * x + c * y
    return delta.astype(np.float32)


def wrap(angle):
    return (angle + np.pi) % (2 * np.pi) - np.pi
