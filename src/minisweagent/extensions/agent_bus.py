"""Agent-to-agent messaging (``SendMessage``).

The bus is a process-local set of inboxes. Root agents register as ``main``;
subagents get a unique id that the coordinator receives when it starts a
background ``Task``. Messages sent to a running agent are injected as user
messages before its next model call.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

from minisweagent.extensions.config import AgentBusConfig
from minisweagent.extensions.registry import Tool, err, ok


@dataclass
class AgentMessage:
    id: str
    sender: str
    recipient: str
    message: str
    summary: str = ""
    timestamp: float = 0.0
    sequence: int = 0

    def render(self) -> str:
        summary = f' summary="{self.summary}"' if self.summary else ""
        return (
            f'<agent_message id="{self.id}" from="{self.sender}"{summary}>\n'
            f"{self.message}\n"
            f"</agent_message>"
        )


class AgentBus:
    def __init__(self, config: AgentBusConfig | None = None) -> None:
        self.config = config or AgentBusConfig(enabled=True)
        self._inboxes: dict[str, deque[AgentMessage]] = {}
        self._kinds: dict[str, str] = {}
        self._lock = threading.Lock()
        self._sequence = 0
        self.history: deque[AgentMessage] = deque(maxlen=200)

    def register(self, agent_id: str, *, kind: str = "agent") -> None:
        with self._lock:
            self._inboxes.setdefault(agent_id, deque())
            self._kinds[agent_id] = kind

    def unregister(self, agent_id: str) -> None:
        with self._lock:
            self._kinds.pop(agent_id, None)

    def agents(self) -> list[dict]:
        with self._lock:
            return [
                {"id": agent_id, "kind": self._kinds.get(agent_id, "agent"), "pending": len(inbox)}
                for agent_id, inbox in self._inboxes.items()
                if agent_id in self._kinds
            ]

    def send(self, sender: str, recipient: str, message: str, summary: str = "") -> dict:
        message = message.strip()
        if not message:
            return err("message is required")
        if len(message) > 50_000:
            return err("message is too long (limit: 50000 characters)")
        with self._lock:
            inbox = self._inboxes.get(recipient)
            if inbox is None or recipient not in self._kinds:
                available = ", ".join(sorted(self._kinds)) or "(none)"
                return err(f"Unknown agent {recipient!r}. Available: {available}")
            if len(inbox) >= self.config.max_inbox:
                return err(f"Inbox for {recipient!r} is full ({self.config.max_inbox}).")
            self._sequence += 1
            entry = AgentMessage(
                id=f"msg-{uuid.uuid4().hex[:8]}",
                sender=sender,
                recipient=recipient,
                message=message,
                summary=summary.strip(),
                timestamp=time.time(),
                sequence=self._sequence,
            )
            inbox.append(entry)
            self.history.append(entry)
            pending = len(inbox)
        return ok(
            f"Message delivered to {recipient} ({pending} pending). "
            "It will be injected before that agent's next model call."
        )

    def drain(self, agent_id: str) -> list[AgentMessage]:
        with self._lock:
            inbox = self._inboxes.get(agent_id)
            if not inbox:
                return []
            messages = list(inbox)
            inbox.clear()
            return messages

    def serialize(self) -> dict:
        with self._lock:
            return {
                "agents": [
                    {"id": agent_id, "kind": self._kinds.get(agent_id, "agent"), "pending": len(inbox)}
                    for agent_id, inbox in self._inboxes.items()
                    if agent_id in self._kinds
                ],
                "recent_messages": [
                    {
                        "id": entry.id,
                        "from": entry.sender,
                        "to": entry.recipient,
                        "summary": entry.summary,
                        "timestamp": entry.timestamp,
                    }
                    for entry in list(self.history)[-20:]
                ],
            }


def build_send_message_tool(bus: AgentBus, sender_id: str) -> Tool:
    def handler(args: dict) -> dict:
        recipient = str(args.get("to") or "").strip()
        message = str(args.get("message") or "")
        summary = str(args.get("summary") or "")
        if not recipient:
            return err("to is required (use the agent id, or 'main' for the coordinator)")
        return bus.send(sender_id, recipient, message, summary)

    return Tool(
        name="SendMessage",
        description=(
            "Send a message to another agent. Use the agent_id returned by a background Task, or "
            "'main' to report to the coordinator. Messages are queued and injected before the "
            "recipient's next model call; this tool does not wait for a reply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient agent id (or 'main')"},
                "message": {"type": "string", "description": "Message body"},
                "summary": {"type": "string", "description": "Optional 5-10 word preview"},
            },
            "required": ["to", "message"],
        },
        handler=handler,
        read_only=False,
    )
