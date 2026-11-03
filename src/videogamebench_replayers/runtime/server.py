"""One isolated upstream session per task, with optional human recorder controls."""

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from recorder import (
    archive_attempt,
    rewind_prefix,
    timed_resume_plan,
)
from session import Session

LOCK = threading.Lock()
INSTANCE = uuid.uuid4().hex
SCRIPT = str(Path(__file__).resolve())
RESTORING = False
RESTORE_PROGRESS = 0
RESTORE_TOTAL = 0
RESTORE_ERROR = None
SNAPSHOT_STEP = 0


def status():
    return {
        **SESSION.status(),
        "restoring": RESTORING,
        "restore_progress": RESTORE_PROGRESS,
        "restore_total": RESTORE_TOTAL,
        "restore_error": RESTORE_ERROR,
        "snapshot_step": SNAPSHOT_STEP,
        "execution_recording": bool(
            getattr(getattr(SESSION, "dos_states", None), "execution", None)
        ),
    }


def restore(plan):
    global RESTORING, RESTORE_PROGRESS, RESTORE_ERROR, SNAPSHOT_STEP
    from PIL import Image

    started = time.monotonic()
    try:
        if plan["identity"] != SESSION.manifest["identity"]:
            raise ValueError("Rewind settings do not match this task")
        SESSION.start()
        SESSION.wait_ready()
        if plan.get("kind") == "dos-state":
            SESSION.dos_states.run("restore", plan["slot"])
            RESTORE_PROGRESS = SESSION.steps
            return
        timed_dos = plan.get("kind") == "timed-dos"
        SNAPSHOT_STEP = 0 if timed_dos else SESSION.restore_snapshot(plan["actions"])
        RESTORE_PROGRESS = SNAPSHOT_STEP
        if not timed_dos:
            SESSION.set_playback_speed(plan["speed"])
        with (SESSION.state_dir / "restored-screen-hashes.jsonl").open("w") as output:
            for index, action in enumerate(
                plan["actions"][SNAPSHOT_STEP:], SNAPSHOT_STEP + 1
            ):
                if timed_dos:
                    SESSION.submit(
                        action["action"], at_control_sec=action["control_elapsed_sec"]
                    )
                else:
                    SESSION.submit(action)
                SESSION.wait_ready()
                if SESSION.finished:
                    raise RuntimeError("Session ended during rewind")
                with Image.open(io.BytesIO(SESSION.screen)) as image:
                    digest = hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()
                output.write(json.dumps({"step": index, "pixel_sha256": digest}) + "\n")
                RESTORE_PROGRESS = index
        if not timed_dos:
            SESSION.set_playback_speed(1)
        # Persist the reconstructed prefix immediately, even before more play.
        SESSION.save("recording")
        (SESSION.state_dir / "restore-metrics.json").write_text(
            json.dumps(
                {
                    "snapshot_step": SNAPSHOT_STEP,
                    "execution_recording": bool(
                        getattr(getattr(SESSION, "dos_states", None), "execution", None)
                    ),
                    "target_step": SESSION.steps,
                    "replayed_actions": SESSION.steps - SNAPSHOT_STEP,
                    "restore_seconds": time.monotonic() - started,
                    "kind": plan.get("kind", "gb-rewind"),
                },
                indent=2,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- surface worker failures to the recorder UI
        RESTORE_ERROR = str(exc)
    finally:
        RESTORING = False


def restart():
    """A new interpreter avoids retaining upstream monkeypatches or emulator state."""
    os.chdir(ARGS.root)
    if WORKSPACE is not None:
        WORKSPACE.cleanup()
    os.execv(sys.executable, [sys.executable, SCRIPT, *SERVER_ARGS])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, data, content_type="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self.send(
                Path(__file__)
                .with_name(
                    "player-dos.html"
                    if SESSION.manifest["emulator"] == "dos"
                    else "player.html"
                )
                .read_bytes(),
                "text/html; charset=utf-8",
            )
        elif self.path == "/controls":
            self.send(
                json.dumps(
                    {
                        "game": SESSION.manifest["game"],
                        "emulator": SESSION.manifest["emulator"],
                        "mode": SESSION.settings["mode"],
                        "action_frames": SESSION.settings["action_frames"],
                        "allow_restart": ARGS.allow_restart,
                        "can_save_recording": ARGS.allow_restart,
                        "dos_save_states": SESSION.dos_states is not None,
                        "execution_recording": bool(
                            getattr(SESSION.dos_states, "execution", None)
                        ),
                        "dos_state_slots": list(SESSION.dos_states.store.slots())
                        if SESSION.dos_states
                        else [],
                        "instance": INSTANCE,
                        "restart_timeout_sec": ARGS.restart_timeout_sec,
                        "replay_speed": ARGS.replay_speed,
                        "snapshot_interval": ARGS.snapshot_interval,
                        "live_observations_fps": SESSION.manual_observation_fps,
                        "can_resume": (
                            ARGS.allow_restart
                            and SESSION.manifest["emulator"] == "dos"
                            and SESSION.settings["mode"] == "realtime"
                            and (SESSION.state_dir / "actions.json").is_file()
                        ),
                    }
                ).encode()
            )
        elif self.path in {"/health", "/status"}:
            self.send(json.dumps(status()).encode())
        elif self.path == "/pointer-bounds":
            try:
                with LOCK:
                    if not ARGS.allow_restart or RESTORING:
                        raise ValueError(
                            "Pointer bounds require an idle human recorder"
                        )
                    bounds = SESSION.pointer_bounds(ARGS.observation_timeout_sec)
                    self.send(json.dumps(bounds).encode())
            except (OSError, RuntimeError, ValueError) as exc:
                self.send_error(503, str(exc))
        elif self.path in {"/screen", "/screen?fresh=1"}:
            try:
                with LOCK:
                    if RESTORING:
                        raise ValueError("Reconstructing the recorded game")
                    if not SESSION.started:
                        SESSION.start()
                    if SESSION.controller == "manual":
                        SESSION.wait_ready()
                    if self.path == "/screen?fresh=1":
                        SESSION.observe_fresh(ARGS.observation_timeout_sec)
                    if SESSION.screen is None:
                        raise ValueError("No screen yet")
                    self.send(
                        SESSION.screen,
                        "image/png"
                        if SESSION.manifest["emulator"] == "gba"
                        else "image/jpeg",
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                self.send_error(503, str(exc))
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(length))
            # A scheduled input can wait for a long idle interval while holding
            # LOCK. Wake its wait before acquiring LOCK so /finish can cancel it.
            if self.path == "/finish" and not RESTORING and data == {}:
                SESSION.stop_requested.set()
            with LOCK:
                if RESTORING:
                    self.send_error(409, "Wait for rewind to finish")
                    return
                if self.path in {"/save-state", "/restore-state", "/delete-state"}:
                    if (
                        not ARGS.allow_restart
                        or SESSION.dos_states is None
                        or SESSION.controller not in {None, "manual"}
                    ):
                        self.send_error(403, "DOS save states are not enabled")
                        return
                    if not isinstance(data, dict) or data.keys() - {"slot"}:
                        raise ValueError("Expected an optional state slot")
                    slot = data.get("slot", "latest")
                    SESSION.dos_states.store.validate_slot(slot)
                    if self.path == "/delete-state":
                        if "slot" not in data:
                            raise ValueError("Select a saved state to delete")
                        SESSION.dos_states.store.delete(slot)
                        self.send(
                            json.dumps({**status(), "deleted_slot": slot}).encode()
                        )
                        return
                    if self.path == "/restore-state" and (
                        not SESSION.started
                        or SESSION.finished
                        or SESSION.dos_states.needs_restart(slot)
                    ):
                        _, metadata = SESSION.dos_states.store.read(slot)
                        SESSION.validate_dos_state(metadata)
                        SESSION.finish()
                        saved = archive_attempt(SESSION.state_dir)
                        (SESSION.state_dir / "restore-plan.json").write_text(
                            json.dumps(
                                {
                                    "identity": SESSION.manifest["identity"],
                                    "kind": "dos-state",
                                    "slot": slot,
                                    "actions": metadata["trace"],
                                }
                            )
                        )
                        self.send(
                            json.dumps(
                                {"saved_attempt": saved.name, "restarting": True}
                            ).encode()
                        )
                        self.wfile.flush()
                        restart()
                        return
                    operation = "save" if self.path == "/save-state" else "restore"
                    result = SESSION.dos_states.run(operation, slot)
                    self.send(json.dumps({**status(), **result}).encode())
                    return
                if self.path in {"/restart", "/rewind", "/resume"}:
                    if (
                        not ARGS.allow_restart
                        or SESSION.controller not in {None, "manual"}
                        or (
                            SESSION.manifest["emulator"] != "dos"
                            and SESSION.settings["mode"] != "lite"
                        )
                        or (
                            self.path == "/rewind"
                            and SESSION.manifest["emulator"] != "gba"
                        )
                    ):
                        self.send_error(
                            403,
                            "Restart requires an enabled human recorder",
                        )
                        return
                    plan = None
                    if self.path == "/resume":
                        if (
                            SESSION.manifest["emulator"] != "dos"
                            or SESSION.settings["mode"] != "realtime"
                            or data != {}
                        ):
                            raise ValueError(
                                "Continue requires a real-time DOS recorder"
                            )
                        if SESSION.started and not SESSION.finished:
                            raise ValueError(
                                "Stop & save before continuing a recording"
                            )
                        if getattr(
                            getattr(SESSION, "dos_states", None), "execution", None
                        ):
                            _, saved_state = SESSION.dos_states.store.read(
                                ARGS.execution_resume_slot
                            )
                            SESSION.validate_dos_state(saved_state)
                            plan = {
                                "identity": SESSION.manifest["identity"],
                                "kind": "dos-state",
                                "slot": ARGS.execution_resume_slot,
                                "actions": saved_state["trace"],
                            }
                        else:
                            plan = timed_resume_plan(
                                json.loads(
                                    (SESSION.state_dir / "actions.json").read_text()
                                ),
                                SESSION.manifest["identity"],
                            )
                        if len(plan["actions"]) >= SESSION.settings["max_steps"]:
                            raise ValueError(
                                "Recording has exhausted this task's action budget"
                            )
                    if self.path == "/rewind":
                        if not isinstance(data, dict) or data.keys() - {
                            "actions",
                            "speed",
                        }:
                            raise ValueError(
                                "Expected rewind actions and optional speed"
                            )
                        prefix = rewind_prefix(SESSION.trace, data.get("actions"))
                        speed = data.get("speed", ARGS.replay_speed)
                        if type(speed) is not int or speed < 0:
                            raise ValueError(
                                "Replay speed must be a nonnegative integer; 0 is unlimited"
                            )
                        plan = {
                            "actions": prefix,
                            "speed": speed,
                            "identity": SESSION.manifest["identity"],
                        }
                    if self.path != "/resume" or SESSION.started:
                        SESSION.finish()
                    saved = archive_attempt(SESSION.state_dir)
                    if plan is not None:
                        plan["source_attempt"] = saved.name
                        (SESSION.state_dir / "restore-plan.json").write_text(
                            json.dumps(plan)
                        )
                    self.send(json.dumps({"saved_attempt": saved.name}).encode())
                    self.wfile.flush()
                    restart()
                    return
                elif self.path == "/start":
                    if not isinstance(data, dict) or data.keys() - {
                        "controller",
                        "api_key",
                        "model",
                    }:
                        raise ValueError("Invalid start request")
                    SESSION.start(**data)
                elif self.path == "/action":
                    SESSION.submit(data)
                    SESSION.wait_ready()
                elif self.path == "/scheduled-action":
                    if (
                        not isinstance(data, dict)
                        or not {"action", "control_elapsed_sec"} <= data.keys()
                        or data.keys()
                        - {"action", "control_elapsed_sec", "max_lateness_sec"}
                        or data["control_elapsed_sec"] is None
                    ):
                        raise ValueError("Expected an action and live-clock deadline")
                    SESSION.submit(
                        data["action"],
                        at_control_sec=data["control_elapsed_sec"],
                        max_lateness_sec=data.get("max_lateness_sec"),
                    )
                    SESSION.wait_ready()
                elif self.path == "/save":
                    if not ARGS.allow_restart or SESSION.controller != "manual":
                        self.send_error(403, "Save requires an enabled human recorder")
                        return
                    if data != {}:
                        raise ValueError("Expected an empty save request")
                    if not SESSION.started or SESSION.finished:
                        raise ValueError("Save requires a running recording")
                    SESSION.wait_ready()
                    # Anchor elapsed idle time with the benchmark's no-op input.
                    # This is a recording checkpoint, not an emulator snapshot.
                    SESSION.submit(
                        {"action": None, "action_input": ""}
                        if SESSION.manifest["emulator"] == "dos"
                        else None
                    )
                    SESSION.wait_ready()
                    if SESSION.finished:
                        raise RuntimeError("Session ended while saving")
                    SESSION.save("recording")
                    if getattr(getattr(SESSION, "dos_states", None), "execution", None):
                        SESSION.dos_states.run("save", ARGS.execution_resume_slot)
                elif self.path == "/finish":
                    if (
                        getattr(getattr(SESSION, "dos_states", None), "execution", None)
                        and SESSION.started
                        and not SESSION.finished
                    ):
                        SESSION.dos_states.run("save", ARGS.execution_resume_slot)
                    SESSION.finish()
                else:
                    self.send_error(404)
                    return
            self.send(json.dumps(status()).encode())
        except (ValueError, TypeError) as exc:
            self.send_error(400, str(exc))
        except (OSError, RuntimeError) as exc:
            self.send_error(503, str(exc))


if __name__ == "__main__":
    from dos_snapshots import DEFAULT_STATE_MAX_BYTES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/opt/game"))
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-restart", action="store_true")
    parser.add_argument("--dos-headless", action="store_true")
    parser.add_argument("--dos-assets-dir", type=Path)
    parser.add_argument("--live-observations-fps", type=float, default=10)
    parser.add_argument("--observation-timeout-sec", type=float, default=900)
    parser.add_argument(
        "--record-action-observations",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Retain before/after action images for human DOS oracle development",
    )
    parser.add_argument(
        "--dos-save-states",
        action="store_true",
        help="Experimental human regular-DOSBox save states; requires pinned cached assets",
    )
    parser.add_argument(
        "--dos-execution-record",
        action="store_true",
        help="Record native events and clocks for a reproducible DOS oracle",
    )
    parser.add_argument(
        "--execution-max-chunk-bytes", type=int, default=16 * 1024 * 1024
    )
    parser.add_argument("--execution-resume-slot", default="recording")
    parser.add_argument("--execution-max-total-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--execution-max-pending-events", type=int, default=500000)
    parser.add_argument("--execution-flush-sec", type=float, default=1)
    parser.add_argument("--execution-slice-ms", type=float, default=10)
    parser.add_argument(
        "--dos-state-max-bytes", type=int, default=DEFAULT_STATE_MAX_BYTES
    )
    parser.add_argument("--dos-state-cache-size", type=int, default=2)
    parser.add_argument("--dos-state-timeout-sec", type=float, default=60)
    parser.add_argument("--dos-state-settle-ms", type=int, default=40)
    parser.add_argument("--dos-state-chunk-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument(
        "--boot-cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse the exact post-boot emulator state in the human recorder",
    )
    parser.add_argument(
        "--snapshot-interval",
        type=int,
        default=100,
        help="Cache a human Game Boy Lite state every N actions; 0 disables snapshots",
    )
    parser.add_argument("--restart-timeout-sec", type=float, default=60)
    parser.add_argument(
        "--replay-speed",
        type=int,
        default=0,
        help="Rewind playback multiplier; 0 removes the speed limit",
    )
    SERVER_ARGS = sys.argv[1:]  # Upstream replaces sys.argv while a game runs.
    ARGS = parser.parse_args()
    if ARGS.dos_execution_record and not ARGS.dos_save_states:
        parser.error("Execution recording requires --dos-save-states")
    if ARGS.dos_save_states and (not ARGS.allow_restart or ARGS.dos_assets_dir is None):
        parser.error("DOS save states require --allow-restart and --dos-assets-dir")
    if (
        ARGS.dos_state_max_bytes <= 0
        or ARGS.dos_state_cache_size <= 0
        or not math.isfinite(ARGS.dos_state_timeout_sec)
        or ARGS.dos_state_timeout_sec <= 0
        or ARGS.dos_state_settle_ms < 0
        or ARGS.dos_state_settle_ms >= ARGS.dos_state_timeout_sec * 1000
        or not 0 < ARGS.dos_state_chunk_bytes <= ARGS.dos_state_max_bytes
    ):
        parser.error("Invalid DOS save-state limits")
    if (
        not math.isfinite(ARGS.observation_timeout_sec)
        or ARGS.observation_timeout_sec <= 0
    ):
        parser.error("Observation timeout must be finite and positive")
    if ARGS.replay_speed < 0 or ARGS.restart_timeout_sec <= 0:
        parser.error("Replay speed must be nonnegative and restart timeout positive")
    if ARGS.snapshot_interval < 0:
        parser.error("Snapshot interval must be nonnegative")
    if (
        not math.isfinite(ARGS.live_observations_fps)
        or not 0 < ARGS.live_observations_fps <= 60
    ):
        parser.error("Live observation FPS must be in (0, 60]")
    ARGS.root, ARGS.state_dir = ARGS.root.resolve(), ARGS.state_dir.resolve()
    WORKSPACE = (
        tempfile.TemporaryDirectory(prefix="vgbench-recorder-")
        if ARGS.allow_restart
        else None
    )
    game_root = ARGS.root
    if WORKSPACE is not None:
        # PyBoy may persist battery RAM beside a ROM. Every human attempt gets
        # its own copy, leaving the generated task's initial assets untouched.
        game_root = Path(WORKSPACE.name) / "game"
        shutil.copytree(ARGS.root, game_root)
    SESSION = Session(game_root, ARGS.state_dir)
    if ARGS.dos_save_states:
        if (
            SESSION.manifest["emulator"] != "dos"
            or SESSION.settings["mode"] != "realtime"
        ):
            parser.error("DOS save states require a real-time DOS task")
        from dos_snapshots import DosSnapshots

        SESSION.dos_states = DosSnapshots(
            SESSION,
            max_bytes=ARGS.dos_state_max_bytes,
            cache_size=ARGS.dos_state_cache_size,
            timeout=ARGS.dos_state_timeout_sec,
            settle_ms=ARGS.dos_state_settle_ms,
            chunk_bytes=ARGS.dos_state_chunk_bytes,
        )
    if ARGS.dos_execution_record:
        from dos_execution import DosExecution

        SESSION.dos_states.store.validate_slot(ARGS.execution_resume_slot)

        SESSION.dos_states.execution = DosExecution(
            SESSION.dos_states,
            max_chunk_bytes=ARGS.execution_max_chunk_bytes,
            max_total_bytes=ARGS.execution_max_total_bytes,
            max_pending_events=ARGS.execution_max_pending_events,
            flush_seconds=ARGS.execution_flush_sec,
            slice_ms=ARGS.execution_slice_ms,
        )
    SESSION.dos_headless = ARGS.dos_headless
    SESSION.dos_assets_dir = (
        ARGS.dos_assets_dir.resolve() if ARGS.dos_assets_dir else None
    )
    if ARGS.allow_restart and SESSION.manifest["emulator"] == "dos":
        SESSION.manual_observation_fps = ARGS.live_observations_fps
        SESSION.record_action_observations = ARGS.record_action_observations
    if ARGS.allow_restart and ARGS.boot_cache:
        SESSION.boot_cache_dir = ARGS.state_dir / ".boot-cache"
    if (
        ARGS.allow_restart
        and ARGS.snapshot_interval
        and SESSION.manifest["emulator"] == "gba"
        and SESSION.settings["mode"] == "lite"
    ):
        from importlib.metadata import version

        from snapshots import SnapshotStore

        SESSION.snapshots = SnapshotStore(
            ARGS.state_dir / ".snapshots",
            SESSION.manifest["identity"],
            version("pyboy"),
            ARGS.snapshot_interval,
        )
    plan_file = ARGS.state_dir / "restore-plan.json"
    if ARGS.allow_restart and plan_file.exists():
        plan = json.loads(plan_file.read_text())
        RESTORE_TOTAL = len(plan["actions"])
        plan_file.rename(ARGS.state_dir / "restoration.json")
        RESTORING = True
        threading.Thread(target=restore, args=(plan,), daemon=True).start()
    ThreadingHTTPServer((ARGS.host, ARGS.port), Handler).serve_forever()
