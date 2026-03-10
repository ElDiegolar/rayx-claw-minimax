"""
Stage 2: Edge & Calibration

Computes market edge, expected value, and statistical significance.
Tracks calibration quality using Brier scores and rolling z-scores.
"""

from dataclasses import dataclass
from typing import Optional, List, Tuple
import logging
import math
from collections import deque

from config import (
    EDGE_THRESHOLD,
    EDGE_THRESHOLD_STRICT,
    Z_SCORE_THRESHOLD,
    BRIER_THRESHOLD,
    CALIBRATION_WINDOW,
    MIN_CALIBRATION_POINTS,
)
from models import Signal


logger = logging.getLogger(__name__)


@dataclass
class CalibrationData:
    """Stores calibration data for a market or strategy."""
    predictions: List[float]  # Model probabilities
    outcomes: List[float]      # Actual outcomes (0 or 1)
    brier_scores: List[float]  # Brier scores for each prediction
    
    def __post_init__(self):
        if self.predictions is None:
            self.predictions = []
        if self.outcomes is None:
            self.outcomes = []
        if self.brier_scores is None:
            self.brier_scores = []
    
    @property
    def count(self) -> int:
        return len(self.predictions)
    
    @property
    def avg_brier(self) -> float:
        if not self.brier_scores:
            return 1.0  # Worst case
        return sum(self.brier_scores) / len(self.brier_scores)
    
    @property
    def accuracy(self) -> float:
        if not self.predictions:
            return 0.5
        correct = sum(
            1 for p, o in zip(self.predictions, self.outcomes)
            if (p >= 0.5 and o == 1.0) or (p < 0.5 and o == 0.0)
        )
        return correct / len(self.predictions)


class EdgeCalculator:
    """
    Handles edge calculation and calibration tracking.
    
    Maintains rolling history of predictions and outcomes
    for calibration analysis.
    """
    
    def __init__(self, window_size: int = CALIBRATION_WINDOW):
        self.window_size = window_size
        self.calibration_data: deque = deque(maxlen=window_size)
        self.rolling_predictions: deque = deque(maxlen=window_size)
        self.rolling_outcomes: deque = deque(maxlen=window_size)
        self.rolling_errors: deque = deque(maxlen=window_size)
        
    def compute_market_edge(self, p_model: float, p_market: float) -> float:
        """
        Compute market edge as difference between model and market probabilities.
        
        Args:
            p_model: Model's predicted probability
            p_market: Market's implied probability
            
        Returns:
            Edge (positive = model thinks market underpriced)
        """
        edge = p_model - p_market
        return edge
    
    def compute_ev(
        self,
        p_model: float,
        p_market: float,
        payout_ratio: float = 1.0
    ) -> float:
        """
        Compute expected value of the trade.
        
        EV = P_win * payout - P_loss * risk
        
        For binary outcome with payout ratio R:
        EV = p_model * R - (1 - p_model) * 1
            = p_model * R - 1 + p_model
            = p_model * (R + 1) - 1
            
        Args:
            p_model: Model's predicted probability
            p_market: Market's implied probability
            payout_ratio: Payout if win (e.g., 1.0 for even money)
            
        Returns:
            Expected value (positive = profitable)
        """
        # Payout is based on market price
        # If p_market = 0.5, payout = 1 (even money)
        # If p_market = 0.25, payout = 3 (3:1)
        if p_market <= 0 or p_market >= 1:
            return 0.0
            
        payout = (1 - p_market) / p_market
        
        # Expected value
        ev = p_model * payout - (1 - p_model)
        
        return ev
    
    def compute_z_score(
        self,
        p_model: float,
        p_market: float,
        rolling_std: Optional[float] = None
    ) -> float:
        """
        Compute z-score for the edge.
        
        z = (p_model - p_market) / std_error
        
        Args:
            p_model: Model's predicted probability
            p_market: Market's implied probability
            rolling_std: Optional rolling standard deviation
            
        Returns:
            Z-score (δ)
        """
        edge = p_model - p_market
        
        # Standard error for binomial proportion
        # SE = sqrt(p * (1-p) / n), assuming n=1 for single prediction
        std_error = math.sqrt(p_model * (1 - p_model))
        
        if std_error == 0:
            return 0.0
            
        z = edge / std_error
        
        return z
    
    def _compute_brier_score(self, prediction: float, outcome: float) -> float:
        """
        Compute Brier score for a single prediction.
        
        Brier Score = (prediction - outcome)^2
        
        Lower is better. Range: 0 to 1
        
        Args:
            prediction: Predicted probability
            outcome: Actual outcome (0 or 1)
            
        Returns:
            Brier score
        """
        return (prediction - outcome) ** 2
    
    def update_brier_score(
        self,
        prediction: float,
        outcome: float
    ) -> float:
        """
        Update Brier score tracking and return the score.
        
        Args:
            prediction: Predicted probability
            outcome: Actual outcome (0 or 1)
            
        Returns:
            Brier score for this prediction
        """
        brier = self._compute_brier_score(prediction, outcome)
        
        # Update rolling history
        self.rolling_predictions.append(prediction)
        self.rolling_outcomes.append(outcome)
        self.rolling_errors.append(brier)
        
        # Also maintain calibration data
        self.calibration_data.append({
            'prediction': prediction,
            'outcome': outcome,
            'brier': brier,
        })
        
        return brier
    
    def get_rolling_brier(self) -> float:
        """Get average Brier score over rolling window."""
        if not self.rolling_errors:
            return 1.0
        return sum(self.rolling_errors) / len(self.rolling_errors)
    
    def get_rolling_std(self) -> float:
        """Get rolling standard deviation of prediction errors."""
        if len(self.rolling_errors) < 2:
            return 0.5  # Default high uncertainty
            
        mean_error = sum(self.rolling_errors) / len(self.rolling_errors)
        variance = sum((e - mean_error) ** 2 for e in self.rolling_errors) / len(self.rolling_errors)
        return math.sqrt(variance)
    
    def check_calibration(self, signal: Signal) -> bool:
        """
        Check if signal passes calibration requirements.
        
        Args:
            signal: Signal to check
            
        Returns:
            True if calibration is acceptable
        """
        # Check if we have enough data
        if len(self.calibration_data) < MIN_CALIBRATION_POINTS:
            logger.debug(f"Insufficient calibration data: {len(self.calibration_data)} < {MIN_CALIBRATION_POINTS}")
            return True  # Can't verify, allow through
        
        # Check Brier score
        avg_brier = self.get_rolling_brier()
        if avg_brier > BRIER_THRESHOLD:
            logger.warning(f"Calibration check failed: Brier {avg_brier:.3f} > {BRIER_THRESHOLD}")
            return False
            
        return True
    
    def check_edge(self, signal: Signal, strict: bool = False) -> Tuple[bool, str]:
        """
        Check if signal has sufficient edge.
        
        Args:
            signal: Signal to check
            strict: Use stricter threshold
            
        Returns:
            Tuple of (passes, reason)
        """
        threshold = EDGE_THRESHOLD_STRICT if strict else EDGE_THRESHOLD
        
        if signal.edge < threshold:
            return False, f"Edge {signal.edge:.4f} < {threshold}"
        
        return True, "Edge sufficient"
    
    def check_z_score(self, signal: Signal) -> Tuple[bool, str]:
        """
        Check if signal's z-score is statistically significant.
        
        Args:
            signal: Signal to check
            
        Returns:
            Tuple of (passes, reason)
        """
        rolling_std = self.get_rolling_std()
        z = self.compute_z_score(signal.p_model, signal.p_market, rolling_std)
        
        if abs(z) < Z_SCORE_THRESHOLD:
            return False, f"Z-score {z:.2f} below threshold {Z_SCORE_THRESHOLD}"
        
        return True, f"Z-score {z:.2f} significant"
    
    def validate_signal(self, signal: Signal) -> Tuple[bool, str, dict]:
        """
        Run all edge and calibration checks on a signal.
        
        Args:
            signal: Signal to validate
            
        Returns:
            Tuple of (passes, reason, details)
        """
        details = {
            'edge': signal.edge,
            'p_model': signal.p_model,
            'p_market': signal.p_market,
            'z_score': self.compute_z_score(signal.p_model, signal.p_market),
            'avg_brier': self.get_rolling_brier(),
            'calibration_count': len(self.calibration_data),
        }
        
        # Check edge
        edge_pass, edge_reason = self.check_edge(signal)
        if not edge_pass:
            return False, edge_reason, details
        
        # Check z-score
        z_pass, z_reason = self.check_z_score(signal)
        if not z_pass:
            return False, z_reason, details
        
        # Check calibration
        cal_pass = self.check_calibration(signal)
        if not cal_pass:
            return False, "Calibration check failed", details
        
        return True, "All edge checks passed", details
    
    def record_outcome(self, prediction: float, outcome: float) -> None:
        """
        Record an actual outcome for calibration tracking.
        
        Args:
            prediction: The predicted probability
            outcome: The actual outcome (0 or 1)
        """
        self.update_brier_score(prediction, outcome)
        logger.debug(f"Recorded outcome: pred={prediction:.3f}, outcome={outcome}, brier={self._compute_brier_score(prediction, outcome):.4f}")


def compute_market_edge(p_model: float, p_market: float) -> float:
    """
    Standalone function to compute market edge.
    
    Args:
        p_model: Model's predicted probability
        p_market: Market's implied probability
        
    Returns:
        Edge value
    """
    return p_model - p_market


def compute_ev(p_model: float, p_market: float, payout_ratio: float = 1.0) -> float:
    """
    Standalone function to compute expected value.
    
    Args:
        p_model: Model's predicted probability
        p_market: Market's implied probability
        payout_ratio: Payout if win
        
    Returns:
        Expected value
    """
    if p_market <= 0 or p_market >= 1:
        return 0.0
        
    payout = (1 - p_market) / p_market
    ev = p_model * payout - (1 - p_model)
    
    return ev


def compute_z_score(p_model: float, p_market: float, rolling_std: float = None) -> float:
    """
    Standalone function to compute z-score.
    
    Args:
        p_model: Model's predicted probability
        p_market: Market's implied probability
        rolling_std: Optional rolling standard deviation
        
    Returns:
        Z-score
    """
    edge = p_model - p_market
    std_error = math.sqrt(p_model * (1 - p_model))
    
    if std_error == 0:
        return 0.0
        
    return edge / std_error
