"""Verify an exported DOS execution oracle through a fresh upstream session."""

import argparse
import asyncio
import functools
import json
import math
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dos_snapshots import DEFAULT_STATE_MAX_BYTES, DosSnapshots
from execution_store import ExecutionStore, atomic_write, canonical
from session import Session


class Replay:
    def __init__(self, snapshots, manifest, base_url, max_chunk_bytes, slice_ms):
        self.snapshots = snapshots
        self.config = {
            "replay": {"chunks": manifest["chunks"], "base_url": base_url},
            "max_chunk_bytes": max_chunk_bytes,
            "max_pending_events": 1,
            "slice_ms": slice_ms,
            "hold_markers": False,
        }

    async def start(self):
        pass

    async def finish(self):
        result = await self.snapshots.command("exec-status", "")
        if result["error"] or not result["stopped"] or result["marker"] != "end":
            raise RuntimeError("Execution replay did not verify: " + json.dumps(result))
        return result


class ChunkHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *_args):
        pass


def validate_bundle(path, max_chunk_bytes, max_total_bytes):
    manifest = json.loads((path / "execution.json").read_text())
    if manifest.get("format") != 1:
        raise ValueError("Unsupported execution format")
    store = ExecutionStore(
        path, max_chunk_bytes=max_chunk_bytes, max_total_bytes=max_total_bytes
    )
    if store.chain(manifest["head"]) != manifest["chunks"]:
        raise ValueError("Execution manifest does not match its immutable chain")
    final = store.read(manifest["head"])["events"][-1]
    if final[:2] != [4, "end"]:
        raise ValueError("Execution bundle is not finalized")
    expected = manifest["metadata"]["verification"]
    if expected.get("marker") != "end" or any(
        expected.get(k) != v for k, v in final[2].items()
    ):
        raise ValueError("Manifest verification does not match its final event")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/opt/game"))
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--dos-assets-dir", type=Path, required=True)
    parser.add_argument("--timeout-sec", type=float, default=3600)
    parser.add_argument("--progress-sec", type=float, default=10)
    parser.add_argument("--max-chunk-bytes", type=int, default=16 * 1024**2)
    parser.add_argument("--max-total-bytes", type=int, default=4 * 1024**3)
    parser.add_argument(
        "--dos-state-max-bytes", type=int, default=DEFAULT_STATE_MAX_BYTES
    )
    parser.add_argument("--slice-ms", type=float, default=10)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--live-observations-fps", type=float, default=10)
    args = parser.parse_args()
    if args.dos_state_max_bytes <= 0:
        parser.error("--dos-state-max-bytes must be positive")
    if (
        any(
            not math.isfinite(v) or v <= 0
            for v in (args.timeout_sec, args.progress_sec, args.slice_ms)
        )
        or not 0 < args.live_observations_fps <= 60
    ):
        parser.error("Timing limits must be positive")
    manifest = validate_bundle(
        args.recording, args.max_chunk_bytes, args.max_total_bytes
    )
    if args.state_dir.exists() and any(args.state_dir.iterdir()):
        parser.error("Use an empty --state-dir to preserve previous evidence")
    session = Session(args.root, args.state_dir)
    if session.manifest["identity"] != manifest["metadata"]["identity"]:
        parser.error("Recording belongs to a different task/settings")
    session.dos_headless = True
    session.manual_observation_fps = args.live_observations_fps
    session.dos_assets_dir = args.dos_assets_dir
    snapshots = DosSnapshots(
        session,
        max_bytes=args.dos_state_max_bytes,
        cache_size=1,
        timeout=120,
        settle_ms=0,
        chunk_bytes=4 * 1024**2,
    )
    session.dos_states = snapshots
    handler = functools.partial(ChunkHandler, directory=str(args.recording / "objects"))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    snapshots.execution = Replay(
        snapshots,
        manifest,
        f"http://127.0.0.1:{server.server_port}/",
        args.max_chunk_bytes,
        args.slice_ms,
    )
    result = {}
    started = time.monotonic()
    try:
        session.start()
        session.wait_ready(args.timeout_sec)
        deadline, next_progress = started + args.timeout_sec, 0
        while time.monotonic() < deadline:
            result = asyncio.run_coroutine_threadsafe(
                snapshots.command("exec-status", ""), snapshots.loop
            ).result(120)
            if time.monotonic() >= next_progress:
                print(
                    json.dumps(
                        {
                            **result,
                            "elapsed_sec": round(time.monotonic() - started, 1),
                            "chunks_total": len(manifest["chunks"]),
                        }
                    ),
                    flush=True,
                )
                next_progress = time.monotonic() + args.progress_sec
            if result["error"]:
                raise RuntimeError(result["error"])
            if result["stopped"]:
                if result["marker"] != "end":
                    raise RuntimeError("Replay stopped before final verification")
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("Execution replay timed out")
        frame = asyncio.run_coroutine_threadsafe(
            snapshots.browser.get_screenshot(), snapshots.loop
        ).result(120)
        (args.state_dir / "replay-final.jpg").write_bytes(frame)
        # Observe the verified state using the unmodified upstream action/grader path.
        session.submit({"action": None, "action_input": ""})
        session.wait_ready(120)
        result.update(
            verified=True,
            elapsed_sec=time.monotonic() - started,
            expected_state=manifest["metadata"]["verification"],
        )
        atomic_write(args.state_dir / "execution-result.json", canonical(result))
    finally:
        try:
            session.finish()
        finally:
            server.shutdown()
            server.server_close()
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
