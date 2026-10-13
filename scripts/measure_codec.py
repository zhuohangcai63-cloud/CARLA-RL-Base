#!/usr/bin/env python3
"""Benchmark the local codec on one real observation captured from a service.

After reset, the client session is closed. Encoding and validated decode/copy then
run locally against fixed data; these timings do not measure IPC or simulation.
Only the standalone client package and its dependencies are required.
"""
import argparse
import json
from pathlib import Path
import platform
import sys
import time
import uuid

import numpy as np
from easycarla_client import CarlaEnvClient
from easycarla_client.protocol import HEADER_BYTES, encode, read_slot, write_slot


def summary_ms(samples):
    values = np.asarray(samples, dtype=np.float64) * 1000
    return {"count": len(samples), "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)), "mean": float(values.mean()),
            "min": float(values.min()), "max": float(values.max())}


def array_bytes(value):
    if isinstance(value, np.ndarray):
        return value.nbytes
    if isinstance(value, dict):
        return sum(array_bytes(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return sum(array_bytes(v) for v in value)
    return 0


def benchmark_observation(obs, iterations):
    """Pure local benchmark, separately callable by tests without a live service."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    payload = encode(obs)
    # The bytearray mimics an already published slot; its allocation and initial
    # publication occur outside the measured loops. No semaphore or mmap is used.
    slot = bytearray(HEADER_BYTES + len(payload))
    write_slot(slot, epoch=str(uuid.uuid4()), session=str(uuid.uuid4()), sequence=1,
               operation="OBSERVATION", payload=payload)
    _, restored = read_slot(slot, copy=True)
    del restored
    encoding, decoding = [], []
    for _ in range(iterations):
        started = time.perf_counter()
        encoded = encode(obs)
        encoding.append(time.perf_counter() - started)
        del encoded
    for _ in range(iterations):
        started = time.perf_counter()
        _, restored = read_slot(slot, copy=True)
        decoding.append(time.perf_counter() - started)
        del restored
    return {"iterations": iterations, "payload_bytes": len(payload),
            "slot_bytes_including_header": len(slot),
            "observation_array_bytes": array_bytes(obs),
            "encoding_ms": summary_ms(encoding),
            "validation_decode_copy_ms": summary_ms(decoding)}


def measure(args):
    report = {"status": "running", "measurement_kind": "local_fixed_observation_codec_microbenchmark",
              "instance": args.instance, "python": platform.python_version(),
              "client_prefix": sys.prefix, "requested_iterations": args.iterations,
              "is_ipc_benchmark": False, "is_simulation_benchmark": False,
              "notes": ["A fixed observation is captured by reset; the client session is closed before timing.",
                        "Encoding includes JSON/tree processing, array layout and payload construction; excludes SHA-256 and slot publication.",
                        "Validation/decode/copy includes header/capacity validation, SHA-256 verification, metadata decode and independent ndarray copies.",
                        "Fixed-slot allocation, initial publication, one warm-up decode and result destruction are outside the measured intervals.",
                        "These local timings are not shared-memory IPC latency or simulator performance."]}
    try:
        with CarlaEnvClient(args.instance, timeout=args.timeout, copy=True) as env:
            obs, info = env.reset(seed=args.seed)
        report["session_closed_before_benchmark"] = True
        report["backend"] = info.get("backend", "carla" if info.get("observation_profile") == "privileged_structured_truth" else "not_reported")
        report["scenario"] = info.get("scenario", {})
        report["observation_profile"] = info.get("observation_profile", "not_reported")
        report["frame"] = obs.get("frame")
        report["sensor_keys"] = sorted(obs.get("sensors", {}))
        report.update(benchmark_observation(obs, args.iterations))
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    return report


def json_scalar(value):
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError(f"Unsupported report value: {type(value).__name__}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="carla_01")
    parser.add_argument("--output", type=Path, help="Write JSON report in addition to stdout")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    if args.iterations < 1:
        parser.error("iterations must be positive")
    report = measure(args)
    text = json.dumps(report, indent=2, ensure_ascii=False, default=json_scalar)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n")
    print(text)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
