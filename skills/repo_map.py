#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".env", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
    "build", ".idea", ".vscode", ".cache", "coverage", "target", ".next", ".nuxt",
}
CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".kt", ".swift"}
MAX_CHARS = 12000


def iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() in CODE_SUFFIXES:
                yield path


def python_symbols(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, ValueError, OSError):
        return []
    symbols = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in node.args.args]
            symbols.append(f"def {node.name}({', '.join(args)})")
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            suffix = f" [{', '.join(methods[:8])}]" if methods else ""
            symbols.append(f"class {node.name}{suffix}")
    return symbols


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact repository map via AST/regex.")
    parser.add_argument("path", nargs="?", default=".", help="Repository root (default: cwd).")
    parser.add_argument("--max-chars", type=int, default=MAX_CHARS)
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    lines = [f"# Repo Map: {root}", ""]
    count = 0
    for path in sorted(iter_files(root), key=lambda p: p.as_posix().lower()):
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        entry = [f"## {rel}"]
        if path.suffix == ".py":
            symbols = python_symbols(path)
            entry.extend(f"- {s}" for s in symbols[:40])
        count += 1
        candidate = "\n".join(lines + entry) + "\n"
        if len(candidate) > args.max_chars:
            lines.append(f"... truncated after {count - 1} files ...")
            break
        lines.extend(entry)
        lines.append("")

    lines.insert(1, f"Files scanned: {count}")
    sys.stdout.write("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
