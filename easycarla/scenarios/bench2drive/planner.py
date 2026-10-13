"""Bounded immutable-map topology reuse for CARLA 0.9.15 route planners.

Only graph construction is shared. Each native consumer receives its own planner
object and turn-decision state. No world, actor, controller or episode is cached.
"""
from copy import copy
import hashlib

_template = None
_key = None


def GlobalRoutePlanner(world_map, sampling_resolution):
    from agents.navigation.global_route_planner import GlobalRoutePlanner as CarlaPlanner
    return cached_planner(world_map, sampling_resolution, CarlaPlanner)


def cached_planner(world_map, sampling_resolution, builder):
    global _key, _template
    resolution = float(sampling_resolution)
    if resolution <= 0:
        raise ValueError('Route-planner sampling resolution must be positive')
    key = (hashlib.sha256(world_map.to_opendrive().encode()).hexdigest(), resolution, builder)
    if key != _key:
        # Only one map/resolution is retained. Assign after a successful build;
        # a failed constructor must not poison subsequent initialization.
        template = builder(world_map, resolution)
        _key, _template = key, template
    # CARLA's trace_route mutates _previous_decision/_intersection_end_node, but
    # reads topology, graph and waypoint indexes. Never return the template.
    return copy(_template)


def clear_cache():
    global _key, _template
    _key = _template = None
