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


def valid_rectangle(crop, size):
    return (
        isinstance(crop, list)
        and len(crop) == 4
        and all(type(n) is int for n in crop)
        and 0 <= crop[0] < crop[2] <= size[0]
        and 0 <= crop[1] < crop[3] <= size[1]
    )


def is_dos_action(action):
    return (
        isinstance(action, dict)
        and set(action) == {"action", "action_input"}
        and (action["action"] is None or isinstance(action["action"], str))
        and isinstance(action["action_input"], str)
    )


class VisualChecks:
    def __init__(self, path: Path, action_count: int, log: Path):
        from PIL import Image

        document = json.loads(path.read_text())
        version = document.get("version")
        if (
            type(version) is not int
            or version not in (1, 2)
            or not isinstance(document.get("checks"), list)
        ):
            raise ValueError("Expected version 1 or 2 visual checks")
        self.checks = []
        self.log = log
        substitutions = set()
        for index, check in enumerate(document["checks"]):
            before = check["before_action"]
            if type(before) is not int or not 1 <= before <= action_count + 1:
                raise ValueError(
                    "before_action must identify an action or the end of replay"
                )
            retries = check["max_retries"]
            if type(retries) is not int or retries < 0:
                raise ValueError("max_retries must be a nonnegative integer")
            if "shift_schedule" in check and (
                version < 2 or type(check["shift_schedule"]) is not bool
            ):
                raise ValueError("shift_schedule must be a version 2 boolean")
            aim = check.get("aim_at_match", False)
            if type(aim) is not bool or (
                aim and (version < 2 or before > action_count or retries)
            ):
                raise ValueError(
                    "aim_at_match requires a version 2 pre-action check without retries"
                )
            if "substitute_if_match" in check:
                if (
                    version < 2
                    or before > action_count
                    or before in substitutions
                    or retries
                    or aim
                    or not is_dos_action(check["substitute_if_match"])
                ):
                    raise ValueError(
                        "substitute_if_match requires one valid alternative input "
                        "per action in version 2, without aiming or retries"
                    )
                substitutions.add(before)
            for field in ("wait_sec", "retry_wait_sec", "max_mean_error"):
                number(check[field], field)
            number(check["poll_interval_sec"], "poll_interval_sec", positive=True)
            if check["max_mean_error"] > 255:
                raise ValueError("max_mean_error must be in [0, 255]")
            if "at_elapsed_sec" in check:
                number(check["at_elapsed_sec"], "at_elapsed_sec")
            actions = check["retry_actions"]
            alternatives = check.get("retry_sequences")
            if "retry_sequences" in check:
                if (
                    version < 2
                    or actions != []
                    or not isinstance(alternatives, list)
                    or len(alternatives) != retries
                ):
                    raise ValueError(
                        "retry_sequences requires one sequence per retry in version 2 "
                        "and an empty retry_actions list"
                    )
                sequences = alternatives
            else:
                sequences = [actions]
            for sequence in sequences:
                if not isinstance(sequence, list) or (retries and not sequence):
                    raise ValueError("Retries require a list of recovery actions")
                for action in sequence:
                    if not is_dos_action(action):
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
                if not valid_rectangle(crop, size):
                    raise ValueError(
                        "crop must be a nonempty rectangle inside the reference image"
                    )
                rgb = image.convert("RGB")
                expected = rgb.crop(crop)
                skip = check.get("skip_if")
                skip_patch = None
                if skip is not None:
                    if (
                        version < 2
                        or not isinstance(skip, dict)
                        or not valid_rectangle(skip.get("crop"), size)
                    ):
                        raise ValueError(
                            "skip_if requires a version 2 reference rectangle"
                        )
                    limit = number(skip.get("max_mean_error"), "skip_if.max_mean_error")
                    if limit > 255:
                        raise ValueError("skip_if.max_mean_error must be in [0, 255]")
                    skip_patch = rgb.crop(skip["crop"])
            positions = check.get("match_positions", [crop[:2]])
            if "match_positions" in check and version < 2:
                raise ValueError("match_positions requires version 2 visual checks")
            if not isinstance(positions, list) or not positions:
                raise ValueError("match_positions must be a nonempty list")
            regions = []
            for position in positions:
                if not (
                    isinstance(position, list)
                    and len(position) == 2
                    and all(type(n) is int for n in position)
                    and 0 <= position[0] <= size[0] - expected.width
                    and 0 <= position[1] <= size[1] - expected.height
                ):
                    raise ValueError("Each match position must fit the patch on screen")
                x, y = position
                regions.append((x, y, x + expected.width, y + expected.height))
            self.checks.append(
                {
                    **check,
                    "index": index,
                    "expected": expected,
                    "size": size,
                    "regions": regions,
                    "skip_patch": skip_patch,
                }
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
                skip = check["skip_patch"]
                skipped = False
                if skip is not None:
                    difference = ImageChops.difference(
                        skip, current.crop(check["skip_if"]["crop"])
                    )
                    error = sum(ImageStat.Stat(difference).mean) / 3
                    if error <= check["skip_if"]["max_mean_error"]:
                        matched = skipped = True
                        break
                errors = []
                for region in check["regions"]:
                    difference = ImageChops.difference(
                        check["expected"], current.crop(region)
                    )
                    errors.append(sum(ImageStat.Stat(difference).mean) / 3)
                best = min(range(len(errors)), key=errors.__getitem__)
                error = errors[best]
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
            if matched and not skipped and "substitute_if_match" in check:
                event["substituted_action"] = check["substitute_if_match"]
            if skipped:
                event["skipped"] = True
            elif "match_positions" in check:
                event["closest_position"] = check["regions"][best][:2]
            with self.log.open("a") as output:
                output.write(json.dumps(event) + "\n")
            print("[visual-check] " + json.dumps(event), flush=True)
            if skipped:
                return
            if "substitute_if_match" in check:
                return check["substitute_if_match"] if matched else None
            if check.get("aim_at_match"):
                if matched:
                    x, y, right, bottom = check["regions"][best]
                    point = f"{(x + right) // 2},{(y + bottom) // 2}"
                    status = json.loads(
                        call("/action", {"action": "move", "action_input": point})
                    )
                    if status.get("error") or status.get("finished"):
                        raise RuntimeError(
                            f"Game ended while aiming at a visual target: {status}"
                        )
                return
            if matched:
                return
            if attempt < check["max_retries"]:
                sequence = (
                    check["retry_sequences"][attempt]
                    if "retry_sequences" in check
                    else check["retry_actions"]
                )
                for action in sequence:
                    status = json.loads(call("/action", action))
                    if status.get("error") or status.get("finished"):
                        raise RuntimeError(f"Game ended during recovery: {status}")
        raise RuntimeError(f"Visual check failed: {check.get('name', check['index'])}")
