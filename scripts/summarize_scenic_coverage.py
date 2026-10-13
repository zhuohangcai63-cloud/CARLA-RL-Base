#!/usr/bin/env python3
"""Audit real Scenic event evidence; successful execution alone is insufficient."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

FAMILIES = ("pedestrian_crossing", "hard_break_route", "parking_cut_in",
            "vehicle_opens_door", "construction_obstacle")


def episode_evidence(scenario, episode):
    values = episode.get("events", {})
    if scenario == "pedestrian_crossing":
        generated = int(values.get("forced_crossing_spawned", 0))
        # Each spawned crossing walker starts scripted motion immediately; the
        # events counter counts trigger batches, not individual walkers.
        triggered = generated if int(values.get("forced_crossing_events", 0)) > 0 else 0
        completed = int(values.get("forced_crossing_completed", 0))
        failed = int(values.get("forced_crossing_failed", 0)) + int(values.get("forced_crossing_timeout", 0))
        skipped, reason = 0, ""
    elif scenario == "hard_break_route":
        generated = int(bool(values.get("hard_break_lead_spawned", False)))
        triggered = int(values.get("lead_brake_events", 0))
        completed = int(values.get("lead_brake_completed", 0))
        failed = int(values.get("lead_brake_aborted", 0))
        reason = str(values.get("hard_break_lead_spawn_failure", ""))
        # A failed dedicated lead spawn is recorded, but is not an accepted
        # geometric skip unless the test explicitly records skip authorization.
        skipped = 0
    else:
        generated = int(values.get("long_tail_event_spawned", 0))
        triggered = int(values.get("long_tail_events_triggered", 0))
        completed = int(values.get("long_tail_events_completed", 0))
        failed = int(values.get("long_tail_events_failed", 0))
        skipped = int(values.get("long_tail_event_skipped", 0))
        reason = str(values.get("long_tail_skip_reason", ""))
    explicit_skip = skipped > 0 and bool(reason)
    generated_or_skipped = generated > 0 or explicit_skip
    all_triggered_or_skipped = generated_or_skipped and triggered >= generated
    return {"seed": episode.get("seed"), "generated": generated, "triggered": triggered,
            "completed": completed, "failed_or_aborted": failed, "skipped": skipped,
            "skip_reason": reason, "explicit_skip": explicit_skip,
            "untriggered": max(0, generated - triggered),
            "generation_accounted": generated_or_skipped,
            "all_triggered_or_skipped": all_triggered_or_skipped,
            "completion_observed": completed > 0,
            "episode_termination_reason": episode.get("reason", ""),
            "steps": episode.get("steps")}


def summarize(directory):
    directory = Path(directory)
    scenarios = {}
    for scenario in FAMILIES:
        path = directory / f"carla-{scenario}.json"
        if not path.is_file():
            scenarios[scenario] = {"status": "not_run", "report": str(path)}
            continue
        data = json.loads(path.read_text())
        if data.get("backend") != "carla":
            raise ValueError(f"{path} is not a real CARLA report")
        episodes = [episode_evidence(scenario, item) for item in data.get("episodes", [])]
        supplemental = []
        probe_reports = []
        for supplemental_path in sorted(directory.rglob(f"carla-completion-{scenario}.json")):
            extra = json.loads(supplemental_path.read_text())
            if extra.get("backend") != "carla":
                raise ValueError(f"{supplemental_path} is not a real CARLA report")
            probe_reports.append({"report": str(supplemental_path), "driver": extra.get("driver"),
                                  "execution_passed": bool(extra.get("execution_passed")),
                                  "completion_passed": bool(extra.get("completion_passed")),
                                  "error": extra.get("error")})
            supplemental.extend({**episode_evidence(scenario, item),
                                 "report": str(supplemental_path), "driver": extra.get("driver"),
                                 "execution_passed": bool(extra.get("execution_passed"))}
                                for item in extra.get("episodes", []))
        execution = bool(data.get("execution_passed"))
        generated = bool(episodes) and all(row["generation_accounted"] for row in episodes)
        triggered = bool(episodes) and all(row["all_triggered_or_skipped"] for row in episodes)
        completed = (any(row["completion_observed"] for row in episodes)
                     or any(row["execution_passed"] and row["completion_observed"] for row in supplemental))
        all_skipped = bool(episodes) and all(row["generated"] == 0 and row["explicit_skip"] for row in episodes)
        passed = execution and generated and triggered and (completed or all_skipped)
        scenarios[scenario] = {"status": "passed" if passed else "incomplete",
            "report": str(path), "execution_passed": execution,
            "generation_accounted": generated, "all_triggered_or_skipped": triggered,
            "completion_observed": completed, "all_explicitly_skipped": all_skipped,
            "episodes": episodes, "completion_probes": supplemental, "probe_reports": probe_reports, "error": data.get("error")}
    return {"schema_version": 1, "backend": "carla",
            "passed": all(row["status"] == "passed" for row in scenarios.values()),
            "acceptance": "Every episode must generate or explicitly skip; every generated instance must trigger; each family must demonstrate completion or be explicitly skipped throughout. Execution success is separate.",
            "scenarios": scenarios}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-complete", action="store_true", help="exit nonzero when any family lacks required evidence")
    args = parser.parse_args()
    result = summarize(args.directory)
    output = args.output or args.directory / "scenic-coverage.json"
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"report": str(output), "passed": result["passed"],
                      "scenarios": {key: value["status"] for key, value in result["scenarios"].items()}}, ensure_ascii=False))
    return 1 if args.require_complete and not result["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
