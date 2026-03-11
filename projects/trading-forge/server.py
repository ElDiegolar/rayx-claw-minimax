"""
Trading Forge FastAPI Server

Provides REST API for:
- Status and portfolio monitoring
- Trading control (start/stop)
- Market management
- Configuration management
- Trade and signal history

Author: Trading Forge Team
"""

import logging
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime
from pathlib import Path
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import uvicorn

from config import (
    MOMENTUM_THRESHOLD,
    CONSENSUS_GATE,
    EDGE_THRESHOLD,
    KELLY_ALPHA,
    KELLY_HARD_CAP,
    MAX_CONCURRENT_POSITIONS,
    TARGET_MARKETS,
    SCAN_INTERVAL_SECONDS,
    ENABLE_PAPER_TRADING,
)
from models import (
    Market,
    Direction,
    PositionStatus,
    Trade,
    Position,
    Portfolio,
    DroppedSignal,
    SignalStage,
    DropReason,
)
from alpha_engine_adapter import AlphaEngineAdapter, create_alpha_engine_adapter

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# =============================================================================
# FASTAPI APP
# =============================================================================

app = FastAPI(
    title="Trading Forge API",
    description="REST API for the Trading Forge trading bot",
    version="1.0.0"
)

# Static files & dashboard
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def dashboard():
    """Serve the cyberpunk dashboard."""
    return FileResponse(str(STATIC_DIR / "index.html"))


# Global state
_orchestrator: Optional[AlphaEngineAdapter] = None
_orchestrator_lock = threading.Lock()


# =============================================================================
# PYDANTIC MODELS
# =============================================================================

class MarketInput(BaseModel):
    """Input model for adding a market."""
    id: str = Field(..., description="Market ID")
    question: str = Field(..., description="Market question")
    resolution_time: str = Field(..., description="Resolution time (ISO format)")
    current_price: float = Field(..., ge=0.0, le=1.0, description="Current YES price")
    volume: float = Field(..., ge=0.0, description="Trading volume")
    yes_bid: float = Field(..., ge=0.0, le=1.0, description="Best bid for YES")
    yes_ask: float = Field(..., ge=0.0, le=1.0, description="Best ask for YES")
    no_bid: float = Field(..., ge=0.0, le=1.0, description="Best bid for NO")
    no_ask: float = Field(..., ge=0.0, le=1.0, description="Best ask for NO")


class ConfigUpdate(BaseModel):
    """Input model for updating configuration."""
    momentum_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    consensus_gate: Optional[float] = Field(None, ge=0.0)
    edge_threshold: Optional[float] = Field(None, ge=0.0)
    kelly_alpha: Optional[float] = Field(None, ge=0.0, le=1.0)
    kelly_hard_cap: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_concurrent_positions: Optional[int] = Field(None, ge=1)
    scan_interval_seconds: Optional[int] = Field(None, ge=1)
    enable_paper_trading: Optional[bool] = None


class StatusResponse(BaseModel):
    """Response model for status endpoint."""
    running: bool
    bankroll: float
    total_pnl: float
    daily_pnl: float
    drawdown: float
    num_positions: int
    exposure: float
    last_scan: Optional[str]


class PortfolioResponse(BaseModel):
    """Response model for portfolio endpoint."""
    bankroll: float
    start_bankroll: float
    peak_bankroll: float
    total_pnl: float
    daily_pnl: float
    drawdown: float
    exposure: float
    num_positions: int
    total_return: float
    daily_return: float
    positions: List[Dict[str, Any]]


class TradeResponse(BaseModel):
    """Response model for trade endpoint."""
    trade_id: str
    market_id: str
    market_question: str
    direction: str
    signal_edge: float
    signal_consensus: float
    p_model: float
    p_market: float
    edge_confirmed: float
    size: float
    entry_price: float
    exit_price: Optional[float]
    pnl: float
    pnl_pct: float
    open_time: str
    close_time: Optional[str]
    status: str
    execution_latency_ms: float
    slippage: float


class DroppedSignalResponse(BaseModel):
    """Response model for dropped signal endpoint."""
    market_id: str
    direction: str
    p_model: float
    p_market: float
    edge: float
    consensus: float
    stage: str
    reason: str
    details: str
    timestamp: str


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def get_orchestrator() -> AlphaEngineAdapter:
    """Get or create the global AlphaEngineAdapter instance."""
    global _orchestrator

    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                _orchestrator = create_alpha_engine_adapter(
                    total_bankroll=10000.0
                )
                logger.info("Created new AlphaEngineAdapter instance")

    return _orchestrator


# =============================================================================
# HEALTH ENDPOINT
# =============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "trading-forge-api"
    }


# =============================================================================
# STATUS ENDPOINTS
# =============================================================================

@app.get("/status")
async def get_status():
    """Get current trading status."""
    return get_orchestrator().get_status()


@app.get("/portfolio")
async def get_portfolio():
    """
    Get full portfolio details including per-position reasoning data.
    """
    orch = get_orchestrator()
    return orch.get_portfolio()


@app.get("/positions")
async def get_positions():
    """
    Get current open positions.
    
    Returns:
        List of open positions
    """
    orch = get_orchestrator()
    
    positions = []
    for pos in orch.portfolio.positions:
        if pos.status == PositionStatus.OPEN:
            positions.append({
                "position_id": pos.position_id,
                "market_id": pos.market_id,
                "direction": pos.direction.value if isinstance(pos.direction, Direction) else str(pos.direction),
                "entry_price": pos.entry_price,
                "size": pos.size,
                "current_price": pos.current_price,
                "unrealized_pnl": pos.unrealized_pnl,
                "open_time": pos.open_time.isoformat() if pos.open_time else None,
                "status": pos.status.value if isinstance(pos.status, PositionStatus) else str(pos.status)
            })
    
    return {
        "count": len(positions),
        "positions": positions
    }


@app.get("/metrics")
async def get_metrics():
    """Get trading metrics (trades, win rate, etc.)."""
    return get_orchestrator().get_metrics()


@app.get("/feed-activity")
async def get_feed_activity(limit: int = 100):
    """Get live data feed activity and discovered markets."""
    return get_orchestrator().get_feed_activity(limit=limit)


# =============================================================================
# CONTROL ENDPOINTS
# =============================================================================

@app.post("/start")
async def start_trading(background_tasks: BackgroundTasks):
    """
    Start the trading engine.

    Returns:
        Confirmation message
    """
    orch = get_orchestrator()

    if orch.running:
        return {
            "status": "already_running",
            "message": "Trading engine is already running"
        }

    orch.start()
    logger.info("Trading engine started")

    return {
        "status": "started",
        "message": "Trading engine started successfully"
    }


@app.post("/stop")
async def stop_trading():
    """
    Stop the trading engine.

    Returns:
        Confirmation message
    """
    orch = get_orchestrator()

    if not orch.running:
        return {
            "status": "not_running",
            "message": "Trading engine is not running"
        }

    orch.stop()
    logger.info("Trading engine stopped")

    return {
        "status": "stopped",
        "message": "Trading engine stopped successfully"
    }


@app.post("/reset")
async def reset_engine():
    """Stop engine and recreate with fresh state."""
    global _orchestrator
    orch = get_orchestrator()
    if orch.running:
        orch.stop()
    _orchestrator = None
    logger.info("Engine reset — fresh state")
    return {"status": "reset", "message": "Engine reset to fresh state"}


@app.post("/markets")
async def add_market(market_input: MarketInput):
    """
    Add a market to track.
    
    Args:
        market_input: Market details
        
    Returns:
        Confirmation message
    """
    orch = get_orchestrator()
    
    try:
        resolution_time = datetime.fromisoformat(market_input.resolution_time.replace('Z', '+00:00'))
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid resolution_time format. Use ISO format.")
    
    market = Market(
        id=market_input.id,
        question=market_input.question,
        resolution_time=resolution_time,
        current_price=market_input.current_price,
        volume=market_input.volume,
        yes_bid=market_input.yes_bid,
        yes_ask=market_input.yes_ask,
        no_bid=market_input.no_bid,
        no_ask=market_input.no_ask
    )
    
    orch.add_market(market)
    
    logger.info(f"Added market: {market.id}")
    
    return {
        "status": "added",
        "market_id": market.id,
        "message": f"Market {market.id} added to tracking"
    }


# =============================================================================
# HISTORY ENDPOINTS
# =============================================================================

@app.get("/trades")
async def get_trades(limit: int = 50):
    """
    Get list of completed trades.
    """
    orch = get_orchestrator()
    trades_data = orch.get_trades(limit=limit)
    # Fall back to history if no session trades yet
    if not trades_data:
        trades_data = orch.get_history(limit=limit)
    return {
        "count": len(trades_data),
        "trades": trades_data
    }


@app.get("/dropped-signals")
async def get_dropped_signals(limit: int = 50):
    """Get dropped signals from current session."""
    orch = get_orchestrator()
    dropped_data = orch.get_dropped(limit=limit)
    return {
        "count": len(dropped_data),
        "dropped_signals": dropped_data
    }


@app.get("/history")
async def get_history(limit: int = 200):
    """Get full persistent trade history (survives restarts)."""
    orch = get_orchestrator()
    trades = orch.get_history(limit=limit)
    return {"count": len(trades), "trades": trades}


# =============================================================================
# CONFIGURATION ENDPOINTS
# =============================================================================

@app.get("/config")
async def get_config():
    """
    Get current configuration (read from config.py).
    
    Returns:
        Current configuration values
    """
    return {
        "momentum_threshold": MOMENTUM_THRESHOLD,
        "consensus_gate": CONSENSUS_GATE,
        "edge_threshold": EDGE_THRESHOLD,
        "kelly_alpha": KELLY_ALPHA,
        "kelly_hard_cap": KELLY_HARD_CAP,
        "max_concurrent_positions": MAX_CONCURRENT_POSITIONS,
        "target_markets": TARGET_MARKETS,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "enable_paper_trading": ENABLE_PAPER_TRADING
    }


@app.patch("/config")
async def update_config(config_update: ConfigUpdate):
    """
    Update some config values at runtime.
    
    Note: This is a simplified implementation. In production,
    you'd want to use a mutable config object.
    
    Args:
        config_update: Configuration values to update
        
    Returns:
        Updated configuration
    """
    # This is a placeholder - in production you'd want to 
    # update actual config values or use a config manager
    updated_values = {}
    
    if config_update.momentum_threshold is not None:
        updated_values["momentum_threshold"] = config_update.momentum_threshold
    
    if config_update.consensus_gate is not None:
        updated_values["consensus_gate"] = config_update.consensus_gate
    
    if config_update.edge_threshold is not None:
        updated_values["edge_threshold"] = config_update.edge_threshold
    
    if config_update.kelly_alpha is not None:
        updated_values["kelly_alpha"] = config_update.kelly_alpha
    
    if config_update.kelly_hard_cap is not None:
        updated_values["kelly_hard_cap"] = config_update.kelly_hard_cap
    
    if config_update.max_concurrent_positions is not None:
        updated_values["max_concurrent_positions"] = config_update.max_concurrent_positions
    
    if config_update.scan_interval_seconds is not None:
        updated_values["scan_interval_seconds"] = config_update.scan_interval_seconds
    
    if config_update.enable_paper_trading is not None:
        updated_values["enable_paper_trading"] = config_update.enable_paper_trading
    
    logger.info(f"Config updated: {updated_values}")
    
    return {
        "status": "updated",
        "updated_values": updated_values,
        "message": "Configuration updated (runtime updates are temporary)"
    }


# =============================================================================
# LIFECYCLE EVENTS
# =============================================================================

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on server shutdown."""
    global _orchestrator

    if _orchestrator and _orchestrator.running:
        _orchestrator.stop()
        logger.info("Engine stopped due to server shutdown")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, '/home/ldfla/workspace/rayx-claw-final/projects/trading-forge')
    uvicorn.run(app, host="0.0.0.0", port=8000)
