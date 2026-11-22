"""Export a human recording with its referenced observations."""

import argparse
import json
from pathlib import Path

from recorder import export_recording


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, help="Defaults to STATE_DIR/actions.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trace = args.trace or args.state_dir / "actions.json"
    print(json.dumps(export_recording(args.state_dir, trace, args.output), indent=2))


if __name__ == "__main__":
    main()
