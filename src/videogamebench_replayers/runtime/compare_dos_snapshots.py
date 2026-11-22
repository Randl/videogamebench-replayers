"""Compare complete exported DOS snapshots, including memory outside the display."""

import argparse
import gzip
import hashlib
import json
import struct
from pathlib import Path


def fingerprint(path, *, max_bytes, max_header_bytes):
    with gzip.open(path, "rb") as source:
        data = source.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("Snapshot exceeds the uncompressed size limit")
    if len(data) < 12 or data[:8] != b"VGBDOS01":
        raise ValueError("Unsupported snapshot format")
    header_size = struct.unpack_from("<I", data, 8)[0]
    if not 0 < header_size <= max_header_bytes or 12 + header_size >= len(data):
        raise ValueError("Invalid snapshot header length")
    metadata = json.loads(data[12 : 12 + header_size])
    payload = memoryview(data)[12 + header_size :]
    heap_size = metadata.get("heapSize")
    if (
        metadata.get("version") != 1
        or type(heap_size) is not int
        or not 0 < heap_size <= len(payload)
    ):
        raise ValueError("Invalid snapshot heap or version")
    heap = payload[:heap_size]
    digest = lambda value: hashlib.sha256(value).hexdigest()
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    return {
        "build": metadata.get("build"),
        "identity": metadata.get("identity"),
        "heap_bytes": heap_size,
        "heap_sha256": digest(heap),
        "filesystem_bytes_sha256": digest(payload[heap_size:]),
        "metadata_sha256": digest(canonical.encode()),
    }


def compare(left, right, *, max_bytes, max_header_bytes):
    options = {"max_bytes": max_bytes, "max_header_bytes": max_header_bytes}
    a, b = fingerprint(left, **options), fingerprint(right, **options)
    return {
        "equal_exported_state": a == b,
        "different_fields": [key for key in a if a[key] != b[key]],
        "left": a,
        "right": b,
        "scope": (
            "All exported memory, filesystem data and snapshot metadata. "
            "Equality does not cover browser or emulator fields absent from the "
            "snapshot format, and does not establish continuation reproducibility."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-bytes", type=int, default=256 * 1024 * 1024)
    parser.add_argument("--max-header-bytes", type=int, default=2 * 1024 * 1024)
    args = parser.parse_args()
    if min(args.max_bytes, args.max_header_bytes) <= 0:
        parser.error("Size limits must be positive")
    report = compare(
        args.left,
        args.right,
        max_bytes=args.max_bytes,
        max_header_bytes=args.max_header_bytes,
    )
    text = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text, end="")
    return 0 if report["equal_exported_state"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
