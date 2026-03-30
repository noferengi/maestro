"""
app/agent/manual_session.py
-----------------------------
ManualSession — a human-controlled tool execution session.

The human acts as the reasoning layer: they pick tools, see results,
add notes, and decide when to end.  No LLM is invoked.
"""

import uuid
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ManualSession:
    session_id: str
    task_id: str
    task_title: str
    messages: list = field(default_factory=list)
    status: str = "active"  # 'active' | 'ended'
    signal: str | None = None
    summary: str | None = None

    @classmethod
    def create(cls, task_id: str, task_title: str, initial_context: str) -> "ManualSession":
        session_id = str(uuid.uuid4())
        session = cls(session_id=session_id, task_id=task_id, task_title=task_title)
        session.messages.append({
            "role": "system",
            "content": initial_context,
        })
        return session

    def add_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def record_tool_call(self, tool_name: str, arguments: dict, result: str) -> None:
        self.messages.append({
            "role": "tool_call",
            "tool_name": tool_name,
            "arguments": arguments,
            "content": f"[TOOL] {tool_name}",
        })
        self.messages.append({
            "role": "tool_result",
            "tool_name": tool_name,
            "content": result,
        })

    def end(self, signal: str, summary: str) -> None:
        self.status = "ended"
        self.signal = signal
        self.summary = summary
        self.messages.append({
            "role": "system",
            "content": f"[SESSION ENDED] Signal: {signal}. Summary: {summary}",
        })


# In-memory session registry — lost on server restart
_ACTIVE_MANUAL_SESSIONS: dict[str, ManualSession] = {}
