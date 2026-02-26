from __future__ import annotations

import json
import re
from typing import AsyncIterator

import anthropic

from config import Settings

settings = Settings()

# MiniMax via Anthropic-compatible API
minimax_client = anthropic.AsyncAnthropic(
    base_url="https://api.minimax.io/anthropic",
    api_key=settings.minimax_api_key,
)


async def call_minimax_stream(
    messages: list[dict], system: str = ""
) -> AsyncIterator[str]:
    """Stream a response from MiniMax via Anthropic-compatible API."""
    async with minimax_client.messages.stream(
        model=settings.minimax_model,
        max_tokens=16384,
        system=system or anthropic.NOT_GIVEN,
        messages=messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def call_minimax(messages: list[dict], system: str = "") -> str:
    """Non-streaming MiniMax call. Returns the full response text."""
    response = await minimax_client.messages.create(
        model=settings.minimax_model,
        max_tokens=16384,
        system=system or anthropic.NOT_GIVEN,
        messages=messages,
    )
    return response.content[0].text


def extract_json(text: str) -> dict:
    """Extract JSON from text that might contain markdown fences or extra prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))
    raise ValueError(f"No JSON found in: {text[:200]}")
