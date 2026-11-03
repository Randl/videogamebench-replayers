import importlib.util
import io
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from videogamebench_replayers.runtime import recorder


def test_human_drag_bounds_preserve_queue_order_and_record_valid_release():
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for recorder UI regression tests")
    player = Path(__file__).parents[1] / "src/videogamebench_replayers/runtime/player-dos.html"
    script = r"""
const fs=require('node:fs'), assert=require('node:assert/strict');
const html=fs.readFileSync(process.argv[1],'utf8');
const source=html.slice(html.indexOf('function boundedDrag('),html.indexOf('function action('));
let active=true,saving=false,queued=0,queue=Promise.resolve();
const status={textContent:''}, sent=[];
const bounds={left:77.328125,top:0,right:610.65625,bottom:400};
async function api(path,body){
 if(path==='/pointer-bounds'){await new Promise(r=>setTimeout(r,10));return bounds;}
 sent.push(body);return {};
}
function update(){}
function fail(error){throw error;}
eval(source);
const move={action:'move',action_input:'271,209'};
const drag={action:'drag',action_input:'631,392'};
const key={action:'press_key',action_input:'p'};
assert.deepEqual(boundedDrag([move,drag],bounds),[move,{...drag,action_input:'610,392'}]);
assert.deepEqual(boundedDrag([move,{...drag,action_input:'-20,-5'}],bounds),[move,{...drag,action_input:'78,0'}]);
assert.equal(boundedDrag([move,drag],null),null);
assert.equal(boundedDrag([{...move,action_input:'631,392'},drag],bounds),null);
enqueue([move,drag],true);
enqueue([key]);
queue.then(()=>{
 assert.deepEqual(sent,[move,{...drag,action_input:'610,392'},key]);
 assert.equal(queued,0);
 assert.equal(drag.action_input,'631,392'); // The caller's input is not mutated.
}).catch(e=>{console.error(e);process.exitCode=1;});
"""
    subprocess.run([node, "-e", script, str(player)], check=True, capture_output=True)


def test_restart_preserves_separate_attempts(tmp_path):
    cache = tmp_path / ".boot-cache"
    cache.mkdir()
    (cache / "initial.state").write_bytes(b"initial state")
    saved = []
    for steps in (25, 0, 12):
        (tmp_path / "actions.json").write_text(json.dumps({"steps": steps}))
        (tmp_path / "gameplay.mp4").write_bytes(bytes([steps]))
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs/game.log").write_text(str(steps))
        saved.append(recorder.archive_attempt(tmp_path))
        assert set(tmp_path.iterdir()) == {tmp_path / "attempts", cache}
        assert (cache / "initial.state").read_bytes() == b"initial state"
    assert len(set(saved)) == 3
    for path, steps in zip(saved, (25, 0, 12), strict=True):
        assert json.loads((path / "actions.json").read_text())["steps"] == steps
        assert (path / "gameplay.mp4").read_bytes() == bytes([steps])
        assert (path / "logs/game.log").read_text() == str(steps)


def test_export_restored_branch_keeps_prefix_images_and_omits_discarded(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    prefix = recorder.store_observation(state, b"\x89PNG prefix")
    discarded = recorder.store_observation(state, b"\x89PNG discarded")
    (state / "actions.json").write_text(
        json.dumps(
            {
                "actions": [
                    {"action": "prefix", "before_observation": prefix},
                    {"action": "discarded", "before_observation": discarded},
                ]
            }
        )
    )
    archive = recorder.archive_attempt(state)
    suffix = recorder.store_observation(state, b"\x89PNG suffix")
    selected = {
        "actions": [
            {"action": "prefix", "before_observation": prefix},
            {"action": "retained", "after_observation": suffix},
        ]
    }
    (state / "actions.json").write_text(json.dumps(selected))
    output = tmp_path / "export"
    report = recorder.export_recording(state, state / "actions.json", output)
    assert report["actions"] == 2
    assert report["actions_with_before_observation"] == 1
    assert report["actions_with_after_observation"] == 1
    assert report["fresh_replay_validated"] is False
    assert {p.name for p in (output / "observations").iterdir()} == {
        prefix["file"],
        suffix["file"],
    }
    assert (
        json.loads((output / "actions.json").read_text())["actions"]
        == selected["actions"]
    )
    # Archived and saved prefixes still resolve after another attempt is started.
    recorder.export_recording(state, archive / "actions.json", tmp_path / "old-export")
    with pytest.raises(FileExistsError):
        recorder.export_recording(state, state / "actions.json", output)
    (state / recorder.OBSERVATIONS / prefix["file"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        recorder.export_recording(
            state, state / "actions.json", tmp_path / "bad-export"
        )
    assert not (tmp_path / "bad-export").exists()


def test_export_older_recording_reports_missing_observations(tmp_path):
    trace = tmp_path / "actions.json"
    trace.write_text(json.dumps({"actions": [{"action": None}]}))
    result = recorder.export_recording(tmp_path, trace, tmp_path / "export")
    assert result["actions"] == 1
    assert result["actions_with_before_observation"] == 0
    assert result["actions_with_after_observation"] == 0


@pytest.mark.parametrize(
    "enabled,controller", [(True, "manual"), (False, "manual"), (True, "vgagent")]
)
def test_save_recording_keeps_session_running(monkeypatch, enabled, controller):
    monkeypatch.setitem(sys.modules, "recorder", recorder)
    monkeypatch.setitem(sys.modules, "session", SimpleNamespace(Session=None))
    spec = importlib.util.spec_from_file_location(
        "save_recorder_test", Path(recorder.__file__).with_name("server.py")
    )
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    events = []
    monkeypatch.setattr(
        server, "ARGS", SimpleNamespace(allow_restart=enabled), raising=False
    )
    # No finish/restart method: saving must leave the live emulator alone.
    session = SimpleNamespace(
        started=True,
        finished=False,
        controller=controller,
        manifest={"emulator": "dos"},
        wait_ready=lambda: events.append("ready"),
        submit=lambda action: events.append(action),
        save=lambda status: events.append(status),
    )
    monkeypatch.setattr(server, "SESSION", session, raising=False)
    monkeypatch.setattr(server, "status", lambda: {"finished": session.finished})
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/save"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.send = lambda data: events.append(json.loads(data))
    handler.send_error = lambda code, text: events.append(code)
    handler.do_POST()
    assert events == (
        [
            "ready",
            {"action": None, "action_input": ""},
            "ready",
            "recording",
            {"finished": False},
        ]
        if enabled and controller == "manual"
        else [403]
    )


@pytest.mark.parametrize(
    "enabled,controller,emulator,mode",
    [
        (False, "manual", "gba", "lite"),
        (True, "vgagent", "gba", "lite"),
        (True, "vgagent", "dos", "realtime"),
        (True, "manual", "gba", "realtime"),
    ],
)
@pytest.mark.parametrize("endpoint", ["/restart", "/rewind"])
def test_benchmark_restart_is_rejected(
    monkeypatch, enabled, controller, emulator, mode, endpoint
):
    monkeypatch.setitem(sys.modules, "recorder", recorder)
    monkeypatch.setitem(sys.modules, "session", SimpleNamespace(Session=None))
    path = Path(recorder.__file__).with_name("server.py")
    spec = importlib.util.spec_from_file_location("recorder_test_server", path)
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(
        server, "ARGS", SimpleNamespace(allow_restart=enabled), raising=False
    )
    # There is deliberately no finish method: rejection must not change a run.
    monkeypatch.setattr(
        server,
        "SESSION",
        SimpleNamespace(
            controller=controller,
            manifest={"emulator": emulator},
            settings={"mode": mode},
        ),
        raising=False,
    )
    monkeypatch.setattr(server, "LOCK", threading.Lock(), raising=False)
    handler = server.Handler.__new__(server.Handler)
    handler.path = endpoint
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    errors = []
    handler.send_error = lambda code, text: errors.append(code)
    handler.do_POST()
    assert errors == [403]


def test_rewind_keeps_prefix_and_rejects_invalid_counts():
    trace = [{"action": {"START": True}}, {"action": None}, {"action": {"RIGHT": True}}]
    assert recorder.rewind_prefix(trace, 1) == [{"START": True}, None]
    assert recorder.rewind_prefix(trace, 3) == []
    assert len(trace) == 3
    for count in (0, -1, 4, 1.5, True, None):
        with pytest.raises(ValueError):
            recorder.rewind_prefix(trace, count)  # ty: ignore[invalid-argument-type]


def test_snapshots_follow_exact_prefix_and_skip_corruption(tmp_path):
    from videogamebench_replayers.runtime.snapshots import SnapshotStore

    actions = [None, {"RIGHT": True}, {"A": True, "RIGHT": True}, None]
    store = SnapshotStore(tmp_path, "identity", "2.6.1", 2)
    store.advance(actions[:2])
    store.save(b"state2", b"screen2", {"checkpoint_index": 1})
    store.advance(actions[2:])
    store.save(b"state4", b"screen4", {"checkpoint_index": 2})
    assert store.nearest(actions)[0]["steps"] == 4
    assert store.nearest(actions[:3])[1] == b"state2"
    assert store.nearest(actions[:2] + [{"LEFT": True}, None])[1] == b"state2"
    assert store.nearest([{"START": True}]) is None
    assert SnapshotStore(tmp_path, "different-rom", "2.6.1").nearest(actions) is None
    assert SnapshotStore(tmp_path, "identity", "other-version").nearest(actions) is None
    (tmp_path / f"{store.digest}.snapshot").write_bytes(b"truncated")
    assert store.nearest(actions)[1] == b"state2"


def test_snapshot_cache_survives_archival(tmp_path):
    cache = tmp_path / ".snapshots"
    cache.mkdir()
    (cache / "sample.snapshot").write_bytes(b"state")
    (tmp_path / "actions.json").write_text("{}")
    archive = recorder.archive_attempt(tmp_path)
    assert (archive / "actions.json").exists()
    assert (cache / "sample.snapshot").read_bytes() == b"state"


@pytest.mark.parametrize("endpoint", ["/restart", "/rewind"])
def test_human_dos_can_restart_but_not_rewind(monkeypatch, tmp_path, endpoint):
    monkeypatch.setitem(sys.modules, "recorder", recorder)
    monkeypatch.setitem(sys.modules, "session", SimpleNamespace(Session=None))
    spec = importlib.util.spec_from_file_location(
        "dos_restart_test", Path(recorder.__file__).with_name("server.py")
    )
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    events = []
    monkeypatch.setattr(
        server, "ARGS", SimpleNamespace(allow_restart=True), raising=False
    )
    session = SimpleNamespace(
        controller="manual",
        manifest={"emulator": "dos"},
        settings={"mode": "realtime"},
        state_dir=tmp_path,
        finish=lambda: events.append("finish"),
    )
    monkeypatch.setattr(server, "SESSION", session, raising=False)
    monkeypatch.setattr(server, "archive_attempt", lambda path: Path("saved-attempt"))
    monkeypatch.setattr(server, "restart", lambda: events.append("restart"))
    handler = server.Handler.__new__(server.Handler)
    handler.path = endpoint
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.wfile = io.BytesIO()
    handler.send = lambda data: events.append(json.loads(data))
    handler.send_error = lambda code, text: events.append(code)
    handler.do_POST()
    assert events == (
        ["finish", {"saved_attempt": "saved-attempt"}, "restart"]
        if endpoint == "/restart"
        else [403]
    )


def test_timed_dos_resume_plan_validates_before_restart():
    trace = {
        "identity": "task",
        "mode": "realtime",
        "actions": [
            {
                "action": {"action": "press_key", "action_input": "Enter"},
                "control_elapsed_sec": 1.5,
            },
            {"action": {"action": None, "action_input": ""}, "control_elapsed_sec": 3},
        ],
    }
    plan = recorder.timed_resume_plan(trace, "task")
    assert plan["kind"] == "timed-dos"
    assert plan["actions"] == trace["actions"]
    with pytest.raises(ValueError, match="does not match"):
        recorder.timed_resume_plan(trace, "other")
    for bad in (
        [],
        [{"action": None}],
        [{"action": {}, "control_elapsed_sec": float("nan")}],
        trace["actions"][::-1],
    ):
        with pytest.raises(ValueError):
            recorder.timed_resume_plan({**trace, "actions": bad}, "task")


def load_recorder_server(monkeypatch):
    monkeypatch.setitem(sys.modules, "recorder", recorder)
    monkeypatch.setitem(sys.modules, "session", SimpleNamespace(Session=None))
    spec = importlib.util.spec_from_file_location(
        "dos_resume_test", Path(recorder.__file__).with_name("server.py")
    )
    assert spec is not None and spec.loader is not None
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


def test_resume_preserves_saved_prefix_when_server_is_fresh(monkeypatch, tmp_path):
    server = load_recorder_server(monkeypatch)
    trace = {
        "identity": "task",
        "mode": "realtime",
        "actions": [
            {"action": {"action": None, "action_input": ""}, "control_elapsed_sec": 2},
        ],
    }
    original = json.dumps(trace)
    (tmp_path / "actions.json").write_text(original)
    events = []
    monkeypatch.setattr(
        server, "ARGS", SimpleNamespace(allow_restart=True), raising=False
    )
    monkeypatch.setattr(
        server,
        "SESSION",
        SimpleNamespace(
            controller=None,
            started=False,
            finished=False,
            manifest={"emulator": "dos", "identity": "task"},
            settings={"mode": "realtime", "max_steps": 100},
            state_dir=tmp_path,
            # No finish method: it would overwrite the saved recording on a fresh server.
        ),
        raising=False,
    )
    monkeypatch.setattr(server, "restart", lambda: events.append("restart"))
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/resume"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = io.BytesIO(b"{}")
    handler.wfile = io.BytesIO()
    handler.send = lambda data: events.append(json.loads(data))
    handler.send_error = lambda code, text: events.append(code)
    handler.do_POST()
    assert events[-1] == "restart"
    archive = tmp_path / "attempts" / events[0]["saved_attempt"]
    assert (archive / "actions.json").read_text() == original
    assert (
        json.loads((tmp_path / "restore-plan.json").read_text())["actions"]
        == trace["actions"]
    )


def test_dos_restore_keeps_session_open_and_preserves_schedule(monkeypatch, tmp_path):
    from PIL import Image

    server = load_recorder_server(monkeypatch)
    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    clock = [100.0]
    actions = []
    deadlines = []
    saves = []
    session = SimpleNamespace(
        manifest={"identity": "task"},
        state_dir=tmp_path,
        control_started_at=100.0,
        steps=0,
        finished=False,
        screen=image.getvalue(),
        start=lambda: None,
        wait_ready=lambda: None,
        save=lambda status: saves.append(status),
    )

    def submit(action, *, at_control_sec):
        deadlines.append(at_control_sec)
        actions.append(action)
        session.steps += 1

    session.submit = submit
    monkeypatch.setattr(server, "SESSION", session, raising=False)
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        server.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    trace = {
        "identity": "task",
        "mode": "realtime",
        "actions": [
            {
                "action": {"action": "press_key", "action_input": "Enter"},
                "control_elapsed_sec": 1,
            },
            {"action": {"action": None, "action_input": ""}, "control_elapsed_sec": 3},
        ],
    }
    server.restore(recorder.timed_resume_plan(trace, "task"))
    assert server.RESTORE_ERROR is None
    assert actions == [a["action"] for a in trace["actions"]]
    assert clock[0] == 100
    assert deadlines == [1, 3]
    assert saves == ["recording"]
    assert session.finished is False
    assert server.RESTORE_PROGRESS == 2
    assert (
        json.loads((tmp_path / "restore-metrics.json").read_text())["replayed_actions"]
        == 2
    )


@pytest.mark.parametrize(
    "payload,valid",
    [
        ({"action": None, "control_elapsed_sec": 2}, True),
        ({"action": None, "control_elapsed_sec": 2, "max_lateness_sec": 0.1}, True),
        ({"action": None}, False),
        ({"action": None, "control_elapsed_sec": None}, False),
        ({"action": None, "control_elapsed_sec": 2, "clock": "virtual"}, False),
        (None, False),
    ],
)
def test_scheduled_action_endpoint(monkeypatch, payload, valid):
    server = load_recorder_server(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "status", lambda: {"finished": False})
    monkeypatch.setattr(
        server,
        "SESSION",
        SimpleNamespace(
            submit=lambda action, **kw: calls.append((action, kw)),
            wait_ready=lambda: calls.append("ready"),
        ),
        raising=False,
    )
    handler = server.Handler.__new__(server.Handler)
    data = json.dumps(payload).encode()
    handler.path = "/scheduled-action"
    handler.headers = {"Content-Length": str(len(data))}
    handler.rfile = io.BytesIO(data)
    handler.send = lambda data: calls.append(json.loads(data))
    handler.send_error = lambda code, text: calls.append(code)
    handler.do_POST()
    if valid:
        assert calls == [
            (
                None,
                {
                    "at_control_sec": 2,
                    "max_lateness_sec": payload.get("max_lateness_sec"),
                },
            ),
            "ready",
            {"finished": False},
        ]
    else:
        assert calls == [400]


def test_finish_cancels_scheduled_request_while_it_holds_lock(monkeypatch):
    server = load_recorder_server(monkeypatch)
    pending, cancelled, finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    errors = []

    def wait_ready():
        pending.set()
        if not cancelled.wait(2):
            errors.append("finish blocked behind scheduled request")

    monkeypatch.setattr(server, "status", lambda: {"finished": finished.is_set()})
    monkeypatch.setattr(
        server,
        "SESSION",
        SimpleNamespace(
            submit=lambda *a, **kw: None,
            wait_ready=wait_ready,
            stop_requested=cancelled,
            finish=finished.set,
        ),
        raising=False,
    )

    def post(path, payload):
        handler = server.Handler.__new__(server.Handler)
        data = json.dumps(payload).encode()
        handler.path = path
        handler.headers = {"Content-Length": str(len(data))}
        handler.rfile = io.BytesIO(data)
        handler.send = lambda data: None
        handler.send_error = lambda code, text: errors.append((code, text))
        handler.do_POST()

    worker = threading.Thread(
        target=post,
        args=("/scheduled-action", {"action": None, "control_elapsed_sec": 60}),
    )
    worker.start()
    try:
        assert pending.wait(1)
        post("/finish", {})
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert finished.is_set()
        assert not errors
    finally:
        cancelled.set()
        worker.join(timeout=3)


@pytest.mark.parametrize("shrink", [False, True])
def test_restore_restarts_emulator_only_when_heap_must_shrink(
    monkeypatch, tmp_path, shrink
):
    server = load_recorder_server(monkeypatch)
    calls = []
    metadata = {"trace": [{"action": None}]}
    snapshots = SimpleNamespace(
        store=SimpleNamespace(
            validate_slot=lambda slot: None, read=lambda slot: (tmp_path, metadata)
        ),
        needs_restart=lambda slot: shrink,
        run=lambda op, slot: calls.append((op, slot)) or {"saved_steps": 1},
    )
    session = SimpleNamespace(
        controller="manual",
        started=True,
        finished=False,
        dos_states=snapshots,
        state_dir=tmp_path,
        manifest={"identity": "task"},
        validate_dos_state=lambda value: calls.append("validate"),
        finish=lambda: calls.append("finish"),
    )
    monkeypatch.setattr(server, "SESSION", session, raising=False)
    monkeypatch.setattr(
        server, "ARGS", SimpleNamespace(allow_restart=True), raising=False
    )
    monkeypatch.setattr(server, "archive_attempt", lambda path: Path("archived"))
    monkeypatch.setattr(server, "restart", lambda: calls.append("restart"))
    monkeypatch.setattr(server, "status", dict)
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/restore-state"
    data = b'{"slot":"earlier"}'
    handler.headers = {"Content-Length": str(len(data))}
    handler.rfile, handler.wfile = io.BytesIO(data), io.BytesIO()
    handler.send = lambda value: calls.append(json.loads(value))
    handler.send_error = lambda code, message: pytest.fail(f"{code}: {message}")
    handler.do_POST()
    if shrink:
        assert calls == [
            "validate",
            "finish",
            {"saved_attempt": "archived", "restarting": True},
            "restart",
        ]
        assert json.loads((tmp_path / "restore-plan.json").read_text()) == {
            "identity": "task",
            "kind": "dos-state",
            "slot": "earlier",
            "actions": metadata["trace"],
        }
    else:
        assert calls == [("restore", "earlier"), {"saved_steps": 1}]
        assert not (tmp_path / "restore-plan.json").exists()
