"""Optional human DOS save states for a checksum-pinned JS-DOS runtime."""

import asyncio
import gzip
import hashlib
import json
import re
import secrets
import shutil
import struct
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

GLUE_SHA256 = "a0806cc99e0c66bc594e7efd78eb22d675af7ebb0a4de543697cbe5cd7440a2e"
WASM_SHA256 = "b33afba80de1a420c123086e5952d135de041f9e85d4444170e29d9249ec049c"
FRONTEND_SHA256 = "3e4da1cc59fa89c42182d3df5b7e738c6207090c3c0aec115d67d047353b826c"
RUNTIME = Path(__file__).parent
DEFAULT_STATE_MAX_BYTES = 1024**3


def host_monotonic(epoch_ms):
    """Map the worker's host clock to Python's monotonic clock on the same host."""
    return time.monotonic() + (epoch_ms / 1000 - time.time())


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2))
    temporary.replace(path)


class DosStateStore:
    def __init__(self, directory, identity, max_bytes):
        self.directory, self.identity, self.max_bytes = directory, identity, max_bytes
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_slot(slot):
        if not isinstance(slot, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", slot):
            raise ValueError(
                "State slot must contain 1–64 letters, digits, underscores or dashes"
            )
        return slot

    def slots(self):
        path = self.directory / "index.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def read(self, slot):
        self.validate_slot(slot)
        identifier = self.slots().get(slot)
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[a-f0-9]{32}", identifier
        ):
            raise ValueError("No saved state in this slot")
        directory = self.directory / identifier
        metadata = json.loads((directory / "session.json").read_text())
        state = directory / "state.bin.gz"
        if (
            metadata.get("format") != 1
            or metadata.get("identity") != self.identity
            or metadata.get("wasm_sha256") != WASM_SHA256
            or metadata.get("glue_sha256") != GLUE_SHA256
            or metadata.get("id") != identifier
        ):
            raise ValueError("Saved state does not match this task/runtime")
        if not 0 < state.stat().st_size <= self.max_bytes:
            raise ValueError("Saved state exceeds configured size limit")
        if digest(state) != metadata.get("state_sha256"):
            raise ValueError("Saved state checksum mismatch")
        return directory, metadata

    def commit(self, slot, pending, metadata):
        self.validate_slot(slot)
        metadata.update(
            format=1,
            identity=self.identity,
            wasm_sha256=WASM_SHA256,
            glue_sha256=GLUE_SHA256,
            state_sha256=digest(pending / "state.bin.gz"),
        )
        atomic_json(pending / "session.json", metadata)
        pending.rename(self.directory / metadata["id"])
        slots = self.slots()
        slots[slot] = metadata["id"]
        atomic_json(self.directory / "index.json", slots)

    def delete(self, slot):
        self.validate_slot(slot)
        slots = self.slots()
        identifier = slots.get(slot)
        if not isinstance(identifier, str) or not re.fullmatch(
            r"[a-f0-9]{32}", identifier
        ):
            raise ValueError("No saved state in this slot")
        del slots[slot]
        # Remove the slot atomically before deleting its immutable data. A crash
        # can leave an unreferenced directory, but never a dangling restore entry.
        atomic_json(self.directory / "index.json", slots)
        directory = self.directory / identifier
        if identifier not in slots.values() and directory.exists():
            shutil.rmtree(directory)


class DosSnapshots:
    def __init__(
        self, session, *, max_bytes, cache_size, timeout, settle_ms, chunk_bytes
    ):
        self.session, self.timeout = session, timeout
        self.store = DosStateStore(
            session.state_dir / ".dos-states", session.manifest["identity"], max_bytes
        )
        self.config = {
            "identity": session.manifest["identity"],
            "wasm_sha256": WASM_SHA256,
            "max_bytes": max_bytes,
            "cache_size": cache_size,
            "timeout_ms": timeout * 1000,
            "settle_ms": settle_ms,
            "chunk_bytes": chunk_bytes,
        }
        self.browser = self.loop = None
        self.execution = None

    def patch_assets(self, assets):
        found = set()
        for url, (metadata, data) in list(assets.items()):
            name = url.rsplit("/", 1)[-1]
            expected = {
                "wdosbox.js": GLUE_SHA256,
                "wdosbox.wasm": WASM_SHA256,
                "js-dos.js": FRONTEND_SHA256,
            }.get(name)
            if expected is None:
                continue
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError(f"Unsupported save-state runtime: {name}")
            found.add(name)
            if name == "wdosbox.js":
                script = data.decode()
                if self.execution:
                    from dos_execution import patch_execution

                    script = patch_execution(script, self.execution.config)
                replacements = {
                    "function messageHandler(e){": (
                        "const VGB_STATE_CONFIG="
                        + json.dumps(self.config)
                        + ";\n"
                        + (RUNTIME / "dos-state-worker.js").read_text()
                        + "\nfunction messageHandler(e){"
                    ),
                    "function processMessage(data){": (
                        "function processMessage(data){if(data.name==='wc-vgb-state' && !data.props.operation.startsWith('exec-')){"
                        "Module.vgbStateCommand(data.props).then(result=>postMessage({"
                        "vgbState:data.props.request,result},result.bytes?[result.bytes.buffer]:[]),"
                        "error=>postMessage({vgbState:data.props.request,error:String(error)}));return;}"
                    ),
                    "function emsc_end_frame_update(frameRgb,frameWidth,frameHeight){": (
                        "function emsc_end_frame_update(frameRgb,frameWidth,frameHeight){"
                        "Module.snapshotFrame=[frameRgb,frameWidth,frameHeight];"
                    ),
                    "function emsc_ws_client_sound_init(freq){": (
                        "function emsc_ws_client_sound_init(freq){Module.snapshotAudioRate=freq;"
                    ),
                }
                for marker, replacement in replacements.items():
                    if script.count(marker) != 1:
                        raise ValueError("Unsupported DOSBox glue layout")
                    script = script.replace(marker, replacement)
                data = script.encode()
            elif name == "js-dos.js":
                data += b"\n;window.Dos=((original)=>function(element,options={}){return original(element,{...options,onEvent:(event,ci,...args)=>{if(event==='ci-ready')window.vgbStates.bind(ci);if(options.onEvent)return options.onEvent(event,ci,...args);}});})(window.Dos);\n"
            assets[url] = (metadata, data)
        if found != {"wdosbox.js", "wdosbox.wasm", "js-dos.js"}:
            raise ValueError(
                "DOS save states require the pinned JS-DOS assets in --dos-assets-dir"
            )

    async def attach(self, browser):
        self.browser, self.loop = browser, asyncio.get_running_loop()
        await browser.page.add_init_script(
            script="const VGB_STATE_CONFIG="
            + json.dumps(self.config)
            + ";\n"
            + (RUNTIME / "dos-state-bridge.js").read_text()
        )

        if self.execution:
            await browser.page.expose_function(
                "vgbExecutionStart", self.execution.start
            )
            await browser.page.add_init_script(
                "const executionBind=vgbStates.bind; vgbStates.bind=value=>{executionBind(value);window.vgbExecutionStart();};"
            )
            original_close = browser.close

            async def close():
                try:
                    await self.execution.finish()
                finally:
                    await original_close()

            browser.close = close

    async def command(self, operation, slot):
        return await self.browser.page.evaluate(
            "([op, slot]) => vgbStates.command(op, slot)", [operation, slot]
        )

    @contextmanager
    def transfer(self, path, *, upload):
        token = secrets.token_urlsafe(24)
        limit, chunk_limit = self.config["max_bytes"], self.config["chunk_bytes"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if upload or self.path != "/" + token:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)

            def do_POST(self):
                try:
                    prefix = "/" + token + "/"
                    if not upload or not self.path.startswith(prefix):
                        self.send_error(404)
                        return
                    offset = int(self.path[len(prefix) :])
                    length = int(self.headers.get("Content-Length", "0"))
                    existing = path.stat().st_size if path.exists() else 0
                    if (
                        offset != existing
                        or not 0 < length <= chunk_limit
                        or offset + length > limit
                    ):
                        raise ValueError("Invalid snapshot chunk")
                    self.connection.settimeout(self.server.transfer_timeout)
                    with path.open("ab") as destination:
                        while length:
                            data = self.rfile.read(min(length, 1024 * 1024))
                            if not data:
                                raise ValueError("Incomplete snapshot chunk")
                            destination.write(data)
                            length -= len(data)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")
                except (OSError, ValueError) as exc:
                    self.send_error(400, str(exc))

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.transfer_timeout = self.timeout
        thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05), daemon=True
        )
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/{token}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=self.timeout)

    def run(self, operation, slot="latest"):
        if operation not in {"save", "restore"}:
            raise ValueError("Unknown save-state operation")
        self.store.validate_slot(slot)
        session = self.session
        if session.controller != "manual" or session.finished or self.browser is None:
            raise ValueError("Save states require a running human DOS recorder")
        metadata = self.store.read(slot) if operation == "restore" else None
        if metadata is not None:
            session.validate_dos_state(metadata[1])
        session.wait_ready(self.timeout)
        session.dos_state_busy = True
        session.dos_state_quiet.clear()
        started = time.monotonic()
        try:
            if not session.dos_state_quiet.wait(self.timeout):
                raise TimeoutError("Observation loop did not become idle")
            future = asyncio.run_coroutine_threadsafe(
                self._run(operation, slot, metadata), self.loop
            )
            # asyncio owns the deadline so its finally block resumes the emulator
            # before the HTTP thread admits another input.
            result = future.result()
            return {**result, "seconds": time.monotonic() - started}
        finally:
            session.dos_state_busy = False

    def needs_restart(self, slot):
        """A smaller WASM heap requires a fresh browser; memory cannot shrink."""
        directory, _ = self.store.read(slot)
        with gzip.open(directory / "state.bin.gz", "rb") as source:
            prefix = source.read(12)
            if len(prefix) != 12 or prefix[:8] != b"VGBDOS01":
                raise ValueError("Invalid DOSBox state header")
            length = struct.unpack_from("<I", prefix, 8)[0]
            if not 0 < length <= min(self.config["max_bytes"], 2 * 1024**2):
                raise ValueError("Invalid DOSBox state header length")
            heap_bytes = json.loads(source.read(length))["heapSize"]
        if type(heap_bytes) is not int or heap_bytes < 65536 or heap_bytes % 65536:
            raise ValueError("Invalid DOSBox state heap size")
        self.session.wait_ready(self.timeout)
        future = asyncio.run_coroutine_threadsafe(
            self.command("heap-size", ""), self.loop
        )
        try:
            current = future.result(self.timeout)
        except Exception as exc:
            future.cancel()
            raise RuntimeError(f"Cannot inspect DOSBox heap: {exc}") from exc
        return current["heap_bytes"] > heap_bytes

    async def _run(self, operation, slot, loaded):
        try:
            async with asyncio.timeout(self.timeout * 3):
                return await self._perform(operation, slot, loaded)
        except (OSError, RuntimeError, ValueError):
            raise
        except Exception as exc:
            # Playwright's Error is not a RuntimeError. Normalize browser failures
            # after _perform resumes the game, so HTTP returns an error response
            # instead of dropping the connection and showing "NetworkError".
            raise RuntimeError(f"DOS state {operation} failed: {exc}") from exc

    async def _perform(self, operation, slot, loaded):
        session = self.session
        identifier = loaded[1]["id"] if loaded else uuid4().hex
        restored = False
        pending = None
        try:
            paused = await self.command("pause", identifier)
            if self.execution:
                await self.execution.flush()
            if operation == "save":
                result = await self.command("save", identifier)
                metadata = session.capture_dos_state(
                    host_monotonic(paused["pause_host_ms"])
                )
                metadata.update(
                    id=identifier,
                    clock_alignment="pause-boundary-v2",
                    mouse=self.browser.current_mouse_position,
                    front=await self.browser.page.evaluate(
                        "hostMs => vgbStates.front(hostMs)", paused["pause_host_ms"]
                    ),
                )
                pending = self.store.directory / (".pending-" + identifier)
                pending.mkdir()
                screen = await self.browser.get_screenshot()
                (pending / "screen.jpg").write_bytes(screen)
                with self.transfer(pending / "state.bin.gz", upload=True) as url:
                    sizes = await self.browser.page.evaluate(
                        "args => vgbStates.upload(args)",
                        {"slot": identifier, "url": url},
                    )
                metadata.update(sizes)
                if self.execution:
                    metadata["execution_head"] = self.execution.head
                self.store.commit(slot, pending, metadata)
                pending = None
                session.save("recording")
                return {**result, **sizes, "slot": slot, "saved_steps": session.steps}
            directory, metadata = loaded
            cached = (await self.command("has", identifier))["cached"]
            if not cached:
                with self.transfer(directory / "state.bin.gz", upload=False) as url:
                    await self.browser.page.evaluate(
                        "args => vgbStates.download(args)",
                        {"slot": identifier, "url": url},
                    )
            await self.browser.page.mouse.move(*metadata["mouse"])
            await self.command("restore", identifier)
            session.archive_dos_branch()
            session.apply_dos_state(metadata)
            self.browser.current_mouse_position = tuple(metadata["mouse"])
            await self.browser.page.evaluate(
                "state => vgbStates.setFront(state)", metadata["front"]
            )
            restored = True
            session.publish(await self.browser.get_screenshot())
            session.save("recording")
            return {"slot": slot, "cached": cached, "saved_steps": session.steps}
        finally:
            resumed = await self.command("resume", identifier)
            if restored:
                now = host_monotonic(resumed["resumed_host_ms"])
                session.control_started_at = now - loaded[1]["control_elapsed_sec"]
                session.started_at = now - loaded[1]["elapsed_sec"]
                await self.browser.page.evaluate(
                    "args => vgbStates.setFront(args.state, args.hostMs)",
                    {"state": loaded[1]["front"], "hostMs": resumed["resumed_host_ms"]},
                )
            else:
                seconds = resumed["paused_ms"] / 1000
                session.control_started_at += seconds
                session.started_at += seconds
                await self.browser.page.evaluate(
                    "ms => vgbStates.shiftClock(ms)", resumed["paused_ms"]
                )
            if pending is not None:
                shutil.rmtree(pending)
