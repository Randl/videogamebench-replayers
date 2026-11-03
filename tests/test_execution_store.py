import gzip
import json
from pathlib import Path

import pytest

from videogamebench_replayers.runtime.execution_store import ExecutionStore

FP = {"heap_sha256": "a" * 64, "frame_sha256": "b" * 64, "width": 640, "height": 480}


def store(tmp_path):
    return ExecutionStore(tmp_path, max_chunk_bytes=4096, max_total_bytes=32768)


def test_restore_branches_keep_old_prefixes_immutable(tmp_path):
    log = store(tmp_path / "record")
    boot = log.put({"parent": None, "events": [[0, 1.5, 2], [2]]})
    discarded = log.put({"parent": boot, "events": [[4, "discarded", FP]]})
    kept = log.put({"parent": boot, "events": [[4, "end", FP]]})
    manifest = log.export(kept, tmp_path / "export", {"identity": "task"})
    assert manifest["chunks"] == [boot, kept]
    assert log.chain(discarded) == [boot, discarded]
    assert log.chain(kept) == [boot, kept]
    assert not (tmp_path / "export/objects" / (discarded + ".json.gz")).exists()


def test_corrupt_prefix_cannot_be_exported(tmp_path):
    log = store(tmp_path / "record")
    head = log.put({"parent": None, "events": [[4, "end", FP]]})
    log.path(head).write_bytes(gzip.compress(b'{"parent": null, "events": [[2]]}'))
    with pytest.raises(ValueError, match="checksum"):
        log.export(head, tmp_path / "export", {})
    assert not (tmp_path / "export").exists()


def test_recording_must_end_at_verified_boundary(tmp_path):
    log = store(tmp_path / "record")
    head = log.put({"parent": None, "events": [[4, "ready", FP]]})
    with pytest.raises(ValueError, match="final verification"):
        log.export(head, tmp_path / "export", {})


@pytest.mark.parametrize(
    "event", [[99], [3, "wc-write-memory", {}], [0, float("nan"), 1], [0, 1, -1]]
)
def test_execution_log_rejects_unsupported_operations(tmp_path, event):
    log = store(tmp_path)
    with pytest.raises(ValueError):
        log.put({"parent": None, "events": [event]})


def test_final_marker_must_be_last_event(tmp_path):
    log = store(tmp_path / "store")
    head = log.put({"parent": None, "events": [[4, "end", FP], [2]]})
    with pytest.raises(ValueError, match="final verification"):
        log.export(head, tmp_path / "export", {})


@pytest.mark.parametrize(
    "event",
    [
        [4, "end"],
        [4, "end", {}],
        [3, [], {}],
        [3, "wc-add-key", {"key": "bad", "pressed": True, "timeMs": 1}],
        [3, "wc-asyncify-stats", {"sessionId": "injected"}],
    ],
)
def test_malformed_payloads_cannot_enter_trace(tmp_path, event):
    with pytest.raises(ValueError):
        store(tmp_path).put({"parent": None, "events": [event]})


@pytest.mark.parametrize("capture_prefix", [False, True])
def test_reconstruction_can_capture_prefix_but_verification_requires_end(
    capture_prefix,
):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the execution worker test")
    worker = (
        Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/dos-execution-worker.js"
    )
    script = r"""
const vm=require('node:vm'),fs=require('node:fs');
const context=vm.createContext({
 VGB_EXECUTION_CONFIG:{replay:{events:[[5,['session']],[2],[0,12.5,1],[1,1700000000123,1]],chunks:['prefix-head']},
  capture_prefix:process.argv[2]==='true',slice_ms:50},
 Date:{now:()=>1700000009999},performance:{now:()=>0},
 Module:{},console:{error(){}},setTimeout(){throw Error('Unexpected scheduling')}
});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext(`Module.vgbArgs(['live-session']);Module.sync_wakeUp=()=>{
 vgbPerformanceNow();vgbDateNow();};`,context);
(async()=>{
 await vm.runInContext('vgbDrive()',context);
 const status=await context.Module.vgbExecution.command('exec-status');
 console.log(JSON.stringify({status,clock:context.Module.vgbExecution.snapshotClock(),
  cursor:status.marker==='prefix'?context.Module.vgbExecution.capture():null}));
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = json.loads(
        subprocess.check_output(
            [node, "-e", script, str(worker), str(capture_prefix).lower()], text=True
        )
    )
    if capture_prefix:
        assert result["status"]["marker"] == "prefix"
        assert result["status"]["error"] is None
        assert result["cursor"] == {"head": "prefix-head"}
        assert result["clock"] == {"date": 1700000000123, "performance": 12.5}
    else:
        assert "without final verification" in result["status"]["error"]
        assert result["status"]["marker"] is None
        assert result["clock"] is None


@pytest.mark.parametrize("tail_chunks,expected_index", [(0, 1), (1, 2), (5, 3)])
def test_idle_trim_keeps_complete_input_chunk_and_requested_tail(
    tmp_path, monkeypatch, tail_chunks, expected_index
):
    monkeypatch.syspath_prepend(
        str(Path(__file__).parents[1] / "src/videogamebench_replayers/runtime")
    )
    from trim_execution import select_prefix

    log = store(tmp_path)
    chunks = [log.put({"parent": None, "events": [[5, ["session"]]]})]
    chunks.append(
        log.put(
            {
                "parent": chunks[-1],
                "events": [
                    [
                        3,
                        "wc-mouse-button",
                        {"button": 0, "pressed": False, "timeMs": 15},
                    ],
                    [2],
                    [0, 16, 1],
                ],
            }
        )
    )
    for t in (20, 30):
        chunks.append(log.put({"parent": chunks[-1], "events": [[2], [0, t, 1]]}))
    assert select_prefix(log, chunks[-1], tail_chunks) == chunks[expected_index]
    assert log.chain(chunks[-1]) == chunks  # Original remains intact.
    with pytest.raises(ValueError, match="no human inputs"):
        select_prefix(log, chunks[0], tail_chunks)
    with pytest.raises(ValueError, match="nonnegative"):
        select_prefix(log, chunks[-1], -1)


def test_suffix_replay_requires_matching_paused_flushed_prefix():
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the suffix replay check")
    worker = (
        Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/dos-execution-worker.js"
    )
    script = r"""
const vm=require('node:vm'),fs=require('node:fs');
const context=vm.createContext({
 VGB_EXECUTION_CONFIG:{replay:null,max_pending_events:100,slice_ms:50},
 Date:{now:()=>1000},performance:{now:()=>0},console:{error(){}},setTimeout(){},
 Module:{vgbLoadChunk:async(id,parent)=>{if(parent!=='prefix')throw Error('Wrong parent');return [[2],[0,25,1],[1,1250,1]]}}
});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext(`Module.vgbArgs(['session']);Module.sync_wakeUp=()=>{vgbPerformanceNow();vgbDateNow()}`,context);
(async()=>{
 const e=context.Module.vgbExecution, suffix={parent:'prefix',chunks:['tail'],base_url:'http://local/'};
 let failures=0;
 try{await e.command('exec-replay-suffix',suffix)}catch{failures++}
 e.pause();
 try{await e.command('exec-replay-suffix',suffix)}catch{failures++}
 const pending=await e.command('exec-drain');
 if(pending.events.length!==1)throw Error('Rejected transition lost recorded data');
 await e.command('exec-head','prefix');
 try{await e.command('exec-replay-suffix',{...suffix,parent:'wrong'})}catch{failures++}
 await e.command('exec-replay-suffix',suffix);
 e.resume();await e.command('exec-advance');
 const status=await e.command('exec-status');
 if(failures!==3||status.error||status.marker!=='prefix'||status.head!=='tail')throw Error(JSON.stringify(status));
 if(JSON.stringify(e.capture())!==JSON.stringify({head:'tail'}))throw Error('Incorrect saved continuation');
 console.log('verified paused prefix transition');
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(
        [node, "-e", script, str(worker)], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "verified paused prefix transition"
