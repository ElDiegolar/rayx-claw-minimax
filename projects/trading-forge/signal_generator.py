"""
Stage 1: Directional Signal Generation

Computes signals from multiple data sources:
- Momentum (price changes over N periods)
- CVD (Cumulative Volume Delta)
- Funding Rate
- OI Delta (Open Interest change)
- Options Skew (IV difference)

Combines signals using Bayesian combination with base_rate 0.50
Computes consensus score using logit-weighted approach
"""

from dataclasses import dataclass
from typing import Optional, Tuple
import math
import logging

from config import (
    MOMENTUM_THRESHOLD,
    CVD_WEIGHT,
    FUNDING_WEIGHT,
    OI_WEIGHT,
    SKEW_WEIGHT,
    CONSENSUS_GATE,
)
from models import Market, Signal, Direction


logger = logging.getLogger(__name__)


def compute_momentum_signal(prices: list[float], N: int = 5) -> Tuple[float, float]:
    """
    Compute momentum signal from price history.
    
    Args:
        prices: List of recent closing prices (most recent last)
        N: Number of periods for momentum calculation
        
    Returns:
        Tuple of (signal_value, adjustment)
        - signal_value: +1 for bullish, -1 for bearish, 0 for neutral
        - adjustment: adjustment to add to final probability
    """
    if len(prices) < N:
        logger.warning(f"Insufficient price history: {len(prices)} < {N}")
        return 0.0, 0.0
    
    # Calculate price change over N periods
    current_price = prices[-1]
    past_price = prices[-N]
    price_change = (current_price - past_price) / past_price if past_price != 0 else 0.0
    
    # Compute momentum as percentage
    momentum = price_change
    
    # Determine signal based on threshold
    if momentum > MOMENTUM_THRESHOLD:
        # Strong bullish momentum
        # Adjustment scales with momentum magnitude, capped at threshold * 2
        adjustment = min(momentum / MOMENTUM_THRESHOLD * 0.05, 0.10)
        return 1.0, adjustment
    elif momentum < -MOMENTUM_THRESHOLD:
        # Strong bearish momentum
        adjustment = min(abs(momentum) / MOMENTUM_THRESHOLD * 0.05, 0.10)
        return -1.0, -adjustment
    else:
        # Below threshold - weak/neutral signal
        return 0.0, 0.0


def compute_cvd_signal(cvd_now: float, cvd_5min_ago: float) -> Tuple[float, float]:
    """
    Compute CVD (Cumulative Volume Delta) signal.
    
    Args:
        cvd_now: Current cumulative volume delta
        cvd_5min_ago: CVD 5 minutes ago
        
    Returns:
        Tuple of (signal_value, adjustment)
    """
    cvd_change = cvd_now - cvd_5min_ago
    
    # CVD adjustment: +0.05 for positive CVD trend, -0.05 for negative
    if cvd_change > 0:
        # Buying pressure increasing
        adjustment = CVD_WEIGHT
        return 1.0, adjustment
    elif cvd_change < 0:
        # Selling pressure increasing
        adjustment = -CVD_WEIGHT
        return -1.0, adjustment
    else:
        return 0.0, 0.0


def compute_funding_signal(funding_rate: float) -> Tuple[float, float]:
    """
    Compute funding rate signal.
    
    Args:
        funding_rate: Current funding rate (e.g., 0.0001 for 0.01%)
        
    Returns:
        Tuple of (signal_value, adjustment)
    """
    # Funding adjustment: +0.03 if positive, -0.03 if negative
    # Funding > 0 means longers pay shorts (bullish bias)
    # Funding < 0 means shorts pay longers (bearish bias)
    
    if funding_rate > FUNDING_WEIGHT:
        # Positive funding - bullish bias
        adjustment = FUNDING_WEIGHT
        return 1.0, adjustment
    elif funding_rate < -FUNDING_WEIGHT:
        # Negative funding - bearish bias
        adjustment = -FUNDING_WEIGHT
        return -1.0, adjustment
    else:
        return 0.0, 0.0


def compute_oi_delta_signal(
    price_now: float,
    price_5min_ago: float,
    oi_now: float,
    oi_5min_ago: float
) -> Tuple[float, float]:
    """
    Compute OI (Open Interest) delta signal.
    
    Rising price + rising OI = new money entering (confirmation)
    Rising price + falling OI = short covering (weak)
    Falling price + rising OI = new shorts (confirmation)
    Falling price + falling OI = long liquidation (weak)
    
    Args:
        price_now: Current price
        price_5min_ago: Price 5 minutes ago
        oi_now: Current open interest
        oi_5min_ago: OI 5 minutes ago
        
    Returns:
        Tuple of (signal_value, adjustment)
    """
    price_change = price_now - price_5min_ago
    oi_change = oi_now - oi_5min_ago
    
    if oi_5min_ago == 0:
        return 0.0, 0.0
    
    oi_pct_change = oi_change / oi_5min_ago
    
    # OI change threshold
    oi_threshold = OI_WEIGHT
    
    # Rising OI confirms price move
    if oi_pct_change > oi_threshold:
        if price_change > 0:
            # Rising price + rising OI = bullish confirmation
            adjustment = OI_WEIGHT
            return 1.0, adjustment
        elif price_change < 0:
            # Falling price + rising OI = bearish confirmation
            adjustment = -OI_WEIGHT
            return -1.0, adjustment
    # Falling OI contradicts price move
    elif oi_pct_change < -oi_threshold:
        if price_change > 0:
            # Rising price + falling OI = weak bullish (short covering)
            adjustment = OI_WEIGHT * 0.5
            return 0.5, adjustment * 0.5
        elif price_change < 0:
            # Falling price + falling OI = weak bearish (long liquidation)
            adjustment = -OI_WEIGHT * 0.5
            return -0.5, adjustment * 0.5
    
    return 0.0, 0.0


def compute_options_skew(call_iv: float, put_iv: float) -> Tuple[float, float]:
    """
    Compute options skew signal from IV differential.
    
    Args:
        call_iv: Call option implied volatility
        put_iv: Put option implied volatility
        
    Returns:
        Tuple of (signal_value, adjustment)
    """
    if call_iv == 0 or put_iv == 0:
        return 0.0, 0.0
    
    # Skew = put IV - call IV (positive = more demand for puts = bearish)
    skew = put_iv - call_iv
    
    # Skew adjustment: +0.03 if skew < -0.03 (calls favored = bullish),
    # -0.03 if skew > 0.03 (puts favored = bearish)
    skew_threshold = SKEW_WEIGHT
    
    if skew < -skew_threshold:
        # Calls favored over puts - bullish bias
        adjustment = SKEW_WEIGHT
        return 1.0, adjustment
    elif skew > skew_threshold:
        # Puts favored over calls - bearish bias
        adjustment = -SKEW_WEIGHT
        return -1.0, adjustment
    
    return 0.0, 0.0


def _logit(p: float) -> float:
    """Compute logit function with clamping."""
    p = max(0.01, min(0.99, p))
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    """Compute sigmoid function."""
    return 1 / (1 + math.exp(-x))


def aggregate_signals(
    momentum_signal: float,
    cvd_signal: float,
    funding_signal: float,
    oi_signal: float,
    skew_signal: float,
    base_rate: float = 0.50
) -> float:
    """
    Aggregate multiple signals using Bayesian combination.
    
    Uses logit-space averaging with base_rate prior.
    Each signal is treated as a log-odds adjustment.
    
    Args:
        momentum_signal: -1 to +1
        cvd_signal: -1 to +1
        funding_signal: -1 to +1
        oi_signal: -1 to +1
        skew_signal: -1 to +1
        base_rate: Prior probability (default 0.50)
        
    Returns:
        Aggregated probability (0 to 1)
    """
    # Convert base rate to logit
    base_logit = _logit(base_rate)
    
    # Weight each signal by its absolute magnitude
    # Signals further from 0 get more weight
    weights = {
        'momentum': abs(momentum_signal),
        'cvd': abs(cvd_signal),
        'funding': abs(funding_signal),
        'oi': abs(oi_signal),
        'skew': abs(skew_signal),
    }
    
    total_weight = sum(weights.values())
    if total_weight == 0:
        return base_rate
    
    # Normalize weights
    normalized_weights = {k: v / total_weight for k, v in weights.items()}
    
    # Convert signals to logit adjustments
    # Signal of +1 means strongly bullish, -1 means strongly bearish
    # Map to probability adjustments: +1 -> 0.75, -1 -> 0.25
    signal_probs = {
        'momentum': _sigmoid(momentum_signal * 1.5),  # Scale for reasonable probability
        'cvd': _sigmoid(cvd_signal * 1.5),
        'funding': _sigmoid(funding_signal * 1.5),
        'oi': _sigmoid(oi_signal * 1.5),
        'skew': _sigmoid(skew_signal * 1.5),
    }
    
    # Convert to logits and apply weighted average
    weighted_logit_sum = 0.0
    for key, prob in signal_probs.items():
        signal_logit = _logit(prob)
        weighted_logit_sum += normalized_weights[key] * (signal_logit - base_logit)
    
    # Combine with base
    final_logit = base_logit + weighted_logit_sum
    
    # Convert back to probability
    final_prob = _sigmoid(final_logit)
    
    return final_prob


def compute_consensus(
    momentum_signal: float,
    cvd_signal: float,
    funding_signal: float,
    oi_signal: float,
    skew_signal: float
) -> float:
    """
    Compute consensus score C using logit-weighted approach.
    
    Consensus measures agreement between signals.
    C = sum of log-odds weighted signals / number of signals
    
    Args:
        momentum_signal: -1 to +1
        cvd_signal: -1 to +1
        funding_signal: -1 to +1
        oi_signal: -1 to +1
        skew_signal: -1 to +1
        
    Returns:
        Consensus score (0 to infinity, higher = more consensus)
    """
    signals = [
        momentum_signal,
        cvd_signal,
        funding_signal,
        oi_signal,
        skew_signal,
    ]
    
    # Count non-zero signals
    active_signals = [s for s in signals if s != 0]
    if not active_signals:
        return 0.0
    
    # Calculate average absolute signal strength
    avg_abs = sum(abs(s) for s in active_signals) / len(active_signals)
    
    # Calculate agreement: how many signals point in same direction
    positive_count = sum(1 for s in active_signals if s > 0)
    negative_count = sum(1 for s in active_signals if s < 0)
    
    max_aligned = max(positive_count, negative_count)
    alignment_ratio = max_aligned / len(active_signals)
    
    # Consensus C = avg_abs * alignment_ratio * 10
    # Range: 0 to 10
    C = avg_abs * alignment_ratio * 10
    
    return C


def generate_signal(market: Market, market_data: dict) -> Optional[Signal]:
    """
    Generate complete Signal for a market.
    
    Args:
        market: Market data
        market_data: Dictionary containing:
            - prices: list of recent prices
            - cvd_now, cvd_5min_ago: CVD values
            - funding_rate: current funding
            - oi_now, oi_5min_ago: OI values
            - call_iv, put_iv: IV values
            - yes_bid, yes_ask, no_bid, no_ask: order book
            
    Returns:
        Signal object or None if insufficient data
    """
    try:
        # Extract data
        prices = market_data.get('prices', [])
        cvd_now = market_data.get('cvd_now', 0.0)
        cvd_5min_ago = market_data.get('cvd_5min_ago', 0.0)
        funding_rate = market_data.get('funding_rate', 0.0)
        oi_now = market_data.get('oi_now', 0.0)
        oi_5min_ago = market_data.get('oi_5min_ago', 0.0)
        call_iv = market_data.get('call_iv', 0.0)
        put_iv = market_data.get('put_iv', 0.0)
        
        # Compute individual signals
        momentum_signal, momentum_adj = compute_momentum_signal(prices)
        cvd_signal, cvd_adj = compute_cvd_signal(cvd_now, cvd_5min_ago)
        funding_signal, funding_adj = compute_funding_signal(funding_rate)
        
        # Safely get price 5 min ago for OI signal
        if len(prices) >= 5:
            price_5min_ago = prices[-5]
        else:
            price_5min_ago = market.current_price
        
        oi_signal, oi_adj = compute_oi_delta_signal(
            market.current_price,
            price_5min_ago,
            oi_now,
            oi_5min_ago
        )
        skew_signal, skew_adj = compute_options_skew(call_iv, put_iv)
        
        # Aggregate signals to get model probability
        p_model = aggregate_signals(
            momentum_signal,
            cvd_signal,
            funding_signal,
            oi_signal,
            skew_signal
        )
        
        # Get market probability from order book mid-price
        yes_mid = (market.yes_bid + market.yes_ask) / 2 if market.yes_bid and market.yes_ask else market.current_price
        no_mid = (market.no_bid + market.no_ask) / 2 if market.no_bid and market.no_ask else (1 - market.current_price)
        p_market = yes_mid
        
        # Compute edge (model probability - market probability)
        edge = p_model - p_market
        
        # Compute consensus score
        consensus_score = compute_consensus(
            momentum_signal,
            cvd_signal,
            funding_signal,
            oi_signal,
            skew_signal
        )
        
        # Check consensus gate
        if consensus_score < CONSENSUS_GATE:
            logger.info(f"Signal rejected: consensus {consensus_score:.2f} < {CONSENSUS_GATE}")
            return None
        
        # Check for meaningful edge
        if abs(edge) < 0.01:
            logger.info(f"Signal rejected: edge {edge:.3f} too small")
            return None
        
        # Determine direction based on edge - return Direction enum
        if edge > 0:
            direction = Direction.UP
        else:
            direction = Direction.DOWN
        
        signal = Signal(
            market_id=market.id,
            direction=direction,
            p_model=p_model,
            p_market=p_market,
            edge=edge,
            cvd_signal=cvd_signal,
            momentum_signal=momentum_signal,
            funding_signal=funding_signal,
            oi_signal=oi_signal,
            skew_signal=skew_signal,
            consensus_score=consensus_score,
        )
        
        logger.info(f"Generated signal for {market.id}: {direction.value} (p_model={p_model:.3f}, p_market={p_market:.3f}, edge={edge:.3f}, C={consensus_score:.2f})")
        
        return signal
        
    except Exception as e:
        logger.error(f"Error generating signal for {market.id}: {e}")
        return None
