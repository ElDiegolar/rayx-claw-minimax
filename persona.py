"""Persona loader — reads YAML persona definitions and builds system prompts."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PERSONAS_DIR = Path(__file__).parent / "personas"


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base recursively."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Persona:
    """Loaded persona with accessors and system prompt builder."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def name(self) -> str:
        return self._data.get("name", "Claw")

    @property
    def tagline(self) -> str:
        return self._data.get("tagline", "AI assistant")

    @property
    def greeting(self) -> str:
        return self._data.get("greeting", f"Hello! I'm {self.name}. How can I help?")

    @property
    def tone(self) -> str:
        return self._data.get("personality", {}).get("tone", "professional")

    @property
    def style(self) -> str:
        return self._data.get("personality", {}).get("style", "concise and helpful")

    @property
    def traits(self) -> list[str]:
        return self._data.get("personality", {}).get("traits", [])

    @property
    def role_description(self) -> str:
        return self._data.get("role", {}).get("description", "You are a capable AI assistant.")

    @property
    def instructions(self) -> list[str]:
        return self._data.get("role", {}).get("instructions", [])

    @property
    def guardrails_do(self) -> list[str]:
        return self._data.get("guardrails", {}).get("do", [])

    @property
    def guardrails_dont(self) -> list[str]:
        return self._data.get("guardrails", {}).get("dont", [])

    @property
    def avatar_emoji(self) -> str:
        return self._data.get("ui", {}).get("avatar_emoji", "🤖")

    @property
    def primary_color(self) -> str:
        return self._data.get("ui", {}).get("primary_color", "#e94560")

    @property
    def theme(self) -> str:
        return self._data.get("ui", {}).get("theme", "dark")

    @property
    def raw(self) -> dict[str, Any]:
        return self._data.copy()

    def build_system_prompt(self, workspace: str) -> str:
        sections = []

        sections.append(f"You are {self.name}, {self.role_description.strip()}")

        if self.tone or self.style:
            sections.append(f"Communication style: {self.tone}. {self.style}.")

        if self.traits:
            traits_text = "\n".join(f"- {t}" for t in self.traits)
            sections.append(f"Personality traits:\n{traits_text}")

        sections.append("""Available tools:
- Filesystem tools: read_file, write_file, list_directory, search_files, grep_files
- run_command: Execute shell commands (start servers, run scripts, install packages, git, npm, pip, etc.)
- delegate_to_subagent: Spawn a named sub-agent that runs IN THE BACKGROUND with its own tools and conversation. Use descriptive agent_ids. Call multiple times in ONE turn to run agents in PARALLEL.
- check_subagents: Check status and collect results from background sub-agents.
- save_memory / recall_memory / delete_memory: Persistent memory across sessions
- report_progress: Send status updates to the user during autonomous execution
- mark_complete: Signal that the current task is finished""")

        if self.instructions:
            instructions_text = "\n".join(f"- {i}" for i in self.instructions)
            sections.append(f"Instructions:\n{instructions_text}")

        guardrails_parts = []
        if self.guardrails_do:
            guardrails_parts.append("Always:\n" + "\n".join(f"- {g}" for g in self.guardrails_do))
        if self.guardrails_dont:
            guardrails_parts.append("Never:\n" + "\n".join(f"- {g}" for g in self.guardrails_dont))
        if guardrails_parts:
            sections.append("Guardrails:\n" + "\n".join(guardrails_parts))

        sections.append(f"Workspace root: {workspace}")

        return "\n\n".join(sections)

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tagline": self.tagline,
            "greeting": self.greeting,
            "avatar_emoji": self.avatar_emoji,
            "primary_color": self.primary_color,
            "theme": self.theme,
        }


def load_persona(name: str = "default") -> Persona:
    default_path = PERSONAS_DIR / "default.yaml"
    if not default_path.exists():
        log.warning("Default persona not found at %s, using built-in defaults", default_path)
        return Persona({})

    with open(default_path, "r", encoding="utf-8") as f:
        base_data = yaml.safe_load(f) or {}

    if name == "default":
        log.info("Loaded persona: default (%s)", base_data.get("name", "unknown"))
        return Persona(base_data)

    custom_path = PERSONAS_DIR / f"{name}.yaml"
    if not custom_path.exists():
        log.warning("Persona '%s' not found, falling back to default", name)
        return Persona(base_data)

    with open(custom_path, "r", encoding="utf-8") as f:
        custom_data = yaml.safe_load(f) or {}

    merged = _deep_merge(base_data, custom_data)
    log.info("Loaded persona: %s (%s)", name, merged.get("name", "unknown"))
    return Persona(merged)
