#!/usr/bin/env python3
"""Pure CARLA API probe: reloads a dedicated local world, never starts a server.

This intentionally does not import easycarla, Scenic or Bench2Drive. A route
manifest supplies a spawn location only; this is not scenario execution testing.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import time
import traceback
import xml.etree.ElementTree as ET

import numpy as np


SETTING_FIELDS = ("synchronous_mode", "fixed_delta_seconds", "no_rendering_mode",
                  "spectator_as_ego", "tile_stream_distance", "actor_active_distance")


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def coordinates(value):
    try:
        values = [float(item) for item in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError("expected x,y,z[,yaw_degrees]") from None
    if len(values) not in (3, 4) or not all(math.isfinite(v) for v in values):
        raise argparse.ArgumentTypeError("expected finite x,y,z[,yaw_degrees]")
    return values if len(values) == 4 else [*values, 0.0]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "WARNING: reloads maps, destroying existing world actors. Use a dedicated "
        "local CARLA server with no other clients. Never runs a benchmark or retries a failed tick."))
    parser.add_argument("--port", type=positive_int, required=True)
    parser.add_argument("--host", choices=("127.0.0.1", "localhost"), default="127.0.0.1")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--town", action="append", nargs="+", help="exact installed map name(s); repeatable")
    parser.add_argument("--repeats", type=positive_int, default=3)
    parser.add_argument("--ticks", type=positive_int, default=100)
    parser.add_argument("--timeout", type=positive, default=75.0)
    parser.add_argument("--idle-seconds", type=positive, default=.2)
    parser.add_argument("--distance", type=positive, default=500.0,
                        help="tile and actor active distances in metres; alters distant traffic semantics")
    parser.add_argument("--spectator-as-ego", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--no-rendering", action="store_true", help="disable world rendering; incompatible with --camera")
    parser.add_argument("--camera", action="store_true", help="validate 320x180 RGB, exact frame and nonuniform pixels")
    parser.add_argument("--spawn", type=coordinates, help="exact CARLA world x,y,z[,yaw_degrees], used for every town")
    parser.add_argument("--route-manifest", type=Path)
    parser.add_argument("--route-id")
    parser.add_argument("--route-root", type=Path, help="root containing the manifest entry's XML file")
    args = parser.parse_args(argv)
    args.town = [town for group in (args.town or []) for town in group]
    if args.port > 65535:
        parser.error("--port must not exceed 65535")
    if args.camera and args.no_rendering:
        parser.error("--camera requires rendering (omit --no-rendering)")
    route_options = (args.route_manifest, args.route_id, args.route_root)
    if any(v is not None for v in route_options) and not all(v is not None for v in route_options):
        parser.error("--route-manifest, --route-id and --route-root must be supplied together")
    if args.spawn and args.route_manifest:
        parser.error("--spawn and route selection are mutually exclusive")
    if not args.town and not args.route_manifest:
        parser.error("supply --town or a route manifest selection")
    return args


def route_start(args):
    """Verify the selected input; load no upstream Python dependencies."""
    raw = args.route_manifest.read_bytes()
    manifest = json.loads(raw)
    entries = [entry for entry in manifest["entries"] if str(entry["route_id"]) == args.route_id]
    if len(entries) != 1:
        raise ValueError("route ID must identify exactly one manifest entry")
    entry = entries[0]
    root = args.route_root.expanduser().resolve()
    source = (root / entry["file"]).resolve()
    if not source.is_relative_to(root):
        raise ValueError("route XML must be inside --route-root")
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != entry["file_sha256"]:
        raise ValueError("route XML digest differs from the manifest")
    routes = [route for route in ET.fromstring(content).findall("route") if route.get("id") == args.route_id]
    if len(routes) != 1 or routes[0].get("town") != entry["town"]:
        raise ValueError("route XML and manifest disagree")
    positions = routes[0].findall("waypoints/position")
    if len(positions) < 2:
        raise ValueError("route requires at least two positions to define its starting heading")
    first, second = [[float(point.attrib[key]) for key in ("x", "y", "z")] for point in positions[:2]]
    if not all(math.isfinite(v) for v in first + second):
        raise ValueError("route positions contain non-finite data")
    yaw = math.degrees(math.atan2(second[1] - first[1], second[0] - first[0]))
    spawn = [first[0], first[1], first[2] + .5, yaw]
    if not all(math.isfinite(v) for v in spawn):
        raise ValueError("route spawn contains non-finite data")
    if args.town and any(town.rsplit("/", 1)[-1] != entry["town"] for town in args.town):
        raise ValueError("selected route belongs to a different town")
    return spawn, {"route_id": args.route_id, "town": entry["town"], "file": str(source),
                   "file_sha256": digest, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                   "scope": "first route position + 0.5 m height, first segment heading; no scenario execution"}


@contextmanager
def world_lease(port):
    # Deliberately identical to WorldSession, including across host aliases and Conda environments.
    directory = Path(f"/tmp/easycarla-{os.getuid()}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / f"world-port-{port}.lock").open("a+") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("This CARLA port already has a world owner") from None
        yield
        # Do not unlink the stable lock file. Closing releases this lease.


def settings_dict(world):
    settings = world.get_settings()
    return {key: getattr(settings, key) for key in SETTING_FIELDS}


def frame_state(world):
    snapshot = world.get_snapshot()
    return {"episode_id": world.id, "frame": snapshot.frame,
            "elapsed_seconds": snapshot.timestamp.elapsed_seconds}


def frame_change(before, after):
    return {"before": before, "after": after,
            "same_episode_frame_delta": after["frame"] - before["frame"]
            if before["episode_id"] == after["episode_id"] else None}


def camera_stats(image, frame):
    if image.frame != frame or (image.width, image.height) != (320, 180):
        raise RuntimeError("Camera frame or dimensions differ from the requested observation")
    pixels = np.frombuffer(image.raw_data, dtype=np.uint8)
    if pixels.size != 320 * 180 * 4:
        raise RuntimeError("Camera byte count does not match BGRA dimensions")
    rgb = pixels.reshape(180, 320, 4)[:, :, :3]
    deviation = float(rgb.std())
    spatial_deviation = float(rgb.std(axis=(0, 1)).max())
    if spatial_deviation < 1.0:
        raise RuntimeError(f"Camera produced a uniform/empty image (spatial_std={spatial_deviation})")
    return {"frame": frame, "width": image.width, "height": image.height,
            "std": deviation, "spatial_std": spatial_deviation, "min": int(rgb.min()), "max": int(rgb.max())}


def wait_camera(images, frame, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            image = images.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            raise TimeoutError(f"Camera did not deliver physical frame {frame}") from None
        if image.frame > frame:
            raise RuntimeError(f"Camera skipped required frame {frame}; received {image.frame}")
        if image.frame == frame:
            return camera_stats(image, frame)


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def wait_server(carla, host, port, timeout, record):
    """Retry reads with fresh short-lived connections; never advance a world."""
    started = time.monotonic()
    deadline = started + timeout
    record.update(attempts=0, seconds=0.0, last_error=None)
    while time.monotonic() < deadline:
        record["attempts"] += 1
        try:
            client = carla.Client(host, port)
            client.set_timeout(min(1.0, max(.01, deadline - time.monotonic())))
            version = client.get_server_version()
            client.set_timeout(min(1.0, max(.01, deadline - time.monotonic())))
            world = client.get_world()
            record.update(seconds=time.monotonic() - started, server_version=version)
            return client, world
        except RuntimeError as exc:
            record["last_error"] = str(exc)
            record["seconds"] = time.monotonic() - started
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"CARLA readiness failed after {record['attempts']} read-only attempts: {record['last_error']}")


def run(args, carla):
    report = {"version": 1, "started_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "native API map/hero/camera smoke test; no driving or scenario coverage",
              "status": "running", "config": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
              "episodes": [], "cleanup": []}
    world = original = None
    owned = []
    client = None
    cleaning_up = False

    def persist():
        try:
            save(args.output, report)
        except (OSError, ValueError, TypeError) as exc:
            if not cleaning_up:
                raise
            # Failed report encoding or output sinks must not skip actor
            # destruction or settings restoration. The exit status still fails.
            report["status"] = "failed"
            report.setdefault("report_write_errors", []).append(str(exc))

    def stage(name, record=report):
        record["stage"] = name
        persist()
        if name not in ("control_and_tick", "camera_wait"):
            try:
                print(name, flush=True)
            except OSError as exc:
                report["status"] = "failed"
                report["console_error"] = str(exc)
                if not cleaning_up:
                    raise

    def api_call(name, function, record):
        stage(name, record)
        before = frame_state(world)
        started = time.perf_counter()
        result = function()
        record.setdefault("api_phases", []).append({"operation": name,
            "seconds": time.perf_counter() - started, **frame_change(before, frame_state(world))})
        persist()
        return result

    def destroy_owned():
        errors = []
        remaining = []
        for actor in reversed(owned):
            if actor.type_id.startswith("sensor."):
                try:
                    actor.stop()
                except Exception as exc:
                    errors.append(f"{actor.id} stop: {exc}")
            try:
                if actor.destroy() is False:
                    raise RuntimeError("actor destruction was refused")
            except Exception as exc:
                errors.append(f"{actor.id}: {exc}")
                remaining.append(actor)
        owned[:] = remaining
        if errors:
            raise RuntimeError("Owned actor cleanup failed: " + "; ".join(errors))

    def spawn_owned(blueprint, transform, **kwargs):
        actor = world.spawn_actor(blueprint, transform, **kwargs)
        owned.append(actor)
        return actor

    try:
        spawn = args.spawn
        if args.route_manifest:
            spawn, report["route"] = route_start(args)
            args.town = args.town or [report["route"]["town"]]
        stage("connecting")
        report["readiness"] = {}
        client, world = wait_server(carla, args.host, args.port, args.timeout, report["readiness"])
        client.set_timeout(args.timeout)
        report["server_version"] = report["readiness"]["server_version"]
        report["client_version"] = client.get_client_version()
        original = world.get_settings()
        report["original_map"] = world.get_map().name
        report["original_settings"] = settings_dict(world)
        requested = world.get_settings()
        requested.synchronous_mode, requested.fixed_delta_seconds = True, .05
        requested.no_rendering_mode, requested.spectator_as_ego = args.no_rendering, args.spectator_as_ego
        requested.tile_stream_distance = requested.actor_active_distance = args.distance
        available = client.get_available_maps()
        for repeat in range(args.repeats):
            for town in args.town:
                matches = [name for name in available if name == town or name.rsplit("/", 1)[-1] == town]
                if len(matches) != 1:
                    raise ValueError(f"Town {town!r} resolves to {len(matches)} assets")
                record = {"town": town, "asset": matches[0], "repeat": repeat, "status": "running", "ticks": 0}
                report["episodes"].append(record)
                api_call("pre_load_settings", lambda: world.apply_settings(requested), record)
                stage("load_world", record)
                before, started = frame_state(world), time.perf_counter()
                world = client.load_world(matches[0], reset_settings=False, map_layers=carla.MapLayer.All)
                record["load_seconds"] = time.perf_counter() - started
                record["load_frame_change"] = frame_change(before, frame_state(world))
                record["immediate_loaded_settings"] = settings_dict(world)
                stage("map_metadata", record)
                deadline = time.monotonic() + args.timeout
                while world.get_map().name.rsplit("/", 1)[-1] != town.rsplit("/", 1)[-1]:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Loaded map metadata did not match requested asset")
                    time.sleep(.05)
                    world = client.get_world()
                api_call("post_load_settings", lambda: world.apply_settings(requested), record)
                record["settings"] = settings_dict(world)
                if any(record["settings"][key] != getattr(requested, key) for key in SETTING_FIELDS):
                    raise RuntimeError("World settings read-back does not match requested values")
                current_map = world.get_map()
                record["opendrive_sha256"] = hashlib.sha256(current_map.to_opendrive().encode()).hexdigest()
                if spawn:
                    transform = carla.Transform(carla.Location(*spawn[:3]), carla.Rotation(yaw=spawn[3]))
                else:
                    points = current_map.get_spawn_points()
                    if not points:
                        raise RuntimeError("Map has no recommended spawn points; specify --spawn")
                    transform = points[0]
                record["spawn"] = {"x": transform.location.x, "y": transform.location.y,
                                   "z": transform.location.z, "yaw": transform.rotation.yaw}
                blueprint = world.get_blueprint_library().find("vehicle.lincoln.mkz_2020")
                blueprint.set_attribute("role_name", "hero")
                ego = api_call("hero_spawn", lambda: spawn_owned(blueprint, transform), record)
                api_call("spectator_position", lambda: world.get_spectator().set_transform(carla.Transform(
                    transform.location + carla.Location(z=30), carla.Rotation(pitch=-90))), record)
                images, callback_errors = queue.Queue(maxsize=32), []
                if args.camera:
                    blueprint = world.get_blueprint_library().find("sensor.camera.rgb")
                    # Zero asks UE to tick the sensor every physical frame.
                    # A positive interval uses its separate timer, which need
                    # not deliver every frame even when nominally equal to dt.
                    for key, value in {"image_size_x": "320", "image_size_y": "180", "sensor_tick": "0.0"}.items():
                        blueprint.set_attribute(key, value)
                    record["camera_config"] = {"sensor_tick": 0.0, "image_size_x": 320, "image_size_y": 180,
                                               "capture_schedule": "every_physical_tick"}
                    camera = api_call("camera_spawn", lambda: spawn_owned(blueprint,
                        carla.Transform(carla.Location(x=1.5, z=2.4)), attach_to=ego), record)
                    def receive(image, target=images, errors=callback_errors):
                        try:
                            target.put_nowait(image)
                        except queue.Full:
                            errors.append("Camera callback queue overflow")
                    camera.listen(receive)
                tick_times, camera_times = [], []
                record["camera_frames"] = []
                before_idle = frame_state(world)
                time.sleep(args.idle_seconds)
                if frame_state(world) != before_idle:
                    raise RuntimeError("World advanced during idle; another client may be ticking")
                record["idle_verified"] = True
                for _ in range(args.ticks):
                    stage("control_and_tick", record)
                    before = frame_state(world)
                    replies = client.apply_batch_sync([carla.command.ApplyVehicleControl(
                        ego.id, carla.VehicleControl(brake=1))], False)
                    if len(replies) != 1 or replies[0].error:
                        raise RuntimeError("Brake control command was not acknowledged")
                    started = time.perf_counter()
                    frame = world.tick(args.timeout)
                    tick_times.append(time.perf_counter() - started)
                    after = frame_state(world)
                    if after["episode_id"] != before["episode_id"] or frame != before["frame"] + 1 or after["frame"] != frame:
                        raise RuntimeError("Unexpected episode change or noncontiguous physical frame")
                    if abs(after["elapsed_seconds"] - before["elapsed_seconds"] - .05) > 1e-5:
                        raise RuntimeError("Physical time increment differs from the requested 0.05 seconds")
                    if not ego.is_alive:
                        raise RuntimeError("Hero disappeared during the probe")
                    record["ticks"] += 1
                    record["last_frame"] = frame
                    if args.camera:
                        stage("camera_wait", record)
                        started = time.perf_counter()
                        record["camera_frames"].append(wait_camera(images, frame, args.timeout))
                        camera_times.append(time.perf_counter() - started)
                        if callback_errors:
                            raise RuntimeError(callback_errors[0])
                record["timing"] = {name: {"p50": float(np.percentile(values, 50)),
                                           "p95": float(np.percentile(values, 95)), "max": max(values)}
                                    for name, values in (("tick_seconds", tick_times), ("camera_wait_seconds", camera_times)) if values}
                stage("destroy_owned", record)
                destroy_owned()
                record["status"] = "passed"
                stage("passed", record)
        report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        if report["episodes"] and report["episodes"][-1]["status"] == "running":
            report["episodes"][-1]["status"] = "failed"
            report["episodes"][-1]["outcome"] = "last submitted operation may have executed; never replayed"
    finally:
        cleaning_up = True
        persist()
        if world is not None and original is not None:
            try:
                stage("cleanup_liveness")
                client.set_timeout(min(5.0, args.timeout))
                client.get_server_version()
                if client.get_world().id != world.id:
                    raise RuntimeError("World changed externally; refusing to destroy actors or restore settings")
                try:
                    destroy_owned()
                except Exception as exc:
                    # A failed actor RPC must not leave an otherwise reachable
                    # server in synchronous mode, hanging its later shutdown.
                    report["cleanup"].append({"status": "failed", "stage": "destroy_owned", "error": str(exc)})
                    report["status"] = "failed"
                stage("restore_settings")
                client.set_timeout(args.timeout)
                api_call("restore_settings", lambda: world.apply_settings(original), report)
                restored = settings_dict(world)
                if any(restored[key] != getattr(original, key) for key in SETTING_FIELDS):
                    raise RuntimeError("Original world settings were not restored")
                report["cleanup"].append({"status": "passed", "stage": "restore_settings", "settings": restored})
            except Exception as exc:
                report["cleanup"].append({"status": "failed", "error": str(exc)})
                report["status"] = "failed"
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        persist()
    return 0 if report["status"] == "passed" else 1


def main(argv=None):
    args = parse_args(argv)
    # Claim the same world lease before making any CARLA connection or mutation.
    try:
        with world_lease(args.port):
            import carla
            return run(args, carla)
    except Exception as exc:
        save(args.output, {"version": 1, "status": "failed_preflight", "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
