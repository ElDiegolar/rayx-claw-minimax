from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    minimax_api_key: str = ""
    minimax_model: str = "MiniMax-M2.7"
    workspace: str = "/home/ldfla/workspace/Test/rayx-claw-final"
    persona_name: str = "default"
    host: str = "127.0.0.1"
    port: int = 8080
    # Auth — set AUTH_TOKEN in .env to enable; empty = no auth (local dev only)
    auth_token: str = ""
    # Rate limiting
    max_rpm: int = 12  # requests per minute (conservative for token plan)
    max_concurrent: int = 3  # max parallel API calls
    max_subagents: int = 3  # max parallel sub-agents
    # Autonomous loop
    max_orchestrator_rounds: int = 100
    max_subagent_rounds: int = 15
    # Self-iteration
    max_iteration_cycles: int = 50
    iteration_token_budget: int = 5_000_000  # max tokens per iteration session
    iteration_snapshot_method: str = "git"  # git or copy

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
