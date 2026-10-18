"""Session persistence: save a conversation and resume it later.

A session stores the full message list plus the counters and compaction state
needed to continue a run. Session files are plain JSON so they can be inspected
and copied. The trajectory file written by ``save()`` remains the benchmark
artifact; sessions are for interactive work and crash recovery.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from minisweagent.agents.compaction import CompactionRecord, CompactionState
from minisweagent.extensions.config import SessionConfig
from minisweagent.extensions.registry import Tool, err, ok
from minisweagent.models.utils.content_string import get_content_string

SESSION_VERSION = 1


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def sanitize_messages(messages: list[dict], *, keep_media: bool = True) -> list[dict]:
    """Copy messages without fields that are large and useless on resume."""
    cleaned: list[dict] = []
    for message in messages:
        item = dict(message)
        extra = dict(item.get("extra") or {})
        extra.pop("response", None)
        extra.pop("traceback", None)
        extra.pop("attachments", None)
        if extra:
            item["extra"] = extra
        else:
            item.pop("extra", None)
        if not keep_media and isinstance(item.get("content"), list):
            item["content"] = [
                part
                for part in item["content"]
                if isinstance(part, dict) and part.get("type") == "text" and "data:" not in str(part.get("text", ""))
            ]
        cleaned.append(item)
    return cleaned


def compaction_state_from_dict(raw: dict | None) -> CompactionState:
    state = CompactionState()
    if not raw:
        return state
    state.summary = str(raw.get("summary") or "")
    try:
        state.compacted_until = int(raw.get("compacted_until") or 1)
    except (TypeError, ValueError):
        state.compacted_until = 1
    for entry in raw.get("records") or []:
        if not isinstance(entry, dict):
            continue
        try:
            state.records.append(
                CompactionRecord(
                    summary=str(entry.get("summary") or ""),
                    compacted_until=int(entry.get("compacted_until") or 1),
                    reason=str(entry.get("reason") or ""),
                    estimated_tokens_before=int(entry.get("estimated_tokens_before") or 0),
                    estimated_tokens_after=int(entry.get("estimated_tokens_after") or 0),
                    cost=float(entry.get("cost") or 0.0),
                    timestamp=float(entry.get("timestamp") or time.time()),
                )
            )
        except (TypeError, ValueError):
            continue
    return state


class SessionStore:
    def __init__(self, config: SessionConfig, workspace: str | Path) -> None:
        self.config = config
        self.workspace = str(Path(workspace).expanduser().resolve())
        base = Path(config.dir).expanduser()
        if not base.is_absolute():
            base = Path(self.workspace) / base
        self.dir = base
        self.dir.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        return time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}"

    def path_for(self, session_id: str) -> Path:
        candidate = Path(session_id).expanduser()
        if candidate.suffix == ".json":
            return candidate if candidate.is_absolute() else (Path(self.workspace) / candidate)
        if len(candidate.parts) > 1 or candidate.is_absolute():
            return candidate
        return self.dir / f"{session_id}.json"

    def save(self, payload: dict) -> Path:
        session_id = str(payload["id"])
        path = self.path_for(session_id)
        payload = {**payload, "version": SESSION_VERSION, "updated_at": time.time()}
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
        self._prune()
        return path

    def load(self, spec: str | None = None) -> dict:
        if not spec or spec in {"latest", "continue"}:
            path = self.latest()
            if path is None:
                raise FileNotFoundError(f"No saved sessions in {self.dir}")
        else:
            path = self.path_for(spec)
            if not path.is_file():
                raise FileNotFoundError(f"Session not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "messages" not in data:
            raise ValueError(f"Not a mini-swe-agent session file: {path}")
        if int(data.get("version", 0)) > SESSION_VERSION:
            raise ValueError(f"Session {path} was written by a newer version")
        data["_path"] = str(path)
        return data

    def latest(self, workspace: str | None = None) -> Path | None:
        wanted = str(Path(workspace).resolve()) if workspace else self.workspace
        candidates: list[tuple[float, Path]] = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or "messages" not in data:
                continue
            if str(data.get("workspace") or "") != wanted:
                continue
            candidates.append((float(data.get("updated_at") or 0.0), path))
        if not candidates:
            return None
        return max(candidates)[1]

    def list(self, limit: int = 50) -> list[dict]:
        entries: list[dict] = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or "messages" not in data:
                continue
            entries.append(
                {
                    "id": data.get("id") or path.stem,
                    "updated_at": float(data.get("updated_at") or 0.0),
                    "workspace": data.get("workspace", ""),
                    "model": data.get("model", ""),
                    "status": data.get("status", ""),
                    "n_calls": data.get("n_calls", 0),
                    "messages": len(data.get("messages") or []),
                    "task": str(data.get("task") or "")[:100],
                    "path": str(path),
                }
            )
        entries.sort(key=lambda entry: entry["updated_at"], reverse=True)
        return entries[:limit]

    def _prune(self) -> None:
        if self.config.max_sessions <= 0:
            return
        files = sorted(self.dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[self.config.max_sessions :]:
            try:
                path.unlink()
            except OSError:
                pass


def build_session_read_tool(store: SessionStore) -> Tool:
    def handler(args: dict) -> dict:
        spec = str(args.get("session_id") or "latest").strip() or "latest"
        query = str(args.get("query") or "").strip().lower()
        try:
            max_chars = min(50_000, max(1_000, int(args.get("max_chars") or 8_000)))
        except (TypeError, ValueError):
            max_chars = 8_000
        try:
            data = store.load(spec)
        except (FileNotFoundError, ValueError) as exc:
            return err(str(exc))

        lines: list[str] = []
        for message in data.get("messages") or []:
            role = str(message.get("role") or "unknown")
            text = get_content_string(message)
            if not text:
                continue
            if query and query not in text.lower():
                continue
            lines.append(f"[{role}] {text}")
        body = "\n\n".join(lines)
        if len(body) > max_chars:
            body = body[:max_chars] + "\n...[session transcript truncated]"
        header = (
            f'<session id="{data.get("id", "")}" status="{data.get("status", "")}" '
            f'calls="{data.get("n_calls", 0)}" model="{data.get("model", "")}">\n'
            f'<task>{data.get("task", "")}</task>'
        )
        if not body:
            body = "(no matching messages)"
        return ok(f"{header}\n{body}\n</session>")

    return Tool(
        name="SessionRead",
        description=(
            "Read a previous session transcript. Omit session_id for the most recent session in this "
            "workspace. Use query to keep only messages containing a keyword. Images are omitted; only "
            "text is returned."
        ),
        parameters={
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session id or path (default: latest in this workspace)",
                },
                "query": {"type": "string", "description": "Case-insensitive keyword filter"},
                "max_chars": {"type": "integer", "default": 8000},
            },
            "required": [],
        },
        handler=handler,
        read_only=True,
    )
