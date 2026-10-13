"""Progress telemetry for native route wrappers, using only py_trees."""
from types import SimpleNamespace

import pytest

py_trees = pytest.importorskip("py_trees")

from easycarla.scenarios.bench2drive.progress import scenario_progress


Status = py_trees.common.Status
ATOMS = "srunner.scenariomanager.scenarioatomics."


class WaitForBlackboardVariable(py_trees.behaviour.Behaviour):
    __module__ = ATOMS + "atomic_trigger_conditions"

    def __init__(self, variable):
        super().__init__("route trigger")
        self._variable_name, self._variable_value = variable, True
        self.triggered = False

    def update(self):
        return Status.SUCCESS if self.triggered else Status.RUNNING


class WaitForever(py_trees.behaviours.Running):
    __module__ = ATOMS + "atomic_behaviors"


class MovePedestrianWithEgo(py_trees.behaviours.Running):
    __module__ = ATOMS + "atomic_behaviors"


class ScenarioBody(py_trees.behaviour.Behaviour):
    def __init__(self):
        super().__init__("scenario body")
        self.next_status = Status.RUNNING

    def update(self):
        return self.next_status


def make_scenario(trigger_kind="simple"):
    variable = "native_progress_test.route"
    body = ScenarioBody()
    trigger = WaitForBlackboardVariable(variable)
    wrapper = py_trees.composites.Sequence(body.name)
    if trigger_kind == "simple":
        wrapper.add_child(trigger)
    elif trigger_kind == "pedestrian":
        parallel = py_trees.composites.Parallel(policy=py_trees.common.ParallelPolicy.SUCCESS_ON_ONE)
        parallel.add_children([MovePedestrianWithEgo("walker 1"), MovePedestrianWithEgo("walker 2"), trigger])
        wrapper.add_child(parallel)
    end = py_trees.composites.Sequence("route end")
    end.add_children([py_trees.blackboard.SetBlackboardVariable("reset route", variable, False),
                      WaitForever("WaitForever")])
    wrapper.add_children([body, end])
    scenario = SimpleNamespace(route_mode=True, config=SimpleNamespace(route_var_name=variable),
                               behavior_tree=wrapper)
    return scenario, trigger, body


@pytest.mark.parametrize("trigger_kind", ["simple", "pedestrian"])
def test_waiting_wrapper_is_not_activation_and_inner_success_is_completion(trigger_kind):
    scenario, trigger, body = make_scenario(trigger_kind)
    scenario.behavior_tree.tick_once()
    waiting = scenario_progress(scenario)
    assert waiting["wrapper_status"] == "Status.RUNNING"
    assert waiting["actual_behavior_status"] == "Status.INVALID"
    assert waiting["activation_observed"] is False
    assert waiting["completion_observed"] is False

    trigger.triggered = True
    scenario.behavior_tree.tick_once()
    active = scenario_progress(scenario)
    assert active["activation_observed"] is True
    assert active["completion_observed"] is False

    body.next_status = Status.SUCCESS
    scenario.behavior_tree.tick_once()
    complete = scenario_progress(scenario)
    assert complete["wrapper_status"] == "Status.RUNNING"
    assert complete["actual_behavior_status"] == "Status.SUCCESS"
    assert complete["completion_observed"] is True
    assert complete["reason"] is None

    # The caller must latch the preceding observations before native teardown.
    scenario.behavior_tree.stop(Status.INVALID)
    invalidated = scenario_progress(scenario)
    assert invalidated["activation_observed"] is False
    assert invalidated["completion_observed"] is False
    assert any("latch" in text for text in invalidated["limitations"])


@pytest.mark.parametrize("body_status", [Status.SUCCESS, Status.FAILURE])
def test_optional_trigger_and_body_failure_are_reported_without_ticking(body_status):
    scenario, _, body = make_scenario("none")
    body.next_status = body_status
    scenario.behavior_tree.tick_once()
    expected_statuses = [node.status for node in scenario.behavior_tree.iterate()]
    progress = scenario_progress(scenario)
    assert progress["schema_version"] == 1
    assert progress["extraction"] == "pinned_basic_scenario_route_wrapper_v1"
    assert progress["trigger_status"] is None
    assert progress["activation_observed"] is True
    assert progress["completion_observed"] is (body_status == Status.SUCCESS)
    assert [node.status for node in scenario.behavior_tree.iterate()] == expected_statuses


@pytest.mark.parametrize("malformation", [
    "not_route_mode", "wrong_reset_variable", "wrong_reset_value", "wrong_forever_node",
    "wrong_trigger_variable", "unknown_trigger", "extra_child", "wrong_wrapper_name",
])
def test_unrecognized_layout_is_explicitly_unknown(malformation):
    scenario, trigger, body = make_scenario()
    wrapper = scenario.behavior_tree
    end = wrapper.children[-1]
    if malformation == "not_route_mode":
        scenario.route_mode = False
    elif malformation == "wrong_reset_variable":
        end.children[0].variable_name = "another_route"
    elif malformation == "wrong_reset_value":
        end.children[0].variable_value = True
    elif malformation == "wrong_forever_node":
        end.replace_child(end.children[1], py_trees.behaviours.Running("unrecognized idle"))
    elif malformation == "wrong_trigger_variable":
        trigger._variable_name = "another_route"
    elif malformation == "unknown_trigger":
        wrapper.replace_child(trigger, py_trees.behaviours.Success("unknown trigger"))
    elif malformation == "extra_child":
        wrapper.add_child(py_trees.behaviours.Success("unexpected child"))
    elif malformation == "wrong_wrapper_name":
        wrapper.name = "not the scenario body"
    body.status = Status.SUCCESS
    progress = scenario_progress(scenario)
    assert progress["extraction"] == "unsupported"
    assert progress["reason"]
    assert progress["activation_observed"] is None
    assert progress["completion_observed"] is None
    assert progress["actual_behavior_status"] is None
