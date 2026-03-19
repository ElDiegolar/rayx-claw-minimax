from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import json

from config import Settings
from minimax_api import stream_and_collect, MiniMaxResponse
from models import Agent, TaskStatus, WSMessage
from persona import load_persona
from rate_limiter import RateLimiter
from storage import HistoryStore, TaskStateStore
from subagent_manager import SubAgentManager
from iteration_engine import iteration_engine
from tools import ORCHESTRATOR_TOOLS, execute_tool, execute_tool_async, memory_store

log = logging.getLogger(__name__)
settings = Settings()

Send = Callable[[WSMessage], Awaitable[None]]


class AutonomousOrchestrator:
    """MiniMax-powered autonomous orchestrator with dual-queue architecture.

    - Runs a continuous async loop as an asyncio.Task
    - User messages are queued and injected at the top of each iteration
    - When no user input exists, the model gets an autonomous continuation prompt
    - Sub-agents are spawned in parallel via SubAgentManager
    """

    def __init__(self) -> None:
        self._persona = load_persona(settings.persona_name)
        self._rate_limiter = RateLimiter(
            max_rpm=settings.max_rpm,
            max_concurrent=settings.max_concurrent,
        )

        self._cancel_event = asyncio.Event()
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # Not paused by default

        self._user_queue: asyncio.Queue[str] = asyncio.Queue()
        self._send: Send | None = None
        self._loop_task: asyncio.Task | None = None
        self._task_complete = False

        self.history = HistoryStore()
        self.task_state = TaskStateStore()
        self.messages: list[dict] = self.history.get_api_messages()

        self.subagent_manager = SubAgentManager(
            rate_limiter=self._rate_limiter,
            cancel_event=self._cancel_event,
        )

        self._repair_messages()

        if self.messages:
            log.info("Loaded %d messages from history", len(self.messages))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_send(self, send: Send) -> None:
        self._send = send

    async def accept_user_message(self, content: str) -> None:
        """Accept a user message. Starts the autonomous loop on first message."""
        await self._user_queue.put(content)
        if self._loop_task is None or self._loop_task.done():
            self._cancel_event.clear()
            self._pause_event.set()
            self._task_complete = False
            self.task_state.reset()
            # Start fresh — don't carry stale history into the API context.
            # UI history replay is handled separately by HistoryStore.
            self.messages = []
            self._loop_task = asyncio.create_task(self._autonomous_loop())

    def pause(self) -> None:
        self._pause_event.clear()
        self.task_state.update(status=TaskStatus.PAUSED)
        log.info("Orchestrator paused")

    def resume(self) -> None:
        self._pause_event.set()
        self.task_state.update(status=TaskStatus.RUNNING)
        log.info("Orchestrator resumed")

    def cancel(self) -> None:
        self._cancel_event.set()
        self._pause_event.set()  # Unpause so the loop can exit
        self.task_state.update(status=TaskStatus.CANCELLED)
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
        log.info("Orchestrator cancelled")

    def confirm(self) -> None:
        """Legacy no-op kept for API compat."""
        log.info("Confirm received (no-op — loop auto-continues)")

    def switch_persona(self, persona_name: str) -> dict:
        """Hot-swap the active persona. Returns persona info for UI update."""
        self._persona = load_persona(persona_name)
        log.info("Orchestrator persona switched to: %s (%s)", persona_name, self._persona.name)
        return self._persona.to_api_dict()

    def get_ui_history(self) -> list[dict]:
        return self.history.get_ui_history()

    # ------------------------------------------------------------------
    # System prompt
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        system = self._persona.build_system_prompt(settings.workspace)

        # Override tool docs for autonomous mode
        system += "\n\n" + (
            "WORKSPACE RESTRICTION:\n"
            f"You are STRICTLY confined to the project workspace: {settings.workspace}\n"
            "ALL file operations (read, write, list, search, grep) and ALL shell commands "
            "MUST stay within this directory. You CANNOT access, read, write, or reference "
            "any files or paths outside this workspace. This applies to you AND all sub-agents.\n\n"
            "AUTONOMOUS MODE INSTRUCTIONS:\n"
            "You are running in autonomous mode. You are the COORDINATOR — your job is to "
            "delegate work and stay responsive to the user at all times.\n\n"
            "CRITICAL RULE: ALWAYS delegate work to sub-agents using delegate_to_subagent. "
            "NEVER execute task work (run_command, write_file, read_file, etc.) directly yourself. "
            "Your ONLY tools should be: delegate_to_subagent, check_subagents, report_progress, "
            "mark_complete, and memory tools. All actual task execution MUST go through sub-agents.\n\n"
            "This keeps you free to respond to the user immediately while sub-agents do the work.\n\n"
            "1. When you receive a task, IMMEDIATELY delegate it to one or more sub-agents. "
            "Break complex tasks into parallel sub-agents when possible — call delegate_to_subagent "
            "multiple times in a SINGLE turn to launch them in PARALLEL.\n"
            "2. Use report_progress to keep the user informed of what you delegated and why\n"
            "3. Use check_subagents to poll for results from your sub-agents\n"
            "4. When sub-agents complete, review their results and either delegate follow-up "
            "work to new sub-agents or call mark_complete if the task is done\n"
            "5. If the user sends a new message mid-execution, respond IMMEDIATELY — you are "
            "free because sub-agents are doing the heavy lifting\n"
            "6. Call mark_complete when the entire task is done\n"
            "7. Do NOT greet the user or ask clarifying questions — immediately delegate and execute\n"
            "8. Do NOT respond with just text. EVERY response MUST include at least one tool call.\n"
            "9. MEMORY: Use save_memory/recall_memory to persist important context across sessions. "
            "When delegating tasks, instruct sub-agents to use save_memory to store key findings, "
            "project details, and decisions. Sub-agents have full memory access. "
            "Always recall_memory at the start of a new task to check for relevant stored context."
        )

        mem_context = memory_store.get_context()
        if mem_context:
            system += "\n\n" + mem_context

        # Inject iteration mode context
        if iteration_engine.is_active:
            state = iteration_engine.state
            system += "\n\n" + (
                "SELF-ITERATION MODE ACTIVE\n"
                "==========================\n"
                f"Project: {state.project_path}\n"
                f"Status: {state.status} ({state.phase})\n"
                f"Cycle: {state.current_cycle} / {state.max_cycles}\n"
                f"Tokens used: {state.total_tokens_used:,} / {state.token_budget:,}\n\n"
                "ITERATION WORKFLOW:\n"
                "You are running in self-iteration mode. Follow this lifecycle:\n\n"
                "PHASE 1 — DISCOVERY (status: discovering)\n"
                "  Spin up a discovery team (2-3 sub-agents in parallel):\n"
                "  - 'analyzer': Read project files, understand architecture and purpose\n"
                "  - 'metric-finder': Determine measurable success metrics and how to collect them\n"
                "  - 'strategy-planner': Propose iteration strategies based on project type\n"
                "  When all complete, call set_iteration_plan with their combined findings.\n\n"
                "PHASE 2 — BASELINE (status: baselining)\n"
                "  Delegate to a 'runner' sub-agent to execute the project and collect baseline metrics.\n"
                "  Call record_iteration_metrics with the baseline results.\n\n"
                "PHASE 3 — ITERATE (status: iterating)\n"
                "  For each cycle:\n"
                "  a) Call advance_iteration_cycle to move to next cycle\n"
                "  b) Create a checkpoint with iteration_checkpoint\n"
                "  c) Delegate to a team:\n"
                "     - 'researcher': Analyze metrics history, decide what to improve, research approach\n"
                "     - 'implementer': Make the code changes based on researcher's plan\n"
                "     - 'validator': Run the project, collect new metrics, compare with previous\n"
                "  d) Based on validator results, call record_iteration_metrics with kept=true/false\n"
                "  e) If changes hurt metrics, use iteration_revert and record kept=false\n"
                "  f) Check iteration_status for budget — stop if running low\n"
                "  g) Loop back to (a) unless stopping conditions met\n\n"
                "STOPPING CONDITIONS:\n"
                "- Token budget exhausted (advance_iteration_cycle handles this automatically)\n"
                "- Max cycles reached\n"
                "- Diminishing returns (3+ cycles with no improvement)\n"
                "- User requests stop\n\n"
                "TOKEN EFFICIENCY:\n"
                "- Reuse sub-agent IDs across cycles to maintain context (saves prompt tokens)\n"
                "- When budget is >75% used, switch to targeted single-change iterations\n"
                "- When budget is >90% used, do a final review and stop\n\n"
                "IMPORTANT: Always use report_progress to keep the user informed of iteration progress."
            )
        else:
            system += "\n\n" + (
                "SELF-ITERATION MODE AVAILABLE\n"
                "=============================\n"
                "You have self-iteration tools available. When a user asks you to iterate on, "
                "improve, or optimize a project, use start_iteration to begin an autonomous "
                "iteration session. This works for ANY project type.\n\n"
                "Usage: start_iteration(project_path='projects/my-project')\n"
                "The system will guide you through discovery, baselining, and iterative improvement."
            )

        return system

    # ------------------------------------------------------------------
    # Message repair
    # ------------------------------------------------------------------

    def _repair_messages(self) -> None:
        """Fix dangling tool_use blocks that have no tool_result."""
        if not self.messages:
            return

        repaired = []
        i = 0
        while i < len(self.messages):
            msg = self.messages[i]
            repaired.append(msg)

            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    tool_use_ids = set()
                    for b in content:
                        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                        bid = b.get("id") if isinstance(b, dict) else getattr(b, "id", None)
                        if btype == "tool_use" and bid:
                            tool_use_ids.add(bid)

                    if tool_use_ids:
                        next_msg = self.messages[i + 1] if i + 1 < len(self.messages) else None
                        covered_ids = set()
                        if next_msg and next_msg.get("role") == "user":
                            next_content = next_msg.get("content")
                            if isinstance(next_content, list):
                                for r in next_content:
                                    if isinstance(r, dict) and r.get("type") == "tool_result":
                                        covered_ids.add(r.get("tool_use_id"))

                        missing = tool_use_ids - covered_ids
                        if missing:
                            log.warning(
                                "Repairing %d dangling tool_use block(s) at message %d",
                                len(missing), i,
                            )
                            stubs = [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "content": "[interrupted — tool was not executed]",
                                }
                                for tid in missing
                            ]
                            if (next_msg and next_msg.get("role") == "user"
                                    and isinstance(next_msg.get("content"), list)):
                                next_msg["content"].extend(stubs)
                            else:
                                repaired.append({"role": "user", "content": stubs})
            i += 1

        self.messages = repaired

    def _truncate_messages(self, max_count: int) -> list[dict]:
        """Trim messages while preserving tool_use/tool_result pairs.

        Keeps the first user message and the last N messages, but ensures
        we never split an assistant tool_use from its following tool_result.
        Injects a summary of dropped content so the model retains awareness.
        After truncation, runs repair to patch any remaining orphans.
        """
        if len(self.messages) <= max_count:
            return self.messages

        # We want to keep messages[:1] + tail. Find a safe cut point in the tail.
        # Reserve 2 slots: first message + context summary
        tail_size = max_count - 2
        cut = len(self.messages) - tail_size

        # Walk forward from cut to find a safe boundary — don't start mid-pair.
        # A safe start is a user message that is NOT a tool_result list,
        # or an assistant message that has no tool_use blocks.
        while cut < len(self.messages) - 2:
            msg = self.messages[cut]
            role = msg.get("role")
            content = msg.get("content")

            # If it's a user message with tool_results, we need the preceding
            # assistant message too — back up.
            if role == "user" and isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    cut += 1
                    continue

            # If it's an assistant message, it's a safe start (its tool_results
            # will follow it in the tail).
            break

        # Build a summary of the dropped messages so the model knows what happened
        dropped = self.messages[1:cut]
        summary = self._summarize_dropped(dropped)

        trimmed = self.messages[:1]
        if summary:
            trimmed.append({"role": "user", "content": summary})
            trimmed.append({"role": "assistant", "content": "Understood. I have context from the earlier rounds and will continue accordingly."})
        trimmed.extend(self.messages[cut:])

        # Repair any orphaned tool_use/tool_result pairs created by the cut
        old_messages = self.messages
        self.messages = trimmed
        self._repair_messages()
        result = self.messages
        self.messages = old_messages
        return result

    def _summarize_dropped(self, dropped: list[dict]) -> str:
        """Extract key context from dropped messages to preserve awareness."""
        tools_used = []
        user_requests = []
        progress_notes = []

        for msg in dropped:
            role = msg.get("role")
            content = msg.get("content")

            if role == "user" and isinstance(content, str):
                # Capture user messages (skip tool results)
                text = content.strip()
                if text and not text.startswith("[Sub-agent") and len(text) < 500:
                    user_requests.append(text[:200])

            elif role == "assistant" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_use":
                            name = block.get("name", "")
                            inp = block.get("input", {})
                            if name == "report_progress":
                                progress_notes.append(inp.get("message", "")[:200])
                            elif name not in tools_used:
                                tools_used.append(name)

            elif role == "user" and isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result_text = block.get("content", "")
                        if isinstance(result_text, str) and "error" in result_text.lower():
                            progress_notes.append(f"Tool error: {result_text[:150]}")

        if not tools_used and not user_requests and not progress_notes:
            return ""

        parts = ["[CONTEXT FROM EARLIER ROUNDS — messages were truncated to fit context window]"]

        if user_requests:
            parts.append("User requests/messages:")
            for req in user_requests[:5]:
                parts.append(f"  - {req}")

        if progress_notes:
            parts.append("Progress so far:")
            for note in progress_notes[:10]:
                parts.append(f"  - {note}")

        if tools_used:
            parts.append(f"Tools used: {', '.join(tools_used[:15])}")

        # Include task state for extra context
        if self.task_state.current_goal:
            parts.append(f"Current goal: {self.task_state.current_goal}")

        recent_progress = self.task_state.progress_notes[-5:] if self.task_state.progress_notes else []
        if recent_progress:
            parts.append("Recent progress notes from task state:")
            for note in recent_progress:
                parts.append(f"  - {note}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Autonomous loop
    # ------------------------------------------------------------------

    async def _autonomous_loop(self) -> None:
        """Continuous loop: drain queue -> build turn -> stream -> execute tools -> repeat.

        Sub-agent delegations run in the background. The loop auto-continues
        when sub-agents are active, collecting their results as they finish
        while remaining responsive to user input at all times.
        """
        send = self._send
        if not send:
            log.error("No send callback set")
            return

        ui_messages: list[dict] = []
        text_parts: list[str] = []
        first_user_msg = ""

        async def send_and_record(msg: WSMessage):
            ui_messages.append(msg.model_dump())
            try:
                await send(msg)
            except Exception:
                pass

        async def _safe_send(msg: WSMessage):
            try:
                await send(msg)
            except Exception:
                pass

        try:
            self.task_state.update(status=TaskStatus.RUNNING)
            await _safe_send(WSMessage(
                type="task_state", agent=Agent.SYSTEM,
                task_state=self.task_state.to_dict(),
            ))

            max_rounds = settings.max_orchestrator_rounds
            round_num = 0
            nudge_pending = False
            has_pending_turn = False  # True when tool_results already appended

            while round_num < max_rounds:
                # 1. Check cancel
                if self._cancel_event.is_set():
                    log.info("Autonomous loop: cancelled at round %d", round_num)
                    break

                # 2. Wait on pause
                if not self._pause_event.is_set():
                    self.task_state.update(status=TaskStatus.PAUSED)
                    await _safe_send(WSMessage(
                        type="task_state", agent=Agent.SYSTEM,
                        task_state=self.task_state.to_dict(),
                    ))
                    await _safe_send(WSMessage(
                        type="status", agent=Agent.SYSTEM,
                        content="Paused. Waiting to resume...",
                    ))
                    await self._pause_event.wait()
                    if self._cancel_event.is_set():
                        break
                    self.task_state.update(status=TaskStatus.RUNNING)
                    await _safe_send(WSMessage(
                        type="task_state", agent=Agent.SYSTEM,
                        task_state=self.task_state.to_dict(),
                    ))

                # 3. Drain user queue (non-blocking)
                user_messages = []
                while not self._user_queue.empty():
                    try:
                        user_messages.append(self._user_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # 3b. Collect completed sub-agent results
                completed_subagents = self.subagent_manager.collect_completed()
                if completed_subagents:
                    active_ids = list(self.subagent_manager._background_tasks.keys())
                    self.task_state.update(subagents_active=active_ids)
                    await _safe_send(WSMessage(
                        type="task_state", agent=Agent.SYSTEM,
                        task_state=self.task_state.to_dict(),
                    ))

                # 4. Build turn
                if has_pending_turn:
                    # Tool results already appended — go straight to API call.
                    # Inject any user messages that arrived during tool execution.
                    has_pending_turn = False
                    if user_messages:
                        last = self.messages[-1]
                        if isinstance(last.get("content"), list):
                            last["content"].append({
                                "type": "text",
                                "text": "[New user message]: " + "\n\n".join(user_messages),
                            })
                        else:
                            self.messages.append({"role": "assistant", "content": "Acknowledged."})
                            self.messages.append({"role": "user", "content": "\n\n".join(user_messages)})
                elif nudge_pending:
                    # Nudge message already appended — go straight to API call.
                    # But if a user message arrived, replace the nudge with it.
                    nudge_pending = False
                    if user_messages:
                        self.messages[-1] = {"role": "user", "content": "\n\n".join(user_messages)}
                elif user_messages:
                    combined = "\n\n".join(user_messages)
                    if round_num == 0:
                        first_user_msg = combined
                    # Append sub-agent results alongside user message
                    if completed_subagents:
                        sub_parts = []
                        for aid, result in completed_subagents.items():
                            sub_parts.append(f"[Sub-agent '{aid}' completed]\n{result}")
                        combined += "\n\n" + "\n\n".join(sub_parts)
                    self.messages.append({"role": "user", "content": combined})
                elif round_num == 0:
                    # First round but no message yet — wait for one
                    first_msg = await self._user_queue.get()
                    first_user_msg = first_msg
                    self.messages.append({"role": "user", "content": first_msg})
                elif completed_subagents:
                    # Sub-agents finished — feed results back to model
                    parts = []
                    for aid, result in completed_subagents.items():
                        parts.append(f"[Sub-agent '{aid}' completed]\n{result}")
                    if self.subagent_manager.has_running_tasks():
                        active = list(self.subagent_manager._background_tasks.keys())
                        parts.append(f"Sub-agents still running: {', '.join(active)}")
                    self.messages.append({"role": "user", "content": "\n\n".join(parts)})
                elif self.subagent_manager.has_running_tasks():
                    # Sub-agents still running, no user input, no completions yet
                    # Wait briefly for a completion or user message before continuing
                    got_input = False
                    for _ in range(20):  # up to ~5 seconds
                        if not self._user_queue.empty():
                            got_input = True
                            break
                        if self.subagent_manager._completed_results:
                            break  # Non-destructive peek — collect_completed on next iteration
                        if self._cancel_event.is_set() or not self._pause_event.is_set():
                            break
                        await asyncio.sleep(0.25)

                    if got_input:
                        # User sent a message — drain it and make an API call
                        # so the orchestrator stays responsive during delegation
                        queued = []
                        while not self._user_queue.empty():
                            try:
                                queued.append(self._user_queue.get_nowait())
                            except asyncio.QueueEmpty:
                                break
                        if queued:
                            active = list(self.subagent_manager._background_tasks.keys())
                            context = "\n\n".join(queued)
                            if active:
                                context += f"\n\n[Sub-agents still running: {', '.join(active)}]"
                            self.messages.append({"role": "user", "content": context})
                            # Fall through to API call below
                        else:
                            continue
                    else:
                        # No user input — loop back to re-drain queues and completions
                        continue
                else:
                    # No sub-agents, no user input — wait for a new user message
                    await _safe_send(WSMessage(
                        type="status", agent=Agent.SYSTEM,
                        content=f"{self._persona.name} waiting for input...",
                    ))
                    # Block until user sends a message
                    while self._user_queue.empty():
                        if self._cancel_event.is_set():
                            break
                        await asyncio.sleep(0.1)
                    if self._cancel_event.is_set():
                        break
                    queued = []
                    while not self._user_queue.empty():
                        try:
                            queued.append(self._user_queue.get_nowait())
                        except asyncio.QueueEmpty:
                            break
                    if queued:
                        self.messages.append({"role": "user", "content": "\n\n".join(queued)})
                    else:
                        continue

                # Increment round counter (only when we're about to make an API call)
                round_num += 1
                self.task_state.update(round_number=round_num)
                await _safe_send(WSMessage(
                    type="task_state", agent=Agent.SYSTEM,
                    task_state=self.task_state.to_dict(),
                ))

                # Late-drain: pick up any messages that arrived during tool execution
                late_messages = []
                while not self._user_queue.empty():
                    try:
                        late_messages.append(self._user_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if late_messages:
                    combined_late = "\n\n".join(late_messages)
                    # Inject into the last user message so the model sees it this round
                    last_msg = self.messages[-1] if self.messages else None
                    if last_msg and last_msg.get("role") == "user":
                        existing = last_msg["content"]
                        if isinstance(existing, str):
                            last_msg["content"] = existing + "\n\n[New user message]: " + combined_late
                        elif isinstance(existing, list):
                            # tool_results list — append a text block alongside
                            existing.append({"type": "text", "text": "[New user message]: " + combined_late})
                        else:
                            self.messages.append({"role": "assistant", "content": "Acknowledged."})
                            self.messages.append({"role": "user", "content": combined_late})
                    else:
                        self.messages.append({"role": "assistant", "content": "Acknowledged."})
                        self.messages.append({"role": "user", "content": combined_late})

                # Trim message list to avoid unbounded context growth
                MAX_API_MESSAGES = 80
                if len(self.messages) > MAX_API_MESSAGES:
                    self.messages = self._truncate_messages(MAX_API_MESSAGES)

                if round_num > 1:
                    await send_and_record(WSMessage(
                        type="status", agent=Agent.MINIMAX,
                        content=f"{self._persona.name} thinking... (round {round_num})",
                    ))

                # 5. Stream MiniMax response
                log.info(
                    "Round %d: sending %d messages, %d tools to API",
                    round_num, len(self.messages), len(ORCHESTRATOR_TOOLS),
                )
                response = await self._stream_response(send_and_record, text_parts)

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                log.info(
                    "Round %d: stop_reason=%s, tool_uses=%d, content_types=%s",
                    round_num, response.stop_reason, len(tool_uses),
                    [b.type for b in response.content],
                )

                # Preserve full content (including thinking blocks) for round-trip
                assistant_content = response.to_content_list()

                if response.stop_reason == "end_turn" or not tool_uses:
                    self.messages.append({"role": "assistant", "content": assistant_content})
                    if self._task_complete:
                        break
                    # If the model just talked without using tools and the task
                    # isn't done, nudge it to actually act (up to 3 nudges).
                    if round_num <= 3 and not self.subagent_manager.has_running_tasks():
                        self.messages.append({
                            "role": "user",
                            "content": (
                                "You responded with text only. You are in AUTONOMOUS MODE — "
                                "you MUST use your tools to execute the task, not just describe "
                                "what you would do. Use tools NOW to fulfill the user's request."
                            ),
                        })
                        nudge_pending = True
                    continue

                self.messages.append({"role": "assistant", "content": assistant_content})

                # 6. Partition: delegation vs regular tools
                delegation_calls = []
                other_calls = []
                for tool_use in tool_uses:
                    if tool_use.name == "delegate_to_subagent":
                        delegation_calls.append(tool_use)
                    else:
                        other_calls.append(tool_use)

                tool_results = []

                # 7a. Execute regular tools sequentially
                for i, tool_use in enumerate(other_calls):
                    if self._cancel_event.is_set():
                        break

                    # Acknowledge any user messages that arrived during tool execution
                    if not self._user_queue.empty():
                        await _safe_send(WSMessage(
                            type="status", agent=Agent.SYSTEM,
                            content="Message received — will respond after current tools complete.",
                        ))

                    name = tool_use.name
                    inp = dict(tool_use.input)

                    await send_and_record(WSMessage(
                        type="status", agent=Agent.MINIMAX,
                        content=f"{self._persona.name} running {name}..." + (
                            f" ({i+1}/{len(other_calls)})" if len(other_calls) > 1 else ""
                        ),
                    ))
                    await send_and_record(WSMessage(
                        type="tool_use", agent=Agent.MINIMAX,
                        tool_name=name, tool_input=inp,
                    ))

                    # Handle special tools
                    if name == "report_progress":
                        msg = inp.get("message", "")
                        goal = inp.get("goal")
                        self.task_state.add_progress(msg)
                        if goal:
                            self.task_state.update(current_goal=goal)
                        await _safe_send(WSMessage(
                            type="progress", agent=Agent.MINIMAX,
                            content=msg,
                            task_state=self.task_state.to_dict(),
                        ))

                    if name == "mark_complete":
                        self._task_complete = True
                        self.task_state.update(
                            status=TaskStatus.COMPLETED,
                            completed_at=datetime.now(timezone.utc).isoformat(),
                        )
                        self.task_state.add_progress(f"COMPLETE: {inp.get('summary', '')}")
                        await _safe_send(WSMessage(
                            type="task_state", agent=Agent.SYSTEM,
                            task_state=self.task_state.to_dict(),
                        ))

                    if name == "check_subagents":
                        # Handle inline — needs access to subagent_manager
                        status = self.subagent_manager.get_status()
                        completed = self.subagent_manager.collect_completed()
                        for aid, result in completed.items():
                            status[aid] = {"status": "completed", "result": result[:2000]}
                        result_text = json.dumps(status, indent=2) if status else "No active or recently completed sub-agents."
                    else:
                        result_text = await execute_tool_async(name, inp)

                    log.info("Tool %s -> %d chars", name, len(result_text))

                    display = result_text[:2000] + "..." if len(result_text) > 2000 else result_text
                    await send_and_record(WSMessage(
                        type="tool_result", agent=Agent.SYSTEM,
                        content=display, tool_name=name,
                    ))

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result_text,
                    })

                # 7b. Launch delegations in background (non-blocking)
                if delegation_calls and not self._cancel_event.is_set():
                    agent_ids = [dict(tc.input).get("agent_id", "default") for tc in delegation_calls]
                    self.task_state.update(subagents_active=agent_ids)

                    await send_and_record(WSMessage(
                        type="status", agent=Agent.MINIMAX,
                        content=f"Launching {len(delegation_calls)} sub-agent(s) in background: {', '.join(agent_ids)}",
                    ))

                    for i, tc in enumerate(delegation_calls):
                        inp = dict(tc.input)
                        agent_id = inp.get("agent_id", "default")

                        await send_and_record(WSMessage(
                            type="tool_use", agent=Agent.MINIMAX,
                            tool_name="delegate_to_subagent",
                            tool_input=inp,
                        ))

                        self.subagent_manager.spawn_background(
                            agent_id, inp["prompt"], inp.get("system", ""), send_and_record
                        )

                        # Stagger sub-agent launches to avoid rate limit bursts
                        if i < len(delegation_calls) - 1:
                            await asyncio.sleep(2)

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tc.id,
                            "content": (
                                f"Sub-agent '{agent_id}' launched in background. "
                                "Use check_subagents tool to monitor progress and collect results."
                            ),
                        })

                    await _safe_send(WSMessage(
                        type="task_state", agent=Agent.SYSTEM,
                        task_state=self.task_state.to_dict(),
                    ))

                self.messages.append({"role": "user", "content": tool_results})
                has_pending_turn = True  # Signal next iteration to call API immediately

                # 8. Check completion
                if self._task_complete:
                    break

        except (asyncio.CancelledError, Exception) as exc:
            if isinstance(exc, asyncio.CancelledError):
                log.info("Autonomous loop cancelled")
                await _safe_send(WSMessage(
                    type="status", agent=Agent.SYSTEM, content="Cancelled.",
                ))
            else:
                log.exception("Autonomous loop error")
                err_msg = str(exc) or repr(exc) or f"{type(exc).__name__}: (no details)"
                await _safe_send(WSMessage(
                    type="error", agent=Agent.SYSTEM, content=err_msg,
                ))
                self.task_state.update(status=TaskStatus.FAILED)
        finally:
            await self.subagent_manager.cancel_all()
            self._repair_messages()
            await _safe_send(WSMessage(type="done", agent=Agent.SYSTEM))
            await _safe_send(WSMessage(
                type="task_state", agent=Agent.SYSTEM,
                task_state=self.task_state.to_dict(),
            ))
            assistant_text = "\n".join(text_parts)
            if first_user_msg:
                self.history.add_exchange(first_user_msg, assistant_text, ui_messages)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def _stream_response(self, send: Send, text_parts: list[str]) -> MiniMaxResponse:
        """Stream MiniMax response with rate limiting, sending text chunks to UI."""
        collected_text: list[str] = []

        async def on_text(text: str):
            collected_text.append(text)
            await send(WSMessage(
                type="chunk", agent=Agent.MINIMAX,
                content=text,
            ))

        async with self._rate_limiter:
            max_retries = 5
            for attempt in range(max_retries + 1):
                try:
                    response = await stream_and_collect(
                        messages=self.messages,
                        system=self._build_system_prompt(),
                        tools=ORCHESTRATOR_TOOLS,
                        on_text=on_text,
                    )
                    break  # success
                except RuntimeError as exc:
                    err_str = str(exc)
                    if "429" in err_str or "rate" in err_str.lower():
                        if attempt >= max_retries:
                            raise
                        backoff = min(5 * (2 ** attempt), 60)  # 5s, 10s, 20s, 40s, 60s
                        log.warning(
                            "Orchestrator hit rate limit (attempt %d/%d), backing off %ds",
                            attempt + 1, max_retries, backoff,
                        )
                        await asyncio.sleep(backoff)
                        collected_text.clear()  # reset partial text on retry
                        continue
                    elif "tool_use_id" in err_str or "tool id" in err_str.lower() or "not found" in err_str.lower():
                        log.warning("Tool ID mismatch — repairing messages and retrying: %s", err_str[:200])
                        self._repair_messages()
                        response = await stream_and_collect(
                            messages=self.messages,
                            system=self._build_system_prompt(),
                            tools=ORCHESTRATOR_TOOLS,
                            on_text=on_text,
                        )
                        break
                    else:
                        raise

        full_text = "".join(collected_text)
        if full_text.strip():
            text_parts.append(full_text)

        # Track token usage for iteration budget
        if iteration_engine.is_active and response.usage:
            total_tokens = (
                response.usage.get("input_tokens", 0)
                + response.usage.get("output_tokens", 0)
            )
            if total_tokens:
                iteration_engine.add_token_usage(total_tokens)

        return response
