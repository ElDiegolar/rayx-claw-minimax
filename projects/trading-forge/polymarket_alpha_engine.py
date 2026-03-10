"""
╔══════════════════════════════════════════════════════════════════╗
║   POLYMARKET BTC ALPHA ENGINE v2.0                               ║
║   4-Stream Quant Trading System                                  ║
║   Stream 1: Directional Signal                                   ║
║   Stream 2: Cross-Venue Arbitrage (Polymarket <> Kalshi)         ║
║   Stream 3: Market Making + Rebate Harvesting                    ║
║   Stream 4: Smart Money / Whale Tracking                         ║
╚══════════════════════════════════════════════════════════════════╝

DISCLAIMER: Research/educational framework only.
Real trading involves substantial financial risk. All API keys,
credentials, and live connections are placeholders. Do not deploy
with real capital without extensive testing and legal review.
"""

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
import statistics

# ── Optional imports ─────────────────────────────────────────────
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    print("[WARN] websockets not installed. Live feeds disabled.")

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    print("[WARN] aiohttp not installed. REST calls will be mocked.")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ALPHA_ENGINE")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 1 — CONFIGURATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class EngineConfig:
    # ── Bankroll & allocation ─────────────────────────────────────
    total_bankroll: float        = 10_000.0
    stream1_alloc: float         = 0.40   # Directional
    stream2_alloc: float         = 0.30   # Cross-arb
    stream3_alloc: float         = 0.20   # Market making
    stream4_alloc: float         = 0.10   # Whale copy

    # ── Kelly parameters ─────────────────────────────────────────
    alpha_base: float            = 0.25
    alpha_geo_risk: float        = 0.12
    alpha_cascade: float         = 0.00
    alpha_cme_pin: float         = 0.10
    alpha_ibit_confirm: float    = 0.30
    max_bet_pct: float           = 0.02   # Hard cap: 2% of total bankroll

    # ── Edge & filter thresholds ──────────────────────────────────
    edge_min_base: float         = 0.055
    edge_min_geo: float          = 0.08
    edge_min_pin: float          = 0.08
    edge_min_cascade_hi: float   = 0.07
    spread_max: float            = 0.04
    vpin_threshold: float        = 0.70
    vol_spike_halt: float        = 2.00   # 200% annualised IV
    vol_spike_resume: float      = 1.50

    # ── Consensus gate ────────────────────────────────────────────
    consensus_threshold: float   = 6.0

    # ── Risk limits ───────────────────────────────────────────────
    daily_var_limit: float       = 0.05
    max_drawdown: float          = 0.08
    max_exposure_ratio: float    = 0.20
    streak_loss_pause_n: int     = 4
    streak_pause_min: int        = 15

    # ── Cross-arb ────────────────────────────────────────────────
    xarb_min_net_spread: float   = 0.015
    xarb_fill_timeout: float     = 10.0

    # ── Intra-arb ────────────────────────────────────────────────
    intra_arb_threshold: float   = 0.985

    # ── Market making ────────────────────────────────────────────
    mm_quote_offset: float       = 0.005
    mm_size_pct: float           = 0.005
    mm_signal_cancel_edge: float = 0.05

    # ── Whale detection ───────────────────────────────────────────
    whale_bet_min: float         = 2_000.0
    whale_copy_pct: float        = 0.30
    whale_tier1_winrate: float   = 0.65
    whale_tier1_copy_pct: float  = 0.50

    # ── Geo-risk (oil price filter) ───────────────────────────────
    oil_shock_pct_1hr: float     = 0.03
    oil_elevated_pct_1hr: float  = 0.01

    # ── Liquidation cascade ───────────────────────────────────────
    cascade_halt_usd: float      = 50_000_000
    cascade_elevated_usd: float  = 10_000_000

    # ── CME strike pinning ────────────────────────────────────────
    pin_distance_tight: float    = 0.005
    pin_distance_wide: float     = 0.015
    pin_time_window_min: int     = 60

    # ── IBIT gamma signal ─────────────────────────────────────────
    ibit_pcr_bullish: float      = 0.50
    ibit_pcr_bearish: float      = 2.00

    # ── API endpoints (PLACEHOLDERS — replace with real keys) ─────
    polymarket_clob_ws: str      = "wss://clob.polymarket.com/ws"
    polymarket_api: str          = "https://clob.polymarket.com"
    kalshi_api: str              = "https://trading-api.kalshi.com/trade-api/v2"
    binance_ws: str              = "wss://stream.binance.com:9443/ws"

    polymarket_api_key: str      = "YOUR_POLYMARKET_API_KEY"
    kalshi_api_key: str          = "YOUR_KALSHI_API_KEY"
    bitquery_api_key: str        = "YOUR_BITQUERY_API_KEY"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 2 — DATA MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class Direction(Enum):
    UP   = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"

class RegimeMode(Enum):
    NORMAL    = "NORMAL"
    GEO_RISK  = "GEO_RISK"
    CASCADE   = "CASCADE"
    CME_PIN   = "CME_PIN"

class StreamMode(Enum):
    ALL         = "ALL"
    ARB_MM_ONLY = "ARB_MM_ONLY"

@dataclass
class MarketState:
    btc_spot: float              = 0.0
    btc_price_history: deque     = field(default_factory=lambda: deque(maxlen=300))

    cvd_now: float               = 0.0
    cvd_5min_ago: float          = 0.0
    cvd_history: deque           = field(default_factory=lambda: deque(maxlen=100))

    funding_rate_8hr: float      = 0.0
    oi_now: float                = 0.0
    oi_5min_ago: float           = 0.0

    iv_atm: float                = 0.0
    iv_25d_call: float           = 0.0
    iv_25d_put: float            = 0.0
    iv_1min_realised: float      = 0.0

    ibit_pcr: float              = 1.0
    ibit_net_flow_30min: float   = 0.0

    cme_basis: float             = 0.0
    cme_max_pain: float          = 0.0
    cme_expiry_minutes: float    = 9999.0

    poly_yes_bid: float          = 0.0
    poly_yes_ask: float          = 0.0
    poly_no_bid: float           = 0.0
    poly_no_ask: float           = 0.0

    kalshi_yes_bid: float        = 0.0
    kalshi_yes_ask: float        = 0.0

    liq_volume_1min: float       = 0.0

    oil_price_now: float         = 0.0
    oil_price_1hr_ago: float     = 0.0

    last_updated: float          = field(default_factory=time.time)


@dataclass
class Signal:
    p_model: float               = 0.50
    direction: Direction         = Direction.FLAT
    consensus_score: float       = 0.0
    timestamp: float             = field(default_factory=time.time)

    mom_adj: float               = 0.0
    cvd_adj: float               = 0.0
    funding_adj: float           = 0.0
    oi_adj: float                = 0.0
    skew_adj: float              = 0.0
    ibit_gamma_adj: float        = 0.0
    cme_pin_adj: float           = 0.0


@dataclass
class TradeOrder:
    stream: int                  = 1
    market_id: str               = ""
    venue: str                   = "polymarket"
    direction: Direction         = Direction.UP
    leg: str                     = "YES"
    size_usdc: float             = 0.0
    limit_price: float           = 0.0
    p_model: float               = 0.50
    p_market: float              = 0.50
    edge: float                  = 0.0
    delta_z: float               = 0.0
    ev: float                    = 0.0
    consensus_score: float       = 0.0
    kelly_f: float               = 0.0
    timestamp: float             = field(default_factory=time.time)


@dataclass
class TradeLog:
    order: TradeOrder            = field(default_factory=TradeOrder)
    fill_price: float            = 0.0
    fill_time: float             = 0.0
    resolved_outcome: Optional[bool] = None
    pnl: float                   = 0.0


@dataclass
class PortfolioState:
    bankroll: float                   = 10_000.0
    peak_bankroll: float              = 10_000.0
    open_bets: list                   = field(default_factory=list)
    daily_pnl: float                  = 0.0
    daily_start_bankroll: float       = 10_000.0
    consecutive_losses_up: int        = 0
    consecutive_losses_down: int      = 0
    up_paused_until: float            = 0.0
    down_paused_until: float          = 0.0
    resolved_trades: list             = field(default_factory=list)

    brier_scores: dict                = field(default_factory=lambda: {
        "momentum": 0.25, "cvd": 0.20, "oi_delta": 0.25,
        "funding": 0.25, "skew": 0.25,
    })

    edge_history: deque               = field(default_factory=lambda: deque(maxlen=50))
    whale_db: dict                    = field(default_factory=dict)

    regime: RegimeMode                = RegimeMode.NORMAL
    stream_mode: StreamMode           = StreamMode.ALL
    current_alpha: float              = 0.25


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 3 — FEE MODEL
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class FeeModel:
    """Polymarket taker fee + maker rebate (Jan 2026 fee structure)."""

    FEE_RATE = 0.0625

    @staticmethod
    def taker_fee(p: float) -> float:
        """Fee per $1 of shares at probability p."""
        p = max(0.01, min(0.99, p))
        return p * (1 - p) * FeeModel.FEE_RATE

    @staticmethod
    def maker_rebate_estimate(p: float) -> float:
        """~50% of taker fee returned as rebate for limit orders."""
        return FeeModel.taker_fee(p) * 0.50

    @staticmethod
    def breakeven_edge_taker(p: float) -> float:
        p = max(0.01, min(0.99, p))
        return FeeModel.taker_fee(p) / (1 - p)

    @staticmethod
    def net_ev(p_model: float, p_market: float, is_maker: bool = False) -> float:
        """
        Net EV per $1 of shares, accounting for fees.
        EV = p_model * b - (1 - p_model) - fee + rebate
        """
        p_model  = max(0.01, min(0.99, p_model))
        p_market = max(0.01, min(0.99, p_market))
        b = (1.0 / p_market) - 1.0
        gross_ev = p_model * b - (1 - p_model)
        fee      = FeeModel.taker_fee(p_market)
        rebate   = FeeModel.maker_rebate_estimate(p_market) if is_maker else 0.0
        return gross_ev - fee + rebate

    @staticmethod
    def print_fee_table():
        print("\n── Fee Analysis Table ──────────────────────────────────")
        print(f"  {'p':>6} │ {'fee':>8} │ {'BE edge':>10} │ {'maker rebate':>14}")
        print("  " + "─" * 46)
        for p in [0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95]:
            fee    = FeeModel.taker_fee(p)
            be     = FeeModel.breakeven_edge_taker(p)
            rebate = FeeModel.maker_rebate_estimate(p)
            print(f"  {p:>6.2f} │ {fee:>8.4f} │ {be:>10.4f} │ {rebate:>14.4f}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 4 — MATH UTILITIES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MathUtils:

    @staticmethod
    def logit(p: float) -> float:
        p = max(0.001, min(0.999, p))
        return math.log(p / (1 - p))

    @staticmethod
    def kelly_fraction(p: float, b: float) -> float:
        """Full Kelly: f* = (p*b - q) / b"""
        q = 1 - p
        if b <= 0:
            return 0.0
        return max(0.0, (p * b - q) / b)

    @staticmethod
    def brier_score(p_vals: list, outcomes: list) -> float:
        if not p_vals or len(p_vals) != len(outcomes):
            return 0.25
        return sum((p - o) ** 2 for p, o in zip(p_vals, outcomes)) / len(p_vals)

    @staticmethod
    def rolling_std(values: deque) -> float:
        if len(values) < 2:
            return 0.01
        return statistics.stdev(list(values))

    @staticmethod
    def realised_vol_annualised(prices: deque) -> float:
        prices = list(prices)
        if len(prices) < 2:
            return 0.0
        returns = [
            math.log(prices[i] / prices[i - 1])
            for i in range(1, len(prices))
            if prices[i - 1] > 0 and prices[i] > 0
        ]
        if not returns:
            return 0.0
        std = statistics.stdev(returns) if len(returns) > 1 else 0.0
        return std * math.sqrt(60 * 60 * 24 * 365)  # Annualise tick vol

    @staticmethod
    def vpin(buy_vol: float, sell_vol: float, total_vol: float) -> float:
        if total_vol <= 0:
            return 0.0
        return abs(buy_vol - sell_vol) / total_vol

    @staticmethod
    def weighted_consensus(logits: list, weights: list) -> float:
        total_w = sum(weights)
        if total_w == 0:
            return 0.0
        return sum(l * w for l, w in zip(logits, weights)) / total_w


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 5 — DATA FEED LAYER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DataFeedManager:
    """
    Manages all incoming data streams.
    LIVE_MODE = False → all feeds use realistic mock data.
    LIVE_MODE = True  → real websocket/REST connections.
    """

    LIVE_MODE = False

    def __init__(self, config: EngineConfig, state: MarketState):
        self.cfg   = config
        self.state = state
        self._log  = logging.getLogger("DataFeed")

    # ── BTC Price Feed (Binance aggTrade websocket) ───────────────
    async def start_btc_feed(self):
        if not self.LIVE_MODE:
            await self._mock_btc_feed()
            return
        if not HAS_WEBSOCKETS:
            self._log.error("websockets required for live BTC feed")
            return
        url = f"{self.cfg.binance_ws}/btcusdt@trade"
        async for ws in websockets.connect(url):
            try:
                async for msg in ws:
                    data = json.loads(msg)
                    self.state.btc_spot = float(data["p"])
                    self.state.btc_price_history.append(self.state.btc_spot)
            except Exception as e:
                self._log.warning(f"BTC feed reconnect: {e}")
                await asyncio.sleep(1)

    async def _mock_btc_feed(self):
        import random
        price = 82_000.0
        while True:
            price += random.gauss(0, price * 0.0003)
            self.state.btc_spot = price
            self.state.btc_price_history.append(price)
            self.state.last_updated = time.time()
            await asyncio.sleep(0.1)

    # ── CVD Feed (Binance aggTrade — buy/sell split) ──────────────
    async def start_cvd_feed(self):
        if not self.LIVE_MODE:
            await self._mock_cvd_feed()
            return
        if not HAS_WEBSOCKETS:
            return
        url = f"{self.cfg.binance_ws}/btcusdt@aggTrade"
        async for ws in websockets.connect(url):
            try:
                async for msg in ws:
                    data  = json.loads(msg)
                    qty   = float(data["q"])
                    delta = -qty if data["m"] else qty  # m=True means seller is maker
                    self.state.cvd_5min_ago = self.state.cvd_now
                    self.state.cvd_now     += delta
                    self.state.cvd_history.append(self.state.cvd_now)
            except Exception as e:
                self._log.warning(f"CVD feed reconnect: {e}")
                await asyncio.sleep(1)

    async def _mock_cvd_feed(self):
        import random
        cvd = 0.0
        while True:
            cvd += random.gauss(0, 8)
            self.state.cvd_5min_ago = self.state.cvd_now
            self.state.cvd_now      = cvd
            self.state.cvd_history.append(cvd)
            await asyncio.sleep(1)

    # ── Funding Rate + OI (Binance Futures REST) ──────────────────
    async def poll_funding_and_oi(self):
        while True:
            if self.LIVE_MODE and HAS_AIOHTTP:
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(
                            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
                        ) as r:
                            d = await r.json()
                            self.state.funding_rate_8hr = float(d.get("lastFundingRate", 0))

                        async with s.get(
                            "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
                        ) as r:
                            d = await r.json()
                            self.state.oi_5min_ago = self.state.oi_now
                            self.state.oi_now      = float(d.get("openInterest", self.state.oi_now))
                except Exception as e:
                    self._log.warning(f"Funding/OI poll error: {e}")
            else:
                import random
                self.state.funding_rate_8hr = random.gauss(0.0001, 0.00005)
                self.state.oi_5min_ago      = self.state.oi_now
                self.state.oi_now          += random.gauss(0, 1e6)
            await asyncio.sleep(60)

    # ── Deribit Options (IV + skew) ───────────────────────────────
    async def poll_deribit(self):
        while True:
            if self.LIVE_MODE and HAS_WEBSOCKETS:
                # Production: subscribe to Deribit ticker for ATM contract
                # and compute 25-delta skew from active contracts
                pass
            else:
                import random
                self.state.iv_atm      = 0.60 + random.gauss(0, 0.05)
                self.state.iv_25d_call = self.state.iv_atm + random.gauss(0.02, 0.01)
                self.state.iv_25d_put  = self.state.iv_atm + random.gauss(0.01, 0.01)
            await asyncio.sleep(30)

    # ── Realised Volatility (computed from price history) ─────────
    async def compute_realised_vol(self):
        while True:
            self.state.iv_1min_realised = MathUtils.realised_vol_annualised(
                self.state.btc_price_history
            )
            await asyncio.sleep(5)

    # ── Polymarket CLOB (websocket order book) ────────────────────
    async def start_polymarket_feed(self, market_id: str = ""):
        if not self.LIVE_MODE:
            await self._mock_poly_feed()
            return
        if not HAS_WEBSOCKETS:
            return
        payload = json.dumps({
            "auth": {"apiKey": self.cfg.polymarket_api_key},
            "type": "subscribe", "channel": "market",
            "markets": [market_id],
        })
        async for ws in websockets.connect(self.cfg.polymarket_clob_ws):
            try:
                await ws.send(payload)
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("channel") == "book":
                        bids = data.get("bids", [])
                        asks = data.get("asks", [])
                        if bids:
                            self.state.poly_yes_bid = float(bids[0][0])
                        if asks:
                            self.state.poly_yes_ask = float(asks[0][0])
                        self.state.poly_no_bid = round(1 - self.state.poly_yes_ask, 4)
                        self.state.poly_no_ask = round(1 - self.state.poly_yes_bid, 4)
            except Exception as e:
                self._log.warning(f"Poly feed reconnect: {e}")
                await asyncio.sleep(1)

    async def _mock_poly_feed(self):
        import random
        while True:
            mid = max(0.05, min(0.95, 0.50 + random.gauss(0, 0.08)))
            self.state.poly_yes_bid = round(mid - 0.005, 4)
            self.state.poly_yes_ask = round(mid + 0.005, 4)
            self.state.poly_no_bid  = round(1 - self.state.poly_yes_ask, 4)
            self.state.poly_no_ask  = round(1 - self.state.poly_yes_bid, 4)
            await asyncio.sleep(2)

    # ── Kalshi REST (2s poll) ─────────────────────────────────────
    async def poll_kalshi(self, ticker: str = ""):
        while True:
            if self.LIVE_MODE and HAS_AIOHTTP and ticker:
                try:
                    url = f"{self.cfg.kalshi_api}/markets/{ticker}/orderbook"
                    headers = {"Authorization": f"Bearer {self.cfg.kalshi_api_key}"}
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url, headers=headers) as r:
                            data = await r.json()
                            ob = data.get("orderbook", {})
                            yes_bids = ob.get("yes", {}).get("bids", [])
                            yes_asks = ob.get("yes", {}).get("asks", [])
                            if yes_bids:
                                self.state.kalshi_yes_bid = float(yes_bids[0][0]) / 100
                            if yes_asks:
                                self.state.kalshi_yes_ask = float(yes_asks[0][0]) / 100
                except Exception as e:
                    self._log.warning(f"Kalshi poll error: {e}")
            else:
                import random
                mid = self.state.poly_yes_bid + random.uniform(-0.04, 0.04)
                mid = max(0.05, min(0.95, mid))
                self.state.kalshi_yes_bid = round(mid - 0.008, 4)
                self.state.kalshi_yes_ask = round(mid + 0.008, 4)
            await asyncio.sleep(2)

    # ── Liquidation Feed (Binance) ────────────────────────────────
    async def start_liquidation_feed(self):
        if not self.LIVE_MODE:
            await self._mock_liq_feed()
            return
        if not HAS_WEBSOCKETS:
            return
        url = f"{self.cfg.binance_ws}/btcusdt@forceOrder"
        async for ws in websockets.connect(url):
            try:
                async for msg in ws:
                    data    = json.loads(msg)
                    o       = data.get("o", {})
                    usd_val = float(o.get("q", 0)) * float(o.get("p", 0))
                    # Accumulate (reset logic should be on 60s timer in production)
                    self.state.liq_volume_1min += usd_val
            except Exception as e:
                self._log.warning(f"Liq feed reconnect: {e}")
                await asyncio.sleep(1)

    async def _mock_liq_feed(self):
        import random
        while True:
            # Simulate occasional cascade
            self.state.liq_volume_1min = (
                random.uniform(20e6, 80e6) if random.random() < 0.02
                else random.uniform(0, 5e6)
            )
            await asyncio.sleep(5)

    # ── Oil Price (CL1 — mock; connect to broker feed live) ───────
    async def poll_oil_price(self):
        while True:
            if not self.LIVE_MODE:
                import random
                if self.state.oil_price_1hr_ago == 0:
                    self.state.oil_price_1hr_ago = 110.0
                    self.state.oil_price_now      = 110.0
                else:
                    self.state.oil_price_1hr_ago = self.state.oil_price_now
                    self.state.oil_price_now     += random.gauss(0, 0.5)
            await asyncio.sleep(60)

    # ── IBIT Options PCR (CBOE — mock) ────────────────────────────
    async def poll_ibit_options(self):
        while True:
            if not self.LIVE_MODE:
                import random
                self.state.ibit_pcr            = random.uniform(0.3, 2.5)
                self.state.ibit_net_flow_30min = random.gauss(0, 30e6)
            await asyncio.sleep(60)

    # ── CME Basis + Max Pain (mock) ───────────────────────────────
    async def poll_cme(self):
        while True:
            if not self.LIVE_MODE:
                import random
                self.state.cme_basis           = random.uniform(0.01, 0.05)
                self.state.cme_max_pain        = self.state.btc_spot * random.uniform(0.99, 1.01)
                self.state.cme_expiry_minutes  = random.uniform(10, 480)
            await asyncio.sleep(300)

    async def start_all(self):
        await asyncio.gather(
            self.start_btc_feed(),
            self.start_cvd_feed(),
            self.poll_funding_and_oi(),
            self.poll_deribit(),
            self.compute_realised_vol(),
            self.start_polymarket_feed(),
            self.poll_kalshi(),
            self.start_liquidation_feed(),
            self.poll_oil_price(),
            self.poll_ibit_options(),
            self.poll_cme(),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 6 — REGIME CLASSIFIER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RegimeClassifier:
    """Priority-ordered regime detection. First match wins."""

    def __init__(self, config: EngineConfig):
        self.cfg  = config
        self._log = logging.getLogger("Regime")

    def classify(
        self, state: MarketState, portfolio: PortfolioState
    ) -> tuple[RegimeMode, float, StreamMode]:

        # 1. Liquidation Cascade
        if state.liq_volume_1min >= self.cfg.cascade_halt_usd:
            self._log.warning(f"CASCADE HALT: liq=${state.liq_volume_1min/1e6:.1f}M/min")
            return RegimeMode.CASCADE, self.cfg.alpha_cascade, StreamMode.ARB_MM_ONLY

        # 2. Oil Shock / Geopolitical
        if state.oil_price_1hr_ago > 0:
            oil_chg = abs(
                (state.oil_price_now - state.oil_price_1hr_ago) / state.oil_price_1hr_ago
            )
            if oil_chg >= self.cfg.oil_shock_pct_1hr:
                self._log.warning(f"GEO_RISK: oil Δ={oil_chg:.2%}")
                return RegimeMode.GEO_RISK, self.cfg.alpha_geo_risk, StreamMode.ARB_MM_ONLY

        # 3. Volatility Spike Halt
        if state.iv_1min_realised > self.cfg.vol_spike_halt:
            self._log.warning(f"VOL SPIKE: {state.iv_1min_realised:.0%}")
            return RegimeMode.NORMAL, 0.0, StreamMode.ARB_MM_ONLY

        # 4. CME Strike Pin
        if (
            state.btc_spot > 0 and state.cme_max_pain > 0
            and state.cme_expiry_minutes <= self.cfg.pin_time_window_min
        ):
            dist = abs(state.btc_spot - state.cme_max_pain) / state.btc_spot
            if dist < self.cfg.pin_distance_tight:
                self._log.info(f"CME PIN: dist={dist:.2%}")
                return RegimeMode.CME_PIN, self.cfg.alpha_cme_pin, StreamMode.ALL

        # 5. IBIT Creation Confirmed
        if state.ibit_net_flow_30min > 50e6:
            return RegimeMode.NORMAL, self.cfg.alpha_ibit_confirm, StreamMode.ALL

        return RegimeMode.NORMAL, self.cfg.alpha_base, StreamMode.ALL


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 7 — STREAM 1: DIRECTIONAL SIGNAL GENERATOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DirectionalSignalGenerator:
    """
    7 independent signal components aggregated into p_model.
    Weights are re-normalised at runtime by inverse Brier Score.
    """

    BASE_WEIGHTS = {
        "momentum":    0.20,
        "cvd":         0.22,
        "oi_delta":    0.15,
        "funding":     0.10,
        "skew":        0.10,
        "ibit_gamma":  0.13,
        "cme_pin":     0.10,
    }

    def __init__(self, config: EngineConfig):
        self.cfg  = config
        self._log = logging.getLogger("SignalGen")

    def _momentum(self, state: MarketState) -> float:
        prices = list(state.btc_price_history)
        lk = min(300, len(prices))           # Up to 5min of 1s ticks
        if lk < 10:
            return 0.0
        mom = (prices[-1] - prices[-lk]) / prices[-lk]
        if mom > 0.0015:  return +0.06
        if mom < -0.0015: return -0.06
        return 0.0

    def _cvd(self, state: MarketState) -> float:
        delta = state.cvd_now - state.cvd_5min_ago
        if delta > 0 and state.cvd_now > 0:  return +0.05
        if delta < 0 and state.cvd_now < 0:  return -0.05
        return 0.0

    def _funding(self, state: MarketState) -> float:
        fr = state.funding_rate_8hr
        if fr > 0.0001:   return -0.03   # Longs crowded → bearish
        if fr < -0.0001:  return +0.03   # Shorts crowded → bullish
        return 0.0

    def _oi_delta(self, state: MarketState) -> float:
        prices = list(state.btc_price_history)
        if len(prices) < 2:
            return 0.0
        price_up = prices[-1] > prices[-2]
        oi_up    = state.oi_now > state.oi_5min_ago
        if price_up and oi_up:      return +0.04   # Confirmed long pressure
        if price_up and not oi_up:  return -0.02   # Short covering, fade
        if not price_up and oi_up:  return -0.04   # Confirmed short pressure
        return -0.02                                # Liq risk

    def _skew(self, state: MarketState) -> float:
        skew = state.iv_25d_call - state.iv_25d_put
        if skew > 0.02:   return +0.03
        if skew < -0.02:  return -0.03
        return 0.0

    def _ibit_gamma(self, state: MarketState) -> float:
        """
        Call-heavy PCR → dealers structurally short gamma → amplify upside.
        Put-heavy PCR  → dealers short gamma on downside → amplify drops.
        """
        prices = list(state.btc_price_history)
        if len(prices) < 2:
            return 0.0
        trend_up = prices[-1] > prices[-2]
        pcr      = state.ibit_pcr
        if pcr < self.cfg.ibit_pcr_bullish:
            return +0.02 if trend_up else 0.0
        if pcr > self.cfg.ibit_pcr_bearish:
            return -0.02 if not trend_up else 0.0
        return 0.0

    def _cme_pin(self, state: MarketState) -> float:
        """
        Near expiry, price gravitates toward max pain strike.
        Far from pin AND expiry close → dealer hedging accelerates pin pull.
        """
        if state.cme_max_pain <= 0 or state.btc_spot <= 0:
            return 0.0
        if state.cme_expiry_minutes > self.cfg.pin_time_window_min:
            return 0.0
        dist = (state.btc_spot - state.cme_max_pain) / state.btc_spot
        if abs(dist) < self.cfg.pin_distance_tight:
            # At pin — strong mean-reversion resistance
            return -0.08 if dist > 0 else +0.08
        if abs(dist) > self.cfg.pin_distance_wide:
            # Being pulled toward pin
            return -0.04 if dist > 0 else +0.04
        return 0.0

    def _exhaustion_penalty(self, state: MarketState) -> float:
        """Penalise when 3 consecutive candles align but CVD diverges."""
        prices = list(state.btc_price_history)
        if len(prices) < 180:
            return 0.0
        c1 = prices[-60]  > prices[-120]
        c2 = prices[-1]   > prices[-60]
        c3 = prices[-120] > prices[-180]
        all_same = (c1 == c2 == c3)
        cvd_div  = (
            (c1 and state.cvd_now < state.cvd_5min_ago) or
            (not c1 and state.cvd_now > state.cvd_5min_ago)
        )
        return -0.05 if (all_same and cvd_div) else 0.0

    def generate(
        self, state: MarketState, portfolio: PortfolioState
    ) -> Optional[Signal]:

        sig          = Signal()
        sig.mom_adj        = self._momentum(state)
        sig.cvd_adj        = self._cvd(state)
        sig.funding_adj    = self._funding(state)
        sig.oi_adj         = self._oi_delta(state)
        sig.skew_adj       = self._skew(state)
        sig.ibit_gamma_adj = self._ibit_gamma(state)
        sig.cme_pin_adj    = self._cme_pin(state)

        # Dynamic weight re-normalisation by inverse Brier Score
        w = dict(self.BASE_WEIGHTS)
        for key in ["momentum", "cvd", "oi_delta", "funding", "skew"]:
            bs   = portfolio.brier_scores.get(key, 0.25)
            w[key] = 1.0 / max(bs, 0.01)
        total_w = sum(w.values())
        nw      = {k: v / total_w for k, v in w.items()}

        raw_adj = (
            sig.mom_adj        * nw["momentum"]
            + sig.cvd_adj      * nw["cvd"]
            + sig.oi_adj       * nw["oi_delta"]
            + sig.funding_adj  * nw["funding"]
            + sig.skew_adj     * nw["skew"]
            + sig.ibit_gamma_adj * nw["ibit_gamma"]
            + sig.cme_pin_adj  * nw["cme_pin"]
        )

        exhaustion = self._exhaustion_penalty(state)
        sig.p_model = max(0.05, min(0.95, 0.50 + raw_adj + exhaustion))

        # Consensus via weighted log-odds
        p_sources = [
            max(0.05, min(0.95, 0.50 + adj))
            for adj in [sig.mom_adj, sig.cvd_adj, sig.oi_adj, sig.funding_adj, sig.skew_adj]
        ]
        logits  = [MathUtils.logit(p) for p in p_sources]
        weights = [nw["momentum"], nw["cvd"], nw["oi_delta"], nw["funding"], nw["skew"]]
        sig.consensus_score = MathUtils.weighted_consensus(logits, weights)

        if abs(sig.consensus_score) < self.cfg.consensus_threshold:
            return None

        sig.direction = Direction.UP if sig.p_model > 0.50 else Direction.DOWN
        return sig


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 8 — STAGE 2: EDGE & CALIBRATION
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EdgeCalibrator:

    def __init__(self, config: EngineConfig):
        self.cfg  = config
        self._log = logging.getLogger("EdgeCalib")

    def evaluate(
        self,
        signal: Signal,
        state: MarketState,
        portfolio: PortfolioState,
        regime: RegimeMode,
    ) -> tuple[float, float, float, bool]:
        """Returns (edge, delta_z, net_ev, passes)."""

        p_market = (state.poly_yes_bid + state.poly_yes_ask) / 2.0
        if p_market <= 0:
            return 0, 0, 0, False

        edge = signal.p_model - p_market

        # Regime-adjusted minimum edge
        if regime == RegimeMode.GEO_RISK:
            edge_min = self.cfg.edge_min_geo
        elif regime == RegimeMode.CME_PIN:
            edge_min = self.cfg.edge_min_pin
        elif (self.cfg.cascade_elevated_usd
              <= state.liq_volume_1min < self.cfg.cascade_halt_usd):
            edge_min = self.cfg.edge_min_cascade_hi
        else:
            edge_min = self.cfg.edge_min_base

        if abs(edge) < edge_min:
            return edge, 0, 0, False

        net_ev = FeeModel.net_ev(signal.p_model, p_market, is_maker=True)
        if net_ev <= 0:
            return edge, 0, net_ev, False

        # Mispricing Z-score
        portfolio.edge_history.append(edge)
        sigma   = MathUtils.rolling_std(portfolio.edge_history)
        delta_z = edge / sigma if sigma > 0 else 0.0

        if abs(delta_z) < 1.96:
            return edge, delta_z, net_ev, False

        return edge, delta_z, net_ev, True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 9 — STAGE 3: TOXICITY FILTERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ToxicityFilter:

    def __init__(self, config: EngineConfig):
        self.cfg                     = config
        self._vol_spike_active       = False
        self._vol_resume_count       = 0
        self._log                    = logging.getLogger("ToxFilter")

    def check(
        self, state: MarketState, signal: Signal, portfolio: PortfolioState
    ) -> tuple[bool, str]:
        """Returns (passes, reason_if_blocked)."""

        # VPIN (approximate from CVD)
        total_vol  = abs(state.cvd_now) + 1e-9
        buy_vol    = max(0, state.cvd_now)
        sell_vol   = max(0, -state.cvd_now)
        vpin       = MathUtils.vpin(buy_vol, sell_vol, total_vol)
        vpin_limit = 0.80 if state.ibit_net_flow_30min > 50e6 else self.cfg.vpin_threshold
        if vpin > vpin_limit:
            return False, f"VPIN={vpin:.2f}"

        # Spread
        spread = state.poly_yes_ask - state.poly_yes_bid
        if spread > self.cfg.spread_max:
            return False, f"Spread={spread:.3f}"

        # Volatility spike
        iv = state.iv_1min_realised
        if iv > self.cfg.vol_spike_halt:
            self._vol_spike_active = True
            self._vol_resume_count = 0
            return False, f"VolSpike={iv:.0%}"
        if self._vol_spike_active:
            if iv < self.cfg.vol_spike_resume:
                self._vol_resume_count += 1
                if self._vol_resume_count >= 2:
                    self._vol_spike_active = False
                    self._log.info("Vol spike cleared")
                else:
                    return False, f"VolSpike cooling ({self._vol_resume_count}/2)"
            else:
                self._vol_resume_count = 0
                return False, f"Vol still elevated={iv:.0%}"

        # Direction streak pause
        now = time.time()
        if signal.direction == Direction.UP and portfolio.up_paused_until > now:
            return False, f"UP streak pause ({(portfolio.up_paused_until-now)/60:.1f}min)"
        if signal.direction == Direction.DOWN and portfolio.down_paused_until > now:
            return False, f"DOWN streak pause ({(portfolio.down_paused_until-now)/60:.1f}min)"

        return True, ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 10 — STAGE 4+5: KELLY SIZER + RISK GATES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KellySizer:

    def __init__(self, config: EngineConfig):
        self.cfg  = config
        self._log = logging.getLogger("Kelly")

    def size(
        self,
        signal: Signal,
        state: MarketState,
        portfolio: PortfolioState,
        alpha: float,
    ) -> tuple[float, float, bool]:
        """Returns (bet_size_usdc, kelly_f, passes)."""

        bankroll = portfolio.bankroll
        b_stream = bankroll * self.cfg.stream1_alloc
        p_market = (state.poly_yes_bid + state.poly_yes_ask) / 2.0
        if p_market <= 0:
            return 0, 0, False

        b      = (1.0 / p_market) - 1.0
        f_star = MathUtils.kelly_fraction(signal.p_model, b)
        f      = alpha * f_star
        bet    = min(f * b_stream, self.cfg.max_bet_pct * bankroll)

        if bet <= 0:
            return 0, f, False

        # Risk Gate 1: Daily VaR
        if bankroll > 0:
            loss_pct = (portfolio.daily_start_bankroll - bankroll) / bankroll
            if loss_pct > self.cfg.daily_var_limit:
                self._log.warning(f"Daily VaR breached: {loss_pct:.2%}")
                return 0, f, False

        # Risk Gate 2: Max Drawdown
        if portfolio.peak_bankroll > 0:
            mdd = (portfolio.peak_bankroll - bankroll) / portfolio.peak_bankroll
            if mdd > self.cfg.max_drawdown:
                self._log.critical(f"MDD HALT: {mdd:.2%}")
                return 0, f, False

        # Risk Gate 3: Exposure Ratio
        open_sum = sum(o.get("size", 0) for o in portfolio.open_bets)
        er       = open_sum / bankroll if bankroll > 0 else 0
        if er > self.cfg.max_exposure_ratio:
            self._log.warning(f"ER breached: {er:.2%}")
            return 0, f, False

        # Risk Gate 4: Same-direction correlation block
        for ob in portfolio.open_bets:
            if ob.get("direction") == signal.direction.value:
                return 0, f, False

        # Risk Gate 5: Max 2 simultaneous positions
        if len(portfolio.open_bets) >= 2:
            return 0, f, False

        return bet, f, True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 11 — STREAM 2: ARBITRAGE ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ArbitrageEngine:

    def __init__(self, config: EngineConfig, portfolio: PortfolioState):
        self.cfg       = config
        self.portfolio = portfolio
        self._log      = logging.getLogger("Arb")

    def scan_intra_arb(self, state: MarketState) -> Optional[TradeOrder]:
        """
        YES + NO combined cost < $0.985 → guaranteed profit at resolution.
        """
        cost = state.poly_yes_ask + state.poly_no_ask
        if cost <= 0 or cost >= self.cfg.intra_arb_threshold:
            return None

        profit = 1.0 - cost
        size   = min(
            self.portfolio.bankroll * 0.05,
            self.portfolio.bankroll * self.cfg.stream2_alloc,
        )
        self._log.info(
            f"INTRA-ARB: YES={state.poly_yes_ask:.3f} + "
            f"NO={state.poly_no_ask:.3f} = {cost:.3f} → ${profit:.3f}/share profit"
        )
        return TradeOrder(
            stream=2, venue="polymarket", direction=Direction.UP, leg="YES",
            size_usdc=size, limit_price=state.poly_yes_ask,
            p_model=1.0, p_market=state.poly_yes_ask, edge=profit, ev=profit,
        )

    def scan_cross_arb(self, state: MarketState) -> Optional[list]:
        """
        Polymarket vs Kalshi: capture spread after fee adjustment.
        Polymarket leads price discovery — fade Kalshi toward Poly mid.
        """
        if state.kalshi_yes_bid <= 0 or state.poly_yes_bid <= 0:
            return None

        poly_mid   = (state.poly_yes_bid + state.poly_yes_ask) / 2.0
        kalshi_mid = (state.kalshi_yes_bid + state.kalshi_yes_ask) / 2.0
        raw_spread = abs(poly_mid - kalshi_mid)
        net_spread = raw_spread - FeeModel.taker_fee(poly_mid) - FeeModel.taker_fee(kalshi_mid)

        if net_spread < self.cfg.xarb_min_net_spread:
            return None

        self._log.info(
            f"CROSS-ARB: Poly={poly_mid:.3f} Kalshi={kalshi_mid:.3f} "
            f"net={net_spread:.4f}"
        )
        size = (self.portfolio.bankroll * self.cfg.stream2_alloc) / 2.0

        if poly_mid > kalshi_mid:
            # Buy YES on Kalshi (cheaper), sell (NO) on Polymarket
            return [
                TradeOrder(stream=2, venue="kalshi", direction=Direction.UP, leg="YES",
                           size_usdc=size, limit_price=kalshi_mid,
                           p_model=poly_mid, p_market=kalshi_mid,
                           edge=net_spread, ev=net_spread),
                TradeOrder(stream=2, venue="polymarket", direction=Direction.DOWN, leg="NO",
                           size_usdc=size, limit_price=round(1-poly_mid, 4),
                           p_model=1-kalshi_mid, p_market=1-poly_mid,
                           edge=net_spread, ev=net_spread),
            ]
        else:
            return [
                TradeOrder(stream=2, venue="polymarket", direction=Direction.UP, leg="YES",
                           size_usdc=size, limit_price=poly_mid,
                           p_model=kalshi_mid, p_market=poly_mid,
                           edge=net_spread, ev=net_spread),
                TradeOrder(stream=2, venue="kalshi", direction=Direction.DOWN, leg="NO",
                           size_usdc=size, limit_price=round(1-kalshi_mid, 4),
                           p_model=1-poly_mid, p_market=1-kalshi_mid,
                           edge=net_spread, ev=net_spread),
            ]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 12 — STREAM 3: MARKET MAKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class MarketMaker:
    """
    Posts two-sided limit orders around market mid when no strong signal.
    Earns: bid/ask spread + Polymarket Q-score USDC rebates.
    Cancels all quotes immediately when directional signal fires.
    Expected P&L per round-trip: ~1.4% (spread 1.0% + rebate 0.4%).
    """

    def __init__(self, config: EngineConfig, portfolio: PortfolioState):
        self.cfg           = config
        self.portfolio     = portfolio
        self.active_quotes: list[TradeOrder] = []
        self._log          = logging.getLogger("MM")

    def should_activate(self, signal: Optional[Signal]) -> bool:
        return signal is None or abs(signal.p_model - 0.50) < 0.04

    def should_cancel(self, signal: Optional[Signal]) -> bool:
        return signal is not None and abs(signal.p_model - 0.50) >= self.cfg.mm_signal_cancel_edge

    def generate_quotes(self, state: MarketState) -> tuple[TradeOrder, TradeOrder]:
        mid  = max(0.05, min(0.95, (state.poly_yes_bid + state.poly_yes_ask) / 2.0))
        size = self.portfolio.bankroll * self.cfg.mm_size_pct
        bid  = round(mid - self.cfg.mm_quote_offset, 4)
        ask  = round(mid + self.cfg.mm_quote_offset, 4)
        est_pnl = (self.cfg.mm_quote_offset * 2 + FeeModel.maker_rebate_estimate(mid)) * size
        self._log.debug(f"MM quotes bid={bid} ask={ask} est_pnl=${est_pnl:.4f}")

        bid_order = TradeOrder(stream=3, venue="polymarket", direction=Direction.UP,
                               leg="YES", size_usdc=size, limit_price=bid,
                               p_model=mid, p_market=mid)
        ask_order = TradeOrder(stream=3, venue="polymarket", direction=Direction.DOWN,
                               leg="NO",  size_usdc=size, limit_price=round(1-ask, 4),
                               p_model=mid, p_market=mid)
        return bid_order, ask_order


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 13 — STREAM 4: WHALE TRACKER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class WhaleBet:
    wallet_address: str
    direction: Direction
    size_usdc: float
    wallet_age_hours: float
    timestamp: float = field(default_factory=time.time)

class WhaleTracker:

    def __init__(self, config: EngineConfig, portfolio: PortfolioState):
        self.cfg       = config
        self.portfolio = portfolio
        self._log      = logging.getLogger("Whale")

    def severity_score(self, bet: WhaleBet) -> float:
        return bet.size_usdc / max(bet.wallet_age_hours, 0.1)

    def evaluate(
        self, bet: WhaleBet, signal: Optional[Signal]
    ) -> Optional[TradeOrder]:

        if bet.size_usdc < self.cfg.whale_bet_min:
            return None

        score    = self.severity_score(bet)
        db_entry = self.portfolio.whale_db.get(bet.wallet_address, {})
        tier1    = db_entry.get("winrate", 0) >= self.cfg.whale_tier1_winrate

        self._log.info(
            f"WHALE: {bet.wallet_address[:8]}... "
            f"${bet.size_usdc:.0f} {bet.direction.value} "
            f"score={score:.0f} tier1={tier1}"
        )

        # Don't copy if our signal strongly contradicts
        if signal is not None and abs(signal.p_model - 0.50) > 0.15:
            if signal.direction != bet.direction:
                self._log.info("Whale copy blocked — model contradiction")
                return None

        if tier1:
            copy_pct = self.cfg.whale_tier1_copy_pct
        elif score >= 500:
            copy_pct = self.cfg.whale_copy_pct
        elif score >= 200:
            copy_pct = 0.10
        else:
            self._update_db(bet)
            return None

        budget = self.portfolio.bankroll * self.cfg.stream4_alloc
        size   = min(copy_pct * bet.size_usdc, budget * 0.5)

        self._update_db(bet)

        return TradeOrder(
            stream=4, venue="polymarket",
            direction=bet.direction,
            leg="YES" if bet.direction == Direction.UP else "NO",
            size_usdc=size, limit_price=0.50,
            p_model=0.55, p_market=0.50, edge=0.05, ev=0.05,
        )

    def _update_db(self, bet: WhaleBet):
        addr = bet.wallet_address
        if addr not in self.portfolio.whale_db:
            self.portfolio.whale_db[addr] = {"bets": 0, "wins": 0, "winrate": 0.5}
        self.portfolio.whale_db[addr]["bets"] += 1

    async def poll_on_chain(self, market_id: str) -> list[WhaleBet]:
        """Query Polymarket on-chain data via Bitquery GraphQL (Polygon)."""
        if not DataFeedManager.LIVE_MODE or not HAS_AIOHTTP:
            return []

        query = """
        query($market: String!) {
          ethereum(network: matic) {
            dexTrades(
              options: {limit: 100, desc: "block.timestamp.time"}
              smartContractAddress: {is: $market}
            ) {
              buyer { address }
              tradeAmount(in: USD)
            }
          }
        }
        """
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://graphql.bitquery.io",
                    json={"query": query, "variables": {"market": market_id}},
                    headers={"X-API-KEY": self.cfg.bitquery_api_key},
                ) as r:
                    data   = await r.json()
                    trades = data.get("data", {}).get("ethereum", {}).get("dexTrades", [])
                    return [
                        WhaleBet(
                            wallet_address=t["buyer"]["address"],
                            direction=Direction.UP,
                            size_usdc=float(t.get("tradeAmount", 0)),
                            wallet_age_hours=1.0,
                        )
                        for t in trades
                        if float(t.get("tradeAmount", 0)) >= self.cfg.whale_bet_min
                    ]
        except Exception as e:
            self._log.warning(f"Bitquery error: {e}")
            return []


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 14 — EXECUTION ENGINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExecutionEngine:
    """
    Paper mode (PAPER_MODE=True): simulates fills for testing.
    Live mode: submits post-only limit orders to CLOB APIs.
    """

    PAPER_MODE = True  # Always start in paper mode!

    def __init__(self, config: EngineConfig, portfolio: PortfolioState):
        self.cfg       = config
        self.portfolio = portfolio
        self._log      = logging.getLogger("Execution")

    async def submit_order(
        self, order: TradeOrder, state: MarketState
    ) -> Optional[TradeLog]:
        if self.PAPER_MODE:
            return await self._paper_fill(order)
        if order.venue == "polymarket":
            return await self._submit_polymarket(order)
        elif order.venue == "kalshi":
            return await self._submit_kalshi(order)
        return None

    async def _paper_fill(self, order: TradeOrder) -> Optional[TradeLog]:
        import random
        await asyncio.sleep(0.05)
        if random.random() > 0.95:   # 5% miss rate
            return None
        fill_price = order.limit_price + random.uniform(0, 0.002)
        self._log.info(
            f"PAPER │ S{order.stream} {order.venue:12} "
            f"{order.direction.value:4} {order.leg} "
            f"${order.size_usdc:>8.2f} @ {fill_price:.4f}  "
            f"edge={order.edge:.4f}"
        )
        self.portfolio.open_bets.append({
            "id":         id(order),
            "direction":  order.direction.value,
            "size":       order.size_usdc,
            "leg":        order.leg,
            "fill_price": fill_price,
        })
        return TradeLog(order=order, fill_price=fill_price, fill_time=time.time())

    async def _submit_polymarket(self, order: TradeOrder) -> Optional[TradeLog]:
        """Post-only limit order to Polymarket CLOB."""
        if not HAS_AIOHTTP:
            return None
        payload = {
            "market": order.market_id,
            "side":   order.leg,
            "price":  str(round(order.limit_price, 4)),
            "size":   str(round(order.size_usdc / max(order.limit_price, 0.01), 2)),
            "type":   "LIMIT",
            "time_in_force": "GTD",
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{self.cfg.polymarket_api}/order",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.cfg.polymarket_api_key}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    data = await r.json()
                    if data.get("status") == "matched":
                        return TradeLog(order=order,
                                        fill_price=float(data["price"]),
                                        fill_time=time.time())
        except Exception as e:
            self._log.error(f"Poly order error: {e}")
        return None

    async def _submit_kalshi(self, order: TradeOrder) -> Optional[TradeLog]:
        """Submit order to Kalshi REST API."""
        if not HAS_AIOHTTP:
            return None
        price_cents = int(round(order.limit_price * 100))
        payload = {
            "action":    "buy",
            "side":      order.leg.lower(),
            "ticker":    order.market_id,
            "count":     max(1, int(order.size_usdc)),
            "yes_price": price_cents if order.leg == "YES" else 100 - price_cents,
        }
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    f"{self.cfg.kalshi_api}/portfolio/orders",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.cfg.kalshi_api_key}"},
                    timeout=aiohttp.ClientTimeout(total=self.cfg.xarb_fill_timeout),
                ) as r:
                    data = await r.json()
                    if "order" in data:
                        return TradeLog(order=order,
                                        fill_price=price_cents/100,
                                        fill_time=time.time())
        except Exception as e:
            self._log.error(f"Kalshi order error: {e}")
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 15 — RESOLUTION MANAGER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ResolutionManager:

    def __init__(self, config: EngineConfig, portfolio: PortfolioState):
        self.cfg       = config
        self.portfolio = portfolio
        self._log      = logging.getLogger("Resolution")
        self._hist: dict = {}

    def on_resolve(self, trade: TradeLog, won: bool) -> float:
        pnl = trade.order.size_usdc if won else -trade.order.size_usdc

        self.portfolio.bankroll     += pnl
        self.portfolio.peak_bankroll = max(self.portfolio.peak_bankroll,
                                           self.portfolio.bankroll)
        self.portfolio.daily_pnl    += pnl

        # Remove from open bets
        self.portfolio.open_bets = [
            ob for ob in self.portfolio.open_bets if ob.get("id") != id(trade.order)
        ]

        # Streak tracking
        d = trade.order.direction
        if not won:
            if d == Direction.UP:
                self.portfolio.consecutive_losses_up += 1
                if self.portfolio.consecutive_losses_up >= self.cfg.streak_loss_pause_n:
                    self.portfolio.up_paused_until = time.time() + self.cfg.streak_pause_min * 60
                    self._log.warning(f"UP streak pause: {self.cfg.streak_loss_pause_n} losses")
            else:
                self.portfolio.consecutive_losses_down += 1
                if self.portfolio.consecutive_losses_down >= self.cfg.streak_loss_pause_n:
                    self.portfolio.down_paused_until = time.time() + self.cfg.streak_pause_min * 60
                    self._log.warning(f"DOWN streak pause")
        else:
            if d == Direction.UP:  self.portfolio.consecutive_losses_up = 0
            else:                  self.portfolio.consecutive_losses_down = 0

        # Brier score update
        outcome = 1.0 if won else 0.0
        self._update_brier("cvd",      trade.order.p_model, outcome)
        self._update_brier("momentum", trade.order.p_model, outcome)

        self._log.info(
            f"RESOLVED: {trade.order.direction.value} "
            f"{'WIN' if won else 'LOSS'} "
            f"PnL=${pnl:.2f} | Bankroll=${self.portfolio.bankroll:.2f}"
        )

        trade.pnl = pnl
        self.portfolio.resolved_trades.append(trade)
        return pnl

    def _update_brier(self, source: str, p: float, outcome: float):
        key = f"_h_{source}"
        if not hasattr(self, key):
            setattr(self, key, [])
        hist = getattr(self, key)
        hist.append((p, outcome))
        ps, os = zip(*hist[-50:])
        self.portfolio.brier_scores[source] = MathUtils.brier_score(list(ps), list(os))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 16 — PERFORMANCE MONITOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PerformanceMonitor:

    def __init__(self, portfolio: PortfolioState):
        self.portfolio      = portfolio
        self._daily_returns: deque = deque(maxlen=30)
        self._log           = logging.getLogger("Perf")

    def update_daily(self):
        if self.portfolio.daily_start_bankroll > 0:
            r = ((self.portfolio.bankroll - self.portfolio.daily_start_bankroll)
                 / self.portfolio.daily_start_bankroll)
            self._daily_returns.append(r)
        self.portfolio.daily_start_bankroll = self.portfolio.bankroll
        self.portfolio.daily_pnl = 0.0

    def sharpe(self, rf: float = 0.04/365) -> float:
        r = list(self._daily_returns)
        if len(r) < 2:
            return 0.0
        mu  = sum(r) / len(r)
        std = statistics.stdev(r)
        return (mu - rf) / std * math.sqrt(365) if std > 0 else 0.0

    def profit_factor(self) -> float:
        trades = self.portfolio.resolved_trades
        if not trades:
            return 0.0
        gp = sum(t.pnl for t in trades if t.pnl > 0)
        gl = abs(sum(t.pnl for t in trades if t.pnl < 0))
        return gp / max(gl, 0.01)

    def win_rate(self) -> float:
        trades = self.portfolio.resolved_trades
        if not trades:
            return 0.0
        return sum(1 for t in trades if t.pnl > 0) / len(trades)

    def print_dashboard(self):
        br   = self.portfolio.bankroll
        peak = self.portfolio.peak_bankroll
        mdd  = (peak - br) / peak if peak > 0 else 0
        print("\n" + "═" * 58)
        print("  ALPHA ENGINE  ──  LIVE DASHBOARD")
        print("═" * 58)
        print(f"  Bankroll        ${br:>10,.2f}")
        print(f"  Peak            ${peak:>10,.2f}")
        print(f"  MDD             {mdd:>10.2%}  [limit -8%]")
        print(f"  Daily PnL       ${self.portfolio.daily_pnl:>10,.2f}")
        print(f"  Sharpe (30d)    {self.sharpe():>10.2f}  [target >2.0]")
        print(f"  Profit Factor   {self.profit_factor():>10.2f}  [target >2.0]")
        print(f"  Win Rate        {self.win_rate():>10.2%}  [target >60%]")
        print(f"  Open Positions  {len(self.portfolio.open_bets):>10}")
        print(f"  Resolved        {len(self.portfolio.resolved_trades):>10}")
        print(f"  Regime          {self.portfolio.regime.value:>10}")
        print(f"  Alpha (Kelly ×) {self.portfolio.current_alpha:>10.2f}")
        print(f"  Brier Scores    {self.portfolio.brier_scores}")
        print("═" * 58 + "\n")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 17 — TRADE LOGGER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TradeLogger:

    def __init__(self, log_dir: str = "./logs"):
        import os
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._trade_path  = f"{log_dir}/trades_{ts}.jsonl"
        self._signal_path = f"{log_dir}/signals_{ts}.jsonl"
        self._drop_path   = f"{log_dir}/dropped_{ts}.jsonl"

    def log_trade(self, trade: TradeLog):
        self._w(self._trade_path, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stream": trade.order.stream, "venue": trade.order.venue,
            "direction": trade.order.direction.value, "leg": trade.order.leg,
            "size": trade.order.size_usdc, "fill": trade.fill_price,
            "p_model": trade.order.p_model, "p_market": trade.order.p_market,
            "edge": trade.order.edge, "delta_z": trade.order.delta_z,
            "ev": trade.order.ev, "kelly_f": trade.order.kelly_f, "pnl": trade.pnl,
        })

    def log_dropped(self, reason: str, signal: Optional[Signal], stage: int):
        self._w(self._drop_path, {
            "ts": datetime.now(timezone.utc).isoformat(), "stage": stage,
            "reason": reason,
            "p_model": signal.p_model if signal else None,
            "consensus": signal.consensus_score if signal else None,
        })

    def log_signal(self, sig: Signal, edge: float, dz: float):
        self._w(self._signal_path, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "p_model": sig.p_model, "direction": sig.direction.value,
            "consensus": sig.consensus_score, "edge": edge, "delta_z": dz,
            "mom": sig.mom_adj, "cvd": sig.cvd_adj, "funding": sig.funding_adj,
            "oi": sig.oi_adj, "skew": sig.skew_adj,
            "ibit": sig.ibit_gamma_adj, "pin": sig.cme_pin_adj,
        })

    @staticmethod
    def _w(path: str, r: dict):
        try:
            with open(path, "a") as f:
                f.write(json.dumps(r) + "\n")
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 18 — MAIN ORCHESTRATOR
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AlphaEngine:

    CYCLE_SECONDS = 60

    def __init__(self, config: Optional[EngineConfig] = None):
        self.cfg       = config or EngineConfig()
        self.state     = MarketState()
        self.portfolio = PortfolioState(
            bankroll             = self.cfg.total_bankroll,
            peak_bankroll        = self.cfg.total_bankroll,
            daily_start_bankroll = self.cfg.total_bankroll,
        )
        self.feeds      = DataFeedManager(self.cfg, self.state)
        self.regime     = RegimeClassifier(self.cfg)
        self.signal_gen = DirectionalSignalGenerator(self.cfg)
        self.edge_cal   = EdgeCalibrator(self.cfg)
        self.tox        = ToxicityFilter(self.cfg)
        self.kelly      = KellySizer(self.cfg)
        self.arb        = ArbitrageEngine(self.cfg, self.portfolio)
        self.mm         = MarketMaker(self.cfg, self.portfolio)
        self.whale      = WhaleTracker(self.cfg, self.portfolio)
        self.executor   = ExecutionEngine(self.cfg, self.portfolio)
        self.resolver   = ResolutionManager(self.cfg, self.portfolio)
        self.perf       = PerformanceMonitor(self.portfolio)
        self.logger     = TradeLogger()
        self._log       = logging.getLogger("Engine")
        self._cycle     = 0

    async def run_cycle(self):
        self._cycle += 1
        self._log.info(f"── Cycle {self._cycle} ──")

        # ── Regime ───────────────────────────────────────────────
        mode, alpha, s_mode = self.regime.classify(self.state, self.portfolio)
        self.portfolio.regime        = mode
        self.portfolio.current_alpha = alpha

        # ── Stream 2: Intra-arb (always active) ──────────────────
        intra = self.arb.scan_intra_arb(self.state)
        if intra:
            fill = await self.executor.submit_order(intra, self.state)
            if fill:
                self.logger.log_trade(fill)

        # ── Stream 2: Cross-arb ───────────────────────────────────
        cross = self.arb.scan_cross_arb(self.state)
        if cross:
            for fill in await asyncio.gather(*[
                self.executor.submit_order(o, self.state) for o in cross
            ]):
                if fill:
                    self.logger.log_trade(fill)

        # ── Directional suspended? ────────────────────────────────
        if s_mode == StreamMode.ARB_MM_ONLY:
            self._log.info(f"Directional suspended [{mode.value}]")
            self._tick_mm(signal=None)
            return

        # ── Stream 1: Directional ─────────────────────────────────
        signal = self.signal_gen.generate(self.state, self.portfolio)

        if signal is None:
            self.logger.log_dropped("Consensus gate", None, 1)
        else:
            self._log.info(
                f"SIGNAL {signal.direction.value}  "
                f"p={signal.p_model:.4f}  C={signal.consensus_score:.2f}"
            )
            await self._directional_pipeline(signal, alpha)

        # ── Stream 3: Market Making ───────────────────────────────
        self._tick_mm(signal)

    async def _directional_pipeline(self, signal: Signal, alpha: float):
        # Stage 2: Edge
        edge, dz, ev, ok = self.edge_cal.evaluate(signal, self.state,
                                                   self.portfolio, self.portfolio.regime)
        self.logger.log_signal(signal, edge, dz)
        if not ok:
            self.logger.log_dropped(f"Edge {edge:.4f} δ={dz:.2f}", signal, 2)
            return

        # Stage 3: Toxicity
        passes, reason = self.tox.check(self.state, signal, self.portfolio)
        if not passes:
            self.logger.log_dropped(reason, signal, 3)
            return

        # Stage 4+5: Kelly + Risk
        bet, f, ok = self.kelly.size(signal, self.state, self.portfolio, alpha)
        if not ok:
            self.logger.log_dropped("Risk gates", signal, 5)
            return

        # Stage 6: Execute
        p_market = (self.state.poly_yes_bid + self.state.poly_yes_ask) / 2.0
        leg      = "YES" if signal.direction == Direction.UP else "NO"
        order    = TradeOrder(
            stream=1, venue="polymarket",
            direction=signal.direction, leg=leg,
            size_usdc=bet, limit_price=p_market + 0.005,
            p_model=signal.p_model, p_market=p_market,
            edge=edge, delta_z=dz, ev=ev,
            consensus_score=signal.consensus_score, kelly_f=f,
        )
        fill = await self.executor.submit_order(order, self.state)
        if fill:
            self.logger.log_trade(fill)
        else:
            self.logger.log_dropped("Unfilled", signal, 6)

    def _tick_mm(self, signal: Optional[Signal]):
        if self.mm.should_cancel(signal):
            self.mm.active_quotes.clear()
            return
        if self.mm.should_activate(signal):
            bid, ask = self.mm.generate_quotes(self.state)
            self.mm.active_quotes = [bid, ask]
            # In live mode: submit as post-only limits to CLOB

    async def run(self):
        self._log.info("═" * 58)
        self._log.info("  POLYMARKET BTC ALPHA ENGINE v2.0 — STARTING")
        self._log.info("═" * 58)
        FeeModel.print_fee_table()

        feed_task      = asyncio.create_task(self.feeds.start_all())
        self._log.info("Waiting 5s for initial data warm-up...")
        await asyncio.sleep(5)

        cycle_task     = asyncio.create_task(self._strategy_loop())
        dashboard_task = asyncio.create_task(self._dashboard_loop())

        try:
            await asyncio.gather(feed_task, cycle_task, dashboard_task)
        except asyncio.CancelledError:
            self._log.info("Shutdown requested")

    async def _strategy_loop(self):
        while True:
            try:
                await self.run_cycle()
            except Exception as e:
                self._log.error(f"Cycle error: {e}", exc_info=True)
            await asyncio.sleep(self.CYCLE_SECONDS)

    async def _dashboard_loop(self):
        while True:
            await asyncio.sleep(300)
            self.perf.update_daily()
            self.perf.print_dashboard()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SECTION 19 — DEMO / BACKTEST RUNNER
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_demo():
    """
    Runs a single pipeline pass with injected mock state to verify
    all 19 sections are wired correctly. No live connections needed.
    """
    print("\n" + "═" * 58)
    print("  ALPHA ENGINE v2.0  ──  DEMO MODE")
    print("═" * 58 + "\n")

    import random
    cfg   = EngineConfig()
    state = MarketState()
    port  = PortfolioState(bankroll=10_000, peak_bankroll=10_000,
                           daily_start_bankroll=10_000)

    # Inject realistic market state
    state.btc_spot             = 82_000.0
    state.cvd_now              = 150.0
    state.cvd_5min_ago         = 80.0
    state.funding_rate_8hr     = 0.00005
    state.oi_now               = 5.2e9
    state.oi_5min_ago          = 5.0e9
    state.iv_atm               = 0.65
    state.iv_25d_call          = 0.68
    state.iv_25d_put           = 0.64
    state.iv_1min_realised     = 0.80
    state.ibit_pcr             = 0.40
    state.ibit_net_flow_30min  = 20e6
    state.cme_basis            = 0.025
    state.cme_max_pain         = 81_500.0
    state.cme_expiry_minutes   = 120.0
    state.poly_yes_bid         = 0.530
    state.poly_yes_ask         = 0.540
    state.poly_no_bid          = 0.455
    state.poly_no_ask          = 0.465
    state.kalshi_yes_bid       = 0.495
    state.kalshi_yes_ask       = 0.515
    state.liq_volume_1min      = 2e6
    state.oil_price_now        = 112.0
    state.oil_price_1hr_ago    = 111.0

    price = 82_000.0
    for _ in range(400):
        price += random.gauss(0, price * 0.0002)
        state.btc_price_history.append(price)

    print("── Regime Classification ──────────────────────────────")
    mode, alpha, s_mode = RegimeClassifier(cfg).classify(state, port)
    print(f"  Regime:       {mode.value}")
    print(f"  Alpha scalar: {alpha}")
    print(f"  Stream mode:  {s_mode.value}")

    print("\n── Stream 1: Directional Signal ───────────────────────")
    sg  = DirectionalSignalGenerator(cfg)
    sig = sg.generate(state, port)
    if sig:
        print(f"  Direction:    {sig.direction.value}")
        print(f"  p_model:      {sig.p_model:.4f}")
        print(f"  Consensus:    {sig.consensus_score:.2f}")
        print(f"  Components:")
        print(f"    mom={sig.mom_adj:+.3f}  cvd={sig.cvd_adj:+.3f}  "
              f"fund={sig.funding_adj:+.3f}  oi={sig.oi_adj:+.3f}")
        print(f"    skew={sig.skew_adj:+.3f}  ibit_γ={sig.ibit_gamma_adj:+.3f}  "
              f"cme_pin={sig.cme_pin_adj:+.3f}")

        print("\n── Stage 2: Edge & Calibration ────────────────────────")
        edge, dz, ev, ok = EdgeCalibrator(cfg).evaluate(sig, state, port, mode)
        print(f"  Edge:         {edge:.4f}  (min {cfg.edge_min_base})")
        print(f"  Delta-Z:      {dz:.2f}  (min ±1.96)")
        print(f"  Net EV:       {ev:.4f}")
        print(f"  Passes:       {ok}")

        print("\n── Stage 3: Toxicity Filters ──────────────────────────")
        ok_tox, reason = ToxicityFilter(cfg).check(state, sig, port)
        print(f"  Passes:       {ok_tox}")
        if not ok_tox:
            print(f"  Reason:       {reason}")

        print("\n── Stage 4+5: Kelly + Risk Gates ──────────────────────")
        bet, f, ok_risk = KellySizer(cfg).size(sig, state, port, alpha)
        print(f"  Kelly f*×α:   {f:.4f}")
        print(f"  Bet size:     ${bet:.2f}")
        print(f"  Passes:       {ok_risk}")
    else:
        print("  No signal (consensus gate not met)")

    print("\n── Stream 2: Arbitrage Scan ────────────────────────────")
    arb   = ArbitrageEngine(cfg, port)
    intra = arb.scan_intra_arb(state)
    cross = arb.scan_cross_arb(state)
    print(f"  Intra-arb:    {'FOUND' if intra else 'none'}")
    if cross:
        print(f"  Cross-arb:    FOUND ({len(cross)} legs)")
        for o in cross:
            print(f"    {o.venue:12} {o.leg} @ {o.limit_price:.4f}  net_edge={o.edge:.4f}")
    else:
        print(f"  Cross-arb:    none")

    FeeModel.print_fee_table()
    print("\n✓ Demo complete.")
    print("  ─ Set DataFeedManager.LIVE_MODE = True for live data feeds")
    print("  ─ Set ExecutionEngine.PAPER_MODE = False to submit real orders")
    print("  ─ Supply API keys in EngineConfig\n")


if __name__ == "__main__":
    import sys
    if "--live" in sys.argv:
        # DataFeedManager.LIVE_MODE = True
        # ExecutionEngine.PAPER_MODE = False
        asyncio.run(AlphaEngine().run())
    else:
        run_demo()
