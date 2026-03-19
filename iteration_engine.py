"""Self-iteration engine for autonomous project evolution.

Manages the lifecycle of iterative improvement on any project:
1. Discovery — spin up a team to analyze the project and determine metrics/strategy
2. Baseline — run the project, collect initial metrics
3. Iterate — research improvements, implement, test, compare, keep or revert
4. Track — token budget, metrics history, iteration count, snapshots
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import Settings

log = logging.getLogger(__name__)
settings = Settings()

ITERATION_STATE_FILE = Path(__file__).parent / "data" / "iteration_state.json"


@dataclass
class IterationMetrics:
    """Snapshot of metrics for one iteration cycle."""
    cycle: int
    timestamp: str
    metrics: dict[str, Any]
    changes_summary: str = ""
    kept: bool = True
    token_usage: int = 0


@dataclass
class IterationPlan:
    """Discovery output — what to measure and how to iterate."""
    project_type: str = ""  # e.g. "trading_bot", "web_app", "ml_pipeline"
    description: str = ""
    success_metrics: list[dict[str, str]] = field(default_factory=list)
    # e.g. [{"name": "sharpe_ratio", "direction": "maximize", "how": "run backtest_mtf.py"}]
    run_command: str = ""  # how to execute/test the project
    test_command: str = ""  # how to run tests if they exist
    iteration_strategies: list[str] = field(default_factory=list)
    # e.g. ["parameter_tuning", "strategy_research", "architecture_refactor"]
    constraints: list[str] = field(default_factory=list)
    # e.g. ["don't change API interface", "keep under 100ms latency"]


@dataclass
class IterationState:
    """Full state of an iteration session."""
    active: bool = False
    project_path: str = ""
    plan: IterationPlan = field(default_factory=IterationPlan)
    current_cycle: int = 0
    max_cycles: int = 50
    total_tokens_used: int = 0
    token_budget: int = 5_000_000
    metrics_history: list[dict] = field(default_factory=list)
    snapshot_branch: str = ""  # git branch for snapshots
    started_at: str = ""
    status: str = "idle"  # idle, discovering, baselining, iterating, paused, completed, failed
    last_error: str = ""
    phase: str = ""  # current sub-phase within iteration

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "project_path": self.project_path,
            "plan": {
                "project_type": self.plan.project_type,
                "description": self.plan.description,
                "success_metrics": self.plan.success_metrics,
                "run_command": self.plan.run_command,
                "test_command": self.plan.test_command,
                "iteration_strategies": self.plan.iteration_strategies,
                "constraints": self.plan.constraints,
            },
            "current_cycle": self.current_cycle,
            "max_cycles": self.max_cycles,
            "total_tokens_used": self.total_tokens_used,
            "token_budget": self.token_budget,
            "metrics_history": self.metrics_history,
            "snapshot_branch": self.snapshot_branch,
            "started_at": self.started_at,
            "status": self.status,
            "last_error": self.last_error,
            "phase": self.phase,
            "budget_remaining": self.token_budget - self.total_tokens_used,
            "budget_pct_used": round(
                (self.total_tokens_used / self.token_budget * 100) if self.token_budget else 0, 1
            ),
        }


class IterationEngine:
    """Manages self-iteration state and snapshots.

    This engine does NOT make API calls directly — it provides state management,
    snapshot handling, and prompt generation that the orchestrator uses to drive
    iteration through its existing sub-agent delegation pattern.
    """

    def __init__(self) -> None:
        self._state = self._load_state()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> IterationState:
        if not ITERATION_STATE_FILE.exists():
            return IterationState()
        try:
            data = json.loads(ITERATION_STATE_FILE.read_text(encoding="utf-8"))
            plan_data = data.pop("plan", {})
            # Remove computed fields that aren't in the dataclass
            data.pop("budget_remaining", None)
            data.pop("budget_pct_used", None)
            plan = IterationPlan(**plan_data) if plan_data else IterationPlan()
            return IterationState(plan=plan, **data)
        except Exception as e:
            log.warning("Failed to load iteration state: %s", e)
            return IterationState()

    def _save_state(self) -> None:
        ITERATION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            ITERATION_STATE_FILE.write_text(
                json.dumps(self._state.to_dict(), indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("Failed to save iteration state: %s", e)

    @property
    def state(self) -> IterationState:
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state.active

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, project_path: str, max_cycles: int | None = None, token_budget: int | None = None) -> str:
        """Initialize a new iteration session on a project folder."""
        workspace = Path(settings.workspace).resolve()
        p = Path(project_path)
        if not p.is_absolute():
            p = workspace / p
        resolved = p.resolve()

        # Validate path is within workspace
        try:
            resolved.relative_to(workspace)
        except ValueError:
            return f"Error: {project_path} is outside workspace"

        if not resolved.is_dir():
            return f"Error: {project_path} is not a directory"

        if self._state.active:
            return (
                f"Error: iteration already active on {self._state.project_path}. "
                "Stop it first with stop_iteration."
            )

        self._state = IterationState(
            active=True,
            project_path=str(resolved.relative_to(workspace)),
            current_cycle=0,
            max_cycles=max_cycles or settings.max_iteration_cycles,
            token_budget=token_budget or settings.iteration_token_budget,
            total_tokens_used=0,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="discovering",
            phase="project_analysis",
        )

        # Create a git snapshot branch if in a git repo
        self._create_snapshot_branch(resolved)
        self._save_state()

        return (
            f"Iteration session started on: {self._state.project_path}\n"
            f"Max cycles: {self._state.max_cycles}\n"
            f"Token budget: {self._state.token_budget:,}\n"
            f"Snapshot branch: {self._state.snapshot_branch or 'N/A'}\n"
            f"Status: discovering\n"
            f"Next step: The orchestrator should now spin up a discovery team to analyze "
            f"the project and determine success metrics, run commands, and iteration strategies."
        )

    def stop(self, reason: str = "user_requested") -> str:
        """Stop the active iteration session."""
        if not self._state.active:
            return "No active iteration session."

        project = self._state.project_path
        cycles = self._state.current_cycle
        tokens = self._state.total_tokens_used

        self._state.active = False
        self._state.status = "completed" if reason == "budget_exhausted" else reason
        self._save_state()

        return (
            f"Iteration stopped on: {project}\n"
            f"Cycles completed: {cycles}\n"
            f"Tokens used: {tokens:,}\n"
            f"Reason: {reason}\n"
            f"Metrics history preserved in iteration state."
        )

    def set_plan(self, plan_data: dict) -> str:
        """Set the iteration plan (output from discovery phase)."""
        if not self._state.active:
            return "Error: no active iteration session"

        self._state.plan = IterationPlan(
            project_type=plan_data.get("project_type", ""),
            description=plan_data.get("description", ""),
            success_metrics=plan_data.get("success_metrics", []),
            run_command=plan_data.get("run_command", ""),
            test_command=plan_data.get("test_command", ""),
            iteration_strategies=plan_data.get("iteration_strategies", []),
            constraints=plan_data.get("constraints", []),
        )
        self._state.status = "baselining"
        self._state.phase = "baseline_collection"
        self._save_state()

        return (
            f"Iteration plan set:\n"
            f"  Project type: {self._state.plan.project_type}\n"
            f"  Metrics: {[m['name'] for m in self._state.plan.success_metrics]}\n"
            f"  Strategies: {self._state.plan.iteration_strategies}\n"
            f"  Run command: {self._state.plan.run_command}\n"
            f"Next step: collect baseline metrics."
        )

    def record_metrics(self, metrics: dict, changes_summary: str = "", kept: bool = True) -> str:
        """Record metrics for the current cycle."""
        if not self._state.active:
            return "Error: no active iteration session"

        entry = {
            "cycle": self._state.current_cycle,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
            "changes_summary": changes_summary,
            "kept": kept,
        }
        self._state.metrics_history.append(entry)
        self._save_state()

        # Build comparison with previous cycle
        comparison = ""
        if len(self._state.metrics_history) >= 2:
            prev = self._state.metrics_history[-2]["metrics"]
            comparison = "\nComparison with previous cycle:"
            for key, val in metrics.items():
                if key in prev:
                    try:
                        diff = float(val) - float(prev[key])
                        direction = "+" if diff > 0 else ""
                        comparison += f"\n  {key}: {prev[key]} -> {val} ({direction}{diff:.4f})"
                    except (ValueError, TypeError):
                        comparison += f"\n  {key}: {prev[key]} -> {val}"

        return (
            f"Metrics recorded for cycle {self._state.current_cycle}:\n"
            f"  {json.dumps(metrics, indent=2)}\n"
            f"  Changes: {changes_summary}\n"
            f"  Kept: {kept}"
            f"{comparison}"
        )

    def advance_cycle(self) -> str:
        """Move to the next iteration cycle."""
        if not self._state.active:
            return "Error: no active iteration session"

        self._state.current_cycle += 1
        self._state.status = "iterating"
        self._state.phase = "research_and_implement"

        # Check budget
        if self._state.total_tokens_used >= self._state.token_budget:
            self.stop("budget_exhausted")
            return (
                f"TOKEN BUDGET EXHAUSTED ({self._state.total_tokens_used:,} / "
                f"{self._state.token_budget:,}). Iteration stopped automatically."
            )

        # Check max cycles
        if self._state.current_cycle >= self._state.max_cycles:
            self.stop("max_cycles_reached")
            return (
                f"MAX CYCLES REACHED ({self._state.current_cycle} / "
                f"{self._state.max_cycles}). Iteration stopped automatically."
            )

        self._save_state()

        budget_remaining = self._state.token_budget - self._state.total_tokens_used
        return (
            f"Advanced to cycle {self._state.current_cycle}\n"
            f"Tokens used: {self._state.total_tokens_used:,} / {self._state.token_budget:,} "
            f"({self.budget_pct_used}% used, {budget_remaining:,} remaining)\n"
            f"Status: iterating"
        )

    @property
    def budget_pct_used(self) -> float:
        if not self._state.token_budget:
            return 0
        return round(self._state.total_tokens_used / self._state.token_budget * 100, 1)

    def add_token_usage(self, tokens: int) -> None:
        """Track token consumption."""
        self._state.total_tokens_used += tokens
        self._save_state()

    def get_status(self) -> str:
        """Get formatted status of the iteration session."""
        if not self._state.active:
            if self._state.project_path:
                return (
                    f"Last iteration session (inactive):\n"
                    f"  Project: {self._state.project_path}\n"
                    f"  Cycles completed: {self._state.current_cycle}\n"
                    f"  Tokens used: {self._state.total_tokens_used:,}\n"
                    f"  Status: {self._state.status}\n"
                    f"  Metrics history: {len(self._state.metrics_history)} entries"
                )
            return "No iteration session (active or past)."

        budget_remaining = self._state.token_budget - self._state.total_tokens_used
        metrics_summary = ""
        if self._state.metrics_history:
            latest = self._state.metrics_history[-1]
            metrics_summary = f"\n  Latest metrics (cycle {latest['cycle']}): {json.dumps(latest['metrics'], indent=4)}"

        return (
            f"Active iteration session:\n"
            f"  Project: {self._state.project_path}\n"
            f"  Type: {self._state.plan.project_type or 'TBD (discovery pending)'}\n"
            f"  Cycle: {self._state.current_cycle} / {self._state.max_cycles}\n"
            f"  Status: {self._state.status} ({self._state.phase})\n"
            f"  Tokens: {self._state.total_tokens_used:,} / {self._state.token_budget:,} "
            f"({self.budget_pct_used}% used, {budget_remaining:,} remaining)\n"
            f"  Strategies: {self._state.plan.iteration_strategies}\n"
            f"  Metrics tracked: {[m['name'] for m in self._state.plan.success_metrics]}"
            f"{metrics_summary}\n"
            f"  History entries: {len(self._state.metrics_history)}"
        )

    # ------------------------------------------------------------------
    # Snapshots (git-based)
    # ------------------------------------------------------------------

    def _create_snapshot_branch(self, project_path: Path) -> None:
        """Create a git branch to track iteration snapshots."""
        workspace = Path(settings.workspace).resolve()
        try:
            # Check if we're in a git repo
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(workspace), capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                log.info("Not a git repo — skipping snapshot branch")
                return

            branch_name = f"iteration/{self._state.project_path.replace('/', '-')}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                cwd=str(workspace), capture_output=True, text=True, timeout=10,
            )
            self._state.snapshot_branch = branch_name
            log.info("Created iteration snapshot branch: %s", branch_name)
        except Exception as e:
            log.warning("Failed to create snapshot branch: %s", e)

    def create_checkpoint(self, label: str = "") -> str:
        """Create a git checkpoint for the current iteration cycle."""
        if not self._state.snapshot_branch:
            return "No snapshot branch — checkpoints disabled"

        workspace = Path(settings.workspace).resolve()
        try:
            msg = f"iteration-cycle-{self._state.current_cycle}"
            if label:
                msg += f": {label}"

            subprocess.run(
                ["git", "add", "-A"], cwd=str(workspace),
                capture_output=True, text=True, timeout=10,
            )
            result = subprocess.run(
                ["git", "commit", "-m", msg, "--allow-empty"],
                cwd=str(workspace), capture_output=True, text=True, timeout=10,
            )
            return f"Checkpoint created: {msg}"
        except Exception as e:
            return f"Checkpoint failed: {e}"

    def revert_to_checkpoint(self, cycle: int | None = None) -> str:
        """Revert project to a previous iteration checkpoint."""
        if not self._state.snapshot_branch:
            return "No snapshot branch — revert unavailable"

        workspace = Path(settings.workspace).resolve()
        target = f"iteration-cycle-{cycle}" if cycle is not None else "HEAD~1"

        try:
            if cycle is not None:
                # Find the commit with this cycle label
                result = subprocess.run(
                    ["git", "log", "--oneline", "--grep", target],
                    cwd=str(workspace), capture_output=True, text=True, timeout=10,
                )
                if not result.stdout.strip():
                    return f"No checkpoint found for cycle {cycle}"
                commit_hash = result.stdout.strip().split("\n")[0].split()[0]
                subprocess.run(
                    ["git", "reset", "--hard", commit_hash],
                    cwd=str(workspace), capture_output=True, text=True, timeout=10,
                )
            else:
                subprocess.run(
                    ["git", "reset", "--hard", "HEAD~1"],
                    cwd=str(workspace), capture_output=True, text=True, timeout=10,
                )
            return f"Reverted to {target}"
        except Exception as e:
            return f"Revert failed: {e}"

    # ------------------------------------------------------------------
    # Prompt generation for orchestrator
    # ------------------------------------------------------------------

    def get_discovery_prompt(self) -> str:
        """Generate the prompt for the discovery phase sub-agents."""
        return (
            f"SELF-ITERATION DISCOVERY PHASE\n"
            f"==============================\n\n"
            f"You are analyzing the project at: {self._state.project_path}\n\n"
            f"Your job is to thoroughly understand this project and produce an iteration plan.\n\n"
            f"STEPS:\n"
            f"1. Read all key files in the project directory\n"
            f"2. Understand what the project does, its architecture, and its goals\n"
            f"3. Identify measurable success metrics (quantitative where possible)\n"
            f"4. Find how to run/test the project (commands, scripts, entry points)\n"
            f"5. Propose iteration strategies appropriate for this project type\n"
            f"6. Note any constraints (don't break APIs, maintain compatibility, etc.)\n\n"
            f"OUTPUT FORMAT — you MUST save your findings using save_memory with key 'iteration_plan' "
            f"as a JSON string with this structure:\n"
            f'{{\n'
            f'  "project_type": "string (e.g. trading_bot, web_app, ml_model, cli_tool, api_server)",\n'
            f'  "description": "one paragraph description of what the project does",\n'
            f'  "success_metrics": [\n'
            f'    {{"name": "metric_name", "direction": "maximize|minimize", "how": "how to measure it"}}\n'
            f'  ],\n'
            f'  "run_command": "shell command to run/test the project",\n'
            f'  "test_command": "shell command to run tests (empty if none)",\n'
            f'  "iteration_strategies": ["strategy1", "strategy2", ...],\n'
            f'  "constraints": ["constraint1", "constraint2", ...]\n'
            f'}}\n\n'
            f"STRATEGY EXAMPLES by project type:\n"
            f"- Trading bot: parameter_tuning, indicator_research, risk_management, backtest_optimization\n"
            f"- Web app: performance_optimization, ux_improvement, error_handling, test_coverage\n"
            f"- ML model: hyperparameter_tuning, feature_engineering, architecture_search, data_augmentation\n"
            f"- API server: latency_optimization, throughput_improvement, error_rate_reduction\n"
            f"- CLI tool: speed_optimization, usability_improvement, edge_case_handling\n\n"
            f"Be thorough but practical. Focus on metrics that can actually be measured by running code."
        )

    def get_baseline_prompt(self) -> str:
        """Generate the prompt for baseline metrics collection."""
        plan = self._state.plan
        metrics_list = "\n".join(
            f"  - {m['name']} ({m['direction']}): {m['how']}"
            for m in plan.success_metrics
        )
        return (
            f"SELF-ITERATION BASELINE PHASE\n"
            f"=============================\n\n"
            f"Project: {self._state.project_path}\n"
            f"Type: {plan.project_type}\n\n"
            f"Collect baseline metrics by running the project as-is.\n\n"
            f"Run command: {plan.run_command}\n"
            f"Test command: {plan.test_command}\n\n"
            f"Metrics to collect:\n{metrics_list}\n\n"
            f"STEPS:\n"
            f"1. Run the project using the run command\n"
            f"2. Parse the output to extract each metric\n"
            f"3. If a metric can't be extracted automatically, estimate it or note it as N/A\n"
            f"4. Save results using save_memory with key 'iteration_baseline' as JSON:\n"
            f'   {{"metric_name": value, ...}}\n\n'
            f"5. Also save any observations about the current state in 'iteration_observations'\n\n"
            f"Be precise with numbers. These are the baseline we'll improve against."
        )

    def get_iteration_prompt(self, cycle: int) -> str:
        """Generate the prompt for an iteration cycle."""
        plan = self._state.plan
        budget_remaining = self._state.token_budget - self._state.total_tokens_used
        budget_pct = self.budget_pct_used

        # Build metrics history summary
        history_summary = "No previous iterations."
        if self._state.metrics_history:
            recent = self._state.metrics_history[-5:]  # Last 5 cycles
            history_lines = []
            for entry in recent:
                status = "KEPT" if entry.get("kept", True) else "REVERTED"
                history_lines.append(
                    f"  Cycle {entry['cycle']} [{status}]: {json.dumps(entry['metrics'])} "
                    f"| {entry.get('changes_summary', 'N/A')}"
                )
            history_summary = "\n".join(history_lines)

        # Determine best metrics so far
        best_summary = ""
        if self._state.metrics_history:
            for metric_def in plan.success_metrics:
                name = metric_def["name"]
                direction = metric_def.get("direction", "maximize")
                values = [
                    (e["cycle"], e["metrics"].get(name))
                    for e in self._state.metrics_history
                    if name in e.get("metrics", {}) and e["metrics"][name] is not None
                ]
                if values:
                    if direction == "maximize":
                        best_cycle, best_val = max(values, key=lambda x: float(x[1]) if x[1] is not None else float('-inf'))
                    else:
                        best_cycle, best_val = min(values, key=lambda x: float(x[1]) if x[1] is not None else float('inf'))
                    best_summary += f"\n  {name}: best={best_val} (cycle {best_cycle})"

        strategies = ", ".join(plan.iteration_strategies)
        constraints = "\n".join(f"  - {c}" for c in plan.constraints) if plan.constraints else "  None"

        return (
            f"SELF-ITERATION CYCLE {cycle}\n"
            f"{'=' * 40}\n\n"
            f"Project: {self._state.project_path}\n"
            f"Type: {plan.project_type}\n"
            f"Token budget: {budget_remaining:,} remaining ({budget_pct}% used)\n\n"
            f"RECENT HISTORY:\n{history_summary}\n\n"
            f"BEST METRICS SO FAR:{best_summary or ' (baseline only)'}\n\n"
            f"AVAILABLE STRATEGIES: {strategies}\n\n"
            f"CONSTRAINTS:\n{constraints}\n\n"
            f"YOUR TASK FOR THIS CYCLE:\n"
            f"1. RESEARCH: Based on the metrics history and available strategies, decide what "
            f"improvement to attempt this cycle. Be specific about what you'll change and why.\n"
            f"2. IMPLEMENT: Make the code changes in the project directory\n"
            f"3. TEST: Run the project using: {plan.run_command}\n"
            f"4. MEASURE: Extract the same metrics as baseline\n"
            f"5. COMPARE: Determine if the changes improved the target metrics\n"
            f"6. DECIDE: Keep the changes (if improved) or revert them\n\n"
            f"Save your results using save_memory with key 'iteration_cycle_{cycle}_result' as JSON:\n"
            f'{{\n'
            f'  "metrics": {{"metric_name": value, ...}},\n'
            f'  "changes_summary": "what you changed and why",\n'
            f'  "kept": true/false,\n'
            f'  "reasoning": "why you kept or reverted"\n'
            f'}}\n\n'
            f"TOKEN EFFICIENCY: You have {budget_remaining:,} tokens remaining. "
            f"Be efficient — focus on high-impact changes. If budget is low (<20%), "
            f"focus on the single most promising improvement.\n\n"
            f"IMPORTANT: If you revert changes, use git to restore the previous state. "
            f"If you keep changes, they persist for the next cycle."
        )

    def get_review_prompt(self) -> str:
        """Generate the prompt for the review/summary phase at end of session."""
        history = self._state.metrics_history
        if not history:
            return "No iteration history to review."

        return (
            f"ITERATION SESSION REVIEW\n"
            f"========================\n\n"
            f"Project: {self._state.project_path}\n"
            f"Cycles completed: {self._state.current_cycle}\n"
            f"Tokens used: {self._state.total_tokens_used:,}\n\n"
            f"Full metrics history:\n{json.dumps(history, indent=2)}\n\n"
            f"Write a summary report that includes:\n"
            f"1. Overall improvement from baseline to final state\n"
            f"2. What worked (kept changes)\n"
            f"3. What didn't work (reverted changes)\n"
            f"4. Recommendations for future iterations\n"
            f"5. Any risks or concerns with the current state\n\n"
            f"Save the report using save_memory with key 'iteration_report'."
        )


# Singleton for the application
iteration_engine = IterationEngine()
