from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from config import Settings
from models import Agent, UserMessage, WSMessage
from orchestrator import AutonomousOrchestrator
from persona_api import router as persona_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger(__name__)

settings = Settings()
app = FastAPI(title="Rayx-Claw-Final — Autonomous Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"


def _check_token(token: str | None) -> None:
    """Validate auth token if AUTH_TOKEN is configured."""
    if not settings.auth_token:
        return  # No auth configured — allow (local dev)
    if token != settings.auth_token:
        raise HTTPException(status_code=401, detail="Unauthorized")


async def require_auth(request: Request):
    """Dependency that checks Bearer token or query param."""
    if not settings.auth_token:
        return
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    _check_token(token)


app.include_router(persona_router, dependencies=[Depends(require_auth)])


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/persona", dependencies=[Depends(require_auth)])
async def get_persona():
    from persona_api import get_active_persona
    p = get_active_persona()
    return {"name": p.name}


@app.post("/api/persona", dependencies=[Depends(require_auth)])
async def set_persona(body: dict):
    from persona_api import switch_active_persona
    name = body.get("name", "").strip()
    if name:
        switch_active_persona(name)
        settings.persona_name = name
    return {"name": name}


@app.get("/api/status", dependencies=[Depends(require_auth)])
async def tool_status():
    import httpx as _httpx

    statuses = {
        "filesystem": {"ok": True, "label": "Filesystem", "desc": "Read, write, search files in workspace"},
        "memory": {"ok": True, "label": "Memory", "desc": "Persistent key-value memory across sessions"},
    }

    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.minimax.io/anthropic")
            statuses["minimax"] = {
                "ok": r.status_code < 500,
                "label": "MiniMax",
                "desc": "MiniMax M2.7 — autonomous orchestrator + sub-agents",
            }
    except Exception:
        statuses["minimax"] = {
            "ok": False,
            "label": "MiniMax",
            "desc": "MiniMax M2.7 — autonomous orchestrator + sub-agents",
        }

    return statuses


@app.get("/api/iteration", dependencies=[Depends(require_auth)])
async def iteration_status():
    from iteration_engine import iteration_engine
    return iteration_engine.state.to_dict()


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
    import asyncio as _aio

    # Auth check for WebSocket — uses query param since WS can't set headers easily
    if settings.auth_token and token != settings.auth_token:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws.accept()
    orchestrator = AutonomousOrchestrator()
    log.info("Client connected")

    async def send_ws(msg: WSMessage):
        await ws.send_text(msg.model_dump_json())

    orchestrator.set_send(send_ws)

    # Send chat history to client for replay
    ui_history = orchestrator.get_ui_history()
    if ui_history:
        log.info("Sending %d history exchanges to client", len(ui_history))
        await ws.send_text(WSMessage(
            type="history",
            agent=Agent.SYSTEM,
            history=ui_history,
        ).model_dump_json())

    # Send current task state
    await ws.send_text(WSMessage(
        type="task_state",
        agent=Agent.SYSTEM,
        task_state=orchestrator.task_state.to_dict(),
    ).model_dump_json())

    try:
        while True:
            raw = await ws.receive_text()
            user_msg = UserMessage(**json.loads(raw))

            if user_msg.type == "cancel":
                log.info("Cancel requested")
                orchestrator.cancel()
                continue

            if user_msg.type == "pause":
                log.info("Pause requested")
                orchestrator.pause()
                await ws.send_text(WSMessage(
                    type="task_state", agent=Agent.SYSTEM,
                    task_state=orchestrator.task_state.to_dict(),
                ).model_dump_json())
                await ws.send_text(WSMessage(
                    type="status", agent=Agent.SYSTEM, content="Paused.",
                ).model_dump_json())
                continue

            if user_msg.type == "resume":
                log.info("Resume requested")
                orchestrator.resume()
                await ws.send_text(WSMessage(
                    type="task_state", agent=Agent.SYSTEM,
                    task_state=orchestrator.task_state.to_dict(),
                ).model_dump_json())
                await ws.send_text(WSMessage(
                    type="status", agent=Agent.SYSTEM, content="Resumed.",
                ).model_dump_json())
                continue

            if user_msg.type == "confirm":
                log.info("User confirmed continuation")
                orchestrator.confirm()
                continue

            if user_msg.type == "switch_persona" and user_msg.content.strip():
                persona_id = user_msg.content.strip()
                log.info("Persona switch requested: %s", persona_id)
                from persona_api import switch_active_persona
                try:
                    switch_active_persona(persona_id)
                    settings.persona_name = persona_id
                    persona_info = orchestrator.switch_persona(persona_id)
                    await ws.send_text(WSMessage(
                        type="persona_update", agent=Agent.SYSTEM,
                        content=json.dumps(persona_info),
                    ).model_dump_json())
                except Exception as e:
                    log.error("Persona switch failed: %s", e)
                    await ws.send_text(WSMessage(
                        type="error", agent=Agent.SYSTEM,
                        content=f"Failed to switch persona: {e}",
                    ).model_dump_json())
                continue

            if user_msg.type == "message" and user_msg.content.strip():
                log.info("Message received: %s", user_msg.content[:80])
                is_busy = orchestrator._loop_task is not None and not orchestrator._loop_task.done()
                await orchestrator.accept_user_message(user_msg.content)
                if is_busy:
                    await ws.send_text(WSMessage(
                        type="status", agent=Agent.SYSTEM,
                        content="Message received — will be processed after current step.",
                    ).model_dump_json())

    except WebSocketDisconnect:
        log.info("Client disconnected")
        orchestrator.cancel()


# Static files mounted last so /ws takes priority
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
