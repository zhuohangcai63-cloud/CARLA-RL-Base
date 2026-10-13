"""Validated CARLA 0.9.15 geometry, with explicit unavailable measurements."""

import math
import numpy as np
from .geometry import xyz

D = np.diag([1.0, -1.0, 1.0])


def rotation(transform):
    if hasattr(transform, "get_matrix"):
        return D @ np.asarray(transform.get_matrix(), float)[:3, :3] @ D
    r = transform.rotation
    y, p, b = map(math.radians, (r.yaw, getattr(r, "pitch", 0), getattr(r, "roll", 0)))
    cy, sy, cp, sp, cb, sb = (
        math.cos(y),
        math.sin(y),
        math.cos(p),
        math.sin(p),
        math.cos(b),
        math.sin(b),
    )
    return (
        D
        @ np.array(
            [
                [cp * cy, cy * sp * sb - sy * cb, -cy * sp * cb - sy * sb],
                [cp * sy, sy * sp * sb + cy * cb, -sy * sp * cb + cy * sb],
                [sp, -cp * sb, cp * cb],
            ]
        )
        @ D
    )


def world_origin_from_heartbeat(snapshot, ego_id, packet):
    """Identity-attached IMU header is UE-local; snapshot is CARLA-global.

    CARLA 0.9.15 serializes Sensor.GetActorTransform into sensor headers.
    UE map origins are integer centimeters. Never guess a tile-grid origin.
    """
    if packet.frame != snapshot.frame:
        raise ValueError("heartbeat and world snapshot frames differ")
    actor = snapshot.find(ego_id)
    if actor is None:
        raise ValueError("ego missing from heartbeat world frame")
    transform = actor.get_transform()
    offset = xyz(transform.location) - xyz(packet.transform.location)
    rounded = np.round(offset * 100) / 100
    if not np.isfinite(offset).all() or np.max(np.abs(offset - rounded)) > 0.003:
        raise ValueError(
            "sensor/world map origin is not an integer centimeter translation"
        )
    if not np.allclose(rotation(transform), rotation(packet.transform), atol=1e-4):
        raise ValueError("identity heartbeat attachment rotation differs from ego")
    return rounded


def can_query_vehicle_physics(actor, physics_enabled):
    """Only query a live, active vehicle with explicitly enabled physics.

    CARLA 0.9.15 can crash natively when this RPC reaches a disabled vehicle.
    Unknown lifecycle state must never be treated as enabled.
    """
    return (actor.type_id.startswith("vehicle.")
            and isinstance(physics_enabled, (bool, np.bool_)) and bool(physics_enabled)
            and bool(getattr(actor, "is_alive", True))
            and bool(getattr(actor, "is_active", True)))


def geometry(actor, world_origin=None, physics_enabled=True):
    """Wheel positions in the 0.9.15 RPC are UE world centimeters.

    Convert once to actor-local meters; never reuse spawn-world wheel positions.
    A measurement outside plausible passenger/road vehicle bounds is invalid.
    """
    bbox = actor.bounding_box
    offset = xyz(bbox.location) if hasattr(bbox, "location") else np.zeros(3)
    result = {
        "bbox_offset": offset.tolist(),
        "bbox_valid": hasattr(bbox, "location"),
        "bbox_yaw": -math.radians(getattr(getattr(bbox, "rotation", None), "yaw", 0)),
        "rear_offset": [0.0, 0.0, 0.0],
        "wheelbase": 0.0,
        "max_steer_angle": 0.0,
        "rear_valid": False,
        "center_of_mass_offset": [0.0, 0.0, 0.0],
        "center_of_mass_valid": not actor.type_id.startswith("vehicle."),
        "physics_pending": False,
        "source": "carla_0.9.15_bbox_and_physics_wheels",
    }
    if not actor.type_id.startswith("vehicle."):
        return result
    if not can_query_vehicle_physics(actor, physics_enabled):
        # 0.9.15 GetVehiclePhysicsControl dereferences PVehicle after a
        # disabled vehicle destroyed it. A native crash cannot be caught here.
        result["physics_pending"] = True
        result["source"] = "carla_bbox; physics_disabled_or_inactive"
        return result
    try:
        physics = actor.get_physics_control()
        wheels = physics.wheels
        if hasattr(physics, "center_of_mass"):
            mass_center = xyz(physics.center_of_mass)
            if np.isfinite(mass_center).all() and np.max(np.abs(mass_center)) < 15:
                result.update(
                    center_of_mass_offset=mass_center.tolist(),
                    center_of_mass_valid=True,
                )
        if len(wheels) != 4:
            return result
        transform = actor.get_transform()
        world = np.array([xyz(w.position) for w in wheels]) * 0.01
        if world_origin is not None:
            world += world_origin
        offsets = (world - xyz(transform.location)) @ rotation(transform)
        front, rear = offsets[:2].mean(axis=0), offsets[2:].mean(axis=0)
        wheelbase = float(front[0] - rear[0])
        steer = math.radians(max(w.max_steer_angle for w in wheels[:2]))
        if (
            np.isfinite(offsets).all()
            and 0.5 < wheelbase < 12
            and np.max(np.abs(offsets)) < 15
            and 0 < steer < math.pi / 2
        ):
            result.update(
                rear_offset=rear.tolist(),
                wheelbase=wheelbase,
                max_steer_angle=steer,
                rear_valid=True,
            )
    except (AttributeError, RuntimeError, TypeError):
        pass
    return result


def enrich(actor, physical, state, cached, carla=None):
    transform = physical.get_transform()
    rot = rotation(transform)
    origin = np.asarray(state["position"])
    rear = rot @ np.asarray(cached["rear_offset"])
    bbox = rot @ np.asarray(cached["bbox_offset"])
    state.update(
        geometry=cached,
        bbox_center=(origin + bbox).tolist(),
        bbox_yaw=state["yaw"] + cached["bbox_yaw"],
        rear_axle=(origin + rear).tolist(),
        object_type=object_type(actor),
        steering_angle=0.0,
        steering_valid=False,
    )
    # Vehicle physics linear velocity is at the body's mass center. CARLA's
    # physics control exposes its local offset in meters, unlike wheel RPCs.
    omega = state["yaw_rate"]
    mass_center = rot @ np.asarray(cached["center_of_mass_offset"])
    rear -= mass_center
    bbox -= mass_center
    state["rear_velocity"] = (
        np.asarray(state["velocity"]) + np.array([-omega * rear[1], omega * rear[0], 0])
    ).tolist()
    state["bbox_velocity"] = (
        np.asarray(state["velocity"]) + np.array([-omega * bbox[1], omega * bbox[0], 0])
    ).tolist()
    state["rear_offset_world"] = rear.tolist()
    state["bbox_offset_world"] = bbox.tolist()
    if carla is not None and actor.type_id.startswith("vehicle."):
        try:
            loc = carla.VehicleWheelLocation
            angles = [
                actor.get_wheel_steer_angle(loc.FL_Wheel),
                actor.get_wheel_steer_angle(loc.FR_Wheel),
            ]
            value = -math.radians(sum(angles) / 2)
            if math.isfinite(value):
                state.update(steering_angle=value, steering_valid=True)
        except (AttributeError, RuntimeError, TypeError):
            pass
    return state


def object_type(actor):
    """1 vehicle, 2 pedestrian, 3 bicycle, 4 motorcycle; 0 unknown."""
    if actor.type_id.startswith("walker."):
        return 2
    if not actor.type_id.startswith("vehicle."):
        return 0
    kind = actor.type_id.lower()
    if any(s in kind for s in ("crossbike", "omafiets", "century")):
        return 3
    if any(s in kind for s in ("harley", "kawasaki", "vespa", "yamaha")):
        return 4
    # Unrecognized two-wheel blueprints must not be silently called bicycles.
    if str(getattr(actor, "attributes", {}).get("number_of_wheels", "4")) == "2":
        return 0
    return 1


def static_type(kind):
    text = kind.lower()
    if "cone" in text:
        return 3
    if "barrier" in text:
        return 2
    if any(v in text for v in ("construction", "warning")) and "sign" in text:
        return 1
    return 4
