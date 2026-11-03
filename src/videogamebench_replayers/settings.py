"""Reproducible experiment parameters; defaults follow upstream main.py."""

import math
from dataclasses import asdict, dataclass, fields

REVISION = "faab53179cb4babdd102f35cb83c24a5f2c29d05"
TEST_GAMES = (
    "doom2",
    "pokemon_crystal",
    "zelda",
    "civ",
    "need_for_speed",
    "kirby",
    "incredible-machine",
)
DEV_GAMES = (
    "doom",
    "quake",
    "warcraft2",
    "oregon_trail",
    "x-com",
    "prince",
    "age_of_empires",
    "pokemon_red",
    "super_mario_land",
    "mega_man",
    "donkey_kong",
    "castlevania",
    "scooby_doo",
)
PARITY_GAMES = TEST_GAMES


@dataclass
class Settings:
    mode: str = "lite"
    max_steps: int = 15000
    action_frames: int = 15
    skip_frames: int = 1
    step_delay: float = 0.0
    threshold: float | None = None
    model: str = "gpt-4o-2024-08-06"
    temperature: float = 0.7
    max_tokens: int = 1024
    history_tokens: int = 4000
    max_context_size: int = 20
    num_screenshots_per_action: int = 0
    max_cost: float = 30.0
    api_base: str | None = None
    api_key_env: str = "OPENAI_API_KEY"
    port: int = 8000
    record_video: bool = True
    video_fps: float = 5.0
    seed: int | None = None

    def __post_init__(self):
        if (
            type(self.record_video) is not bool
            or not isinstance(self.video_fps, (int, float))
            or not math.isfinite(self.video_fps)
            or not 0 < self.video_fps <= 60
        ):
            raise ValueError(
                "record_video must be boolean and video_fps must be in (0, 60]"
            )
        if self.mode not in {"lite", "realtime"}:
            raise ValueError("mode must be lite or realtime")
        for name in (
            "max_steps",
            "action_frames",
            "skip_frames",
            "max_tokens",
            "history_tokens",
            "max_context_size",
            "port",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("step_delay", "temperature", "max_cost", "threshold"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be finite and nonnegative")
        if (
            type(self.num_screenshots_per_action) is not int
            or self.num_screenshots_per_action < 0
        ):
            raise ValueError("num_screenshots_per_action must be nonnegative")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("seed must be an integer or null")
        if not 1 <= self.port <= 65535 or self.port == 8080:
            raise ValueError(
                "port must be available and distinct from control port 8080"
            )
        if not self.model or not self.api_key_env:
            raise ValueError("model and api_key_env cannot be empty")

    @classmethod
    def from_dict(cls, values):
        unknown = values.keys() - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"Unknown settings: {sorted(unknown)}")
        return cls(**values)

    def to_dict(self):
        return asdict(self)
