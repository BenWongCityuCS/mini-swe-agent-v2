"""TodoWrite: a small persistent task list for multi-step work."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from minisweagent.extensions.registry import Tool, err, ok

_STATUSES = ("pending", "in_progress", "completed")
_PRIORITIES = ("high", "medium", "low")


@dataclass
class TodoItem:
    content: str
    status: str = "pending"
    priority: str = "medium"

    def line(self) -> str:
        return f"[{self.status}] ({self.priority}) {self.content}"


class TodoStore:
    def __init__(self, workspace: Path, persist: bool = True) -> None:
        self.path = Path(workspace) / ".mini-swe-agent" / "todos.json"
        self.persist = persist
        self.items: list[TodoItem] = []
        self._load()

    def _load(self) -> None:
        if not self.persist or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("todos", []):
            if isinstance(raw, dict) and raw.get("content"):
                self.items.append(
                    TodoItem(
                        content=str(raw["content"]),
                        status=str(raw.get("status", "pending")),
                        priority=str(raw.get("priority", "medium")),
                    )
                )

    def save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"todos": [asdict(item) for item in self.items]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def update(self, raw_items: list[dict]) -> list[TodoItem]:
        items: list[TodoItem] = []
        for raw in raw_items:
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            status = str(raw.get("status") or "pending").strip()
            if status not in _STATUSES:
                status = "pending"
            priority = str(raw.get("priority") or "medium").strip()
            if priority not in _PRIORITIES:
                priority = "medium"
            items.append(TodoItem(content=content, status=status, priority=priority))
        self.items = items
        self.save()
        return items

    def format(self) -> str:
        if not self.items:
            return "Todo list is empty."
        completed = sum(1 for item in self.items if item.status == "completed")
        lines = [f"Todos ({completed}/{len(self.items)} completed):"]
        lines.extend(f"- {item.line()}" for item in self.items)
        return "\n".join(lines)


def build_todo_read_tool(store: TodoStore) -> Tool:
    def handler(args: dict) -> dict:
        return ok(store.format())

    return Tool(
        name="TodoRead",
        description=(
            "Return the current todo list with statuses and priorities. Use it to check progress "
            "without rewriting the list."
        ),
        parameters={"type": "object", "properties": {}},
        handler=handler,
        read_only=True,
        metadata={"persist_path": str(store.path)},
    )


def build_todo_tool(store: TodoStore) -> Tool:
    def handler(args: dict) -> dict:
        todos = args.get("todos")
        if not isinstance(todos, list):
            return err("todos must be a list of {content, status, priority} objects")
        store.update(todos)
        return ok(store.format())

    return Tool(
        name="TodoWrite",
        description=(
            "Create and update a task list for the current run. Pass the complete list every time; "
            "it replaces the previous one. Keep exactly one item in_progress while working, and mark "
            "items completed as soon as they are done. Use it for multi-step work, not for trivial tasks."
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": list(_STATUSES)},
                            "priority": {"type": "string", "enum": list(_PRIORITIES)},
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
        handler=handler,
        read_only=False,
        metadata={"persist_path": str(store.path)},
    )
