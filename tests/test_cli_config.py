"""Configuration assets resolve independently of the caller's working directory."""
from pathlib import Path
import json

import pytest
import yaml

from easycarla.service.cli import load_config


def test_default_paths_are_relative_to_config_directory(tmp_path, monkeypatch):
    settings = tmp_path / "settings"
    settings.mkdir()
    path = settings / "scene.yaml"
    path.write_text(yaml.safe_dump({"scenario": {"source": "scenic", "file": "scene.scenic"},
                                    "simulation": {"carla_root": "carla"}}))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    config = load_config(path)
    assert config["scenario"]["file"] == str(settings / "scene.scenic")
    assert config["simulation"]["carla_root"] == str(settings / "carla")


def test_explicit_base_applies_recursively_but_does_not_read_manifests(tmp_path):
    settings = tmp_path / "config"
    settings.mkdir()
    path = settings / "mixed.yaml"
    path.write_text(yaml.safe_dump({"path_base": "..", "scenario": {
        "source": "mixed", "sources": [
            {"source": "scenic", "files": ["a.scenic", "b.scenic"],
             "fallback": {"source": "scenic", "file": "fallback.scenic"}},
            {"source": "bench2drive", "manifest": "missing-manifest.json", "dependency_root": "dependency"}]}}))
    config = load_config(path)
    first, second = config["scenario"]["sources"]
    assert first["files"] == [str(tmp_path / "a.scenic"), str(tmp_path / "b.scenic")]
    assert first["fallback"]["file"] == str(tmp_path / "fallback.scenic")
    assert second["manifest"] == str(tmp_path / "missing-manifest.json")
    assert second["dependency_root"] == str(tmp_path / "dependency")
    assert "path_base" not in config


def test_home_paths_and_absolute_paths_are_preserved(tmp_path):
    path = tmp_path / "scene.json"
    path.write_text('{"simulation":{"carla_root":"~/carla"},"scenario":{"file":"/opt/scene.scenic"}}')
    config = load_config(path)
    assert config["simulation"]["carla_root"] == str(Path.home() / "carla")
    assert config["scenario"]["file"] == "/opt/scene.scenic"


@pytest.mark.parametrize("data", [{"path_base": None}, {"path_base": 2},
                                  {"scenario": {"file": []}}, {"scenario": {"files": "bad"}}])
def test_invalid_path_configuration_fails_before_backend_creation(tmp_path, data):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_config(path)


def test_repository_examples_can_be_loaded_from_another_working_directory(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    paths = list((root / "configs").glob("*.yaml")) + [
        path for path in (root / "configs/validation").glob("*.json")
        if "scenario" in json.loads(path.read_text())]
    for path in paths:
        config = load_config(path)
        def check(source):
            for key in ("file", "manifest"):
                if key in source:
                    assert Path(source[key]).is_absolute()
                    assert Path(source[key]).is_file(), (path, source[key])
            for filename in source.get("files", []):
                assert Path(filename).is_file(), (path, filename)
            for child in source.get("sources", []):
                check(child)
            if source.get("fallback"):
                check(source["fallback"])
        if "scenario" in config:
            check(config["scenario"])


def test_nested_bench2drive_settings_resolve_same_base(tmp_path):
    path = tmp_path / "native.yaml"
    path.write_text(yaml.safe_dump({"scenario": {"source": "bench2drive", "bench2drive": {
        "manifest": "routes.json", "dependency_root": "source"}}}))
    settings = load_config(path)["scenario"]["bench2drive"]
    assert settings["manifest"] == str(tmp_path / "routes.json")
    assert settings["dependency_root"] == str(tmp_path / "source")


def test_environment_default_uses_carla_root_without_importing_simulator(tmp_path):
    import os
    import subprocess
    import sys
    env = dict(os.environ, CARLA_ROOT=str(tmp_path / "custom-cARLa"))
    output = subprocess.check_output([sys.executable, "-c",
        "from easycarla.config import validate_config; print(validate_config({})['simulation']['carla_root'])"],
        text=True, env=env)
    assert output.strip() == str(tmp_path / "custom-cARLa")


def test_direct_runtime_dictionary_expands_home_for_carla_python_api(monkeypatch):
    import sys
    from types import SimpleNamespace
    from easycarla.config import validate_config
    from easycarla.simulation.session import WorldSession
    expected = Path.home() / "PythonAPI/carla"
    original = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: True if self == expected else original(self))
    monkeypatch.setitem(sys.modules, "carla", SimpleNamespace(
        Client=lambda host, port: SimpleNamespace(set_timeout=lambda timeout: None)))
    monkeypatch.setattr(sys, "path", list(sys.path))
    session = WorldSession(validate_config({"simulation": {"carla_root": "~", "port": 61397}}))
    try:
        assert str(expected) in sys.path
    finally:
        session.close()


def test_cyclic_yaml_scenario_alias_has_bounded_preflight(tmp_path):
    path = tmp_path / "cycle.yaml"
    path.write_text("scenario: &source\n  source: scenic\n  fallback: *source\n")
    with pytest.raises(ValueError, match="deeply nested or cyclic"):
        load_config(path)
