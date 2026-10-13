"""Command-line launch and crash-residue cleanup; no training dependencies."""
import argparse
import json
import logging
from pathlib import Path
import signal

from easycarla_client.transport import cleanup_instance
from .server import EnvironmentService


def load_config(path):
    """Resolve local config assets once, independently of the launch directory.

    YAML/JSON paths default to their config directory. ``path_base`` overrides
    that base relative to the config directory. Manifest entry paths themselves
    remain relative to the prepared Bench2Drive checkout, owned by its adapter.
    """
    path = Path(path).expanduser().resolve()
    text = path.read_text()
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        import yaml
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a mapping")
    base_value = data.pop("path_base", ".")
    if not isinstance(base_value, str) or not base_value:
        raise ValueError("path_base must be a nonempty path string")
    base = Path(base_value).expanduser()
    if not base.is_absolute():
        base = path.parent / base
    base = base.resolve()

    def resolve(value, name):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a nonempty path string")
        result = Path(value).expanduser()
        return str((base / result).resolve()) if not result.is_absolute() else str(result.resolve())

    def scenario_paths(scenario, depth=0):
        if depth > 16:
            raise ValueError("Scenario path configuration is too deeply nested or cyclic")
        if not isinstance(scenario, dict):
            raise ValueError("scenario sources must be mappings")
        for key in ("file", "manifest", "dependency_root"):
            if key in scenario:
                scenario[key] = resolve(scenario[key], "scenario." + key)
        if "files" in scenario:
            if not isinstance(scenario["files"], list):
                raise ValueError("scenario.files must be a list")
            scenario["files"] = [resolve(value, "scenario.files") for value in scenario["files"]]
        children = scenario.get("sources", [])
        if not isinstance(children, list):
            raise ValueError("scenario.sources must be a list")
        for child in children:
            scenario_paths(child, depth + 1)
        if "bench2drive" in scenario:
            scenario_paths(scenario["bench2drive"], depth + 1)
        if scenario.get("fallback") is not None:
            scenario_paths(scenario["fallback"], depth + 1)

    if "scenario" in data:
        scenario_paths(data["scenario"])
    if isinstance(data.get("simulation"), dict) and "carla_root" in data["simulation"]:
        data["simulation"]["carla_root"] = resolve(data["simulation"]["carla_root"], "simulation.carla_root")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent EasyCARLA environment service")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--instance", default="carla_01")
    serve.add_argument("--config", required=True)
    clean = sub.add_parser("cleanup", help="Remove abandoned IPC objects; refuses active instances")
    clean.add_argument("--instance", required=True)
    fleet = sub.add_parser("fleet", help="Manage owned headless CARLA workers")
    fleet.add_argument("action", choices=("start", "status", "stop", "restart"))
    fleet.add_argument("--config", required=True)
    fleet.add_argument("--worker", help="Operate on a single configured worker ID")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if args.command == "fleet":
        from .fleet import fleet_command
        print(json.dumps(fleet_command(args.action, args.config, args.worker), indent=2))
        return
    if args.command == "cleanup":
        cleanup_instance(args.instance)
        return
    config = load_config(args.config)
    backend = config.get("backend", "carla")
    if backend == "fake":
        from .testing import FakeBackend
        factory = lambda: FakeBackend(config.get("fake", {}))
    elif backend == "carla":
        from easycarla.runtime import EnvironmentRuntime
        factory = lambda: EnvironmentRuntime(config)
    else:
        parser.error(f"Unknown backend {backend!r}")
    service = EnvironmentService(args.instance, factory, **config.get("ipc", {}))
    signal.signal(signal.SIGTERM, lambda *_: service.stop())
    signal.signal(signal.SIGINT, lambda *_: service.stop())
    service.serve_forever()


if __name__ == "__main__":
    main()
