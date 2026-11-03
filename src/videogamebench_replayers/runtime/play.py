"""Visual game client for CLI agents, replay candidates and the VG-Agent shim."""

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path


def request(url, path, value=None, timeout=900):
    body = json.dumps(value).encode() if value is not None else None
    with urllib.request.urlopen(
        urllib.request.Request(url + path, data=body), timeout=timeout
    ) as response:
        return response.read()


def replay_offsets(trace, timing):
    actions = trace["actions"] if isinstance(trace, dict) else trace
    if timing == "immediate":
        return [0.0] * len(actions)
    if not isinstance(trace, dict) or any(
        not isinstance(item, dict) for item in actions
    ):
        raise ValueError("Recorded timing requires a trace with timed action records")
    offsets = [item.get("control_elapsed_sec") for item in actions]
    if any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0
        for value in offsets
    ) or offsets != sorted(offsets):
        raise ValueError(
            "Recorded timing requires ordered control_elapsed_sec on every action"
        )
    return offsets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=10,
        help="Seconds between replay progress messages; 0 disables periodic messages",
    )
    parser.add_argument("--screen", type=Path, default=Path("screen.png"))
    parser.add_argument("--visual-checks", type=Path)
    parser.add_argument(
        "--check-log", type=Path, default=Path("visual-checks.jsonl")
    )
    parser.add_argument(
        "--replay-timing", choices=["immediate", "recorded"], default="immediate"
    )
    parser.add_argument(
        "--replay-scheduler",
        choices=["server", "client"],
        default="server",
        help="Schedule recorded inputs on the game's live clock (client: legacy dispatch)",
    )
    parser.add_argument(
        "--max-replay-lateness-sec",
        type=float,
        help="Abort before an input if its server execution deadline is missed by this much",
    )
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--buttons", nargs="+")
    commands.add_argument("--no-op", action="store_true")
    commands.add_argument(
        "--action", help='DOS JSON: {"action":"press_key","action_input":"ArrowRight"}'
    )
    commands.add_argument("--replay", type=Path)
    commands.add_argument("--finish", action="store_true")
    commands.add_argument("--vgagent", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
    args = parser.parse_args()
    if not math.isfinite(args.progress_interval) or args.progress_interval < 0:
        parser.error("--progress-interval must be finite and nonnegative")
    if args.max_replay_lateness_sec is not None and (
        not math.isfinite(args.max_replay_lateness_sec)
        or args.max_replay_lateness_sec < 0
        or args.replay_timing != "recorded"
        or args.replay_scheduler != "server"
    ):
        parser.error(
            "--max-replay-lateness-sec requires server-scheduled recorded timing "
            "and a finite nonnegative value"
        )

    def call(path, value=None):
        return request(args.url, path, value, args.timeout)

    if args.finish:
        call("/finish", {})
        return
    if args.vgagent:
        call(
            "/start",
            {
                "controller": "vgagent",
                "api_key": os.environ.get(args.api_key_env),
                "model": args.model,
            },
        )
        while True:
            status = json.loads(call("/status"))
            if status["finished"]:
                if status["error"]:
                    raise RuntimeError(status["error"])
                return
            time.sleep(args.poll_interval)
    trace = json.loads(args.replay.read_text()) if args.replay else None
    offsets = replay_offsets(trace, args.replay_timing) if args.replay else []
    checks = None
    if args.visual_checks:
        if not args.replay:
            parser.error("--visual-checks requires --replay")
        from visual_checks import VisualChecks

        checks = VisualChecks(args.visual_checks, len(offsets), args.check_log)
    args.screen.parent.mkdir(parents=True, exist_ok=True)
    args.screen.write_bytes(call("/screen"))
    if args.replay:
        # A trace is a candidate policy: each action is executed and scored anew.
        actions = trace["actions"] if isinstance(trace, dict) else trace
        started = last_progress = time.monotonic()
        if args.replay_timing == "recorded":
            # The game is already advancing while the first screenshot travels
            # to this client. Anchor input times to its control clock instead
            # of adding that startup delay to every recorded input.
            requested_at = time.monotonic()
            status = json.loads(call("/status"))
            received_at = time.monotonic()
            elapsed = status["control_elapsed_sec"]
            if (
                type(elapsed) not in (int, float)
                or not math.isfinite(elapsed)
                or elapsed < 0
            ):
                raise ValueError("Invalid game control clock")
            started = (requested_at + received_at) / 2 - elapsed
        schedule_shift = 0
        server_scheduled = (
            args.replay_timing == "recorded" and args.replay_scheduler == "server"
        )
        maximum_lateness = 0
        total = len(actions)

        def run_checks(before_action):
            nonlocal schedule_shift
            if checks is None:
                return
            for check in checks.at(before_action):
                checked_at = time.monotonic()
                if "at_elapsed_sec" in check:
                    time.sleep(
                        max(
                            0,
                            started
                            + schedule_shift
                            + check["at_elapsed_sec"]
                            - time.monotonic(),
                        )
                    )
                try:
                    checks.run(check, call)
                except Exception:
                    call("/finish", {})
                    raise
                # Recovery is part of the oracle policy. Keep subsequent
                # recorded input gaps instead of compressing overdue actions.
                schedule_shift += time.monotonic() - checked_at

        print(
            f"Replaying {total} actions from a fresh game…", file=sys.stderr, flush=True
        )
        for index, item in enumerate(actions, 1):
            action = item["action"] if isinstance(trace, dict) else item
            delay = offsets[index - 1] + schedule_shift - (time.monotonic() - started)
            # Send future inputs early. The game process waits until execution
            # time, avoiding transport/recorder work after the target deadline.
            if delay > 0 and (
                not server_scheduled or (checks is not None and checks.at(index))
            ):
                time.sleep(delay)
            run_checks(index)
            endpoint = "/scheduled-action" if server_scheduled else "/action"
            payload = (
                {
                    "action": action,
                    "control_elapsed_sec": offsets[index - 1] + schedule_shift,
                    "max_lateness_sec": args.max_replay_lateness_sec,
                }
                if server_scheduled
                else action
            )
            # JSON null is an action, so send it explicitly (GET has no body).
            with urllib.request.urlopen(
                urllib.request.Request(
                    args.url + endpoint, data=json.dumps(payload).encode()
                ),
                timeout=args.timeout,
            ) as response:
                status = json.load(response)
            maximum_lateness = max(
                maximum_lateness,
                status.get("last_action_timing", {}).get("dispatch_lateness_sec", 0),
            )
            now = time.monotonic()
            if (
                args.progress_interval and now - last_progress >= args.progress_interval
            ) or index == total:
                elapsed = now - started
                rate = index / elapsed if elapsed else 0
                remaining = (
                    max(0, offsets[-1] + schedule_shift - elapsed)
                    if args.replay_timing == "recorded"
                    else ((total - index) / rate if rate else 0)
                )
                print(
                    f"Replay {index}/{total} ({index / total:.1%}) | "
                    f"{rate:.2f} actions/s | elapsed {elapsed / 60:.1f} min | "
                    f"ETA {remaining / 60:.1f} min"
                    + (
                        f" | max start delay {maximum_lateness * 1000:.1f} ms"
                        if server_scheduled
                        else ""
                    ),
                    file=sys.stderr,
                    flush=True,
                )
                last_progress = now
            if status.get("error"):
                raise RuntimeError(status["error"])
            if status["finished"]:
                break
        run_checks(total + 1)
        call("/finish", {})
        print(
            "Replay finished. See result.json and progress.jsonl in the server state directory.",
            file=sys.stderr,
            flush=True,
        )
    elif args.buttons or args.no_op or args.action:
        value = (
            json.loads(args.action)
            if args.action
            else ({key: True for key in args.buttons} if args.buttons else None)
        )
        with urllib.request.urlopen(
            urllib.request.Request(
                args.url + "/action", data=json.dumps(value).encode()
            ),
            timeout=args.timeout,
        ) as response:
            response.read()
    args.screen.write_bytes(call("/screen"))
    print(args.screen)


if __name__ == "__main__":
    main()
