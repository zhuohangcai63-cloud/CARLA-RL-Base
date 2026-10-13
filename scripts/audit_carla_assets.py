#!/usr/bin/env python3
"""Read-only local CARLA tar.gz/installation audit; no CARLA or network required.

Base archive members are overlaid by extra archives in command-line order.
Duplicate paths within an archive use the last member. A temporary SQLite index
keeps memory bounded; optional JSONL manifests stream full per-file evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile
import time


def _digest(stream):
    result = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        result.update(chunk)
    return result.hexdigest()


def _member_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"Unsafe archive member path: {name!r}")
    return str(path)


def _scan(archive, index, number):
    result = {"path": str(archive), "bytes": archive.stat().st_size,
              "gzip_crc": "not_verified", "regular_members": 0,
              "regular_bytes": 0, "duplicate_paths": 0,
              "overlaid_paths": 0, "skipped_non_regular": {}}
    skipped = Counter()
    try:
        with gzip.open(archive, "rb") as compressed:
            with tarfile.open(fileobj=compressed, mode="r|") as members:
                for member in members:
                    # Python 3.10 tarfile retains TarInfo objects even in stream
                    # mode. We need only the current one, not an in-memory index.
                    members.members.clear()
                    if member.isdir() and member.name in (".", "./"):
                        skipped["directories"] += 1
                        continue
                    name = _member_path(member.name)
                    if not member.isfile():
                        skipped["directories" if member.isdir() else "other"] += 1
                        continue
                    with members.extractfile(member) as content:
                        digest = _digest(content)
                    previous = index.execute("SELECT source FROM files WHERE path = ?", (name,)).fetchone()
                    if previous:
                        key = "duplicate_paths" if previous[0] == number else "overlaid_paths"
                        result[key] += 1
                    index.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                                  (name, member.size, digest, number))
                    result["regular_members"] += 1
                    result["regular_bytes"] += member.size
            # tar's zero blocks precede the gzip trailer: force CRC/size validation.
            while compressed.read(1024 * 1024):
                pass
        result["gzip_crc"] = "passed"
        if result["regular_members"] == 0:
            raise ValueError("Archive contains no regular files to audit")
    except (OSError, EOFError, tarfile.TarError, ValueError) as error:
        result["error"] = {"type": type(error).__name__, "message": str(error)}
    result["skipped_non_regular"] = dict(skipped)
    index.commit()
    return result


def _compare(root, row):
    name, size, digest, source = row
    result = {"path": name, "expected_bytes": size, "expected_sha256": digest,
              "source_archive_index": source}
    installed = root / name
    try:
        # Do not follow an installed symlink out of the installation tree.
        if not installed.resolve().is_relative_to(root):
            result["status"] = "unsafe_installed_path"
        elif not installed.exists():
            result["status"] = "missing"
        elif not installed.is_file() or installed.is_symlink():
            result["status"] = "not_regular_file"
        else:
            result["installed_bytes"] = installed.stat().st_size
            with installed.open("rb") as content:
                result["installed_sha256"] = _digest(content)
            result["status"] = "match" if digest == result["installed_sha256"] else "content_mismatch"
    except (OSError, RuntimeError) as error:
        result.update(status="read_error", error=str(error))
    return result


def _check_output(path, root, archives):
    if path is None:
        return
    resolved = Path(path).resolve()
    if resolved.is_relative_to(root) or resolved in archives:
        raise ValueError("Audit output must be outside the CARLA installation and input archives")


@contextmanager
def _atomic_writer(path):
    """Never expose partial evidence or truncate an existing hard-linked file."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".partial",
                                         delete=False) as output:
            temporary = Path(output.name)
            yield output
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _write_json(path, value):
    with _atomic_writer(path) as output:
        output.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def audit(carla_root, base_archive, extra_archives=(), *, manifest=None, max_differences=100):
    """Return a bounded summary. No input files are created, extracted or changed."""
    start = time.monotonic()
    root = Path(carla_root).resolve()
    archives = [Path(p).resolve() for p in (base_archive, *extra_archives)]
    if not root.is_dir():
        raise ValueError(f"CARLA root is not a directory: {root}")
    if max_differences < 0:
        raise ValueError("max_differences must be non-negative")
    _check_output(manifest, root, archives)
    report = {
        "schema_version": 1, "checked_utc": datetime.now(timezone.utc).isoformat(),
        "carla_root": str(root), "status": "running", "archives": [],
        "scope": "Regular archive files; installation-only files and non-regular archive members are not audited",
        "overlay_order": "Base, then extras in argument order; last duplicate member wins",
        "network_used": False, "differences": [],
    }
    with tempfile.TemporaryDirectory(prefix="easycarla-asset-audit-") as temporary:
        with sqlite3.connect(str(Path(temporary) / "index.sqlite")) as index:
            index.execute("PRAGMA cache_size = -2048")
            index.execute("CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, digest TEXT, source INTEGER)")
            for number, archive in enumerate(archives):
                result = _scan(archive, index, number)
                report["archives"].append(result)
                if "error" in result:
                    report.update(status="invalid_archive", elapsed_seconds=time.monotonic() - start)
                    return report
            counts = Counter()
            expected_bytes = 0
            writer = _atomic_writer(manifest) if manifest is not None else nullcontext(None)
            with writer as manifest_file:
                for row in index.execute("SELECT path, size, digest, source FROM files ORDER BY path"):
                    result = _compare(root, row)
                    counts[result["status"]] += 1
                    expected_bytes += row[1]
                    if result["status"] != "match" and len(report["differences"]) < max_differences:
                        report["differences"].append(result)
                    if manifest_file:
                        manifest_file.write(json.dumps(result, ensure_ascii=False) + "\n")
    total = sum(counts.values())
    mismatch_count = total - counts["match"]
    report.update(status="passed" if mismatch_count == 0 else "differences_found",
                  regular_paths=total, expected_bytes=expected_bytes, counts=dict(counts),
                  difference_count=mismatch_count,
                  differences_omitted=max(0, mismatch_count - len(report["differences"])),
                  elapsed_seconds=time.monotonic() - start)
    if manifest is not None:
        report["manifest"] = str(Path(manifest).resolve())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--carla-root", type=Path, required=True)
    parser.add_argument("--base-archive", type=Path, required=True)
    parser.add_argument("--extra-archive", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, help="Summary JSON, defaults to stdout")
    parser.add_argument("--manifest", type=Path, help="Optional full per-file JSONL hash manifest")
    parser.add_argument("--max-differences", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        root = args.carla_root.resolve()
        archives = [p.resolve() for p in (args.base_archive, *args.extra_archive)]
        _check_output(args.output, root, archives)
        if args.output and args.manifest and args.output.resolve() == args.manifest.resolve():
            raise ValueError("Summary and manifest must have different paths")
        if args.output:
            _write_json(args.output, {"schema_version": 1, "status": "running",
                                     "carla_root": str(root), "archives": [str(p) for p in archives]})
        result = audit(root, args.base_archive, args.extra_archive,
                       manifest=args.manifest, max_differences=args.max_differences)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    encoded = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        _write_json(args.output, result)
    else:
        print(encoded, end="")
    return {"passed": 0, "differences_found": 1, "invalid_archive": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
