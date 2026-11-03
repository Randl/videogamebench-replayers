"""Optional observation checks for an oracle policy, using ordinary game actions."""

import io
import json
import math
import time
from pathlib import Path


def number(value, name, *, positive=False):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValueError(
            f"{name} must be finite and {'positive' if positive else 'nonnegative'}"
        )
    return value


class VisualChecks:
    def __init__(self, path: Path, action_count: int, log: Path):
        from PIL import Image

        document = json.loads(path.read_text())
        if document.get("version") != 1 or not isinstance(document.get("checks"), list):
            raise ValueError("Expected version 1 visual checks")
        self.checks = []
        self.log = log
        for index, check in enumerate(document["checks"]):
            before = check["before_action"]
            if type(before) is not int or not 1 <= before <= action_count + 1:
                raise ValueError(
                    "before_action must identify an action or the end of replay"
                )
            retries = check["max_retries"]
            if type(retries) is not int or retries < 0:
                raise ValueError("max_retries must be a nonnegative integer")
            for field in ("wait_sec", "retry_wait_sec", "max_mean_error"):
                number(check[field], field)
            number(check["poll_interval_sec"], "poll_interval_sec", positive=True)
            if check["max_mean_error"] > 255:
                raise ValueError("max_mean_error must be in [0, 255]")
            if "at_elapsed_sec" in check:
                number(check["at_elapsed_sec"], "at_elapsed_sec")
            actions = check["retry_actions"]
            if not isinstance(actions, list) or (retries and not actions):
                raise ValueError("Retries require a list of recovery actions")
            for action in actions:
                if (
                    not isinstance(action, dict)
                    or set(action) != {"action", "action_input"}
                    or not (
                        action["action"] is None or isinstance(action["action"], str)
                    )
                    or not isinstance(action["action_input"], str)
                ):
                    raise ValueError(
                        "Recovery actions must be DOS action/input objects"
                    )
            reference = (path.parent / check["reference"]).resolve()
            if not reference.is_relative_to(path.parent.resolve()):
                raise ValueError(
                    "Reference image must be inside the visual-check bundle"
                )
            with Image.open(reference) as image:
                size = image.size
                crop = check["crop"]
                if (
                    not isinstance(crop, list)
                    or len(crop) != 4
                    or any(type(n) is not int for n in crop)
                    or not 0 <= crop[0] < crop[2] <= size[0]
                    or not 0 <= crop[1] < crop[3] <= size[1]
                ):
                    raise ValueError(
                        "crop must be a nonempty rectangle inside the reference image"
                    )
                expected = image.convert("RGB").crop(crop)
            self.checks.append(
                {**check, "index": index, "expected": expected, "size": size}
            )

    def at(self, before_action):
        return [
            check for check in self.checks if check["before_action"] == before_action
        ]

    def run(self, check, call):
        from PIL import Image, ImageChops, ImageStat

        self.log.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(check["max_retries"] + 1):
            deadline = time.monotonic() + (
                check["wait_sec"] if attempt == 0 else check["retry_wait_sec"]
            )
            while True:
                with Image.open(io.BytesIO(call("/screen?fresh=1"))) as image:
                    if image.size != check["size"]:
                        raise RuntimeError(
                            "Live screenshot dimensions differ from the reference"
                        )
                    current = image.convert("RGB")
                difference = ImageChops.difference(
                    check["expected"], current.crop(check["crop"])
                )
                error = sum(ImageStat.Stat(difference).mean) / 3
                matched = error <= check["max_mean_error"]
                if matched or time.monotonic() >= deadline:
                    break
                time.sleep(
                    min(check["poll_interval_sec"], max(0, deadline - time.monotonic()))
                )
            screen = (
                self.log.parent
                / f"check-{check['index']:03d}-attempt-{attempt:03d}.png"
            )
            current.save(screen)
            event = {
                "check": check.get("name", str(check["index"])),
                "before_action": check["before_action"],
                "attempt": attempt,
                "mean_error": error,
                "matched": matched,
                "screen": screen.name,
            }
            with self.log.open("a") as output:
                output.write(json.dumps(event) + "\n")
            print("[visual-check] " + json.dumps(event), flush=True)
            if matched:
                return
            if attempt < check["max_retries"]:
                for action in check["retry_actions"]:
                    status = json.loads(call("/action", action))
                    if status.get("error") or status.get("finished"):
                        raise RuntimeError(f"Game ended during recovery: {status}")
        raise RuntimeError(f"Visual check failed: {check.get('name', check['index'])}")
