"""
Stage 4 & 5: Risk Manager - Kelly Sizing & Risk Limits

Stage 4: Kelly Criterion position sizing
Stage 5: Risk limits and constraints

All risk enforcement is deterministic/mathematical.
"""

from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
import logging
from datetime import datetime, timedelta
from collections import deque

from config import (
    KELLY_ALPHA,
    KELLY_HARD_CAP,
    KELLY_MINIMUM,
    DAILY_VAR_LIMIT,
    MAX_DRAWDOWN_LIMIT,
    SINGLE_MARKET_EXPOSURE,
    TOTAL_EXPOSURE_LIMIT,
    MAX_CONCURRENT_POSITIONS,
    LOSS_STREAK_HALT,
    LOSS_STREAK_PAUSE_MINUTES,
    MAX_POSITION_CORRELATION,
    WIN_RATE_TARGET,
)
from models import Portfolio, Position, Trade, Signal, Direction


logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    """Result of risk check."""
    passes: bool
    reason: str
    details: Dict


class RiskManager:
    """
    Manages position sizing and risk limits.
    
    Stage 4: Kelly Criterion sizing
    Stage 5: Risk limits (VaR, DD, exposure, correlation, streak)
    """
    
    def __init__(self, bankroll: float):
        self.initial_bankroll = bankroll
        self.bankroll = bankroll
        self.daily_pnl = 0.0
        self.daily_start = bankroll
        
        # Position tracking
        self.positions: Dict[str, Position] = {}  # market_id -> Position
        self.position_history: deque = deque(maxlen=100)
        
        # Streak tracking
        self.consecutive_losses = 0
        self.consecutive_wins = 0
        self.last_trade_time: Optional[datetime] = None
        self.streak_pause_until: Optional[datetime] = None
        
        # Performance tracking
        self.trade_history: deque = deque(maxlen=500)
        self.pnl_history: deque = deque(maxlen=100)
        
    def compute_kelly_fraction(
        self,
        win_probability: float,
        win_loss_ratio: float,
        alpha: float = KELLY_ALPHA
    ) -> float:
        """
        Compute Kelly Criterion fraction.
        
        Kelly % = W - (1-W)/R
        Where:
            W = win probability
            R = win/loss ratio
            
        Adjusted by alpha for fractional Kelly.
        
        Args:
            win_probability: Estimated win probability (0-1)
            win_loss_ratio: Ratio of average win to average loss
            alpha: Fractional Kelly multiplier
            
        Returns:
            Kelly fraction (0-1)
        """
        if win_probability <= 0 or win_loss_ratio <= 0:
            return 0.0
            
        # Kelly formula
        kelly = win_probability - (1 - win_probability) / win_loss_ratio
        
        # Apply fractional Kelly
        kelly = kelly * alpha
        
        # Clamp to valid range
        kelly = max(0.0, min(kelly, KELLY_HARD_CAP))
        
        return kelly
    
    def compute_bet_size(
        self,
        kelly_fraction: float,
        bankroll: float,
        edge: float
    ) -> float:
        """
        Compute bet size from Kelly fraction.
        
        Args:
            kelly_fraction: Kelly fraction (0-1)
            bankroll: Current bankroll
            edge: Signal edge
            
        Returns:
            Bet size in dollars
        """
        # Base size from Kelly
        base_size = bankroll * kelly_fraction
        
        # Scale by edge confidence
        # Higher edge = more confident = larger size
        edge_multiplier = min(abs(edge) / 0.1, 2.0)  # Cap at 2x
        
        size = base_size * edge_multiplier
        
        # Apply hard cap
        max_size = bankroll * KELLY_HARD_CAP
        size = min(size, max_size)
        
        # Apply minimum
        min_size = bankroll * KELLY_MINIMUM
        size = max(size, min_size)
        
        return size
    
    def check_max_single_bet(self, bet_size: float, bankroll: float) -> Tuple[bool, str]:
        """
        Check if single bet exceeds maximum.
        
        Args:
            bet_size: Proposed bet size
            bankroll: Current bankroll
            
        Returns:
            Tuple of (passes, reason)
        """
        max_bet = bankroll * KELLY_HARD_CAP
        
        if bet_size > max_bet:
            return False, f"Bet {bet_size:.2f} exceeds max {max_bet:.2f}"
        
        return True, "Bet size acceptable"
    
    def check_daily_var(
        self,
        pnl_history: deque,
        threshold: float = DAILY_VAR_LIMIT
    ) -> Tuple[bool, str]:
        """
        Check daily Value at Risk limit.
        
        Args:
            pnl_history: Today's PnL history
            threshold: Maximum allowed loss (negative)
            
        Returns:
            Tuple of (passes, reason)
        """
        if not pnl_history:
            return True, "No trades today"
        
        # Calculate current daily PnL
        daily_pnl = sum(pnl_history)
        
        if daily_pnl < self.initial_bankroll * threshold:
            return False, f"Daily loss {daily_pnl:.2f} exceeds VaR {threshold:.2%}"
        
        return True, "Daily VaR OK"
    
    def check_max_drawdown(
        self,
        peak: float,
        current: float,
        limit: float = MAX_DRAWDOWN_LIMIT
    ) -> Tuple[bool, str]:
        """
        Check maximum drawdown limit.
        
        Args:
            peak: Peak bankroll
            current: Current bankroll
            limit: Maximum drawdown (negative)
            
        Returns:
            Tuple of (passes, reason)
        """
        if peak == 0:
            return True, "No peak established"
        
        drawdown = (peak - current) / peak
        
        if drawdown > abs(limit):
            return False, f"Drawdown {drawdown:.2%} exceeds limit {limit:.2%}"
        
        return True, "Drawdown OK"
    
    def check_exposure_ratio(
        self,
        open_positions: Dict[str, Position],
        bankroll: float,
        market_id: Optional[str] = None,
        single_limit: float = SINGLE_MARKET_EXPOSURE,
        total_limit: float = TOTAL_EXPOSURE_LIMIT
    ) -> Tuple[bool, str]:
        """
        Check exposure limits.
        
        Args:
            open_positions: Current open positions
            bankroll: Current bankroll
            market_id: Optional market to check single exposure
            single_limit: Max exposure per market
            total_limit: Max total exposure
            
        Returns:
            Tuple of (passes, reason)
        """
        if not open_positions:
            return True, "No positions"
        
        # Total exposure
        total_exposure = sum(p.size for p in open_positions.values())
        total_ratio = total_exposure / bankroll if bankroll > 0 else 0
        
        if total_ratio > total_limit:
            return False, f"Total exposure {total_ratio:.2%} exceeds {total_limit:.2%}"
        
        # Single market exposure
        if market_id and market_id in open_positions:
            market_exposure = open_positions[market_id].size
            market_ratio = market_exposure / bankroll if bankroll > 0 else 0
            
            if market_ratio > single_limit:
                return False, f"Market exposure {market_ratio:.2%} exceeds {single_limit:.2%}"
        
        return True, "Exposure OK"
    
    def check_correlation_block(
        self,
        positions: Dict[str, Position],
        max_correlation: float = MAX_POSITION_CORRELATION
    ) -> Tuple[bool, str]:
        """
        Check correlation between positions.
        
        Args:
            positions: Open positions
            max_correlation: Maximum allowed correlation
            
        Returns:
            Tuple of (passes, reason)
        """
        # Simplified: In production, would compute actual correlations
        # For now, just limit concurrent positions
        if len(positions) >= MAX_CONCURRENT_POSITIONS:
            return False, f"Max positions {MAX_CONCURRENT_POSITIONS} reached"
        
        return True, "Correlation OK"
    
    def check_streak_block(
        self,
        direction: Direction,
        loss_streak_limit: int = LOSS_STREAK_HALT,
        pause_minutes: int = LOSS_STREAK_PAUSE_MINUTES
    ) -> Tuple[bool, str]:
        """
        Check loss/winstreak limits.
        
        Args:
            direction: Direction of proposed trade
            loss_streak_limit: Consecutive losses before pause
            pause_minutes: Minutes to pause after streak
            
        Returns:
            Tuple of (passes, reason)
        """
        now = datetime.utcnow()
        
        # Check if in pause period
        if self.streak_pause_until and now < self.streak_pause_until:
            remaining = (self.streak_pause_until - now).total_seconds() / 60
            return False, f"In streak pause: {remaining:.0f}min remaining"
        
        # Check loss streak
        if self.consecutive_losses >= loss_streak_limit:
            self.streak_pause_until = now + timedelta(minutes=pause_minutes)
            return False, f"Loss streak {self.consecutive_losses} >= {loss_streak_limit}"
        
        return True, "Streak OK"
    
    def update_streak(self, trade: Trade) -> None:
        """Update win/loss streak counters."""
        if trade.pnl > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        
        self.last_trade_time = datetime.utcnow()
    
    def calculate_win_loss_ratio(self) -> float:
        """Calculate historical win/loss ratio."""
        if not self.trade_history:
            return 1.0
        
        wins = [t.pnl for t in self.trade_history if t.pnl > 0]
        losses = [abs(t.pnl) for t in self.trade_history if t.pnl < 0]
        
        if not wins or not losses:
            return 1.0
        
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        
        return avg_win / avg_loss if avg_loss > 0 else 1.0
    
    def estimate_win_probability(self) -> float:
        """Estimate win probability from historical performance."""
        if not self.trade_history:
            return WIN_RATE_TARGET  # Use target as prior
        
        wins = sum(1 for t in self.trade_history if t.pnl > 0)
        total = len(self.trade_history)
        
        # Bayesian update with prior
        alpha = 1.0  # Prior strength
        beta = 1.0
        
        posterior_wins = wins + alpha
        posterior_total = total + alpha + beta
        
        return posterior_wins / posterior_total
    
    def run_risk_checks(
        self,
        signal: Signal,
        bet_size: float,
        portfolio: Portfolio
    ) -> RiskCheckResult:
        """
        Run all risk checks on proposed trade.
        
        Args:
            signal: Signal to check
            bet_size: Proposed bet size
            portfolio: Current portfolio state
            
        Returns:
            RiskCheckResult with pass/fail and details
        """
        details = {}
        
        # 1. Check streak block
        streak_pass, streak_reason = self.check_streak_block(signal.direction)
        details['streak'] = streak_reason
        if not streak_pass:
            return RiskCheckResult(passes=False, reason=streak_reason, details=details)
        
        # 2. Check single bet size
        bet_pass, bet_reason = self.check_max_single_bet(bet_size, portfolio.bankroll)
        details['bet_size'] = bet_reason
        if not bet_pass:
            return RiskCheckResult(passes=False, reason=bet_reason, details=details)
        
        # 3. Check daily VaR
        var_pass, var_reason = self.check_daily_var(
            self.pnl_history,
            DAILY_VAR_LIMIT
        )
        details['daily_var'] = var_reason
        if not var_pass:
            return RiskCheckResult(passes=False, reason=var_reason, details=details)
        
        # 4. Check max drawdown
        dd_pass, dd_reason = self.check_max_drawdown(
            portfolio.peak_bankroll,
            portfolio.bankroll,
            MAX_DRAWDOWN_LIMIT
        )
        details['drawdown'] = dd_reason
        if not dd_pass:
            return RiskCheckResult(passes=False, reason=dd_reason, details=details)
        
        # 5. Check exposure
        exp_pass, exp_reason = self.check_exposure_ratio(
            self.positions,
            portfolio.bankroll,
            signal.market_id
        )
        details['exposure'] = exp_reason
        if not exp_pass:
            return RiskCheckResult(passes=False, reason=exp_reason, details=details)
        
        # 6. Check correlation/position limit
        corr_pass, corr_reason = self.check_correlation_block(self.positions)
        details['correlation'] = corr_reason
        if not corr_pass:
            return RiskCheckResult(passes=False, reason=corr_reason, details=details)
        
        return RiskCheckResult(
            passes=True,
            reason="All risk checks passed",
            details=details
        )
    
    def calculate_position_size(
        self,
        signal: Signal,
        portfolio: Portfolio,
        regime_alpha: float = 1.0
    ) -> Tuple[float, str]:
        """
        Calculate position size using Kelly criterion.
        
        Args:
            signal: Signal to size
            portfolio: Current portfolio
            regime_alpha: Regime adjustment to Kelly alpha
            
        Returns:
            Tuple of (position_size, reason)
        """
        # Estimate win probability
        win_prob = self.estimate_win_probability()
        
        # Adjust probability by signal edge direction
        if signal.edge > 0:
            # UP signal - slightly higher win prob
            adj_prob = win_prob + abs(signal.edge) * 0.5
        else:
            # DOWN signal
            adj_prob = win_prob - abs(signal.edge) * 0.5
        
        adj_prob = max(0.1, min(0.9, adj_prob))  # Clamp
        
        # Get win/loss ratio
        win_loss_ratio = self.calculate_win_loss_ratio()
        
        # Compute Kelly
        kelly = self.compute_kelly_fraction(
            adj_prob,
            win_loss_ratio,
            KELLY_ALPHA * regime_alpha
        )
        
        if kelly < KELLY_MINIMUM:
            return 0.0, f"Kelly too small: {kelly:.4f}"
        
        # Calculate bet size
        bet_size = self.compute_bet_size(kelly, portfolio.bankroll, signal.edge)
        
        return bet_size, f"Kelly: {kelly:.4f}, Size: {bet_size:.2f}"
    
    def record_trade(self, trade: Trade) -> None:
        """Record completed trade for tracking."""
        self.trade_history.append(trade)
        self.pnl_history.append(trade.pnl)
        self.position_history.append(trade)
        
        # Update streak
        self.update_streak(trade)
        
        # Update bankroll
        self.bankroll += trade.pnl
        
        # Reset daily if new day
        if self.last_trade_time:
            hours_since = (datetime.utcnow() - self.last_trade_time).total_seconds() / 3600
            if hours_since > 24:
                self.daily_pnl = 0.0
                self.daily_start = self.bankroll
    
    def get_stats(self) -> Dict:
        """Get risk manager statistics."""
        return {
            'bankroll': self.bankroll,
            'daily_pnl': self.daily_pnl,
            'consecutive_losses': self.consecutive_losses,
            'consecutive_wins': self.consecutive_wins,
            'num_positions': len(self.positions),
            'total_exposure': sum(p.size for p in self.positions.values()),
            'win_rate': self.estimate_win_probability(),
            'win_loss_ratio': self.calculate_win_loss_ratio(),
        }
    
    def reset_daily(self) -> None:
        """Reset daily tracking."""
        self.daily_pnl = 0.0
        self.daily_start = self.bankroll
        self.pnl_history.clear()


# Standalone functions for simpler use cases

def compute_kelly_fraction(p: float, b: float, alpha: float = 0.25) -> float:
    """
    Compute Kelly fraction.
    
    Args:
        p: Win probability
        b: Win/loss ratio
        alpha: Fractional Kelly multiplier
        
    Returns:
        Kelly fraction
    """
    if p <= 0 or b <= 0:
        return 0.0
    
    kelly = p - (1 - p) / b
    kelly = kelly * alpha
    kelly = max(0.0, min(kelly, KELLY_HARD_CAP))
    
    return kelly


def compute_bet_size(kelly_f: float, bankroll: float, hard_cap: float = 0.02) -> float:
    """
    Compute bet size from Kelly fraction.
    
    Args:
        kelly_f: Kelly fraction
        bankroll: Current bankroll
        hard_cap: Maximum position size
        
    Returns:
        Bet size
    """
    size = bankroll * kelly_f
    size = min(size, bankroll * hard_cap)
    size = max(size, bankroll * KELLY_MINIMUM)
    
    return size


def check_max_single_bet(bet: float, bankroll: float) -> Tuple[bool, str]:
    """Check max single bet."""
    max_bet = bankroll * KELLY_HARD_CAP
    if bet > max_bet:
        return False, f"Exceeds max {max_bet:.2f}"
    return True, "OK"


def check_daily_var(pnl_history: List[float], threshold: float = -0.05) -> Tuple[bool, str]:
    """Check daily VaR."""
    if not pnl_history:
        return True, "No trades"
    
    daily_pnl = sum(pnl_history)
    if daily_pnl < threshold:
        return False, f"Loss {daily_pnl:.2f} exceeds {threshold:.2%}"
    return True, "OK"


def check_max_drawdown(peak: float, trough: float, limit: float = -0.08) -> Tuple[bool, str]:
    """Check max drawdown."""
    if peak == 0:
        return True, "No peak"
    
    dd = (peak - trough) / peak
    if dd > abs(limit):
        return False, f"DD {dd:.2%} exceeds {limit:.2%}"
    return True, "OK"


def check_exposure_ratio(open_bets: float, bankroll: float, limit: float = 0.50) -> Tuple[bool, str]:
    """Check exposure ratio."""
    if bankroll == 0:
        return False, "Zero bankroll"
    
    ratio = open_bets / bankroll
    if ratio > limit:
        return False, f"Exposure {ratio:.2%} exceeds {limit:.2%}"
    return True, "OK"


def check_correlation_block(markets: List[str], max_positions: int = 5) -> Tuple[bool, str]:
    """Check correlation/position limit."""
    if len(markets) >= max_positions:
        return False, f"Max {max_positions} positions"
    return True, "OK"


def check_streak_block(
    direction_losses: int,
    direction: str = "SELL",
    loss_limit: int = 4,
    pause_minutes: int = 15
) -> Tuple[bool, str]:
    """Check streak block."""
    if direction_losses >= loss_limit:
        return False, f"Loss streak {direction_losses}"
    return True, "OK"
