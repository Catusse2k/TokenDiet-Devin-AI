#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fnmatch
import glob
import io
import json
import os
import sys
import tokenize
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", "env",
    ".env", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist",
    "build", ".idea", ".vscode", ".cache", "coverage", "target", ".next", ".nuxt",
}
DEFAULT_EXCLUDED_FILES = {
    ".DS_Store", "Thumbs.db", "*.pyc", "*.pyo", "*.log", "*.sqlite3", "*.sqlite",
}
LANGUAGE_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".md": "markdown", ".sh": "bash", ".ps1": "powershell",
    ".bash": "bash", ".zsh": "bash", ".html": "html", ".css": "css", ".sql": "sql",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby",
    ".php": "php", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".env.example": "dotenv", ".devinrules": "markdown",
}


@dataclass
class PackedFile:
    path: str
    absolute_path: Path
    language: str
    content: str
    size_bytes: int
    lines: int
    estimated_tokens: int


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def is_secret_path(path: Path) -> bool:
    name = path.name
    if name in {".env", ".env.local", ".env.development", ".env.production", ".env.test", ".env.staging"}:
        return True
    if name.startswith(".env.") and name != ".env.example":
        return True
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"} or name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    return False


def matches_any(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern) for pattern in patterns)


def expand_inputs(raw_paths: list[str]) -> list[Path]:
    expanded: list[Path] = []
    for raw in raw_paths:
        raw_expanded = os.path.expanduser(raw)
        if glob.has_magic(raw_expanded):
            expanded.extend(Path(match) for match in glob.glob(raw_expanded, recursive=True))
        else:
            expanded.append(Path(raw_expanded))
    return expanded


def iter_files(inputs: list[Path], extra_excludes: list[str]) -> Iterable[Path]:
    seen: set[Path] = set()
    for path in inputs:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            yield resolved
        elif resolved.is_dir():
            for root, dirnames, filenames in os.walk(resolved):
                dirnames[:] = [
                    dirname for dirname in dirnames
                    if dirname not in DEFAULT_EXCLUDED_DIRS
                    and not matches_any(dirname, extra_excludes)
                ]
                for filename in filenames:
                    yield Path(root) / filename


def display_path(path: Path, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except (OSError, ValueError):
        return path.resolve().as_posix()


def language_for(path: Path) -> str:
    name = path.name.lower()
    if name in LANGUAGE_BY_SUFFIX:
        return LANGUAGE_BY_SUFFIX[name]
    suffix = path.suffix.lower()
    return LANGUAGE_BY_SUFFIX.get(suffix, "text")


def compress_text(text: str, strip_blank_lines: bool, strip_trailing: bool, max_lines: int | None) -> str:
    had_final_newline = text.endswith("\n")
    lines = text.splitlines()
    if strip_trailing:
        lines = [line.rstrip() for line in lines]
    if strip_blank_lines:
        lines = [line for line in lines if line.strip()]
    omitted = 0
    if max_lines is not None and len(lines) > max_lines:
        omitted = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... {omitted} lines omitted by --max-lines-per-file ..."]
    output = "\n".join(lines)
    if had_final_newline or omitted:
        output += "\n"
    return output


def strip_python_comments(text: str) -> str:
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError):
        return text
    lines = text.splitlines(keepends=True)
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            index = row - 1
            if index >= len(lines):
                break
            line = lines[index]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            lines[index] = line[:start] + line[end:]
    return "".join(lines)


LINE_COMMENT_MARKERS = {
    "python": ("#",),
    "bash": ("#",),
    "powershell": ("#",),
    "yaml": ("#",),
    "toml": ("#",),
    "dotenv": ("#",),
    "ruby": ("#",),
    "javascript": ("//",),
    "typescript": ("//",),
    "java": ("//",),
    "go": ("//",),
    "rust": ("//",),
    "c": ("//",),
    "cpp": ("//",),
    "csharp": ("//",),
    "kotlin": ("//",),
    "php": ("//",),
    "sql": ("--",),
    "lua": ("--",),
    "html": (),
    "markdown": (),
    "json": (),
    "css": (),
    "text": (),
}
BLOCK_COMMENT_PAIRS = (("/*", "*/"), ("<!--", "-->"))


def strip_generic_comments(text: str, language: str) -> str:
    line_markers = LINE_COMMENT_MARKERS.get(language, ("//",))
    out_lines: list[str] = []
    in_block = False
    block_end = ""
    for lineno, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        line = raw_line
        if in_block:
            end_index = line.find(block_end)
            if end_index == -1:
                continue
            line = line[end_index + len(block_end):]
            in_block = False
        while True:
            opener = closer = ""
            start = -1
            for candidate_opener, candidate_closer in BLOCK_COMMENT_PAIRS:
                found = line.find(candidate_opener)
                if found != -1 and (start == -1 or found < start):
                    start, opener, closer = found, candidate_opener, candidate_closer
            if start == -1:
                break
            end = line.find(closer, start + len(opener))
            if end == -1:
                in_block = True
                block_end = closer
                line = line[:start]
                break
            line = line[:start] + line[end + len(closer):]
        stripped = line.strip()
        if stripped.startswith(line_markers) and not (lineno == 1 and stripped.startswith("#!")):
            continue
        if stripped:
            out_lines.append(line)
    return "".join(out_lines)


def strip_comments(text: str, language: str) -> str:
    stripped = strip_python_comments(text) if language == "python" else strip_generic_comments(text, language)
    return compress_text(stripped, True, True, None)


def number_lines(text: str) -> str:
    lines = text.splitlines()
    return "\n".join(f"{index:>5} | {line}" for index, line in enumerate(lines, start=1))


def render_markdown(files: list[PackedFile], stats: dict, include_stats: bool) -> str:
    parts = [
        "# Devin Context Pack",
        "",
        f"Generated: {stats['generated_at']}",
        f"Files: {stats['included_files']} | Estimated tokens: {stats['estimated_tokens']} | Bytes: {stats['included_bytes']}",
        "",
    ]
    for item in files:
        parts.extend([
            f"## FILE: {item.path}",
            "",
            f"Language: {item.language} | Lines: {item.lines} | Bytes: {item.size_bytes} | Est. tokens: {item.estimated_tokens}",
            "",
            "```" + item.language,
            item.content.rstrip("\n"),
            "```",
            "",
        ])
    if include_stats:
        parts.extend(["## Packing Stats", "", "```json", json.dumps(stats, indent=2, ensure_ascii=False), "```", ""])
    return "\n".join(parts)


def render_plain(files: list[PackedFile]) -> str:
    parts: list[str] = []
    for item in files:
        parts.extend([f"===== FILE: {item.path} =====", item.content.rstrip("\n"), ""])
    return "\n".join(parts)


def render_json(files: list[PackedFile], stats: dict) -> str:
    return json.dumps({
        "stats": stats,
        "files": [
            {
                "path": item.path,
                "language": item.language,
                "size_bytes": item.size_bytes,
                "lines": item.lines,
                "estimated_tokens": item.estimated_tokens,
                "content": item.content,
            }
            for item in files
        ],
    }, ensure_ascii=False, indent=2)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compress and package code/context files for AI agents.")
    parser.add_argument("paths", nargs="+", help="Files, directories, or glob patterns to include.")
    parser.add_argument("-o", "--output", help="Output file. Defaults to stdout.")
    parser.add_argument("--format", choices=["markdown", "plain", "json"], default="markdown")
    parser.add_argument("--exclude", action="append", default=[], help="Additional glob pattern to exclude; repeatable.")
    parser.add_argument("--max-file-size-kb", type=int, default=env_int("CONTEXT_PACKER_MAX_FILE_SIZE_KB", 200))
    parser.add_argument("--max-total-size-kb", type=int, default=env_int("CONTEXT_PACKER_MAX_TOTAL_SIZE_KB", 1024))
    parser.add_argument("--token-budget", type=int, default=env_int("CONTEXT_PACKER_TOKEN_BUDGET", 120000))
    parser.add_argument("--compress", action="store_true", help="Remove blank lines and trailing whitespace (safe whitespace compression).")
    parser.add_argument("--strip-comments", action="store_true", help="Remove comments (docstrings preserved) and collapse blank lines.")
    parser.add_argument("--copy", action="store_true", help="Copy the rendered bundle to the clipboard (requires pyperclip); defaults output to context_bundle.md.")
    parser.add_argument("--strip-blank-lines", action="store_true")
    parser.add_argument("--strip-trailing-whitespace", action="store_true")
    parser.add_argument("--max-lines-per-file", type=int, default=None)
    parser.add_argument("--line-numbers", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print packing stats without writing the output document.")
    parser.add_argument("--stats", action="store_true", help="Include packing statistics in markdown/plain output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inputs = [path for path in expand_inputs(args.paths) if path.exists()]
    if not inputs:
        print("error: no valid input paths found", file=sys.stderr)
        return 2

    single_dir = inputs[0].resolve() if len(inputs) == 1 and inputs[0].resolve().is_dir() else Path.cwd()
    output_path = Path(args.output).expanduser().resolve() if args.output and args.output != "-" else (Path("context_bundle.md").expanduser().resolve() if args.copy else None)
    max_file_bytes = max(1, args.max_file_size_kb) * 1024
    max_total_bytes = max(1, args.max_total_size_kb) * 1024
    strip_blank = args.compress or args.strip_blank_lines
    strip_trailing = args.compress or args.strip_trailing_whitespace

    packed: list[PackedFile] = []
    skipped: list[dict[str, str]] = []
    total_bytes = 0
    total_tokens = 0

    for candidate in sorted(iter_files(inputs, args.exclude), key=lambda item: item.as_posix().lower()):
        rel = display_path(candidate, single_dir)
        if output_path and candidate.resolve() == output_path:
            continue
        if is_secret_path(candidate):
            skipped.append({"path": rel, "reason": "secret-looking file excluded by default"})
            continue
        if matches_any(rel, DEFAULT_EXCLUDED_FILES) or matches_any(rel, args.exclude):
            skipped.append({"path": rel, "reason": "excluded by pattern"})
            continue
        try:
            size_bytes = candidate.stat().st_size
        except OSError as exc:
            skipped.append({"path": rel, "reason": f"unreadable: {exc}"})
            continue
        if size_bytes > max_file_bytes:
            skipped.append({"path": rel, "reason": f"file too large ({size_bytes} bytes)"})
            continue
        try:
            raw = candidate.read_bytes()
        except OSError as exc:
            skipped.append({"path": rel, "reason": f"read error: {exc}"})
            continue
        if b"\x00" in raw[:8192]:
            skipped.append({"path": rel, "reason": "binary file"})
            continue
        text = raw.decode("utf-8", errors="replace")
        if args.strip_comments:
            text = strip_comments(text, language_for(candidate))
        text = compress_text(text, strip_blank, strip_trailing, args.max_lines_per_file)
        encoded = text.encode("utf-8")
        estimated_tokens = max(1, (len(encoded) + 3) // 4)
        if total_bytes + len(encoded) > max_total_bytes:
            skipped.append({"path": rel, "reason": "total size budget exceeded"})
            continue
        if total_tokens + estimated_tokens > args.token_budget:
            skipped.append({"path": rel, "reason": "token budget exceeded"})
            continue

        if args.line_numbers:
            text = number_lines(text)
            encoded = text.encode("utf-8")
            estimated_tokens = max(1, (len(encoded) + 3) // 4)
        total_bytes += len(encoded)
        total_tokens += estimated_tokens
        packed.append(PackedFile(
            path=rel,
            absolute_path=candidate,
            language=language_for(candidate),
            content=text,
            size_bytes=len(encoded),
            lines=len(text.splitlines()),
            estimated_tokens=estimated_tokens,
        ))

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "format": args.format,
        "included_files": len(packed),
        "included_bytes": total_bytes,
        "estimated_tokens": total_tokens,
        "token_budget": args.token_budget,
        "max_file_size_kb": args.max_file_size_kb,
        "max_total_size_kb": args.max_total_size_kb,
        "compress": strip_blank or strip_trailing,
        "skipped_files": len(skipped),
        "skipped": skipped[:100],
    }

    if args.dry_run:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0 if packed else 2

    if args.format == "markdown":
        rendered = render_markdown(packed, stats, args.stats)
    elif args.format == "plain":
        rendered = render_plain(packed)
        if args.stats:
            rendered += "\n===== PACKING STATS =====\n" + json.dumps(stats, indent=2, ensure_ascii=False) + "\n"
    else:
        rendered = render_json(packed, stats)

    if args.copy:
        try:
            import pyperclip
            pyperclip.copy(rendered)
            print("Copied bundle to clipboard", file=sys.stderr)
        except Exception as exc:
            print(f"warning: clipboard copy unavailable: {exc}", file=sys.stderr)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
        print(f"Wrote {len(packed)} files ({total_bytes} bytes, ~{total_tokens} tokens) to {output_path}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
    return 0 if packed else 2


if __name__ == "__main__":
    raise SystemExit(main())
