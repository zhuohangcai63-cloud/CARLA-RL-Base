#!/usr/bin/env python3
"""Make a reproducible, isolated Bench2Drive dependency copy and route lists.

Only tracked files at PINNED_COMMIT are read. The source checkout is never edited.
No package installation or CARLA connection occurs here.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

PINNED_COMMIT = "acbeb0dd8aa99dd0e71b9b75bb542f6fe7c7f3d8"
PATCH_VERSION = 6
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from easycarla.scenarios.manifests import audit_overlap, route_content  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_controls(source):
    """Rewrite only method-call expressions, preserving surrounding upstream text."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode()))
    raw = source.encode()
    methods = {"apply_control", "set_autopilot", "set_target_velocity", "set_target_angular_velocity",
               "enable_constant_velocity", "disable_constant_velocity", "get_world", "destroy", "set_simulate_physics"}
    edits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in methods:
            continue
        start = offsets[node.func.lineno - 1] + node.func.col_offset
        end = offsets[node.func.end_lineno - 1] + node.func.end_col_offset
        receiver = ast.get_source_segment(source, node.func.value)
        # Retain the original call's arguments by replacing the receiver.method(
        # prefix. The opening parenthesis immediately follows any whitespace.
        paren = raw.find(b"(", end)
        edits.append((start, paren + 1,
                      f"_easycarla_native_call({receiver}, {node.func.attr!r}, ".encode()))
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    if not edits:
        return source, 0
    source = raw.decode()
    tree = ast.parse(source)
    anchor = 0
    for node in tree.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)) or (
                    isinstance(node, ast.ImportFrom) and node.module == "__future__"):
            anchor = node.end_lineno
        else:
            break
    lines = source.splitlines(keepends=True)
    lines.insert(anchor, "\nfrom easycarla.scenarios.bench2drive.native_control import native_actor_call as _easycarla_native_call\n")
    result = "".join(lines)
    ast.parse(result)
    return result, len(edits)


def patch_background_ownership(source):
    """Make two pinned background-traffic handoffs explicit before actuation."""
    zero = "_easycarla_native_call(actor, 'set_target_velocity', carla.Vector3D(0, 0, 0))"
    disable = "_easycarla_native_call(actor, 'set_autopilot', False, self._tm_port)"
    old_tail = "actor.set_light_state(carla.VehicleLightState(lights))\n                        " + disable
    impulse = "_easycarla_native_call(actor, 'set_target_velocity', carla.Vector3D())"
    if source.count(zero) != 1 or source.count(old_tail) != 1 or source.count(impulse) != 1:
        raise ValueError("Pinned background handoff patch no longer matches upstream")
    source = source.replace(old_tail, "actor.set_light_state(carla.VehicleLightState(lights))")
    source = source.replace(zero, disable + "\n                        " + zero)
    return source.replace(impulse, "_easycarla_native_call(actor, 'apply_native_velocity_impulse', carla.Vector3D())")


def patch_transform_reset(source):
    """Name only ActorTransformSetter's pinned pre-teleport velocity reset."""
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ActorTransformSetter"]
    if len(classes) != 1:
        raise ValueError("Pinned ActorTransformSetter patch no longer matches upstream")
    node = classes[0]
    lines = source.splitlines(keepends=True)
    block = "".join(lines[node.lineno-1:node.end_lineno])
    old = "_easycarla_native_call(self._actor, 'set_target_velocity', carla.Vector3D(0, 0, 0))"
    if block.count(old) != 1:
        raise ValueError("Pinned transform velocity reset no longer matches upstream")
    block = block.replace(old, old.replace("'set_target_velocity'", "'apply_native_transform_reset'"))
    return "".join(lines[:node.lineno-1]) + block + "".join(lines[node.end_lineno:])


def patch_spawn_physics(source):
    old = "PhysicsCommand(FutureActor, False)"
    old_import = "from easycarla.scenarios.bench2drive.native_control import native_actor_call as _easycarla_native_call"
    if source.count(old) != 1 or source.count(old_import) != 1:
        raise ValueError("Pinned spawn physics patch no longer matches upstream")
    source = source.replace(old, "_easycarla_spawn_physics(command, " + old + ", False)")
    return source.replace(old_import, old_import + ", mark_spawn_physics as _easycarla_spawn_physics")


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).expanduser().resolve()
    if source == destination or source in destination.parents:
        raise ValueError("Dependency copy must be outside the user's source checkout")
    if destination.exists():
        lock = json.loads((destination / "easycarla-lock.json").read_text())
        if lock["commit"] != PINNED_COMMIT or lock["patch_version"] != PATCH_VERSION:
            raise ValueError("Existing dependency copy has a different version; choose a new destination")
        for name, digest in lock["files"].items():
            if sha(destination / name) != digest:
                raise ValueError(f"Existing dependency file modified: {name}")
        return destination
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", PINNED_COMMIT], text=True).strip()
    if commit != PINNED_COMMIT:
        raise ValueError("Pinned commit not available")
    archive = subprocess.check_output(["git", "-C", str(source), "archive", commit,
                                       "leaderboard", "scenario_runner"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".prepare-bench2drive-", dir=destination.parent))
    patches = []
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for member in bundle.getmembers():
                path = (temporary / member.name).resolve()
                if not path.is_relative_to(temporary) or member.issym() or member.islnk():
                    raise ValueError(f"Unsafe archive entry {member.name}")
            bundle.extractall(temporary)
        for path in sorted(temporary.rglob("*.py")):
            original = path.read_text()
            patched = original.replace("scenario.getchildren()", "list(scenario)")
            patched = patched.replace(
                "from agents.navigation.constant_velocity_agent import ConstantVelocityAgent",
                "from easycarla.scenarios.bench2drive.agents import ConstantVelocityAgent")
            if path.name in ("carla_data_provider.py", "route_manipulation.py"):
                patched = patched.replace("from agents.navigation.global_route_planner import GlobalRoutePlanner",
                                          "from easycarla.scenarios.bench2drive.planner import GlobalRoutePlanner")
            patched, control_calls = patch_controls(patched)
            if path.name == "background_activity.py":
                patched = patch_background_ownership(patched)
            if path.name == "atomic_behaviors.py":
                patched = patch_transform_reset(patched)
            if path.name == "carla_data_provider.py":
                patched = patch_spawn_physics(patched)
            if path.name == "route_scenario.py":
                # Never continue a route after silently dropping its scene.
                patched = patched.replace(
                    "            except Exception as e:\n",
                    "            except Exception as e:\n                raise RuntimeError(f\"Scenario construction failed: {scenario_config.name}\") from e\n")
                # The upstream loop mutates lists during iteration, skipping entries.
                patched = patched.replace("for scenario_config in self.missing_scenario_configurations:",
                                          "for scenario_config in list(self.missing_scenario_configurations):")
                patched = patched.replace("for slot in available_parking_locations:",
                                          "for slot in list(available_parking_locations):")
                patched = patched.replace("for slot in self.available_parking_locations:",
                                          "for slot in list(self.available_parking_locations):")
                patched = patched.replace('available_parking_locations = getattr(parked_vehicles, map_name, [])',
                                          'available_parking_locations = list(getattr(parked_vehicles, map_name, []))')
            if patched != original:
                ast.parse(patched)
                path.write_text(patched)
                patches.append({"file": str(path.relative_to(temporary)),
                                "upstream_sha256": hashlib.sha256(original.encode()).hexdigest(),
                                "patched_sha256": sha(path), "control_calls": control_calls})
        files = {str(p.relative_to(temporary)): sha(p) for p in sorted(temporary.rglob("*")) if p.is_file()}
        lock = {"commit": PINNED_COMMIT, "patch_version": PATCH_VERSION,
                "upstream": "https://github.com/Thinklab-SJTU/Bench2Drive", "patches": patches, "files": files}
        (temporary / "easycarla-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination


def create_manifests(root, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    def manifest(name, filename, split, start=None, stop=None):
        relative = f"leaderboard/data/{filename}"
        path = root / relative
        entries = []
        routes = ET.parse(path).getroot().findall("route")[start:stop]
        for route in routes:
            entries.append({"file": relative, "file_sha256": sha(path), "route_id": route.attrib["id"],
                            "town": route.attrib["town"], "scenario_types": sorted(
                                {s.attrib["type"] for s in route.findall("./scenarios/scenario")}),
                            **route_content(route)})
        return {"version": 1, "source": "bench2drive", "name": name, "split": split,
                "upstream_commit": PINNED_COMMIT, "entries": entries}
    training = manifest("town12_long_train78", "routes_training.xml", "train")
    development = manifest("town12_long_dev12", "routes_training.xml", "dev")
    # The 90 IDs represent 30 geometries repeated three times. Keep all variants
    # of a geometry together; an 80/10 ID split would leak ten entire routes.
    training["entries"] = [e for e in training["entries"] if int(e["route_id"]) % 30 < 26]
    development["entries"] = [e for e in development["entries"] if int(e["route_id"]) % 30 >= 26]
    manifests = [training, development,
                 manifest("v004_eval220", "bench2drive_0.0.4_val.xml", "eval"),
                 manifest("legacy_eval220", "bench2drive220.xml", "eval"),
                 manifest("legacy_dev10_reserved", "drivetransformer_bench2drive_dev10.xml", "eval")]
    manifests[1]["restriction"] = "development holdout by exact geometry and trigger; not final benchmark evaluation"
    for item in manifests:
        (output / (item["name"] + ".json")).write_text(json.dumps(item, indent=2) + "\n")
    reports = [audit_overlap(a, b) for i, a in enumerate(manifests) for b in manifests[i + 1:]]
    (output / "overlap-audit.json").write_text(json.dumps(reports, indent=2) + "\n")
    return manifests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="existing Bench2Drive git checkout (read-only)")
    parser.add_argument("--destination", default=str(Path.home() / ".cache/easycarla/bench2drive" / (PINNED_COMMIT + f"-p{PATCH_VERSION}")))
    parser.add_argument("--manifests", default=str(REPO / "configs/manifests"))
    args = parser.parse_args()
    root = prepare(args.source, args.destination)
    manifests = create_manifests(root, args.manifests)
    print(json.dumps({"dependency_root": str(root), "commit": PINNED_COMMIT,
                      "manifests": {m["name"]: len(m["entries"]) for m in manifests}}, indent=2))


if __name__ == "__main__":
    main()
