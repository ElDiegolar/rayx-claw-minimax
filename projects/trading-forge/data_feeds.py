"""
External Data Feeds Module

Provides data feeds for:
- BTC Price (current and history)
- Order Book (bids/asks for YES and NO)
- Funding Rate
- Open Interest (OI)
- Cumulative Volume Delta (CVD)
- Options IV (implied volatility)

All feeds support get_current() and get_history() methods.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Deque
from collections import deque
import logging
import time


logger = logging.getLogger(__name__)


# Type aliases
PriceHistory = List[float]
TimestampedValue = tuple[datetime, float]


@dataclass
class Candle:
    """OHLCV candle data."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderBookLevel:
    """Single order book level."""
    price: float
    volume: float


@dataclass
class OrderBook:
    """Full order book snapshot."""
    yes_bids: List[OrderBookLevel]
    yes_asks: List[OrderBookLevel]
    no_bids: List[OrderBookLevel]
    no_asks: List[OrderBookLevel]
    timestamp: datetime
    
    @property
    def yes_bid(self) -> float:
        """Best YES bid."""
        return self.yes_bids[0].price if self.yes_bids else 0.0
    
    @property
    def yes_ask(self) -> float:
        """Best YES ask."""
        return self.yes_asks[0].price if self.yes_asks else 1.0
    
    @property
    def no_bid(self) -> float:
        """Best NO bid."""
        return self.no_bids[0].price if self.no_bids else 0.0
    
    @property
    def no_ask(self) -> float:
        """Best NO ask."""
        return self.no_asks[0].price if self.no_asks else 1.0
    
    @property
    def yes_spread(self) -> float:
        """YES spread."""
        return self.yes_ask - self.yes_bid
    
    @property
    def no_spread(self) -> float:
        """NO spread."""
        return self.no_ask - self.no_bid
    
    @property
    def yes_mid(self) -> float:
        """YES mid price."""
        return (self.yes_bid + self.yes_ask) / 2
    
    @property
    def no_mid(self) -> float:
        """NO mid price."""
        return (self.no_bid + self.no_ask) / 2


class DataFeedBase:
    """Base class for all data feeds."""
    
    def __init__(self, name: str, window_size: int = 100):
        self.name = name
        self.window_size = window_size
        self._history: Deque = deque(maxlen=window_size)
        self._last_update: Optional[datetime] = None
        
    def get_current(self) -> Optional[float]:
        """Get current value. Override in subclasses."""
        raise NotImplementedError
    
    def get_history(self) -> List:
        """Get historical values."""
        return list(self._history)
    
    def _update_history(self, value: float, timestamp: Optional[datetime] = None) -> None:
        """Update history with new value."""
        ts = timestamp or datetime.utcnow()
        self._history.append((ts, value))
        self._last_update = ts


class BTCPriceFeed(DataFeedBase):
    """
    BTC Price feed with momentum calculation.
    
    Provides current price and price history for momentum signals.
    """
    
    def __init__(self, window_size: int = 100):
        super().__init__("BTC Price", window_size)
        self._current_price: float = 0.0
        self._price_history: Deque[float] = deque(maxlen=window_size)
        
    def update(self, price: float, timestamp: Optional[datetime] = None) -> None:
        """
        Update with new price.
        
        Args:
            price: Current BTC price
            timestamp: Optional timestamp
        """
        self._current_price = price
        self._price_history.append(price)
        self._update_history(price, timestamp)
        
    def get_current(self) -> float:
        """Get current BTC price."""
        return self._current_price
    
    def get_price_history(self, n: int = 12) -> List[float]:
        """
        Get last N prices.
        
        Args:
            n: Number of recent prices
            
        Returns:
            List of recent prices (most recent last)
        """
        if len(self._price_history) < n:
            return list(self._price_history)
        return list(self._price_history)[-n:]
    
    def get_momentum(self, n: int = 5) -> float:
        """
        Calculate momentum over N periods.
        
        Args:
            n: Number of periods
            
        Returns:
            Momentum as fractional change
        """
        history = self.get_price_history(n + 1)
        if len(history) < n + 1:
            return 0.0
        
        current = history[-1]
        past = history[-(n+1)]
        
        if past == 0:
            return 0.0
            
        return (current - past) / past
    
    def get_price_at(self, minutes_ago: int) -> Optional[float]:
        """
        Get price N minutes ago.
        
        Args:
            minutes_ago: Minutes in the past
            
        Returns:
            Price at that time, or None if not available
        """
        if not self._history:
            return None
            
        target_time = datetime.utcnow() - timedelta(minutes=minutes_ago)
        
        for ts, price in reversed(self._history):
            if ts <= target_time:
                return price
                
        return None


class OrderBookFeed(DataFeedBase):
    """
    Order Book feed for YES/NO prices and depths.
    """
    
    def __init__(self, window_size: int = 50):
        super().__init__("OrderBook", window_size)
        self._current_ob: Optional[OrderBook] = None
        
    def update(self, order_book: OrderBook) -> None:
        """
        Update with new order book.
        
        Args:
            order_book: New order book snapshot
        """
        self._current_ob = order_book
        self._update_history(order_book, order_book.timestamp)
        
    def get_current(self) -> Optional[OrderBook]:
        """Get current order book."""
        return self._current_ob
    
    def get_spread(self) -> float:
        """Get current YES spread."""
        if not self._current_ob:
            return 0.0
        return self._current_ob.yes_spread
    
    def get_effective_spread(self) -> float:
        """Get effective spread as percentage."""
        if not self._current_ob:
            return 0.0
        mid = self._current_ob.yes_mid
        if mid == 0:
            return 0.0
        return self._current_ob.yes_spread / mid
    
    def get_imbalance(self) -> float:
        """
        Get order book imbalance.
        
        Returns:
            Imbalance ratio (-1 to 1)
            Positive = more bids, Negative = more asks
        """
        if not self._current_ob:
            return 0.0
        
        total_bid_vol = sum(l.volume for l in self._current_ob.yes_bids)
        total_ask_vol = sum(l.volume for l in self._current_ob.yes_asks)
        total = total_bid_vol + total_ask_vol
        
        if total == 0:
            return 0.0
            
        return (total_bid_vol - total_ask_vol) / total


class FundingFeed(DataFeedBase):
    """
    Funding rate feed.
    
    Polymarket uses a funding mechanism where long and short holders
    exchange payments. Positive funding = longs pay shorts.
    """
    
    def __init__(self, window_size: int = 20):
        super().__init__("Funding", window_size)
        self._current_funding: float = 0.0
        
    def update(self, funding_rate: float, timestamp: Optional[datetime] = None) -> None:
        """
        Update with new funding rate.
        
        Args:
            funding_rate: Current funding rate (e.g., 0.0001 for 0.01%)
            timestamp: Optional timestamp
        """
        self._current_funding = funding_rate
        self._update_history(funding_rate, timestamp)
        
    def get_current(self) -> float:
        """Get current funding rate."""
        return self._current_funding
    
    def get_funding_history(self, n: int = 4) -> List[float]:
        """Get last N funding rates."""
        history = [v for _, v in self._history]
        if len(history) < n:
            return history
        return history[-n:]
    
    def get_funding_direction(self) -> int:
        """
        Get funding direction.
        
        Returns:
            1 if positive (bulls pay), -1 if negative (bears pay), 0 if neutral
        """
        if self._current_funding > 0.0001:
            return 1
        elif self._current_funding < -0.0001:
            return -1
        return 0


class OIFeed(DataFeedBase):
    """
    Open Interest (OI) feed.
    
    OI tracks total outstanding contracts. Changes in OI indicate
    new money entering or exiting positions.
    """
    
    def __init__(self, window_size: int = 50):
        super().__init__("OI", window_size)
        self._current_oi: float = 0.0
        self._oi_history: Deque[float] = deque(maxlen=window_size)
        
    def update(self, oi: float, timestamp: Optional[datetime] = None) -> None:
        """
        Update with new OI.
        
        Args:
            oi: Current open interest
            timestamp: Optional timestamp
        """
        self._current_oi = oi
        self._oi_history.append(oi)
        self._update_history(oi, timestamp)
        
    def get_current(self) -> float:
        """Get current OI."""
        return self._current_oi
    
    def get_oi_at(self, minutes_ago: int) -> Optional[float]:
        """Get OI N minutes ago."""
        target_time = datetime.utcnow() - timedelta(minutes=minutes_ago)
        
        for ts, oi in reversed(self._history):
            if ts <= target_time:
                return oi
                
        return None
    
    def get_oi_delta(self, minutes_ago: int = 5) -> float:
        """
        Get change in OI over N minutes.
        
        Args:
            minutes_ago: Lookback period
            
        Returns:
            OI change (positive = increasing)
        """
        oi_now = self._current_oi
        oi_past = self.get_oi_at(minutes_ago)
        
        if oi_past is None or oi_past == 0:
            return 0.0
            
        return (oi_now - oi_past) / oi_past
    
    def get_oi_history(self, n: int = 6) -> List[float]:
        """Get last N OI values."""
        if len(self._oi_history) < n:
            return list(self._oi_history)
        return list(self._oi_history)[-n:]


class CVDFeed(DataFeedBase):
    """
    Cumulative Volume Delta (CVD) feed.
    
    CVD tracks net buying/selling volume over time.
    Positive CVD = more buying, Negative CVD = more selling.
    """
    
    def __init__(self, window_size: int = 100):
        super().__init__("CVD", window_size)
        self._current_cvd: float = 0.0
        self._cvd_history: Deque[float] = deque(maxlen=window_size)
        
    def update(self, cvd: float, timestamp: Optional[datetime] = None) -> None:
        """
        Update with new CVD.
        
        Args:
            cvd: Current cumulative volume delta
            timestamp: Optional timestamp
        """
        self._current_cvd = cvd
        self._cvd_history.append(cvd)
        self._update_history(cvd, timestamp)
        
    def get_current(self) -> float:
        """Get current CVD."""
        return self._current_cvd
    
    def get_cvd_at(self, minutes_ago: int) -> Optional[float]:
        """Get CVD N minutes ago."""
        target_time = datetime.utcnow() - timedelta(minutes=minutes_ago)
        
        for ts, cvd in reversed(self._history):
            if ts <= target_time:
                return cvd
                
        return None
    
    def get_cvd_delta(self, minutes_ago: int = 5) -> float:
        """
        Get change in CVD over N minutes.
        
        Args:
            minutes_ago: Lookback period
            
        Returns:
            CVD change
        """
        cvd_now = self._current_cvd
        cvd_past = self.get_cvd_at(minutes_ago)
        
        if cvd_past is None:
            return 0.0
            
        return cvd_now - cvd_past
    
    def get_cvd_history(self, n: int = 10) -> List[float]:
        """Get last N CVD values."""
        if len(self._cvd_history) < n:
            return list(self._cvd_history)
        return list(self._cvd_history)[-n:]


class OptionsFeed(DataFeedBase):
    """
    Options Implied Volatility (IV) feed.
    
    Provides call/put IV for skew calculation.
    """
    
    def __init__(self, window_size: int = 50):
        super().__init__("Options", window_size)
        self._current_call_iv: float = 0.0
        self._current_put_iv: float = 0.0
        
    def update(
        self,
        call_iv: float,
        put_iv: float,
        timestamp: Optional[datetime] = None
    ) -> None:
        """
        Update with new IV data.
        
        Args:
            call_iv: Call option IV
            put_iv: Put option IV
            timestamp: Optional timestamp
        """
        self._current_call_iv = call_iv
        self._current_put_iv = put_iv
        skew = call_iv - put_iv
        self._update_history(skew, timestamp)
        
    def get_current(self) -> tuple[float, float]:
        """Get current call and put IV."""
        return self._current_call_iv, self._current_put_iv
    
    def get_skew(self) -> float:
        """
        Get IV skew.
        
        Positive skew = put IV > call IV (bearish)
        Negative skew = call IV > put IV (bullish)
        """
        return self._current_call_iv - self._current_put_iv
    
    def get_atm_iv(self) -> float:
        """Get ATM (average) IV."""
        return (self._current_call_iv + self._current_put_iv) / 2


class CompositeMarketData:
    """
    Combines all feeds into a single market data object.
    
    Provides a unified interface for market data required
    by signal generation and other stages.
    """
    
    def __init__(self, market_id: str):
        self.market_id = market_id
        self.btc_price = BTCPriceFeed()
        self.order_book = OrderBookFeed()
        self.funding = FundingFeed()
        self.oi = OIFeed()
        self.cvd = CVDFeed()
        self.options = OptionsFeed()
        self._timestamp: Optional[datetime] = None
        
    def update(
        self,
        btc_price: float,
        order_book: OrderBook,
        funding_rate: float,
        oi: float,
        cvd: float,
        call_iv: float = 0.0,
        put_iv: float = 0.0
    ) -> None:
        """Update all feeds with new data."""
        self._timestamp = datetime.utcnow()
        
        self.btc_price.update(btc_price, self._timestamp)
        self.order_book.update(order_book)
        self.funding.update(funding_rate, self._timestamp)
        self.oi.update(oi, self._timestamp)
        self.cvd.update(cvd, self._timestamp)
        self.options.update(call_iv, put_iv, self._timestamp)
        
    def get_timestamp(self) -> Optional[datetime]:
        """Get last update timestamp."""
        return self._timestamp
    
    def to_dict(self) -> dict:
        """Convert to dictionary for signal generation."""
        ob = self.order_book.get_current()
        
        return {
            'prices': self.btc_price.get_price_history(12),
            'btc_price': self.btc_price.get_current(),
            'btc_price_5min_ago': self.btc_price.get_price_at(5) or self.btc_price.get_current(),
            'cvd_now': self.cvd.get_current(),
            'cvd_5min_ago': self.cvd.get_cvd_at(5) or 0.0,
            'cvd_history': self.cvd.get_cvd_history(10),
            'funding_rate': self.funding.get_current(),
            'funding_history': self.funding.get_funding_history(4),
            'oi_now': self.oi.get_current(),
            'oi_5min_ago': self.oi.get_oi_at(5) or 0.0,
            'oi_history': self.oi.get_oi_history(6),
            'call_iv': self.options.get_current()[0],
            'put_iv': self.options.get_current()[1],
            'iv_history': list(self.options.get_history()),
            'yes_bid': ob.yes_bid if ob else 0.0,
            'yes_ask': ob.yes_ask if ob else 1.0,
            'no_bid': ob.no_bid if ob else 0.0,
            'no_ask': ob.no_ask if ob else 1.0,
            'volume_1min': 0.0,  # Would need volume feed
            'volume_avg': 0.0,   # Would need volume feed
        }


class DataFeedManager:
    """
    Manages multiple market data feeds.
    
    Provides a central interface for accessing data for multiple markets.
    """
    
    def __init__(self):
        self._feeds: dict[str, CompositeMarketData] = {}
        
    def get_or_create_feed(self, market_id: str) -> CompositeMarketData:
        """Get existing feed or create new one for market."""
        if market_id not in self._feeds:
            self._feeds[market_id] = CompositeMarketData(market_id)
        return self._feeds[market_id]
    
    def get_feed(self, market_id: str) -> Optional[CompositeMarketData]:
        """Get feed for market, or None if not exists."""
        return self._feeds.get(market_id)
    
    def list_markets(self) -> List[str]:
        """List all markets with feeds."""
        return list(self._feeds.keys())
    
    def remove_feed(self, market_id: str) -> bool:
        """Remove feed for market."""
        if market_id in self._feeds:
            del self._feeds[market_id]
            return True
        return False
