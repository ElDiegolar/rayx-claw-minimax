from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

from config import Settings
from minimax_api import stream_and_collect
from models import Agent, WSMessage
from rate_limiter import RateLimiter
from tools import SUBAGENT_TOOLS, execute_tool_async

log = logging.getLogger(__name__)
settings = Settings()

Send = Callable[[WSMessage], Awaitable[None]]


class SubAgent:
    """A single MiniMax sub-agent with its own conversation history and tool-use loop."""

    def __init__(
        self,
        agent_id: str,
        rate_limiter: RateLimiter,
        cancel_event: asyncio.Event,
    ) -> None:
        self.agent_id = agent_id
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self.messages: list[dict] = []
        self._system: str = ""

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise asyncio.CancelledError("Sub-agent cancelled")

    async def run(self, prompt: str, system: str, send: Send) -> str:
        """Execute a task with tool-use loop. Returns the full text output."""
        if not self.messages and system:
            self._system = system

        self.messages.append({"role": "user", "content": prompt})

        max_rounds = settings.max_subagent_rounds
        full_text = ""

        round_num = 0
        while round_num < max_rounds:
            self._check_cancelled()

            if round_num > 0:
                await send(WSMessage(
                    type="status", agent=Agent.MINIMAX,
                    content=f"Sub-agent [{self.agent_id}] continuing... (round {round_num + 1})",
                ))
            else:
                await send(WSMessage(
                    type="status", agent=Agent.MINIMAX,
                    content=f"Sub-agent [{self.agent_id}] working...",
                ))

            # Rate-limited API call
            round_text_parts = []

            async def _on_text(text: str):
                round_text_parts.append(text)
                await send(WSMessage(
                    type="chunk", agent=Agent.MINIMAX,
                    content=text,
                    tool_name=self.agent_id,
                ))

            async with self._rate_limiter:
                try:
                    response = await stream_and_collect(
                        messages=self.messages,
                        system=self._system or "",
                        tools=SUBAGENT_TOOLS,
                        on_text=_on_text,
                    )
                except RuntimeError as exc:
                    err_str = str(exc)
                    if "429" in err_str or "rate" in err_str.lower():
                        log.warning("Sub-agent [%s] hit rate limit, backing off", self.agent_id)
                        await asyncio.sleep(5)
                        continue  # retry without incrementing round_num
                    if "tool_use_id" in err_str or "tool id" in err_str.lower() or "not found" in err_str.lower():
                        log.warning("Sub-agent [%s] tool ID mismatch, skipping round: %s", self.agent_id, err_str[:200])
                        break  # exit tool loop — can't recover sub-agent context easily
                    raise

            round_num += 1

            full_text += "".join(round_text_parts)
            # Preserve full content (including thinking blocks) for round-trip
            self.messages.append({"role": "assistant", "content": response.to_content_list()})

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if not tool_uses:
                break

            # Execute tools sequentially
            tool_results = []
            for tool_use in tool_uses:
                self._check_cancelled()
                name = tool_use.name
                inp = dict(tool_use.input)

                await send(WSMessage(
                    type="status", agent=Agent.MINIMAX,
                    content=f"Sub-agent [{self.agent_id}] running {name}...",
                ))
                await send(WSMessage(
                    type="tool_use", agent=Agent.MINIMAX,
                    tool_name=name, tool_input=inp,
                ))

                result_text = await execute_tool_async(name, inp)
                log.info("Sub-agent [%s] tool %s -> %d chars", self.agent_id, name, len(result_text))

                display = result_text[:2000] + "..." if len(result_text) > 2000 else result_text
                await send(WSMessage(
                    type="tool_result", agent=Agent.SYSTEM,
                    content=display, tool_name=name,
                ))

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": result_text,
                })

            self.messages.append({"role": "user", "content": tool_results})

        return full_text


class SubAgentManager:
    """Manages named sub-agents, spawning them on demand and running them in parallel."""

    def __init__(
        self,
        rate_limiter: RateLimiter,
        cancel_event: asyncio.Event,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agents: dict[str, SubAgent] = {}
        self._background_tasks: dict[str, asyncio.Task] = {}
        self._completed_results: dict[str, str] = {}

    def _get_or_create(self, agent_id: str) -> SubAgent:
        if agent_id not in self._agents:
            log.info("Spawning new sub-agent: %s", agent_id)
            self._agents[agent_id] = SubAgent(
                agent_id=agent_id,
                rate_limiter=self._rate_limiter,
                cancel_event=self._cancel_event,
            )
        return self._agents[agent_id]

    async def spawn(
        self, agent_id: str, prompt: str, system: str, send: Send
    ) -> str:
        """Run a single sub-agent task. Returns the full text output."""
        agent = self._get_or_create(agent_id)

        await send(WSMessage(
            type="subagent_event", agent=Agent.MINIMAX,
            content=json.dumps({"action": "spawned", "agent_id": agent_id, "task": prompt[:200]}),
        ))

        result = await agent.run(prompt, system, send)

        await send(WSMessage(
            type="subagent_event", agent=Agent.MINIMAX,
            content=json.dumps({"action": "completed", "agent_id": agent_id}),
        ))

        return result

    async def spawn_parallel(
        self,
        tasks: list[dict],
        send: Send,
    ) -> list[str]:
        """Run multiple sub-agent tasks in parallel.

        tasks: list of {"agent_id": str, "prompt": str, "system": str}
        Returns list of results in same order.
        """
        max_parallel = settings.max_subagents
        tasks_to_run = tasks[:max_parallel]
        if len(tasks) > max_parallel:
            log.warning(
                "Capping parallel sub-agents from %d to %d",
                len(tasks), max_parallel,
            )

        async def _run_one(t: dict) -> str:
            return await self.spawn(
                t["agent_id"], t["prompt"], t.get("system", ""), send
            )

        results = await asyncio.gather(
            *[_run_one(t) for t in tasks_to_run],
            return_exceptions=True,
        )

        final = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                agent_id = tasks_to_run[i]["agent_id"]
                log.error("Sub-agent [%s] failed: %s", agent_id, r)
                final.append(f"Error: sub-agent [{agent_id}] failed: {r}")
            else:
                final.append(r)
        return final

    def spawn_background(
        self, agent_id: str, prompt: str, system: str, send: Send
    ) -> None:
        """Launch a sub-agent task in the background without awaiting."""
        agent = self._get_or_create(agent_id)

        async def _run():
            await send(WSMessage(
                type="subagent_event", agent=Agent.MINIMAX,
                content=json.dumps({"action": "spawned", "agent_id": agent_id, "task": prompt[:200]}),
            ))
            result = await agent.run(prompt, system, send)
            await send(WSMessage(
                type="subagent_event", agent=Agent.MINIMAX,
                content=json.dumps({"action": "completed", "agent_id": agent_id}),
            ))
            return result

        task = asyncio.create_task(_run())
        self._background_tasks[agent_id] = task
        task.add_done_callback(lambda t, aid=agent_id: self._on_background_done(aid, t))
        log.info("Spawned background sub-agent: %s", agent_id)

    def _on_background_done(self, agent_id: str, task: asyncio.Task) -> None:
        self._background_tasks.pop(agent_id, None)
        try:
            result = task.result()
            self._completed_results[agent_id] = result
        except asyncio.CancelledError:
            self._completed_results[agent_id] = "[cancelled]"
        except Exception as e:
            self._completed_results[agent_id] = f"[error: {e or repr(e)}]"
        log.info("Background sub-agent '%s' done", agent_id)

    def has_active_tasks(self) -> bool:
        return bool(self._background_tasks) or bool(self._completed_results)

    def has_running_tasks(self) -> bool:
        return bool(self._background_tasks)

    def get_status(self) -> dict:
        """Get status of all background sub-agents."""
        status = {}
        for aid in self._background_tasks:
            status[aid] = {"status": "running"}
        for aid, result in self._completed_results.items():
            status[aid] = {"status": "completed", "result": result[:2000]}
        return status

    def collect_completed(self) -> dict[str, str]:
        """Drain and return all completed results."""
        results = dict(self._completed_results)
        self._completed_results.clear()
        return results

    async def cancel_all(self) -> None:
        """Cancel all running sub-agent tasks."""
        for task_id, task in self._background_tasks.items():
            if not task.done():
                task.cancel()
                log.info("Cancelled background sub-agent: %s", task_id)
        self._background_tasks.clear()

    @property
    def active_agent_ids(self) -> list[str]:
        return list(self._agents.keys())
