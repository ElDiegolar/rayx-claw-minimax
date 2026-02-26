from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import anthropic

from agents import minimax_client
from config import Settings
from models import Agent, WSMessage
from rate_limiter import RateLimiter
from tools import SUBAGENT_TOOLS, execute_tool

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

        for round_num in range(max_rounds):
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
            round_text = ""
            async with self._rate_limiter:
                try:
                    async with minimax_client.messages.stream(
                        model=settings.minimax_model,
                        max_tokens=16384,
                        system=self._system or anthropic.NOT_GIVEN,
                        messages=self.messages,
                        tools=SUBAGENT_TOOLS,
                    ) as stream:
                        async for event in stream:
                            if event.type == "content_block_delta":
                                if hasattr(event.delta, "text") and event.delta.text:
                                    round_text += event.delta.text
                                    await send(WSMessage(
                                        type="chunk", agent=Agent.MINIMAX,
                                        content=event.delta.text,
                                        tool_name=self.agent_id,
                                    ))

                        response = await stream.get_final_message()
                except anthropic.RateLimitError:
                    log.warning("Sub-agent [%s] hit rate limit, backing off", self.agent_id)
                    await asyncio.sleep(5)
                    continue

            full_text += round_text
            self.messages.append({"role": "assistant", "content": response.content})

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

                result_text = execute_tool(name, inp)
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
        self._running_tasks: dict[str, asyncio.Task] = {}

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
            content=f"spawned:{agent_id}",
        ))

        result = await agent.run(prompt, system, send)

        await send(WSMessage(
            type="subagent_event", agent=Agent.MINIMAX,
            content=f"completed:{agent_id}",
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

    async def cancel_all(self) -> None:
        """Cancel all running sub-agent tasks."""
        for task_id, task in self._running_tasks.items():
            if not task.done():
                task.cancel()
                log.info("Cancelled sub-agent task: %s", task_id)
        self._running_tasks.clear()

    @property
    def active_agent_ids(self) -> list[str]:
        return list(self._agents.keys())
