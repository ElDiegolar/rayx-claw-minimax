from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel


class Agent(str, Enum):
    MINIMAX = "minimax"
    SYSTEM = "system"


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    SUBAGENT = "subagent"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class TaskState(BaseModel):
    """Persistent state for the autonomous orchestrator."""
    status: TaskStatus = TaskStatus.PENDING
    current_goal: str = ""
    round_number: int = 0
    progress_notes: list[str] = []
    subagents_active: list[str] = []
    completed_at: Optional[str] = None

    def add_progress(self, note: str) -> None:
        self.progress_notes.append(note)
        if len(self.progress_notes) > 100:
            self.progress_notes = self.progress_notes[-100:]


class WSMessage(BaseModel):
    """Server -> Client WebSocket message."""
    type: Literal[
        "chunk", "status", "error", "done", "tool_use", "tool_result",
        "history", "progress", "subagent_event", "task_state", "persona_update",
        "await_confirm", "iteration_state"
    ]
    agent: Agent
    content: str = ""
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    history: Optional[list] = None
    task_state: Optional[dict] = None


class UserMessage(BaseModel):
    """Client -> Server WebSocket message."""
    type: Literal["message", "cancel", "pause", "resume", "switch_persona", "confirm"]
    content: str = ""
