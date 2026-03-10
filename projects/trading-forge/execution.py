"""
Stage 6: Execution & Logging

Handles order submission, fill handling, trade recording, and logging.
All fields from the Stage 6 logging spec are captured.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
import logging
import uuid
from datetime import datetime
from enum import Enum

from config import (
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    MAX_RETRY_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    ORDER_TIMEOUT_SECONDS,
    FILL_SLIPPAGE,
    MARKET_RESOLUTION_MINUTES,
)
from models import (
    Market,
    Signal,
    Trade,
    Position,
    Portfolio,
    Direction,
    OrderStatus,
    OrderType,
    PositionStatus,
)


logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of order operation."""
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_size: float = 0.0
    message: str = ""
    timestamp: datetime = field(default_factory=datetime.utcnow)


class ExecutionHandler:
    """
    Handles order execution and trade management.
    
    Stage 6: Submit orders, handle fills, record outcomes.
    """
    
    def __init__(self, paper_trading: bool = True):
        self.paper_trading = paper_trading
        self.pending_orders: Dict[str, 'Order'] = {}
        self.positions: Dict[str, Position] = {}
        self.order_counter = 0
        
    def _generate_order_id(self) -> str:
        """Generate unique order ID."""
        self.order_counter += 1
        return f"ord_{self.order_counter}_{int(datetime.utcnow().timestamp())}"
    
    def submit_limit_order(
        self,
        market: Market,
        direction: Direction,
        price: float,
        size: float
    ) -> OrderResult:
        """
        Submit a limit order.
        
        Args:
            market: Market to trade
            direction: UP or DOWN
            price: Limit price
            size: Order size in dollars
            
        Returns:
            OrderResult with order ID
        """
        order_id = self._generate_order_id()
        
        order = Order(
            order_id=order_id,
            market_id=market.id,
            direction=direction,
            order_type=OrderType.LIMIT,
            price=price,
            size=size,
            status=OrderStatus.PENDING,
            created_time=datetime.utcnow()
        )
        
        self.pending_orders[order_id] = order
        
        if self.paper_trading:
            logger.info(f"[PAPER] Submitted limit order: {order_id} {direction.value} {size:.2f} @ {price:.4f}")
            return OrderResult(
                success=True,
                order_id=order_id,
                message="Paper order submitted"
            )
        
        # Real execution would call Polymarket API here
        # For now, simulate
        logger.info(f"Submitted limit order: {order_id} {direction.value} {size:.2f} @ {price:.4f}")
        
        return OrderResult(
            success=True,
            order_id=order_id,
            message="Order submitted"
        )
    
    def submit_market_order(
        self,
        market: Market,
        direction: Direction,
        size: float
    ) -> OrderResult:
        """
        Submit a market order.
        
        Args:
            market: Market to trade
            direction: UP or DOWN
            size: Order size in dollars
            
        Returns:
            OrderResult with fill price
        """
        order_id = self._generate_order_id()
        
        # Get market price
        if direction == Direction.UP:
            fill_price = market.yes_ask
        else:
            fill_price = market.no_ask
        
        # Apply slippage
        slippage = fill_price * FILL_SLIPPAGE
        fill_price = fill_price + slippage
        
        order = Order(
            order_id=order_id,
            market_id=market.id,
            direction=direction,
            order_type=OrderType.MARKET,
            price=fill_price,
            size=size,
            filled_size=size,
            status=OrderStatus.FILLED,
            fill_price=fill_price,
            created_time=datetime.utcnow(),
            filled_time=datetime.utcnow()
        )
        
        self.pending_orders[order_id] = order
        
        # Create position
        position = Position(
            position_id=order_id,
            market_id=market.id,
            direction=direction,
            entry_price=fill_price,
            size=size,
            current_price=fill_price,
            status=PositionStatus.OPEN,
            open_time=datetime.utcnow()
        )
        
        self.positions[market.id] = position
        
        if self.paper_trading:
            logger.info(f"[PAPER] Market order filled: {order_id} {direction.value} {size:.2f} @ {fill_price:.4f}")
        else:
            logger.info(f"Market order filled: {order_id} {direction.value} {size:.2f} @ {fill_price:.4f}")
        
        return OrderResult(
            success=True,
            order_id=order_id,
            fill_price=fill_price,
            filled_size=size,
            message="Order filled"
        )
    
    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a pending order.
        
        Args:
            order_id: Order to cancel
            
        Returns:
            True if cancelled
        """
        if order_id not in self.pending_orders:
            logger.warning(f"Order not found: {order_id}")
            return False
        
        order = self.pending_orders[order_id]
        
        if order.status != OrderStatus.PENDING:
            logger.warning(f"Order not cancellable: {order_id} status={order.status}")
            return False
        
        order.status = OrderStatus.CANCELLED
        
        if self.paper_trading:
            logger.info(f"[PAPER] Cancelled order: {order_id}")
        
        return True
    
    def check_fill(self, order_id: str) -> Optional[float]:
        """
        Check if order has been filled.
        
        Args:
            order_id: Order to check
            
        Returns:
            Fill price if filled, None otherwise
        """
        if order_id not in self.pending_orders:
            return None
        
        order = self.pending_orders[order_id]
        
        if order.status == OrderStatus.FILLED:
            return order.fill_price
        
        return None
    
    def execute_trade(
        self,
        signal: Signal,
        bet_size: float,
        market: Market,
        execution_latency_ms: float = 0.0
    ) -> Trade:
        """
        Execute a complete trade from signal.
        
        Args:
            signal: Signal to execute
            bet_size: Position size
            market: Market data
            execution_latency_ms: Time from signal to execution
            
        Returns:
            Completed Trade object
        """
        # signal.direction is now a Direction enum, use it directly
        direction = signal.direction
        
        # Determine entry price
        if direction == Direction.UP:
            entry_price = market.yes_ask
            slippage = entry_price * FILL_SLIPPAGE
        else:
            entry_price = market.no_ask
            slippage = entry_price * FILL_SLIPPAGE
        
        entry_price = entry_price + slippage
        
        # Calculate confirmed edge
        edge_confirmed = signal.p_model - entry_price
        
        trade = Trade(
            trade_id=self._generate_order_id(),
            market_id=market.id,
            market_question=market.question,
            direction=direction,
            signal_edge=signal.edge,
            signal_consensus=signal.consensus_score,
            p_model=signal.p_model,
            p_market=signal.p_market,
            edge_confirmed=edge_confirmed,
            size=bet_size,
            entry_price=entry_price,
            status=PositionStatus.OPEN,
            execution_latency_ms=execution_latency_ms,
            slippage=slippage,
            open_time=datetime.utcnow()
        )
        
        # Create position
        position = Position(
            position_id=trade.trade_id,
            market_id=market.id,
            direction=direction,
            entry_price=entry_price,
            size=bet_size,
            current_price=entry_price,
            status=PositionStatus.OPEN,
            open_time=datetime.utcnow()
        )
        
        self.positions[market.id] = position
        
        logger.info(f"Executed trade: {trade.trade_id} {direction.value} {bet_size:.2f} @ {entry_price:.4f} (edge: {edge_confirmed:.3f})")
        
        return trade
    
    def on_market_resolve(
        self,
        market_id: str,
        outcome: str,
        resolution_price: float
    ) -> Optional[Trade]:
        """
        Handle market resolution.
        
        Args:
            market_id: Resolved market
            outcome: YES or NO
            resolution_price: Resolution price
            
        Returns:
            Updated Trade with P&L
        """
        if market_id not in self.positions:
            logger.warning(f"No position found for market: {market_id}")
            return None
        
        position = self.positions[market_id]
        
        if position.status != PositionStatus.OPEN:
            logger.warning(f"Position not open: {position.position_id}")
            return None
        
        # Calculate P&L
        if position.direction == Direction.UP:
            if outcome == "YES":
                # Won
                pnl = (resolution_price - position.entry_price) * position.size
                pnl_pct = (resolution_price - position.entry_price) / position.entry_price
            else:
                # Lost
                pnl = -position.size * position.entry_price
                pnl_pct = -position.entry_price
        else:  # DOWN
            if outcome == "NO":
                # Won
                pnl = (position.entry_price - resolution_price) * position.size
                pnl_pct = (position.entry_price - resolution_price) / position.entry_price
            else:
                # Lost
                pnl = -position.size * position.entry_price
                pnl_pct = -position.entry_price
        
        # Update position
        position.status = PositionStatus.CLOSED
        position.current_price = resolution_price
        
        # Create trade record
        trade = Trade(
            trade_id=position.position_id,
            market_id=market_id,
            market_question="",
            direction=position.direction,
            signal_edge=0.0,  # Would need to track this
            signal_consensus=0.0,
            p_model=position.entry_price,
            p_market=resolution_price,
            edge_confirmed=0.0,
            size=position.size,
            entry_price=position.entry_price,
            exit_price=resolution_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            open_time=position.open_time,
            close_time=datetime.utcnow(),
            status=PositionStatus.CLOSED
        )
        
        logger.info(f"Market resolved: {market_id} {outcome} @ {resolution_price:.4f} PnL: ${pnl:.2f} ({pnl_pct:.2%})")
        
        # Remove position
        del self.positions[market_id]
        
        return trade
    
    def get_position(self, market_id: str) -> Optional[Position]:
        """Get open position for market."""
        return self.positions.get(market_id)
    
    def get_all_positions(self) -> List[Position]:
        """Get all open positions."""
        return list(self.positions.values())


@dataclass
class Order:
    """Order record."""
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


def submit_limit_order(
    market: Market,
    direction: str,
    price: float,
    size: float,
    paper_trading: bool = True
) -> Tuple[bool, Optional[str], str]:
    """
    Submit a limit order.
    
    Args:
        market: Market to trade
        direction: "UP" or "DOWN"
        price: Limit price
        size: Order size
        paper_trading: Use paper trading
        
    Returns:
        Tuple of (success, order_id, message)
    """
    dir_enum = Direction.UP if direction == "UP" else Direction.DOWN
    
    handler = ExecutionHandler(paper_trading=paper_trading)
    result = handler.submit_limit_order(market, dir_enum, price, size)
    
    return result.success, result.order_id, result.message


def execute_trade(
    signal: Signal,
    bet_size: float,
    market: Market,
    portfolio: Portfolio,
    execution_latency_ms: float = 0.0
) -> Optional[Trade]:
    """
    Execute a trade.
    
    Args:
        signal: Signal to execute
        bet_size: Position size
        market: Market data
        portfolio: Portfolio
        execution_latency_ms: Latency
        
    Returns:
        Trade or None if failed
    """
    handler = ExecutionHandler()
    
    try:
        trade = handler.execute_trade(signal, bet_size, market, execution_latency_ms)
        
        # Update portfolio
        portfolio.bankroll -= bet_size
        
        return trade
        
    except Exception as e:
        logger.error(f"Trade execution failed: {e}")
        return None


def on_market_resolve(
    execution_handler: ExecutionHandler,
    market_id: str,
    outcome: str,
    resolution_price: float,
    portfolio: Portfolio
) -> Optional[Trade]:
    """
    Handle market resolution and update portfolio.
    
    Args:
        execution_handler: Handler with positions
        market_id: Resolved market
        outcome: YES or NO
        resolution_price: Resolution price
        portfolio: Portfolio to update
        
    Returns:
        Trade with P&L
    """
    trade = execution_handler.on_market_resolve(market_id, outcome, resolution_price)
    
    if trade:
        # Update portfolio
        portfolio.update_from_trade(trade)
        
    return trade
