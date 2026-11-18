#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TEST_DIRS = ("tests", "test", "spec", "__tests__")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".cache", "dist", "build"}
TEST_TIMEOUT = 120


def git_lines(args: list[str], cwd: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()]


def changed_files(root: Path) -> list[str]:
    files = set(git_lines(["status", "--porcelain"], root))
    parsed = set()
    for entry in files:
        path = entry[3:] if len(entry) > 3 else entry
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        parsed.add(path.strip('"'))
    parsed.update(git_lines(["diff", "--name-only", "HEAD"], root))
    parsed.update(git_lines(["diff", "--name-only", "--cached"], root))
    return sorted(p for p in parsed if p and not any(part in SKIP_DIRS for part in Path(p).parts))


def test_candidates(root: Path, changed: list[str]) -> list[str]:
    candidates: set[str] = set()
    for rel in changed:
        path = Path(rel)
        if any(part in TEST_DIRS for part in path.parts) or "test" in path.stem.lower() or path.suffix in {".test.js", ".test.ts", ".spec.js", ".spec.ts"}:
            candidates.add(rel)
            continue
        stem = path.stem
        parent = path.parent
        for name in (f"test_{stem}.py", f"{stem}_test.py", f"{stem}.test.js", f"{stem}.spec.js"):
            candidate = parent / name
            if candidate.is_file():
                candidates.add(candidate.as_posix())
        for test_dir in TEST_DIRS:
            base = root / test_dir
            if not base.is_dir():
                continue
            for match in base.rglob(f"*{stem}*"):
                if match.is_file() and match.suffix in {".py", ".js", ".ts"}:
                    candidates.add(match.relative_to(root).as_posix())
    return sorted(candidates)


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect targeted tests from git changes.")
    parser.add_argument("path", nargs="?", default=".", help="Repository root (default: cwd).")
    parser.add_argument("--run", action="store_true", help="Actually run pytest on the detected test files.")
    parser.add_argument("--timeout", type=int, default=TEST_TIMEOUT)
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not (root / ".git").exists():
        print("error: not a git repository", file=sys.stderr)
        return 2

    changed = changed_files(root)
    tests = test_candidates(root, changed)

    out = ["# Targeted Tests from git diff", "", "## Changed files"]
    if changed:
        out.extend(f"- {f}" for f in changed)
    else:
        out.append("- (none)")
    out.extend(["", "## Candidate test files"])
    if tests:
        out.extend(f"- {t}" for t in tests)
    else:
        out.append("- (none)")

    if not tests:
        sys.stdout.write("\n".join(out) + "\nNo test files could be mapped; run the full suite only if needed.\n")
        return 0

    pytest_targets = [t for t in tests if t.endswith(".py")]
    command = [sys.executable, "-m", "pytest", "-x", "-q", *pytest_targets]
    out.extend(["", "## Suggested command", "", "```", " ".join(command), "```"])

    if args.run and pytest_targets:
        out.extend(["", "## pytest output", ""])
        try:
            result = subprocess.run(command, cwd=str(root), capture_output=True, text=True, timeout=args.timeout)
            out.append((result.stdout + result.stderr).strip()[-8000:] or "(no output)")
            out.append(f"exit_code: {result.returncode}")
        except subprocess.TimeoutExpired:
            out.append(f"pytest timed out after {args.timeout}s")
    sys.stdout.write("\n".join(out) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
