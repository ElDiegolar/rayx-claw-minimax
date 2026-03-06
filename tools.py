from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
from pathlib import Path

from config import Settings
from storage import MemoryStore

log = logging.getLogger(__name__)

settings = Settings()
memory_store = MemoryStore()

# ---------------------------------------------------------------------------
# Anthropic tool definitions (JSON schema format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the file contents with line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (absolute or relative to workspace)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or overwrite a file with the given content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file (absolute or relative to workspace)",
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and directories in a path. Returns names with [DIR] or [FILE] prefix.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (absolute or relative to workspace). Defaults to workspace root.",
                    "default": ".",
                },
            },
            "required": [],
        },
    },
    {
        "name": "search_files",
        "description": "Search for files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match files against",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (absolute or relative to workspace). Defaults to workspace root.",
                    "default": ".",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "grep_files",
        "description": "Search file contents for lines matching a regex pattern. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern to search for in file contents",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (absolute or relative to workspace). Defaults to workspace root.",
                    "default": ".",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob filter for file names (e.g. '*.py', '*.ts')",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Save a piece of information to persistent memory. Use this to remember "
            "user preferences, important facts, project context, or anything worth "
            "recalling in future conversations. Memory persists across sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "A short descriptive key (e.g. 'user_name', 'current_project', 'preference_style')",
                },
                "value": {
                    "type": "string",
                    "description": "The information to remember",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "recall_memory",
        "description": (
            "Recall information from persistent memory. Call with no key to see all "
            "stored memories, or with a specific key to recall one item."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Optional key to recall. Omit to see all memories.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "delete_memory",
        "description": "Delete a specific key from persistent memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The key to delete",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command and return its output (stdout + stderr). "
            "Commands run in the workspace directory by default. Use this to "
            "start servers, run scripts, install packages, run tests, git operations, etc. "
            "Commands have a timeout (default 30s, max 300s). For long-running processes "
            "like servers, use background the command with &."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory (absolute or relative to workspace). Defaults to workspace root.",
                    "default": ".",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 300)",
                    "default": 30,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "report_progress",
        "description": (
            "Report progress on the current autonomous task. Use this to keep the user "
            "informed about what you're doing, what you've accomplished, and what's next. "
            "The message will be displayed in the progress panel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "A progress update message for the user",
                },
                "goal": {
                    "type": "string",
                    "description": "Optional: update the current goal displayed in the progress panel",
                },
            },
            "required": ["message"],
        },
    },
    {
        "name": "mark_complete",
        "description": (
            "Mark the current autonomous task as complete. Call this when you have "
            "finished all work on the user's request. Include a summary of what was done."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "A summary of what was accomplished",
                },
            },
            "required": ["summary"],
        },
    },
    {
        "name": "check_subagents",
        "description": (
            "Check the status of background sub-agents. Returns which agents are "
            "still running and the results of any that have completed. Use this "
            "after delegating tasks to monitor progress and collect results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "delegate_to_subagent",
        "description": (
            "Delegate a task to a named MiniMax sub-agent. Each agent_id maintains its own "
            "conversation history, so you can have ongoing multi-turn conversations with "
            "multiple agents. Call this tool multiple times in one turn to run agents in "
            "PARALLEL. Use descriptive agent_ids like 'researcher', 'coder', 'reviewer'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": (
                        "Name for this agent instance (e.g. 'coder', 'researcher', 'reviewer'). "
                        "Re-use the same id to continue a conversation with that agent."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "The task prompt or follow-up message for the sub-agent",
                },
                "system": {
                    "type": "string",
                    "description": "Optional system prompt to set the sub-agent's role. Only used on first message to this agent_id.",
                    "default": "",
                },
            },
            "required": ["agent_id", "prompt"],
        },
    },
]

# Tool sets by role
ORCHESTRATOR_TOOL_NAMES = {
    "read_file", "write_file", "list_directory", "search_files", "grep_files",
    "run_command", "save_memory", "recall_memory", "delete_memory",
    "report_progress", "mark_complete", "delegate_to_subagent", "check_subagents",
}
SUBAGENT_TOOL_NAMES = {
    "read_file", "write_file", "list_directory", "search_files", "grep_files",
    "run_command", "save_memory", "recall_memory", "delete_memory",
}

ORCHESTRATOR_TOOLS = [t for t in TOOL_DEFINITIONS if t["name"] in ORCHESTRATOR_TOOL_NAMES]
SUBAGENT_TOOLS = [t for t in TOOL_DEFINITIONS if t["name"] in SUBAGENT_TOOL_NAMES]


# ---------------------------------------------------------------------------
# Path sandboxing
# ---------------------------------------------------------------------------

SENSITIVE_PATTERNS = {".env", ".env.local", ".env.production", "credentials.json", "secrets.yaml"}

def _resolve_path(path_str: str) -> Path:
    """Resolve a path, ensuring it stays within the workspace and is not a symlink escape."""
    workspace = Path(settings.workspace).resolve()
    p = Path(path_str)

    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (workspace / p).resolve()

    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise PermissionError(
            f"Access denied: {path_str} is outside workspace ({workspace})"
        )

    # Block symlinks that point outside the workspace
    if resolved.is_symlink():
        real_target = resolved.resolve()
        try:
            real_target.relative_to(workspace)
        except ValueError:
            raise PermissionError(
                f"Access denied: {path_str} is a symlink pointing outside workspace"
            )

    # Block access to sensitive files
    if resolved.name.lower() in SENSITIVE_PATTERNS:
        raise PermissionError(f"Access denied: {resolved.name} is a protected file")

    return resolved


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _read_file(path: str) -> str:
    resolved = _resolve_path(path)
    if not resolved.is_file():
        return f"Error: {path} is not a file or does not exist"

    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"

    lines = text.splitlines()
    numbered = [f"{i + 1:4d}  {line}" for i, line in enumerate(lines)]

    if len(numbered) > 500:
        numbered = numbered[:500]
        numbered.append(f"\n... truncated ({len(lines)} total lines)")

    return "\n".join(numbered)


def _write_file(path: str, content: str) -> str:
    resolved = _resolve_path(path)

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"File written: {resolved} ({len(content)} bytes)"
    except Exception as e:
        return f"Error writing file: {e}"


def _list_directory(path: str = ".") -> str:
    resolved = _resolve_path(path)
    if not resolved.is_dir():
        return f"Error: {path} is not a directory or does not exist"

    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except Exception as e:
        return f"Error listing directory: {e}"

    total = len(entries)
    lines = []
    for entry in entries[:200]:
        prefix = "[DIR] " if entry.is_dir() else "[FILE]"
        lines.append(f"{prefix} {entry.name}")

    if total > 200:
        lines.append(f"\n... truncated (200 of {total} entries shown)")

    return "\n".join(lines) if lines else "(empty directory)"


def _search_files(pattern: str, path: str = ".") -> str:
    resolved = _resolve_path(path)
    if not resolved.is_dir():
        return f"Error: {path} is not a directory"

    matches = []
    for match in resolved.glob(pattern):
        if match.is_file():
            try:
                rel = match.relative_to(Path(settings.workspace).resolve())
                matches.append(str(rel))
            except ValueError:
                matches.append(str(match))

            if len(matches) >= 100:
                break

    if not matches:
        return f"No files matching '{pattern}' found in {path}"

    result = "\n".join(matches)
    if len(matches) == 100:
        result += "\n... (results capped at 100)"
    return result


def _grep_files(pattern: str, path: str = ".", glob_filter: str | None = None) -> str:
    resolved = _resolve_path(path)

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex: {e}"

    results = []
    max_results = 50

    if resolved.is_file():
        files = [resolved]
    else:
        files = []
        for root, _dirs, filenames in os.walk(resolved):
            root_path = Path(root)
            if any(part.startswith(".") or part in ("node_modules", "__pycache__", ".git")
                   for part in root_path.parts):
                continue
            for fname in filenames:
                if glob_filter and not fnmatch.fnmatch(fname, glob_filter):
                    continue
                files.append(root_path / fname)
            if len(files) > 5000:
                break

    workspace_root = Path(settings.workspace).resolve()

    for fpath in files:
        if len(results) >= max_results:
            break
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for i, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                try:
                    rel = fpath.relative_to(workspace_root)
                except ValueError:
                    rel = fpath
                results.append(f"{rel}:{i}  {line.rstrip()[:200]}")
                if len(results) >= max_results:
                    break

    if not results:
        return f"No matches for '{pattern}' in {path}"

    result = "\n".join(results)
    if len(results) == max_results:
        result += f"\n... (results capped at {max_results})"
    return result


# ---------------------------------------------------------------------------
# Shell command execution
# ---------------------------------------------------------------------------

BLOCKED_COMMANDS = re.compile(
    r"(^|\s*[;&|]\s*)(rm\s+-rf\s+/|mkfs\.|dd\s+if=|:(){ :|chmod\s+-R\s+777\s+/|curl\s+.*\|\s*sh|wget\s+.*\|\s*sh)",
    re.IGNORECASE,
)

def _run_command(command: str, cwd: str = ".", timeout: int = 30) -> str:
    import subprocess

    if BLOCKED_COMMANDS.search(command):
        return "Error: command blocked by safety filter"

    workspace = Path(settings.workspace).resolve()
    cwd_path = Path(cwd)
    if cwd_path.is_absolute():
        resolved_cwd = cwd_path.resolve()
    else:
        resolved_cwd = (workspace / cwd_path).resolve()

    try:
        resolved_cwd.relative_to(workspace)
    except ValueError:
        return f"Error: cwd '{cwd}' is outside workspace ({workspace})"

    if not resolved_cwd.is_dir():
        return f"Error: cwd '{cwd}' is not a directory"

    timeout = max(1, min(timeout, 300))

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(resolved_cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")

        output = "\n".join(output_parts) if output_parts else "(no output)"

        if len(output) > 10000:
            output = output[:10000] + f"\n... truncated ({len(output)} total chars)"

        exit_info = f"[exit code: {result.returncode}]"
        return f"{output}\n{exit_info}"

    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except Exception as e:
        return f"Error running command: {e}"


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def execute_tool(name: str, tool_input: dict) -> str:
    """Execute a tool by name and return the result as a string."""
    if name == "read_file":
        return _read_file(tool_input["path"])
    elif name == "write_file":
        return _write_file(tool_input["path"], tool_input["content"])
    elif name == "list_directory":
        return _list_directory(tool_input.get("path", "."))
    elif name == "search_files":
        return _search_files(tool_input["pattern"], tool_input.get("path", "."))
    elif name == "grep_files":
        return _grep_files(
            tool_input["pattern"],
            tool_input.get("path", "."),
            tool_input.get("glob"),
        )
    elif name == "run_command":
        return _run_command(
            tool_input["command"],
            tool_input.get("cwd", "."),
            tool_input.get("timeout", 30),
        )
    elif name == "save_memory":
        return memory_store.save(tool_input["key"], tool_input["value"])
    elif name == "recall_memory":
        return memory_store.recall(tool_input.get("key"))
    elif name == "delete_memory":
        return memory_store.delete(tool_input["key"])
    elif name == "report_progress":
        return f"Progress reported: {tool_input['message']}"
    elif name == "mark_complete":
        return f"Task marked complete: {tool_input['summary']}"
    else:
        return f"Unknown tool: {name}"


async def execute_tool_async(name: str, tool_input: dict) -> str:
    """Execute a tool in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(execute_tool, name, tool_input)
