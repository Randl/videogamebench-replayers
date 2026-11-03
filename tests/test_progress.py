import json
from pathlib import Path

import pytest


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
                "game": "test_game",
                "checkpoint_count": 2,
                "settings": {"record_video": False, "max_steps": 100},
            }
        )
    )
    return Session(root, tmp_path / "state")


def test_private_score_logs_and_throttling(session, capsys):
    session.controller = "manual"
    session.log_progress("started", force=True)
    session.log_progress()  # The immediate periodic update is suppressed.
    session.steps = 15
    session.checkpoint_index = 2
    session.completed = True
    session.log_progress("checkpoint", force=True)
    session.error = "emulator failed"
    session.log_progress("finished", force=True, status="infrastructure_error")
    records = [
        json.loads(line)
        for line in (session.state_dir / "progress.jsonl").read_text().splitlines()
    ]
    assert [r["event"] for r in records] == ["started", "checkpoint", "finished"]
    assert [r["score"] for r in records] == [0, 1, None]
    assert records[1]["steps"] == 15
    assert records[1]["checkpoint_index"] == 2
    assert records[2]["error"] == "emulator failed"
    assert len(capsys.readouterr().out.splitlines()) == 3
    assert not {"score", "checkpoint_index", "completion"} & session.status().keys()


def test_unscored_game_and_disabled_periodic_logs(session):
    session.log_interval_sec = 0
    session.manifest["checkpoint_count"] = 0
    session.log_progress()
    assert not (session.state_dir / "progress.jsonl").exists()
    session.log_progress("finished", force=True, status="stopped")
    result = json.loads((session.state_dir / "progress.jsonl").read_text())
    assert result["score"] is None
    assert result["status"] == "stopped"


@pytest.mark.parametrize("transport_delay", [0, 0.06])
@pytest.mark.parametrize("scheduler", ["client", "server"])
@pytest.mark.parametrize("timing", ["immediate", "recorded"])
@pytest.mark.parametrize("startup_elapsed", [0, 0.75, 4])
def test_replay_progress_preserves_inputs(
    tmp_path, monkeypatch, capsys, timing, startup_elapsed, scheduler, transport_delay
):
    import importlib.util
    import io
    import sys

    path = (
        Path(__file__).parents[1]
        / "src/videogamebench_replayers/runtime/play.py"
    )
    spec = importlib.util.spec_from_file_location("replay_progress_test", path)
    assert spec is not None and spec.loader is not None
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    trace = tmp_path / "actions.json"
    trace.write_text(
        json.dumps(
            {
                "actions": [
                    {"action": None, "control_elapsed_sec": 1},
                    {"action": {"UP": True}, "control_elapsed_sec": 3},
                ]
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(path),
            "--replay",
            str(trace),
            "--screen",
            str(tmp_path / "screen.png"),
            "--progress-interval",
            "0.000001",
            "--replay-timing",
            timing,
            "--replay-scheduler",
            scheduler,
        ],
    )
    received = []
    dispatch_times = []
    submission_times = []
    clock = [10.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    )

    def urlopen(request, timeout):
        if request.full_url.endswith("/scheduled-action"):
            payload = json.loads(request.data)
            submission_times.append(clock[0])
            clock[0] += transport_delay
            clock[0] = max(
                clock[0], 10 + payload["control_elapsed_sec"] - startup_elapsed
            )
            received.append(payload["action"])
            dispatch_times.append(clock[0])
            clock[0] += 0.25
        elif request.full_url.endswith("/action"):
            clock[0] += transport_delay
            received.append(json.loads(request.data))
            dispatch_times.append(clock[0])
            clock[0] += 0.25
        if request.full_url.endswith("/screen"):
            return io.BytesIO(b"screen")
        return io.BytesIO(
            json.dumps(
                {
                    "steps": len(received),
                    "finished": False,
                    "error": None,
                    "control_elapsed_sec": startup_elapsed,
                }
            ).encode()
        )

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    client.main()
    assert received == [None, {"UP": True}]
    if timing == "immediate":
        first = 10 + transport_delay
        expected = [first, first + 0.25 + transport_delay]
    elif scheduler == "client":
        first = max(10, 11 - startup_elapsed) + transport_delay
        expected = [first, max(first + 0.25, 13 - startup_elapsed) + transport_delay]
    else:
        first = max(10 + transport_delay, 11 - startup_elapsed)
        expected = [first, max(first + 0.25 + transport_delay, 13 - startup_elapsed)]
    assert dispatch_times == pytest.approx(expected)
    if timing == "recorded" and scheduler == "server":
        assert submission_times == [10, dispatch_times[0] + 0.25]
    output = capsys.readouterr().err
    assert "Replay 2/2 (100.0%)" in output
    assert "ETA" in output
    assert "server state directory" in output


def test_recorded_replay_timing_validation():
    import importlib.util

    path = (
        Path(__file__).parents[1]
        / "src/videogamebench_replayers/runtime/play.py"
    )
    spec = importlib.util.spec_from_file_location("timed_replay_test", path)
    assert spec is not None and spec.loader is not None
    client = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(client)
    assert client.replay_offsets(
        {
            "actions": [
                {"action": None, "control_elapsed_sec": 2},
                {"action": None, "control_elapsed_sec": 5},
            ]
        },
        "recorded",
    ) == [2, 5]
    for offsets in ([2, 1], [float("nan")], [-1], [None]):
        with pytest.raises(ValueError, match="Recorded timing"):
            client.replay_offsets(
                {"actions": [{"control_elapsed_sec": v} for v in offsets]}, "recorded"
            )
    assert client.replay_offsets({"actions": [{"action": None}]}, "immediate") == [0]


@pytest.mark.parametrize("record_observations", [False, True])
def test_dos_live_observations_do_not_inject_actions(session, record_observations):
    import asyncio
    import time
    from types import SimpleNamespace

    async def run():
        session.manifest["emulator"] = "dos"
        session.manual_observation_fps = 60
        session.record_action_observations = record_observations
        session.started_at = time.monotonic()
        observations = []

        async def observe():
            observations.append(True)
            return b"frame"

        session.evaluator = SimpleNamespace(
            game=SimpleNamespace(get_observation=observe)
        )
        task = asyncio.create_task(session.next_action())
        await asyncio.sleep(0.06)
        action = {"action": "press_key", "action_input": "Enter"}
        session.actions.put(action)
        assert await asyncio.wait_for(task, 1) == action
        assert observations
        assert len(session.trace) == 1
        assert session.trace[0]["action"] == action
        assert session.trace[0]["control_elapsed_sec"] >= 0.05
        assert session.screen == b"frame"
        if record_observations:
            reference = session.trace[0]["before_observation"]
            assert reference["source"] == "latest_published_frame"
            assert reference["age_sec"] >= 0
            assert (
                session.state_dir / ".recording-observations" / reference["file"]
            ).read_bytes() == b"frame"
        else:
            assert "before_observation" not in session.trace[0]
            assert not (session.state_dir / ".recording-observations").exists()

    asyncio.run(run())


def test_dos_state_transaction_quiesces_observation_loop(session):
    import asyncio
    import time
    from types import SimpleNamespace

    async def run():
        session.manifest["emulator"] = "dos"
        session.manual_observation_fps = 60
        session.started_at = time.monotonic()
        observations = []

        async def observe():
            observations.append(True)
            return b"frame"

        session.evaluator = SimpleNamespace(
            game=SimpleNamespace(get_observation=observe)
        )
        task = asyncio.create_task(session.next_action())
        try:
            session.dos_state_busy = True
            for _ in range(100):
                if session.dos_state_quiet.is_set():
                    break
                await asyncio.sleep(0.01)
            assert session.dos_state_quiet.is_set()
            count = len(observations)
            await asyncio.sleep(0.05)
            assert len(observations) == count
            assert session.trace == []
            session.dos_state_busy = False
            await asyncio.sleep(0.05)
            assert len(observations) > count
        finally:
            session.dos_state_busy = False
            session.actions.put({"action": None, "action_input": ""})
            await asyncio.wait_for(task, 1)

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_scheduled_input_waits_after_recorder_work_and_can_stop(session, cancel):
    import asyncio
    import time

    from videogamebench_replayers.runtime.session import StopEpisode

    async def run():
        session.manifest["emulator"] = "dos"
        session.controller = "manual"
        session.started_at = session.control_started_at = time.monotonic()
        session.screen = b"frame"
        session.screen_published_at = session.started_at
        session.record_action_observations = True
        session.record_observation = lambda *a, **kw: {"age_sec": 0}
        task = asyncio.create_task(session.next_action())
        while not session.waiting:
            await asyncio.sleep(0)
        action = {"action": "click", "action_input": ""}
        deadline = 60 if cancel else 0.08
        session.submit(action, at_control_sec=deadline)
        await asyncio.sleep(0.02)
        assert not task.done()
        assert session.trace == []
        if cancel:
            session.stop_requested.set()
            with pytest.raises(StopEpisode):
                await asyncio.wait_for(task, 1)
            assert session.trace == []
        else:
            assert await asyncio.wait_for(task, 1) == action
            item = session.trace[-1]
            assert item["control_elapsed_sec"] >= deadline
            assert item["scheduled_control_elapsed_sec"] == deadline
            assert item["dispatch_lateness_sec"] >= 0
            assert item["before_observation"]["age_sec"] >= deadline
            assert (
                session.status()["last_action_timing"]["scheduled_control_elapsed_sec"]
                == deadline
            )

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_scheduled_dos_wait_refreshes_frames_without_executing_early(session, cancel):
    import asyncio
    import time
    from types import SimpleNamespace

    from videogamebench_replayers.runtime.session import StopEpisode

    async def run():
        session.manifest["emulator"] = "dos"
        session.controller = "manual"
        session.manual_observation_fps = 60
        session.record_action_observations = True
        session.started_at = session.control_started_at = time.monotonic()
        observed = asyncio.Event()
        frames = []

        async def observe():
            frame = f"frame-{len(frames)}".encode()
            frames.append(frame)
            observed.set()
            return frame

        session.evaluator = SimpleNamespace(
            game=SimpleNamespace(get_observation=observe)
        )
        task = asyncio.create_task(session.next_action())
        while not session.waiting:
            await asyncio.sleep(0)
        action = {"action": "click", "action_input": ""}
        deadline = 60 if cancel else 0.2
        session.submit(action, at_control_sec=deadline)
        await asyncio.wait_for(observed.wait(), 1)
        assert not session.waiting  # The future input has left the queue.
        assert not task.done()
        assert session.trace == []
        assert session.steps == 0
        assert session.screen == frames[-1]
        if cancel:
            session.stop_requested.set()
            with pytest.raises(StopEpisode):
                await asyncio.wait_for(task, 1)
            assert session.trace == []
        else:
            assert await asyncio.wait_for(task, 1) == action
            assert len(frames) >= 2
            assert len(session.trace) == 1
            item = session.trace[0]
            assert item["control_elapsed_sec"] >= deadline
            saved = session.state_dir / ".recording-observations"
            assert (saved / item["before_observation"]["file"]).read_bytes() == frames[
                -1
            ]

    asyncio.run(run())


def test_late_scheduled_input_fails_before_execution(session):
    import asyncio
    import time

    from videogamebench_replayers.runtime.session import InfrastructureFailure

    async def run():
        session.manifest["emulator"] = "dos"
        session.controller = "manual"
        session.started_at = session.control_started_at = time.monotonic() - 10
        task = asyncio.create_task(session.next_action())
        while not session.waiting:
            await asyncio.sleep(0)
        session.submit(
            {"action": "click", "action_input": ""},
            at_control_sec=1,
            max_lateness_sec=0.1,
        )
        with pytest.raises(
            InfrastructureFailure, match="missed its live-clock deadline"
        ):
            await asyncio.wait_for(task, 1)
        assert session.trace == []

    asyncio.run(run())


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "1"])
@pytest.mark.parametrize("field", ["at_control_sec", "max_lateness_sec"])
def test_scheduled_input_rejects_invalid_deadlines(session, value, field):
    session.controller = "manual"
    session.waiting = True
    kwargs = {"at_control_sec": 1, field: value}
    with pytest.raises(ValueError, match="finite and nonnegative"):
        session.submit({"action": "click", "action_input": ""}, **kwargs)
    assert session.actions.empty()
    assert session.waiting




def test_fresh_observation_advances_image_without_inputs(session):
    import asyncio
    import time
    from types import SimpleNamespace

    async def run():
        session.manifest["emulator"] = "dos"
        session.controller = "manual"
        session.started_at = time.monotonic()
        session.publish(b"old frame")
        reads = []

        async def observe():
            reads.append(True)
            return b"new frame"

        session.evaluator = SimpleNamespace(
            game=SimpleNamespace(get_observation=observe)
        )
        task = asyncio.create_task(session.next_action())
        try:
            while not session.waiting:
                await asyncio.sleep(0)
            assert await asyncio.to_thread(session.observe_fresh, 1) == b"new frame"
            assert session.screen == b"new frame"
            assert reads == [True]
            assert session.steps == 0
            assert session.trace == []
            assert session.actions.empty()
        finally:
            session.actions.put({"action": None, "action_input": ""})
            await asyncio.wait_for(task, 1)

    asyncio.run(run())


@pytest.mark.parametrize(
    "bounds", [None, {"left": 77.328125, "top": 0, "right": 610.65625, "bottom": 400}]
)
def test_pointer_bounds_inspection_does_not_execute_inputs(session, bounds):
    import asyncio
    import time
    from types import SimpleNamespace

    async def run():
        session.manifest["emulator"] = "dos"
        session.controller = "manual"
        session.started_at = time.monotonic()

        async def evaluate(script):
            assert "getBoundingClientRect" in script
            return bounds

        session.evaluator = SimpleNamespace(
            game=SimpleNamespace(
                browser=SimpleNamespace(page=SimpleNamespace(evaluate=evaluate))
            )
        )
        task = asyncio.create_task(session.next_action())
        try:
            while not session.waiting:
                await asyncio.sleep(0)
            assert await asyncio.to_thread(session.pointer_bounds, 1) == bounds
            assert session.steps == 0
            assert session.trace == []
            assert session.actions.empty()
        finally:
            session.actions.put({"action": None, "action_input": ""})
            await asyncio.wait_for(task, 1)

    asyncio.run(run())
