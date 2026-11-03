"""Run an execution oracle with stock upstream main.py and an external game.html.

The local completion endpoint returns a no-op only after native replay verifies.
It never supplies screenshots, checkpoints, or rewards to the upstream grader.
"""

import argparse
import functools
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from dos_snapshots import DEFAULT_STATE_MAX_BYTES, RUNTIME, DosSnapshots
from execution_store import atomic_write, canonical
from replay_execution import Replay, validate_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--root", type=Path, default=Path("/opt/game"))
    parser.add_argument("--upstream", type=Path, default=Path("/opt/game/upstream"))
    parser.add_argument("--dos-assets-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-sec", type=float, default=3600)
    parser.add_argument("--progress-sec", type=float, default=10)
    parser.add_argument("--max-chunk-bytes", type=int, default=16 * 1024**2)
    parser.add_argument("--max-total-bytes", type=int, default=4 * 1024**3)
    parser.add_argument(
        "--dos-state-max-bytes", type=int, default=DEFAULT_STATE_MAX_BYTES
    )
    parser.add_argument("--slice-ms", type=float, default=10)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--game-port", type=int, default=8000)
    parser.add_argument("--model", default="gpt-4o-2024-08-06")
    parser.add_argument(
        "--virtual-display", action=argparse.BooleanOptionalAction, default=True
    )
    args = parser.parse_args()
    if args.dos_state_max_bytes <= 0:
        parser.error("--dos-state-max-bytes must be positive")
    if any(
        not math.isfinite(v) or v <= 0
        for v in (args.timeout_sec, args.progress_sec, args.slice_ms)
    ):
        parser.error("Timing limits must be finite and positive")
    manifest = validate_bundle(
        args.recording, args.max_chunk_bytes, args.max_total_bytes
    )
    task = json.loads((args.root / "manifest.json").read_text())
    if task["identity"] != manifest["metadata"]["identity"]:
        parser.error("Recording belongs to a different task/settings")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    root = args.output_dir.resolve()
    public = root / "public"
    public.mkdir()
    status, ready = {}, threading.Event()

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type,Authorization"
            )
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 32 * 1024**2:
                self.send_error(413)
                return
            request = json.loads(self.rfile.read(length))
            if self.path == "/trace-status":
                status.update(request)
                if status.get("error") or (
                    status.get("stopped") and status.get("marker") == "end"
                ):
                    ready.set()
                response = {}
            elif self.path == "/v1/chat/completions":
                if not ready.wait(args.timeout_sec) or status.get("error"):
                    self.send_error(503, "Execution oracle did not verify")
                    return
                response = {
                    "id": "execution-oracle",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": args.model,
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "thought": "Recorded execution verified; observe the game.",
                                        "action": None,
                                        "action_input": "",
                                    }
                                ),
                            },
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            else:
                self.send_error(404)
                return
            body = canonical(response)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), functools.partial(Handler, directory=str(public))
    )
    base = f"http://127.0.0.1:{server.server_port}/"
    snapshots = DosSnapshots(
        SimpleNamespace(state_dir=root, manifest=task),
        max_bytes=args.dos_state_max_bytes,
        cache_size=1,
        timeout=120,
        settle_ms=0,
        chunk_bytes=4 * 1024**2,
    )
    snapshots.execution = Replay(
        snapshots,
        manifest,
        base + "execution/objects/",
        args.max_chunk_bytes,
        args.slice_ms,
    )
    assets = {}
    for path in args.dos_assets_dir.glob("*.json"):
        metadata = json.loads(path.read_text())
        data = path.with_suffix(".bin").read_bytes()
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise ValueError("Cached browser asset checksum mismatch")
        assets[metadata["url"]] = (metadata, data)
    snapshots.patch_assets(assets)
    for url, (_metadata, data) in assets.items():
        name = urlparse(url).path.removeprefix("/latest/")
        destination = public / "assets" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.suffix in {".js", ".css"}:
            data = data.replace(
                b"https://v8.js-dos.com/latest/", (base + "assets/").encode()
            )
        destination.write_bytes(data)
    shutil.copytree(args.recording, public / "execution")
    shutil.copyfile(args.root / "bundle.jsdos", public / "bundle.jsdos")
    # Upstream writes logs relative to CWD. Use an unchanged source copy so the
    # user's checkout stays untouched and all run artifacts live under output.
    upstream = root / "upstream"
    shutil.copytree(
        args.upstream,
        upstream,
        ignore=shutil.ignore_patterns(".git", "logs", "__pycache__", ".venv"),
    )
    game = task["game"]
    config = root / "config" / game
    shutil.copytree(upstream / "configs" / game, config)
    html = (config / "game.html").read_text()
    html = html.replace("https://v8.js-dos.com/latest/", base + "assets/")
    html = re.sub(
        r'url:\s*"[^"]+"', "url: " + json.dumps(base + "bundle.jsdos"), html, count=1
    )
    bridge = (
        "const VGB_STATE_CONFIG="
        + json.dumps(snapshots.config)
        + ";\n"
        + (RUNTIME / "dos-state-bridge.js").read_text()
    )
    bridge += """\nconst stockBind=vgbStates.bind;
vgbStates.bind=ci=>{stockBind(ci);const timer=setInterval(async()=>{
 try {const state=await vgbStates.command('exec-status','');
 await fetch(VGB_STATUS_URL,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(state)});
 if(state.stopped)clearInterval(timer);
 }catch(error){console.error(error);}
},500);};
""".replace("VGB_STATUS_URL", json.dumps(base + "trace-status"))
    (public / "bridge.js").write_text(bridge)
    html = html.replace(
        "<head>", '<head><script src="' + base + 'bridge.js"></script>', 1
    )
    (config / "game.html").write_text(html)
    command = [
        sys.executable,
        "main.py",
        "--emulator",
        "dos",
        "--game",
        game,
        "--config-folder",
        str(config.parent),
        "--max-steps",
        "1",
        "--model",
        args.model,
        "--api-key",
        "local-execution-oracle",
        "--port",
        str(args.game_port),
    ]
    if args.virtual_display:
        command = ["xvfb-run", "-a", *command]
    env = {**os.environ, "OPENAI_API_BASE": base + "v1", "OPENAI_BASE_URL": base + "v1"}
    atomic_write(
        root / "command.json",
        canonical({"command": command, "cwd": str(upstream), "api_base": base + "v1"}),
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started, next_progress = time.monotonic(), 0
    try:
        with (root / "upstream.log").open("w") as log:
            process = subprocess.Popen(
                command,
                cwd=upstream,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                while process.poll() is None:
                    if time.monotonic() - started > args.timeout_sec:
                        raise TimeoutError("Upstream execution replay timed out")
                    if status.get("error"):
                        raise RuntimeError(status["error"])
                    if time.monotonic() >= next_progress:
                        print(
                            json.dumps(
                                {
                                    **status,
                                    "elapsed_sec": round(time.monotonic() - started, 1),
                                    "chunks_total": len(manifest["chunks"]),
                                }
                            ),
                            flush=True,
                        )
                        next_progress = time.monotonic() + args.progress_sec
                    time.sleep(0.2)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        if process.returncode or status.get("error") or status.get("marker") != "end":
            raise RuntimeError("Upstream run did not verify; see upstream.log")
        result = {
            "verified": True,
            "upstream_exit_code": process.returncode,
            "elapsed_sec": time.monotonic() - started,
            "state": manifest["metadata"]["verification"],
            "runtime": "stock upstream Python; external recorded-timing frontend",
        }
        atomic_write(root / "execution-result.json", canonical(result))
        print(json.dumps(result, indent=2), flush=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
