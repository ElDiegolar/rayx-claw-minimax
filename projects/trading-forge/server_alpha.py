"""
Trading Forge FastAPI Server (Alpha Engine Edition)

Provides REST API for:
- Status and portfolio monitoring
- Trading control (start/stop)
- Market management
- Configuration management
- Trade and signal history

Uses AlphaEngine from polymarket_alpha_engine.py via AlphaEngineAdapter.

Author: Trading Forge Team
"""

import logging
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn

# Import config from the trading forge config module
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

# Import AlphaEngineAdapter instead of TradingOrchestrator
from alpha_engine_adapter import AlphaEngineAdapter
from polymarket_alpha_engine import EngineConfig

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
    description="REST API for the Trading Forge trading bot (Alpha Engine)",
    version="2.0.0"
)

# Global state - using AlphaEngineAdapter
_orchestrator: Optional[AlphaEngineAdapter] = None
_orchestrator_thread: Optional[threading.Thread] = None
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
                # Create EngineConfig from existing config values
                config = EngineConfig()
                config.total_bankroll = 10000.0  # Default bankroll
                
                _orchestrator = AlphaEngineAdapter(config)
                logger.info("Created new AlphaEngineAdapter instance")
    
    return _orchestrator


def run_orchestrator():
    """Run the AlphaEngineAdapter in a background thread."""
    global _orchestrator
    
    orch = get_orchestrator()
    
    try:
        orch.start()
    except Exception as e:
        logger.error(f"Orchestrator error: {e}")
        orch.running = False


# =============================================================================
# HEALTH ENDPOINT
# =============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": "trading-forge-api-alpha-engine"
    }


# =============================================================================
# STATUS ENDPOINTS
# =============================================================================

@app.get("/status", response_model=StatusResponse)
async def get_status():
    """
    Get current trading status.
    
    Returns:
        StatusResponse with current trading state
    """
    orch = get_orchestrator()
    
    return StatusResponse(
        running=orch.running,
        bankroll=orch.portfolio.bankroll,
        total_pnl=orch.portfolio.total_pnl,
        daily_pnl=orch.portfolio.daily_pnl,
        drawdown=orch.portfolio.drawdown,
        num_positions=orch.portfolio.num_positions,
        exposure=orch.portfolio.exposure,
        last_scan=orch.last_scan_time.isoformat() if orch.last_scan_time else None
    )


@app.get("/portfolio", response_model=PortfolioResponse)
async def get_portfolio():
    """
    Get full portfolio details.
    
    Returns:
        PortfolioResponse with complete portfolio state
    """
    orch = get_orchestrator()
    portfolio_data = orch.get_portfolio()
    
    return PortfolioResponse(
        bankroll=portfolio_data["bankroll"],
        start_bankroll=portfolio_data["start_bankroll"],
        peak_bankroll=portfolio_data["peak_bankroll"],
        total_pnl=portfolio_data["total_pnl"],
        daily_pnl=portfolio_data["daily_pnl"],
        drawdown=portfolio_data["drawdown"],
        exposure=portfolio_data["exposure"],
        num_positions=portfolio_data["num_positions"],
        total_return=portfolio_data["total_return"],
        daily_return=portfolio_data["daily_return"],
        positions=portfolio_data["positions"]
    )


@app.get("/positions")
async def get_positions():
    """
    Get current open positions.
    
    Returns:
        List of open positions
    """
    orch = get_orchestrator()
    
    positions = orch.get_positions()
    
    return {
        "count": len(positions),
        "positions": positions
    }


@app.get("/metrics")
async def get_metrics():
    """
    Get trading metrics (trades, win rate, etc.).
    
    Returns:
        Dictionary with performance metrics
    """
    orch = get_orchestrator()
    
    return orch.get_metrics()


# =============================================================================
# CONTROL ENDPOINTS
# =============================================================================

@app.post("/start")
async def start_trading(background_tasks: BackgroundTasks):
    """
    Start the AlphaEngine trading bot.
    
    Returns:
        Confirmation message
    """
    global _orchestrator_thread
    
    orch = get_orchestrator()
    
    if orch.running:
        return {
            "status": "already_running",
            "message": "Trading bot is already running"
        }
    
    # Start orchestrator in background thread
    orch.start()
    _orchestrator_thread = threading.Thread(target=run_orchestrator, daemon=True)
    _orchestrator_thread.start()
    
    logger.info("AlphaEngine trading started")
    
    return {
        "status": "started",
        "message": "AlphaEngine trading started successfully"
    }


@app.post("/stop")
async def stop_trading():
    """
    Stop the AlphaEngine trading bot.
    
    Returns:
        Confirmation message
    """
    orch = get_orchestrator()
    
    if not orch.running:
        return {
            "status": "not_running",
            "message": "Trading bot is not running"
        }
    
    orch.stop()
    logger.info("AlphaEngine trading stopped")
    
    return {
        "status": "stopped",
        "message": "AlphaEngine trading stopped successfully"
    }


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
    
    Args:
        limit: Maximum number of trades to return (default 50)
        
    Returns:
        List of completed trades
    """
    orch = get_orchestrator()
    
    return {
        "count": limit,
        "trades": orch.get_trades(limit)
    }


@app.get("/dropped-signals")
async def get_dropped_signals(limit: int = 50):
    """
    Get list of dropped signals.
    
    Args:
        limit: Maximum number of signals to return (default 50)
        
    Returns:
        List of dropped signals
    """
    orch = get_orchestrator()
    
    return {
        "count": limit,
        "signals": orch.get_dropped_signals(limit)
    }


# =============================================================================
# CONFIG ENDPOINTS
# =============================================================================

@app.get("/config")
async def get_config():
    """
    Get current configuration.
    
    Returns:
        Current configuration values
    """
    orch = get_orchestrator()
    config = orch._config  # Access internal config
    
    return {
        "total_bankroll": config.total_bankroll,
        "stream1_alloc": config.stream1_alloc,
        "stream2_alloc": config.stream2_alloc,
        "stream3_alloc": config.stream3_alloc,
        "stream4_alloc": config.stream4_alloc,
        "alpha_base": config.alpha_base,
        "alpha_geo_risk": config.alpha_geo_risk,
        "alpha_cascade": config.alpha_cascade,
        "alpha_cme_pin": config.alpha_cme_pin,
        "alpha_ibit_confirm": config.alpha_ibit_confirm,
        "max_bet_pct": config.max_bet_pct,
        "edge_min_base": config.edge_min_base,
        "edge_min_geo": config.edge_min_geo,
        "edge_min_pin": config.edge_min_pin,
        "edge_min_cascade_hi": config.edge_min_cascade_hi,
        "spread_max": config.spread_max,
        "vpin_threshold": config.vpin_threshold,
        "vol_spike_halt": config.vol_spike_halt,
        "vol_spike_resume": config.vol_spike_resume,
        "consensus_threshold": config.consensus_threshold,
        "daily_var_limit": config.daily_var_limit,
        "max_drawdown": config.max_drawdown,
        "max_exposure_ratio": config.max_exposure_ratio,
        "streak_loss_pause_n": config.streak_loss_pause_n,
        "streak_pause_min": config.streak_pause_min,
    }


@app.patch("/config")
async def update_config(config_update: ConfigUpdate):
    """
    Update configuration values.
    
    Args:
        config_update: Configuration values to update
        
    Returns:
        Confirmation message
    """
    orch = get_orchestrator()
    config = orch._config  # Access internal config
    
    if config_update.momentum_threshold is not None:
        # Map to appropriate config field
        pass
    
    if config_update.kelly_alpha is not None:
        config.alpha_base = config_update.kelly_alpha
    
    if config_update.kelly_hard_cap is not None:
        config.max_bet_pct = config_update.kelly_hard_cap
    
    if config_update.edge_threshold is not None:
        config.edge_min_base = config_update.edge_threshold
    
    if config_update.consensus_gate is not None:
        config.consensus_threshold = config_update.consensus_gate
    
    logger.info("Configuration updated")
    
    return {
        "status": "updated",
        "message": "Configuration updated successfully"
    }


# =============================================================================
# ENGINE STATUS ENDPOINTS
# =============================================================================

@app.get("/engine/status")
async def get_engine_status():
    """
    Get AlphaEngine internal status.
    
    Returns:
        Internal engine state
    """
    orch = get_orchestrator()
    engine = orch._engine
    
    return {
        "running": orch.running,
        "cycle": engine._cycle if hasattr(engine, '_cycle') else 0,
        "regime": engine.portfolio.regime.value if hasattr(engine.portfolio, 'regime') else "UNKNOWN",
        "stream_mode": engine.portfolio.stream_mode.value if hasattr(engine.portfolio, 'stream_mode') else "UNKNOWN",
        "current_alpha": engine.portfolio.current_alpha if hasattr(engine.portfolio, 'current_alpha') else 0.0,
    }


@app.get("/engine/state")
async def get_engine_state():
    """
    Get AlphaEngine market state.
    
    Returns:
        Current market data
    """
    orch = get_orchestrator()
    state = orch._engine.state
    
    return {
        "btc_spot": state.btc_spot,
        "cvd_now": state.cvd_now,
        "cvd_5min_ago": state.cvd_5min_ago,
        "funding_rate_8hr": state.funding_rate_8hr,
        "oi_now": state.oi_now,
        "oi_5min_ago": state.oi_5min_ago,
        "iv_atm": state.iv_atm,
        "poly_yes_bid": state.poly_yes_bid,
        "poly_yes_ask": state.poly_yes_ask,
        "poly_no_bid": state.poly_no_bid,
        "poly_no_ask": state.poly_no_ask,
        "last_updated": state.last_updated,
    }
