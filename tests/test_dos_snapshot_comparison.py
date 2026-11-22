import gzip
import json
import struct

import pytest

from videogamebench_replayers.runtime import compare_dos_snapshots as comparison


def snapshot(path, heap=b"pixels\x00hidden", metadata=None):
    metadata = metadata or {
        "version": 1,
        "heapSize": len(heap),
        "build": "pinned-runtime",
        "identity": "task",
        "module": {"wakeUpAt": 100},
    }
    header = json.dumps(metadata).encode()
    path.write_bytes(
        gzip.compress(b"VGBDOS01" + struct.pack("<I", len(header)) + header + heap)
    )
    return path


def test_same_pixels_cannot_hide_memory_divergence(tmp_path):
    left = snapshot(tmp_path / "left.gz")
    right = snapshot(tmp_path / "right.gz", heap=b"pixels\x00HIDDEN")
    report = comparison.compare(left, right, max_bytes=4096, max_header_bytes=1024)
    assert not report["equal_exported_state"]
    assert report["different_fields"] == ["heap_sha256"]


def test_metadata_participates_in_state_comparison(tmp_path):
    left = snapshot(tmp_path / "left.gz")
    metadata = {
        "version": 1,
        "heapSize": len(b"pixels\x00hidden"),
        "build": "pinned-runtime",
        "identity": "task",
        "module": {"wakeUpAt": 200},
    }
    right = snapshot(tmp_path / "right.gz", metadata=metadata)
    report = comparison.compare(left, right, max_bytes=4096, max_header_bytes=1024)
    assert report["different_fields"] == ["metadata_sha256"]


def test_limits_apply_after_decompression(tmp_path):
    path = snapshot(tmp_path / "large.gz", heap=b"\x00" * 8192)
    with pytest.raises(ValueError, match="size limit"):
        comparison.fingerprint(path, max_bytes=1024, max_header_bytes=512)


def test_invalid_heap_is_not_accepted(tmp_path):
    path = snapshot(
        tmp_path / "invalid.gz", metadata={"version": 1, "heapSize": 99999}
    )
    with pytest.raises(ValueError, match="heap"):
        comparison.fingerprint(path, max_bytes=4096, max_header_bytes=1024)
