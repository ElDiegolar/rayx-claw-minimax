"""Dedicated MiniMax M2.5 API handler using httpx directly.

Talks to MiniMax's Anthropic-compatible endpoint without the Anthropic SDK,
giving us full control over request/response handling and proper preservation
of thinking blocks for interleaved reasoning.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from config import Settings

log = logging.getLogger(__name__)
settings = Settings()

BASE_URL = "https://api.minimax.io/anthropic/v1/messages"

# Shared client for connection pooling — reused across all API calls
_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    """Get or create the shared httpx client."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=120)
    return _shared_client


@dataclass
class ContentBlock:
    """A single content block in a response (text, tool_use, thinking, etc.)."""
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict = field(default_factory=dict)
    # Preserve any extra fields from MiniMax (thinking, etc.)
    _raw: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        """Serialize back for API round-trip — preserves all original fields."""
        if self._raw:
            return dict(self._raw)
        if self.type == "text":
            return {"type": "text", "text": self.text}
        if self.type == "tool_use":
            return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}
        return {"type": self.type, "text": self.text}


@dataclass
class MiniMaxResponse:
    """Parsed response from MiniMax API."""
    content: list[ContentBlock]
    stop_reason: str = "end_turn"
    usage: dict = field(default_factory=dict)

    def to_content_list(self) -> list[dict]:
        """Serialize content for round-trip in messages — preserves thinking blocks."""
        return [b.to_dict() for b in self.content]


def _parse_content_block(raw: dict) -> ContentBlock:
    """Parse a single content block from the API response."""
    block_type = raw.get("type", "text")
    return ContentBlock(
        type=block_type,
        text=raw.get("text", ""),
        id=raw.get("id", ""),
        name=raw.get("name", ""),
        input=raw.get("input", {}),
        _raw=raw,
    )


def _build_request(
    messages: list[dict],
    system: str,
    tools: list[dict] | None,
    max_tokens: int,
    stream: bool,
) -> dict:
    """Build the API request body."""
    body: dict[str, Any] = {
        "model": settings.minimax_model,
        "max_tokens": max_tokens,
        "messages": _serialize_messages(messages),
    }
    if system:
        body["system"] = system
    if tools:
        body["tools"] = tools
    if stream:
        body["stream"] = True
    return body


def _serialize_messages(messages: list[dict]) -> list[dict]:
    """Serialize messages for the API, converting content blocks properly."""
    result = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content")

        if isinstance(content, str):
            result.append({"role": role, "content": content})
        elif isinstance(content, list):
            serialized_content = []
            for block in content:
                if isinstance(block, dict):
                    serialized_content.append(block)
                elif isinstance(block, ContentBlock):
                    serialized_content.append(block.to_dict())
                elif hasattr(block, "type"):
                    # Anthropic SDK content block object — convert to dict
                    serialized_content.append(_sdk_block_to_dict(block))
                else:
                    serialized_content.append(block)
            result.append({"role": role, "content": serialized_content})
        else:
            result.append({"role": role, "content": str(content) if content else ""})
    return result


def _sdk_block_to_dict(block) -> dict:
    """Convert an Anthropic SDK content block object to a dict."""
    if hasattr(block, "model_dump"):
        return block.model_dump()
    d = {"type": getattr(block, "type", "text")}
    if d["type"] == "text":
        d["text"] = getattr(block, "text", "")
    elif d["type"] == "tool_use":
        d["id"] = getattr(block, "id", "")
        d["name"] = getattr(block, "name", "")
        d["input"] = getattr(block, "input", {})
    elif d["type"] == "tool_result":
        d["tool_use_id"] = getattr(block, "tool_use_id", "")
        d["content"] = getattr(block, "content", "")
    return d


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "anthropic-version": "2023-06-01",
    }


async def call_minimax(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int = 16384,
) -> MiniMaxResponse:
    """Non-streaming call to MiniMax. Returns parsed response."""
    body = _build_request(messages, system, tools, max_tokens, stream=False)

    log.info(
        "MiniMax API call: %d messages, %d tools, system=%d chars",
        len(messages), len(tools or []), len(system),
    )

    client = _get_client()
    resp = await client.post(BASE_URL, json=body, headers=_headers())

    if resp.status_code != 200:
        log.error("MiniMax API error %d: %s", resp.status_code, resp.text[:500])
        raise RuntimeError(f"MiniMax API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()

    content_blocks = [_parse_content_block(b) for b in data.get("content", [])]
    stop_reason = data.get("stop_reason", "end_turn")
    usage = data.get("usage", {})

    log.info(
        "MiniMax response: stop_reason=%s, blocks=%s, usage=%s",
        stop_reason,
        [b.type for b in content_blocks],
        usage,
    )

    return MiniMaxResponse(content=content_blocks, stop_reason=stop_reason, usage=usage)


async def stream_minimax(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int = 16384,
) -> AsyncIterator[dict]:
    """Streaming call to MiniMax. Yields SSE events as parsed dicts.

    Yields events like:
      {"type": "content_block_start", "content_block": {...}}
      {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "..."}}
      {"type": "content_block_stop"}
      {"type": "message_delta", "delta": {"stop_reason": "..."}}
      {"type": "message_stop"}
    """
    body = _build_request(messages, system, tools, max_tokens, stream=True)

    log.info(
        "MiniMax stream: %d messages, %d tools, system=%d chars",
        len(messages), len(tools or []), len(system),
    )

    client = _get_client()
    async with client.stream("POST", BASE_URL, json=body, headers=_headers()) as resp:
        if resp.status_code != 200:
            error_text = await resp.aread()
            log.error("MiniMax stream error %d: %s", resp.status_code, error_text[:500])
            raise RuntimeError(f"MiniMax API error {resp.status_code}: {error_text[:200]}")

        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                event = json.loads(payload)
                yield event
            except json.JSONDecodeError:
                log.warning("Failed to parse SSE event: %s", payload[:200])


async def stream_and_collect(
    messages: list[dict],
    system: str = "",
    tools: list[dict] | None = None,
    max_tokens: int = 16384,
    on_text: Any = None,
) -> MiniMaxResponse:
    """Stream a response, calling on_text(str) for each text chunk.

    Returns the full collected response for processing tool_use blocks.
    """
    content_blocks: list[ContentBlock] = []
    current_block: dict | None = None
    current_index = -1
    stop_reason = "end_turn"
    usage = {}
    collected_text: list[str] = []
    tool_input_json: list[str] = []

    async for event in stream_minimax(messages, system, tools, max_tokens):
        event_type = event.get("type", "")

        if event_type == "content_block_start":
            current_block = event.get("content_block", {})
            current_index = event.get("index", current_index + 1)
            collected_text = []
            tool_input_json = []

        elif event_type == "content_block_delta":
            delta = event.get("delta", {})
            delta_type = delta.get("type", "")

            if delta_type == "text_delta" and delta.get("text"):
                text = delta["text"]
                collected_text.append(text)
                if on_text:
                    await on_text(text)

            elif delta_type == "input_json_delta" and delta.get("partial_json"):
                tool_input_json.append(delta["partial_json"])

        elif event_type == "content_block_stop":
            if current_block:
                block_type = current_block.get("type", "text")
                if block_type == "text":
                    current_block["text"] = current_block.get("text", "") + "".join(collected_text)
                elif block_type == "tool_use":
                    raw_json = "".join(tool_input_json)
                    if raw_json:
                        try:
                            current_block["input"] = json.loads(raw_json)
                        except json.JSONDecodeError:
                            log.warning("Failed to parse tool input JSON: %s", raw_json[:200])
                            current_block["input"] = {}
                content_blocks.append(_parse_content_block(current_block))
                current_block = None

        elif event_type == "message_delta":
            delta = event.get("delta", {})
            if "stop_reason" in delta:
                stop_reason = delta["stop_reason"]
            if "usage" in event:
                usage = event["usage"]

        elif event_type == "message_stop":
            if "usage" in event.get("message", {}):
                usage = event["message"]["usage"]

    log.info(
        "MiniMax stream done: stop_reason=%s, blocks=%s",
        stop_reason, [b.type for b in content_blocks],
    )

    return MiniMaxResponse(content=content_blocks, stop_reason=stop_reason, usage=usage)
