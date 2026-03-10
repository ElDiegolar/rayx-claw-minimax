"""
Trading Forge Configuration Module

Contains all constants, thresholds, risk parameters, and configuration values
for the trading bot. Organized by functional area.

Author: Trading Forge Team
"""

from dataclasses import dataclass
from typing import Final


# =============================================================================
# SIGNAL GENERATION THRESHOLDS (Stage 1)
# =============================================================================

# Momentum threshold for 5-minute candles
MOMENTUM_THRESHOLD: Final[float] = 0.0015

# Price momentum direction (1 = up, -1 = down)
MOMENTUM_UP: Final[int] = 1
MOMENTUM_DOWN: Final[int] = -1


# =============================================================================
# SIGNAL ADJUSTMENTS (Stage 1)
# =============================================================================

# Cumulative Volume Delta (CVD) adjustments
CVD_WEIGHT: Final[float] = 0.05
CVD_POSITIVE_ADJUSTMENT: Final[float] = +0.05
CVD_NEGATIVE_ADJUSTMENT: Final[float] = -0.05

# Funding rate adjustments
FUNDING_WEIGHT: Final[float] = 0.03
FUNDING_POSITIVE_ADJUSTMENT: Final[float] = +0.03  # Positive funding favors UP
FUNDING_NEGATIVE_ADJUSTMENT: Final[float] = -0.03  # Negative funding favors DOWN

# Open Interest (OI) adjustments
OI_WEIGHT: Final[float] = 0.04
OI_INCREASE_ADJUSTMENT: Final[float] = +0.04  # OI increasing favors continuation
OI_DECREASE_ADJUSTMENT: Final[float] = -0.04  # OI decreasing favors reversal

# Options Skew adjustments
SKEW_WEIGHT: Final[float] = 0.03
SKEW_POSITIVE_ADJUSTMENT: Final[float] = +0.03  # Positive skew favors UP
SKEW_NEGATIVE_ADJUSTMENT: Final[float] = -0.03  # Negative skew favors DOWN


# =============================================================================
# CONSENSUS GATE (Stage 1)
# =============================================================================

# Minimum consensus score required to pass
CONSENSUS_GATE: Final[float] = 6.0
CONSENSUS_GATE_STRICT: Final[float] = 7.0  # Stricter gate for high confidence


# =============================================================================
# EDGE CALCULATION (Stage 2)
# =============================================================================

# Minimum edge required to proceed (4%)
EDGE_THRESHOLD: Final[float] = 0.04

# Stricter edge threshold for larger positions (6%)
EDGE_THRESHOLD_STRICT: Final[float] = 0.06

# Z-score threshold for statistical significance
Z_SCORE_THRESHOLD: Final[float] = 1.96  # ~95% confidence interval

# Brier score threshold (lower is better)
BRIER_THRESHOLD: Final[float] = 0.20


# =============================================================================
# TOXICITY FILTERS (Stage 3)
# =============================================================================

# Volume-synchronized Probability of Informed Trading (VPIN)
VPIN_THRESHOLD_HALT: Final[float] = 0.70
VPIN_THRESHOLD_STRICT: Final[float] = 0.80

# Bid-ask spread threshold
SPREAD_THRESHOLD_HALT: Final[float] = 0.04  # 4% spread halts trading
SPREAD_THRESHOLD_WARN: Final[float] = 0.03  # 3% spread triggers warning

# Volume spike thresholds
VOL_SPIKE_HALT_THRESHOLD: Final[float] = 2.0  # 200% of average volume
VOL_SPIKE_RESUME_THRESHOLD: Final[float] = 1.5  # 150% of average volume

# Order book imbalance threshold
ORDERBOOK_IMBALANCE_THRESHOLD: Final[float] = 0.70


# =============================================================================
# RISK MANAGEMENT - KELLY CRITERION (Stage 4)
# =============================================================================

# Kelly fraction multiplier (conservative approach)
KELLY_ALPHA: Final[float] = 0.25

# Hard cap on position size as fraction of bankroll
KELLY_HARD_CAP: Final[float] = 0.02  # 2% maximum

# Minimum Kelly fraction to execute
KELLY_MINIMUM: Final[float] = 0.005  # 0.5% minimum

# Kelly calculation parameters
KELLY_OPTIMAL_FRACTION: Final[float] = 0.25  # Standard Kelly
KELLY_FRACTIONAL: Final[float] = 0.25  # Fractional Kelly multiplier


# =============================================================================
# RISK LIMITS (Stage 5)
# =============================================================================

# Daily Value at Risk (VaR) limit
DAILY_VAR_LIMIT: Final[float] = -0.05  # -5% daily loss limit

# Maximum Drawdown (DD) limit
MAX_DRAWDOWN_LIMIT: Final[float] = -0.08  # -8% maximum drawdown

# Maximum exposure to single market
SINGLE_MARKET_EXPOSURE: Final[float] = 0.20  # 20% max per market

# Maximum total exposure
TOTAL_EXPOSURE_LIMIT: Final[float] = 0.50  # 50% max total exposure

# Maximum number of concurrent positions
MAX_CONCURRENT_POSITIONS: Final[int] = 5

# Maximum correlation between positions
MAX_POSITION_CORRELATION: Final[float] = 0.70


# =============================================================================
# STREAK MANAGEMENT
# =============================================================================

# Consecutive loss threshold to trigger pause
LOSS_STREAK_HALT: Final[int] = 4

# Pause duration in minutes after streak halt
LOSS_STREAK_PAUSE_MINUTES: Final[int] = 15

# Cooldown after consecutive wins
WIN_STREAK_COOLDOWN: Final[int] = 2


# =============================================================================
# PERFORMANCE TARGETS
# =============================================================================

# Sharpe Ratio target
SHARPE_RATIO_TARGET: Final[float] = 2.0

# Win rate target
WIN_RATE_TARGET: Final[float] = 0.58  # 58%

# Minimum profit factor
PROFIT_FACTOR_MIN: Final[float] = 1.5

# Maximum slippage tolerance (as fraction of position)
MAX_SLIPPAGE_TOLERANCE: Final[float] = 0.02  # 2%


# =============================================================================
# MARKET SPECIFIC PARAMETERS
# =============================================================================

# Target markets
TARGET_MARKETS: Final[list[str]] = ["BTC_UP", "BTC_DOWN"]

# Market resolution time (minutes)
MARKET_RESOLUTION_MINUTES: Final[int] = 30

# Minimum market volume to consider
MIN_MARKET_VOLUME: Final[float] = 10000.0

# Minimum liquidity (yes + no volume)
MIN_LIQUIDITY: Final[float] = 5000.0


# =============================================================================
# TRADING HOURS & SCHEDULES
# =============================================================================

# Trading session hours (UTC)
TRADING_START_HOUR: Final[int] = 0
TRADING_END_HOUR: Final[int] = 23

# Market scanning interval (seconds)
SCAN_INTERVAL_SECONDS: Final[int] = 30

# Signal validity duration (seconds)
SIGNAL_VALIDITY_SECONDS: Final[int] = 120


# =============================================================================
# EXECUTION PARAMETERS (Stage 6)
# =============================================================================

# Order types
ORDER_TYPE_LIMIT: Final[str] = "limit"
ORDER_TYPE_MARKET: Final[str] = "market"

# Order execution parameters
MAX_RETRY_ATTEMPTS: Final[int] = 3
RETRY_DELAY_SECONDS: Final[float] = 1.0
ORDER_TIMEOUT_SECONDS: Final[float] = 10.0

# Fill price slippage
FILL_SLIPPAGE: Final[float] = 0.001  # 0.1%


# =============================================================================
# DATA WINDOWS & HISTORIES
# =============================================================================

# Price history window for momentum calculation
PRICE_HISTORY_WINDOW: Final[int] = 12  # 12 * 5min = 60min

# CVD calculation window
CVD_WINDOW: Final[int] = 10

# OI history window
OI_HISTORY_WINDOW: Final[int] = 6

# Funding history window
FUNDING_HISTORY_WINDOW: Final[int] = 4

# Options data window
OPTIONS_HISTORY_WINDOW: Final[int] = 24


# =============================================================================
# CALIBRATION PARAMETERS
# =============================================================================

# Model calibration window
CALIBRATION_WINDOW: Final[int] = 100

# Calibration update frequency (in signals)
CALIBRATION_UPDATE_FREQ: Final[int] = 50

# Historical calibration data points needed
MIN_CALIBRATION_POINTS: Final[int] = 30


# =============================================================================
# LOGGING & MONITORING
# =============================================================================

# Log levels
LOG_LEVEL_DEBUG: Final[str] = "DEBUG"
LOG_LEVEL_INFO: Final[str] = "INFO"
LOG_LEVEL_WARNING: Final[str] = "WARNING"
LOG_LEVEL_ERROR: Final[str] = "ERROR"

# Performance tracking
PERFORMANCE_LOG_INTERVAL: Final[int] = 100  # Log every N trades

# Alert thresholds
ALERT_DRAWDOWN: Final[float] = -0.05  # Alert at 5% drawdown
ALERT_LOSS_STREAK: Final[int] = 3  # Alert after 3 consecutive losses


# =============================================================================
# STAGE CONFIGURATION
# =============================================================================

@dataclass(frozen=True)
class StageConfig:
    """Configuration for each pipeline stage."""
    enabled: bool = True
    timeout_seconds: float = 5.0
    required: bool = True


# Stage-specific configurations
STAGE_SIGNAL: Final[StageConfig] = StageConfig(
    enabled=True,
    timeout_seconds=2.0,
    required=True
)

STAGE_EDGE: Final[StageConfig] = StageConfig(
    enabled=True,
    timeout_seconds=1.0,
    required=True
)

STAGE_TOXICITY: Final[StageConfig] = StageConfig(
    enabled=True,
    timeout_seconds=1.0,
    required=True
)

STAGE_RISK: Final[StageConfig] = StageConfig(
    enabled=True,
    timeout_seconds=1.0,
    required=True
)

STAGE_EXECUTION: Final[StageConfig] = StageConfig(
    enabled=True,
    timeout_seconds=10.0,
    required=True
)


# =============================================================================
# FEATURE FLAGS
# =============================================================================

# Feature toggles
ENABLE_PAPER_TRADING: Final[bool] = True
ENABLE_AUTO_CALIBRATION: Final[bool] = True
ENABLE_DYNAMIC_KELLY: Final[bool] = True
ENABLE_STREAK_MANAGEMENT: Final[bool] = True
ENABLE_CIRCUIT_BREAKERS: Final[bool] = True

# Debug flags
DEBUG_MODE: Final[bool] = False
VERBOSE_LOGGING: Final[bool] = False
SAVE_INTERMEDIATE_STAGES: Final[bool] = False


# =============================================================================
# API & CONNECTIONS
# =============================================================================

# Polymarket API (example endpoints)
POLYMARKET_API_BASE: Final[str] = "https://clob.polymarket.com"
POLYMARKET_WS_BASE: Final[str] = "wss://ws-subscriptions-clob.polymarket.com"

# Rate limiting
MAX_REQUESTS_PER_MINUTE: Final[int] = 60
RATE_LIMIT_DELAY: Final[float] = 1.0  # seconds between requests

# Connection timeouts
HTTP_TIMEOUT: Final[float] = 10.0
WS_TIMEOUT: Final[float] = 30.0


# =============================================================================
# VALIDATION RANGES
# =============================================================================

# Valid price range
MIN_VALID_PRICE: Final[float] = 0.01
MAX_VALID_PRICE: Final[float] = 0.99

# Valid probability range
MIN_PROBABILITY: Final[float] = 0.0
MAX_PROBABILITY: Final[float] = 1.0

# Valid direction
DIRECTION_UP: Final[str] = "UP"
DIRECTION_DOWN: Final[str] = "DOWN"
VALID_DIRECTIONS: Final[list[str]] = [DIRECTION_UP, DIRECTION_DOWN]
