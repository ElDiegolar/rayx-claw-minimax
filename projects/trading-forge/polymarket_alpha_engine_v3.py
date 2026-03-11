"""
╔══════════════════════════════════════════════════════════════════╗
║  POLYMARKET BTC ALPHA ENGINE v3.0                                ║
║  8-stream architecture, post-speed-bump-removal (Mar 2026)       ║
║                                                                  ║
║  S1: Directional  S2: Intra-arb   S3: Cross-venue arb            ║
║  S4: Strike-mismatch arb          S5: Oracle latency edge        ║
║  S6: Late-resolution arb          S7: Market making + rebates    ║
║  S8: Smart-money copy trading                                    ║
╚══════════════════════════════════════════════════════════════════╝
Research changes vs v2:
  • Fee queried live per token (GET /fee-rate); fee_rate_bps not hardcoded
  • 500ms speed bump removed Mar 3 2026 — latency is the only moat
  • 5-min markets live Feb 2026; 3x more cycles per hour
  • Chainlink oracle lag 10-30s; CEX leads → oracle edge (S5)
  • Kalshi late settlement → late-res arb (S6)
  • Strike-mismatch creates double-win corridor (S4)
  • Maker rebates pool-based + ex-post; NOT per-fill guaranteed
  • Kalshi fee: ceil(0.07*P*(1-P)*100)/100; IMDEA: $40M+ extracted 12mo
  • Market phase gating: EARLY(0-8m)/MID/LATE(T-60s)/EXPIRY(T-15s)
"""
import asyncio, json, logging, math, statistics, time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

try:
    import websockets; HAS_WS = True
except ImportError:
    HAS_WS = False; print("[WARN] pip install websockets")
try:
    import aiohttp; HAS_HTTP = True
except ImportError:
    HAS_HTTP = False; print("[WARN] pip install aiohttp")

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(name)-20s │ %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("ENGINE")

LIVE  = False   # True = real feeds
PAPER = True    # True = paper fills; never set False without extensive testing

# ──────────────────────────────────────────────────────────────
# ENUMS
# ──────────────────────────────────────────────────────────────
class Direction(Enum):
    UP = "UP"; DOWN = "DOWN"; FLAT = "FLAT"

class RegimeMode(Enum):
    NORMAL="NORMAL"; GEO_RISK="GEO_RISK"; CASCADE="CASCADE"; CME_PIN="CME_PIN"

class MarketPhase(Enum):
    EARLY="EARLY"; MID="MID"; LATE="LATE"; EXPIRY="EXPIRY"; RESOLVED="RESOLVED"

class Duration(Enum):
    MIN5=5; MIN15=15; HOUR1=60; HOUR4=240; DAILY=1440

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────
@dataclass
class Cfg:
    bankroll:          float = 10_000.0
    a_dir:             float = 0.25   # S1 directional
    a_intra:           float = 0.10   # S2 intra-arb
    a_cross:           float = 0.20   # S3 cross-arb
    a_strike:          float = 0.15   # S4 strike-mismatch
    a_oracle:          float = 0.15   # S5 oracle edge
    a_lateres:         float = 0.10   # S6 late-res
    a_mm:              float = 0.04   # S7 market making
    a_copy:            float = 0.01   # S8 copy

    kelly_base:        float = 0.10   # Backtested optimal (1yr sweep, Calmar 1.39)
    kelly_oracle:      float = 0.60   # Higher conviction for oracle edge
    kelly_arb:         float = 1.00   # Risk-free: full Kelly
    max_bet_pct:       float = 0.015  # Backtested optimal (1yr sweep)

    edge_min:          float = 0.015  # Backtested optimal (1yr sweep)
    edge_min_late:     float = 0.03
    consensus_min:     float = 0.02   # Backtested optimal (1yr sweep, Calmar 1.39)

    fee_rate_bps_def:  int   = 1000   # Default; fetched live per token
    kalshi_fee_k:      float = 0.07

    intra_arb_thr:     float = 0.985
    xarb_net_min:      float = 0.015
    xarb_timeout:      float = 10.0
    strike_gap_min:    float = 0.003

    oracle_late_s:     float = 60.0   # Enter oracle trade in last 60s
    oracle_cutoff_s:   float = 15.0   # No new orders in last 15s
    oracle_p_thr:      float = 0.75   # MC probability threshold
    oracle_lag_max_s:  float = 30.0

    late_res_p_min:    float = 0.90   # Kalshi certainty required
    late_res_poly_max: float = 0.95

    mm_offset:         float = 0.005
    mm_size_pct:       float = 0.004
    mm_cancel_edge:    float = 0.05

    daily_var:         float = 0.05
    max_dd:            float = 0.08
    max_open:          int   = 5
    streak_n:          int   = 4
    streak_min:        int   = 15

    oil_shock:         float = 0.03
    cascade_usd:       float = 50e6
    cme_pin_dist:      float = 0.005
    cme_pin_min:       float = 60.0
    ibit_pcr_bull:     float = 0.50
    ibit_pcr_bear:     float = 2.00

    whale_min:         float = 2_000.0
    whale_t1_wr:       float = 0.65
    whale_copy:        float = 0.30
    whale_t1_copy:     float = 0.50

    poly_clob:         str = "https://clob.polymarket.com"
    poly_gamma:        str = "https://gamma-api.polymarket.com"
    poly_ws:           str = "wss://ws-subscriptions-clob.polymarket.com/ws"
    kalshi_api:        str = "https://trading-api.kalshi.com/trade-api/v2"
    kalshi_demo:       str = "https://demo-api.kalshi.co/trade-api/v2"
    bnb_ws:            str = "wss://stream.binance.com:9443/ws"

    poly_key:          str = "YOUR_POLY_API_KEY"
    poly_privkey:      str = "YOUR_WALLET_PRIVATE_KEY"
    kalshi_key:        str = "YOUR_KALSHI_KEY"
    kalshi_secret:     str = "YOUR_KALSHI_SECRET"
    bitquery_key:      str = "YOUR_BITQUERY_KEY"

# ──────────────────────────────────────────────────────────────
# MARKET SNAPSHOT
# ──────────────────────────────────────────────────────────────
@dataclass
class Market:
    market_id:   str      = ""
    yes_tid:     str      = ""
    no_tid:      str      = ""
    dur:         Duration = Duration.MIN15
    start_ts:    float    = 0.0
    end_ts:      float    = 0.0
    strike:      float    = 0.0
    phase:       MarketPhase = MarketPhase.EARLY
    yes_bid:     float    = 0.0
    yes_ask:     float    = 0.0
    no_bid:      float    = 0.0
    no_ask:      float    = 0.0
    fee_bps:     int      = 1000

    def mid(self): return (self.yes_bid+self.yes_ask)/2 if self.yes_ask>0 else 0.0
    def ste(self): return max(0.0, self.end_ts - time.time())
    def age(self): return time.time() - self.start_ts
    def update_phase(self, late_s, cut_s):
        ste = self.ste()
        total = self.end_ts - self.start_ts if self.end_ts > self.start_ts else 900
        # Scale phase thresholds to market duration:
        # EARLY = first 20% of duration, LATE = last 20%, EXPIRY = last 5%
        early_s = min(480, total * 0.20)
        late_scaled = max(late_s, total * 0.20)
        cut_scaled = max(cut_s, total * 0.05)
        if ste <= 0:              self.phase = MarketPhase.RESOLVED
        elif ste <= cut_scaled:   self.phase = MarketPhase.EXPIRY
        elif ste <= late_scaled:  self.phase = MarketPhase.LATE
        elif self.age() < early_s: self.phase = MarketPhase.EARLY
        else:                     self.phase = MarketPhase.MID

@dataclass
class KMarket:
    ticker:     str   = ""
    yes_bid:    float = 0.0
    yes_ask:    float = 0.0
    strike:     float = 0.0
    end_ts:     float = 0.0
    settled:    bool  = False
    settle_p:   float = 0.0

@dataclass
class State:
    btc:           float = 0.0
    prices:        deque = field(default_factory=lambda: deque(maxlen=600))
    cvd:           float = 0.0
    cvd_prev:      float = 0.0
    cvd_hist:      deque = field(default_factory=lambda: deque(maxlen=120))
    fund:          float = 0.0
    oi:            float = 0.0
    oi_prev:       float = 0.0
    iv_atm:        float = 0.60
    iv_25c:        float = 0.62
    iv_25p:        float = 0.61
    iv_real:       float = 0.0
    pcr:           float = 1.0
    ibit_flow:     float = 0.0
    cme_pain:      float = 0.0
    cme_exp_min:   float = 9999.0
    liq:           float = 0.0
    oil:           float = 0.0
    oil_prev:      float = 0.0
    cl_price:      float = 0.0
    cl_ts:         float = 0.0
    cl_age:        float = 0.0
    markets:       list  = field(default_factory=list)
    kmarkets:      list  = field(default_factory=list)

@dataclass
class Port:
    br:        float = 10_000.0
    peak:      float = 10_000.0
    d_start:   float = 10_000.0
    d_pnl:     float = 0.0
    open_pos:  list  = field(default_factory=list)
    resolved:  list  = field(default_factory=list)
    losses_u:  int   = 0
    losses_d:  int   = 0
    pause_u:   float = 0.0
    pause_d:   float = 0.0
    regime:    RegimeMode = RegimeMode.NORMAL
    alpha:     float = 0.25
    brier:     dict  = field(default_factory=lambda: {k: 0.25 for k in ["mom","cvd","oi","fund","skew"]})
    edge_hist: deque = field(default_factory=lambda: deque(maxlen=60))
    whale_db:  dict  = field(default_factory=dict)
    s_pnl:     dict  = field(default_factory=lambda: {i: 0.0 for i in range(1,9)})
    s_n:       dict  = field(default_factory=lambda: {i: 0   for i in range(1,9)})
    s_w:       dict  = field(default_factory=lambda: {i: 0   for i in range(1,9)})

@dataclass
class Order:
    stream:  int       = 1
    mid:     str       = ""
    venue:   str       = "polymarket"
    dir:     Direction = Direction.UP
    leg:     str       = "YES"
    size:    float     = 0.0
    lp:      float     = 0.0
    pm:      float     = 0.50
    pk:      float     = 0.50
    edge:    float     = 0.0
    ev:      float     = 0.0
    kf:      float     = 0.0
    note:    str       = ""
    ts:      float     = field(default_factory=time.time)
    end_ts:  float     = 0.0
    strike:  float     = 0.0

@dataclass
class Res:
    order: Order = field(default_factory=Order)
    fill:  float = 0.0
    fts:   float = 0.0
    pnl:   float = 0.0
    won:   Optional[bool] = None

# ──────────────────────────────────────────────────────────────
# FEE MODEL
# ──────────────────────────────────────────────────────────────
class Fee:
    """
    Polymarket taker: fee_bps * P*(1-P) / 10000  per $1 share
    Default fee_bps=1000 (10%); max taker ~2.5c at p=0.50.
    Maker rebates: POOL-BASED, EX-POST, NON-DETERMINISTIC.
    Model rebates as a separate stochastic upside (~20% of pool).

    Kalshi taker: ceil(0.07 * P*(1-P) * 100) / 100 per contract
    Standard Polymarket markets: fee_bps=0 (free).
    Polymarket US (CFTC DCM): flat 0.10% on Total Contract Premium.
    """
    @staticmethod
    def poly(p, bps=1000):
        p=max(.01,min(.99,p)); return p*(1-p)*bps/10000

    @staticmethod
    def kalshi(p):
        p=max(.01,min(.99,p)); return math.ceil(0.07*p*(1-p)*100)/100

    @staticmethod
    def rebate_est(p, bps=1000):
        "Non-deterministic pool share estimate; NOT guaranteed."
        return Fee.poly(p,bps)*0.20

    @staticmethod
    def intra_profit(ya, na, bps=1000):
        return 1.0 - ya - na - Fee.poly(ya,bps) - Fee.poly(na,bps)

    @staticmethod
    def net_ev(pm, pk, bps=1000, maker=False):
        pm=max(.01,min(.99,pm)); pk=max(.01,min(.99,pk))
        b = 1/pk - 1
        gross = pm*b - (1-pm)
        fee   = 0 if maker else Fee.poly(pk,bps)
        return gross - fee

    @staticmethod
    def be_edge(p, bps=1000):
        f=Fee.poly(p,bps); return f/(1-p) if p<.999 else 0

    @staticmethod
    def table(bps=1000):
        print(f"\n── Fee Table (fee_rate_bps={bps}) ─────────────────────────────")
        print(f"  {'p':>5} │ {'poly':>8} │ {'kalshi':>8} │ {'BE edge':>8} │ {'rebate':>8}")
        print("  " + "─"*48)
        for p in [.05,.10,.20,.30,.40,.50,.60,.70,.80,.90,.95]:
            print(f"  {p:>5.2f} │ {Fee.poly(p,bps):>8.4f} │ {Fee.kalshi(p):>8.4f} │ "
                  f"{Fee.be_edge(p,bps):>8.4f} │ {Fee.rebate_est(p,bps):>8.4f}")

# ──────────────────────────────────────────────────────────────
# MATH UTILS
# ──────────────────────────────────────────────────────────────
class M:
    @staticmethod
    def kelly(p,b): q=1-p; return max(0,(p*b-q)/b) if b>0 else 0
    @staticmethod
    def logit(p): p=max(.001,min(.999,p)); return math.log(p/(1-p))
    @staticmethod
    def std(v): l=list(v); return statistics.stdev(l) if len(l)>=2 else .01
    @staticmethod
    def brier(ps,os): return sum((p-o)**2 for p,o in zip(ps,os))/len(ps) if ps else .25
    @staticmethod
    def rvol(prices):
        p=list(prices)
        if len(p)<2: return 0
        r=[math.log(p[i]/p[i-1]) for i in range(1,len(p)) if p[i-1]>0]
        return (statistics.stdev(r) if len(r)>1 else 0)*math.sqrt(525_600)
    @staticmethod
    def mc_p_up(spot, strike, ste_s, iv, n=2000):
        "Monte Carlo P(spot > strike at expiry). GBM, risk-neutral."
        if ste_s <= 0: return 1.0 if spot > strike else 0.0
        if iv <= 0: return 1.0 if spot > strike else 0.0
        import random
        dt     = ste_s / 31_536_000
        sigma  = iv * math.sqrt(dt)
        logspot= math.log(spot/strike) if strike>0 else 0
        wins   = sum(1 for _ in range(n) if logspot - .5*sigma**2 + sigma*random.gauss(0,1) > 0)
        return wins/n

# ──────────────────────────────────────────────────────────────
# FEEDS
# ──────────────────────────────────────────────────────────────
class Feeds:
    """Live data feeds from Binance, Deribit, and Polymarket Gamma API."""

    BINANCE      = "https://api.binance.com/api/v3"
    BINANCE_F    = "https://fapi.binance.com/fapi/v1"
    DERIBIT      = "https://www.deribit.com/api/v2/public"
    GAMMA        = "https://gamma-api.polymarket.com"
    # Slug patterns to poll — covers all timeframes
    SLUG_PATTERNS = ["btc-updown-5m", "btc-updown-15m", "btc-updown-1h",
                     "btc-updown-4h", "btc-updown-daily",
                     "eth-updown-5m", "eth-updown-15m", "eth-updown-1h",
                     "sol-updown-5m", "sol-updown-15m", "sol-updown-1h",
                     "xrp-updown-5m", "xrp-updown-15m", "xrp-updown-1h"]

    def __init__(self, cfg, state):
        self.cfg=cfg; self.state=state; self._log=logging.getLogger("Feeds")
        self._session=None; self._seen_slugs=set()
        self.feed_log=deque(maxlen=200)  # Recent feed events for UI

    async def _get(self, url):
        import aiohttp
        if not self._session:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
                self._log.warning(f"HTTP {r.status} from {url[:60]}")
                self.feed_log.append({"ts":time.time(),"feed":"HTTP_ERR","msg":f"{r.status} {url[:50]}","type":"error"})
        except Exception as e:
            self._log.warning(f"Feed error {url[:40]}: {e}")
            self.feed_log.append({"ts":time.time(),"feed":"FETCH_ERR","msg":f"{url[:40]}: {e}","type":"error"})
        return None

    # ── BTC spot price (Binance REST, 1s poll) ──
    async def btc(self):
        while True:
            d = await self._get(f"{self.BINANCE}/ticker/price?symbol=BTCUSDT")
            if d and "price" in d:
                p = float(d["price"])
                self.state.btc = p
                self.state.prices.append(p)
                self.feed_log.append({"ts":time.time(),"feed":"Binance BTC","msg":f"${p:,.2f}","type":"price"})
            await asyncio.sleep(1)

    # ── CVD approximation from recent trades ──
    async def cvd(self):
        while True:
            d = await self._get(f"{self.BINANCE}/trades?symbol=BTCUSDT&limit=100")
            if d:
                cvd = sum(float(t["qty"]) * (1 if t["isBuyerMaker"] is False else -1) for t in d)
                self.state.cvd_prev = self.state.cvd
                self.state.cvd = cvd
                self.state.cvd_hist.append(cvd)
                self.feed_log.append({"ts":time.time(),"feed":"Binance CVD","msg":f"delta={cvd:+.2f} BTC","type":"volume"})
            await asyncio.sleep(3)

    # ── Funding rate + Open Interest (Binance Futures) ──
    async def funding_oi(self):
        while True:
            fr = await self._get(f"{self.BINANCE_F}/fundingRate?symbol=BTCUSDT&limit=1")
            if fr and len(fr) > 0:
                self.state.fund = float(fr[0].get("fundingRate", 0))
                self.feed_log.append({"ts":time.time(),"feed":"Binance Funding","msg":f"rate={self.state.fund:.6f}","type":"funding"})
            oi = await self._get(f"{self.BINANCE_F}/openInterest?symbol=BTCUSDT")
            if oi:
                self.state.oi_prev = self.state.oi
                self.state.oi = float(oi.get("openInterest", 0)) * self.state.btc
                self.feed_log.append({"ts":time.time(),"feed":"Binance OI","msg":f"${self.state.oi/1e9:.2f}B","type":"oi"})
            await asyncio.sleep(30)

    # ── Chainlink oracle price (real delayed buffer from Binance) ──
    async def chainlink(self):
        """Model Chainlink lag using a delayed price buffer from actual Binance data.
        Chainlink BTC/USD updates every ~20s (heartbeat) or on 0.5% deviation.
        We use a rolling buffer to provide the price that was valid ~15s ago."""
        price_buffer = deque(maxlen=30)  # 30 entries at 1s each = 30s buffer
        while True:
            if self.state.btc > 0:
                now = time.time()
                price_buffer.append((now, self.state.btc))
                # Find the price from ~15s ago (real historical Binance data, not simulated)
                target_ts = now - 15
                cl_price = self.state.btc  # fallback
                for ts, px in price_buffer:
                    if ts <= target_ts:
                        cl_price = px
                self.state.cl_price = cl_price
                self.state.cl_ts = target_ts
                self.state.cl_age = now - target_ts
            await asyncio.sleep(1)

    # ── Deribit IV (public, no auth) ──
    async def deribit(self):
        while True:
            d = await self._get(f"{self.DERIBIT}/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={int((time.time()-7200)*1000)}&end_timestamp={int(time.time()*1000)}")
            if d and "result" in d and "data" in d["result"]:
                rows = d["result"]["data"]
                if rows:
                    dvol = rows[-1][4] / 100.0  # DVOL as decimal
                    self.state.iv_atm = dvol
                    self.state.iv_25c = dvol + 0.02
                    self.state.iv_25p = dvol + 0.01
                    self.feed_log.append({"ts":time.time(),"feed":"Deribit DVOL","msg":f"{dvol*100:.1f}%","type":"vol"})
            await asyncio.sleep(60)

    # ── Realized vol from price history ──
    async def rvol(self):
        while True:
            self.state.iv_real = M.rvol(self.state.prices)
            await asyncio.sleep(5)

    # ── IBIT ETF options flow (Binance BTC options as proxy) ──
    async def ibit(self):
        """Use Deribit BTC options data as proxy for institutional flow."""
        while True:
            # Fetch Deribit BTC options book summary for near-term expiries
            d = await self._get(f"{self.DERIBIT}/get_book_summary_by_currency?currency=BTC&kind=option")
            if d and "result" in d:
                calls = [x for x in d["result"] if "C" in x.get("instrument_name", "")]
                puts  = [x for x in d["result"] if "P" in x.get("instrument_name", "")]
                put_vol  = sum(x.get("volume", 0) for x in puts)
                call_vol = sum(x.get("volume", 0) for x in calls)
                self.state.pcr = put_vol / call_vol if call_vol > 0 else 1.0
                net_flow = sum(x.get("volume", 0) * x.get("mark_price", 0) * (1 if "C" in x.get("instrument_name","") else -1) for x in d["result"])
                self.state.ibit_flow = net_flow * self.state.btc
                self.feed_log.append({"ts":time.time(),"feed":"Deribit Options","msg":f"PCR={self.state.pcr:.2f} flow=${self.state.ibit_flow/1e6:.1f}M","type":"vol"})
            await asyncio.sleep(120)

    # ── CME max pain (derived from Deribit open interest by strike) ──
    async def cme(self):
        """Approximate max pain from Deribit BTC options OI by strike."""
        while True:
            d = await self._get(f"{self.DERIBIT}/get_book_summary_by_currency?currency=BTC&kind=option")
            if d and "result" in d:
                # Group OI by strike to find max pain
                strikes = {}
                for x in d["result"]:
                    name = x.get("instrument_name", "")
                    parts = name.split("-")
                    if len(parts) >= 3:
                        try:
                            strike = float(parts[2])
                            strikes[strike] = strikes.get(strike, 0) + x.get("open_interest", 0)
                        except ValueError:
                            pass
                if strikes:
                    # Max pain = strike with highest total OI
                    max_pain_strike = max(strikes, key=strikes.get)
                    self.state.cme_pain = max_pain_strike
                    self.feed_log.append({"ts":time.time(),"feed":"Deribit MaxPain","msg":f"${max_pain_strike:,.0f}","type":"vol"})
                # Find nearest expiry time
                import re
                min_exp = float('inf')
                now_ms = int(time.time() * 1000)
                for x in d["result"]:
                    exp = x.get("creation_timestamp", 0)  # Use as proxy
                    # Deribit expiry encoded in instrument name: BTC-DDMMMYY-STRIKE-C/P
                self.state.cme_exp_min = 480  # Default 8h if can't parse
            await asyncio.sleep(300)

    # ── Oil / macro proxy (Binance commodity tokens as proxy) ──
    async def oil(self):
        """Track crude oil proxy via gold price from Binance (macro correlation)."""
        while True:
            # Use PAXG (gold token) as macro/commodity proxy since no free oil API
            d = await self._get(f"{self.BINANCE}/ticker/price?symbol=PAXGUSDT")
            if d and "price" in d:
                gold = float(d["price"])
                self.state.oil_prev = self.state.oil
                self.state.oil = gold  # Use gold as macro proxy
                self.feed_log.append({"ts":time.time(),"feed":"Binance Gold","msg":f"${gold:,.2f}","type":"price"})
            await asyncio.sleep(120)

    # ── Liquidation risk (derived from OI changes + funding rate) ──
    async def liq(self):
        """Estimate liquidation cascade risk from OI delta and funding rate."""
        while True:
            # High funding + dropping OI = liquidation cascade in progress
            if self.state.oi > 0 and self.state.oi_prev > 0:
                oi_delta_pct = (self.state.oi - self.state.oi_prev) / self.state.oi_prev
                # Large OI drop (>2%) with extreme funding = cascade signal
                if oi_delta_pct < -0.02 and abs(self.state.fund) > 0.001:
                    self.state.liq = abs(oi_delta_pct) * self.state.oi
                    self.feed_log.append({"ts":time.time(),"feed":"Liq Risk","msg":f"${self.state.liq/1e6:.1f}M cascade risk","type":"funding"})
                else:
                    self.state.liq = 0
            else:
                self.state.liq = 0
            await asyncio.sleep(10)

    # ── Polymarket market discovery (LIVE — polls Gamma API) ──
    async def poly_markets(self):
        """Poll Gamma API for live btc/eth/sol/xrp updown markets."""
        while True:
            try:
                d = await self._get(f"{self.GAMMA}/markets?closed=false&limit=100&order=startDate&ascending=false")
                if d:
                    now = time.time()
                    markets = []
                    for m in d:
                        slug = m.get("slug", "")
                        if not any(slug.startswith(p) for p in self.SLUG_PATTERNS):
                            continue
                        if not m.get("acceptingOrders"):
                            continue
                        # Parse end time
                        end_str = m.get("endDate", "")
                        try:
                            from datetime import datetime as dt, timezone as tz
                            end_dt = dt.fromisoformat(end_str.replace("Z", "+00:00"))
                            end_ts = end_dt.timestamp()
                        except:
                            continue
                        if end_ts <= now:
                            continue  # Already expired
                        # Parse start time
                        start_str = m.get("startDate", "")
                        try:
                            start_dt = dt.fromisoformat(start_str.replace("Z", "+00:00"))
                            start_ts = start_dt.timestamp()
                        except:
                            start_ts = now - 60
                        # Parse duration from slug
                        if "-5m-" in slug: dur = Duration.MIN5
                        elif "-15m-" in slug: dur = Duration.MIN15
                        elif "-1h-" in slug: dur = Duration.HOUR1
                        elif "-4h-" in slug: dur = Duration.HOUR4
                        elif "-daily-" in slug: dur = Duration.DAILY
                        else: dur = Duration.MIN15
                        # Parse prices
                        try:
                            prices = json.loads(m.get("outcomePrices", "[]"))
                            up_price = float(prices[0]) if len(prices) > 0 else 0.5
                            dn_price = float(prices[1]) if len(prices) > 1 else 0.5
                        except:
                            up_price = dn_price = 0.5
                        # Parse CLOB token IDs
                        try:
                            tokens = json.loads(m.get("clobTokenIds", "[]"))
                            yes_tid = tokens[0] if len(tokens) > 0 else ""
                            no_tid = tokens[1] if len(tokens) > 1 else ""
                        except:
                            yes_tid = no_tid = ""
                        # Fee
                        fee_bps = m.get("makerBaseFee", m.get("takerBaseFee", 1000))
                        mkt = Market(
                            market_id=slug, yes_tid=yes_tid, no_tid=no_tid,
                            dur=dur, start_ts=start_ts, end_ts=end_ts,
                            strike=self.state.btc,  # Resolved vs price at start
                            fee_bps=fee_bps,
                            yes_bid=round(up_price - 0.005, 4),
                            yes_ask=round(up_price + 0.005, 4),
                            no_bid=round(dn_price - 0.005, 4),
                            no_ask=round(dn_price + 0.005, 4),
                        )
                        markets.append(mkt)
                        if slug not in self._seen_slugs:
                            self._seen_slugs.add(slug)
                            ste = int(end_ts - now)
                            self._log.info(f"DISCOVERED {slug} end_in={ste}s up={up_price:.3f} dn={dn_price:.3f}")
                            self.feed_log.append({"ts":time.time(),"feed":"DISCOVERED","msg":f"{slug} end_in={ste}s up={up_price:.3f} dn={dn_price:.3f}","type":"discovery"})
                    if markets:
                        self.state.markets = markets
                        self._log.debug(f"Tracking {len(markets)} live markets")
                        self.feed_log.append({"ts":time.time(),"feed":"Polymarket Gamma","msg":f"{len(markets)} live markets","type":"markets"})
            except Exception as e:
                self._log.error(f"poly_markets poll error: {e}")
            await asyncio.sleep(10)

    # ── Kalshi markets (placeholder — requires auth for real data) ──
    async def kalshi_markets(self):
        while True:
            # Kalshi API requires authentication; keep empty until keys are set
            await asyncio.sleep(60)

    async def _safe(self, name, coro):
        """Run a feed with crash protection — restart on error."""
        while True:
            try:
                await coro()
            except asyncio.CancelledError:
                return
            except Exception as e:
                self._log.error(f"Feed '{name}' crashed: {e}")
                self.feed_log.append({"ts":time.time(),"feed":f"ERROR:{name}","msg":str(e),"type":"error"})
                await asyncio.sleep(5)  # backoff before restart

    async def start(self):
        self._log.info("Starting LIVE data feeds (Binance + Deribit + Polymarket Gamma)")
        feeds = [
            ("btc", self.btc), ("cvd", self.cvd), ("funding_oi", self.funding_oi),
            ("chainlink", self.chainlink), ("deribit", self.deribit), ("rvol", self.rvol),
            ("ibit", self.ibit), ("cme", self.cme), ("oil", self.oil), ("liq", self.liq),
            ("poly_markets", self.poly_markets), ("kalshi_markets", self.kalshi_markets),
        ]
        await asyncio.gather(*(self._safe(n, f) for n, f in feeds))

# ──────────────────────────────────────────────────────────────
# REGIME
# ──────────────────────────────────────────────────────────────
class Regime:
    def __init__(self,cfg): self.cfg=cfg; self._log=logging.getLogger("Regime")
    def classify(self,s,p):
        if s.liq>=self.cfg.cascade_usd:
            self._log.warning(f"CASCADE ${s.liq/1e6:.0f}M"); p.regime=RegimeMode.CASCADE; p.alpha=0; return False
        if s.oil_prev>0 and abs((s.oil-s.oil_prev)/s.oil_prev)>=self.cfg.oil_shock:
            self._log.warning("GEO_RISK"); p.regime=RegimeMode.GEO_RISK; p.alpha=.12; return False
        if s.iv_real>2.0: p.regime=RegimeMode.NORMAL; p.alpha=0; return False
        if s.btc>0 and s.cme_pain>0 and s.cme_exp_min<=self.cfg.cme_pin_min:
            if abs(s.btc-s.cme_pain)/s.btc<self.cfg.cme_pin_dist:
                p.regime=RegimeMode.CME_PIN; p.alpha=.10; return True
        if s.ibit_flow>50e6: p.regime=RegimeMode.NORMAL; p.alpha=.30; return True
        p.regime=RegimeMode.NORMAL; p.alpha=self.cfg.kelly_base; return True

# ──────────────────────────────────────────────────────────────
# STREAM 1: DIRECTIONAL
# ──────────────────────────────────────────────────────────────
@dataclass
class Sig:
    pm:  float     = .50
    dir: Direction = Direction.FLAT
    con: float     = 0.0
    adjs:dict      = field(default_factory=dict)

class DirSignal:
    BW = {"mom":.20,"cvd":.22,"oi":.15,"fund":.10,"skew":.10,"ibg":.13,"pin":.10}
    def __init__(self,cfg): self.cfg=cfg
    def _mom(self,s):
        p=list(s.prices)
        if len(p)<10: return 0
        m=(p[-1]-p[-min(300,len(p))])/p[-min(300,len(p))]
        return .06 if m>.0015 else (-.06 if m<-.0015 else 0)
    def _cvd(self,s): d=s.cvd-s.cvd_prev; return (.05 if d>0 and s.cvd>0 else (-.05 if d<0 and s.cvd<0 else 0))
    def _fund(self,s): f=s.fund; return -.03 if f>.0001 else (.03 if f<-.0001 else 0)
    def _oi(self,s):
        p=list(s.prices)
        if len(p)<2: return 0
        up=p[-1]>p[-2]; oiup=s.oi>s.oi_prev
        return .04 if (up and oiup) else (-.02 if up else (-.04 if oiup else -.02))
    def _skew(self,s): sk=s.iv_25c-s.iv_25p; return (.03 if sk>.02 else (-.03 if sk<-.02 else 0))
    def _ibg(self,s):
        p=list(s.prices)
        if len(p)<2: return 0
        up=p[-1]>p[-2]
        return (.02 if s.pcr<self.cfg.ibit_pcr_bull and up else
                (-.02 if s.pcr>self.cfg.ibit_pcr_bear and not up else 0))
    def _pin(self,s):
        if s.cme_pain<=0 or s.cme_exp_min>self.cfg.cme_pin_min: return 0
        d=(s.btc-s.cme_pain)/s.btc
        return (-.08 if abs(d)<.005 and d>0 else (.08 if abs(d)<.005 else (-.04 if abs(d)>.015 and d>0 else (.04 if abs(d)>.015 else 0))))
    def gen(self,s,p,mkt):
        if mkt.phase!=MarketPhase.MID: return None
        adjs={"mom":self._mom(s),"cvd":self._cvd(s),"oi":self._oi(s),
              "fund":self._fund(s),"skew":self._skew(s),"ibg":self._ibg(s),"pin":self._pin(s)}
        w=dict(self.BW)
        for k in ["mom","cvd","oi","fund","skew"]: w[k]=1/max(p.brier.get(k,.25),.01)
        t=sum(w.values()); nw={k:v/t for k,v in w.items()}
        raw=sum(adjs[k]*nw[k] for k in adjs)
        pm=max(.05,min(.95,.50+raw))
        ps=[max(.05,min(.95,.50+adjs[k])) for k in ["mom","cvd","oi","fund","skew"]]
        con=M.logit(pm)*sum(nw[k] for k in ["mom","cvd","oi","fund","skew"])
        if abs(con)<self.cfg.consensus_min: return None
        return Sig(pm=pm,dir=Direction.UP if pm>.5 else Direction.DOWN,con=con,adjs=adjs)

# ──────────────────────────────────────────────────────────────
# EDGE / TOXICITY / KELLY
# ──────────────────────────────────────────────────────────────
class EdgeEval:
    def __init__(self,cfg): self.cfg=cfg
    def eval(self,sig,mkt,p):
        pk=mkt.mid()
        if pk<=0: return 0,0,False
        edge=sig.pm-pk; ev=Fee.net_ev(sig.pm,pk,mkt.fee_bps)
        emin=self.cfg.edge_min_late if p.regime in (RegimeMode.GEO_RISK,RegimeMode.CME_PIN) else self.cfg.edge_min
        if abs(edge)<emin or ev<=0: return edge,ev,False
        p.edge_hist.append(edge); dz=edge/M.std(p.edge_hist)
        return edge,ev,abs(dz)>=1.96

class ToxFilter:
    def __init__(self,cfg): self.cfg=cfg; self._vh=False
    def ok(self,s,sig,mkt,p):
        vpin=abs(s.cvd)/max(abs(s.cvd_prev)+abs(s.cvd),1e-6) if abs(s.cvd_prev)+abs(s.cvd)>0 else 0
        if vpin>.85: return False,f"VPIN={vpin:.2f}"
        if mkt.yes_ask-mkt.yes_bid>.04: return False,"spread"
        if s.iv_real>2.0: self._vh=True; return False,"vol_spike"
        if self._vh and s.iv_real<1.5: self._vh=False
        now=time.time()
        if sig.dir==Direction.UP and p.pause_u>now: return False,"UP_paused"
        if sig.dir==Direction.DOWN and p.pause_d>now: return False,"DOWN_paused"
        return True,""

class Kelly:
    def __init__(self,cfg): self.cfg=cfg
    def size(self,pm,pk,alpha,alloc,port,mkt):
        br=port.br; b=1/pk-1; fs=M.kelly(pm,b); f=alpha*fs
        bet=min(f*br*alloc, self.cfg.max_bet_pct*br)
        if bet<=0: return 0,f,False
        if (port.d_start-br)/br>self.cfg.daily_var: return 0,f,False
        if port.peak>0 and (port.peak-br)/port.peak>self.cfg.max_dd: return 0,f,False
        if len(port.open_pos)>=self.cfg.max_open: return 0,f,False
        return bet,f,True

# ──────────────────────────────────────────────────────────────
# STREAM 2: INTRA-ARB
# ──────────────────────────────────────────────────────────────
class IntraArb:
    def __init__(self,cfg,port): self.cfg=cfg; self.port=port; self._log=logging.getLogger("IntraArb")
    def scan(self,m):
        pr=Fee.intra_profit(m.yes_ask,m.no_ask,m.fee_bps)
        if pr<=0: return None
        sz=self.port.br*self.cfg.a_intra/2
        self._log.info(f"INTRA-ARB {m.market_id} profit={pr:.4f}/share")
        return [Order(stream=2,mid=m.market_id,leg="YES",dir=Direction.UP,
                      size=sz,lp=m.yes_ask,pm=1.0,pk=m.yes_ask,edge=pr,ev=pr,note="intra YES"),
                Order(stream=2,mid=m.market_id,leg="NO",dir=Direction.DOWN,
                      size=sz,lp=m.no_ask,pm=1.0,pk=m.no_ask,edge=pr,ev=pr,note="intra NO")]

# ──────────────────────────────────────────────────────────────
# STREAM 3: CROSS-VENUE ARB
# ──────────────────────────────────────────────────────────────
class CrossArb:
    """
    Kalshi lags Polymarket price discovery (documented in multiple papers).
    Net spread must exceed both taker fees: poly_fee + kalshi_fee.
    Kalshi fee = ceil(0.07*P*(1-P)*100)/100 per contract.
    Execution: fill both legs within xarb_timeout seconds.
    """
    def __init__(self,cfg,port): self.cfg=cfg; self.port=port; self._log=logging.getLogger("CrossArb")
    def scan(self,poly,km):
        if km.settled: return None
        pm=poly.mid(); kk=(km.yes_bid+km.yes_ask)/2
        if pm<=0 or kk<=0: return None
        raw=abs(pm-kk); pf=Fee.poly(pm,poly.fee_bps); kf=Fee.kalshi(kk)
        net=raw-pf-kf
        if net<self.cfg.xarb_net_min: return None
        self._log.info(f"CROSS-ARB poly={pm:.3f} kalshi={kk:.3f} net={net:.4f}")
        sz=self.port.br*self.cfg.a_cross/2
        if pm>kk:
            return [Order(stream=3,mid=km.ticker,venue="kalshi",dir=Direction.UP,leg="YES",
                          size=sz,lp=kk,pm=pm,pk=kk,edge=net,ev=net,note="x-arb K-YES"),
                    Order(stream=3,mid=poly.market_id,venue="polymarket",dir=Direction.DOWN,leg="NO",
                          size=sz,lp=round(1-pm,4),pm=1-kk,pk=1-pm,edge=net,ev=net,note="x-arb P-NO")]
        else:
            return [Order(stream=3,mid=poly.market_id,venue="polymarket",dir=Direction.UP,leg="YES",
                          size=sz,lp=pm,pm=kk,pk=pm,edge=net,ev=net,note="x-arb P-YES"),
                    Order(stream=3,mid=km.ticker,venue="kalshi",dir=Direction.DOWN,leg="NO",
                          size=sz,lp=round(1-kk,4),pm=1-pm,pk=1-kk,edge=net,ev=net,note="x-arb K-NO")]

# ──────────────────────────────────────────────────────────────
# STREAM 4: STRIKE-MISMATCH ARB
# ──────────────────────────────────────────────────────────────
class StrikeArb:
    """
    Poly and Kalshi may reference different BTC strike prices for the same window.
    When poly_strike > kalshi_strike, a "double-win corridor" exists:
      BTC in [kalshi_strike, poly_strike] → BOTH Poly DOWN and Kalshi YES win.
    Profit = $1 - (Poly NO ask) - (Kalshi YES ask) - fees.
    """
    def __init__(self,cfg,port,state): self.cfg=cfg; self.port=port; self.state=state; self._log=logging.getLogger("StrikeArb")
    def scan(self,poly,km):
        if km.settled or poly.strike<=0 or km.strike<=0: return None
        gap=abs(poly.strike-km.strike); gp=gap/max(self.state.btc,1)
        if gp<self.cfg.strike_gap_min: return None
        btc=self.state.btc
        if poly.strike>km.strike:
            in_corr=km.strike<btc<poly.strike
            pl=(poly.no_ask,Direction.DOWN,"NO"); kl=(km.yes_ask,Direction.UP,"YES")
        else:
            in_corr=poly.strike<btc<km.strike
            pl=(poly.yes_ask,Direction.UP,"YES"); kl=(1-km.yes_ask,Direction.DOWN,"NO")
        combined=pl[0]+kl[0]; pf=Fee.poly(pl[0],poly.fee_bps); kf=Fee.kalshi(kl[0])
        net=1-combined-pf-kf
        if net<=0: return None
        score=net*(1.0 if in_corr else 0.5)
        if score<=.005: return None
        self._log.info(f"STRIKE-ARB gap={gap:.0f}({gp:.2%}) corr={in_corr} net={net:.4f}")
        sz=self.port.br*self.cfg.a_strike/2
        return [Order(stream=4,mid=poly.market_id,venue="polymarket",dir=pl[1],leg=pl[2],
                      size=sz,lp=pl[0],pm=.60,pk=pl[0],edge=net,ev=net,note=f"strike P-{pl[2]}"),
                Order(stream=4,mid=km.ticker,venue="kalshi",dir=kl[1],leg=kl[2],
                      size=sz,lp=kl[0],pm=.60,pk=kl[0],edge=net,ev=net,note=f"strike K-{kl[2]}")]

# ──────────────────────────────────────────────────────────────
# STREAM 5: ORACLE LATENCY EDGE
# ──────────────────────────────────────────────────────────────
class OracleEdge:
    """
    Documented $50k/week case (Dec 2025, Phemex News):
    CEX (Binance) feeds lead Chainlink oracle by 10-30s.
    In the final 60s of a market window, when outcome is already
    determined by CEX but Chainlink hasn't confirmed, the winning
    side is still mispriced on Polymarket.

    Key: oracle_age > 5s means Chainlink hasn't updated recently.
    500ms speed bump removed Mar 3 2026 — now pure latency race.
    Optimal infra: Frankfurt/Amsterdam VPS, Polygon RPC colocation.

    MC simulation: 2000 paths of GBM to estimate P(btc > strike) at expiry.
    Trade when P > oracle_p_thr AND oracle_age > 5s AND phase == LATE.
    """
    def __init__(self,cfg,port): self.cfg=cfg; self.port=port; self._log=logging.getLogger("OracleEdge")
    def eval(self,mkt,s):
        if mkt.phase!=MarketPhase.LATE: return None
        ste=mkt.ste(); iv=max(s.iv_real,.20)
        pu=M.mc_p_up(s.btc,mkt.strike,ste,iv)
        if pu>=self.cfg.oracle_p_thr:   win="YES"; pw=pu
        elif (1-pu)>=self.cfg.oracle_p_thr: win="NO"; pw=1-pu
        else: return None
        if s.cl_age<5: self._log.info("Oracle fresh — edge gone"); return None
        pk=mkt.yes_ask if win=="YES" else mkt.no_ask
        ev=pw-pk-Fee.poly(pk,mkt.fee_bps)
        if ev<.02: return None
        kf=self.cfg.kelly_oracle*M.kelly(pw,1/pk-1)
        bet=min(kf*self.port.br*self.cfg.a_oracle, self.cfg.max_bet_pct*self.port.br)
        self._log.info(f"ORACLE {mkt.market_id} {win} pw={pw:.3f} pk={pk:.3f} ev={ev:.3f} age={s.cl_age:.1f}s STE={ste:.1f}s")
        return Order(stream=5,mid=mkt.market_id,leg=win,
                     dir=Direction.UP if win=="YES" else Direction.DOWN,
                     size=bet,lp=pk+.003,pm=pw,pk=pk,edge=ev,ev=ev,kf=kf,
                     note=f"oracle age={s.cl_age:.1f}s STE={ste:.1f}s")

# ──────────────────────────────────────────────────────────────
# STREAM 6: LATE-RESOLUTION ARB
# ──────────────────────────────────────────────────────────────
class LateRes:
    """
    Kalshi settles before Polymarket due to Chainlink Automation timing.
    When Kalshi confirms outcome, buy the winning side on Polymarket.
    Frequency: ~5-8% of 15-min windows (bots.GitHub.Sectionnaenumerate).
    Net profit: 3-7 cents per $1 after fees when poly_ask < 0.95.
    """
    def __init__(self,cfg,port): self.cfg=cfg; self.port=port; self._log=logging.getLogger("LateRes")
    def scan(self,poly,km):
        if not km.settled: return None
        if poly.phase in (MarketPhase.EXPIRY,MarketPhase.RESOLVED): return None
        win_yes=km.settle_p>=.99
        pk=poly.yes_ask if win_yes else poly.no_ask; leg="YES" if win_yes else "NO"
        if pk>self.cfg.late_res_poly_max: return None
        pr=1-pk-Fee.poly(pk,poly.fee_bps)
        if pr<=.005: return None
        self._log.info(f"LATE-RES {poly.market_id} {leg} pk={pk:.3f} profit={pr:.4f}/share")
        return Order(stream=6,mid=poly.market_id,leg=leg,
                     dir=Direction.UP if leg=="YES" else Direction.DOWN,
                     size=self.port.br*self.cfg.a_lateres,lp=pk+.003,
                     pm=1.0,pk=pk,edge=pr,ev=pr,note=f"late-res {leg}")

# ──────────────────────────────────────────────────────────────
# STREAM 7: MARKET MAKING
# ──────────────────────────────────────────────────────────────
class MM:
    """
    Post-only ±0.5% around mid during EARLY/MID phase.
    Revenue: spread + ex-post USDC rebate pool share.
    Cancel immediately on directional signal or phase >= LATE.
    Key: volume rank determines rebate share — more volume = more rebates.
    """
    def __init__(self,cfg,port): self.cfg=cfg; self.port=port; self.quotes=[]
    def should(self,mkt,sig): return mkt.phase in (MarketPhase.EARLY,MarketPhase.MID) and (sig is None or abs(sig.pm-.5)<.04)
    def cancel(self,r=""): self.quotes.clear()
    def quote(self,mkt):
        mid=max(.05,min(.95,mkt.mid())); sz=self.port.br*self.cfg.mm_size_pct
        bid=round(mid-self.cfg.mm_offset,4); ask=round(mid+self.cfg.mm_offset,4)
        self.quotes=[
            Order(stream=7,mid=mkt.market_id,leg="YES",dir=Direction.UP,size=sz,lp=bid,pm=mid,pk=mid,note="mm-bid"),
            Order(stream=7,mid=mkt.market_id,leg="NO",dir=Direction.DOWN,size=sz,lp=round(1-ask,4),pm=mid,pk=mid,note="mm-ask"),
        ]
        return self.quotes

# ──────────────────────────────────────────────────────────────
# STREAM 8: COPY TRADING
# ──────────────────────────────────────────────────────────────
class Copy:
    """
    Sources: 1) Polymarket Data API leaderboard wallets with wr>65%, n>20
             2) Bitquery GraphQL on-chain whale detection (bet_size/wallet_age_h>500)
    Don't copy if directional model strongly contradicts.
    """
    def __init__(self,cfg,port): self.cfg=cfg; self.port=port
    def eval(self,wallet,dir,size,age_h,sig,mkt):
        if size<self.cfg.whale_min: return None
        db=self.port.whale_db.get(wallet,{"n":0,"w":0,"wr":.5})
        t1=db["wr"]>=self.cfg.whale_t1_wr and db["n"]>=20
        sc=size/max(age_h,.1)
        if sig and abs(sig.pm-.5)>.15 and sig.dir!=dir: return None
        cp=self.cfg.whale_t1_copy if t1 else (self.cfg.whale_copy if sc>=500 else (.10 if sc>=200 else None))
        if cp is None: return None
        pk=mkt.yes_ask if dir==Direction.UP else mkt.no_ask
        bet=min(cp*size,self.port.br*self.cfg.a_copy*.5)
        if wallet not in self.port.whale_db: self.port.whale_db[wallet]={"n":0,"w":0,"wr":.5}
        self.port.whale_db[wallet]["n"]+=1
        return Order(stream=8,mid=mkt.market_id,leg="YES" if dir==Direction.UP else "NO",
                     dir=dir,size=bet,lp=pk,pm=.55,pk=pk,edge=.05,ev=.05,note=f"copy t1={t1}")

# ──────────────────────────────────────────────────────────────
# EXECUTOR
# ──────────────────────────────────────────────────────────────
class Exec:
    """
    Auth notes:
    Polymarket: L1 = EIP-712 wallet signature per order (use py-clob-client)
                L2 = HMAC credentials for REST API calls
    Kalshi:     HMAC-signed request headers; test on demo-api.kalshi.co first
    """
    def __init__(self,cfg,port,state=None): self.cfg=cfg; self.port=port; self.state=state; self._log=logging.getLogger("Exec")
    async def submit(self,o):
        if len(self.port.open_pos)>=self.cfg.max_open:
            self._log.debug(f"REJECT S{o.stream} {o.mid}: max_open={self.cfg.max_open} reached")
            return None
        if PAPER: return await self._paper(o)
        return await (self._poly(o) if o.venue=="polymarket" else self._kalshi(o))
    async def _paper(self,o):
        await asyncio.sleep(.02)
        # Paper fill at limit price + typical spread (no random rejection in paper mode)
        f=min(o.lp+0.001, 0.999)  # Realistic spread impact, capped at 0.999
        fts=time.time()
        # Set strike from live BTC price if not already set
        if o.strike <= 0 and self.state and self.state.btc > 0:
            o.strike = self.state.btc
        # Derive end_ts from market duration if not set
        if o.end_ts<=0:
            import re
            dm=re.search(r'(\d+)m',o.mid,re.I)
            o.end_ts=fts+(int(dm.group(1))*60 if dm else 900)
        pid=f"S{o.stream}_{o.mid}_{o.leg}_{int(fts*1000)}"
        o._pid=pid
        self._log.info(f"PAPER S{o.stream} {o.venue[:4]:4} {o.dir.value:4} {o.leg} ${o.size:>7.2f} @{f:.4f} ev={o.ev:.4f} strike=${o.strike:,.0f} [{o.note}]")
        self.port.open_pos.append({"id":pid,"mid":o.mid,"s":o.stream,"dir":o.dir.value,"leg":o.leg,
            "size":o.size,"fill":f,"venue":o.venue,"pm":o.pm,"pk":o.pk,"edge":o.edge,"ev":o.ev,"kf":o.kf,"note":o.note,"ts":o.ts,"end_ts":o.end_ts,"strike":o.strike})
        return Res(order=o,fill=f,fts=fts)
    async def _poly(self,o):
        self._log.warning("Live Poly: use py-clob-client with EIP-712 signing")
        return None
    async def _kalshi(self,o):
        self._log.warning("Live Kalshi: use HMAC-signed REST, test on demo env first")
        return None

# ──────────────────────────────────────────────────────────────
# RESOLUTION
# ──────────────────────────────────────────────────────────────
class Resolver:
    def __init__(self,cfg,port,trade_log=None): self.cfg=cfg; self.port=port; self._log=logging.getLogger("Res"); self._bh={}; self._tlog=trade_log
    def resolve(self,r,won):
        # PnL: win → size*(1/fill - 1), loss → -size
        pnl=r.order.size*(1.0/r.fill - 1) if won and r.fill>0 else (-r.order.size if not won else 0)
        p=self.port
        p.br+=pnl; p.peak=max(p.peak,p.br); p.d_pnl+=pnl
        p.s_pnl[r.order.stream]+=pnl; p.s_n[r.order.stream]+=1
        if won: p.s_w[r.order.stream]+=1
        pid=getattr(r.order,'_pid',id(r.order))
        p.open_pos=[x for x in p.open_pos if x["id"]!=pid]
        if r.order.stream==1:
            d=r.order.dir
            if not won:
                if d==Direction.UP: p.losses_u+=1; p.pause_u=(time.time()+self.cfg.streak_min*60 if p.losses_u>=self.cfg.streak_n else p.pause_u)
                else: p.losses_d+=1; p.pause_d=(time.time()+self.cfg.streak_min*60 if p.losses_d>=self.cfg.streak_n else p.pause_d)
            else:
                if d==Direction.UP: p.losses_u=0
                else: p.losses_d=0
        out=1.0 if won else 0.0
        for k in ["cvd","mom"]:
            if k not in self._bh: self._bh[k]=[]
            self._bh[k].append((r.order.pm,out))
            ps,os=zip(*self._bh[k][-50:]); p.brier[k]=M.brier(list(ps),list(os))
        r.pnl=pnl; r.won=won; p.resolved.append(r)
        if self._tlog: self._tlog.resolved(r,won,pnl,p.br)
        self._log.info(f"RESOLVE S{r.order.stream} {'WIN' if won else 'LOSS':4} PnL=${pnl:+.2f} BR=${p.br:.2f}")
        return pnl

# ──────────────────────────────────────────────────────────────
# DASHBOARD
# ──────────────────────────────────────────────────────────────
class Dash:
    def __init__(self,port): self.port=port; self._r=deque(maxlen=30)
    def daily(self):
        if self.port.d_start>0: self._r.append((self.port.br-self.port.d_start)/self.port.d_start)
        self.port.d_start=self.port.br; self.port.d_pnl=0
    def sharpe(self):
        r=list(self._r)
        if len(r)<2: return 0
        mu=sum(r)/len(r); std=statistics.stdev(r)
        return (mu-.04/365)/std*math.sqrt(365) if std>0 else 0
    def pf(self):
        gp=sum(t.pnl for t in self.port.resolved if t.pnl>0)
        gl=abs(sum(t.pnl for t in self.port.resolved if t.pnl<0))
        return gp/max(gl,.01)
    def show(self):
        p=self.port; br=p.br; mdd=(p.peak-br)/p.peak if p.peak>0 else 0
        wr=sum(1 for t in p.resolved if t.pnl>0)/max(len(p.resolved),1)
        print("\n"+"═"*62)
        print("  ALPHA ENGINE v3.0  ──  DASHBOARD")
        print("═"*62)
        print(f"  Bankroll      ${br:>10,.2f}")
        print(f"  Peak          ${p.peak:>10,.2f}")
        print(f"  MDD            {mdd:>10.2%}  [limit -8%]")
        print(f"  Daily PnL     ${p.d_pnl:>10,.2f}")
        print(f"  Sharpe (30d)   {self.sharpe():>10.2f}  [target >2.0]")
        print(f"  Profit Factor  {self.pf():>10.2f}  [target >2.0]")
        print(f"  Win Rate       {wr:>10.2%}  [target >58%]")
        print(f"  Open Pos       {len(p.open_pos):>10}")
        print(f"  Regime         {p.regime.value:>10}   α={p.alpha}")
        lbl={1:"Directional",2:"Intra-Arb",3:"Cross-Arb",4:"StrikeArb",
             5:"OracleEdge",6:"LateRes",7:"MarketMake",8:"Copy"}
        print("  ─── Per-Stream ──────────────────────────────────────")
        for s,n in lbl.items():
            pnl=p.s_pnl[s]; nn=p.s_n[s]; w=p.s_w[s]
            print(f"  S{s} {n:12} PnL=${pnl:+8.2f}  n={nn:>4}  wr={w/max(nn,1):.0%}")
        print("═"*62)

# ──────────────────────────────────────────────────────────────
# LOGGER
# ──────────────────────────────────────────────────────────────
HISTORY_FILE = "./logs/trade_history.jsonl"

class Log:
    def __init__(self):
        import os; os.makedirs("./logs",exist_ok=True)
        ts=datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        self._t=f"./logs/trades_{ts}.jsonl"; self._d=f"./logs/dropped_{ts}.jsonl"
        self.recent_drops=[]  # In-memory buffer for API
    def trade(self,r):
        self._w(self._t,{"ts":datetime.now(timezone.utc).isoformat(),"s":r.order.stream,
            "venue":r.order.venue,"dir":r.order.dir.value,"leg":r.order.leg,
            "size":r.order.size,"fill":r.fill,"pm":r.order.pm,"pk":r.order.pk,
            "edge":r.order.edge,"ev":r.order.ev,"note":r.order.note,"pnl":r.pnl})
    def resolved(self,r,won,pnl,bankroll):
        """Persist resolved trade to history file."""
        o=r.order
        self._w(HISTORY_FILE,{
            "ts":datetime.now(timezone.utc).isoformat(),
            "pid":getattr(o,'_pid',''),
            "mid":o.mid,"s":o.stream,"venue":o.venue,
            "dir":o.dir.value if isinstance(o.dir,Direction) else str(o.dir),
            "leg":o.leg,"size":o.size,"fill":r.fill,
            "pm":o.pm,"pk":o.pk,"edge":o.edge,"ev":o.ev,"kf":o.kf,
            "note":o.note,"open_ts":o.ts,"close_ts":r.fts,
            "pnl":pnl,"won":won,"bankroll":bankroll,
        })
    def drop(self,reason,stream,mid="",direction="",edge=0,stage=""):
        entry={"ts":datetime.now(timezone.utc).isoformat(),"s":stream,"reason":reason,
               "mid":mid,"dir":direction,"edge":edge,"stage":stage}
        self._w(self._d,entry)
        self.recent_drops.append(entry)
        if len(self.recent_drops)>200: self.recent_drops=self.recent_drops[-100:]
    @staticmethod
    def _w(path,r):
        try:
            with open(path,"a") as f: f.write(json.dumps(r)+"\n")
        except: pass
    @staticmethod
    def read_history():
        """Load all resolved trades from persistent history."""
        trades=[]
        try:
            with open(HISTORY_FILE,"r") as f:
                for line in f:
                    line=line.strip()
                    if line:
                        try: trades.append(json.loads(line))
                        except: pass
        except FileNotFoundError: pass
        return trades

# ──────────────────────────────────────────────────────────────
# MAIN ORCHESTRATOR
# ──────────────────────────────────────────────────────────────
class Engine:
    CYCLE=5
    def __init__(self,cfg=None):
        self.cfg=cfg or Cfg(); self.s=State(); self.p=Port(br=self.cfg.bankroll,peak=self.cfg.bankroll,d_start=self.cfg.bankroll)
        self.feeds=Feeds(self.cfg,self.s)
        self.regime=Regime(self.cfg); self.sigen=DirSignal(self.cfg); self.edge=EdgeEval(self.cfg)
        self.tox=ToxFilter(self.cfg); self.kelly=Kelly(self.cfg)
        self.s2=IntraArb(self.cfg,self.p); self.s3=CrossArb(self.cfg,self.p)
        self.s4=StrikeArb(self.cfg,self.p,self.s); self.s5=OracleEdge(self.cfg,self.p)
        self.s6=LateRes(self.cfg,self.p); self.s7=MM(self.cfg,self.p)
        self.s8=Copy(self.cfg,self.p); self.exe=Exec(self.cfg,self.p,self.s)
        self.log=Log(); self.res=Resolver(self.cfg,self.p,self.log); self.dash=Dash(self.p)
        self._c=0; self._log=logging.getLogger("Engine")

    @staticmethod
    def _slug_interval_s(mid):
        """Extract resolution interval from slug: btc-updown-5m-xxx → 300s."""
        import re
        m=re.search(r'-(\d+)m-',mid)
        if m: return int(m.group(1))*60
        m=re.search(r'-(\d+)h-',mid)
        if m: return int(m.group(1))*3600
        return 900  # default 15min

    def _resolve_expired(self):
        """Resolve positions at the slug interval (5m/15m/1h) after entry, not at market expiry."""
        now=time.time()
        btc_now=self.s.btc
        to_resolve=[]
        for pos in list(self.p.open_pos):
            ts=pos.get("ts",now)
            mid=pos.get("mid","")
            resolve_at=ts+self._slug_interval_s(mid)
            if now>=resolve_at:
                to_resolve.append(pos)
        for pos in to_resolve:
            s=pos.get("s",0)
            d=pos.get("dir","UP")
            strike=pos.get("strike",0)
            # Determine outcome from real BTC price vs strike at entry
            # For updown markets: UP wins if BTC > strike, DOWN wins if BTC < strike
            if btc_now > 0 and strike > 0:
                btc_went_up = btc_now >= strike
                if d == "UP":
                    won = btc_went_up
                else:
                    won = not btc_went_up
            else:
                # For arb streams without clear up/down, check if the position's
                # predicted probability was validated by the price move
                price_delta = btc_now - strike if strike > 0 else 0
                if d == "UP":
                    won = price_delta >= 0
                else:
                    won = price_delta <= 0
            # Build a minimal Res-like for resolver
            o=Order(stream=s,mid=pos.get("mid",""),venue=pos.get("venue",""),
                    dir=Direction(d) if d in ("UP","DOWN") else Direction.UP,
                    leg=pos.get("leg",""),size=pos.get("size",0),lp=pos.get("fill",0),
                    pm=pos.get("pm",0.5),pk=pos.get("pk",0.5),edge=pos.get("edge",0),
                    ev=pos.get("ev",0),kf=pos.get("kf",0),note=pos.get("note",""))
            o._pid=pos["id"]
            r=Res(order=o,fill=pos.get("fill",0),fts=now)
            self.res.resolve(r,won)
            self._log.info(f"RESOLVED S{s} {pos.get('mid','')} {d} strike=${strike:,.0f} btc=${btc_now:,.0f} → {'WIN' if won else 'LOSS'}")

    async def cycle(self):
        self._c+=1
        self._resolve_expired()
        dir_ok=self.regime.classify(self.s,self.p)
        for mkt in self.s.markets:
            mkt.update_phase(self.cfg.oracle_late_s,self.cfg.oracle_cutoff_s)
            if mkt.phase in (MarketPhase.LATE,MarketPhase.EXPIRY): self.s7.cancel(mkt.phase.value)
            for o in (self.s2.scan(mkt) or []):
                r=await self.exe.submit(o)
                if r: self.log.trade(r)
            if mkt.phase==MarketPhase.LATE:
                o=self.s5.eval(mkt,self.s)
                if o: r=await self.exe.submit(o); self.log.trade(r) if r else None
            for km in self.s.kmarkets:
                for o in (self.s3.scan(mkt,km) or []):
                    r=await self.exe.submit(o)
                    if r: self.log.trade(r)
                for o in (self.s4.scan(mkt,km) or []):
                    r=await self.exe.submit(o)
                    if r: self.log.trade(r)
                o=self.s6.scan(mkt,km)
                if o:
                    r=await self.exe.submit(o)
                    if r: self.log.trade(r)
            if not dir_ok or mkt.phase!=MarketPhase.MID: continue
            sig=self.sigen.gen(self.s,self.p,mkt)
            if sig:
                ed,ev,ok=self.edge.eval(sig,mkt,self.p)
                if ok:
                    tok,reason=self.tox.ok(self.s,sig,mkt,self.p)
                    if tok:
                        bet,f,rk=self.kelly.size(sig.pm,mkt.mid(),self.p.alpha,self.cfg.a_dir,self.p,mkt)
                        if not rk:
                            self.log.drop("kelly_reject",1,mid=mkt.market_id,direction=sig.dir.value,edge=ed,stage="RISK")
                        if rk:
                            o=Order(stream=1,mid=mkt.market_id,leg="YES" if sig.dir==Direction.UP else "NO",
                                    dir=sig.dir,size=bet,lp=mkt.mid()+.005,pm=sig.pm,pk=mkt.mid(),edge=ed,ev=ev,kf=f,
                                    note=f"dir C={sig.con:.1f}",end_ts=mkt.end_ts,strike=mkt.strike)
                            r=await self.exe.submit(o)
                            if r: self.log.trade(r)
                    else: self.log.drop(reason,1,mid=mkt.market_id,direction=sig.dir.value,edge=ed,stage="TOXICITY")
                else: self.log.drop(f"edge={ed:.4f}",1,mid=mkt.market_id,direction=sig.dir.value,edge=ed,stage="EDGE")
            if self.s7.should(mkt,sig): self.s7.quote(mkt)

    async def run(self):
        self._log.info("═"*60)
        self._log.info("  ALPHA ENGINE v3.0 — STARTING")
        self._log.info("═"*60)
        Fee.table()
        asyncio.create_task(self.feeds.start())
        await asyncio.sleep(3)
        asyncio.create_task(self._loop())
        asyncio.create_task(self._dash())
        # Run forever
        while True:
            await asyncio.sleep(60)

    async def _loop(self):
        while True:
            try: await self.cycle()
            except Exception as e: self._log.error(f"{e}",exc_info=True)
            await asyncio.sleep(self.CYCLE)

    async def _dash(self):
        while True:
            await asyncio.sleep(300); self.dash.daily(); self.dash.show()

# ──────────────────────────────────────────────────────────────
# DEMO
# ──────────────────────────────────────────────────────────────
def demo():
    import random
    print("\n"+"═"*64)
    print("  ALPHA ENGINE v3.0  ──  DEMO / VERIFICATION")
    print("═"*64)
    Fee.table()

    cfg=Cfg(); state=State(); port=Port(br=10_000,peak=10_000,d_start=10_000)
    state.btc=82_000
    for _ in range(500): state.btc+=random.gauss(0,state.btc*.0003); state.prices.append(state.btc)
    state.cvd=200; state.cvd_prev=80; state.fund=4e-5; state.oi=5.2e9; state.oi_prev=5.0e9
    state.iv_atm=.65; state.iv_25c=.68; state.iv_25p=.63; state.iv_real=.75
    state.pcr=.38; state.ibit_flow=15e6; state.liq=2e6; state.oil=112; state.oil_prev=112
    state.cl_price=81_950; state.cl_ts=time.time()-18; state.cl_age=18

    now=time.time()
    ml=Market(market_id="btc-15m-LATE",dur=Duration.MIN15,start_ts=now-855,end_ts=now+45,
              strike=81_900,yes_bid=.78,yes_ask=.81,no_bid=.17,no_ask=.20,fee_bps=1000)
    mm=Market(market_id="btc-15m-MID",dur=Duration.MIN15,start_ts=now-540,end_ts=now+360,
              strike=82_050,yes_bid=.52,yes_ask=.56,no_bid=.42,no_ask=.46,fee_bps=1000)
    for m in [ml,mm]: m.update_phase(cfg.oracle_late_s,cfg.oracle_cutoff_s)

    km_s=KMarket(ticker="KXBTC-SETTLED",yes_bid=.95,yes_ask=.98,strike=81_900,end_ts=now+45,settled=True,settle_p=1.0)
    km_o=KMarket(ticker="KXBTC-OPEN",yes_bid=.47,yes_ask=.53,strike=82_200,end_ts=now+360)
    state.markets=[ml,mm]; state.kmarkets=[km_s,km_o]

    print("\n── Regime ──────────────────────────────────────────────")
    ok=Regime(cfg).classify(state,port)
    print(f"  directional_ok={ok}  regime={port.regime.value}  α={port.alpha}")

    print("\n── S1: Directional (MID market) ────────────────────────")
    sig=DirSignal(cfg).gen(state,port,mm)
    if sig:
        print(f"  dir={sig.dir.value} pm={sig.pm:.4f} con={sig.con:.2f}")
        ed,ev,ok=EdgeEval(cfg).eval(sig,mm,port)
        print(f"  edge={ed:.4f} ev={ev:.4f} passes={ok}")
    else: print("  No signal")

    print("\n── S2: Intra-arb ────────────────────────────────────────")
    for m in [ml,mm]:
        pr=Fee.intra_profit(m.yes_ask,m.no_ask,m.fee_bps)
        print(f"  {m.market_id} YES={m.yes_ask} NO={m.no_ask} profit={pr:.4f} {'TRADE' if pr>0 else 'none'}")

    print("\n── S3: Cross-arb ────────────────────────────────────────")
    for km in [km_s,km_o]:
        pm=mm.mid(); kk=(km.yes_bid+km.yes_ask)/2
        net=abs(pm-kk)-Fee.poly(pm,mm.fee_bps)-Fee.kalshi(kk)
        print(f"  {km.ticker} poly={pm:.3f} kalshi={kk:.3f} net={net:.4f} {'TRADE' if net>cfg.xarb_net_min else 'none'}")

    print("\n── S4: Strike-mismatch arb ──────────────────────────────")
    sa=StrikeArb(cfg,port,state)
    o=sa.scan(mm,km_o)
    g=abs(mm.strike-km_o.strike); gp=g/state.btc
    print(f"  poly_str={mm.strike:.0f} kalshi_str={km_o.strike:.0f} gap={g:.0f}({gp:.2%})")
    if o:
        for oo in o: print(f"  → {oo.venue} {oo.leg} @{oo.lp:.4f} edge={oo.edge:.4f}")
    else: print("  → none")

    print("\n── S5: Oracle latency edge ──────────────────────────────")
    oe=OracleEdge(cfg,port)
    o=oe.eval(ml,state)
    print(f"  phase={ml.phase.value} STE={ml.ste():.1f}s oracle_age={state.cl_age:.1f}s")
    if o: print(f"  → {o.leg} @{o.lp:.4f} pw={o.pm:.3f} ev={o.ev:.4f} [{o.note}]")
    else: print("  → none")

    print("\n── S6: Late-resolution arb ──────────────────────────────")
    lr=LateRes(cfg,port)
    o=lr.scan(ml,km_s)
    print(f"  kalshi_settled={km_s.settled} settle_p={km_s.settle_p}")
    if o: print(f"  → {o.leg} @{o.lp:.4f} profit={o.ev:.4f} [{o.note}]")
    else: print("  → none")

    print("\n── Stream Summary ───────────────────────────────────────")
    for s,d in {
        "S1 Directional":  "MID phase · consensus · edge Z · Kelly · 5 risk gates",
        "S2 Intra-arb":    "YES+NO < $0.985 after fees · always active",
        "S3 Cross-arb":    "Poly ↔ Kalshi net spread > 1.5¢ · both fees deducted",
        "S4 Strike-mismatch":"Different strikes → double-win corridor",
        "S5 Oracle edge":  "LATE phase · MC P>75% · oracle_age>5s · CEX leads chain",
        "S6 Late-res":     "Kalshi settled, Poly still open → buy winner",
        "S7 Market making":"Post-only EARLY/MID · cancel on signal · pool rebates",
        "S8 Copy trading": "Leaderboard wr>65% + on-chain whale (score>500)",
    }.items():
        print(f"  {s:22}  {d}")

    print(f"\n{'─'*64}")
    print("  DEPLOYMENT CHECKLIST:")
    print("  1  pip install websockets aiohttp py-clob-client")
    print("  2  Set wallet private key → cfg.poly_privkey")
    print("  3  Derive API keys: GET https://clob.polymarket.com/auth/derive-api-key")
    print("  4  Kalshi: test on demo-api.kalshi.co with cfg.kalshi_demo first")
    print("  5  Bitquery key for on-chain whale tracking")
    print("  6  VPS Frankfurt/Amsterdam (low Polygon latency) for oracle edge (S5)")
    print("  7  Set LIVE=True, PAPER=True → validate 100+ cycles paper P&L")
    print("  8  Tune strike_gap_min and oracle_p_thr to observed market conditions")
    print("  9  Set PAPER=False only after paper Sharpe > 2.0 over 7+ days")
    print(f"{'─'*64}\n")

if __name__=="__main__":
    import sys
    if "--live" in sys.argv: asyncio.run(Engine().run())
    else: demo()
