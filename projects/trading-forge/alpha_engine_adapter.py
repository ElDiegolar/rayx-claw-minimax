"""
AlphaEngine v3 Adapter for FastAPI Server

Wraps Engine (v3) from polymarket_alpha_engine_v3.py to provide the same
interface as TradingOrchestrator for use with the existing FastAPI server.

V3 field mapping:
  Engine  → self._engine          Port.br         → bankroll
  Cfg     → config                Port.peak       → peak_bankroll
  State   → self._engine.s        Port.d_pnl      → daily_pnl
  Port    → self._engine.p        Port.d_start    → daily_start_bankroll
  Res     → resolved trade        Port.open_pos   → open positions
  Order   → trade order           Port.resolved   → resolved trades

Author: Trading Forge Team
"""

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from polymarket_alpha_engine_v3 import (
    Engine,
    Cfg,
    State,
    Port,
    Order,
    Res,
    Direction,
)
from models import PositionStatus

logger = logging.getLogger(__name__)


# =============================================================================
# POSITION PROXY
# =============================================================================

@dataclass
class PositionProxy:
    """Dot-notation compatible position for server endpoint compatibility."""
    position_id: str = "unknown"
    market_id: str = "unknown"
    direction: Any = "UNKNOWN"
    status: Any = PositionStatus.OPEN
    entry_price: float = 0.0
    size: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    open_time: Optional[datetime] = None


# =============================================================================
# PORTFOLIO WRAPPER
# =============================================================================

class PortfolioWrapper:
    """Wraps v3 Port to expose the same interface as models.Portfolio."""

    def __init__(self, port: Port, initial_bankroll: float):
        self._p = port
        self._initial_bankroll = initial_bankroll

    @property
    def bankroll(self) -> float:
        return self._p.br

    @property
    def peak_bankroll(self) -> float:
        return self._p.peak

    @property
    def start_bankroll(self) -> float:
        return self._initial_bankroll

    @property
    def total_pnl(self) -> float:
        return self._p.br - self._initial_bankroll

    @property
    def daily_pnl(self) -> float:
        return self._p.d_pnl

    @property
    def drawdown(self) -> float:
        if self._p.peak <= 0:
            return 0.0
        return (self._p.peak - self._p.br) / self._p.peak

    @property
    def exposure(self) -> float:
        total = sum(pos.get("size", 0) for pos in self._p.open_pos)
        return total / self._p.br if self._p.br > 0 else 0.0

    @property
    def num_positions(self) -> int:
        return len(self._p.open_pos)

    @property
    def total_return(self) -> float:
        if self._initial_bankroll <= 0:
            return 0.0
        return (self._p.br - self._initial_bankroll) / self._initial_bankroll

    @property
    def daily_return(self) -> float:
        if self._p.d_start <= 0:
            return 0.0
        return self._p.d_pnl / self._p.d_start

    @property
    def positions(self) -> List[PositionProxy]:
        """Convert v3 open_pos dicts to PositionProxy objects.

        V3 open_pos dict keys: id, s (stream), dir, leg, size, fill
        """
        result = []
        for pos in self._p.open_pos:
            result.append(PositionProxy(
                position_id=str(pos.get("id", "unknown")),
                market_id=pos.get("mid", "unknown"),
                direction=pos.get("dir", "UNKNOWN"),
                status=PositionStatus.OPEN,
                entry_price=pos.get("fill", 0.0),
                size=pos.get("size", 0.0),
                current_price=pos.get("fill", 0.0),
            ))
        return result


# =============================================================================
# ADAPTER
# =============================================================================

class AlphaEngineAdapter:
    """Wraps v3 Engine to provide TradingOrchestrator-compatible interface."""

    def __init__(self, config: Optional[Cfg] = None):
        self._engine = Engine(config)
        self._initial_bankroll = (config.bankroll if config else 10_000.0)
        self._portfolio = PortfolioWrapper(self._engine.p, self._initial_bankroll)

        # Server-expected state
        self.running = False
        self.last_scan_time: Optional[datetime] = None
        self.total_scans = 0
        self.total_signals = 0
        self.total_trades = 0
        self.completed_trades: List[Any] = []
        self.dropped_signals: List[Any] = []

        # Threading
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None

        logger.info("AlphaEngineAdapter v3 initialized")

    @property
    def portfolio(self) -> PortfolioWrapper:
        return self._portfolio

    def add_market(self, market: Any) -> None:
        """No-op: v3 Engine discovers markets via data feeds."""
        logger.debug(f"Market {market.id} noted (Engine uses data feeds)")

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        if self.running:
            logger.warning("Engine already running")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_async, daemon=True)
        self._thread.start()
        logger.info("Engine v3 started in background thread")

    def _run_async(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._engine.run())
        except asyncio.CancelledError:
            logger.info("Engine cancelled")
        except Exception as e:
            logger.error(f"Engine error: {e}")
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.close()
            self.running = False
            logger.info("Engine stopped")

    def stop(self) -> None:
        if not self.running:
            logger.warning("Engine is not running")
            return

        logger.info("Stopping Engine...")
        self.running = False

        if self._loop and self._loop.is_running():
            async def cancel_tasks():
                for task in asyncio.all_tasks(self._loop):
                    if task is not asyncio.current_task():
                        task.cancel()
            try:
                future = asyncio.run_coroutine_threadsafe(cancel_tasks(), self._loop)
                future.result(timeout=2)
            except Exception as e:
                logger.warning(f"Could not cancel tasks gracefully: {e}")
            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

        logger.info("Engine stopped")

    # ── Data accessors (used by server endpoints) ────────────

    def _port(self) -> Port:
        return self._engine.p

    def get_status(self) -> Dict[str, Any]:
        p = self._port()
        return {
            "running": self.running,
            "bankroll": p.br,
            "total_pnl": p.br - self._initial_bankroll,
            "daily_pnl": p.d_pnl,
            "drawdown": (p.peak - p.br) / p.peak if p.peak > 0 else 0.0,
            "num_positions": len(p.open_pos),
            "exposure": sum(x.get("size", 0) for x in p.open_pos) / p.br if p.br > 0 else 0.0,
            "last_scan": None,
        }

    def get_portfolio(self) -> Dict[str, Any]:
        p = self._port()
        total_pnl = p.br - self._initial_bankroll
        positions_data = []
        stream_labels = {1:"Directional",2:"Intra-Arb",3:"Cross-Arb",4:"Strike-Arb",
                         5:"Oracle-Edge",6:"Late-Res",7:"Market-Making",8:"Copy"}
        for pos in p.open_pos:
            s = pos.get("s", 0)
            positions_data.append({
                "position_id": str(pos.get("id", "unknown")),
                "market_id": pos.get("mid", "unknown"),
                "direction": pos.get("dir", "UNKNOWN"),
                "entry_price": pos.get("fill", 0.0),
                "size": pos.get("size", 0.0),
                "current_price": pos.get("fill", 0.0),
                "unrealized_pnl": 0.0,
                "open_time": datetime.fromtimestamp(pos.get("ts", 0)).isoformat() if pos.get("ts", 0) > 0 else None,
                "status": "OPEN",
                "stream": s,
                "stream_name": stream_labels.get(s, f"S{s}"),
                "leg": pos.get("leg", ""),
                "venue": pos.get("venue", ""),
                "p_model": pos.get("pm", 0.0),
                "p_market": pos.get("pk", 0.0),
                "edge": pos.get("edge", 0.0),
                "ev": pos.get("ev", 0.0),
                "kelly_f": pos.get("kf", 0.0),
                "note": pos.get("note", ""),
                "end_ts": pos.get("end_ts", 0),
            })
        return {
            "bankroll": p.br,
            "start_bankroll": self._initial_bankroll,
            "peak_bankroll": p.peak,
            "total_pnl": total_pnl,
            "daily_pnl": p.d_pnl,
            "drawdown": (p.peak - p.br) / p.peak if p.peak > 0 else 0.0,
            "exposure": sum(x.get("size", 0) for x in p.open_pos) / p.br if p.br > 0 else 0.0,
            "num_positions": len(p.open_pos),
            "total_return": total_pnl / self._initial_bankroll if self._initial_bankroll > 0 else 0.0,
            "daily_return": p.d_pnl / p.d_start if p.d_start > 0 else 0.0,
            "positions": positions_data,
            "per_stream_pnl": dict(p.s_pnl),
            "per_stream_trades": dict(p.s_n),
            "per_stream_wins": dict(p.s_w),
        }

    def get_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Convert v3 Res objects from Port.resolved to trade dicts.

        V3 Res fields: order (Order), fill (float), fts (float), pnl (float), won (Optional[bool])
        V3 Order fields: stream, mid, venue, dir, leg, size, lp, pm, pk, edge, ev, kf, note, ts
        """
        trades = self._port().resolved
        result = []
        for t in trades[-limit:]:
            if isinstance(t, Res):
                o = t.order
                result.append({
                    "trade_id": f"{o.mid}_{int(t.fts)}",
                    "market_id": o.mid,
                    "market_question": o.mid,
                    "direction": o.dir.value if isinstance(o.dir, Direction) else str(o.dir),
                    "signal_edge": o.edge,
                    "signal_consensus": 0.0,
                    "p_model": o.pm,
                    "p_market": o.pk,
                    "edge_confirmed": o.edge,
                    "size": o.size,
                    "entry_price": t.fill,
                    "exit_price": t.fill,
                    "pnl": t.pnl,
                    "pnl_pct": t.pnl / o.size if o.size > 0 else 0.0,
                    "open_time": datetime.fromtimestamp(o.ts).isoformat() if o.ts > 0 else None,
                    "close_time": datetime.fromtimestamp(t.fts).isoformat() if t.fts > 0 else None,
                    "status": "WON" if t.won else ("LOST" if t.won is False else "CLOSED"),
                    "execution_latency_ms": (t.fts - o.ts) * 1000 if t.fts > o.ts else 0.0,
                    "slippage": abs(t.fill - o.lp) if o.lp > 0 else 0.0,
                    "stream": o.stream,
                    "venue": o.venue,
                    "leg": o.leg,
                    "note": o.note,
                })
        return result

    def get_metrics(self) -> Dict[str, Any]:
        trades = self.get_trades(limit=10000)
        p = self._port()

        if not trades:
            return {
                "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
                "win_rate": 0.0, "total_pnl": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "profit_factor": 0.0,
                "total_signals": self.total_signals,
                "dropped_signals": len(self.dropped_signals),
                "scans": self.total_scans,
                "per_stream_pnl": dict(p.s_pnl),
            }

        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        total_pnl = sum(t["pnl"] for t in trades)
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1

        return {
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "win_rate": len(wins) / len(trades),
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": avg_win / avg_loss if avg_loss > 0 else 0,
            "total_signals": self.total_signals,
            "dropped_signals": len(self.dropped_signals),
            "scans": self.total_scans,
            "per_stream_pnl": dict(p.s_pnl),
        }


# =============================================================================
# FACTORY
# =============================================================================

def create_alpha_engine_adapter(
    total_bankroll: float = 10_000.0,
    **config_kwargs
) -> AlphaEngineAdapter:
    config = Cfg(bankroll=total_bankroll, **config_kwargs)
    return AlphaEngineAdapter(config)


if __name__ == "__main__":
    print("\n" + "=" * 58)
    print("  AlphaEngine v3 Adapter - Demo Mode")
    print("=" * 58 + "\n")

    adapter = create_alpha_engine_adapter(total_bankroll=10_000.0)
    print(f"Bankroll: ${adapter.portfolio.bankroll:.2f}")
    print(f"Total PnL: ${adapter.portfolio.total_pnl:.2f}")
    print(f"Total Return: {adapter.portfolio.total_return*100:.2f}%")
    print("\nadapter.start() to begin trading")
    print("adapter.stop()  to stop")
