#!/usr/bin/env python3
"""
Backtester for Polymarket Alpha Engine v3.

Fetches historical BTC klines from Binance, simulates binary updown markets,
and runs the full signal pipeline (DirSignal → EdgeEval → ToxFilter → Kelly → Exec → Resolve)
to evaluate strategy performance without waiting 24 hours for live resolution.

Usage:
    python3 backtest.py                          # Default: 7 days, 15-min markets
    python3 backtest.py --days 30 --interval 5   # 30 days, 5-min markets
    python3 backtest.py --days 90 --interval 60  # 90 days, 1-hour markets
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

import requests

# Import engine components
from polymarket_alpha_engine_v3 import (
    Cfg, State, Port, Market, Duration, MarketPhase, Direction,
    DirSignal, EdgeEval, ToxFilter, Kelly, Regime, Resolver, Fee, M,
    Order, Res,
)

# ──────────────────────────────────────────────────────────────
# HISTORICAL DATA
# ──────────────────────────────────────────────────────────────
def fetch_klines(symbol="BTCUSDT", interval="1m", days=7):
    """Fetch historical klines from Binance REST API."""
    url = "https://api.binance.com/api/v3/klines"
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    all_klines = []
    cursor = start_ms
    print(f"Fetching {days} days of {interval} klines for {symbol}...")
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        all_klines.extend(batch)
        cursor = batch[-1][0] + 1  # Next ms after last candle
        sys.stdout.write(f"\r  {len(all_klines)} candles fetched...")
        sys.stdout.flush()
        time.sleep(0.1)  # Rate limit
    print(f"\r  {len(all_klines)} candles total.          ")
    return all_klines


def parse_klines(raw):
    """Parse Binance kline format into list of dicts."""
    candles = []
    for k in raw:
        candles.append({
            "ts": k[0] / 1000,        # Open time (seconds)
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "buy_vol": float(k[9]),    # Taker buy base volume
        })
    return candles


# ──────────────────────────────────────────────────────────────
# SYNTHETIC MARKET GENERATOR
# ──────────────────────────────────────────────────────────────
def create_markets(candles, interval_min=15):
    """
    Create synthetic binary updown markets from historical candles.
    Each market asks: "Will BTC be above $X at time T?"
    where X = BTC price at market creation and T = interval_min later.
    """
    markets = []
    interval_s = interval_min * 60
    # Create a market every interval_min minutes
    i = 0
    while i < len(candles):
        c = candles[i]
        start_ts = c["ts"]
        end_ts = start_ts + interval_s
        strike = c["close"]  # Strike = price at market creation

        # Find the candle at expiry to determine outcome
        expiry_idx = None
        for j in range(i + 1, len(candles)):
            if candles[j]["ts"] >= end_ts:
                expiry_idx = j
                break
        if expiry_idx is None:
            break  # Not enough data for this market

        expiry_price = candles[expiry_idx]["close"]

        # Simulate realistic bid/ask near 0.50 (fair for at-the-money)
        # Slight bias based on recent momentum
        lookback = max(0, i - 10)
        mom = (c["close"] - candles[lookback]["close"]) / candles[lookback]["close"] if candles[lookback]["close"] > 0 else 0
        fair = 0.50 + min(0.08, max(-0.08, mom * 20))  # ±8% max from momentum
        spread = 0.01
        yes_bid = round(fair - spread / 2, 4)
        yes_ask = round(fair + spread / 2, 4)
        no_bid = round(1 - yes_ask, 4)
        no_ask = round(1 - yes_bid, 4)

        # Duration enum
        if interval_min <= 5:
            dur = Duration.MIN5
        elif interval_min <= 15:
            dur = Duration.MIN15
        elif interval_min <= 60:
            dur = Duration.HOUR1
        elif interval_min <= 240:
            dur = Duration.HOUR4
        else:
            dur = Duration.DAILY

        mkt = Market(
            market_id=f"btc-updown-{interval_min}m-{int(start_ts)}",
            dur=dur,
            start_ts=start_ts,
            end_ts=end_ts,
            strike=strike,
            phase=MarketPhase.MID,  # Pre-set to MID for backtesting
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            fee_bps=1000,
        )
        markets.append({
            "market": mkt,
            "start_idx": i,
            "expiry_idx": expiry_idx,
            "expiry_price": expiry_price,
            "won_up": expiry_price >= strike,
        })
        # Advance to next market (non-overlapping)
        steps = max(1, interval_min)  # 1-min candles per interval
        i += steps
    return markets


# ──────────────────────────────────────────────────────────────
# STATE BUILDER
# ──────────────────────────────────────────────────────────────
def build_state(candles, idx, lookback=300):
    """Build a State object from historical candles at a given index."""
    s = State()
    start = max(0, idx - lookback)
    price_slice = candles[start:idx + 1]

    s.btc = candles[idx]["close"]
    s.prices = deque([c["close"] for c in price_slice], maxlen=600)

    # CVD: cumulative (buy_vol - sell_vol) over lookback
    buy_vols = [c["buy_vol"] for c in price_slice[-60:]]
    sell_vols = [c["volume"] - c["buy_vol"] for c in price_slice[-60:]]
    s.cvd = sum(buy_vols) - sum(sell_vols)
    if len(price_slice) > 60:
        prev_slice = price_slice[-120:-60] if len(price_slice) > 120 else price_slice[:len(price_slice)//2]
        prev_buy = sum(c["buy_vol"] for c in prev_slice)
        prev_sell = sum(c["volume"] - c["buy_vol"] for c in prev_slice)
        s.cvd_prev = prev_buy - prev_sell
    else:
        s.cvd_prev = s.cvd * 0.9

    # Funding rate proxy: premium of close vs VWAP
    if len(price_slice) >= 10:
        vwap = sum(c["close"] * c["volume"] for c in price_slice[-10:]) / max(sum(c["volume"] for c in price_slice[-10:]), 1)
        s.fund = (s.btc - vwap) / s.btc * 0.001  # Scaled to funding rate range
    else:
        s.fund = 0

    # OI proxy: use volume as a proxy (higher volume ≈ more open interest)
    s.oi = sum(c["volume"] for c in price_slice[-30:]) * s.btc
    if len(price_slice) > 30:
        prev_oi_slice = price_slice[-60:-30] if len(price_slice) > 60 else price_slice[:len(price_slice)//2]
        s.oi_prev = sum(c["volume"] for c in prev_oi_slice) * candles[max(0, idx - 30)]["close"]
    else:
        s.oi_prev = s.oi * 0.98

    # IV proxy: realized volatility
    if len(price_slice) >= 20:
        returns = [math.log(price_slice[j]["close"] / price_slice[j-1]["close"])
                   for j in range(max(1, len(price_slice)-20), len(price_slice))
                   if price_slice[j-1]["close"] > 0]
        if returns:
            s.iv_real = statistics.stdev(returns) * math.sqrt(525600)  # Annualized from 1-min
            s.iv_atm = s.iv_real * 1.1  # ATM IV typically slightly above realized
        else:
            s.iv_atm = 0.60
    else:
        s.iv_atm = 0.60

    # Skew: derive from price momentum
    if len(price_slice) >= 30:
        recent_ret = (price_slice[-1]["close"] - price_slice[-15]["close"]) / price_slice[-15]["close"]
        s.iv_25c = s.iv_atm * (1 + recent_ret * 2)
        s.iv_25p = s.iv_atm * (1 - recent_ret * 2)
    else:
        s.iv_25c = 0.62
        s.iv_25p = 0.61

    # PCR proxy: bearish when price dropping
    if len(price_slice) >= 10:
        ret_10 = (price_slice[-1]["close"] - price_slice[-10]["close"]) / price_slice[-10]["close"]
        s.pcr = max(0.3, min(3.0, 1.0 - ret_10 * 50))  # Scale to PCR range
    else:
        s.pcr = 1.0

    # Gold/oil and liq stay at defaults (0) to avoid GEO_RISK trigger
    s.oil = 0
    s.oil_prev = 0
    s.liq = 0

    return s


# ──────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ──────────────────────────────────────────────────────────────
@dataclass
class TradeRecord:
    market_id: str
    direction: str
    size: float
    fill: float
    pm: float
    pk: float
    edge: float
    ev: float
    strike: float
    expiry_price: float
    won: bool
    pnl: float
    timestamp: float


def run_backtest(candles, interval_min=15, cfg=None):
    """Run the full backtest."""
    if cfg is None:
        cfg = Cfg()

    # Create markets
    markets = create_markets(candles, interval_min)
    print(f"Created {len(markets)} synthetic markets ({interval_min}-min intervals)")

    # Initialize portfolio and components
    port = Port(br=cfg.bankroll, peak=cfg.bankroll, d_start=cfg.bankroll)
    regime = Regime(cfg)
    sigen = DirSignal(cfg)
    edge_eval = EdgeEval(cfg)
    tox = ToxFilter(cfg)
    kelly = Kelly(cfg)
    resolver = Resolver(cfg, port)

    trades = []
    dropped = {"edge": 0, "tox": 0, "kelly": 0, "regime": 0, "no_signal": 0, "consensus": 0}
    peak_br = cfg.bankroll
    min_br = cfg.bankroll
    daily_pnl = {}

    for i, mdata in enumerate(markets):
        mkt = mdata["market"]
        start_idx = mdata["start_idx"]
        expiry_price = mdata["expiry_price"]
        won_up = mdata["won_up"]

        # Build state at market creation time
        state = build_state(candles, start_idx)

        # Regime check
        dir_ok = regime.classify(state, port)
        if not dir_ok:
            dropped["regime"] += 1
            continue

        # Generate signal (phase already set to MID)
        sig = sigen.gen(state, port, mkt)
        if sig is None:
            dropped["no_signal"] += 1
            continue

        # Edge evaluation
        ed, ev, ok = edge_eval.eval(sig, mkt, port)
        if not ok:
            dropped["edge"] += 1
            continue

        # Toxicity filter
        tok, reason = tox.ok(state, sig, mkt, port)
        if not tok:
            dropped["tox"] += 1
            continue

        # Kelly sizing
        bet, f, rk = kelly.size(sig.pm, mkt.mid(), port.alpha, cfg.a_dir, port, mkt)
        if not rk:
            dropped["kelly"] += 1
            continue

        # Execute (paper fill)
        fill = min(mkt.mid() + 0.006, 0.999)  # Realistic fill with slippage

        # Determine outcome
        if sig.dir == Direction.UP:
            won = won_up
            leg = "YES"
        else:
            won = not won_up
            leg = "NO"

        # Calculate PnL
        if won:
            pnl = bet * (1.0 / fill - 1)
        else:
            pnl = -bet

        # Apply fee
        fee = Fee.poly(fill, mkt.fee_bps) * bet
        pnl -= fee

        # Update portfolio
        port.br += pnl
        port.peak = max(port.peak, port.br)
        port.d_pnl += pnl
        port.s_pnl[1] = port.s_pnl.get(1, 0) + pnl
        port.s_n[1] = port.s_n.get(1, 0) + 1
        if won:
            port.s_w[1] = port.s_w.get(1, 0) + 1

        # Streak management
        if not won:
            if sig.dir == Direction.UP:
                port.losses_u += 1
                if port.losses_u >= cfg.streak_n:
                    port.pause_u = candles[start_idx]["ts"] + cfg.streak_min * 60
            else:
                port.losses_d += 1
                if port.losses_d >= cfg.streak_n:
                    port.pause_d = candles[start_idx]["ts"] + cfg.streak_min * 60
        else:
            if sig.dir == Direction.UP:
                port.losses_u = 0
            else:
                port.losses_d = 0

        # Track
        peak_br = max(peak_br, port.br)
        min_br = min(min_br, port.br)

        # Daily tracking
        day_key = datetime.fromtimestamp(candles[start_idx]["ts"], tz=timezone.utc).strftime("%Y-%m-%d")
        if day_key not in daily_pnl:
            daily_pnl[day_key] = 0
        daily_pnl[day_key] += pnl

        trades.append(TradeRecord(
            market_id=mkt.market_id,
            direction=sig.dir.value,
            size=bet,
            fill=fill,
            pm=sig.pm,
            pk=mkt.mid(),
            edge=ed,
            ev=ev,
            strike=mkt.strike,
            expiry_price=expiry_price,
            won=won,
            pnl=pnl,
            timestamp=candles[start_idx]["ts"],
        ))

        # Progress
        if (i + 1) % 100 == 0:
            wr = sum(1 for t in trades if t.won) / len(trades) * 100
            sys.stdout.write(f"\r  Market {i+1}/{len(markets)} | Trades: {len(trades)} | WR: {wr:.1f}% | BR: ${port.br:,.2f}")
            sys.stdout.flush()

    print()
    return trades, port, dropped, daily_pnl, peak_br, min_br


# ──────────────────────────────────────────────────────────────
# REPORTING
# ──────────────────────────────────────────────────────────────
def print_report(trades, port, dropped, daily_pnl, peak_br, min_br, cfg, days, interval_min):
    """Print comprehensive backtest results."""
    print("\n" + "═" * 64)
    print("  BACKTEST RESULTS — Alpha Engine v3")
    print("═" * 64)

    total = len(trades)
    wins = sum(1 for t in trades if t.won)
    losses = total - wins

    print(f"\n── Configuration ───────────────────────────────────────")
    print(f"  Period:          {days} days")
    print(f"  Market interval: {interval_min} min")
    print(f"  Starting BR:     ${cfg.bankroll:,.2f}")
    print(f"  Edge min:        {cfg.edge_min}")
    print(f"  Consensus min:   {cfg.consensus_min}")
    print(f"  Max bet:         {cfg.max_bet_pct*100:.1f}% of BR")

    print(f"\n── Performance ─────────────────────────────────────────")
    print(f"  Final bankroll:  ${port.br:,.2f}")
    total_pnl = port.br - cfg.bankroll
    print(f"  Total PnL:       ${total_pnl:+,.2f} ({total_pnl/cfg.bankroll*100:+.2f}%)")
    print(f"  Peak bankroll:   ${peak_br:,.2f}")
    print(f"  Min bankroll:    ${min_br:,.2f}")
    max_dd = (peak_br - min_br) / peak_br * 100 if peak_br > 0 else 0
    print(f"  Max drawdown:    {max_dd:.2f}%")

    print(f"\n── Trade Statistics ─────────────────────────────────────")
    print(f"  Total trades:    {total}")
    print(f"  Wins:            {wins} ({wins/total*100:.1f}%)" if total > 0 else "  Wins:            0")
    print(f"  Losses:          {losses}")
    if total > 0:
        avg_win = sum(t.pnl for t in trades if t.won) / max(wins, 1)
        avg_loss = sum(t.pnl for t in trades if not t.won) / max(losses, 1)
        print(f"  Avg win:         ${avg_win:+,.2f}")
        print(f"  Avg loss:        ${avg_loss:+,.2f}")
        total_wins_pnl = sum(t.pnl for t in trades if t.won)
        total_loss_pnl = abs(sum(t.pnl for t in trades if not t.won))
        pf = total_wins_pnl / total_loss_pnl if total_loss_pnl > 0 else float('inf')
        print(f"  Profit factor:   {pf:.2f}")
        avg_edge = sum(t.edge for t in trades) / total
        print(f"  Avg edge:        {avg_edge:.4f}")
        avg_ev = sum(t.ev for t in trades) / total
        print(f"  Avg EV:          {avg_ev:.4f}")
        avg_size = sum(t.size for t in trades) / total
        print(f"  Avg size:        ${avg_size:,.2f}")
        # Sharpe ratio (annualized)
        if daily_pnl:
            daily_returns = list(daily_pnl.values())
            if len(daily_returns) >= 2:
                mean_ret = statistics.mean(daily_returns) / cfg.bankroll
                std_ret = statistics.stdev(daily_returns) / cfg.bankroll
                sharpe = mean_ret / std_ret * math.sqrt(365) if std_ret > 0 else 0
                print(f"  Sharpe (ann):    {sharpe:.2f}")

    print(f"\n── Dropped Signals ─────────────────────────────────────")
    for reason, count in sorted(dropped.items(), key=lambda x: -x[1]):
        if count > 0:
            print(f"  {reason:15s}  {count}")

    print(f"\n── Direction Breakdown ──────────────────────────────────")
    up_trades = [t for t in trades if t.direction == "UP"]
    dn_trades = [t for t in trades if t.direction == "DOWN"]
    if up_trades:
        up_wr = sum(1 for t in up_trades if t.won) / len(up_trades) * 100
        up_pnl = sum(t.pnl for t in up_trades)
        print(f"  UP:   {len(up_trades):4d} trades, WR={up_wr:.1f}%, PnL=${up_pnl:+,.2f}")
    if dn_trades:
        dn_wr = sum(1 for t in dn_trades if t.won) / len(dn_trades) * 100
        dn_pnl = sum(t.pnl for t in dn_trades)
        print(f"  DOWN: {len(dn_trades):4d} trades, WR={dn_wr:.1f}%, PnL=${dn_pnl:+,.2f}")

    if daily_pnl:
        print(f"\n── Daily PnL ───────────────────────────────────────────")
        for day, pnl in sorted(daily_pnl.items()):
            bar = "█" * max(1, int(abs(pnl) / max(abs(v) for v in daily_pnl.values()) * 20)) if daily_pnl else ""
            sign = "+" if pnl >= 0 else "-"
            color = "\033[32m" if pnl >= 0 else "\033[31m"
            print(f"  {day}  {color}${pnl:>+8.2f}\033[0m  {bar}")

    # Top 5 best/worst trades
    if trades:
        print(f"\n── Top 5 Best Trades ───────────────────────────────────")
        for t in sorted(trades, key=lambda t: t.pnl, reverse=True)[:5]:
            print(f"  ${t.pnl:>+8.2f}  {t.direction:4s}  edge={t.edge:.4f}  "
                  f"strike=${t.strike:,.0f} → ${t.expiry_price:,.0f}")

        print(f"\n── Top 5 Worst Trades ──────────────────────────────────")
        for t in sorted(trades, key=lambda t: t.pnl)[:5]:
            print(f"  ${t.pnl:>+8.2f}  {t.direction:4s}  edge={t.edge:.4f}  "
                  f"strike=${t.strike:,.0f} → ${t.expiry_price:,.0f}")

    print("\n" + "═" * 64)


# ──────────────────────────────────────────────────────────────
# PARAMETER SWEEP
# ──────────────────────────────────────────────────────────────
def sweep(candles, interval_min=15):
    """Run backtest across parameter combinations to find optimal settings."""
    print("\n" + "═" * 64)
    print("  PARAMETER SWEEP")
    print("═" * 64)

    results = []
    edge_mins = [0.010, 0.013, 0.015, 0.018, 0.020, 0.025, 0.030, 0.035]
    consensus_mins = [0.02, 0.025, 0.03, 0.035, 0.04, 0.05]
    kelly_bases = [0.10, 0.12, 0.15, 0.18, 0.20]
    max_bets = [0.02]
    max_opens = [5]

    total_combos = len(edge_mins) * len(consensus_mins) * len(kelly_bases) * len(max_bets) * len(max_opens)
    combo = 0

    for em in edge_mins:
        for cm in consensus_mins:
            for kb in kelly_bases:
                for mb in max_bets:
                    for mo in max_opens:
                        combo += 1
                        cfg = Cfg()
                        cfg.edge_min = em
                        cfg.consensus_min = cm
                        cfg.kelly_base = kb
                        cfg.max_bet_pct = mb
                        cfg.max_open = mo
                        if combo % 10 == 1:
                            sys.stdout.write(f"\r  Combo {combo}/{total_combos}  ")
                            sys.stdout.flush()

                        trades, port, dropped, daily_pnl, peak_br, min_br = run_backtest(
                            candles, interval_min, cfg
                        )
                        total_pnl = port.br - cfg.bankroll
                        n = len(trades)
                        if n < 10:
                            continue  # Skip trivial configs
                        wr = sum(1 for t in trades if t.won) / n * 100
                        dd = (peak_br - min_br) / peak_br * 100 if peak_br > 0 else 0

                        sharpe = 0
                        if daily_pnl and len(daily_pnl) >= 2:
                            daily_returns = list(daily_pnl.values())
                            mean_r = statistics.mean(daily_returns) / cfg.bankroll
                            std_r = statistics.stdev(daily_returns) / cfg.bankroll
                            sharpe = mean_r / std_r * math.sqrt(365) if std_r > 0 else 0

                        calmar = (total_pnl / cfg.bankroll) / (dd / 100) if dd > 1 else 0

                        results.append({
                            "edge_min": em, "consensus_min": cm, "kelly_base": kb,
                            "max_bet": mb, "max_open": mo,
                            "trades": n, "win_rate": wr, "pnl": total_pnl,
                            "max_dd": dd, "sharpe": sharpe, "calmar": calmar,
                            "final_br": port.br,
                        })

    print(f"\r  {combo}/{total_combos} combos done, {len(results)} viable configs")
    # Sort by Calmar (return/risk), then Sharpe
    results.sort(key=lambda r: (r["calmar"], r["sharpe"]), reverse=True)

    print(f"\n  {'edge':>5} {'con':>5} {'klly':>5} {'mbet':>5} {'mop':>3} │ "
          f"{'#':>5} {'WR%':>5} {'PnL':>10} {'DD%':>5} {'Shrp':>5} {'Calm':>5}")
    print("  " + "─" * 72)
    for r in results[:25]:
        pnl_color = "\033[32m" if r["pnl"] >= 0 else "\033[31m"
        print(f"  {r['edge_min']:>5.3f} {r['consensus_min']:>5.3f} {r['kelly_base']:>5.2f} "
              f"{r['max_bet']:>5.3f} {r['max_open']:>3d} │ "
              f"{r['trades']:>5d} {r['win_rate']:>4.1f}% "
              f"{pnl_color}${r['pnl']:>+9.2f}\033[0m {r['max_dd']:>4.1f}% "
              f"{r['sharpe']:>+5.2f} {r['calmar']:>+5.2f}")

    if results:
        best = results[0]
        print(f"\n  Best config:")
        print(f"    edge_min={best['edge_min']}, consensus_min={best['consensus_min']}, "
              f"kelly_base={best['kelly_base']}")
        print(f"    max_bet_pct={best['max_bet']}, max_open={best['max_open']}")
        print(f"  → {best['trades']} trades, {best['win_rate']:.1f}% WR, "
              f"${best['pnl']:+,.2f} PnL, {best['max_dd']:.1f}% DD, "
              f"Sharpe={best['sharpe']:.2f}, Calmar={best['calmar']:.2f}")

    return results


# ──────────────────────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────────────────────
CACHE_DIR = os.path.join(os.path.dirname(__file__), ".bt_cache")


def cache_key(days):
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(CACHE_DIR, f"klines_{days}d_{today}.json")


def load_cached(days):
    path = cache_key(days)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def save_cache(days, data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache_key(days), "w") as f:
        json.dump(data, f)


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Backtest Alpha Engine v3")
    parser.add_argument("--days", type=int, default=7, help="Days of history (default: 7)")
    parser.add_argument("--interval", type=int, default=15, help="Market interval in minutes (default: 15)")
    parser.add_argument("--sweep", action="store_true", help="Run parameter sweep")
    parser.add_argument("--no-cache", action="store_true", help="Skip cache, fetch fresh data")
    parser.add_argument("--edge-min", type=float, default=None, help="Override edge_min")
    parser.add_argument("--consensus-min", type=float, default=None, help="Override consensus_min")
    parser.add_argument("--kelly-base", type=float, default=None, help="Override kelly_base")
    parser.add_argument("--bankroll", type=float, default=10000, help="Starting bankroll (default: 10000)")
    args = parser.parse_args()

    # Fetch or load cached data
    cached = None if args.no_cache else load_cached(args.days)
    if cached:
        print(f"Using cached klines ({args.days} days)")
        raw = cached
    else:
        raw = fetch_klines(days=args.days)
        save_cache(args.days, raw)

    candles = parse_klines(raw)
    print(f"Parsed {len(candles)} candles from "
          f"{datetime.fromtimestamp(candles[0]['ts'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} to "
          f"{datetime.fromtimestamp(candles[-1]['ts'], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")

    if args.sweep:
        sweep(candles, args.interval)
        return

    # Build config with overrides
    cfg = Cfg(bankroll=args.bankroll)
    if args.edge_min is not None:
        cfg.edge_min = args.edge_min
    if args.consensus_min is not None:
        cfg.consensus_min = args.consensus_min
    if args.kelly_base is not None:
        cfg.kelly_base = args.kelly_base

    trades, port, dropped, daily_pnl, peak_br, min_br = run_backtest(candles, args.interval, cfg)
    print_report(trades, port, dropped, daily_pnl, peak_br, min_br, cfg, args.days, args.interval)


if __name__ == "__main__":
    main()
