"""Create a standalone game folder or copy an existing compatible one."""

import argparse
import ast
import hashlib
import json
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

from .settings import REVISION, Settings

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".venv", "logs")


def copy_existing(source, output):
    source = source.resolve()
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("emulator") not in {"gba", "dos"}:
        raise ValueError("Expected a Game Boy or DOS game folder")
    for path in (source / "result.key", source / "upstream/main.py"):
        if not path.is_file():
            raise ValueError(f"Missing game file: {path}")
    if output.resolve().is_relative_to(source):
        raise ValueError("Output must not be inside the source game folder")
    shutil.copytree(source, output, ignore=IGNORE)
    return manifest


def prepare(source, game, asset, output, settings, revision):
    source = source.resolve()
    actual = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != revision:
        raise ValueError(f"Expected upstream revision {revision}; found {actual}")
    if not game or Path(game).name != game or game in {".", ".."}:
        raise ValueError("Invalid game name")
    config = source / "configs" / game
    emulator = yaml.safe_load((config / "config.yaml").read_text())["emulator"]
    if emulator not in {"gba", "dos"}:
        raise ValueError("Unsupported emulator")
    if not asset.is_file():
        raise FileNotFoundError(asset)
    name = "bundle.jsdos"
    if emulator == "gba":
        tree = ast.parse((source / "src/consts.py").read_text())
        mapping = next(
            ast.literal_eval(n.value)
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "ROM_FILE_MAP" for t in n.targets
            )
        )
        name = mapping[game]
    if output.resolve().is_relative_to(source):
        raise ValueError("Output must not be inside the upstream checkout")
    output.mkdir(parents=True, exist_ok=False)
    upstream = output / "upstream"
    shutil.copytree(source / "src", upstream / "src", ignore=IGNORE)
    shutil.copytree(config, upstream / "configs" / game, ignore=IGNORE)
    shutil.copyfile(source / "main.py", upstream / "main.py")
    shutil.copyfile(source / "LICENSE", output / "UPSTREAM_LICENSE")
    source_hash = hashlib.sha256()
    for path in sorted(p for p in upstream.rglob("*") if p.is_file()):
        source_hash.update(str(path.relative_to(upstream)).encode())
        source_hash.update(path.read_bytes())
    destination = output / name
    if emulator == "gba":
        (upstream / "roms").mkdir()
        destination = upstream / "roms" / name
    shutil.copyfile(asset, destination)
    with asset.open("rb") as stream:
        asset_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "game": game,
        "task_id": f"videogamebench-{game.replace('_', '-')}-{settings.mode}",
        "emulator": emulator,
        "settings": settings.to_dict(),
        "upstream_revision": actual,
        "source_sha256": source_hash.hexdigest(),
        "assets": {name: asset_hash},
        "checkpoint_count": len(list((config / "checkpoints").glob("*.png"))),
        "bundle_local": emulator == "dos",
    }
    manifest["identity"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "result.key").write_text(secrets.token_hex(32))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--from-root",
        type=Path,
        help="Copy an existing game folder, preserving recording identity",
    )
    source.add_argument("--upstream", type=Path)
    parser.add_argument("--game")
    parser.add_argument("--asset", type=Path, help="Licensed ROM or .jsdos bundle")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--settings",
        type=Path,
        help="JSON settings overrides; all runtime parameters are configurable",
    )
    parser.add_argument("--revision", default=REVISION)
    args = parser.parse_args(argv)
    if args.from_root:
        if args.settings or args.asset or args.game:
            parser.error("Copy mode preserves the original settings and asset identity")
        manifest = copy_existing(args.from_root, args.output_dir)
    else:
        if not args.game or not args.asset:
            parser.error("--upstream requires --game and --asset")
        values = json.loads(args.settings.read_text()) if args.settings else {}
        config = yaml.safe_load(
            (args.upstream / "configs" / args.game / "config.yaml").read_text()
        )
        values.setdefault("mode", "realtime" if config["emulator"] == "dos" else "lite")
        manifest = prepare(
            args.upstream,
            args.game,
            args.asset,
            args.output_dir,
            Settings.from_dict(values),
            args.revision,
        )
    print(
        json.dumps(
            {
                "root": str(args.output_dir.resolve()),
                "identity": manifest["identity"],
                "emulator": manifest["emulator"],
            },
            indent=2,
        )
    )
