import importlib.util
import io
import json
from pathlib import Path

import pytest
from PIL import Image

ENVIRONMENT = Path(__file__).parents[1] / "src/videogamebench_replayers/runtime"


def load(name):
    spec = importlib.util.spec_from_file_location(name, ENVIRONMENT / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pixels(color):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def plan(tmp_path):
    (tmp_path / "reference.png").write_bytes(pixels("white"))
    check = {
        "name": "construction",
        "before_action": 2,
        "reference": "reference.png",
        "crop": [0, 0, 8, 8],
        "max_mean_error": 1,
        "wait_sec": 0,
        "poll_interval_sec": 0.1,
        "max_retries": 1,
        "retry_wait_sec": 0,
        "retry_actions": [{"action": "click", "action_input": ""}],
    }
    path = tmp_path / "checks.json"
    path.write_text(json.dumps({"version": 1, "checks": [check]}))
    return path, check


def test_failed_command_is_retried_and_success_is_verified(tmp_path):
    module = load("visual_checks")
    path, _ = plan(tmp_path)
    checks = module.VisualChecks(path, 2, tmp_path / "logs/checks.jsonl")
    actions = []

    def call(endpoint, value=None):
        if endpoint == "/screen?fresh=1":
            return pixels("white" if actions else "black")
        assert endpoint == "/action"
        actions.append(value)
        return b'{"error": null, "finished": false}'

    checks.run(checks.at(2)[0], call)
    assert actions == [{"action": "click", "action_input": ""}]
    events = [json.loads(line) for line in checks.log.read_text().splitlines()]
    assert [event["matched"] for event in events] == [False, True]
    assert all((checks.log.parent / event["screen"]).exists() for event in events)


def test_persistent_mismatch_fails_instead_of_reporting_success(tmp_path):
    module = load("visual_checks")
    path, _ = plan(tmp_path)
    checks = module.VisualChecks(path, 2, tmp_path / "checks.jsonl")
    actions = []

    def call(endpoint, value=None):
        if endpoint == "/screen?fresh=1":
            return pixels("black")
        actions.append(value)
        return b'{"error": null, "finished": false}'

    with pytest.raises(RuntimeError, match="Visual check failed"):
        checks.run(checks.at(2)[0], call)
    assert len(actions) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"poll_interval_sec": 0},
        {"wait_sec": float("nan")},
        {"max_retries": -1},
        {"before_action": 4},
        {"crop": [0, 0, 9, 8]},
        {"reference": "../outside.png"},
        {"max_mean_error": 256},
        {"retry_actions": []},
    ],
)
def test_invalid_checks_are_rejected_before_play(tmp_path, change):
    module = load("visual_checks")
    path, check = plan(tmp_path)
    check.update(change)
    path.write_text(json.dumps({"version": 1, "checks": [check]}))
    with pytest.raises(ValueError):
        module.VisualChecks(path, 2, tmp_path / "checks.jsonl")


def test_guard_delays_shift_the_remaining_schedule(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    client = load("play")
    clock = [10.0]
    monkeypatch.setattr(client.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        client.time, "sleep", lambda value: clock.__setitem__(0, clock[0] + value)
    )
    checked = []

    class Checks:
        def __init__(self, *args):
            pass

        def at(self, index):
            return (
                [{"at_elapsed_sec": 6}] if index == 3 else ([{}] if index == 1 else [])
            )

        def run(self, check, call):
            checked.append(clock[0])
            clock[0] += 2

    monkeypatch.setitem(
        sys.modules, "visual_checks", SimpleNamespace(VisualChecks=Checks)
    )
    trace = tmp_path / "actions.json"
    trace.write_text(
        json.dumps(
            {
                "actions": [
                    {"action": None, "control_elapsed_sec": 1},
                    {"action": None, "control_elapsed_sec": 3},
                ]
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "play",
            "--replay",
            str(trace),
            "--replay-timing",
            "recorded",
            "--replay-scheduler",
            "client",
            "--visual-checks",
            str(tmp_path / "checks.json"),
            "--screen",
            str(tmp_path / "screen.png"),
        ],
    )
    dispatched = []
    finished = []

    def urlopen(request, timeout):
        if request.full_url.endswith("/action"):
            dispatched.append(clock[0])
        elif request.full_url.endswith("/finish"):
            finished.append(clock[0])
        elif request.full_url.endswith(("/screen", "/screen?fresh=1")):
            return io.BytesIO(pixels("white"))
        elif request.full_url.endswith("/status"):
            return io.BytesIO(b'{"control_elapsed_sec": 0}')
        return io.BytesIO(b'{"error": null, "finished": false}')

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    client.main()
    assert dispatched == [13, 15]
    assert checked == [11, 18]
    assert finished == [20]


def test_failed_milestone_stops_before_next_recorded_input(tmp_path, monkeypatch):
    import sys

    client = load("play")
    monkeypatch.setitem(sys.modules, "visual_checks", load("visual_checks"))
    path, check = plan(tmp_path)
    check.update(name="mission-victory", max_retries=0, retry_actions=[])
    path.write_text(json.dumps({"version": 1, "checks": [check]}))
    trace = tmp_path / "actions.json"
    trace.write_text(json.dumps({"actions": [{"action": None}] * 3}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "play",
            "--replay",
            str(trace),
            "--visual-checks",
            str(path),
            "--check-log",
            str(tmp_path / "logs/checks.jsonl"),
            "--screen",
            str(tmp_path / "screen.png"),
        ],
    )
    endpoints = []

    def urlopen(request, timeout):
        endpoint = request.full_url.rsplit("/", 1)[-1]
        endpoints.append(endpoint)
        if endpoint in {"screen", "screen?fresh=1"}:
            return io.BytesIO(pixels("black"))
        return io.BytesIO(b'{"error": null, "finished": false}')

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match="mission-victory"):
        client.main()
    assert endpoints.count("action") == 1
    assert endpoints[-1] == "finish"
    event = json.loads((tmp_path / "logs/checks.jsonl").read_text())
    assert event["before_action"] == 2
    assert not event["matched"]


def test_waiting_for_live_condition_does_not_reuse_cached_frame(tmp_path, monkeypatch):
    module = load("visual_checks")
    path, check = plan(tmp_path)
    check.update(wait_sec=1, max_retries=0, retry_actions=[])
    path.write_text(json.dumps({"version": 1, "checks": [check]}))
    checks = module.VisualChecks(path, 2, tmp_path / "log.jsonl")
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        module.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    requests = []

    def call(endpoint, value=None):
        requests.append(endpoint)
        # The cached frame stays black even after the live game changes.
        return pixels(
            "white" if endpoint == "/screen?fresh=1" and clock[0] >= 0.2 else "black"
        )

    checks.run(checks.at(2)[0], call)
    assert requests == ["/screen?fresh=1"] * 3
    assert json.loads(checks.log.read_text())["matched"] is True
