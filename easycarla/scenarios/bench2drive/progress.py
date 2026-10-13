"""Read progress from the pinned BasicScenario route wrapper, without ticking it.

The wrapper waits for a trigger, runs the scenario body, then waits forever so
that a completed child cannot terminate its route. Its own status cannot tell
whether the scenario has started or completed.
"""

_ATOMS = "srunner.scenariomanager.scenarioatomics."
_LIMITATIONS = (
    "Instantaneous observations only; the episode must latch observed True values before tree invalidation.",
    "Behavior completion does not establish route completion, passing scenario criteria, or infraction-free driving.",
    "Unsupported tree layouts have unknown progress; historical transitions cannot be reconstructed.",
)


def _native_type(node, module, name):
    return type(node).__module__ == _ATOMS + module and type(node).__name__ == name


def _route_trigger(node, variable, py_trees):
    def wait_matches(candidate):
        return (_native_type(candidate, "atomic_trigger_conditions", "WaitForBlackboardVariable")
                and getattr(candidate, "_variable_name", None) == variable
                and getattr(candidate, "_variable_value", None) is True)

    if wait_matches(node):
        return True
    # The pinned pedestrian scenarios keep their walkers awake while waiting
    # for the same route trigger. The actual behavior still follows this node.
    if (type(node) is not py_trees.composites.Parallel
            or node.policy != py_trees.common.ParallelPolicy.SUCCESS_ON_ONE):
        return False
    return (len(node.children) >= 2
            and sum(wait_matches(child) for child in node.children) == 1
            and all(wait_matches(child) or _native_type(child, "atomic_behaviors", "MovePedestrianWithEgo")
                    for child in node.children))


def scenario_progress(scenario):
    """Return current observations, or explicit unknowns for an unsupported tree.

    Supports only the pinned route-mode BasicScenario layout with its original
    route-variable reset and WaitForever end sequence. This function neither
    changes the tree nor reads or mutates the blackboard.
    """
    import py_trees

    wrapper = getattr(scenario, "behavior_tree", None)
    wrapper_status = getattr(wrapper, "status", None)
    result = {"schema_version": 1, "extraction": "unsupported",
              "wrapper_status": str(wrapper_status) if wrapper_status is not None else None,
              "actual_behavior_status": None, "trigger_status": None,
              "activation_observed": None, "completion_observed": None,
              "reason": None, "limitations": list(_LIMITATIONS)}

    def unsupported(reason):
        result["reason"] = reason
        return result

    variable = getattr(getattr(scenario, "config", None), "route_var_name", None)
    if not getattr(scenario, "route_mode", False) or not isinstance(variable, str) or not variable:
        return unsupported("Expected route-mode BasicScenario with a nonempty route_var_name")
    if type(wrapper) is not py_trees.composites.Sequence or len(wrapper.children) not in (2, 3):
        return unsupported("Expected Sequence([optional trigger], actual behavior, route end)")
    end = wrapper.children[-1]
    if type(end) is not py_trees.composites.Sequence or len(end.children) != 2:
        return unsupported("Expected the pinned two-node route end sequence")
    reset, forever = end.children
    if (type(reset) is not py_trees.blackboard.SetBlackboardVariable
            or reset.variable_name != variable or reset.variable_value is not False
            or not _native_type(forever, "atomic_behaviors", "WaitForever")):
        return unsupported("Expected route-variable False reset followed by native WaitForever")
    trigger = wrapper.children[0] if len(wrapper.children) == 3 else None
    if trigger is not None and not _route_trigger(trigger, variable, py_trees):
        return unsupported("Unsupported route trigger; expected native route-variable wait or pedestrian wrapper")
    actual = wrapper.children[-2]
    if wrapper.name != actual.name:
        return unsupported("Wrapper name does not match the pinned actual-behavior name")
    if not isinstance(actual.status, py_trees.common.Status):
        return unsupported("Actual behavior has no recognized py_trees status")

    result.update(extraction="pinned_basic_scenario_route_wrapper_v1",
                  actual_behavior_status=str(actual.status),
                  trigger_status=str(trigger.status) if trigger is not None else None,
                  activation_observed=actual.status != py_trees.common.Status.INVALID,
                  completion_observed=actual.status == py_trees.common.Status.SUCCESS)
    return result
