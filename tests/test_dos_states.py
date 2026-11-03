import copy
import gzip
import json
import random
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from videogamebench_replayers.runtime.dos_snapshots import DosSnapshots, DosStateStore
from videogamebench_replayers.runtime.recorder import archive_attempt


@pytest.fixture
def saved(tmp_path):
    store = DosStateStore(tmp_path / ".dos-states", "task-identity", 1024)
    pending = store.directory / "pending"
    pending.mkdir()
    (pending / "state.bin.gz").write_bytes(gzip.compress(b"snapshot"))
    store.commit("latest", pending, {"id": "a" * 32})
    return store


def test_states_survive_restart_and_reject_corruption(saved):
    directory, metadata = saved.read("latest")
    (saved.directory.parent / "actions.json").write_text("{}")
    archive_attempt(saved.directory.parent)
    assert saved.read("latest") == (directory, metadata)
    with (directory / "state.bin.gz").open("ab") as output:
        output.write(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        saved.read("latest")


def test_delete_state_preserves_recording_and_other_slots(saved):
    directory, _ = saved.read("latest")
    recording = saved.directory.parent / "actions.json"
    recording.write_text('{"actions": []}')
    index = saved.slots()
    index["alias"] = index["latest"]
    (saved.directory / "index.json").write_text(json.dumps(index))
    saved.delete("latest")
    assert saved.read("alias")[0] == directory
    assert "latest" not in saved.slots()
    saved.delete("alias")
    assert saved.slots() == {}
    assert not directory.exists()
    assert recording.read_text() == '{"actions": []}'
    with pytest.raises(ValueError, match="No saved state"):
        saved.delete("alias")


def test_delete_rejects_unsafe_identifiers(saved):
    (saved.directory / "index.json").write_text(json.dumps({"latest": "../outside"}))
    with pytest.raises(ValueError):
        saved.delete("latest")
    with pytest.raises(ValueError):
        saved.delete("../latest")
    assert saved.slots() == {"latest": "../outside"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("identity", "other"),
        ("wasm_sha256", "other"),
        ("glue_sha256", "other"),
        ("format", 2),
    ],
)
def test_reject_incompatible_states(saved, field, value):
    directory, metadata = saved.read("latest")
    metadata[field] = value
    (directory / "session.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="task/runtime"):
        saved.read("latest")


@pytest.mark.parametrize("slot", ["../escape", "", "x" * 65, 123])
def test_reject_unsafe_slot(saved, slot):
    with pytest.raises(ValueError, match="slot"):
        saved.read(slot)


def test_state_limits_and_asset_pins(saved):
    saved.max_bytes = 1
    with pytest.raises(ValueError, match="size limit"):
        saved.read("latest")
    session = SimpleNamespace(
        state_dir=saved.directory.parent, manifest={"identity": "task-identity"}
    )
    snapshots = DosSnapshots(
        session, max_bytes=1024, cache_size=1, timeout=1, settle_ms=1, chunk_bytes=128
    )
    with pytest.raises(ValueError, match="Unsupported save-state runtime"):
        snapshots.patch_assets(
            {"https://example.com/wdosbox.js": ({}, b"unknown build")}
        )
    with pytest.raises(ValueError, match="pinned JS-DOS assets"):
        snapshots.patch_assets({})


@pytest.fixture
def session(tmp_path, monkeypatch):
    runtime = Path(__file__).parents[1] / "src/videogamebench_replayers/runtime"
    monkeypatch.syspath_prepend(str(runtime))
    from videogamebench_replayers.runtime.session import Session

    root = tmp_path / "game"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "identity": "task",
                "checkpoint_count": 2,
                "settings": {"record_video": False, "max_steps": 100},
            }
        )
    )
    return Session(root, tmp_path / "state")


def state_metadata():
    return {
        "identity": "task",
        "steps": 1,
        "trace": [
            {
                "action": {"action": "press_key", "action_input": "Enter"},
                "control_elapsed_sec": 2,
            }
        ],
        "control_elapsed_sec": 3,
        "elapsed_sec": 30,
        "random_state": json.loads(json.dumps(random.getstate())),
        "completed_checkpoints": [0],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"steps": 100},
        {"steps": 2},
        {"control_elapsed_sec": 1},
        {"elapsed_sec": float("nan")},
        {"completed_checkpoints": [2]},
        {"identity": "other"},
    ],
)
def test_reject_invalid_recording_metadata(session, change):
    metadata = state_metadata()
    session.validate_dos_state(metadata)
    metadata.update(change)
    with pytest.raises(ValueError):
        session.validate_dos_state(metadata)


def test_restore_copies_trace_and_random_state(session, monkeypatch):
    metadata = state_metadata()
    metadata.update(
        completed=False,
        checkpoint_index=1,
        current_checkpoint_idx=1,
        agent_checkpoint=1,
    )
    session.evaluator = SimpleNamespace(checkpoints=["first", "second"])
    session.agent = SimpleNamespace(current_checkpoint=0)
    monkeypatch.setattr(session, "log_progress", lambda *a, **kw: None)
    previous = random.getstate()
    try:
        session.apply_dos_state(metadata)
        expected = random.random()
        session.trace.append({"discard": True})
        session.apply_dos_state(copy.deepcopy(metadata))
        assert random.random() == expected
        assert len(session.trace) == session.steps == 1
        assert session.evaluator.completed_checkpoints == {"first"}
    finally:
        random.setstate(previous)


def test_snapshot_metadata_uses_pause_boundary_not_copy_completion(session):
    session.control_started_at = 1.0
    session.started_at = -5.0
    session.evaluator = SimpleNamespace(
        current_checkpoint_idx=0, checkpoints=[], completed_checkpoints=set()
    )
    session.agent = SimpleNamespace(current_checkpoint=0)
    metadata = session.capture_dos_state(captured_at=10.0)
    assert metadata["control_elapsed_sec"] == 9.0
    assert metadata["elapsed_sec"] == 15.0


def test_worker_freezes_clocks_before_settling_and_copying():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the worker clock regression check")
    worker = (
        Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/dos-state-worker.js"
    )
    script = r"""
const vm = require('node:vm'), fs = require('node:fs');
const clock = {date: 1700000000000, performance: 1000};
const advance = ms => {clock.date += ms; clock.performance += ms;};
const context = vm.createContext({
    Date: {now: () => clock.date}, performance: {now: () => clock.performance},
    _emscripten_get_now: () => clock.performance,
    VGB_STATE_CONFIG: {wasm_sha256: 'test', settle_ms: 40},
    Module: {alive: true, sockdrives: {}, sync_wakeUp() {},
        _requestPause() {}, _requestResume() {}},
    Asyncify: {state: 0, currData: 1, exportCallStack: []},
    setTimeout(fn, ms) {advance(ms); fn();},
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
    const pause = await context.Module.vgbStateCommand({operation: 'pause'});
    advance(2000); // Expensive snapshot copying/compression must not advance clocks.
    const frozen = vm.runInContext('[Date.now(), _emscripten_get_now()]', context);
    const resumed = await context.Module.vgbStateCommand({operation: 'resume'});
    advance(100);
    const running = vm.runInContext('[Date.now(), _emscripten_get_now()]', context);
    console.log(JSON.stringify({pause, frozen, resumed, running}));
})().catch(error => {console.error(error); process.exit(1);});
"""
    result = subprocess.run(
        [node, "-e", script, str(worker)], capture_output=True, text=True, check=True
    )
    clocks = json.loads(result.stdout)
    assert clocks["pause"]["pause_host_ms"] == 1700000000000
    assert clocks["frozen"] == [1700000000000, 1000]
    assert clocks["resumed"] == {"paused_ms": 2040, "resumed_host_ms": 1700000002040}
    assert clocks["running"] == [1700000000100, 1100]


@pytest.mark.parametrize("portable", [False, True])
def test_restore_does_not_inherit_discarded_branch_sleep_deadline(portable):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the worker restoration regression check")
    worker = (
        Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/dos-state-worker.js"
    )
    script = r"""
const vm = require('node:vm'), fs = require('node:fs');
const clock = {date: 1700000000000, performance: 1000};
const advance = ms => {clock.date += ms; clock.performance += ms;};
const original = () => {}, wrapper = () => {};
const context = vm.createContext({
    Date: {now: () => clock.date}, performance: {now: () => clock.performance},
    _emscripten_get_now: () => clock.performance,
    TextEncoder, TextDecoder, Uint8Array,
    VGB_STATE_CONFIG: {wasm_sha256: 'test', identity: 'test', settle_ms: 40,
        max_bytes: 1024 * 1024, cache_size: 2},
    HEAPU8: new Uint8Array(65536), runtimeKeepaliveCounter: 0,
    Module: {alive: true, sockdrives: {}, sync_wakeUp() {},
        snapshotFrame: [0, 1, 1], sendMessage() {},
        _requestPause() {}, _requestResume() {}},
    Asyncify: {state: 0, currData: 1, exportCallStack: [],
        callStackIdToFunc: new Map([[0, original]]),
        funcWrappers: new Map([[original, wrapper]])},
    wasmExports: {run: wrapper}, emsc_ws_client_frame_set_size() {},
    setTimeout(fn, ms) {advance(ms); fn();},
});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
// Filesystem contents are irrelevant to the scheduler; exercise the real
// portable encoder/decoder and worker save/restore commands below.
vm.runInContext(`capturePortableFs = () => ({entries: []});
    restorePortableFs = () => {};`, context);
(async () => {
    const command = (operation, extra = {}) =>
        context.Module.vgbStateCommand({operation, slot: 'test', ...extra});
    // A skippable Asyncify sleep has no wakeUpAt property.
    await command('save');
    if (process.argv[2] === 'portable') {
        const {bytes} = await command('export');
        await command('import', {bytes});
    }
    await command('resume');
    advance(60000); // User plays a branch that will be discarded.
    context.Module.wakeUpAt = context.Date.now() + 1;
    await command('restore');
    await command('resume');
    advance(2);
    const result = vm.runInContext(`({
        staleDeadline: Module.wakeUpAt !== undefined,
        canWake: Module.wakeUpAt === undefined || Date.now() >= Module.wakeUpAt,
    })`, context);
    console.log(JSON.stringify(result));
})().catch(error => {console.error(error); process.exit(1);});
"""
    result = subprocess.run(
        [node, "-e", script, str(worker), "portable" if portable else "cached"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"staleDeadline": False, "canWake": True}


def test_restore_preserves_parent_timestamps_after_creating_children():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the filesystem restoration regression check")
    worker = (
        Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/dos-state-worker.js"
    )
    script = r"""
const vm=require('node:vm'), fs=require('node:fs');
const nodes=new Map([['/',{mode:1,id:1}],['/dev',{mode:1}],['/proc',{mode:1}]]);
function create(path,mode,contents) {
    const parent=nodes.get(path.slice(0,path.lastIndexOf('/'))||'/');
    parent.atime=parent.mtime=parent.ctime=999999;
    nodes.set(path,{mode,id:nodes.size+1,contents,usedBytes:contents?.length});
}
const FS={streams:[],nextInode:100,
    lookupPath:path=>({node:nodes.get(path)}),isDir:mode=>mode===1,
    isFile:mode=>mode===2,isLink:mode=>mode===3,
    chdir(){},cwd:()=> '/',close(){},hashRemoveNode(){},hashAddNode(){},
    readdir:path=>['.','..',...Array.from(nodes.keys()).filter(p=>p!=='/' && (p.slice(0,p.lastIndexOf('/'))||'/')===path).map(p=>p.split('/').at(-1))],
    mkdir:path=>create(path,1),writeFile:(path,bytes)=>create(path,2,bytes),
};
const context=vm.createContext({FS,TTY:{ttys:[]},Date:{now:()=>999999},
 performance:{now:()=>1000},_emscripten_get_now:()=>1000,
 Module:{},VGB_STATE_CONFIG:{wasm_sha256:'test'}});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.saved={entries:[
 {path:'/',mode:1,id:1,atime:10,mtime:11,ctime:12},
 {path:'/game',mode:1,id:40,atime:20,mtime:21,ctime:22},
 {path:'/game/data',mode:2,id:41,atime:30,mtime:31,ctime:32,contents:new Uint8Array([1,2])}
],streams:[],cwd:'/',nextInode:42,ttys:[]};
vm.runInContext('restorePortableFs(saved)',context);
console.log(JSON.stringify(context.saved.entries.map(e=>{
 const n=nodes.get(e.path);return [n.id,n.atime,n.mtime,n.ctime];
})));
"""
    result = subprocess.run(
        [node, "-e", script, str(worker)], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == [
        [1, 10, 11, 12],
        [40, 20, 21, 22],
        [41, 30, 31, 32],
    ]


def test_browser_save_failure_resumes_game_and_becomes_http_error(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock

    # Playwright Error inherits directly from Exception, not RuntimeError.
    class BrowserError(Exception):
        pass

    session = SimpleNamespace(
        state_dir=tmp_path,
        manifest={"identity": "task"},
        control_started_at=10,
        started_at=5,
    )
    snapshots = DosSnapshots(
        session, max_bytes=1024, cache_size=1, timeout=1, settle_ms=0, chunk_bytes=128
    )
    calls = []

    async def command(operation, slot):
        calls.append(operation)
        if operation == "pause":
            return {"pause_host_ms": 1000}
        if operation == "save":
            raise BrowserError("Heap exceeds configured snapshot limit")
        return {"paused_ms": 250}

    snapshots.command = command
    snapshots.browser = SimpleNamespace(page=SimpleNamespace(evaluate=AsyncMock()))
    with pytest.raises(RuntimeError, match="DOS state save failed:.*snapshot limit"):
        asyncio.run(snapshots._run("save", "latest", None))
    assert calls == ["pause", "save", "resume"]
    assert session.control_started_at == 10.25
    assert session.started_at == 5.25
    assert snapshots.store.slots() == {}


def test_restore_keeps_exact_wasm_size_and_rejects_shrinking_before_branch_change():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the WASM heap restoration check")
    worker = (
        Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/dos-state-worker.js"
    )
    script = r"""
const vm=require('node:vm'),fs=require('node:fs');
function runtime(pages){
 const memory=new WebAssembly.Memory({initial:pages,maximum:16});
 const original=()=>{},wrapper=()=>{};let restores=0;
 const context=vm.createContext({
  VGB_STATE_CONFIG:{wasm_sha256:'test',identity:'test',max_bytes:1048576,cache_size:1,settle_ms:0},
  Date:{now:()=>1000},performance:{now:()=>100},TextEncoder,TextDecoder,Uint8Array,
  HEAPU8:new Uint8Array(memory.buffer),wasmMemory:memory,runtimeKeepaliveCounter:0,
  updateMemoryViews(){context.HEAPU8=new Uint8Array(memory.buffer)},
  Module:{alive:true,sockdrives:{},sync_wakeUp(){},snapshotFrame:[0,1,1],sendMessage(){},
   vgbExecution:{pause(){},resume(){},capture(){return {head:'prefix'}},restore(){restores++}}},
  Asyncify:{state:0,currData:1,exportCallStack:[],callStackIdToFunc:new Map([[0,original]]),funcWrappers:new Map([[original,wrapper]])},
  wasmExports:{run:wrapper},emsc_ws_client_frame_set_size(){},setTimeout(fn){fn()}
 });
 vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
 vm.runInContext('capturePortableFs=()=>({entries:[]});restorePortableFs=()=>{};',context);
 return {context,command:(operation,bytes)=>context.Module.vgbStateCommand({operation,bytes,slot:'s'}),restores:()=>restores};
}
(async()=>{
 const source=runtime(3);source.context.HEAPU8[123]=77;await source.command('save');
 const bytes=(await source.command('export')).bytes;
 const small=runtime(2);await small.command('import',bytes);await small.command('restore');
 if(small.context.HEAPU8.length!==3*65536||small.context.HEAPU8[123]!==77||small.restores()!==1)throw Error('Incorrect exact growth');
 const big=runtime(4);big.context.HEAPU8[123]=99;await big.command('import',bytes);
 let failure='';try{await big.command('restore')}catch(e){failure=String(e)}
 await big.command('resume');
 if(!failure.includes('fresh emulator')||big.restores()!==0||big.context.HEAPU8[123]!==99)throw Error('Unsafe shrink restore');
 console.log('exact grow; safe shrink rejection');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(
        [node, "-e", script, str(worker)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "exact grow; safe shrink rejection"


@pytest.mark.parametrize(
    "live_pages,needs_restart", [(1, False), (2, False), (3, True)]
)
def test_restore_preflight_detects_wasm_heap_shrink(
    tmp_path, monkeypatch, live_pages, needs_restart
):
    import concurrent.futures
    import struct

    from videogamebench_replayers.runtime import dos_snapshots

    header = json.dumps({"heapSize": 2 * 65536}).encode()
    (tmp_path / "state.bin.gz").write_bytes(
        gzip.compress(b"VGBDOS01" + struct.pack("<I", len(header)) + header)
    )
    session = SimpleNamespace(
        state_dir=tmp_path,
        manifest={"identity": "task"},
        wait_ready=lambda timeout: None,
    )
    snapshots = DosSnapshots(
        session,
        max_bytes=1024**2,
        cache_size=1,
        timeout=1,
        settle_ms=0,
        chunk_bytes=128,
    )
    snapshots.store.read = lambda slot: (tmp_path, {})

    def inspect(coroutine, loop):
        coroutine.close()
        result = concurrent.futures.Future()
        result.set_result({"heap_bytes": live_pages * 65536})
        return result

    monkeypatch.setattr(dos_snapshots.asyncio, "run_coroutine_threadsafe", inspect)
    assert snapshots.needs_restart("latest") is needs_restart
