import json
import subprocess
import sys

import pytest

from videogamebench_replayers.cli import SCRIPTS
from videogamebench_replayers.prepare import copy_existing, prepare
from videogamebench_replayers.settings import Settings


@pytest.mark.parametrize("command", ["prepare", *SCRIPTS])
def test_installed_commands_have_help(command):
    result = subprocess.run(
        [sys.executable, "-m", "videogamebench_replayers.cli", command, "--help"],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
    if command == "prepare":
        assert "--from-root" in result.stdout


@pytest.mark.parametrize(
    "emulator,game,asset_name",
    [
        ("gba", "super_mario_land", "super_mario_land.gb"),
        ("dos", "warcraft2", "bundle.jsdos"),
    ],
)
def test_prepare_is_standalone_and_copy_preserves_identity(
    tmp_path, monkeypatch, emulator, game, asset_name
):
    upstream = tmp_path / "upstream"
    (upstream / "src").mkdir(parents=True)
    (upstream / "src/consts.py").write_text(
        "ROM_FILE_MAP = {'super_mario_land': 'super_mario_land.gb'}\n"
    )
    (upstream / "main.py").write_text("# test upstream entry point\n")
    (upstream / "LICENSE").write_text("test fixture\n")
    config = upstream / "configs" / game
    (config / "checkpoints").mkdir(parents=True)
    (config / "config.yaml").write_text(f"emulator: {emulator}\ngame: {game}\n")
    (config / "checkpoints/0.png").write_bytes(b"packaging-test")
    asset = tmp_path / asset_name
    asset.write_bytes(b"synthetic packaging fixture, not a game")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "test-revision\n")
    output = tmp_path / "game"
    result = prepare(upstream, game, asset, output, Settings(), "test-revision")
    assert result["checkpoint_count"] == 1
    assert result["bundle_local"] == (emulator == "dos")
    actual = output / ("upstream/roms" if emulator == "gba" else "") / asset_name
    assert actual.read_bytes() == asset.read_bytes()
    copied = copy_existing(output, tmp_path / "copy")
    assert copied == result
    assert (tmp_path / "copy/result.key").read_bytes() == (
        output / "result.key"
    ).read_bytes()
    assert json.loads((tmp_path / "copy/manifest.json").read_text()) == result
    with pytest.raises(ValueError, match="inside"):
        copy_existing(output, output / "nested")
