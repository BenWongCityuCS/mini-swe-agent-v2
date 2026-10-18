"""Small read-only tools (Read/Grep/Glob) used by subagent profiles."""

from __future__ import annotations

import base64
import fnmatch
import glob as glob_module
import os
import re
import subprocess
from pathlib import Path

from minisweagent.extensions.registry import Tool, err, ok

_MAX_READ_CHARS = 200_000
_MAX_GREP_RESULTS = 500
_MAX_GLOB_RESULTS = 2_000
_MAX_FILE_BYTES = 2_000_000
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}

_MEDIA_TYPES = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".bmp": ("image", "image/bmp"),
    ".svg": ("image", "image/svg+xml"),
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"),
    ".ogv": ("video", "video/ogg"),
    ".avi": ("video", "video/x-msvideo"),
}


def _resolve_path(value: str, workspace: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def _read_handler(
    workspace: Path,
    tracker=None,
    *,
    read_media: bool = False,
    max_image_bytes: int = 5_000_000,
    max_video_bytes: int = 20_000_000,
    media_detail: str = "auto",
):
    def handler(args: dict) -> dict:
        raw = args.get("file_path") or args.get("path")
        if not raw:
            return err("file_path is required")
        path = _resolve_path(str(raw), workspace)
        if not path.is_file():
            return err(f"File not found: {path}")

        media = _MEDIA_TYPES.get(path.suffix.lower())
        if media is not None:
            kind, mime = media
            if not read_media:
                return err(
                    f"{path} is {kind} data. Enable agent.tools.read_media to attach it to the model, "
                    "or describe it with the Vision tool."
                )
            limit = max_image_bytes if kind == "image" else max_video_bytes
            try:
                data = path.read_bytes()
            except OSError as exc:
                return err(f"Could not read {path}: {exc}")
            if len(data) > limit:
                return err(
                    f"{path} is {len(data):,} bytes, over the {limit:,}-byte limit for {kind} input. "
                    "Downscale or increase agent.tools.max_image_bytes/max_video_bytes."
                )
            data_url = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            attachment = {
                "path": str(path),
                "kind": kind,
                "mime": mime,
                "bytes": len(data),
                "data_url": data_url,
            }
            if kind == "image" and media_detail:
                attachment["detail"] = media_detail
            return ok(
                f'<file path="{path}" type="{kind}" mime="{mime}" bytes="{len(data)}">\n'
                f"The {kind} is attached to the next user message as native model input.\n"
                "</file>",
                extra={"attachments": [attachment]},
            )

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return err(f"Could not read {path}: {exc}")
        if tracker is not None:
            tracker.record(path, text)

        try:
            offset = max(1, int(args.get("offset") or 1))
        except (TypeError, ValueError):
            offset = 1
        try:
            limit = max(1, int(args.get("limit") or 2000))
        except (TypeError, ValueError):
            limit = 2000

        lines = text.splitlines()
        selected = lines[offset - 1 : offset - 1 + limit]
        width = len(str(offset + len(selected)))
        body = "\n".join(f"{offset + i:>{width}}\t{line}" for i, line in enumerate(selected))
        if len(body) > _MAX_READ_CHARS:
            body = body[:_MAX_READ_CHARS] + "\n[truncated]"
        header = f"<file path=\"{path}\" lines=\"{offset}-{offset + len(selected) - 1} of {len(lines)}\">"
        return ok(f"{header}\n{body}\n</file>")

    return handler


def _python_grep(root: Path, pattern: re.Pattern, glob_pattern: str, max_results: int) -> list[str]:
    results: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for filename in filenames:
            if not fnmatch.fnmatch(filename, glob_pattern):
                continue
            path = Path(dirpath) / filename
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for lineno, line in enumerate(handle, 1):
                        if pattern.search(line):
                            rel = path.relative_to(root) if path.is_relative_to(root) else path
                            results.append(f"{rel}:{lineno}:{line.rstrip()}")
                            if len(results) >= max_results:
                                return results
            except (OSError, ValueError):
                continue
    return results


def _grep_handler(workspace: Path):
    def handler(args: dict) -> dict:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return err("pattern is required")
        root = _resolve_path(str(args.get("path") or "."), workspace)
        if not root.is_dir():
            return err(f"Search path is not a directory: {root}")
        glob_pattern = str(args.get("glob") or "*")
        case_insensitive = bool(args.get("case_insensitive") or args.get("ignore_case"))
        try:
            flags = re.IGNORECASE if case_insensitive else 0
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return err(f"Invalid regular expression: {exc}")
        try:
            max_results = min(int(args.get("max_results") or 200), _MAX_GREP_RESULTS)
        except (TypeError, ValueError):
            max_results = 200

        results: list[str] = []
        if subprocess.run(["which", "rg"], capture_output=True, text=True).returncode == 0:
            command = ["rg", "-n", "--no-heading", "--color", "never", "-g", glob_pattern]
            if case_insensitive:
                command.append("-i")
            command.extend([pattern, str(root)])
            completed = subprocess.run(command, capture_output=True, text=True)
            results = [line for line in completed.stdout.splitlines() if line][:max_results]
            if completed.returncode not in (0, 1) and not results:
                results = _python_grep(root, regex, glob_pattern, max_results)
        else:
            results = _python_grep(root, regex, glob_pattern, max_results)

        if not results:
            return ok(f"No matches for {pattern!r} under {root}")
        return ok("\n".join(results))

    return handler


def _glob_handler(workspace: Path):
    def handler(args: dict) -> dict:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return err("pattern is required")
        base = _resolve_path(str(args.get("path") or "."), workspace)
        if not base.is_dir():
            return err(f"Glob path is not a directory: {base}")
        matches = sorted(glob_module.glob(str(base / pattern), recursive=True))
        matches = [path for path in matches if Path(path).is_file()][:_MAX_GLOB_RESULTS]
        if not matches:
            return ok(f"No files matching {pattern!r} under {base}")
        rel_paths = []
        for match in matches:
            path = Path(match)
            rel_paths.append(str(path.relative_to(base)) if path.is_relative_to(base) else str(path))
        return ok("\n".join(rel_paths))

    return handler


def build_read_only_tools(
    workspace: Path,
    tracker=None,
    *,
    read_media: bool = False,
    max_image_bytes: int = 5_000_000,
    max_video_bytes: int = 20_000_000,
    media_detail: str = "auto",
) -> list[Tool]:
    return [
        Tool(
            name="Read",
            description=(
                "Read a file. Text is returned with line numbers (use offset/limit for large files). "
                "Images and videos are attached to the next message as native model input when "
                "read_media is enabled."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file (relative to the workspace)"},
                    "offset": {"type": "integer", "description": "1-based first line to read", "default": 1},
                    "limit": {"type": "integer", "description": "Maximum number of lines", "default": 2000},
                },
                "required": ["file_path"],
            },
            handler=_read_handler(
                workspace,
                tracker,
                read_media=read_media,
                max_image_bytes=max_image_bytes,
                max_video_bytes=max_video_bytes,
                media_detail=media_detail,
            ),
            read_only=True,
        ),
        Tool(
            name="Grep",
            description="Search file contents with a regular expression, returning path:line:text matches.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression to search for"},
                    "path": {"type": "string", "description": "Directory to search (default: workspace)", "default": "."},
                    "glob": {"type": "string", "description": "Only search files matching this glob", "default": "*"},
                    "case_insensitive": {"type": "boolean", "default": False},
                    "max_results": {"type": "integer", "default": 200},
                },
                "required": ["pattern"],
            },
            handler=_grep_handler(workspace),
            read_only=True,
        ),
        Tool(
            name="Glob",
            description="List files matching a glob pattern, sorted by path.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. **/*.py"},
                    "path": {"type": "string", "description": "Base directory (default: workspace)", "default": "."},
                },
                "required": ["pattern"],
            },
            handler=_glob_handler(workspace),
            read_only=True,
        ),
    ]
