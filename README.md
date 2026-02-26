# Rayx-Claw MiniMax — Autonomous Agent Platform

An autonomous AI agent platform powered by **MiniMax M2.5**. Unlike traditional chatbots, this agent plans, delegates, executes, and completes tasks independently — you send a goal and it works autonomously while you observe or guide it in real time.

## How It Works

1. You send a task (e.g., "Create a Python REST API with tests")
2. The orchestrator plans an approach and begins executing
3. It can spawn parallel sub-agents for complex work
4. After each round, it pauses and asks if you want it to continue
5. You can send messages, pause, resume, or cancel at any time

The agent has access to filesystem tools (read, write, search, grep), shell commands, persistent memory, and the ability to delegate subtasks to parallel sub-agents.

## Quick Start

### 1. Clone the repo

```bash
git clone git@github.com:ElDiegolar/rayx-claw-minimax.git
cd rayx-claw-minimax
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and add your MiniMax API key:

```
MINIMAX_API_KEY=your-minimax-api-key-here
```

### 4. Start the server

```bash
./start.sh
```

Or specify a port:

```bash
./start.sh 9000
```

The start script auto-detects free ports — if 8080 is in use it tries 8081, 8082, etc.

### 5. Open the UI

Navigate to `http://localhost:8080` in your browser.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `MINIMAX_API_KEY` | (required) | Your MiniMax API key |
| `MINIMAX_MODEL` | `MiniMax-M2.5` | Model to use |
| `WORKSPACE` | `/home/ldfla/workspace` | Root directory the agent can access |
| `PERSONA_NAME` | `default` | Starting persona |
| `HOST` | `127.0.0.1` | Server bind address |
| `PORT` | `8080` | Server port |
| `MAX_RPM` | `18` | Max API requests per minute |
| `MAX_CONCURRENT` | `5` | Max parallel API calls |
| `MAX_SUBAGENTS` | `5` | Max parallel sub-agents |
| `MAX_ORCHESTRATOR_ROUNDS` | `50` | Max autonomous rounds per task |
| `MAX_SUBAGENT_ROUNDS` | `15` | Max rounds per sub-agent |

## UI Controls

- **Send a message** — type in the input bar and press Enter or click Send
- **Continue** — green button that appears when the agent pauses between rounds; click to let it keep working
- **Pause / Resume** — temporarily halt or continue the autonomous loop
- **Cancel** — stop the current task entirely
- **Settings gear** — open the persona manager to switch or edit personas

You can send messages at any time, even while the agent is working. Your message gets injected into the next round.

## Personas

Personas change the agent's personality, tone, and behavior. Switch between them live from the settings panel — no reconnect needed.

| Persona | Style | Color |
|---|---|---|
| **Sophia** (default) | Professional, concise, plans before executing | Cyan |
| **Rex** | Startup energy, ship-it mentality, bold and fast | Orange |
| **Atlas** | Technical architect, direct, code-first | Cyan |
| **Luna** | Creative mentor, warm, uses metaphors | Purple |
| **John** | Custom persona | — |

### Creating a custom persona

Add a YAML file to `personas/`:

```yaml
name: MyAgent
tagline: Short description
greeting: Hello! What should I work on?

personality:
  tone: friendly and focused
  style: concise with examples
  traits:
    - Plans before acting
    - Explains reasoning

role:
  description: A helpful autonomous agent
  instructions:
    - Break tasks into steps
    - Verify output before reporting

guardrails:
  do:
    - Test code after writing
    - Save important context to memory
  dont:
    - Expose secrets
    - Skip verification

ui:
  avatar_emoji: "\U0001F916"
  primary_color: "#00f0ff"
```

## Available Tools

The orchestrator has access to these tools:

| Tool | Description |
|---|---|
| `read_file` | Read file contents |
| `write_file` | Create or overwrite files |
| `list_directory` | List files and directories |
| `search_files` | Find files by name pattern |
| `grep_files` | Search file contents with regex |
| `run_command` | Execute shell commands |
| `save_memory` | Persist key-value data across sessions |
| `recall_memory` | Retrieve saved memory |
| `delete_memory` | Remove a memory key |
| `delegate_to_subagent` | Spawn a sub-agent for a subtask |
| `report_progress` | Send a progress update to the UI |
| `mark_complete` | Signal that the task is finished |

Sub-agents get a restricted set: filesystem tools, `run_command`, and `recall_memory` (read-only).

## Architecture

```
Browser <── WebSocket ──> FastAPI Server <──> AutonomousOrchestrator
                                                  |
                                                  +── user_queue (async message injection)
                                                  +── autonomous loop (plan → execute → pause → confirm)
                                                  +── SubAgentManager
                                                       +── SubAgent #1 (MiniMax)
                                                       +── SubAgent #2 (MiniMax)
                                                       +── ...
```

- **Dual-queue design**: User messages queue asynchronously while the orchestrator runs its loop
- **Confirmation flow**: The agent pauses between rounds and waits for user confirmation before continuing — no runaway loops
- **Rate limiting**: Token-bucket limiter (18 RPM default) with semaphore for concurrent call control
- **Parallel sub-agents**: Complex tasks get split across multiple sub-agents running simultaneously

## Project Structure

```
├── server.py              # FastAPI + WebSocket server
├── orchestrator.py        # Autonomous orchestrator (core loop)
├── subagent_manager.py    # Sub-agent spawning and management
├── agents.py              # MiniMax API client
├── tools.py               # Tool definitions and implementations
├── config.py              # Settings (from .env)
├── models.py              # Pydantic models
├── storage.py             # History, memory, and task state persistence
├── rate_limiter.py        # Token-bucket rate limiter
├── persona.py             # Persona loader
├── persona_api.py         # Persona CRUD API
├── start.sh               # Auto-port startup script
├── static/
│   ├── index.html         # Web UI
│   └── persona-manager.css
├── personas/              # YAML persona definitions
│   ├── default.yaml       # Sophia
│   ├── rex.yaml
│   ├── atlas.yaml
│   ├── luna.yaml
│   └── john.yaml
└── data/                  # Runtime data (auto-created)
    ├── history.json
    ├── memory.json
    └── task_state.json
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `GET` | `/api/status` | Tool and API health check |
| `GET` | `/api/persona` | Get current persona name |
| `POST` | `/api/persona` | Switch persona (`{"name": "rex"}`) |
| `GET` | `/api/personas` | List all personas |
| `GET` | `/api/personas/{id}` | Get persona details |
| `POST` | `/api/personas/{id}` | Create/update persona |
| `DELETE` | `/api/personas/{id}` | Delete persona |
| `WS` | `/ws` | WebSocket for real-time agent communication |

## License

MIT
