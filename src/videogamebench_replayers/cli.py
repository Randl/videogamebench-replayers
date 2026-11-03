"""Command dispatcher; each runtime runs in its own interpreter."""

import argparse
import os
import sys
from pathlib import Path

SCRIPTS = {
    "serve": "server.py",
    "actions": "play.py",
    "replay-dos": "replay_execution.py",
    "replay-upstream": "upstream_execution.py",
    "trim-dos": "trim_execution.py",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", *SCRIPTS])
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if options.command == "prepare":
        from .prepare import main as prepare

        return prepare(options.args)
    script = Path(__file__).parent / "runtime" / SCRIPTS[options.command]
    os.execv(sys.executable, [sys.executable, str(script), *options.args])


if __name__ == "__main__":
    main()
