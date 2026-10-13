#!/usr/bin/env python3
"""Read-only local archive hash check against CARLA release storage multipart ETag."""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import time
import urllib.request


def head(url):
    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as response:
        return {"url": response.url, "status": response.status, "headers": dict(response.headers)}


def verify(path):
    url = "https://carla-releases.s3.us-east-005.backblazeb2.com/Linux/" + path.name
    whole = head(url)
    first = head(url + "?partNumber=1")
    part_size = int(first["headers"]["Content-Length"])
    expected = whole["headers"]["ETag"].strip('"')
    chunks = []
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            part = hashlib.md5()
            total = 0
            while total < part_size:
                data = source.read(min(1024 * 1024, part_size - total))
                if not data:
                    break
                total += len(data)
                digest.update(data)
                part.update(data)
            if not total:
                break
            chunks.append(part.digest())
    actual = hashlib.md5(b"".join(chunks)).hexdigest() + "-" + str(len(chunks))
    return {"path": str(path.resolve()), "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(), "whole_head": whole, "first_part_head": first,
            "part_size": part_size, "actual_multipart_etag": actual,
            "expected_multipart_etag": expected, "matches_official_storage": actual == expected,
            "interpretation": "Multipart MD5 is a transport/content identity check, not a cryptographic publisher signature."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("archives", type=Path, nargs="+")
    args = parser.parse_args()
    start = time.monotonic()
    report = {"checked_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(), "archives": []}
    for archive in args.archives:
        report["archives"].append(verify(archive))
        report["elapsed_seconds"] = time.monotonic() - start
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: report["archives"][-1][k] for k in ("path", "sha256", "matches_official_storage")}), flush=True)
