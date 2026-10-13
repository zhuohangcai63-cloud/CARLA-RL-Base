"""Exercise real Scenic episode construction/rebuild with fake CARLA resources.

The route rejection loop and actual vehicle/walker sampling methods run; only
Scenic compilation, route planning, CARLA resources and event execution are fake.
"""
from copy import deepcopy
import math
import sys
from types import ModuleType, SimpleNamespace as NS

from easycarla.scenarios.scenic_source import ScenicAdapter
from easycarla.simulation.resources import ActorRegistry


class Vector:
    def __init__(self, x=0., y=0., z=0.):
        self.x, self.y, self.z = float(x), float(y), float(z)
    def __sub__(self, other):
        return Vector(self.x-other.x, self.y-other.y, self.z-other.z)
    def distance(self, other):
        d = self-other
        return math.sqrt(d.dot(d))
    def dot(self, other):
        return self.x*other.x + self.y*other.y + self.z*other.z
    def values(self):
        return (self.x, self.y, self.z)


class Transform:
    def __init__(self, location=None, rotation=None):
        self.location = location or Vector()
        self.rotation = rotation or NS(yaw=0.)
    def get_forward_vector(self):
        yaw = math.radians(self.rotation.yaw)
        return Vector(math.cos(yaw), math.sin(yaw))


class Blueprint:
    def __init__(self, name):
        self.id = name
        self.attributes = {"role_name": "", "number_of_wheels": "4", "is_invincible": "true"}
    def has_attribute(self, key):
        return key in self.attributes
    def get_attribute(self, key):
        return self.attributes[key]
    def set_attribute(self, key, value):
        self.attributes[key] = str(value)


class BlueprintLibrary:
    def find(self, name):
        return Blueprint(name)
    def filter(self, pattern):
        names = ["vehicle.alpha", "vehicle.beta", "vehicle.gamma"] if pattern == "vehicle.*" else ["walker.pedestrian.alpha", "walker.pedestrian.beta"]
        return [Blueprint(name) for name in names]


class Actor:
    def __init__(self, actor_id, blueprint, transform, attach_to=None):
        self.id, self.type_id, self.transform = actor_id, blueprint.id, deepcopy(transform)
        self.attributes = dict(blueprint.attributes)
        self.attach_to = attach_to.id if attach_to else None
        self.autopilot = None
        self.speed = None
        self.target = None
        self.is_alive = True
    def get_location(self):
        return self.transform.location
    def set_autopilot(self, enabled, port):
        self.autopilot = (enabled, port)
    def start(self):
        pass
    def stop(self):
        pass
    def go_to_location(self, target):
        self.target = target.values()
    def set_max_speed(self, speed):
        self.speed = speed
    def destroy(self):
        self.is_alive = False
        return True


class ActorList(list):
    def filter(self, pattern):
        # This fixture has no lights; vehicle/walker creation uses the real path.
        assert pattern == "traffic.traffic_light*"
        return []


class World:
    def __init__(self):
        self.actors = ActorList()
        self.navigation_calls = 0
    def get_actors(self):
        return self.actors
    def get_blueprint_library(self):
        return BlueprintLibrary()
    def set_weather(self, weather):
        self.weather = weather
    def spawn_actor(self, blueprint, transform, attach_to=None):
        actor = Actor(100 + len(self.actors), blueprint, transform, attach_to)
        self.actors.append(actor)
        return actor
    try_spawn_actor = spawn_actor
    def get_random_location_from_navigation(self):
        self.navigation_calls += 1
        return Vector(500 + 10*self.navigation_calls, 20)
    def set_pedestrians_cross_factor(self, factor):
        self.cross_factor = factor


class WorldMap:
    name = "/Game/Carla/Maps/FixtureTown"
    def __init__(self, reject_first):
        self.reject_first = reject_first
        self.trace_calls = []
        self.points = [Transform(Vector(i*60, 0)) for i in range(6)]
    def get_spawn_points(self):
        return deepcopy(self.points)


class Planner:
    def __init__(self, world_map, resolution):
        self.world_map = world_map
    def trace_route(self, start, end):
        self.world_map.trace_calls.append((start.values(), end.values()))
        if self.world_map.reject_first and len(self.world_map.trace_calls) == 1:
            return [(NS(transform=Transform(deepcopy(start))), None)]
        yaw = 0. if end.x >= start.x else 180.
        middle = Vector((start.x+end.x)/2, (start.y+end.y)/2)
        return [(NS(transform=Transform(deepcopy(p), NS(yaw=yaw))), None)
                for p in (start, middle, end)]


class Event:
    def __init__(self, *args, **kwargs):
        pass
    def close(self):
        pass

    def setup(self, specs):
        self.specs = deepcopy(specs)


def install_fixture_modules(monkeypatch):
    # Stub imports at their narrow boundaries. No CARLA Python module is imported.
    for name in ("agents", "agents.navigation"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    planner_module = ModuleType("agents.navigation.global_route_planner")
    planner_module.GlobalRoutePlanner = Planner
    monkeypatch.setitem(sys.modules, planner_module.__name__, planner_module)
    events = ModuleType("easycarla.scenarios.events")
    events.__path__ = []
    events.LongTailEventManager = Event
    monkeypatch.setitem(sys.modules, events.__name__, events)
    for module, symbol in (("crossing", "CrossingEvent"), ("lead_brake", "LeadBrakeEvent")):
        stub = ModuleType("easycarla.scenarios.events." + module)
        setattr(stub, symbol, Event)
        monkeypatch.setitem(sys.modules, stub.__name__, stub)


def session(reject_first):
    ticks = []
    return NS(controls=NS(set_autopilot=lambda actor, enabled, owner: actor.set_autopilot(enabled, 8123),
                          claim=lambda *args, **kwargs: None), world=World(), map=WorldMap(reject_first), registry=ActorRegistry(),
              sim={"physics_dt": .05, "tm_port": 8123},
              carla=NS(Transform=Transform, WeatherParameters=NS(ClearNoon="clear")),
              tick=lambda phase: ticks.append(phase), ticks=ticks)


def sampled_actors(world, ego_id):
    return [{"type": actor.type_id, "position": actor.get_location().values(),
             "role": actor.attributes["role_name"], "autopilot": actor.autopilot,
             "controller_speed": actor.speed, "target": actor.target,
             "parent_id": actor.attach_to}
            for actor in world.actors if actor.id != ego_id]


def test_route_rejection_does_not_shift_rebuilt_background_sampling(monkeypatch, tmp_path):
    install_fixture_modules(monkeypatch)
    template = tmp_path / "fixture.scenic"
    template.write_text("# Compilation is replaced; construction and rejection sampling are real.\n")
    params = {"scenario_name": "sampling-regression", "weather": "ClearNoon",
              "ego_spawn_index": 0, "destination_spawn_index": 5, "min_route_distance": 50,
              "number_of_vehicles": 3, "number_of_walkers": 2,
              "walker_speed_min": 1.2, "walker_speed_max": 2.4}
    sampling_calls = []
    def sample(*args):
        sampling_calls.append(args)
        return deepcopy(params), 1
    monkeypatch.setattr("easycarla.scenarios.scenic_source.sample_parameters", sample)
    adapter = ScenicAdapter({"file": str(template), "route_attempts": 3})
    first_session, rebuilt_session = session(True), session(False)
    first = rebuilt = None
    try:
        first = adapter.prepare(first_session, seed=37, options={})
        assert first.ego.attributes["role_name"] == "hero", "Scenic ego must be a CARLA large-map streaming reference"
        assert first_session.registry.roles[first.ego.id] == "scenic/ego/1", "Research identity must remain stable"
        assert len(first_session.map.trace_calls) == 2
        assert len(first.metadata["generation_failures"]) == 1
        assert first.metadata["generation_failures"][0]["reason"] == "route_not_forward_or_too_short"
        recipe = deepcopy(first.metadata)
        rebuilt = adapter.prepare(rebuilt_session, seed=37, options={"recipe": recipe})
        assert rebuilt.ego.attributes["role_name"] == "hero"
        assert rebuilt_session.registry.roles[rebuilt.ego.id] == "scenic/ego/1"
        assert len(rebuilt_session.map.trace_calls) == 1, "Recipe must use the accepted route without repeating rejected draws"
        assert rebuilt.metadata["generation_failures"] == []
        assert len(sampling_calls) == 1, "Rebuild must consume recipe parameters without another Scenic sample"
        assert first.parameters == rebuilt.parameters
        assert [(p.location.values(), p.rotation.yaw) for p in first.route] == [(p.location.values(), p.rotation.yaw) for p in rebuilt.route]
        assert len(first.vehicles) == len(rebuilt.vehicles) == 3
        assert first_session.world.navigation_calls == rebuilt_session.world.navigation_calls == 4
        assert len(sampled_actors(first_session.world, first.ego.id)) == 7  # 3 vehicles, 2 walkers, 2 walker controllers.
        assert sampled_actors(first_session.world, first.ego.id) == sampled_actors(rebuilt_session.world, rebuilt.ego.id)
        assert first.rng.getstate() == rebuilt.rng.getstate()
        assert first_session.registry.roles == rebuilt_session.registry.roles
        assert first_session.ticks == rebuilt_session.ticks == ["initialization"] * 4
    finally:
        if first is not None:
            first.close()
        if rebuilt is not None:
            rebuilt.close()
        first_session.registry.clear()
        rebuilt_session.registry.clear()
