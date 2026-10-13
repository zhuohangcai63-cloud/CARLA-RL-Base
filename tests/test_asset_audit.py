"""Small real gzip/tar fixtures; no CARLA or large installed assets needed."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "asset_audit", Path(__file__).parents[1] / "scripts/audit_carla_assets.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def archive(path, members):
    with tarfile.open(path, "w:gz") as package:
        for name, data in members:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            package.addfile(member, io.BytesIO(data))
    return path


@pytest.fixture
def root(tmp_path):
    result = tmp_path / "carla"
    result.mkdir()
    return result


def test_overlay_and_last_duplicate_win_without_extracting(tmp_path, root):
    base = archive(tmp_path / "base.tar.gz", [("asset", b"base"), ("kept", b"same")])
    extra = archive(tmp_path / "extra.tar.gz", [("asset", b"old"), ("asset", b"final")])
    (root / "asset").write_bytes(b"final")
    (root / "kept").write_bytes(b"same")
    (root / "user-file").write_bytes(b"untouched")
    manifest = tmp_path / "manifest.jsonl"
    result = MODULE.audit(root, base, [extra], manifest=manifest)
    assert result["status"] == "passed"
    assert result["counts"] == {"match": 2}
    assert result["archives"][1]["overlaid_paths"] == 1
    assert result["archives"][1]["duplicate_paths"] == 1
    assert all(a["gzip_crc"] == "passed" for a in result["archives"])
    assert [json.loads(line)["status"] for line in manifest.read_text().splitlines()] == ["match", "match"]
    assert (root / "user-file").read_bytes() == b"untouched"
    assert sorted(p.name for p in root.iterdir()) == ["asset", "kept", "user-file"]


def test_missing_and_tampered_files_are_bounded_in_summary(tmp_path, root):
    package = archive(tmp_path / "base.tar.gz", [("missing", b"one"), ("changed", b"two")])
    (root / "changed").write_bytes(b"bad")
    result = MODULE.audit(root, package, max_differences=1)
    assert result["status"] == "differences_found"
    assert result["counts"] == {"content_mismatch": 1, "missing": 1}
    assert result["difference_count"] == 2
    assert result["differences_omitted"] == 1
    assert len(result["differences"]) == 1
    assert (root / "changed").read_bytes() == b"bad"
    assert not (root / "missing").exists()


def test_corrupt_gzip_crc_is_rejected_after_tar_end(tmp_path, root):
    package = archive(tmp_path / "base.tar.gz", [("asset", b"ok")])
    (root / "asset").write_bytes(b"ok")
    data = bytearray(package.read_bytes())
    data[-8] ^= 1  # Damage CRC, retaining readable tar content.
    package.write_bytes(data)
    result = MODULE.audit(root, package)
    assert result["status"] == "invalid_archive"
    assert result["archives"][0]["gzip_crc"] == "not_verified"
    assert result["archives"][0]["error"]["type"] == "BadGzipFile"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "sub/../../escape"])
def test_unsafe_member_paths_are_rejected(tmp_path, root, name):
    package = archive(tmp_path / "base.tar.gz", [(name, b"bad")])
    result = MODULE.audit(root, package)
    assert result["status"] == "invalid_archive"
    assert "Unsafe archive member path" in result["archives"][0]["error"]["message"]
    assert not list(root.iterdir())
    assert not (tmp_path / "escape").exists()


def test_installed_symlink_cannot_escape_root(tmp_path, root):
    package = archive(tmp_path / "base.tar.gz", [("asset", b"secret")])
    outside = tmp_path / "private"
    outside.write_bytes(b"secret")
    (root / "asset").symlink_to(outside)
    result = MODULE.audit(root, package)
    assert result["counts"] == {"unsafe_installed_path": 1}
    assert "installed_sha256" not in result["differences"][0]


def test_cli_outputs_summary_and_rejects_writing_inside_installation(tmp_path, root):
    package = archive(tmp_path / "base.tar.gz", [("asset", b"ok")])
    (root / "asset").write_bytes(b"ok")
    output = tmp_path / "summary.json"
    args = ["--carla-root", str(root), "--base-archive", str(package), "--output"]
    assert MODULE.main(args + [str(output)]) == 0
    assert json.loads(output.read_text())["status"] == "passed"
    with pytest.raises(SystemExit) as exc:
        MODULE.main(args + [str(root / "asset")])
    assert exc.value.code == 2
    assert (root / "asset").read_bytes() == b"ok"


def test_later_extra_archive_takes_precedence(tmp_path, root):
    archives = [archive(tmp_path / f"{n}.tar.gz", [("asset", str(n).encode())]) for n in range(3)]
    (root / "asset").write_bytes(b"2")
    result = MODULE.audit(root, archives[0], archives[1:])
    assert result["status"] == "passed"
    assert [a["overlaid_paths"] for a in result["archives"]] == [0, 1, 1]


def test_standard_tar_root_directory_is_allowed(tmp_path, root):
    package = tmp_path / "base.tar.gz"
    with tarfile.open(package, "w:gz") as content:
        directory = tarfile.TarInfo("./")
        directory.type = tarfile.DIRTYPE
        content.addfile(directory)
        member = tarfile.TarInfo("./asset")
        member.size = 2
        content.addfile(member, io.BytesIO(b"ok"))
    (root / "asset").write_bytes(b"ok")
    result = MODULE.audit(root, package)
    assert result["status"] == "passed"
    assert result["archives"][0]["skipped_non_regular"] == {"directories": 1}


@pytest.mark.parametrize("directory_only", [False, True])
def test_empty_archives_cannot_pass(tmp_path, root, directory_only):
    package = tmp_path / "empty.tar.gz"
    with tarfile.open(package, "w:gz") as content:
        if directory_only:
            directory = tarfile.TarInfo("./")
            directory.type = tarfile.DIRTYPE
            content.addfile(directory)
    result = MODULE.audit(root, package)
    assert result["status"] == "invalid_archive"
    assert result["archives"][0]["gzip_crc"] == "passed"
    assert "no regular files" in result["archives"][0]["error"]["message"]


def test_atomic_outputs_preserve_hardlinked_inputs_and_publish_running(tmp_path, root, monkeypatch):
    package = archive(tmp_path / "base.tar.gz", [("asset", b"ok")])
    (root / "asset").write_bytes(b"ok")
    original_archive = package.read_bytes()
    summary = tmp_path / "summary.json"
    manifest = tmp_path / "manifest.jsonl"
    os.link(package, summary)
    os.link(root / "asset", manifest)
    original_scan = MODULE._scan

    def scan(*args):
        assert json.loads(summary.read_text())["status"] == "running"
        assert manifest.read_bytes() == b"ok"  # No partially written manifest.
        return original_scan(*args)

    monkeypatch.setattr(MODULE, "_scan", scan)
    assert MODULE.main(["--carla-root", str(root), "--base-archive", str(package),
                        "--output", str(summary), "--manifest", str(manifest)]) == 0
    assert package.read_bytes() == original_archive
    assert (root / "asset").read_bytes() == b"ok"
    assert json.loads(summary.read_text())["status"] == "passed"
    assert json.loads(manifest.read_text())["status"] == "match"
    assert not list(tmp_path.glob("*.partial"))


def test_failed_atomic_writer_keeps_old_file_and_removes_partial(tmp_path):
    output = tmp_path / "manifest.jsonl"
    output.write_text("old-complete")
    with pytest.raises(RuntimeError):
        with MODULE._atomic_writer(output) as stream:
            stream.write("incomplete")
            raise RuntimeError("interrupted")
    assert output.read_text() == "old-complete"
    assert not list(tmp_path.glob("*.partial"))
