"""Recorded clocks/native events for opt-in DOS human oracles, never normal agents."""

import asyncio
import json
import math
from pathlib import Path

from execution_store import ExecutionStore

RUNTIME = Path(__file__).parent


def patch_execution(script, config):
    replacements = {
        "Date.now()": "vgbDateNow()",
        "var _emscripten_get_now=()=>performance.now();": "var _emscripten_get_now=()=>vgbPerformanceNow();",
        "function callMain(args=[]){": "function callMain(args=[]){args=Module.vgbArgs(args);",
        "Module.sessionId=UTF8ToString(sessionId);": "Module.sessionId=Module.vgbLiveSessionId;Module.vgbProcess=processMessage;",
        "Module._runRuntime();": "vgbNativeCall(()=>Module._runRuntime());vgbDrive();",
        "Module.sync_wakeUp=wakeUp;": "Module.sync_wakeUp=vgbWrapWake(wakeUp);if(vgbReplay)return;",
        "Module.receive=function(ev){": "Module.receive=function(ev){if(vgbReplay||Module.vgbExecution.blocked||!Module.sync_wakeUp)return;",
        "function processMessage(data){": """function processMessage(data){
if(data.name==='wc-vgb-state' && data.props.operation.startsWith('exec-')) {
 Module.vgbExecution.command(data.props.operation,data.props.slot).then(result=>postMessage({vgbState:data.props.request,result}),error=>postMessage({vgbState:data.props.request,error:String(error)}));return;
}
if(vgbInputNames.has(data.name)) {
 if(Module.vgbExecution.blocked || (vgbReplay && !vgbDriving))return;
 if(!vgbReplay){const props={...data.props};delete props.sessionId;vgbAppend([3,data.name,props]);}
 return vgbNativeCall(()=>vgbOriginalMessage(data));
}
return vgbOriginalMessage(data);
} function vgbOriginalMessage(data){""",
    }
    for marker, replacement in replacements.items():
        if marker not in script:
            raise ValueError("Unsupported execution glue layout: " + marker)
        script = script.replace(marker, replacement)
    script = script.replace(
        "vgbDateNow()<Module.wakeUpAt", "Date.now()<Module.wakeUpAt"
    )
    # Insert after replacements: recorder infrastructure must use the host clock.
    script = script.replace(
        "var Module=moduleArg;",
        "var Module=moduleArg;const VGB_EXECUTION_CONFIG="
        + json.dumps(config)
        + ";\n"
        + """Module.vgbLoadChunk=async (id,expectedParent)=>{
 const response=await fetch(VGB_EXECUTION_CONFIG.replay.base_url+id+'.json.gz');
 if(!response.ok)throw Error('Execution chunk unavailable');
 const reader=response.body.pipeThrough(new DecompressionStream('gzip')).getReader();
 const parts=[];let length=0;
 for(;;){const {done,value}=await reader.read();if(done)break;
  length+=value.length;if(length>VGB_EXECUTION_CONFIG.max_chunk_bytes){await reader.cancel();throw Error('Oversized execution chunk');}parts.push(value);}
 const bytes=new Uint8Array(length);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}
 const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');
 if(hash!==id)throw Error('Execution chunk checksum mismatch');
 const chunk=JSON.parse(new TextDecoder().decode(bytes));
 if(expectedParent!==undefined && chunk.parent!==expectedParent)throw Error('Execution suffix parent mismatch');
 return chunk.events;
};
if(VGB_EXECUTION_CONFIG.replay)VGB_EXECUTION_CONFIG.replay.events=await Module.vgbLoadChunk(VGB_EXECUTION_CONFIG.replay.chunks[0]);
"""
        + (RUNTIME / "dos-execution-worker.js").read_text()
        + "\n",
    )
    return script


class DosExecution:
    def __init__(
        self,
        snapshots,
        *,
        max_chunk_bytes=16 * 1024 * 1024,
        max_total_bytes=4 * 1024**3,
        max_pending_events=500000,
        flush_seconds=1,
        slice_ms=10,
    ):
        if max_pending_events <= 0 or any(
            not math.isfinite(v) or v <= 0 for v in (flush_seconds, slice_ms)
        ):
            raise ValueError("Execution limits must be positive")
        self.snapshots = snapshots
        self.store = ExecutionStore(
            snapshots.session.state_dir / ".dos-execution",
            max_chunk_bytes=max_chunk_bytes,
            max_total_bytes=max_total_bytes,
        )
        self.config = {
            "replay": None,
            "max_chunk_bytes": max_chunk_bytes,
            "max_pending_events": max_pending_events,
            "slice_ms": slice_ms,
            "hold_markers": False,
        }
        self.flush_seconds = flush_seconds
        self.lock = asyncio.Lock()
        self.task = None
        self.error = None
        self.head = None
        self.finished = None
        self.stopping = asyncio.Event()

    async def flush(self):
        async with self.lock:
            value = await self.snapshots.command("exec-drain", "")
            if value["events"]:
                self.head = await asyncio.to_thread(self.store.put, value)
                await self.snapshots.command("exec-head", self.head)
            else:
                self.head = value["parent"]

    async def start(self):
        async def drain():
            try:
                while True:
                    try:
                        await asyncio.wait_for(self.stopping.wait(), self.flush_seconds)
                        return
                    except TimeoutError:
                        await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 -- stop instead of silently losing trace data
                self.error = str(error)
                self.snapshots.session.error = (
                    "Execution recording failed: " + self.error
                )
                await self.snapshots.command("exec-stop", "end")

        self.task = asyncio.create_task(drain())

    async def finish(self):
        if self.finished is not None:
            return self.finished
        if self.task:
            self.stopping.set()
            await self.task
        if self.error:
            raise RuntimeError(self.error)
        result = await self.snapshots.command("exec-stop", "end")
        await self.flush()
        session = self.snapshots.session
        metadata = {
            "identity": session.manifest["identity"],
            "game": session.manifest["game"],
            "verification": result,
            "config": self.config,
        }
        self.finished = await asyncio.to_thread(
            self.store.export, self.head, session.state_dir / "execution", metadata
        )
        return self.finished
