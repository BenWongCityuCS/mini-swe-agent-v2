"""Write/Edit with ZCode-style read-before-edit enforcement.

The tracker records every file read through the ``Read`` tool (path, mtime and
content hash). ``Edit``/``Write`` refuse to touch a file that was not read or
that changed on disk since it was read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from minisweagent.extensions.registry import Tool, err, ok


@dataclass
class _FileState:
    mtime_ns: int
    digest: str


class FileTracker:
    def __init__(self) -> None:
        self._files: dict[str, _FileState] = {}

    @staticmethod
    def _key(path: Path | str) -> str:
        return str(Path(path).expanduser().resolve())

    def record(self, path: Path | str, text: str) -> None:
        resolved = Path(self._key(path))
        try:
            stat = resolved.stat()
        except OSError:
            return
        self._files[str(resolved)] = _FileState(
            mtime_ns=stat.st_mtime_ns,
            digest=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        )

    def check(self, path: Path | str) -> tuple[bool, str]:
        resolved = Path(self._key(path))
        state = self._files.get(str(resolved))
        if state is None:
            return False, (
                f"{resolved} has not been read in this session. Call Read before editing it, "
                "then retry with the exact text you saw."
            )
        if not resolved.is_file():
            return False, f"{resolved} no longer exists. Read the directory again."
        if resolved.stat().st_mtime_ns != state.mtime_ns:
            return False, (
                f"{resolved} changed on disk since it was read. Call Read again before editing, "
                "so you do not overwrite someone else's changes."
            )
        return True, ""

    def forget(self, path: Path | str) -> None:
        self._files.pop(self._key(path), None)


def _resolve(workspace: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def build_edit_tools(workspace: Path, tracker: FileTracker) -> list[Tool]:
    def write_handler(args: dict) -> dict:
        raw = args.get("file_path") or args.get("path")
        content = args.get("content")
        if not raw or not isinstance(content, str):
            return err("file_path and content (string) are required")
        path = _resolve(workspace, str(raw))
        if path.exists():
            ok_to_edit, reason = tracker.check(path)
            if not ok_to_edit:
                return err(reason)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return err(f"Could not write {path}: {exc}")
        tracker.record(path, content)
        return ok(f"Wrote {len(content)} characters to {path}")

    def edit_handler(args: dict) -> dict:
        raw = args.get("file_path") or args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        replace_all = bool(args.get("replace_all") or False)
        if not raw or not isinstance(old_string, str) or not isinstance(new_string, str):
            return err("file_path, old_string and new_string (strings) are required")
        if old_string == "":
            return err("old_string must not be empty; use Write to create or replace a whole file")
        path = _resolve(workspace, str(raw))
        ok_to_edit, reason = tracker.check(path)
        if not ok_to_edit:
            return err(reason)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return err(f"Could not read {path}: {exc}")
        count = text.count(old_string)
        if count == 0:
            return err("old_string was not found. Read the file again and copy the exact text.")
        if count > 1 and not replace_all:
            return err(
                f"old_string occurs {count} times. Add more surrounding context to make it unique, "
                "or pass replace_all=true."
            )
        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return err(f"Could not write {path}: {exc}")
        tracker.record(path, updated)
        replacements = count if replace_all else 1
        return ok(f"Edited {path} ({replacements} replacement{'s' if replacements != 1 else ''})")

    return [
        Tool(
            name="Write",
            description=(
                "Create a file or replace an existing file. If the file already exists it must have "
                "been read first in this session; otherwise the write is refused."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path relative to the workspace"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["file_path", "content"],
            },
            handler=write_handler,
            read_only=False,
        ),
        Tool(
            name="Edit",
            description=(
                "Replace an exact string in a file. The file must have been read first in this "
                "session and must not have changed since. Use replace_all for repeated matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path relative to the workspace"},
                    "old_string": {"type": "string", "description": "Exact text to replace"},
                    "new_string": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "default": False},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            handler=edit_handler,
            read_only=False,
        ),
    ]
