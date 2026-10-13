"""Explicit route manifests and content overlap auditing (no CARLA import)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import xml.etree.ElementTree as ET


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def route_content(route):
    """Ignore arbitrary IDs/names when identifying repeated physical content."""
    town = route.attrib["town"]
    points = [[float(p.attrib[k]) for k in ("x", "y", "z")]
              for p in route.findall("./waypoints/position")]
    triggers = []
    for scenario in route.findall("./scenarios/scenario"):
        trigger = scenario.find("trigger_point")
        if trigger is None:
            raise ValueError("A Bench2Drive scenario has no trigger_point")
        triggers.append(digest([town, scenario.attrib["type"],
                                {k: float(v) for k, v in trigger.attrib.items()}]))
    return {"route_fingerprint": digest([town, points]),
            "trigger_fingerprints": sorted(set(triggers))}


def load_manifest(path, *, purpose=None):
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text())
    if data.get("version") != 1 or data.get("source") != "bench2drive":
        raise ValueError("Unsupported Bench2Drive manifest")
    if purpose is not None and purpose != data.get("split"):
        raise ValueError(f"Manifest split {data.get('split')!r} does not match purpose {purpose!r}")
    entries = data.get("entries", [])
    if not entries:
        raise ValueError("The route manifest must explicitly list at least one route")
    keys = [(e["file"], str(e["route_id"])) for e in entries]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate routes in manifest")
    return data


def select_entry(manifest, seed, options):
    """A requested route must be in this split; no default eval manifest exists."""
    entries = manifest["entries"]
    requested = options.get("scenario_id", options.get("route_id"))
    if requested is not None:
        matches = [e for e in entries if str(e["route_id"]) == str(requested)]
        if len(matches) != 1:
            raise ValueError(f"Route {requested!r} is absent or ambiguous in configured manifest")
        return matches[0]
    if "scenario_index" in options:
        index = int(options["scenario_index"])
        if not 0 <= index < len(entries):
            raise ValueError("scenario_index outside explicit manifest")
        return entries[index]
    return random.Random(seed).choice(entries)


def verify_entry(entry, dependency_root):
    root = Path(dependency_root).resolve()
    path = (root / entry["file"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Route file must belong to the pinned dependency copy")
    actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_digest != entry["file_sha256"]:
        raise ValueError(f"Route file digest changed: {entry['file']}")
    routes = [r for r in ET.parse(path).getroot().findall("route")
              if r.attrib["id"] == str(entry["route_id"])]
    if len(routes) != 1 or routes[0].attrib["town"] != entry["town"]:
        raise ValueError("Manifest route identity does not match XML")
    if route_content(routes[0]) != {k: entry[k] for k in ("route_fingerprint", "trigger_fingerprints")}:
        raise ValueError("Manifest physical content does not match XML")
    return path


def audit_overlap(left, right):
    def values(manifest, key):
        if key == "route_fingerprint":
            return {e[key] for e in manifest["entries"]}
        return {x for e in manifest["entries"] for x in e[key]}
    routes = values(left, "route_fingerprint") & values(right, "route_fingerprint")
    triggers = values(left, "trigger_fingerprints") & values(right, "trigger_fingerprints")
    return {"left": left["name"], "right": right["name"],
            "exact_route_overlap": len(routes), "exact_trigger_overlap": len(triggers),
            "route_fingerprints": sorted(routes), "trigger_fingerprints": sorted(triggers),
            "method": "exact town/geometry and town/type/trigger; not a geographic-disjointness proof"}


def assert_evaluation_isolation(training, evaluation):
    report = audit_overlap(training, evaluation)
    if report["exact_route_overlap"] or report["exact_trigger_overlap"]:
        raise ValueError(f"Training/evaluation content overlap: {report['exact_route_overlap']} routes, "
                         f"{report['exact_trigger_overlap']} scenario triggers")
    return report
