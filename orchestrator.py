from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable

import anthropic

from agents import minimax_client
from config import Settings
from models import Agent, TaskStatus, WSMessage
from persona import load_persona
from rate_limiter import RateLimiter
from storage import HistoryStore, TaskStateStore
from subagent_manager import SubAgentManager
from tools import ORCHESTRATOR_TOOLS, execute_tool, memory_store

log = logging.getLogger(__name__)
settings = Settings()

Send = Callable[[WSMessage], Awaitable[None]]

CONTINUATION_PROMPT = (
    "Continue working on the current task. Review what you've done so far, "
    "decide what to do next, and proceed. If the task is complete, call mark_complete. "
    "If you need more information from the user, explain what you need and wait."
)


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
            "AUTONOMOUS MODE INSTRUCTIONS:\n"
            "You are running in autonomous mode. You should:\n"
            "1. Plan your approach before starting work\n"
            "2. Execute each step using available tools\n"
            "3. Use report_progress to keep the user informed\n"
            "4. Use delegate_to_subagent for parallel work — each sub-agent has "
            "its own conversation and tool access\n"
            "5. Call mark_complete when the entire task is done\n"
            "6. If the user sends a new message, acknowledge it and adapt your plan\n"
            "7. Be thorough — verify your work by reading files after writing, "
            "running tests after coding, etc."
        )

        mem_context = memory_store.get_context()
        if mem_context:
            system += "\n\n" + mem_context

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

    # ------------------------------------------------------------------
    # Autonomous loop
    # ------------------------------------------------------------------

    async def _autonomous_loop(self) -> None:
        """Continuous loop: drain queue -> build turn -> stream -> execute tools -> repeat."""
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

            for round_num in range(max_rounds):
                # 1. Check cancel
                if self._cancel_event.is_set():
                    log.info("Autonomous loop: cancelled at round %d", round_num)
                    break

                # 2. Wait on pause
                if not self._pause_event.is_set():
                    await _safe_send(WSMessage(
                        type="status", agent=Agent.SYSTEM,
                        content="Paused. Waiting to resume...",
                    ))
                    await self._pause_event.wait()
                    if self._cancel_event.is_set():
                        break

                # 3. Drain user queue (non-blocking)
                user_messages = []
                while not self._user_queue.empty():
                    try:
                        user_messages.append(self._user_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # 4. Build turn
                if user_messages:
                    combined = "\n\n".join(user_messages)
                    if round_num == 0:
                        first_user_msg = combined
                    self.messages.append({"role": "user", "content": combined})
                elif round_num == 0:
                    # First round but no message yet — wait for one
                    first_msg = await self._user_queue.get()
                    first_user_msg = first_msg
                    self.messages.append({"role": "user", "content": first_msg})
                else:
                    # Autonomous continuation
                    self.messages.append({"role": "user", "content": CONTINUATION_PROMPT})

                # Update task state
                self.task_state.update(round_number=round_num + 1)
                await _safe_send(WSMessage(
                    type="task_state", agent=Agent.SYSTEM,
                    task_state=self.task_state.to_dict(),
                ))

                if round_num > 0:
                    await send_and_record(WSMessage(
                        type="status", agent=Agent.MINIMAX,
                        content=f"{self._persona.name} thinking... (round {round_num + 1})",
                    ))

                # 5. Stream MiniMax response
                response = await self._stream_response(send_and_record, text_parts)

                tool_uses = [b for b in response.content if b.type == "tool_use"]

                if response.stop_reason == "end_turn" or not tool_uses:
                    self.messages.append({"role": "assistant", "content": response.content})
                    if self._task_complete:
                        break
                    continue

                self.messages.append({"role": "assistant", "content": response.content})

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

                    result_text = execute_tool(name, inp)
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

                # 7b. Execute delegations in parallel
                if delegation_calls and not self._cancel_event.is_set():
                    agent_ids = [dict(tc.input).get("agent_id", "default") for tc in delegation_calls]
                    self.task_state.update(subagents_active=agent_ids)

                    await send_and_record(WSMessage(
                        type="status", agent=Agent.MINIMAX,
                        content=f"Launching {len(delegation_calls)} sub-agent(s): {', '.join(agent_ids)}",
                    ))

                    async def _run_delegation(tool_use):
                        inp = dict(tool_use.input)
                        agent_id = inp.get("agent_id", "default")

                        await send_and_record(WSMessage(
                            type="tool_use", agent=Agent.MINIMAX,
                            tool_name="delegate_to_subagent",
                            tool_input=inp,
                        ))

                        result_text = await self.subagent_manager.spawn(
                            agent_id, inp["prompt"], inp.get("system", ""), send_and_record
                        )
                        log.info("Sub-agent [%s] -> %d chars", agent_id, len(result_text))

                        display = result_text[:2000] + "..." if len(result_text) > 2000 else result_text
                        await send_and_record(WSMessage(
                            type="tool_result", agent=Agent.SYSTEM,
                            content=display, tool_name=f"subagent:{agent_id}",
                        ))

                        return {
                            "type": "tool_result",
                            "tool_use_id": tool_use.id,
                            "content": result_text,
                        }

                    delegation_results = await asyncio.gather(
                        *[_run_delegation(tc) for tc in delegation_calls],
                        return_exceptions=True,
                    )

                    for r in delegation_results:
                        if isinstance(r, Exception):
                            log.error("Delegation failed: %s", r)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": "error",
                                "content": f"Delegation error: {r}",
                            })
                        else:
                            tool_results.append(r)

                    self.task_state.update(subagents_active=[])

                self.messages.append({"role": "user", "content": tool_results})

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
                await _safe_send(WSMessage(
                    type="error", agent=Agent.SYSTEM, content=str(exc),
                ))
                self.task_state.update(status=TaskStatus.FAILED)
        finally:
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

    async def _stream_response(self, send: Send, text_parts: list[str]):
        """Stream MiniMax response with rate limiting, sending text chunks to UI."""
        collected_text = []

        async with self._rate_limiter:
            try:
                async with minimax_client.messages.stream(
                    model=settings.minimax_model,
                    max_tokens=16384,
                    system=self._build_system_prompt(),
                    messages=self.messages,
                    tools=ORCHESTRATOR_TOOLS,
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            if hasattr(event.delta, "text") and event.delta.text:
                                collected_text.append(event.delta.text)
                                await send(WSMessage(
                                    type="chunk", agent=Agent.MINIMAX,
                                    content=event.delta.text,
                                ))

                    response = await stream.get_final_message()
            except anthropic.RateLimitError:
                log.warning("Orchestrator hit rate limit, backing off 5s")
                await asyncio.sleep(5)
                # Retry once
                async with minimax_client.messages.stream(
                    model=settings.minimax_model,
                    max_tokens=16384,
                    system=self._build_system_prompt(),
                    messages=self.messages,
                    tools=ORCHESTRATOR_TOOLS,
                ) as stream:
                    async for event in stream:
                        if event.type == "content_block_delta":
                            if hasattr(event.delta, "text") and event.delta.text:
                                collected_text.append(event.delta.text)
                                await send(WSMessage(
                                    type="chunk", agent=Agent.MINIMAX,
                                    content=event.delta.text,
                                ))

                    response = await stream.get_final_message()

        full_text = "".join(collected_text)
        if full_text.strip():
            text_parts.append(full_text)

        return response
