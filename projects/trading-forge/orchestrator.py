"""
Orchestrator - Main Trading Loop

Coordinates all stages:
- Market scanning (PolymarketScanner)
- Pipeline execution (PipelineRunner)
- Regime awareness
- Main loop
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import logging
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

from config import (
    SCAN_INTERVAL_SECONDS,
    SIGNAL_VALIDITY_SECONDS,
    MARKET_RESOLUTION_MINUTES,
    TARGET_MARKETS,
    MIN_MARKET_VOLUME,
    MIN_LIQUIDITY,
    SHARPE_RATIO_TARGET,
    WIN_RATE_TARGET,
    MARKET_RESOLUTION_MINUTES,
)
from models import (
    Market,
    Signal,
    Trade,
    Portfolio,
    Direction,
    PositionStatus,
    DroppedSignal,
    SignalStage,
    DropReason,
)
from signal_generator import generate_signal
from edge_calculator import EdgeCalculator
from toxicity_filters import ToxicityFilters, ToxicityResult
from risk_manager import RiskManager, RiskCheckResult
from execution import ExecutionHandler
from data_feeds import DataFeedManager, CompositeMarketData


logger = logging.getLogger(__name__)


# =============================================================================
# MARKET SCANNER
# =============================================================================

class PolymarketScanner:
    """
    Scans Polymarket for active BTC UP/DOWN binary markets.
    
    Filters for:
    - Active markets
    - BTC-related
    - Resolution within 30 minutes
    - Sufficient volume
    """
    
    def __init__(self):
        self.last_scan: Optional[datetime] = None
        self.active_markets: Dict[str, Market] = {}
        
    def scan(self) -> List[Market]:
        """
        Scan for active markets.
        
        Returns:
            List of active markets meeting criteria
        """
        # In production, this would call Polymarket API
        # For now, return empty list (would be populated by data feed)
        
        self.last_scan = datetime.utcnow()
        
        # Filter for:
        # - Active (not resolved)
        # - BTC UP/DOWN
        # - Resolution within 30 min
        # - Volume > MIN_MARKET_VOLUME
        
        markets = []
        
        for market_id, market in self.active_markets.items():
            # Check if resolved
            if market.resolution_time < datetime.utcnow():
                continue
                
            # Check resolution time
            minutes_left = (market.resolution_time - datetime.utcnow()).total_seconds() / 60
            if minutes_left > MARKET_RESOLUTION_MINUTES:
                continue
            
            # Check volume
            if market.volume < MIN_MARKET_VOLUME:
                continue
            
            markets.append(market)
        
        return markets
    
    def add_market(self, market: Market) -> None:
        """Add a market to track."""
        self.active_markets[market.id] = market
    
    def remove_market(self, market_id: str) -> None:
        """Remove a market."""
        if market_id in self.active_markets:
            del self.active_markets[market_id]
    
    def get_market(self, market_id: str) -> Optional[Market]:
        """Get a specific market."""
        return self.active_markets.get(market_id)


# =============================================================================
# PIPELINE RUNNER
# =============================================================================

@dataclass
class PipelineResult:
    """Result of running the full pipeline."""
    success: bool
    trade: Optional[Trade] = None
    stage_failed: Optional[str] = None
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


class PipelineRunner:
    """
    Runs the complete trading pipeline for a single market.
    
    Stage 1: Signal Generation
    Stage 2: Edge Calculation
    Stage 3: Toxicity Filters
    Stage 4: Kelly Sizing
    Stage 5: Risk Limits
    Stage 6: Execution
    """
    
    def __init__(
        self,
        data_feed_manager: DataFeedManager,
        edge_calculator: EdgeCalculator,
        risk_manager: RiskManager,
        execution_handler: ExecutionHandler
    ):
        self.data_feed_manager = data_feed_manager
        self.edge_calculator = edge_calculator
        self.risk_manager = risk_manager
        self.execution_handler = execution_handler
        self.toxicity_filter = ToxicityFilters()
        
    def run(
        self,
        market: Market,
        portfolio: Portfolio,
        dropped_signals: List[DroppedSignal]
    ) -> PipelineResult:
        """
        Run the full pipeline for a market.
        
        Args:
            market: Market to trade
            portfolio: Current portfolio
            dropped_signals: List to record dropped signals
            
        Returns:
            PipelineResult with trade or failure reason
        """
        start_time = time.time()
        signal_start = datetime.utcnow()
        
        # =========================================================================
        # STAGE 1: Signal Generation
        # =========================================================================
        logger.info(f"[STAGE 1] Generating signal for {market.id}")
        
        # Get market data from feed
        feed = self.data_feed_manager.get_or_create_feed(market.id)
        market_data = feed.to_dict()
        
        # Generate signal
        signal = generate_signal(market, market_data)
        
        if signal is None:
            logger.info(f"[STAGE 1] No signal generated for {market.id}")
            return PipelineResult(
                success=False,
                stage_failed="SIGNAL_GENERATION",
                reason="No signal generated"
            )
        
        logger.info(f"[STAGE 1] Signal generated: {signal.direction} (p={signal.p_model:.3f}, edge={signal.edge:.3f}, C={signal.consensus_score:.2f})")
        
        # =========================================================================
        # STAGE 2: Edge Calculation
        # =========================================================================
        logger.info(f"[STAGE 2] Calculating edge for {market.id}")
        
        # Validate edge
        edge_pass, edge_reason, edge_details = self.edge_calculator.validate_signal(signal)
        
        if not edge_pass:
            logger.warning(f"[STAGE 2] Edge check failed: {edge_reason}")
            dropped_signals.append(DroppedSignal(
                signal=signal,
                stage=SignalStage.EDGE_CALCULATION,
                reason=DropReason.EDGE_INSUFFICIENT,
                details=edge_reason
            ))
            return PipelineResult(
                success=False,
                stage_failed="EDGE_CALCULATION",
                reason=edge_reason,
                details=edge_details
            )
        
        logger.info(f"[STAGE 2] Edge validated: {edge_reason}")
        
        # =========================================================================
        # STAGE 3: Toxicity Filters
        # =========================================================================
        logger.info(f"[STAGE 3] Running toxicity filters for {market.id}")
        
        # Run toxicity filters
        market_data['yes_ask'] = market.yes_ask
        market_data['yes_bid'] = market.yes_bid
        market_data['current_volume'] = market.volume
        
        toxicity_result = self.toxicity_filter.run_all_filters(
            market,
            signal,
            market_data
        )
        
        if not toxicity_result.passes:
            logger.warning(f"[STAGE 3] Toxicity filter failed: {toxicity_result.reason}")
            dropped_signals.append(DroppedSignal(
                signal=signal,
                stage=SignalStage.TOXICITY_FILTERS,
                reason=DropReason.VPIN_TOXIC if "VPIN" in toxicity_result.reason else DropReason.SPREAD_TOXIC,
                details=toxicity_result.reason
            ))
            return PipelineResult(
                success=False,
                stage_failed="TOXICITY_FILTERS",
                reason=toxicity_result.reason,
                details=toxicity_result.details
            )
        
        logger.info(f"[STAGE 3] Toxicity filters passed")
        
        # =========================================================================
        # STAGE 4: Kelly Sizing
        # =========================================================================
        logger.info(f"[STAGE 4] Computing Kelly size for {market.id}")
        
        # Calculate position size
        bet_size, size_reason = self.risk_manager.calculate_position_size(
            signal,
            portfolio
        )
        
        if bet_size <= 0:
            logger.warning(f"[STAGE 4] Kelly size too small: {size_reason}")
            dropped_signals.append(DroppedSignal(
                signal=signal,
                stage=SignalStage.RISK_MANAGEMENT,
                reason=DropReason.KELLY_TOO_SMALL,
                details=size_reason
            ))
            return PipelineResult(
                success=False,
                stage_failed="RISK_MANAGEMENT",
                reason=f"Kelly too small: {size_reason}"
            )
        
        logger.info(f"[STAGE 4] Kelly size: ${bet_size:.2f} ({size_reason})")
        
        # =========================================================================
        # STAGE 5: Risk Limits
        # =========================================================================
        logger.info(f"[STAGE 5] Checking risk limits for {market.id}")
        
        # Run risk checks
        risk_result = self.risk_manager.run_risk_checks(
            signal,
            bet_size,
            portfolio
        )
        
        if not risk_result.passes:
            logger.warning(f"[STAGE 5] Risk check failed: {risk_result.reason}")
            dropped_signals.append(DroppedSignal(
                signal=signal,
                stage=SignalStage.RISK_MANAGEMENT,
                reason=DropReason.RISK_LIMIT_HIT,
                details=risk_result.reason
            ))
            return PipelineResult(
                success=False,
                stage_failed="RISK_MANAGEMENT",
                reason=risk_result.reason,
                details=risk_result.details
            )
        
        logger.info(f"[STAGE 5] Risk checks passed")
        
        # =========================================================================
        # STAGE 6: Execution
        # =========================================================================
        logger.info(f"[STAGE 6] Executing trade for {market.id}")
        
        # Calculate latency
        signal_latency = (datetime.utcnow() - signal_start).total_seconds() * 1000
        
        # Execute trade
        trade = self.execution_handler.execute_trade(
            signal=signal,
            bet_size=bet_size,
            market=market,
            execution_latency_ms=signal_latency
        )
        
        if trade is None:
            logger.error(f"[STAGE 6] Trade execution failed")
            return PipelineResult(
                success=False,
                stage_failed="EXECUTION",
                reason="Trade execution failed"
            )
        
        # Update portfolio
        portfolio.bankroll -= bet_size
        if portfolio not in [p for p in []]:
            pass  # Position tracking
        
        elapsed = time.time() - start_time
        logger.info(f"[STAGE 6] Trade executed: {trade.direction.value} ${trade.size:.2f} @ {trade.entry_price:.4f} (latency: {signal_latency:.0f}ms, total: {elapsed:.2f}s)")
        
        return PipelineResult(
            success=True,
            trade=trade,
            reason=f"Trade executed: {trade.direction.value} ${trade.size:.2f}",
            details={
                'signal_latency_ms': signal_latency,
                'total_latency_ms': elapsed * 1000,
                'bet_size': bet_size,
                'entry_price': trade.entry_price
            }
        )


# =============================================================================
# REGIME AWARENESS
# =============================================================================

class Regime:
    """Market regime classification."""
    NORMAL = "normal"
    HIGH_VOL = "high_vol"
    LOW_LIQ = "low_liq"
    CRISIS = "crisis"


class RegimeDetector:
    """Detects market regime for parameter adjustment."""
    
    def __init__(self):
        self.regime = Regime.NORMAL
        self.vol_sma: float = 0.0
        self.liq_sma: float = 0.0
        
    def detect(
        self,
        recent_volatility: float,
        liquidity: float
    ) -> str:
        """
        Detect current market regime.
        
        Args:
            recent_volatility: Recent volatility measure
            liquidity: Current liquidity
            
        Returns:
            Regime string
        """
        # Update SMAs
        if self.vol_sma == 0:
            self.vol_sma = recent_volatility
        else:
            self.vol_sma = 0.7 * self.vol_sma + 0.3 * recent_volatility
            
        if self.liq_sma == 0:
            self.liq_sma = liquidity
        else:
            self.liq_sma = 0.7 * self.liq_sma + 0.3 * liquidity
        
        # Detect regime
        vol_ratio = recent_volatility / self.vol_sma if self.vol_sma > 0 else 1.0
        liq_ratio = liquidity / self.liq_sma if self.liq_sma > 0 else 1.0
        
        if vol_ratio > 2.0:
            self.regime = Regime.CRISIS
        elif vol_ratio > 1.5:
            self.regime = Regime.HIGH_VOL
        elif liq_ratio < 0.5:
            self.regime = Regime.LOW_LIQ
        else:
            self.regime = Regime.NORMAL
        
        return self.regime
    
    def get_alpha_adjustment(self) -> float:
        """Get Kelly alpha adjustment for current regime."""
        adjustments = {
            Regime.NORMAL: 1.0,
            Regime.HIGH_VOL: 0.5,   # Reduce size in high vol
            Regime.LOW_LIQ: 0.5,    # Reduce size in low liquidity
            Regime.CRISIS: 0.25,   # Significantly reduce in crisis
        }
        return adjustments.get(self.regime, 1.0)
    
    def get_edge_threshold_adjustment(self) -> float:
        """Get edge threshold adjustment for current regime."""
        adjustments = {
            Regime.NORMAL: 1.0,
            Regime.HIGH_VOL: 1.5,   # Require more edge in high vol
            Regime.LOW_LIQ: 1.5,    # Require more edge in low liquidity
            Regime.CRISIS: 2.0,     # Much higher threshold in crisis
        }
        return adjustments.get(self.regime, 1.0)


# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================

class TradingOrchestrator:
    """
    Main orchestrator for the trading bot.
    
    Coordinates scanning, pipeline execution, and portfolio management.
    """
    
    def __init__(
        self,
        initial_bankroll: float = 10000.0,
        paper_trading: bool = True
    ):
        # Core components
        self.scanner = PolymarketScanner()
        self.data_feed_manager = DataFeedManager()
        self.edge_calculator = EdgeCalculator()
        self.risk_manager = RiskManager(initial_bankroll)
        self.execution_handler = ExecutionHandler(paper_trading=paper_trading)
        self.regime_detector = RegimeDetector()
        
        # Portfolio
        self.portfolio = Portfolio(
            bankroll=initial_bankroll,
            start_bankroll=initial_bankroll,
            peak_bankroll=initial_bankroll
        )
        
        # Pipeline
        self.pipeline = PipelineRunner(
            data_feed_manager=self.data_feed_manager,
            edge_calculator=self.edge_calculator,
            risk_manager=self.risk_manager,
            execution_handler=self.execution_handler
        )
        
        # State
        self.running = False
        self.last_scan_time: Optional[datetime] = None
        self.dropped_signals: List[DroppedSignal] = []
        self.completed_trades: List[Trade] = []
        
        # Counters
        self.total_scans = 0
        self.total_signals = 0
        self.total_trades = 0
        
        logger.info(f"TradingOrchestrator initialized with bankroll: ${initial_bankroll:.2f}")
    
    def add_market(self, market: Market) -> None:
        """Add a market to track."""
        self.scanner.add_market(market)
    
    def update_market_data(self, market_id: str, data: Dict) -> None:
        """Update market data in the feed."""
        feed = self.data_feed_manager.get_or_create_feed(market_id)
        # Update with new data...
    
    def scan_and_process(self) -> List[PipelineResult]:
        """
        Scan markets and process each through the pipeline.
        
        Returns:
            List of pipeline results
        """
        results = []
        
        # Scan for markets
        markets = self.scanner.scan()
        
        if not markets:
            logger.debug("No markets to trade")
            return results
        
        logger.info(f"Found {len(markets)} markets to process")
        
        # Process each market
        for market in markets:
            # Skip if already have position
            if self.execution_handler.get_position(market.id):
                logger.debug(f"Skipping {market.id}: already have position")
                continue
            
            # Run pipeline
            result = self.pipeline.run(market, self.portfolio, self.dropped_signals)
            results.append(result)
            
            if result.success and result.trade:
                self.completed_trades.append(result.trade)
                self.total_trades += 1
                self.risk_manager.record_trade(result.trade)
        
        self.total_scans += 1
        self.last_scan_time = datetime.utcnow()
        
        return results
    
    def check_resolutions(self) -> List[Trade]:
        """
        Check for resolved markets and close positions.
        
        Returns:
            List of closed trades
        """
        resolved_trades = []
        
        # Get all open positions
        positions = self.execution_handler.get_all_positions()
        
        for position in positions:
            market = self.scanner.get_market(position.market_id)
            
            if market is None:
                continue
            
            # Check if resolved
            if market.resolution_time < datetime.utcnow():
                # In production, would fetch actual resolution
                # For now, simulate based on price
                outcome = "YES" if market.current_price > 0.5 else "NO"
                
                trade = self.execution_handler.on_market_resolve(
                    market_id=position.market_id,
                    outcome=outcome,
                    resolution_price=market.current_price
                )
                
                if trade:
                    resolved_trades.append(trade)
                    
                    # Update portfolio
                    self.portfolio.bankroll += trade.size + trade.pnl
                    self.portfolio.update_from_trade(trade)
                    
                    # Record for calibration
                    self.edge_calculator.record_outcome(
                        trade.p_model,
                        1.0 if outcome == "YES" else 0.0
                    )
        
        return resolved_trades
    
    def get_performance_summary(self) -> Dict:
        """Get performance summary."""
        if not self.completed_trades:
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'sharpe_ratio': 0.0
            }
        
        wins = [t for t in self.completed_trades if t.pnl > 0]
        losses = [t for t in self.completed_trades if t.pnl <= 0]
        
        win_rate = len(wins) / len(self.completed_trades)
        total_pnl = sum(t.pnl for t in self.completed_trades)
        
        return {
            'total_trades': len(self.completed_trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'bankroll': self.portfolio.bankroll,
            'drawdown': self.portfolio.drawdown,
            'total_scans': self.total_scans,
            'dropped_signals': len(self.dropped_signals)
        }
    
    def run(self, duration_seconds: Optional[int] = None):
        """
        Run the main trading loop.
        
        Args:
            duration_seconds: Optional duration limit
        """
        self.running = True
        start_time = time.time()
        
        logger.info("Starting trading loop")
        
        try:
            while self.running:
                # Check duration limit
                if duration_seconds:
                    elapsed = time.time() - start_time
                    if elapsed >= duration_seconds:
                        logger.info(f"Duration limit reached: {elapsed:.0f}s")
                        break
                
                # Check resolutions
                resolved = self.check_resolutions()
                if resolved:
                    logger.info(f"Resolved {len(resolved)} positions")
                
                # Scan and process
                results = self.scan_and_process()
                
                # Log summary
                if results:
                    successes = sum(1 for r in results if r.success)
                    logger.info(f"Scan {self.total_scans}: {len(results)} markets, {successes} trades")
                
                # Sleep until next scan
                time.sleep(SCAN_INTERVAL_SECONDS)
                
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            self.running = False
            logger.info("Trading loop stopped")
            
            # Print final summary
            summary = self.get_performance_summary()
            logger.info(f"Final Summary: {summary}")
    
    def stop(self):
        """Stop the trading loop."""
        self.running = False
        logger.info("Stopping trading orchestrator")


# =============================================================================
# STANDALONE FUNCTIONS
# =============================================================================

def create_orchestrator(
    bankroll: float = 10000.0,
    paper_trading: bool = True
) -> TradingOrchestrator:
    """Create and configure an orchestrator."""
    return TradingOrchestrator(
        initial_bankroll=bankroll,
        paper_trading=paper_trading
    )


def run_single_pipeline(
    market: Market,
    portfolio: Portfolio,
    data_feed_manager: DataFeedManager,
    edge_calculator: EdgeCalculator,
    risk_manager: RiskManager,
    execution_handler: ExecutionHandler
) -> PipelineResult:
    """Run pipeline for a single market."""
    pipeline = PipelineRunner(
        data_feed_manager=data_feed_manager,
        edge_calculator=edge_calculator,
        risk_manager=risk_manager,
        execution_handler=execution_handler
    )
    
    dropped = []
    return pipeline.run(market, portfolio, dropped)
