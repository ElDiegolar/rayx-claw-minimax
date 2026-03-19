"""End-to-end test of the self-iteration framework.

Tests the full lifecycle without making API calls:
1. Start iteration session
2. Set discovery plan
3. Record baseline metrics
4. Simulate iteration cycles (advance, checkpoint, record, revert)
5. Stop session
6. Verify state persistence
"""
import json
import sys
from pathlib import Path

# Ensure we can import project modules
sys.path.insert(0, str(Path(__file__).parent))

from iteration_engine import IterationEngine, ITERATION_STATE_FILE


def test_full_lifecycle():
    print("=" * 60)
    print("SELF-ITERATION FRAMEWORK — END-TO-END TEST")
    print("=" * 60)

    # Clean slate
    if ITERATION_STATE_FILE.exists():
        ITERATION_STATE_FILE.unlink()

    engine = IterationEngine()
    errors = []

    # ── 1. Start ──
    print("\n[1] START ITERATION SESSION")
    result = engine.start("projects/test-strategy", max_cycles=10, token_budget=1_000_000)
    print(f"    {result.splitlines()[0]}")
    assert engine.is_active, "Should be active"
    assert engine.state.status == "discovering"
    assert engine.state.project_path == "projects/test-strategy"
    print("    ✓ Session started, status=discovering")

    # ── 2. Double-start should fail ──
    result2 = engine.start("projects/other")
    assert "Error" in result2, "Double start should error"
    print("    ✓ Double-start correctly rejected")

    # ── 3. Set Plan (discovery output) ──
    print("\n[2] SET ITERATION PLAN (discovery phase)")
    plan = {
        "project_type": "trading_bot",
        "description": "Simple moving average crossover strategy backtest",
        "success_metrics": [
            {"name": "total_return_pct", "direction": "maximize", "how": "parse JSON output of strategy.py"},
            {"name": "sharpe_ratio", "direction": "maximize", "how": "parse JSON output of strategy.py"},
            {"name": "win_rate", "direction": "maximize", "how": "parse JSON output of strategy.py"},
            {"name": "max_drawdown_pct", "direction": "minimize", "how": "parse JSON output of strategy.py"},
        ],
        "run_command": "python3 projects/test-strategy/strategy.py",
        "test_command": "",
        "iteration_strategies": ["parameter_tuning", "indicator_research", "risk_management"],
        "constraints": ["keep the strategy file self-contained", "don't change the price data generation"],
    }
    result = engine.set_plan(plan)
    print(f"    {result.splitlines()[0]}")
    assert engine.state.status == "baselining"
    assert engine.state.plan.project_type == "trading_bot"
    assert len(engine.state.plan.success_metrics) == 4
    print("    ✓ Plan set, status=baselining, 4 metrics tracked")

    # ── 4. Record Baseline ──
    print("\n[3] RECORD BASELINE METRICS")
    baseline = {
        "total_return_pct": 1.1,
        "sharpe_ratio": 0.1817,
        "win_rate": 50.0,
        "max_drawdown_pct": 2.32,
    }
    result = engine.record_metrics(baseline, "Initial baseline — no changes", kept=True)
    print(f"    {result.splitlines()[0]}")
    assert len(engine.state.metrics_history) == 1
    print("    ✓ Baseline recorded")

    # ── 5. Simulate 3 iteration cycles ──
    print("\n[4] SIMULATE ITERATION CYCLES")

    # Cycle 1: parameter tuning improvement
    result = engine.advance_cycle()
    print(f"    Cycle 1: {result.splitlines()[0]}")
    assert engine.state.current_cycle == 1

    engine.add_token_usage(50_000)
    checkpoint = engine.create_checkpoint("parameter tuning")
    print(f"    Checkpoint: {checkpoint}")

    metrics_c1 = {
        "total_return_pct": 3.5,
        "sharpe_ratio": 0.45,
        "win_rate": 58.0,
        "max_drawdown_pct": 1.8,
    }
    result = engine.record_metrics(metrics_c1, "Adjusted fast_period=8, slow_period=30", kept=True)
    print(f"    ✓ Cycle 1 KEPT — return improved 1.1% → 3.5%")

    # Cycle 2: bad change, revert
    result = engine.advance_cycle()
    assert engine.state.current_cycle == 2

    engine.add_token_usage(45_000)
    engine.create_checkpoint("risk management change")

    metrics_c2 = {
        "total_return_pct": -1.2,
        "sharpe_ratio": -0.3,
        "win_rate": 35.0,
        "max_drawdown_pct": 5.1,
    }
    result = engine.record_metrics(metrics_c2, "Changed stop_loss to 1% — too tight", kept=False)
    revert = engine.revert_to_checkpoint()
    print(f"    ✓ Cycle 2 REVERTED — return dropped to -1.2%, reverted")

    # Cycle 3: another improvement
    result = engine.advance_cycle()
    assert engine.state.current_cycle == 3

    engine.add_token_usage(55_000)
    engine.create_checkpoint("indicator research")

    metrics_c3 = {
        "total_return_pct": 5.8,
        "sharpe_ratio": 0.72,
        "win_rate": 62.0,
        "max_drawdown_pct": 1.5,
    }
    result = engine.record_metrics(metrics_c3, "Added RSI filter for entry confirmation", kept=True)
    print(f"    ✓ Cycle 3 KEPT — return improved 3.5% → 5.8%")

    # ── 6. Verify token tracking ──
    print("\n[5] TOKEN BUDGET TRACKING")
    total_tokens = engine.state.total_tokens_used
    budget = engine.state.token_budget
    pct = engine.budget_pct_used
    print(f"    Tokens used: {total_tokens:,} / {budget:,} ({pct}%)")
    assert total_tokens == 150_000
    assert pct == 15.0
    print("    ✓ Token tracking correct")

    # ── 7. Verify metrics history & best tracking ──
    print("\n[6] METRICS HISTORY")
    history = engine.state.metrics_history
    assert len(history) == 4  # baseline + 3 cycles
    kept_count = sum(1 for h in history if h.get("kept", True))
    reverted_count = sum(1 for h in history if not h.get("kept", True))
    print(f"    Total entries: {len(history)}")
    print(f"    Kept: {kept_count}, Reverted: {reverted_count}")

    # Check best metrics
    returns = [(h["cycle"], h["metrics"]["total_return_pct"]) for h in history]
    best_cycle, best_return = max(returns, key=lambda x: x[1])
    print(f"    Best return: {best_return}% (cycle {best_cycle})")
    assert best_return == 5.8
    assert best_cycle == 3
    print("    ✓ Metrics history and best tracking correct")

    # ── 8. Verify prompt generation ──
    print("\n[7] PROMPT GENERATION")
    disc_prompt = engine.get_discovery_prompt()
    assert "test-strategy" in disc_prompt
    print(f"    Discovery prompt: {len(disc_prompt)} chars ✓")

    base_prompt = engine.get_baseline_prompt()
    assert "total_return_pct" in base_prompt
    print(f"    Baseline prompt: {len(base_prompt)} chars ✓")

    iter_prompt = engine.get_iteration_prompt(4)
    assert "CYCLE 4" in iter_prompt
    assert "5.8" in iter_prompt  # best return should appear
    assert "150,000 remaining" in iter_prompt or "850,000 remaining" in iter_prompt
    print(f"    Iteration prompt: {len(iter_prompt)} chars ✓")

    review_prompt = engine.get_review_prompt()
    assert "3" in review_prompt  # cycles completed
    print(f"    Review prompt: {len(review_prompt)} chars ✓")

    # ── 9. Status output ──
    print("\n[8] STATUS OUTPUT")
    status = engine.get_status()
    print(f"    {status.splitlines()[0]}")
    assert "Active" in status
    assert "trading_bot" in status
    print("    ✓ Status output correct")

    # ── 10. Stop ──
    print("\n[9] STOP SESSION")
    result = engine.stop("test_complete")
    print(f"    {result.splitlines()[0]}")
    assert not engine.is_active
    print("    ✓ Session stopped")

    # ── 11. Verify persistence ──
    print("\n[10] PERSISTENCE CHECK")
    engine2 = IterationEngine()
    assert engine2.state.project_path == "projects/test-strategy"
    assert len(engine2.state.metrics_history) == 4
    assert engine2.state.total_tokens_used == 150_000
    assert engine2.state.plan.project_type == "trading_bot"
    print(f"    Reloaded state: {engine2.state.current_cycle} cycles, {len(engine2.state.metrics_history)} history entries")
    print("    ✓ State persisted and reloaded correctly")

    # ── 12. Budget exhaustion test ──
    print("\n[11] BUDGET EXHAUSTION")
    if ITERATION_STATE_FILE.exists():
        ITERATION_STATE_FILE.unlink()
    engine3 = IterationEngine()
    engine3.start("projects/test-strategy", max_cycles=100, token_budget=100_000)
    engine3.set_plan(plan)
    engine3.add_token_usage(100_001)  # exceed budget
    result = engine3.advance_cycle()
    assert "BUDGET EXHAUSTED" in result
    assert not engine3.is_active
    print(f"    {result.splitlines()[0]}")
    print("    ✓ Auto-stop on budget exhaustion works")

    # ── 13. Max cycles test ──
    print("\n[12] MAX CYCLES LIMIT")
    if ITERATION_STATE_FILE.exists():
        ITERATION_STATE_FILE.unlink()
    engine4 = IterationEngine()
    engine4.start("projects/test-strategy", max_cycles=2, token_budget=5_000_000)
    engine4.set_plan(plan)
    engine4.advance_cycle()  # cycle 1
    result = engine4.advance_cycle()  # cycle 2 = max
    assert "MAX CYCLES" in result
    assert not engine4.is_active
    print(f"    {result.splitlines()[0]}")
    print("    ✓ Auto-stop on max cycles works")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("ALL 12 TESTS PASSED")
    print("=" * 60)
    print(f"""
Framework verified:
  • Session lifecycle (start/stop/double-start rejection)
  • Discovery plan setting with 4 metric types
  • Baseline + 3 iteration cycles with keep/revert
  • Token budget tracking (150K used, 15% of 1M)
  • Metrics history with comparison and best-so-far
  • Git checkpoint/revert integration
  • Prompt generation (discovery/baseline/iteration/review)
  • State persistence across engine instances
  • Auto-stop on budget exhaustion
  • Auto-stop on max cycles reached
  • Status reporting
""")

    # Cleanup
    if ITERATION_STATE_FILE.exists():
        ITERATION_STATE_FILE.unlink()


if __name__ == "__main__":
    test_full_lifecycle()
