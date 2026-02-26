from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    minimax_api_key: str = ""
    minimax_model: str = "MiniMax-M2.5"
    workspace: str = "/home/ldfla/workspace"
    persona_name: str = "default"
    host: str = "127.0.0.1"
    port: int = 8080
    # Rate limiting
    max_rpm: int = 18  # requests per minute (buffer under 20 RPM)
    max_concurrent: int = 5  # max parallel API calls
    max_subagents: int = 5  # max parallel sub-agents
    # Autonomous loop
    max_orchestrator_rounds: int = 50
    max_subagent_rounds: int = 15

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
