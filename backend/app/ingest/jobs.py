"""Job identity and raw storage (design 5.2, 18.1). Pure filesystem/id helpers; the job
row itself (status, detection, history) lives in registry.repository so every write goes
through the same registry connection.

Raw bytes are stored once per (device, sha256) under settings.OBJECT_STORE_DIR, a local
folder standing in for the object store (MinIO in production). Re-uploading the same
bytes for the same device reuses the file (and, via repository.find_reusable_job, the
earlier parse) instead of writing or processing it again.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

from app import settings

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def new_id() -> str:
    """A ULID (design 17.2): 48-bit millisecond timestamp + 80 random bits, Crockford
    base32, so ids sort lexicographically by creation time."""
    n = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        n, rem = divmod(n, 32)
        chars.append(_CROCKFORD[rem])
    return "".join(reversed(chars))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_device(device: str) -> str:
    """A device name made safe to use as a path segment."""
    return _SAFE.sub("_", device).strip("._") or "device"


def raw_path(device: str, digest: str) -> Path:
    return settings.OBJECT_STORE_DIR / "raw" / safe_device(device) / digest


def store_raw(device: str, data: bytes) -> tuple[str, str]:
    """Write raw bytes once per (device, sha256); existing files are never overwritten.
    -> (object_key relative to OBJECT_STORE_DIR, sha256)"""
    digest = sha256_hex(data)
    path = raw_path(device, digest)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return str(path.relative_to(settings.OBJECT_STORE_DIR)), digest


def read_raw(object_key: str) -> bytes:
    return (settings.OBJECT_STORE_DIR / object_key).read_bytes()
