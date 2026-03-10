"""
Logging Module

Handles all trading logs:
- Trade logging
- Dropped signal logging
- Performance metrics
- Debug logging

All fields from Stage 6 logging spec are captured.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import logging
import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
import csv


logger = logging.getLogger(__name__)


# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================

# Log file paths
LOG_DIR = Path("/home/ldfla/workspace/rayx-claw-final/projects/trading-forge/logs")

TRADE_LOG_FILE = LOG_DIR / "trades.csv"
DROPPED_SIGNAL_LOG_FILE = LOG_DIR / "dropped_signals.csv"
PERFORMANCE_LOG_FILE = LOG_DIR / "performance.json"
DEBUG_LOG_FILE = LOG_DIR / "debug.log"


# =============================================================================
# TRADE LOGGER
# =============================================================================

class TradeLogger:
    """
    Logs all trades with complete fields from Stage 6 spec.
    
    Fields logged:
    - trade_id, market_id, market_question
    - direction, signal_edge, signal_consensus
    - p_model, p_market, edge_confirmed
    - size, entry_price, exit_price
    - pnl, pnl_pct, open_time, close_time
    - status, execution_latency_ms, slippage
    """
    
    def __init__(self, log_file: Path = TRADE_LOG_FILE):
        self.log_file = log_file
        self.trades: deque = deque(maxlen=1000)
        self._initialized = False
        
    def _ensure_initialized(self) -> None:
        """Ensure log file exists with headers."""
        if self._initialized:
            return
            
        # Create log directory if needed
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Create file with headers if it doesn't exist
        if not self.log_file.exists():
            headers = [
                'trade_id', 'market_id', 'market_question',
                'direction', 'signal_edge', 'signal_consensus',
                'p_model', 'p_market', 'edge_confirmed',
                'size', 'entry_price', 'exit_price',
                'pnl', 'pnl_pct',
                'open_time', 'close_time', 'status',
                'execution_latency_ms', 'slippage',
                'timestamp'
            ]
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
        
        self._initialized = True
    
    def log_trade(self, trade) -> None:
        """
        Log a completed trade.
        
        Args:
            trade: Trade object with all fields
        """
        self._ensure_initialized()
        
        # Store in memory
        self.trades.append(trade)
        
        # Write to file
        row = {
            'trade_id': trade.trade_id,
            'market_id': trade.market_id,
            'market_question': trade.market_question,
            'direction': trade.direction.value if hasattr(trade.direction, 'value') else str(trade.direction),
            'signal_edge': trade.signal_edge,
            'signal_consensus': trade.signal_consensus,
            'p_model': trade.p_model,
            'p_market': trade.p_market,
            'edge_confirmed': trade.edge_confirmed,
            'size': trade.size,
            'entry_price': trade.entry_price,
            'exit_price': trade.exit_price,
            'pnl': trade.pnl,
            'pnl_pct': trade.pnl_pct,
            'open_time': trade.open_time.isoformat() if trade.open_time else '',
            'close_time': trade.close_time.isoformat() if trade.close_time else '',
            'status': trade.status.value if hasattr(trade.status, 'value') else str(trade.status),
            'execution_latency_ms': trade.execution_latency_ms,
            'slippage': trade.slippage,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            writer.writerow(row)
        
        logger.info(f"Logged trade: {trade.trade_id} {row['direction']} ${trade.size:.2f} PnL: ${trade.pnl:.2f}")
    
    def get_recent_trades(self, n: int = 10) -> List:
        """Get N most recent trades."""
        return list(self.trades)[-n:]
    
    def get_trades_by_market(self, market_id: str) -> List:
        """Get all trades for a specific market."""
        return [t for t in self.trades if t.market_id == market_id]
    
    def get_winning_trades(self) -> List:
        """Get all winning trades."""
        return [t for t in self.trades if t.pnl > 0]
    
    def get_losing_trades(self) -> List:
        """Get all losing trades."""
        return [t for t in self.trades if t.pnl <= 0]
    
    def get_total_pnl(self) -> float:
        """Get total P&L across all trades."""
        return sum(t.pnl for t in self.trades)
    
    def get_win_rate(self) -> float:
        """Calculate win rate."""
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for summary."""
        trades_list = [t.to_dict() if hasattr(t, 'to_dict') else t for t in self.trades]
        return {
            'total_trades': len(self.trades),
            'winning_trades': len(self.get_winning_trades()),
            'losing_trades': len(self.get_losing_trades()),
            'total_pnl': self.get_total_pnl(),
            'win_rate': self.get_win_rate(),
            'trades': trades_list
        }


# =============================================================================
# DROPPED SIGNAL LOGGER
# =============================================================================

class DroppedSignalLogger:
    """
    Logs signals that were dropped at each pipeline stage.
    
    Tracks:
    - market_id, direction, edge, consensus
    - stage where dropped
    - reason for drop
    - details
    """
    
    def __init__(self, log_file: Path = DROPPED_SIGNAL_LOG_FILE):
        self.log_file = log_file
        self.dropped_signals: deque = deque(maxlen=1000)
        self._initialized = False
        
    def _ensure_initialized(self) -> None:
        """Ensure log file exists with headers."""
        if self._initialized:
            return
            
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        if not self.log_file.exists():
            headers = [
                'market_id', 'direction',
                'p_model', 'p_market', 'edge', 'consensus',
                'stage', 'reason', 'details',
                'timestamp'
            ]
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
        
        self._initialized = True
    
    def log_dropped_signal(
        self,
        market_id: str,
        direction: str,
        p_model: float,
        p_market: float,
        edge: float,
        consensus: float,
        stage: str,
        reason: str,
        details: str = ""
    ) -> None:
        """
        Log a dropped signal.
        
        Args:
            market_id: Market ID
            direction: Signal direction
            p_model: Model probability
            p_market: Market probability
            edge: Signal edge
            consensus: Consensus score
            stage: Pipeline stage where dropped
            reason: Reason for dropping
            details: Additional details
        """
        self._ensure_initialized()
        
        signal_data = {
            'market_id': market_id,
            'direction': direction,
            'p_model': p_model,
            'p_market': p_market,
            'edge': edge,
            'consensus': consensus,
            'stage': stage,
            'reason': reason,
            'details': details,
            'timestamp': datetime.utcnow().isoformat()
        }
        
        self.dropped_signals.append(signal_data)
        
        # Write to file
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=signal_data.keys())
            writer.writerow(signal_data)
        
        logger.info(f"Dropped signal: {market_id} {direction} @ {stage} ({reason})")
    
    def get_by_stage(self, stage: str) -> List[Dict]:
        """Get dropped signals for a specific stage."""
        return [s for s in self.dropped_signals if s['stage'] == stage]
    
    def get_by_reason(self, reason: str) -> List[Dict]:
        """Get dropped signals for a specific reason."""
        return [s for s in self.dropped_signals if s['reason'] == reason]
    
    def get_drop_rate(self, total_signals: int) -> float:
        """Calculate drop rate."""
        if total_signals == 0:
            return 0.0
        return len(self.dropped_signals) / total_signals
    
    def get_summary(self) -> Dict:
        """Get summary of dropped signals."""
        stages = {}
        reasons = {}
        
        for s in self.dropped_signals:
            stage = s['stage']
            reason = s['reason']
            
            stages[stage] = stages.get(stage, 0) + 1
            reasons[reason] = reasons.get(reason, 0) + 1
        
        return {
            'total_dropped': len(self.dropped_signals),
            'by_stage': stages,
            'by_reason': reasons
        }


# =============================================================================
# PERFORMANCE LOGGER
# =============================================================================

class PerformanceLogger:
    """
    Logs and tracks performance metrics.
    
    Metrics:
    - Total trades, win rate, profit factor
    - Sharpe ratio, max drawdown
    - Current streak, win/loss streaks
    """
    
    def __init__(self, log_file: Path = PERFORMANCE_LOG_FILE):
        self.log_file = log_file
        self.metrics_history: deque = deque(maxlen=100)
        self.trade_logger = TradeLogger()
        self.dropped_logger = DroppedSignalLogger()
        
    def log_performance_metrics(self) -> Dict:
        """
        Log current performance metrics.
        
        Returns:
            Dictionary of performance metrics
        """
        trades = list(self.trade_logger.trades)
        
        if not trades:
            metrics = {
                'timestamp': datetime.utcnow().isoformat(),
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'profit_factor': 0.0,
                'sharpe_ratio': 0.0,
                'max_drawdown': 0.0,
                'current_streak': 0,
                'max_win_streak': 0,
                'max_loss_streak': 0,
            }
        else:
            # Calculate metrics
            wins = [t for t in trades if t.pnl > 0]
            losses = [t for t in trades if t.pnl <= 0]
            
            total_pnl = sum(t.pnl for t in trades)
            win_rate = len(wins) / len(trades) if trades else 0
            
            avg_win = sum(t.pnl for t in wins) / len(wins) if wins else 0
            avg_loss = abs(sum(t.pnl for t in losses) / len(losses)) if losses else 1
            
            profit_factor = avg_win / avg_loss if avg_loss > 0 else 0
            
            # Calculate streaks
            current_streak = 0
            max_win_streak = 0
            max_loss_streak = 0
            
            for t in reversed(trades):
                if t.pnl > 0:
                    if current_streak >= 0:
                        current_streak += 1
                    else:
                        current_streak = 1
                    max_win_streak = max(max_win_streak, current_streak)
                else:
                    if current_streak <= 0:
                        current_streak -= 1
                    else:
                        current_streak = -1
                    max_loss_streak = min(max_loss_streak, current_streak)
            
            # Sharpe ratio (simplified)
            if len(trades) > 1:
                returns = [t.pnl_pct for t in trades]
                avg_return = sum(returns) / len(returns)
                variance = sum((r - avg_return) ** 2 for r in returns) / len(returns)
                std_dev = variance ** 0.5
                sharpe = (avg_return / std_dev * (252 ** 0.5)) if std_dev > 0 else 0
            else:
                sharpe = 0
            
            # Max drawdown
            peak = trades[0].size if trades else 0
            max_dd = 0
            
            cumulative = 0
            for t in trades:
                cumulative += t.pnl
                peak = max(peak, cumulative)
                dd = (peak - cumulative) / peak if peak > 0 else 0
                max_dd = max(max_dd, dd)
            
            metrics = {
                'timestamp': datetime.utcnow().isoformat(),
                'total_trades': len(trades),
                'winning_trades': len(wins),
                'losing_trades': len(losses),
                'win_rate': win_rate,
                'total_pnl': total_pnl,
                'avg_win': avg_win,
                'avg_loss': avg_loss,
                'profit_factor': profit_factor,
                'sharpe_ratio': sharpe,
                'max_drawdown': max_dd,
                'current_streak': current_streak,
                'max_win_streak': max_win_streak,
                'max_loss_streak': abs(max_loss_streak),
            }
        
        # Store and write
        self.metrics_history.append(metrics)
        
        # Write to file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        with open(self.log_file, 'w') as f:
            json.dump(metrics, f, indent=2)
        
        return metrics
    
    def check_targets(
        self,
        sharpe_target: float = 2.0,
        win_rate_target: float = 0.58
    ) -> Dict[str, bool]:
        """
        Check if performance targets are met.
        
        Args:
            sharpe_target: Target Sharpe ratio
            win_rate_target: Target win rate
            
        Returns:
            Dictionary of target checks
        """
        if not self.metrics_history:
            return {
                'sharpe_met': False,
                'win_rate_met': False,
                'all_met': False
            }
        
        latest = self.metrics_history[-1]
        
        return {
            'sharpe_met': latest['sharpe_ratio'] >= sharpe_target,
            'win_rate_met': latest['win_rate'] >= win_rate_target,
            'all_met': latest['sharpe_ratio'] >= sharpe_target and latest['win_rate'] >= win_rate_target
        }
    
    def get_summary(self) -> Dict:
        """Get performance summary."""
        return {
            'total_trades': len(self.trade_logger.trades),
            'total_pnl': self.trade_logger.get_total_pnl(),
            'win_rate': self.trade_logger.get_win_rate(),
            'dropped_signals': len(self.dropped_logger.dropped_signals),
            'drop_summary': self.dropped_logger.get_summary()
        }


# =============================================================================
# DEBUG LOGGER
# =============================================================================

class DebugLogger:
    """
    Handles debug and verbose logging.
    """
    
    def __init__(self, log_file: Path = DEBUG_LOG_FILE):
        self.log_file = log_file
        
        # Setup file handler
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Configure logging
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        )
        
        # Get root logger
        root_logger = logging.getLogger()
        root_logger.addHandler(file_handler)
    
    def log_stage(
        self,
        stage: str,
        market_id: str,
        data: Dict
    ) -> None:
        """Log pipeline stage data."""
        logger.debug(f"[{stage}] {market_id}: {json.dumps(data)}")
    
    def log_signal(self, signal) -> None:
        """Log signal details."""
        logger.debug(f"Signal: {signal.to_dict()}")
    
    def log_market_update(self, market_id: str, data: Dict) -> None:
        """Log market data update."""
        logger.debug(f"Market {market_id}: {json.dumps(data, default=str)}")


# =============================================================================
# MAIN LOGGER CLASS
# =============================================================================

class TradingLogger:
    """
    Main logging interface combining all loggers.
    """
    
    def __init__(
        self,
        log_dir: Path = LOG_DIR,
        debug: bool = False
    ):
        self.log_dir = log_dir
        self.debug = debug
        
        # Initialize sub-loggers
        self.trade_logger = TradeLogger(log_dir / "trades.csv")
        self.dropped_logger = DroppedSignalLogger(log_dir / "dropped_signals.csv")
        self.performance_logger = PerformanceLogger(log_dir / "performance.json")
        
        # Debug logger
        if debug:
            self.debug_logger = DebugLogger(log_dir / "debug.log")
    
    def log_trade(self, trade) -> None:
        """Log a completed trade."""
        self.trade_logger.log_trade(trade)
        
        # Update performance
        self.performance_logger.log_performance_metrics()
    
    def log_dropped_signal(
        self,
        signal,
        stage: str,
        reason: str,
        details: str = ""
    ) -> None:
        """Log a dropped signal."""
        self.dropped_logger.log_dropped_signal(
            market_id=signal.market_id,
            direction=signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction),
            p_model=signal.p_model,
            p_market=signal.p_market,
            edge=signal.edge,
            consensus=signal.consensus_score,
            stage=stage,
            reason=reason,
            details=details
        )
    
    def log_performance(self) -> Dict:
        """Log and return performance metrics."""
        return self.performance_logger.log_performance_metrics()
    
    def get_summary(self) -> Dict:
        """Get complete logging summary."""
        return {
            'trades': self.trade_logger.to_dict(),
            'dropped': self.dropped_logger.get_summary(),
            'performance': self.performance_logger.get_summary()
        }
    
    def check_targets(self) -> Dict[str, bool]:
        """Check if performance targets are met."""
        return self.performance_logger.check_targets()


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def log_trade(trade) -> None:
    """Log a trade."""
    logger_instance = TradeLogger()
    logger_instance.log_trade(trade)


def log_dropped_signal(signal, stage: str, reason: str) -> None:
    """Log a dropped signal."""
    logger_instance = DroppedSignalLogger()
    logger_instance.log_dropped_signal(
        market_id=signal.market_id,
        direction=str(signal.direction),
        p_model=signal.p_model,
        p_market=signal.p_market,
        edge=signal.edge,
        consensus=signal.consensus_score,
        stage=stage,
        reason=reason
    )


def log_performance_metrics() -> Dict:
    """Log performance metrics."""
    logger_instance = PerformanceLogger()
    return logger_instance.log_performance_metrics()


# =============================================================================
# DEFAULT INSTANCE
# =============================================================================

# Default logger instance
default_logger = TradingLogger()
