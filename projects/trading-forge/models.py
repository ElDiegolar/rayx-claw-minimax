"""
Trading Forge Data Models

Contains all data classes and type definitions for the trading bot.
Uses dataclasses for clean, type-hinted data structures.

Author: Trading Forge Team
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(str, Enum):
    """Trade direction enumeration."""
    UP = "UP"
    DOWN = "DOWN"


class PositionStatus(str, Enum):
    """Position status enumeration."""
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"


class OrderStatus(str, Enum):
    """Order status enumeration."""
    PENDING = "PENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class OrderType(str, Enum):
    """Order type enumeration."""
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class MarketResolution(str, Enum):
    """Market resolution outcome."""
    YES = "YES"
    NO = "NO"
    UNRESOLVED = "UNRESOLVED"


class SignalStage(str, Enum):
    """Pipeline stage where signal was dropped."""
    SIGNAL_GENERATION = "SIGNAL_GENERATION"
    EDGE_CALCULATION = "EDGE_CALCULATION"
    TOXICITY_FILTERS = "TOXICITY_FILTERS"
    RISK_MANAGEMENT = "RISK_MANAGEMENT"
    EXECUTION = "EXECUTION"


class DropReason(str, Enum):
    """Reason for dropping a signal."""
    CONSENSUS_FAILED = "CONSENSUS_FAILED"
    EDGE_INSUFFICIENT = "EDGE_INSUFFICIENT"
    Z_SCORE_FAILED = "Z_SCORE_FAILED"
    VPIN_TOXIC = "VPIN_TOXIC"
    SPREAD_TOXIC = "SPREAD_TOXIC"
    VOL_SPIKE = "VOL_SPIKE"
    MOMENTUM_EXHAUSTION = "MOMENTUM_EXHAUSTION"
    MACRO_EVENT = "MACRO_EVENT"
    RISK_LIMIT_HIT = "RISK_LIMIT_HIT"
    KELLY_TOO_SMALL = "KELLY_TOO_SMALL"
    EXPOSURE_EXCEEDED = "EXPOSURE_EXCEEDED"
    STREAK_BLOCK = "STREAK_BLOCK"
    DAILY_VAR_HIT = "DAILY_VAR_HIT"
    MAX_DRAWDOWN_HIT = "MAX_DRAWDOWN_HIT"
    CORRELATION_BLOCK = "CORRELATION_BLOCK"
    ORDER_REJECTED = "ORDER_REJECTED"
    TIMEOUT = "TIMEOUT"


@dataclass
class Market:
    """
    Represents a Polymarket binary market.
    
    Attributes:
        id: Unique market identifier
        question: Market question text
        resolution_time: When the market resolves
        current_price: Current YES price (0-1)
        volume: Total trading volume
        yes_bid for YES
       : Best bid yes_ask: Best ask for YES
        no_bid: Best bid for NO
        no_ask: Best ask for NO
    """
    id: str
    question: str
    resolution_time: datetime
    current_price: float
    volume: float
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    
    @property
    def no_price(self) -> float:
        """Returns the current NO price (1 - YES price)."""
        return 1.0 - self.current_price
    
    @property
    def spread(self) -> float:
        """Returns the bid-ask spread."""
        return self.yes_ask - self.yes_bid
    
    @property
    def effective_spread(self) -> float:
        """Returns the effective spread as percentage of mid-price."""
        mid = (self.yes_bid + self.yes_ask) / 2
        if mid == 0:
            return 0.0
        return (self.yes_ask - self.yes_bid) / mid
    
    @property
    def liquidity(self) -> float:
        """Returns total liquidity (sum of yes and no volume)."""
        return self.volume * 2


@dataclass
class MarketData:
    """
    Complete market data snapshot for a single market.
    
    Combines price, order book, and derived data.
    """
    market: Market
    btc_price: float
    btc_price_5min_ago: float
    btc_price_history: list[float]
    cvd: float
    cvd_5min_ago: float
    cvd_history: list[float]
    funding_rate: float
    funding_history: list[float]
    oi: float
    oi_5min_ago: float
    oi_history: list[float]
    call_iv: float
    put_iv: float
    iv_history: list[float]
    volume_1min: float
    volume_avg: float
    timestamp: datetime
    
    @property
    def momentum(self) -> float:
        """Returns the 5-minute price momentum."""
        if len(self.btc_price_history) < 2:
            return 0.0
        return (self.btc_price - self.btc_price_history[-2]) / self.btc_price_history[-2]
    
    @property
    def momentum_5min(self) -> float:
        """Returns the 5-minute price change."""
        if not self.btc_price_5min_ago:
            return 0.0
        return (self.btc_price - self.btc_price_5min_ago) / self.btc_price_5min_ago
    
    @property
    def oi_delta_pct(self) -> float:
        """Returns the percentage change in OI."""
        if not self.oi_5min_ago:
            return 0.0
        return (self.oi - self.oi_5min_ago) / self.oi_5min_ago
    
    @property
    def skew(self) -> float:
        """Returns the IV skew (call IV - put IV)."""
        return self.call_iv - self.put_iv
    
    @property
    def vol_ratio(self) -> float:
        """Returns the volume spike ratio."""
        if not self.volume_avg:
            return 0.0
        return self.volume_1min / self.volume_avg


@dataclass
class Signal:
    """
    Stage 1 output: Directional signal with all components.
    
    Attributes:
        market_id: Associated market
        direction: UP or DOWN
        p_model: Model probability estimate
        p_market: Market-implied probability
        edge: Expected edge (p_model - p_market)
        cvd_signal: CVD component signal
        momentum_signal: Momentum component signal
        funding_signal: Funding component signal
        oi_signal: OI delta component signal
        skew_signal: Options skew component signal
        consensus_score: Weighted consensus score C
    """
    market_id: str
    direction: Direction
    p_model: float
    p_market: float
    edge: float
    cvd_signal: float
    momentum_signal: float
    funding_signal: float
    oi_signal: float
    skew_signal: float
    consensus_score: float
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    @property
    def is_up(self) -> bool:
        """Returns True if signal is for UP direction."""
        return self.direction == Direction.UP
    
    @property
    def is_down(self) -> bool:
        """Returns True if signal is for DOWN direction."""
        return self.direction == Direction.DOWN
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "market_id": self.market_id,
            "direction": self.direction.value,
            "p_model": self.p_model,
            "p_market": self.p_market,
            "edge": self.edge,
            "cvd_signal": self.cvd_signal,
            "momentum_signal": self.momentum_signal,
            "funding_signal": self.funding_signal,
            "oi_signal": self.oi_signal,
            "skew_signal": self.skew_signal,
            "consensus_score": self.consensus_score,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class Order:
    """
    Represents a trading order.
    
    Attributes:
        order_id: Unique order identifier
        market_id: Associated market
        direction: UP or DOWN
        order_type: LIMIT or MARKET
        price: Limit price (None for market orders)
        size: Order size in dollars
        filled_size: Amount filled so far
        status: Current order status
        created_time: When order was created
        filled_time: When order was filled (None if not filled)
        fill_price: Fill price if filled
    """
    order_id: str
    market_id: str
    direction: Direction
    order_type: OrderType
    price: Optional[float]
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_time: datetime = field(default_factory=datetime.utcnow)
    filled_time: Optional[datetime] = None
    fill_price: Optional[float] = None
    
    @property
    def remaining_size(self) -> float:
        """Returns the remaining unfilled size."""
        return self.size - self.filled_size
    
    @property
    def is_filled(self) -> bool:
        """Returns True if order is fully filled."""
        return self.status == OrderStatus.FILLED
    
    @property
    def is_active(self) -> bool:
        """Returns True if order is still active (pending or partial)."""
        return self.status in [OrderStatus.PENDING, OrderStatus.PARTIAL]


@dataclass
class Trade:
    """
    Stage 6 output: Complete trade record.
    
    This is the primary logging entity for all trades.
    
    Attributes:
        trade_id: Unique trade identifier
        market_id: Associated market
        market_question: Market question
        direction: UP or DOWN
        signal_edge: Signal edge from Stage 1
        signal_consensus: Signal consensus score
        p_model: Model probability
        p_market: Market probability
        edge_confirmed: Confirmed edge after execution
        size: Trade size in dollars
        entry_price: Fill price
        exit_price: Exit price (None if still open)
        pnl: Profit/Loss in dollars
        pnl_pct: Profit/Loss as percentage
        open_time: When trade was opened
        close_time: When trade was closed
        status: Position status
        execution_latency_ms: Time from signal to execution
        slippage: Slippage in dollars
    """
    trade_id: str
    market_id: str
    market_question: str
    direction: Direction
    signal_edge: float
    signal_consensus: float
    p_model: float
    p_market: float
    edge_confirmed: float
    size: float
    entry_price: float
    exit_price: Optional[float] = None
    pnl: float = 0.0
    pnl_pct: float = 0.0
    open_time: datetime = field(default_factory=datetime.utcnow)
    close_time: Optional[datetime] = None
    status: PositionStatus = PositionStatus.PENDING
    execution_latency_ms: float = 0.0
    slippage: float = 0.0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "trade_id": self.trade_id,
            "market_id": self.market_id,
            "market_question": self.market_question,
            "direction": self.direction.value,
            "signal_edge": self.signal_edge,
            "signal_consensus": self.signal_consensus,
            "p_model": self.p_model,
            "p_market": self.p_market,
            "edge_confirmed": self.edge_confirmed,
            "size": self.size,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl": self.pnl,
            "pnl_pct": self.pnl_pct,
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat() if self.close_time else None,
            "status": self.status.value,
            "execution_latency_ms": self.execution_latency_ms,
            "slippage": self.slippage,
        }


@dataclass
class Position:
    """
    Represents an open position in a market.
    
    Attributes:
        position_id: Unique position identifier
        market_id: Associated market
        direction: UP or DOWN
        entry_price: Entry price
        size: Position size in dollars
        current_price: Current market price
        unrealized_pnl: Unrealized profit/loss
        open_time: When position was opened
        status: Position status
    """
    position_id: str
    market_id: str
    direction: Direction
    entry_price: float
    size: float
    current_price: float
    unrealized_pnl: float = 0.0
    open_time: datetime = field(default_factory=datetime.utcnow)
    status: PositionStatus = PositionStatus.OPEN
    
    def update_price(self, new_price: float) -> None:
        """Update current price and recalculate unrealized PnL."""
        self.current_price = new_price
        if self.direction == Direction.UP:
            self.unrealized_pnl = (new_price - self.entry_price) * self.size
        else:
            self.unrealized_pnl = (self.entry_price - new_price) * self.size
    
    def to_trade(self, exit_price: float, close_time: datetime) -> Trade:
        """Convert position to completed trade."""
        if self.direction == Direction.UP:
            pnl = (exit_price - self.entry_price) * self.size
        else:
            pnl = (self.entry_price - exit_price) * self.size
        
        pnl_pct = pnl / self.size if self.size > 0 else 0.0
        
        return Trade(
            trade_id=self.position_id,
            market_id=self.market_id,
            market_question="",  # Will be filled by caller
            direction=self.direction,
            signal_edge=0.0,  # Will be filled by caller
            signal_consensus=0.0,  # Will be filled by caller
            p_model=self.entry_price,
            p_market=self.entry_price,
            edge_confirmed=0.0,
            size=self.size,
            entry_price=self.entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            open_time=self.open_time,
            close_time=close_time,
            status=PositionStatus.CLOSED,
        )


@dataclass
class Portfolio:
    """
    Portfolio state tracking.
    
    Attributes:
        bankroll: Current bankroll in dollars
        positions: Active positions
        daily_pnl: Profit/loss for current day
        total_pnl: All-time profit/loss
        drawdown: Current drawdown from peak
        peak_bankroll: Peak bankroll achieved
        start_bankroll: Starting bankroll
    """
    bankroll: float
    positions: list[Position] = field(default_factory=list)
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    drawdown: float = 0.0
    peak_bankroll: float = 0.0
    start_bankroll: float = 0.0
    
    def __post_init__(self):
        """Initialize derived fields."""
        if self.start_bankroll == 0.0:
            self.start_bankroll = self.bankroll
        if self.peak_bankroll == 0.0:
            self.peak_bankroll = self.bankroll
    
    @property
    def exposure(self) -> float:
        """Returns total exposure as fraction of bankroll."""
        total_exposure = sum(p.size for p in self.positions)
        return total_exposure / self.bankroll if self.bankroll > 0 else 0.0
    
    @property
    def num_positions(self) -> int:
        """Returns number of open positions."""
        return len([p for p in self.positions if p.status == PositionStatus.OPEN])
    
    @property
    def daily_return(self) -> float:
        """Returns daily return as percentage."""
        return self.daily_pnl / self.start_bankroll if self.start_bankroll > 0 else 0.0
    
    @property
    def total_return(self) -> float:
        """Returns total return as percentage."""
        return self.total_pnl / self.start_bankroll if self.start_bankroll > 0 else 0.0
    
    def update_peak(self) -> None:
        """Update peak bankroll if current is higher."""
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll
            self.drawdown = 0.0
        else:
            self.drawdown = (self.peak_bankroll - self.bankroll) / self.peak_bankroll
    
    def update_from_trade(self, trade: Trade) -> None:
        """Update portfolio from completed trade."""
        self.bankroll += trade.pnl
        self.daily_pnl += trade.pnl
        self.total_pnl += trade.pnl
        self.update_peak()


@dataclass
class CalibrationData:
    """Calibration data for probability estimates."""
    market_id: str
    p_model_history: list[float] = field(default_factory=list)
    p_market_history: list[float] = field(default_factory=list)
    outcomes: list[bool] = field(default_factory=list)
    brier_scores: list[float] = field(default_factory=list)
    
    def add_observation(self, p_model: float, p_market: float, outcome: bool) -> None:
        """Add a new calibration observation."""
        self.p_model_history.append(p_model)
        self.p_market_history.append(p_market)
        self.outcomes.append(outcome)
        
        # Calculate Brier score
        brier = (p_model - (1.0 if outcome else 0.0)) ** 2
        self.brier_scores.append(brier)
    
    @property
    def mean_brier(self) -> float:
        """Returns mean Brier score."""
        if not self.brier_scores:
            return 1.0
        return sum(self.brier_scores) / len(self.brier_scores)
    
    @property
    def n_observations(self) -> int:
        """Returns number of observations."""
        return len(self.outcomes)


@dataclass
class PerformanceMetrics:
    """Performance tracking metrics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    current_streak: int = 0
    max_win_streak: int = 0
    max_loss_streak: int = 0
    
    def update_from_trade(self, trade: Trade) -> None:
        """Update metrics from a completed trade."""
        self.total_trades += 1
        
        if trade.pnl > 0:
            self.winning_trades += 1
            self.current_streak = abs(self.current_streak) + 1 if self.current_streak >= 0 else 1
            self.max_win_streak = max(self.max_win_streak, self.current_streak)
            self.avg_win = (self.avg_win * (self.winning_trades - 1) + trade.pnl) / self.winning_trades
            self.largest_win = max(self.largest_win, trade.pnl)
        else:
            self.losing_trades += 1
            self.current_streak = -abs(self.current_streak) - 1 if self.current_streak <= 0 else -1
            self.max_loss_streak = min(self.max_loss_streak, self.current_streak)
            self.avg_loss = (self.avg_loss * (self.losing_trades - 1) + abs(trade.pnl)) / self.losing_trades
            self.largest_loss = min(self.largest_loss, trade.pnl)
        
        self.total_pnl += trade.pnl
        
        # Calculate derived metrics
        if self.total_trades > 0:
            self.win_rate = self.winning_trades / self.total_trades
        
        if self.avg_loss > 0:
            self.profit_factor = self.avg_win / self.avg_loss if self.avg_loss > 0 else 0.0
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "total_pnl": self.total_pnl,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "largest_win": self.largest_win,
            "largest_loss": self.largest_loss,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "current_streak": self.current_streak,
            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,
        }


@dataclass
class DroppedSignal:
    """Record of a dropped signal with reason."""
    signal: Signal
    stage: SignalStage
    reason: DropReason
    details: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for logging."""
        return {
            "market_id": self.signal.market_id,
            "direction": self.signal.direction.value,
            "p_model": self.signal.p_model,
            "p_market": self.signal.p_market,
            "edge": self.signal.edge,
            "consensus": self.signal.consensus_score,
            "stage": self.stage.value,
            "reason": self.reason.value,
            "details": self.details,
            "timestamp": self.timestamp.isoformat(),
        }
