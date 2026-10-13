"""Reporting tests ensure zero activity and missing evidence cannot look passed."""
import json
import runpy

api = runpy.run_path("scripts/summarize_scenic_coverage.py")
evidence = api["episode_evidence"]
summarize = api["summarize"]


def test_zero_activity_is_not_an_implicit_skip():
    for family in api["FAMILIES"]:
        result = evidence(family, {"events": {}})
        assert not result["generation_accounted"]
        assert not result["all_triggered_or_skipped"]
        assert not result["completion_observed"]


def test_partial_global_traffic_trigger_is_incomplete():
    result = evidence("parking_cut_in", {"events": {"long_tail_event_spawned": 5,
        "long_tail_events_triggered": 2, "long_tail_events_completed": 1}})
    assert result["untriggered"] == 3
    assert not result["all_triggered_or_skipped"]
    assert result["completion_observed"]


def test_skip_needs_count_and_reason():
    values = {"long_tail_event_skipped": 1}
    assert not evidence("vehicle_opens_door", {"events": values})["explicit_skip"]
    values["long_tail_skip_reason"] = "no_side_lane"
    result = evidence("vehicle_opens_door", {"events": values})
    assert result["explicit_skip"] and result["all_triggered_or_skipped"]
    assert not result["completion_observed"]


def test_crossing_trigger_batches_are_not_confused_with_walker_instances():
    result = evidence("pedestrian_crossing", {"events": {
        "forced_crossing_spawned": 3, "forced_crossing_events": 1, "forced_crossing_completed": 2}})
    assert result["triggered"] == 3 and result["all_triggered_or_skipped"]


def test_execution_only_and_missing_family_cannot_pass(tmp_path):
    (tmp_path / "carla-hard_break_route.json").write_text(json.dumps({
        "backend": "carla", "execution_passed": True,
        "episodes": [{"events": {"hard_break_lead_spawned": True, "lead_brake_events": 1}}]}))
    result = summarize(tmp_path)
    assert not result["passed"]
    assert result["scenarios"]["hard_break_route"]["status"] == "incomplete"
    assert result["scenarios"]["parking_cut_in"]["status"] == "not_run"


def test_supplemental_completion_is_recorded_without_hiding_failed_probes(tmp_path):
    base = {"backend": "carla", "execution_passed": True, "episodes": [{"events": {
        "long_tail_event_spawned": 1, "long_tail_events_triggered": 1}}]}
    (tmp_path / "carla-construction_obstacle.json").write_text(json.dumps(base))
    failure = {**base, "completion_passed": False}
    (tmp_path / "carla-completion-construction_obstacle.json").write_text(json.dumps(failure))
    sub = tmp_path / "bbox_probe"
    sub.mkdir()
    success = {"backend": "carla", "execution_passed": True, "completion_passed": True,
               "episodes": [{"events": {"long_tail_event_spawned": 1, "long_tail_events_triggered": 1,
                                         "long_tail_events_completed": 1}}]}
    (sub / "carla-completion-construction_obstacle.json").write_text(json.dumps(success))
    result = summarize(tmp_path)["scenarios"]["construction_obstacle"]
    assert result["status"] == "passed"
    assert len(result["probe_reports"]) == 2
    assert sum(probe["completion_passed"] for probe in result["probe_reports"]) == 1
