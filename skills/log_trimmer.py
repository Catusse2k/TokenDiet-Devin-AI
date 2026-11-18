#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

NOISE_PATTERNS = re.compile(
    r"error|err!|warn|warning|critical|fatal|exception|traceback|failed|failure|panic|assert|denied|timeout|refused|unreachable",
    re.IGNORECASE,
)
CONTEXT_LINES = 2
MAX_LINES = 400


def trim(text: str, context_lines: int = CONTEXT_LINES) -> str:
    lines = text.splitlines()
    keep = set()
    for i, line in enumerate(lines):
        if NOISE_PATTERNS.search(line):
            for j in range(max(0, i - context_lines), min(len(lines), i + context_lines + 1)):
                keep.add(j)
    if not keep:
        return "(no errors or warnings found in the provided log)"
    out = [lines[i] for i in sorted(keep)]
    if len(out) > MAX_LINES:
        out = out[:MAX_LINES] + [f"... truncated at {MAX_LINES} lines ..."]
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Trim logs to errors/warnings with context.")
    parser.add_argument("file", nargs="?", help="Log file path. Defaults to stdin.")
    parser.add_argument("--context", type=int, default=CONTEXT_LINES, help="Context lines around matches.")
    args = parser.parse_args()

    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            print(f"error: file not found: {path}", file=sys.stderr)
            return 2
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        text = sys.stdin.read()
    if not text.strip():
        print("error: empty input", file=sys.stderr)
        return 2

    sys.stdout.write(trim(text, max(0, args.context)) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
