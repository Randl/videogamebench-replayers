"""Preserve human attempts without mixing their traces or videos."""

import copy
import hashlib
import json
import math
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

OBSERVATIONS = ".recording-observations"


def store_observation(state_dir: Path, screen: bytes) -> dict:
    """Keep immutable images shared by attempts and snapshot-restored prefixes."""
    digest = hashlib.sha256(screen).hexdigest()
    suffix = ".png" if screen.startswith(b"\x89PNG") else ".jpg"
    name = digest + suffix
    directory = state_dir / OBSERVATIONS
    directory.mkdir(exist_ok=True)
    path = directory / name
    if not path.exists():
        temporary = directory / (uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(screen)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"file": name, "sha256": digest}


def export_recording(state_dir: Path, trace_path: Path, output: Path) -> dict:
    """Export the selected branch and its images, without emulator snapshots."""
    trace = copy.deepcopy(json.loads(trace_path.read_text()))
    references = {}
    counts = {"before": 0, "after": 0}
    for item in trace["actions"]:
        for phase in counts:
            reference = item.get(phase + "_observation")
            if reference is None:
                continue
            name, digest = reference["file"], reference["sha256"]
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[a-f0-9]{64}\.(png|jpg)", name)
                or name.rsplit(".", 1)[0] != digest
            ):
                raise ValueError("Invalid observation reference")
            source = state_dir / OBSERVATIONS / name
            if (
                source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != digest
            ):
                raise ValueError(f"Observation checksum mismatch: {name}")
            references[name] = source
            counts[phase] += 1
    # Validate all references before creating a bundle; never overwrite evidence.
    output.mkdir(parents=True, exist_ok=False)
    images = output / "observations"
    images.mkdir()
    for name, source in references.items():
        shutil.copy2(source, images / name)
    trace["observation_root"] = "observations"
    report = {
        "actions": len(trace["actions"]),
        "actions_with_before_observation": counts["before"],
        "actions_with_after_observation": counts["after"],
        "unique_images": len(references),
        "fresh_replay_validated": False,
        "purpose": "Human demonstration for developing an observation-conditioned oracle",
    }
    (output / "actions.json").write_text(json.dumps(trace, indent=2) + "\n")
    (output / "recording.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def archive_attempt(state_dir: Path) -> Path:
    name = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    archive = state_dir / "attempts" / name
    archive.mkdir(parents=True)
    for item in state_dir.iterdir():
        if item.name not in {
            "attempts",
            ".boot-cache",
            ".snapshots",
            ".dos-states",
            ".dos-execution",
            OBSERVATIONS,
        }:
            item.rename(archive / item.name)
    return archive


def rewind_prefix(trace: list, actions: int) -> list:
    if type(actions) is not int or not 1 <= actions <= len(trace):
        raise ValueError(f"Rewind actions must be between 1 and {len(trace)}")
    return [item["action"] for item in trace[:-actions]]


def timed_resume_plan(trace, identity):
    """Validate a saved DOS prefix before archiving or restarting its session."""
    if trace.get("identity") != identity or trace.get("mode") != "realtime":
        raise ValueError("Saved recording does not match this real-time task")
    actions = trace.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("Saved recording has no actions")
    previous = 0.0
    for item in actions:
        timestamp = item.get("control_elapsed_sec") if isinstance(item, dict) else None
        if (
            type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or timestamp < previous
            or not isinstance(item.get("action"), dict)
            or set(item["action"]) != {"action", "action_input"}
            or not isinstance(item["action"]["action_input"], str)
        ):
            raise ValueError(
                "Saved DOS recording requires ordered timed action records"
            )
        previous = timestamp
    return {"identity": identity, "kind": "timed-dos", "actions": actions}
