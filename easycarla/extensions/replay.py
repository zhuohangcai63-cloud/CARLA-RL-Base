"""Bounded public-prefix materials and explicit repeatability measurements.

No simulator snapshot is claimed. A replay calls a supplied scene reconstruction
function and executes the recorded commands again, then compares raw states.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

import numpy as np


PREFIX_VERSION = 1
PREFIX_METADATA_RESERVE = 512


class PrefixCapacityError(ValueError, RuntimeError):
    pass


def _plain(value):
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if isinstance(value, np.generic):
        return _plain(value.item())
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("portable records require string dictionary keys")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"unsupported or nonfinite record value: {type(value).__name__}")


def _encoded(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _clone(value):
    return json.loads(_encoded(_plain(value)))


class PrefixRecorder:
    """Record commands and post-command raw state without retaining shared views.

    Capacity errors are explicit and leave the last complete material intact.
    The caller must stop recording/report the error; it must not pretend that an
    already executed command was rolled back. No dataset lifecycle is provided.
    """
    def __init__(self, max_commands=10000, max_bytes=64 * 1024 * 1024):
        if max_commands < 1 or max_bytes < 1:
            raise ValueError("prefix capacities must be positive")
        self.max_commands = int(max_commands)
        self.max_bytes = int(max_bytes)
        self._record = None
        self._bytes = 0
        self._active = False

    def start(self, recipe, initial_state):
        record = {"version": PREFIX_VERSION, "recipe": _plain(recipe),
                  "initial_state": _plain(initial_state), "steps": []}
        size = len(_encoded(record))
        if size + PREFIX_METADATA_RESERVE > self.max_bytes:
            raise PrefixCapacityError("initial prefix state exceeds byte capacity")
        if self._active:
            raise RuntimeError("prefix recording is already active")
        self._record = _clone(record)
        self._bytes = size
        self._active = True

    def append(self, command, state):
        if not self._active:
            raise RuntimeError("prefix recording has not started")
        if len(self._record["steps"]) >= self.max_commands:
            raise PrefixCapacityError("prefix command capacity exceeded")
        step = {"command": _plain(command), "state": _plain(state)}
        # Account for comma and reserve final digest/length metadata.
        size = len(_encoded(step)) + (1 if self._record["steps"] else 0)
        if self._bytes + size + PREFIX_METADATA_RESERVE > self.max_bytes:
            raise PrefixCapacityError("prefix byte capacity exceeded")
        self._record["steps"].append(_clone(step))
        self._bytes += size

    def ensure_capacity(self):
        """Preflight known limits before executing another command.

        This cannot predict a future raw state's encoded size; append still
        enforces the byte limit after execution and must report any overflow.
        """
        if not self._active:
            raise RuntimeError("prefix recording has not started")
        if len(self._record["steps"]) >= self.max_commands:
            raise PrefixCapacityError("prefix command capacity exceeded")
        if self._bytes + PREFIX_METADATA_RESERVE >= self.max_bytes:
            raise PrefixCapacityError("prefix byte capacity exhausted")

    def stop(self):
        """Stop accepting commands while retaining the last complete material."""
        self._active = False

    def preview(self):
        """Return a checksummed copy without ending recording (allocation preflight)."""
        if self._record is None:
            raise RuntimeError("prefix recording has not started")
        result = _clone(self._record)
        result["sha256"] = hashlib.sha256(_encoded(result)).hexdigest()
        return result

    def finish(self):
        result = self.preview()
        self._active = False
        return result

    @property
    def active(self):
        return self._active


def _actors(value):
    if isinstance(value, Mapping):
        return {str(key): val for key, val in value.items()}
    if isinstance(value, list):
        result = {}
        for actor in value:
            identity = actor.get("semantic_id", actor.get("id"))
            if identity is None:
                raise ValueError("actors require semantic_id (stable across reconstruction)")
            identity = str(identity)
            if identity in result:
                raise ValueError("duplicate semantic actor id")
            result[identity] = actor
        return result
    raise ValueError("actors must be a dictionary or list")


def _numeric_difference(reference, current, path, differences, numeric_errors=None):
    """Compare shape, labels and every finite numeric leaf; return maximum error."""
    if isinstance(reference, Mapping):
        if not isinstance(current, Mapping) or set(reference) != set(current):
            differences.append(path + ": keys differ")
            return 0.0
        return max((_numeric_difference(reference[key], current[key], path + "." + str(key), differences, numeric_errors)
                    for key in reference), default=0.0)
    if isinstance(reference, list):
        if not isinstance(current, list) or len(reference) != len(current):
            differences.append(path + ": shape differs")
            return 0.0
        return max((_numeric_difference(a, b, f"{path}[{i}]", differences, numeric_errors)
                    for i, (a, b) in enumerate(zip(reference, current))), default=0.0)
    if isinstance(reference, (float, int)) and not isinstance(reference, bool):
        if isinstance(current, bool) or not isinstance(current, (float, int)):
            differences.append(path + ": type differs")
            return 0.0
        error = abs(float(reference) - float(current))
        if error and numeric_errors is not None:
            numeric_errors.append({"path": path, "error": error})
            numeric_errors.sort(key=lambda item: (-item["error"], item["path"]))
            del numeric_errors[5:]
        return error
    if type(reference) is not type(current) or reference != current:
        differences.append(path + ": value differs")
    return 0.0


TOLERANCE_FIELDS = frozenset(("position", "velocity", "yaw", "actor_state", "signals",
                              "controllers", "events", "history", "task"))


def validate_tolerances(tolerances):
    if tolerances is None:
        return {}
    if not isinstance(tolerances, Mapping):
        raise ValueError("tolerances must be a mapping")
    for key, value in tolerances.items():
        if key not in TOLERANCE_FIELDS:
            raise ValueError(f"unknown tolerance {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"tolerance {key!r} must be finite and nonnegative")
    return dict(tolerances)


def compare_states(reference, current, tolerances=None):
    """Measure comparable starting points without implicit tolerances.

    Required sections are actors, signals, controllers, events, history and task.
    Actor keys are semantic identities, not CARLA's transient numeric actor IDs.
    Position/velocity use Euclidean norms and yaw uses shortest angular distance.
    Nested section tolerances bound maximum absolute numeric-leaf difference;
    labels, array shapes and object sets always require exact equality.

    Missing sections/tolerances yield unverified, even if the present values
    match. Any structural mismatch or exceeded supplied tolerance yields failed.
    """
    reference, current = _plain(reference), _plain(current)
    tolerances = validate_tolerances(tolerances)
    checks = {}
    mismatches, unverified = [], []
    for label, state in (("reference", reference), ("current", current)):
        for limitation in state.get("limitations", []):
            unverified.append(f"{label}: {limitation}")
    if "actors" not in reference or "actors" not in current:
        unverified.append("actors section missing")
    else:
        before, after = _actors(reference["actors"]), _actors(current["actors"])
        missing, unexpected = sorted(set(before) - set(after)), sorted(set(after) - set(before))
        checks["actor_set"] = {"missing": missing, "unexpected": unexpected}
        if missing or unexpected:
            mismatches.append("semantic actor sets differ")
        errors = {"position": 0.0, "velocity": 0.0, "yaw": 0.0}
        counts = {key: 0 for key in errors}
        worst_actors = {key: None for key in errors}
        actor_numeric_errors = []
        actor_state_error = 0.0
        for actor_id in sorted(set(before) & set(after)):
            for field in errors:
                if field not in before[actor_id] or field not in after[actor_id]:
                    unverified.append(f"actors.{actor_id}.{field} missing")
                    continue
                a, b = np.asarray(before[actor_id][field]), np.asarray(after[actor_id][field])
                if a.shape != b.shape or (field == "yaw" and a.ndim != 0):
                    mismatches.append(f"actors.{actor_id}.{field}: shape differs")
                    continue
                delta = a - b
                error = abs(float((delta + math.pi) % (2 * math.pi) - math.pi)) if field == "yaw" else float(np.linalg.norm(delta))
                if error > errors[field]:
                    worst_actors[field] = actor_id
                errors[field] = max(errors[field], error)
                counts[field] += 1
            # Blueprint, physics, dimensions and extra state are also part of
            # comparability; runtime-only actor IDs must not enter this schema.
            other_before = {key: val for key, val in before[actor_id].items() if key not in errors}
            other_after = {key: val for key, val in after[actor_id].items() if key not in errors}
            other_error = _numeric_difference(other_before, other_after, f"actors.{actor_id}", mismatches, actor_numeric_errors)
            actor_state_error = max(actor_state_error, other_error)
            if other_error:
                if "actor_state" not in tolerances:
                    unverified.append("actor_state tolerance missing")
                elif other_error > tolerances["actor_state"]:
                    mismatches.append(f"actors.{actor_id}: other numeric state differs")
        checks["actor_state"] = {"max_error": actor_state_error, "tolerance": tolerances.get("actor_state"),
                                 "largest_differences": actor_numeric_errors}
        for field, error in errors.items():
            checks[field] = {"max_error": error, "objects_compared": counts[field],
                             "tolerance": tolerances.get(field), "worst_actor": worst_actors[field]}
            if field not in tolerances:
                unverified.append(f"{field} tolerance missing")
            elif error > tolerances[field]:
                mismatches.append(f"{field} exceeds tolerance")
    for section in ("signals", "controllers", "events", "history", "task"):
        if section not in reference or section not in current:
            unverified.append(f"{section} section missing")
            continue
        numeric_errors = []
        error = _numeric_difference(reference[section], current[section], section, mismatches, numeric_errors)
        checks[section] = {"max_error": error, "tolerance": tolerances.get(section),
                           "largest_differences": numeric_errors}
        if section not in tolerances:
            unverified.append(f"{section} tolerance missing")
        elif error > tolerances[section]:
            mismatches.append(f"{section} exceeds tolerance")
    status = "failed" if mismatches else ("unverified" if unverified else "passed")
    return {"status": status, "comparable": status == "passed", "checks": checks,
            "mismatches": sorted(set(mismatches)), "unverified": sorted(set(unverified))}


def validate_prefix(record):
    record = _clone(record)
    if not isinstance(record, dict):
        raise ValueError("prefix must be an object")
    if type(record.get("version")) is not int or record["version"] != PREFIX_VERSION:
        raise ValueError("unsupported prefix version")
    digest = record.pop("sha256", None)
    if digest != hashlib.sha256(_encoded(record)).hexdigest():
        raise ValueError("prefix checksum mismatch")
    if not isinstance(record.get("steps"), list) or "recipe" not in record or "initial_state" not in record:
        raise ValueError("malformed prefix")
    if not isinstance(record["recipe"], dict) or not isinstance(record["initial_state"], dict):
        raise ValueError("prefix recipe and initial state must be objects")
    for index, step in enumerate(record["steps"]):
        if (not isinstance(step, dict) or not isinstance(step.get("command"), dict)
                or not isinstance(step.get("state"), dict)):
            raise ValueError(f"malformed prefix step {index}")
    return record


def replay_prefix(record, rebuild, execute, read_state, tolerances=None):
    """Rebuild and execute one prefix using explicit runtime callbacks.

    rebuild(recipe) starts a fresh episode. execute(command) advances via the
    runtime's normal, singly owned clock. read_state() returns raw diagnostic
    state without ticking. Exceptions propagate: replay failures are not valid
    terminal RL transitions. No automatic retry, branching or label calculation.
    """
    material = validate_prefix(record)
    tolerances = validate_tolerances(tolerances)
    result = rebuild(material["recipe"])
    initial = compare_states(material["initial_state"], read_state(), tolerances)
    reports = []
    for index, step in enumerate(material["steps"]):
        if not isinstance(step, Mapping) or "command" not in step or "state" not in step:
            raise ValueError(f"malformed prefix step {index}")
        result = execute(step["command"])
        reports.append(compare_states(step["state"], read_state(), tolerances))
    final = reports[-1] if reports else initial
    all_reports = [initial, *reports]
    status = "failed" if any(item["status"] == "failed" for item in all_reports) else (
        "passed" if all(item["status"] == "passed" for item in all_reports) else "unverified")
    return {"result": result, "status": status, "comparable": status == "passed",
            "initial": initial, "steps": reports, "final": final,
            "commands_executed": len(reports)}
