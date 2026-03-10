# Build Prompt: Polymarket Alpha Engine Trading Bot

Use this prompt to instruct an AI agent to build the complete trading bot system from scratch.

---

## PROMPT

You are building a production-grade algorithmic trading bot for **binary prediction markets** (Polymarket + Kalshi). The system has 4 layers: a multi-stream trading engine, an adapter, a REST API, and a real-time dashboard.

---

### 1. TRADING ENGINE (`polymarket_alpha_engine_v3.py`)

Build an async Python trading engine with **8 independent strategy streams** that run on a 5-second cycle. All class and field names should be terse/abbreviated (e.g. `Cfg`, `Port`, `Res`, `br` for bankroll).

#### Enums & Types
- `Direction`: UP, DOWN, FLAT
- `RegimeMode`: NORMAL, GEO_RISK, CASCADE, CME_PIN
- `MarketPhase`: EARLY (0-8m), MID, LATE (T-60s), EXPIRY (T-15s), RESOLVED
- `Duration`: MIN5, MIN15

#### Configuration (`Cfg` dataclass)
```
bankroll:        10_000.0
# Per-stream capital allocation (must sum ≤ 1.0)
a_dir:           0.25    # S1 directional
a_intra:         0.10    # S2 intra-arb
a_cross:         0.20    # S3 cross-arb
a_strike:        0.15    # S4 strike-mismatch
a_oracle:        0.15    # S5 oracle edge
a_lateres:       0.10    # S6 late-resolution
a_mm:            0.04    # S7 market making
a_copy:          0.01    # S8 copy trading

# Kelly sizing
kelly_base:      0.25
kelly_oracle:    0.60    # Higher conviction
kelly_arb:       1.00    # Risk-free: full Kelly

# Hard limits
max_bet_pct:     0.02    # 2% bankroll cap per trade
max_open:        5       # Max concurrent positions (enforced globally)
daily_var:       0.05    # -5% daily loss halt
max_dd:          0.08    # -8% drawdown halt

# Edge gates
edge_min:        0.055   # 5.5% minimum edge
edge_min_late:   0.08    # Tighter in late phase
consensus_min:   6.0     # Minimum weighted consensus score

# Fees
fee_rate_bps_def: 1000   # Polymarket default (fetched live per token)
kalshi_fee_k:    0.07    # Kalshi fee constant

# Arb thresholds
intra_arb_thr:   0.985   # yes_ask + no_ask < this
xarb_net_min:    0.015   # 1.5¢ net after fees
strike_gap_min:  0.003

# Oracle timing
oracle_late_s:   60.0    # Enter in last 60s
oracle_cutoff_s: 15.0    # No orders in last 15s
oracle_p_thr:    0.75    # Monte Carlo P threshold

# Streak management
streak_n:        4       # Consecutive losses before pause
streak_min:      15      # Pause duration (minutes)
```

#### Core Data Classes
- `Market`: market_id, yes_bid, yes_ask, no_bid, no_ask, strike, fee_bps, phase, duration
- `KMarket`: Kalshi mirror — ticker, yes_bid, yes_ask, strike, settled, settle_p
- `State`: btc, cvd, funding, oi, iv_atm, iv_25c, iv_25p, pcr, cme_pain, oil, oracle_ts, oracle_lag
- `Port`: br (bankroll), peak, d_start (daily start), d_pnl (daily P&L), open_pos (list of dicts), resolved (list of Res), s_pnl/s_n/s_w (per-stream counters), alpha, losses_u, losses_d, pause_u, pause_d
- `Order`: stream, mid (market_id), venue, dir, leg, size, lp (limit price), pm (model prob), pk (market prob), edge, ev, kf (kelly fraction), note, ts
- `Res`: order, fill, fts (fill timestamp), pnl, won (Optional[bool])

#### Fee Model (`Fee` class)
- Polymarket taker fee: `p * (1-p) * bps / 10000` per $1 notional
- Kalshi fee: `ceil(0.07 * p * (1-p) * 100) / 100` per contract
- Intra-arb profit: `$1 - yes_ask - no_ask - fee_yes - fee_no`

#### Math Utilities (`M` class)
- `kelly(p, b)`: Kelly criterion = max(0, (p*b - (1-p)) / b)
- `logit(p)`: log(p / (1-p))
- `rvol(prices)`: Annualized realized volatility
- `brier(predictions, outcomes)`: Mean squared error
- `mc_p_up(spot, strike, seconds, iv)`: Monte Carlo (2000 GBM paths) for P(spot > strike)

#### Data Feeds (`Feeds` class)
Async generators (simulated for paper, real APIs for live):
- `btc()`: BTC spot price (GBM simulation in paper)
- `cvd()`: Cumulative Volume Delta
- `funding_oi()`: Funding rate + Open Interest
- `chainlink()`: Oracle price with 5-30s lag
- `deribit()`: IV surface (ATM, 25-delta call/put)
- `rvol()`: Realized vol from price history
- `ibit()`: Put-Call Ratio
- `cme()`: Max pain (options)
- `oil()`: Oil price (geo-risk signal)
- `liq()`: Liquidation events (cascade detection)
- `poly_markets()`, `kalshi_markets()`: Market discovery

#### Regime Classification (`Regime` class)
Check conditions in order:
1. CASCADE: liquidity > $50M → alpha = 0 (full stop)
2. GEO_RISK: oil shock > 3% → alpha = 0.12
3. CME_PIN: BTC within ±0.5% of max pain, expiry < 60min → alpha = 0.10
4. NORMAL: alpha = 0.25

#### The 8 Streams

**S1 — Directional Signal (`DirSignal`)**
Weighted consensus from 7 components:
| Component | Weight | Logic |
|-----------|--------|-------|
| Momentum  | 0.20   | 5-min price change direction |
| CVD       | 0.22   | Volume delta direction |
| OI        | 0.15   | OI increase + price direction |
| Funding   | 0.10   | Positive → UP bias |
| Skew      | 0.10   | Call IV - Put IV |
| IBIT      | 0.13   | PCR thresholds |
| CME Pin   | 0.10   | Proximity to max pain |

Pipeline: Signal → EdgeEval (edge > min, |dz| > 1.96) → ToxFilter (VPIN < 0.70, spread < 4%, vol spike < 2x) → Kelly sizing → Execute.

**S2 — Intra-Arb (`IntraArb`)**
Buy YES + NO on same market when yes_ask + no_ask < $0.985 after fees. Guaranteed profit.

**S3 — Cross-Venue Arb (`CrossArb`)**
Polymarket vs Kalshi price discrepancy. Net spread = |pm - km| - poly_fee - kalshi_fee. Execute both legs atomically within 10s timeout. Min net: 1.5¢.

**S4 — Strike-Mismatch Arb (`StrikeArb`)**
When Poly and Kalshi have different strikes for same underlying:
- If poly_strike > kalshi_strike → Buy Poly NO + Kalshi YES
- Profit when BTC lands in the corridor between strikes
- Score higher if currently in corridor

**S5 — Oracle Latency Edge (`OracleEdge`)**
CEX (Binance) leads Chainlink by 10-30s. In LATE phase (last 60s):
- Monte Carlo P(BTC > strike) using current CEX price
- Trade if P > 75%, oracle_age > 5s, EV > 2¢
- Kelly fraction: 0.60 (high conviction)

**S6 — Late-Resolution Arb (`LateRes`)**
Kalshi settles before Polymarket. When Kalshi confirms outcome:
- Buy winning side on Polymarket if ask < 95%
- Typical profit: 3-7¢ after fees

**S7 — Market Making (`MM`)**
Post-only at ±0.5% around mid during EARLY/MID phases:
- Cancel immediately on directional signal or LATE phase
- Revenue: spread + USDC pool rebate

**S8 — Smart-Money Copy (`Copy`)**
- Source 1: Leaderboard wallets (win rate > 65%, n > 20)
- Source 2: On-chain whale detection (bet_size / wallet_age > 500)
- Skip if directional model strongly contradicts (|pm - 0.5| > 15%)

#### Execution (`Exec` class)
- **Global guard**: Reject ALL orders when `len(open_pos) >= max_open` — this applies to every stream, not just S1
- Paper mode: 93% fill rate, +0-20bps slippage, stable position ID format: `S{stream}_{mid}_{leg}_{timestamp_ms}`
- Live mode: EIP-712 signing for Polymarket (py-clob-client), HMAC for Kalshi
- Store full reasoning in each open_pos dict: `{id, mid, s, dir, leg, size, fill, venue, pm, pk, edge, ev, kf, note, ts}`

#### Resolution (`Resolver`)
- Update: br, peak, d_pnl, per-stream counters (s_pnl, s_n, s_w)
- Remove from open_pos by stable position ID
- S1 streak tracking: 4 consecutive losses → 15-min directional pause
- Brier recalibration on trailing 50 trades

#### Engine Orchestration
- `CYCLE = 5` seconds
- Main loop: regime check → per-market scans (S2/S3/S4/S6) → S1 signal gen (MID only) → S5 eval (LATE only) → S7 quoting → logging
- Dashboard refresh every 5 minutes
- JSONL logging: `trades_{ts}.jsonl`, `dropped_{ts}.jsonl`

---

### 2. ADAPTER (`alpha_engine_adapter.py`)

Create an adapter that wraps the async Engine to provide a synchronous interface for FastAPI.

#### `PositionProxy` dataclass
Fields: position_id, market_id, direction, status (PositionStatus.OPEN), entry_price, size, current_price, unrealized_pnl, open_time. Used for dot-notation access in server endpoints.

#### `PortfolioWrapper`
Wraps `Port` with properties: bankroll→br, peak_bankroll→peak, start_bankroll (from init), total_pnl (br - initial), daily_pnl→d_pnl, drawdown ((peak-br)/peak), exposure (sum sizes / br), num_positions, total_return, daily_return, positions (converts dicts to PositionProxy list).

#### `AlphaEngineAdapter`
- **Constructor**: Creates Engine(Cfg), wraps Port in PortfolioWrapper, tracks server state (running, total_scans, total_signals, total_trades, completed_trades, dropped_signals)
- **start()**: Spawns daemon thread → creates new asyncio event loop → `engine.run()`
- **stop()**: Sets running=False, cancels tasks via `run_coroutine_threadsafe`, stops loop via `call_soon_threadsafe`, joins thread with 5s timeout
- **get_status()**: Dict with running, bankroll, total_pnl, daily_pnl, drawdown, num_positions, exposure
- **get_portfolio()**: Dict with all portfolio fields + positions array including reasoning data (stream, stream_name, p_model, p_market, edge, ev, kelly_f, venue, leg, note)
  - Stream labels: {1:"Directional", 2:"Intra-Arb", 3:"Cross-Arb", 4:"Strike-Arb", 5:"Oracle-Edge", 6:"Late-Res", 7:"Market-Making", 8:"Copy"}
- **get_trades(limit)**: Converts Res objects to trade dicts
- **get_metrics()**: Win rate, profit factor, avg win/loss, per-stream breakdown
- **Factory**: `create_alpha_engine_adapter(total_bankroll=10_000, **config_kwargs)`

**Critical**: The `/portfolio` server endpoint must call `orch.get_portfolio()` directly (returning the dict), NOT reconstruct positions from PortfolioWrapper — otherwise reasoning fields are lost.

---

### 3. REST API (`server.py`)

FastAPI application with these endpoints:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve dashboard (static/index.html) |
| GET | `/health` | Health check |
| GET | `/status` | Engine status + P&L summary |
| GET | `/portfolio` | Full portfolio with position reasoning |
| GET | `/positions` | Open positions list |
| GET | `/metrics` | Performance KPIs |
| GET | `/trades?limit=50` | Completed trade history |
| GET | `/dropped-signals?limit=50` | Rejected signals with reasons |
| GET | `/config` | Current configuration |
| POST | `/start` | Start engine (background thread) |
| POST | `/stop` | Stop engine gracefully |
| POST | `/reset` | Stop + recreate fresh engine |
| POST | `/markets` | Add market (no-op for v3) |
| PATCH | `/config` | Update runtime config |

- Mount `StaticFiles` at `/static`
- Singleton adapter with thread-safe lazy init
- Shutdown event stops engine

---

### 4. DASHBOARD (`static/index.html`)

Single-file cyberpunk-themed SPA. No frameworks — vanilla HTML/CSS/JS.

#### Design System
- **Fonts**: Orbitron (headings), Share Tech Mono (body)
- **Colors**: cyan `#00f0ff`, magenta `#ff00ff`, yellow `#ffd700`, red `#ff003c`, green `#00ff88`
- **Background**: `#0a0a12` with CRT scanline overlay
- **Panels**: `#0d0d1a` with `#1a1a2e` borders, cyan top-border accent

#### Layout
1. **Header**: Logo with glitch-hover animation, status dot (green pulse when LIVE / red when OFFLINE), START / STOP / RESET buttons
2. **Stat Cards** (4-column grid): Bankroll, Total P&L (color-coded), Drawdown, Exposure + position count
3. **Metrics Bar** (6-column): Total Trades, Win Rate, Profit Factor, Avg Win, Avg Loss, Signals Dropped
4. **Panels** (2-column + 1 full-width):
   - **Open Positions**: Clickable cards that expand to show deep-dive reasoning
   - **Recent Trades**: Table (Direction, Size, Entry, Edge, P&L, Time)
   - **Dropped Signals**: Full-width table (Direction, Edge, Stage, Reason, Details)

#### Position Deep-Dive (Expandable)
Each position card shows summary (direction, market, stream tag, size, price). On click, expands to reveal:
- Model Prob vs Market Prob
- Edge (green if positive, red if negative)
- Expected Value
- Kelly Fraction
- Venue, Leg, Open Time
- Reasoning note (from engine)

**Important UX details**:
- Track expanded position IDs in a `Set` so they survive the 3-second auto-refresh cycle
- Guard all number formatting against NaN/null (show `--` instead)
- Show fallback message for positions opened before reasoning data was available
- Toggle hint text: "CLICK TO INSPECT" / "CLICK TO CLOSE"

#### Auto-Refresh
- `refreshAll()` calls all 5 endpoints in parallel via `Promise.all()`
- `setInterval(refreshAll, 3000)` — every 3 seconds
- Responsive: 4-col → 2-col at 1200px, 1-col at 768px

---

### 5. DATA MODELS (`models.py`)

Pydantic-compatible dataclasses and enums:

**Enums**: Direction (UP/DOWN), PositionStatus (OPEN/CLOSED/PENDING/CANCELLED), OrderStatus, OrderType, MarketResolution, SignalStage (5 pipeline stages), DropReason (17 rejection reasons)

**Classes**: Market (with spread/liquidity properties), MarketData (with momentum/skew/vol_ratio), Signal, Order (with remaining_size/is_active), Trade, Position (with update_price/to_trade), Portfolio (with update_peak/update_from_trade), CalibrationData, PerformanceMetrics, DroppedSignal

---

### 6. CONFIGURATION (`config.py`)

Module-level constants organized by category:
- Signal generation weights and thresholds
- Gating (consensus, edge, z-score, brier)
- Toxicity filters (VPIN, spread, vol spike)
- Risk management (Kelly, VAR, drawdown, position limits, streaks)
- Performance targets (Sharpe > 2.0, win rate > 58%, profit factor > 1.5)
- Execution (retries, timeouts, slippage)
- Calibration windows
- Feature flags (paper trading, auto-calibration, dynamic Kelly, streak management, circuit breakers)
- API endpoints (Polymarket REST + WS, rate limits)

---

### 7. LOGGING (`logger.py`)

- `TradeLogger`: CSV with all trade fields
- `DroppedSignalLogger`: CSV with stage/reason/details
- `PerformanceLogger`: JSON metrics (Sharpe, drawdown, streaks)
- `DebugLogger`: File-based debug
- `TradingLogger`: Unified facade
- Output to `./logs/`

---

### KEY ARCHITECTURE DECISIONS

1. **Global max_open enforcement**: The `max_open` check MUST be in `Exec.submit()`, not just in Kelly sizing. Streams S2-S8 bypass Kelly entirely and would pile up unlimited positions otherwise.

2. **Stable position IDs**: Use `S{stream}_{market_id}_{leg}_{timestamp_ms}` — NOT Python `id(obj)` which changes between cycles and breaks UI state tracking.

3. **Adapter returns raw dicts**: The `/portfolio` endpoint must return `orch.get_portfolio()` directly. Do NOT re-serialize through a Pydantic model that strips reasoning fields.

4. **Thread-safe async bridge**: Engine runs in its own thread with its own event loop. Stop uses `run_coroutine_threadsafe` for task cancellation + `call_soon_threadsafe` for loop stop. Never call `loop.stop()` from inside a coroutine on the same loop.

5. **Fee-aware everything**: Every EV calculation deducts venue-specific fees. Edge = pm - pk is meaningless without fee adjustment.

6. **Phase gating**: S1 only on MID, S5 only on LATE, S7 cancels on LATE. This prevents trading in dangerous timing windows.

---

### DEPLOYMENT CHECKLIST

1. `pip install fastapi uvicorn aiohttp websockets pydantic`
2. Set `PAPER = True` (default)
3. Run: `python run_server_alpha.py --port 8000 --reload`
4. Open `http://localhost:8000/` → click START
5. Validate paper trading for 7+ days, target Sharpe > 2.0
6. For live: install `py-clob-client`, set wallet keys, toggle `PAPER = False`
7. Deploy on Frankfurt/Amsterdam VPS for Chainlink oracle latency edge
