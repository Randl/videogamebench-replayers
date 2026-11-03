"""Immutable, bounded execution-log objects shared by recording branches."""

import gzip
import hashlib
import json
import math
import re
from pathlib import Path
from uuid import uuid4

OBJECT_ID = re.compile(r"[a-f0-9]{64}")
INPUT_NAMES = {
    "wc-add-key",
    "wc-mouse-move",
    "wc-mouse-button",
    "wc-mouse-sync",
    "wc-asyncify-stats",
    "wc-mute",
    "wc-unmute",
    "wc-pause",
    "wc-resume",
    "wc-get-running-program",
}


def canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def atomic_write(path, data):
    temporary = path.with_name("." + uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def valid_fingerprint(value):
    if (
        not isinstance(value, dict)
        or not {"heap_sha256", "frame_sha256", "width", "height"} <= value.keys()
    ):
        return False
    if value.keys() - {
        "heap_sha256",
        "frame_sha256",
        "state_sha256",
        "width",
        "height",
    }:
        return False
    return all(
        type(value[k]) is int and 0 < value[k] <= 16384 for k in ("width", "height")
    ) and all(
        isinstance(v, str) and OBJECT_ID.fullmatch(v)
        for k, v in value.items()
        if k.endswith("sha256")
    )


def valid_input(name, props):
    if (
        not isinstance(name, str)
        or name not in INPUT_NAMES
        or not isinstance(props, dict)
    ):
        return False
    fields = {
        "wc-add-key": {"key", "pressed", "timeMs"},
        "wc-mouse-move": {"x", "y", "relative", "timeMs"},
        "wc-mouse-button": {"button", "pressed", "timeMs"},
        "wc-mouse-sync": {"timeMs"},
    }.get(name, set())
    if props.keys() != fields:
        return False
    for key, value in props.items():
        if key in {"pressed", "relative"}:
            if type(value) is not bool:
                return False
        elif type(value) not in (int, float) or not math.isfinite(value):
            return False
    return True


def validate_events(events):
    if not isinstance(events, list) or not events:
        raise ValueError("Execution chunk must contain events")
    for event in events:
        if not isinstance(event, list) or not event or type(event[0]) is not int:
            raise ValueError("Invalid execution event")
        kind = event[0]
        if kind in (0, 1):
            valid = (
                len(event) == 3
                and type(event[1]) in (int, float)
                and math.isfinite(event[1])
                and type(event[2]) is int
                and 0 < event[2] <= 2**32 - 1
            )
        elif kind == 2:
            valid = len(event) == 1
        elif kind == 3:
            valid = len(event) == 3 and valid_input(event[1], event[2])
        elif kind == 4:
            valid = (
                len(event) == 3
                and isinstance(event[1], str)
                and 0 < len(event[1]) <= 128
                and valid_fingerprint(event[2])
            )
        elif kind == 5:
            valid = (
                len(event) == 2
                and isinstance(event[1], list)
                and len(event[1]) == 1
                and isinstance(event[1][0], str)
                and len(event[1][0]) <= 128
            )
        else:
            valid = False
        if not valid:
            raise ValueError(f"Unsupported or malformed execution event: {kind}")


class ExecutionStore:
    def __init__(self, directory, *, max_chunk_bytes, max_total_bytes):
        if not 0 < max_chunk_bytes <= max_total_bytes:
            raise ValueError("Invalid execution-log size limits")
        self.directory = Path(directory)
        self.objects = self.directory / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.max_chunk_bytes = max_chunk_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = sum(p.stat().st_size for p in self.objects.glob("*.json.gz"))
        if self.total_bytes > max_total_bytes:
            raise ValueError("Execution store exceeds its storage limit")

    def path(self, identifier):
        if not isinstance(identifier, str) or not OBJECT_ID.fullmatch(identifier):
            raise ValueError("Invalid execution object identifier")
        return self.objects / (identifier + ".json.gz")

    def read(self, identifier):
        path = self.path(identifier)
        if path.is_symlink():
            raise ValueError("Execution objects must not be symlinks")
        with gzip.open(path, "rb") as source:
            data = source.read(self.max_chunk_bytes + 1)
        if len(data) > self.max_chunk_bytes:
            raise ValueError("Execution chunk exceeds the uncompressed limit")
        if hashlib.sha256(data).hexdigest() != identifier:
            raise ValueError("Execution chunk checksum mismatch")
        value = json.loads(data)
        self.validate(value)
        return value

    def validate(self, value):
        if not isinstance(value, dict) or set(value) != {"parent", "events"}:
            raise ValueError("Invalid execution chunk")
        if value["parent"] is not None:
            self.path(value["parent"])
        validate_events(value["events"])

    def put(self, value):
        self.validate(value)
        if value["parent"] is not None:
            self.read(value["parent"])
        data = canonical(value)
        if len(data) > self.max_chunk_bytes:
            raise ValueError("Execution chunk exceeds the uncompressed limit")
        identifier = hashlib.sha256(data).hexdigest()
        path = self.path(identifier)
        if not path.exists():
            compressed = gzip.compress(data, mtime=0)
            if self.total_bytes + len(compressed) > self.max_total_bytes:
                raise ValueError("Execution recording exceeds its storage limit")
            atomic_write(path, compressed)
            self.total_bytes += len(compressed)
        else:
            self.read(identifier)
        return identifier

    def chain(self, head):
        identifiers, seen = [], set()
        while head is not None:
            if head in seen:
                raise ValueError("Cycle in execution log")
            seen.add(head)
            identifiers.append(head)
            head = self.read(head)["parent"]
        return list(reversed(identifiers))

    def export(self, head, output, metadata):
        identifiers = self.chain(head)
        if not identifiers:
            raise ValueError("Cannot export an empty execution log")
        # Validate the entire retained branch before creating an output bundle.
        markers, events = [], 0
        for identifier in identifiers:
            chunk = self.read(identifier)
            events += len(chunk["events"])
            markers.extend(e[1] for e in chunk["events"] if e[0] == 4)
        if not markers or markers[-1] != "end" or chunk["events"][-1][:2] != [4, "end"]:
            raise ValueError("Execution log has no final verification boundary")
        if len(markers) != len(set(markers)):
            raise ValueError("Execution boundary identifiers must be unique")
        output = Path(output)
        output.mkdir(parents=True, exist_ok=False)
        (output / "objects").mkdir()
        for identifier in identifiers:
            atomic_write(
                output / "objects" / self.path(identifier).name,
                self.path(identifier).read_bytes(),
            )
        manifest = {
            "format": 1,
            "head": head,
            "chunks": identifiers,
            "events": events,
            "markers": markers,
            "metadata": metadata,
        }
        atomic_write(output / "execution.json", canonical(manifest))
        return manifest
