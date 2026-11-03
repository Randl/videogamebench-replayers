# VideoGameBench Replayers

Standalone **Game Boy** and **DOS** browser recorders and replayers, extracted from
the VideoGameBench adapter. Harbor is not required. Both support gameplay video,
headless servers, browser input, recording export, and restoring saved progress.

Game Boy Lite replays the original fixed-frame button inputs, with cached states,
rewind, slow play, and single-action stepping. DOS execution replay captures native
input order and clock readings; action-only DOS traces are not generally deterministic.
DOS state restoration preserves memory, filesystem, and continuation state. Restoring
a smaller WASM heap reloads the emulator because WASM memory cannot shrink.

## Install

Use Linux, Python 3.12+, uv, and Node.js (for the JavaScript regression tests).
System packages needed for headless execution include FFmpeg, SDL2, and, for stock
upstream's headed browser, Xvfb and xauth. For Debian/Ubuntu:

```bash
sudo apt-get install ffmpeg libsdl2-2.0-0 xvfb xauth
uv sync --extra runtime
uv run playwright install --with-deps chromium
uv run vgb-replayers --help
```

## Prepare game folders

The game engine remains an external VideoGameBench checkout, pinned by default to
`faab53179cb4babdd102f35cb83c24a5f2c29d05` from
<https://github.com/alexzhang13/videogamebench>. Supply your licensed games locally.

For **existing recordings**, copy the original game folder so its settings and
identity remain unchanged. It contains `manifest.json`, `result.key`, and `upstream/`:

```bash
uv run vgb-replayers prepare --from-root /path/to/original/game \
  --output-dir games/mario
```

For a new recording, prepare directly from the pinned upstream checkout:

```bash
uv run vgb-replayers prepare --upstream /path/to/videogamebench \
  --game super_mario_land --asset /path/to/super_mario_land.gb \
  --output-dir games/mario
uv run vgb-replayers prepare --upstream /path/to/videogamebench \
  --game warcraft2 --asset /path/to/warcraft2.jsdos \
  --output-dir games/warcraft2
```

`--settings settings.json` accepts runtime overrides, for example
`{"max_steps":30000,"action_frames":15,"record_video":true,"video_fps":5}`.
Defaults are in `settings.py`; all fields can be overridden. Game Boy defaults to
Lite mode; DOS defaults to real time. `--revision` selects a different upstream
checkout revision, but the DOS execution extension still requires its pinned
JS-DOS assets. Preparing a new folder creates a new identity; use copy mode to
retain compatibility with old recordings.

## Game Boy: play and replay

```bash
uv run vgb-replayers serve --root "$PWD/games/mario" \
  --state-dir "$PWD/recordings/mario" --host 127.0.0.1 --port 8773 \
  --allow-restart
```

Open <http://localhost:8773>. Play, save, rewind, or restart in the browser. To replay
an exported input trace, start a separate recorder with an empty state directory,
then run:

```bash
uv run vgb-replayers actions --url http://127.0.0.1:8773 \
  --replay /path/to/actions.json --replay-timing immediate \
  --screen ./last-frame.png
```

`--snapshot-interval` controls state caching. Keep `action_frames` unchanged between
recording and replay. Slow play changes human pacing without changing benchmark
input durations.

## DOS: play and exact replay

Use a local cache of the pinned JS-DOS assets. Each cache entry is a same-stem
`.json` metadata file and `.bin` response body; metadata includes `url`, `status`,
`headers`, and `sha256`. Existing recorder CDN caches can be reused unchanged.
`runtime/dos_snapshots.py` lists the required glue, WASM, and frontend hashes.
The game bundle is provided separately through `prepare --asset`.

```bash
uv run vgb-replayers serve --root "$PWD/games/warcraft2" \
  --state-dir "$PWD/recordings/warcraft2" --host 127.0.0.1 --port 8774 \
  --allow-restart --dos-headless --dos-assets-dir /path/to/cdn \
  --dos-save-states --dos-execution-record
```

Open <http://localhost:8774>. **Save state** keeps the emulator and retained
recording prefix together. **Stop & save** finalizes the replay in
`recordings/warcraft2/execution/`; **Continue saved recording** reloads its saved
state. Use Stop & save when leaving to avoid recording trailing idle time.

Verify from a fresh boot without an interactive screen:

```bash
uv run vgb-replayers replay-dos recordings/warcraft2/execution \
  --root "$PWD/games/warcraft2" --dos-assets-dir /path/to/cdn \
  --state-dir "$PWD/checks/dos-1"
```

Or run the export through unchanged upstream `main.py`, using its existing external
frontend support (requires Xvfb and xauth):

```bash
uv run vgb-replayers replay-upstream recordings/warcraft2/execution \
  --root "$PWD/games/warcraft2" --upstream "$PWD/games/warcraft2/upstream" \
  --dos-assets-dir /path/to/cdn --output-dir "$PWD/checks/upstream-1"
```

Exact DOS replay instruments the external frontend to supply recorded timing; the
upstream Python, game assets, WASM, and scoring remain unchanged. Normal live-timing
runs do not gain deterministic replay from action logs alone. Retain the whole
`execution/` bundle; for resumable recordings also retain `.dos-states/` and
`.dos-execution/` alongside the input trace.

Trim trailing inactivity into a **separate** save and export:

```bash
uv run vgb-replayers trim-dos --source-state-dir "$PWD/recordings/warcraft2" \
  --source-slot latest --output-dir "$PWD/checks/trimmed" \
  --root "$PWD/games/warcraft2" --dos-assets-dir /path/to/cdn --tail-chunks 20
```

Adjust `--tail-chunks` to retain victory-screen animations. Validate the generated
`checks/trimmed/execution/` independently before using the new save. Reconstruction
alone is not verification. Fresh replay checks cover the complete WASM memory,
filesystem, suspended continuation, and pixels. Reproducing a losing recording
still loses; a mission victory is not automatically a full benchmark score.

## Logs, configuration, and server access

State directories contain `actions.json`, `result.json`, `progress.jsonl`, upstream
logs, and, when enabled, `gameplay.mp4` and `video-timestamps.jsonl`. Discarded
branches and previous attempts are archived. Replay progress includes chunk counts
and elapsed time; successful checks write `execution-result.json`.

Every command accepts `--help`. Ports, paths, replay timing, video settings, snapshot
limits, timeouts, chunk sizes, and observation rate are configurable. DOS snapshots
default to a 1 GiB limit (`--dos-state-max-bytes`); old long-running sessions can exceed
256 MiB. Browser state errors are returned to the UI instead of dropping the request.
Game and control ports must be distinct (defaults 8000 and 8080).

For a remote server, keep the control server on loopback and forward its port:
`ssh -L 8774:127.0.0.1:8774 user@server`. Play in your local browser; the emulator
needs no physical display.

## Development

```bash
uv sync
uv run pytest -q
uv run ruff check src tests
uv build
```

This repository contains code, UI assets, and tests. Licensed ROMs, DOS bundles,
CDN caches, credentials, and personal recordings remain external. See `NOTICE`,
`LICENSE`, and `provenance.json` for the extraction provenance.
