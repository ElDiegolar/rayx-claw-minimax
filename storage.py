from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from models import TaskState, TaskStatus

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "history.json"
MEMORY_FILE = DATA_DIR / "memory.json"
TASK_STATE_FILE = DATA_DIR / "task_state.json"

MAX_HISTORY = 50


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Chat History
# ---------------------------------------------------------------------------

class HistoryStore:
    """Persists chat exchanges for UI replay and API context."""

    def __init__(self) -> None:
        _ensure_data_dir()
        self._exchanges: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not HISTORY_FILE.exists():
            return []
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as e:
            log.warning("Failed to load history: %s", e)
            return []

    def _save(self) -> None:
        try:
            HISTORY_FILE.write_text(
                json.dumps(self._exchanges, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("Failed to save history: %s", e)

    def add_exchange(
        self,
        user_message: str,
        assistant_text: str,
        ui_messages: list[dict],
    ) -> None:
        self._exchanges.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "user": user_message,
            "assistant": assistant_text,
            "ui": ui_messages,
        })
        if len(self._exchanges) > MAX_HISTORY:
            self._exchanges = self._exchanges[-MAX_HISTORY:]
        self._save()

    def get_ui_history(self) -> list[dict]:
        return list(self._exchanges)

    def get_api_messages(self) -> list[dict]:
        messages = []
        for ex in self._exchanges:
            messages.append({"role": "user", "content": ex["user"]})
            if ex.get("assistant"):
                messages.append({"role": "assistant", "content": ex["assistant"]})
        return messages

    def clear(self) -> None:
        self._exchanges = []
        self._save()


# ---------------------------------------------------------------------------
# Memory Store
# ---------------------------------------------------------------------------

class MemoryStore:
    """Persistent key-value memory that the agent can read/write."""

    def __init__(self) -> None:
        _ensure_data_dir()
        self._data: dict[str, str] = self._load()

    def _load(self) -> dict[str, str]:
        if not MEMORY_FILE.exists():
            return {}
        try:
            data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning("Failed to load memory: %s", e)
            return {}

    def _save(self) -> None:
        try:
            MEMORY_FILE.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("Failed to save memory: %s", e)

    def save(self, key: str, value: str) -> str:
        self._data[key] = value
        self._save()
        return f"Saved: {key}"

    def recall(self, key: str | None = None) -> str:
        if key:
            val = self._data.get(key)
            return val if val else f"No memory found for key: {key}"
        if not self._data:
            return "Memory is empty."
        lines = [f"- {k}: {v}" for k, v in self._data.items()]
        return "\n".join(lines)

    def delete(self, key: str) -> str:
        if key in self._data:
            del self._data[key]
            self._save()
            return f"Deleted: {key}"
        return f"Key not found: {key}"

    def get_context(self) -> str:
        if not self._data:
            return ""
        lines = [f"- {k}: {v}" for k, v in self._data.items()]
        return "Your persistent memory:\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Task State Store
# ---------------------------------------------------------------------------

class TaskStateStore:
    """Persists autonomous task state across restarts."""

    def __init__(self) -> None:
        _ensure_data_dir()
        self._state: TaskState = self._load()

    def _load(self) -> TaskState:
        if not TASK_STATE_FILE.exists():
            return TaskState()
        try:
            data = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
            return TaskState(**data)
        except Exception as e:
            log.warning("Failed to load task state: %s", e)
            return TaskState()

    def _save(self) -> None:
        try:
            TASK_STATE_FILE.write_text(
                self._state.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("Failed to save task state: %s", e)

    @property
    def state(self) -> TaskState:
        return self._state

    def update(self, **kwargs) -> TaskState:
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)
        self._save()
        return self._state

    def add_progress(self, note: str) -> None:
        self._state.add_progress(note)
        self._save()

    def reset(self) -> TaskState:
        self._state = TaskState()
        self._save()
        return self._state

    def to_dict(self) -> dict:
        return self._state.model_dump()
