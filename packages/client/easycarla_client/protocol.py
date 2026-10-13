"""Versioned bounded JSON / ndarray codec. No CARLA objects or pickle cross IPC."""
from dataclasses import dataclass
import hashlib
import json
import math
import struct
import uuid

import numpy as np

from .errors import ProtocolError

VERSION = 1
MAGIC = b"ECRLIPC\0"
HEADER_BYTES = 256
_HEADER = struct.Struct("<8sII16s16sQQQ32s32s")
EMPTY, READY = 0, 1
DEFAULT_REQUEST_BYTES = 1 << 20
DEFAULT_RESPONSE_BYTES = 32 << 20
DEFAULT_STATIC_BYTES = 16 << 20


@dataclass(frozen=True)
class Header:
    epoch: str
    session: str
    sequence: int
    operation: str
    payload_length: int
    capacity: int
    digest: bytes
    state: int = READY


def _align(n):
    return (n + 63) & ~63


def encode(value):
    arrays = []
    descriptors = []
    offset = 0

    def visit(item):
        nonlocal offset
        if isinstance(item, np.ndarray):
            arr = item if item.flags.c_contiguous else np.ascontiguousarray(item)
            if arr.dtype.kind not in "biufc" or arr.dtype.hasobject:
                raise ProtocolError(f"Unsupported array dtype: {arr.dtype}")
            offset = _align(offset)
            descriptors.append(dict(dtype=arr.dtype.str, shape=list(arr.shape),
                                    offset=offset, nbytes=arr.nbytes))
            arrays.append((offset, arr))
            offset += arr.nbytes
            return {"$array": len(arrays) - 1}
        if isinstance(item, np.generic):
            return visit(item.item())
        if isinstance(item, dict):
            if not all(isinstance(k, str) for k in item):
                raise ProtocolError("Protocol dictionary keys must be strings")
            # Explicit dictionary nodes avoid reserved-key collisions.
            return {"$dict": {k: visit(v) for k, v in item.items()}}
        if isinstance(item, tuple):
            return {"$tuple": [visit(v) for v in item]}
        if isinstance(item, list):
            return [visit(v) for v in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise ProtocolError(f"Unsupported protocol value: {type(item).__name__}")

    try:
        metadata = json.dumps({"tree": visit(value), "arrays": descriptors},
                              separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProtocolError(f"Invalid JSON metadata: {exc}") from exc
    data_start = _align(4 + len(metadata))
    payload = bytearray(data_start + offset)
    struct.pack_into("<I", payload, 0, len(metadata))
    payload[4:4 + len(metadata)] = metadata
    for array_offset, arr in arrays:
        payload[data_start + array_offset:data_start + array_offset + arr.nbytes] = arr.tobytes()
    return payload


def decode(buffer, length, *, copy=True, offset=HEADER_BYTES):
    if length < 4 or offset + length > len(buffer):
        raise ProtocolError("Invalid payload length")
    json_length = struct.unpack_from("<I", buffer, offset)[0]
    if json_length > length - 4:
        raise ProtocolError("JSON metadata exceeds payload")
    try:
        def invalid_constant(value):
            raise ValueError(f"Non-finite JSON constant {value}")
        metadata = json.loads(buffer[offset + 4:offset + 4 + json_length],
                              parse_constant=invalid_constant)
        data_start = offset + _align(4 + json_length)
        arrays = []
        for desc in metadata["arrays"]:
            dtype = np.dtype(desc["dtype"])
            shape = desc["shape"]
            if dtype.kind not in "biufc" or dtype.hasobject or len(shape) > 16:
                raise ValueError("Unsupported dtype or dimension")
            if any(type(n) is not int or n < 0 for n in shape):
                raise ValueError("Invalid shape")
            nbytes = math.prod(shape) * dtype.itemsize
            start = data_start + desc["offset"]
            if desc["offset"] < 0 or nbytes != desc["nbytes"] or start + nbytes > offset + length:
                raise ValueError("Array outside declared payload")
            arr = np.ndarray(shape, dtype=dtype, buffer=buffer, offset=start)
            if copy:
                arr = arr.copy()
            else:
                arr.flags.writeable = False
            arrays.append(arr)

        def restore(item):
            if isinstance(item, list):
                return [restore(v) for v in item]
            if isinstance(item, dict):
                if "$array" in item:
                    idx = item["$array"]
                    if type(idx) is not int or not 0 <= idx < len(arrays):
                        raise ValueError("Invalid array reference")
                    return arrays[idx]
                if "$tuple" in item:
                    return tuple(restore(v) for v in item["$tuple"])
                if "$dict" in item:
                    return {k: restore(v) for k, v in item["$dict"].items()}
                raise ValueError("Invalid tree node")
            return item
        return restore(metadata["tree"])
    except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
        raise ProtocolError(f"Malformed payload: {exc}") from exc


def write_slot(buffer, *, epoch, session, sequence, operation, value=None, payload=None):
    payload = encode(value) if payload is None else payload
    capacity = len(buffer) - HEADER_BYTES
    if len(payload) > capacity:
        raise ProtocolError(f"Payload {len(payload)} exceeds slot capacity {capacity}; configure a larger slot")
    operation_bytes = operation.encode("ascii")
    if len(operation_bytes) > 32:
        raise ProtocolError("Operation name is too long")
    digest = hashlib.sha256(payload).digest()
    # The receiver must wait for publication via the semaphore, not poll this header.
    buffer[HEADER_BYTES:HEADER_BYTES + len(payload)] = payload
    packed = _HEADER.pack(MAGIC, VERSION, READY, uuid.UUID(epoch).bytes,
                          uuid.UUID(session).bytes, sequence, len(payload), capacity,
                          operation_bytes, digest)
    buffer[:len(packed)] = packed
    return digest


def read_header(buffer):
    try:
        magic, version, state, epoch, session, seq, length, capacity, op, digest = _HEADER.unpack_from(buffer)
        if magic != MAGIC or version != VERSION or state != READY:
            raise ProtocolError("Uninitialized slot or incompatible protocol version")
        if capacity != len(buffer) - HEADER_BYTES or length > capacity:
            raise ProtocolError("Invalid slot capacity / payload length")
        return Header(str(uuid.UUID(bytes=epoch)), str(uuid.UUID(bytes=session)), seq,
                      op.rstrip(b"\0").decode("ascii"), length, capacity, digest, state)
    except (ValueError, struct.error) as exc:
        raise ProtocolError(f"Invalid protocol header: {exc}") from exc


def read_slot(buffer, *, copy=True):
    header = read_header(buffer)
    digest = hashlib.sha256(memoryview(buffer)[HEADER_BYTES:HEADER_BYTES + header.payload_length]).digest()
    if digest != header.digest:
        raise ProtocolError("Incomplete or corrupt shared-memory payload")
    return header, decode(buffer, header.payload_length, copy=copy)


def copy_observation(observation):
    """Recursively copy an observation, including arrays borrowed from shared memory."""
    if isinstance(observation, np.ndarray):
        return observation.copy()
    if isinstance(observation, dict):
        return {k: copy_observation(v) for k, v in observation.items()}
    if isinstance(observation, tuple):
        return tuple(copy_observation(v) for v in observation)
    if isinstance(observation, list):
        return [copy_observation(v) for v in observation]
    return observation
