"""Pure geometric fixtures: no CARLA package, server, tick or actor required."""
import math
from types import SimpleNamespace

import pytest

from easycarla.scenarios.reactive_routes import RouteGeometryError, build_reactive_route


class Location:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z
    def distance(self, other):
        return math.sqrt((self.x-other.x)**2 + (self.y-other.y)**2 + (self.z-other.z)**2)


class Rotation:
    def __init__(self, yaw=0, pitch=0):
        self.yaw, self.pitch = yaw, pitch


class Transform:
    def __init__(self, location, rotation=None):
        self.location, self.rotation = location, rotation or Rotation()


CARLA = SimpleNamespace(Location=Location, Rotation=Rotation, Transform=Transform,
                        LaneType=SimpleNamespace(Driving="driving"))


class Waypoint:
    def __init__(self, x=0, y=0, yaw=0, road=1, lane=-1, junction=None, bad_adjacent=False):
        self.transform = Transform(Location(x, y), Rotation(yaw))
        self.road_id, self.lane_id, self.s = road, lane, x + y
        self.lane_type = "driving"
        self.is_junction = junction is not None
        self._junction, self.bad_adjacent = junction, bad_adjacent
    def next(self, distance):
        loc, angle = self.transform.location, math.radians(self.transform.rotation.yaw)
        return [Waypoint(loc.x+distance*math.cos(angle), loc.y+distance*math.sin(angle),
                         self.transform.rotation.yaw, self.road_id, self.lane_id,
                         self._junction, self.bad_adjacent)]
    def previous(self, distance):
        return self.next(-distance)
    def get_right_lane(self):
        loc = self.transform.location
        return Waypoint(loc.x, loc.y+4, 180 if self.bad_adjacent else self.transform.rotation.yaw,
                        self.road_id, 1 if self.bad_adjacent else -2)
    def get_left_lane(self):
        loc = self.transform.location
        return Waypoint(loc.x, loc.y-4, self.transform.rotation.yaw, self.road_id, -2)
    def get_junction(self):
        return self._junction


class RoadMap:
    def __init__(self, crossing=False, bad_adjacent=False):
        self.bad_adjacent = bad_adjacent
        self.junction = SimpleNamespace(id=42, get_waypoints=lambda lane_type: [
            (Waypoint(50,-5,90,2,1), Waypoint(50,5,90,2,1)),
            (Waypoint(50,5,-90,3,-1), Waypoint(50,-5,-90,3,-1))]) if crossing else None
    def get_waypoint(self, location):
        junction = self.junction if 44 <= location.x <= 56 else None
        return Waypoint(location.x, location.y, junction=junction, bad_adjacent=self.bad_adjacent)


class Planner:
    def trace_route(self, start, end):
        length = start.distance(end)
        yaw = math.degrees(math.atan2(end.y-start.y, end.x-start.x))
        count = math.ceil(length / 2)
        return [(Waypoint(start.x+(end.x-start.x)*i/count, start.y+(end.y-start.y)*i/count, yaw), None)
                for i in range(count+1)]


EGO_ROUTE = [Transform(Location(x,0)) for x in range(0,202,2)]


def test_merge_begins_in_requested_lane_and_smoothly_joins_ego_lane():
    route = build_reactive_route(RoadMap(), EGO_ROUTE,
                                 {"route_kind":"merge", "side":"right", "distance":20,"merge_length":20}, CARLA)
    points = [t.location for t in route.transforms]
    assert (points[0].x, points[0].y) == (20,4)
    assert points[10].y == 0
    assert all(a.x < b.x and a.y >= b.y for a,b in zip(points,points[1:]))
    assert route.metadata["route_kind"] == "merge"


def test_merge_rejects_opposing_adjacent_lane_and_missing_side():
    with pytest.raises(RouteGeometryError, match="opposing"):
        build_reactive_route(RoadMap(bad_adjacent=True), EGO_ROUTE,
                             {"route_kind":"merge","side":"right"}, CARLA)
    with pytest.raises(RouteGeometryError, match="explicit"):
        build_reactive_route(RoadMap(), EGO_ROUTE, {"route_kind":"merge"}, CARLA)


@pytest.mark.parametrize("side,sign", [("left",-1),("right",1)])
def test_crossing_selects_actual_transverse_junction_route_on_requested_side(side,sign):
    route = build_reactive_route(RoadMap(crossing=True), EGO_ROUTE,
                                 {"route_kind":"crossing","side":side}, CARLA, Planner())
    assert route.metadata["route_kind"] == "crossing"
    assert route.metadata["ego_distance_to_conflict"] == pytest.approx(50)
    assert route.transforms[0].location.y * sign > 20
    assert route.transforms[-1].location.y * sign < -20
    assert route.metadata["side"] == side


def test_crossing_without_junction_fails_without_becoming_follow():
    with pytest.raises(RouteGeometryError, match="no transverse"):
        build_reactive_route(RoadMap(), EGO_ROUTE, {"route_kind":"crossing"}, CARLA, Planner())


def test_grade_separated_roads_do_not_count_as_a_crossing():
    class OverpassPlanner(Planner):
        def trace_route(self, start, end):
            route = super().trace_route(start, end)
            for waypoint, _ in route:
                waypoint.transform.location.z = 10
            return route
    with pytest.raises(RouteGeometryError, match="no transverse"):
        build_reactive_route(RoadMap(crossing=True), EGO_ROUTE,
                             {"route_kind":"crossing"}, CARLA, OverpassPlanner())


def test_follow_preserves_forward_path_and_invalid_family_fails():
    route = build_reactive_route(RoadMap(), EGO_ROUTE, {"distance":22}, CARLA)
    assert route.transforms[0].location.x == 22
    assert len(route.transforms) == 80
    assert all(t.location.y == 0 for t in route.transforms)
    with pytest.raises(RouteGeometryError, match="Unsupported"):
        build_reactive_route(RoadMap(), EGO_ROUTE, {"route_kind":"typo"}, CARLA)
