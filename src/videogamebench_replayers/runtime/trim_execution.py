"""Reconstruct a separate DOS save without trailing idle execution.

The source recorder is never modified. Validate the generated execution bundle
with upstream_execution.py before importing the new slot into a live recorder.
"""

import argparse
import asyncio
import functools
import gzip
import json
import shutil
import struct
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from dos_snapshots import DEFAULT_STATE_MAX_BYTES, DosSnapshots, DosStateStore
from execution_store import ExecutionStore, atomic_write, canonical
from replay_execution import ChunkHandler, Replay
from session import Session

HUMAN_INPUTS = {"wc-add-key", "wc-mouse-move", "wc-mouse-button", "wc-mouse-sync"}


def select_prefix(store, head, tail_chunks):
    """Keep complete native turns, including a configurable tail after the input."""
    if tail_chunks < 0:
        raise ValueError("tail_chunks must be nonnegative")
    suffix = []
    while head is not None:
        chunk = store.read(head)
        if any(e[0] == 3 and e[1] in HUMAN_INPUTS for e in chunk["events"]):
            return (
                suffix[-min(tail_chunks, len(suffix))]
                if tail_chunks and suffix
                else head
            )
        suffix.append(head)
        head = chunk["parent"]
    raise ValueError("Recording contains no human inputs")


class PrefixReplay(Replay):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config["capture_prefix"] = True
        self.head = args[1]["head"]

    async def flush(self):
        pass  # The retained prefix is already durable and immutable.

    async def finish(self):
        return await self.snapshots.command("exec-status", "")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-state-dir", type=Path, required=True)
    parser.add_argument("--source-slot", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-slot", default="trimmed_last_input")
    parser.add_argument("--root", type=Path, default=Path("/opt/game"))
    parser.add_argument("--dos-assets-dir", type=Path, required=True)
    parser.add_argument(
        "--tail-chunks",
        type=int,
        default=1,
        help="Retain this many drain chunks after the last input's chunk",
    )
    parser.add_argument("--timeout-sec", type=float, default=7200)
    parser.add_argument("--progress-sec", type=float, default=30)
    parser.add_argument("--poll-sec", type=float, default=1)
    parser.add_argument("--slice-ms", type=float, default=50)
    parser.add_argument("--live-observations-fps", type=float, default=2)
    parser.add_argument(
        "--dos-state-max-bytes", type=int, default=DEFAULT_STATE_MAX_BYTES
    )
    parser.add_argument("--dos-state-timeout-sec", type=float, default=120)
    parser.add_argument("--dos-state-chunk-bytes", type=int, default=4 * 1024**2)
    parser.add_argument("--max-chunk-bytes", type=int, default=16 * 1024**2)
    parser.add_argument("--max-total-bytes", type=int, default=4 * 1024**3)
    args = parser.parse_args()
    import math

    if (
        args.tail_chunks < 0
        or any(
            not math.isfinite(v) or v <= 0
            for v in (
                args.timeout_sec,
                args.progress_sec,
                args.poll_sec,
                args.slice_ms,
                args.live_observations_fps,
                args.dos_state_timeout_sec,
                args.dos_state_max_bytes,
                args.dos_state_chunk_bytes,
            )
        )
        or args.dos_state_chunk_bytes > args.dos_state_max_bytes
    ):
        parser.error(
            "Limits must be finite and positive; tail chunks must be nonnegative"
        )
    DosStateStore.validate_slot(args.output_slot)
    task = json.loads((args.root / "manifest.json").read_text())
    source = DosStateStore(
        args.source_state_dir / ".dos-states",
        task["identity"],
        args.dos_state_max_bytes,
    )
    directory, metadata = source.read(args.source_slot)
    if not metadata.get("execution_head"):
        parser.error("The saved state has no execution recording")
    log_options = {
        "max_chunk_bytes": args.max_chunk_bytes,
        "max_total_bytes": args.max_total_bytes,
    }
    source_log = ExecutionStore(args.source_state_dir / ".dos-execution", **log_options)
    head = select_prefix(source_log, metadata["execution_head"], args.tail_chunks)
    identifiers = source_log.chain(head)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    shutil.copytree(directory, args.output_dir / "original-state")
    log = ExecutionStore(args.output_dir / "state/.dos-execution", **log_options)
    for identifier in identifiers:
        shutil.copyfile(source_log.path(identifier), log.path(identifier))
    # Reopen to account for copied objects in the storage limit.
    log = ExecutionStore(log.directory, **log_options)
    with gzip.open(directory / "state.bin.gz", "rb") as snapshot:
        if snapshot.read(8) != b"VGBDOS01":
            raise ValueError("Invalid snapshot header")
        length = struct.unpack("<I", snapshot.read(4))[0]
        if not 0 < length <= min(args.dos_state_max_bytes, 2 * 1024**2):
            raise ValueError("Invalid snapshot header length")
        original_clock = json.loads(snapshot.read(length))["date"]
    session = Session(args.root, args.output_dir / "state")
    session.dos_headless = True
    session.dos_assets_dir = args.dos_assets_dir
    session.manual_observation_fps = args.live_observations_fps
    snapshots = DosSnapshots(
        session,
        max_bytes=args.dos_state_max_bytes,
        cache_size=1,
        timeout=args.dos_state_timeout_sec,
        settle_ms=0,
        chunk_bytes=args.dos_state_chunk_bytes,
    )
    session.dos_states = snapshots
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(ChunkHandler, directory=str(log.objects))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manifest = {"head": head, "chunks": identifiers}
    snapshots.execution = PrefixReplay(
        snapshots,
        manifest,
        f"http://127.0.0.1:{server.server_port}/",
        args.max_chunk_bytes,
        args.slice_ms,
    )

    def call(coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, snapshots.loop).result(
            args.dos_state_timeout_sec * 3
        )

    started, next_progress = time.monotonic(), 0
    try:
        session.start()
        session.wait_ready(args.timeout_sec)
        while time.monotonic() - started < args.timeout_sec:
            status = call(snapshots.command("exec-status", ""))
            if time.monotonic() >= next_progress:
                print(
                    json.dumps(
                        {
                            **status,
                            "chunks_total": len(identifiers),
                            "elapsed_sec": time.monotonic() - started,
                        }
                    ),
                    flush=True,
                )
                next_progress = time.monotonic() + args.progress_sec
            if status["error"]:
                raise RuntimeError(status["error"])
            if status["stopped"]:
                break
            time.sleep(args.poll_sec)
        else:
            raise TimeoutError("Prefix reconstruction timed out")
        if status["marker"] != "prefix":
            raise RuntimeError("Replay stopped before the selected boundary")
        removed = (original_clock - status["clock"]["date"]) / 1000
        if removed < 0:
            raise ValueError("Reconstructed clock follows the original snapshot")
        metadata["elapsed_sec"] -= removed
        metadata["control_elapsed_sec"] -= removed
        metadata["front"]["elapsed_ms"] -= removed * 1000
        session.validate_dos_state(metadata)
        session.apply_dos_state(metadata)
        now = time.monotonic()
        session.control_started_at = now - metadata["control_elapsed_sec"]
        session.started_at = now - metadata["elapsed_sec"]
        snapshots.browser.current_mouse_position = tuple(metadata["mouse"])
        call(
            snapshots.browser.page.evaluate(
                "state=>vgbStates.setFront(state)", metadata["front"]
            )
        )
        result = snapshots.run("save", args.output_slot)
        fingerprint = call(snapshots.command("exec-fingerprint", ""))
        final = log.put({"parent": head, "events": [[4, "end", fingerprint]]})
        log.export(
            final,
            args.output_dir / "execution",
            {
                "identity": task["identity"],
                "game": task["game"],
                "verification": {"marker": "end", **fingerprint},
            },
        )
        result.update(
            removed_idle_sec=removed,
            source_slot=args.source_slot,
            prefix_head=head,
            verified=False,
            next_step="Verify execution/ with upstream_execution.py before importing the save",
        )
        atomic_write(args.output_dir / "result.json", canonical(result))
        print(json.dumps(result, indent=2), flush=True)
    finally:
        try:
            session.finish()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
