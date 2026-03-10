"""
Stage 3: Short-Timeframe Toxicity Filters

Filters out signals in adverse trading conditions:
- VPIN (Volume-Synchronized Probability of Informed Trading)
- Spread Check
- Volume Spike Detection
- Momentum Exhaustion
- Macro Event Window

These filters prevent trading in toxic market conditions.
"""

from dataclasses import dataclass
from typing import Optional, List, Tuple, Dict
import logging
from datetime import datetime, timedelta
from collections import deque

from config import (
    VPIN_THRESHOLD_HALT,
    VPIN_THRESHOLD_STRICT,
    SPREAD_THRESHOLD_HALT,
    SPREAD_THRESHOLD_WARN,
    VOL_SPIKE_HALT_THRESHOLD,
    VOL_SPIKE_RESUME_THRESHOLD,
    ORDERBOOK_IMBALANCE_THRESHOLD,
)
from models import Market, Signal


logger = logging.getLogger(__name__)


@dataclass
class Candle:
    """Represents a price candle."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: float = 0.0
    sell_volume: float = 0.0


@dataclass
class ToxicityResult:
    """Result of toxicity analysis."""
    passes: bool
    reason: str
    details: Dict[str, float]
    warnings: List[str]


class ToxicityFilters:
    """
    Collection of short-timeframe toxicity filters.
    
    Each filter checks for specific adverse market conditions.
    """
    
    def __init__(self, history_size: int = 20):
        self.history_size = history_size
        self.volume_history: deque = deque(maxlen=history_size)
        self.vpin_history: deque = deque(maxlen=history_size)
        self.candle_history: deque = deque(maxlen=10)
        self.vwap: Optional[float] = None
        
    def compute_vpin(
        self,
        v_buy: float,
        v_sell: float,
        v_total: float
    ) -> float:
        """
        Compute Volume-Synchronized Probability of Informed Trading.
        
        VPIN = |Buy Volume - Sell Volume| / Total Volume
        
        High VPIN indicates informed trading / toxic conditions.
        
        Args:
            v_buy: Buy volume
            v_sell: Sell volume
            v_total: Total volume
            
        Returns:
            VPIN value (0 to 1)
        """
        if v_total == 0:
            return 0.0
            
        vpin = abs(v_buy - v_sell) / v_total
        self.vpin_history.append(vpin)
        
        return vpin
    
    def get_average_volume(self) -> float:
        """Get average volume over history."""
        if not self.volume_history:
            return 0.0
        return sum(self.volume_history) / len(self.volume_history)
    
    def check_vpin(self, vpin: float, strict: bool = False) -> Tuple[bool, str]:
        """
        Check VPIN threshold.
        
        Args:
            vpin: Current VPIN value
            strict: Use stricter threshold
            
        Returns:
            Tuple of (passes, reason)
        """
        threshold = VPIN_THRESHOLD_STRICT if strict else VPIN_THRESHOLD_HALT
        
        if vpin > threshold:
            return False, f"VPIN {vpin:.3f} > {threshold}"
        
        return True, "VPIN acceptable"
    
    def check_spread_filter(
        self,
        yes_ask: float,
        yes_bid: float,
        no_ask: Optional[float] = None,
        no_bid: Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Check bid-ask spread.
        
        Args:
            yes_ask: Best ask for YES
            yes_bid: Best bid for YES
            no_ask: Best ask for NO
            no_bid: Best bid for NO
            
        Returns:
            Tuple of (passes, reason)
        """
        if yes_ask == 0 or yes_bid == 0:
            return False, "Invalid order book prices"
            
        # Calculate YES spread as percentage
        spread = (yes_ask - yes_bid) / yes_ask
        
        if spread > SPREAD_THRESHOLD_HALT:
            return False, f"Spread {spread:.3f} > {SPREAD_THRESHOLD_HALT}"
        elif spread > SPREAD_THRESHOLD_WARN:
            return True, f"Spread warning: {spread:.3f}"
        
        return True, "Spread acceptable"
    
    def check_vol_spike(
        self,
        current_volume: float,
        strict: bool = False
    ) -> Tuple[bool, str]:
        """
        Check for volume spike.
        
        Args:
            current_volume: Current volume
            strict: Use stricter threshold
            
        Returns:
            Tuple of (passes, reason)
        """
        avg_volume = self.get_average_volume()
        
        if avg_volume == 0:
            return True, "No volume history"
            
        ratio = current_volume / avg_volume
        
        threshold = VOL_SPIKE_RESUME_THRESHOLD if strict else VOL_SPIKE_HALT_THRESHOLD
        
        if ratio > threshold:
            return False, f"Volume spike {ratio:.2f}x > {threshold}"
        
        self.volume_history.append(current_volume)
        
        return True, "Volume acceptable"
    
    def check_orderbook_imbalance(
        self,
        yes_bid_qty: float,
        yes_ask_qty: float,
        no_bid_qty: float,
        no_ask_qty: float
    ) -> Tuple[bool, str]:
        """
        Check for order book imbalance.
        
        Args:
            yes_bid_qty: YES bid quantity
            yes_ask_qty: YES ask quantity
            no_bid_qty: NO bid quantity
            no_ask_qty: NO ask quantity
            
        Returns:
            Tuple of (passes, reason)
        """
        total = yes_bid_qty + yes_ask_qty + no_bid_qty + no_ask_qty
        if total == 0:
            return True, "No order book data"
        
        # Calculate imbalance for YES side
        yes_total = yes_bid_qty + yes_ask_qty
        if yes_total > 0:
            yes_imbalance = yes_bid_qty / yes_total
        else:
            yes_imbalance = 0.5
            
        # Check if imbalance is too extreme
        if yes_imbalance > ORDERBOOK_IMBALANCE_THRESHOLD:
            return False, f"YES orderbook imbalance {yes_imbalance:.3f} > {ORDERBOOK_IMBALANCE_THRESHOLD}"
        elif yes_imbalance < (1 - ORDERBOOK_IMBALANCE_THRESHOLD):
            return False, f"NO orderbook imbalance {1-yes_imbalance:.3f} > {ORDERBOOK_IMBALANCE_THRESHOLD}"
        
        return True, "Orderbook balanced"
    
    def check_momentum_exhaustion(
        self,
        last_3_candles: List[Candle],
        cvd: float
    ) -> Tuple[bool, str]:
        """
        Check for momentum exhaustion.
        
        Signals that have run too far too fast often reverse.
        
        Args:
            last_3_candles: Last 3 price candles
            cvd: Current cumulative volume delta
            
        Returns:
            Tuple of (passes, reason)
        """
        if len(last_3_candles) < 3:
            return True, "Insufficient candle history"
        
        # Check if price has been moving in one direction
        directions = []
        for i in range(1, 3):
            if last_3_candles[i].close > last_3_candles[i-1].close:
                directions.append(1)
            elif last_3_candles[i].close < last_3_candles[i-1].close:
                directions.append(-1)
            else:
                directions.append(0)
        
        # All same direction = potential exhaustion
        if all(d == 1 for d in directions):
            # Check if CVD is diverging (opposite to price)
            last_price = last_3_candles[-1].close
            first_price = last_3_candles[0].open
            price_up = last_price > first_price
            
            if price_up and cvd < -0.1:
                return False, "Momentum exhaustion: price up, CVD down"
            elif not price_up and cvd > 0.1:
                return False, "Momentum exhaustion: price down, CVD up"
        
        # Check for very rapid moves (> 3% in 3 candles)
        total_move = abs(last_3_candles[-1].close - last_3_candles[0].open) / last_3_candles[0].open
        if total_move > 0.03:
            # Check if volume is declining (exhaustion sign)
            volumes = [c.volume for c in last_3_candles]
            if volumes[-1] < volumes[0] * 0.5:
                return False, f"Rapid move {total_move:.2%} with declining volume"
        
        return True, "No momentum exhaustion"
    
    def check_macro_event(
        self,
        current_time: datetime,
        event_time: Optional[datetime] = None,
        window_minutes: int = 10
    ) -> Tuple[bool, str]:
        """
        Check if near a macro event (halts trading around events).
        
        Args:
            current_time: Current time
            event_time: Time of macro event (optional)
            window_minutes: Window around event to halt
            
        Returns:
            Tuple of (passes, reason)
        """
        # This is a placeholder - in production, you'd check
        # against a calendar of macro events (FOMC, CPI, etc.)
        if event_time is None:
            return True, "No macro event"
        
        time_diff = abs((current_time - event_time).total_seconds() / 60)
        
        if time_diff < window_minutes:
            return False, f"Within {window_minutes}min of macro event"
        
        return True, "No macro event nearby"
    
    def check_time_to_resolution(
        self,
        resolution_time: datetime,
        current_time: datetime,
        min_minutes: int = 5,
        max_minutes: int = 30
    ) -> Tuple[bool, str]:
        """
        Check if market resolution is in optimal window.
        
        Args:
            resolution_time: When market resolves
            current_time: Current time
            min_minutes: Minimum minutes until resolution
            max_minutes: Maximum minutes until resolution
            
        Returns:
            Tuple of (passes, reason)
        """
        minutes_left = (resolution_time - current_time).total_seconds() / 60
        
        if minutes_left < min_minutes:
            return False, f"Too close to resolution: {minutes_left:.0f}min"
        
        if minutes_left > max_minutes:
            return False, f"Too far from resolution: {minutes_left:.0f}min"
        
        return True, f"Resolution in {minutes_left:.0f}min (optimal)"
    
    def run_all_filters(
        self,
        market: Market,
        signal: Signal,
        market_data: Dict
    ) -> ToxicityResult:
        """
        Run all toxicity filters on a signal.
        
        Args:
            market: Market data
            signal: Generated signal
            market_data: Dictionary with current market data
            
        Returns:
            ToxicityResult with pass/fail and details
        """
        warnings = []
        details = {}
        
        # Get data from market_data dict
        yes_ask = market_data.get('yes_ask', 0)
        yes_bid = market_data.get('yes_bid', 0)
        yes_bid_qty = market_data.get('yes_bid_qty', 0)
        yes_ask_qty = market_data.get('yes_ask_qty', 0)
        no_bid_qty = market_data.get('no_bid_qty', 0)
        no_ask_qty = market_data.get('no_ask_qty', 0)
        
        current_volume = market_data.get('current_volume', 0)
        buy_volume = market_data.get('buy_volume', 0)
        sell_volume = market_data.get('sell_volume', 0)
        
        cvd = market_data.get('cvd', 0)
        candles = market_data.get('candles', [])
        
        # 1. Check spread
        spread_pass, spread_reason = self.check_spread_filter(yes_ask, yes_bid)
        details['spread'] = yes_ask - yes_bid if yes_ask and yes_bid else 0
        
        if not spread_pass:
            return ToxicityResult(
                passes=False,
                reason=spread_reason,
                details=details,
                warnings=warnings
            )
        
        # 2. Check VPIN
        if buy_volume > 0 or sell_volume > 0:
            vpin = self.compute_vpin(buy_volume, sell_volume, buy_volume + sell_volume)
            details['vpin'] = vpin
            vpin_pass, vpin_reason = self.check_vpin(vpin)
            if not vpin_pass:
                return ToxicityResult(
                    passes=False,
                    reason=vpin_reason,
                    details=details,
                    warnings=warnings
                )
        
        # 3. Check volume spike
        vol_pass, vol_reason = self.check_vol_spike(current_volume)
        details['vol_ratio'] = current_volume / self.get_average_volume() if self.get_average_volume() > 0 else 0
        
        if not vol_pass:
            return ToxicityResult(
                passes=False,
                reason=vol_reason,
                details=details,
                warnings=warnings
            )
        
        # 4. Check orderbook imbalance
        imbalance_pass, imbalance_reason = self.check_orderbook_imbalance(
            yes_bid_qty, yes_ask_qty, no_bid_qty, no_ask_qty
        )
        
        if not imbalance_pass:
            return ToxicityResult(
                passes=False,
                reason=imbalance_reason,
                details=details,
                warnings=warnings
            )
        
        # 5. Check momentum exhaustion
        if len(candles) >= 3:
            exhaust_pass, exhaust_reason = self.check_momentum_exhaustion(candles, cvd)
            if not exhaust_pass:
                return ToxicityResult(
                    passes=False,
                    reason=exhaust_reason,
                    details=details,
                    warnings=warnings
                )
        
        # 6. Check time to resolution
        current_time = datetime.now()
        res_pass, res_reason = self.check_time_to_resolution(
            market.resolution_time, current_time
        )
        if not res_pass:
            return ToxicityResult(
                passes=False,
                reason=res_reason,
                details=details,
                warnings=warnings
            )
        
        # All checks passed
        return ToxicityResult(
            passes=True,
            reason="All toxicity filters passed",
            details=details,
            warnings=warnings
        )


# Standalone filter functions

def compute_vpin(v_buy: float, v_sell: float, v_total: float) -> float:
    """
    Standalone VPIN computation.
    
    Args:
        v_buy: Buy volume
        v_sell: Sell volume
        v_total: Total volume
        
    Returns:
        VPIN value
    """
    if v_total == 0:
        return 0.0
    return abs(v_buy - v_sell) / v_total


def check_spread_filter(yes_ask: float, yes_bid: float) -> Tuple[bool, str]:
    """
    Standalone spread check.
    
    Args:
        yes_ask: YES ask price
        yes_bid: YES bid price
        
    Returns:
        Tuple of (passes, reason)
    """
    if yes_ask == 0 or yes_bid == 0:
        return False, "Invalid order book prices"
        
    spread = (yes_ask - yes_bid) / yes_ask
    
    if spread > SPREAD_THRESHOLD_HALT:
        return False, f"Spread {spread:.3f} > {SPREAD_THRESHOLD_HALT}"
    
    return True, "Spread acceptable"


def check_vol_spike(iv_1min: float, avg_volume: float = None) -> Tuple[bool, str]:
    """
    Standalone volume spike check.
    
    Args:
        iv_1min: Current volume
        avg_volume: Average volume (optional)
        
    Returns:
        Tuple of (passes, reason)
    """
    if avg_volume is None or avg_volume == 0:
        return True, "No baseline to compare"
    
    ratio = iv_1min / avg_volume
    
    if ratio > VOL_SPIKE_HALT_THRESHOLD:
        return False, f"Volume spike {ratio:.2f}x"
    
    return True, "Volume acceptable"


def check_momentum_exhaustion(last_3_candles: List[Candle], cvd: float) -> Tuple[bool, str]:
    """
    Standalone momentum exhaustion check.
    
    Args:
        last_3_candles: Last 3 candles
        cvd: Current CVD
        
    Returns:
        Tuple of (passes, reason)
    """
    if len(last_3_candles) < 3:
        return True, "Insufficient data"
    
    # Check price direction consistency
    directions = []
    for i in range(1, 3):
        if last_3_candles[i].close > last_3_candles[i-1].close:
            directions.append(1)
        elif last_3_candles[i].close < last_3_candles[i-1].close:
            directions.append(-1)
        else:
            directions.append(0)
    
    # Check for divergence
    if all(d == 1 for d in directions):
        last_price = last_3_candles[-1].close
        first_price = last_3_candles[0].open
        
        if last_price > first_price and cvd < -0.1:
            return False, "Bearish divergence"
        elif last_price < first_price and cvd > 0.1:
            return False, "Bullish divergence"
    
    return True, "No exhaustion"


def check_macro_event(event_time: datetime, current_time: datetime) -> Tuple[bool, str]:
    """
    Standalone macro event check.
    
    Args:
        event_time: Time of macro event
        current_time: Current time
        
    Returns:
        Tuple of (passes, reason)
    """
    window_minutes = 10
    time_diff = abs((current_time - event_time).total_seconds() / 60)
    
    if time_diff < window_minutes:
        return False, f"Within {window_minutes}min of macro event"
    
    return True, "No macro event"
