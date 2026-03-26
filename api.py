"""Dashboard API — FastAPI server for the trading terminal.

Runs inside the bot's async event loop, sharing the BotState object directly.
Provides REST endpoints for state queries and a WebSocket for live streaming.

Endpoints:
    GET  /api/state          Full bot state snapshot
    GET  /api/positions      Open positions only
    GET  /api/trades         Recent trades from DB
    GET  /api/trades/{pair}  Trades for a specific pair
    GET  /api/weights        Current signal weights
    GET  /api/config         Active configuration
    GET  /api/portfolio      Portfolio snapshots for equity curve
    GET  /api/klines/{pair}  Candlestick data from Binance
    POST /api/control/pause  Toggle pause
    POST /api/control/threshold  Adjust buy threshold
    POST /api/control/close-all  Emergency close all positions
    WS   /ws                 Live state stream (1Hz)

Usage:
    Called from bot.py at startup:
        from api import start_api_server
        await start_api_server(state, binance)
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import uvicorn

from config import (
    USE_TESTNET, TRADING_PAIRS, CONFIDENCE_BUY_THRESHOLD,
    STOP_LOSS_PCT, PROFIT_TARGET_PCT, USE_ATR_EXITS,
    ATR_TRAILING_MULTIPLIER, ATR_TP_MULTIPLIER,
    ATR_TRAIL_ACTIVATION_PCT, ATR_MAX_TRAIL_PCT,
    TRADE_SIZE_PCT, MAX_POSITION_PCT, PORTFOLIO_RESERVE_PCT,
    DRAWDOWN_TRIGGER_PCT, TRADE_COOLDOWN_SECONDS,
    SIGNAL_CATEGORY_CAPS, REGIME_MODIFIERS,
    get_base_asset,
)
from logger import get_recent_trades, get_trades_for_pair, get_trade_stats, get_portfolio_snapshots

logger = logging.getLogger("trading_bot.api")

app = FastAPI(title="Trading Bot Dashboard API", docs_url=None, redoc_url=None)

# Shared references — set by start_api_server()
_state = None
_binance = None

# Active WebSocket connections for broadcasting
_ws_clients: set[WebSocket] = set()

# ─── Static Files ────────────────────────────────────────────────────

DASHBOARD_DIR = Path(__file__).parent / "dashboard"
app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")


@app.get("/")
async def index():
    """Serve the main dashboard page."""
    return FileResponse(str(DASHBOARD_DIR / "index.html"))


# ─── Helper: serialize state ─────────────────────────────────────────

def _serialize_pair(ps) -> dict:
    """Serialize a PairState to a JSON-friendly dict."""
    position_value = ps.asset_balance * ps.current_price if ps.current_price > 0 else 0
    pl_pct = 0.0
    if ps.buy_price > 0 and ps.asset_balance > 0:
        pl_pct = (ps.current_price - ps.buy_price) / ps.buy_price * 100

    return {
        "pair": ps.pair,
        "base_asset": ps.base_asset,
        "price": round(ps.current_price, 6),
        # Indicators
        "rsi": round(ps.rsi, 2),
        "macd": round(ps.macd, 6),
        "macd_signal": round(ps.macd_signal, 6),
        "macd_histogram": round(ps.macd_histogram, 6),
        "macd_crossover": ps.macd_crossover,
        "bb_upper": round(ps.bb_upper, 6),
        "bb_middle": round(ps.bb_middle, 6),
        "bb_lower": round(ps.bb_lower, 6),
        "bb_position": ps.bb_position,
        "bb_squeeze": ps.bb_squeeze,
        # Signals
        "etherscan_gas": ps.etherscan_gas,
        "etherscan_activity": ps.etherscan_activity,
        # Strategy
        "confidence": ps.confidence_score,
        "confidence_weighted": round(ps.confidence_weighted, 2),
        "max_score": ps.max_score,
        "decision": ps.decision,
        # Regime & ATR
        "regime": ps.regime,
        "atr": round(ps.atr, 6),
        "atr_pct": round(ps.atr_pct, 6),
        "trend_strength": round(ps.trend_strength, 6),
        "trailing_stop": round(ps.trailing_stop, 6),
        "atr_take_profit": round(ps.atr_take_profit, 6),
        "wick_ratio": round(ps.wick_ratio, 4),
        # Position
        "asset_balance": ps.asset_balance,
        "buy_price": round(ps.buy_price, 6),
        "position_value": round(position_value, 2),
        "pl_usdt": round(ps.pl_usdt, 4),
        "pl_pct": round(pl_pct, 2),
        "pl_today_usdt": round(ps.pl_today_usdt, 4),
        "allocated_usdt": round(ps.allocated_usdt, 2),
        "last_trade_time": ps.last_trade_time,
        "volatility": round(ps.volatility, 6),
    }


def _serialize_state() -> dict:
    """Build full state snapshot for API/WebSocket."""
    if _state is None:
        return {"error": "Bot not initialized"}

    pairs = {}
    for pair_name, ps in _state.pairs.items():
        pairs[pair_name] = _serialize_pair(ps)

    uptime = ""
    if _state.bot_uptime_start:
        delta = datetime.now(timezone.utc) - _state.bot_uptime_start
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        mins, secs = divmod(rem, 60)
        uptime = f"{hours}h {mins}m {secs}s"

    win_rate = 0.0
    if _state.total_sells > 0:
        win_rate = round(_state.profitable_trades / _state.total_sells * 100, 1)

    return {
        "pairs": pairs,
        "portfolio": {
            "total_value": round(_state.portfolio_current_value, 2),
            "start_value": round(_state.portfolio_start_value, 2),
            "usdt_balance": round(_state.usdt_balance, 2),
            "usdt_reserved": round(_state.usdt_reserved, 2),
            "pl_usdt": round(_state.portfolio_pl_usdt, 4),
            "pl_pct": round(_state.portfolio_pl_pct, 2),
            "peak_value": round(_state.portfolio_peak_value, 2),
            "drawdown_pct": round(_state.drawdown_pct, 2),
            "in_drawdown": _state.in_drawdown,
            "open_positions": _state.open_positions_count,
        },
        "stats": {
            "total_trades": _state.total_trades,
            "buys": _state.total_buys,
            "sells": _state.total_sells,
            "profitable": _state.profitable_trades,
            "win_rate": win_rate,
        },
        "market": {
            "btc_crash_active": _state.btc_crash_active,
            "btc_crash_until": _state.btc_crash_until,
            "btc_15m_change_pct": _state.btc_15m_change_pct,
        },
        "signal_weights": {k: round(v, 3) for k, v in _state.signal_weights.items()},
        "bot": {
            "paused": _state.is_paused,
            "uptime": uptime,
            "mode": "TESTNET" if USE_TESTNET else "LIVE",
            "threshold": CONFIDENCE_BUY_THRESHOLD,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─── REST Endpoints ──────────────────────────────────────────────────

@app.get("/api/state")
async def get_state():
    return _serialize_state()


@app.get("/api/positions")
async def get_positions():
    if _state is None:
        return {"positions": []}
    positions = []
    for pair_name, ps in _state.pairs.items():
        if ps.asset_balance > 0:
            positions.append(_serialize_pair(ps))
    return {"positions": positions}


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    trades = await asyncio.get_running_loop().run_in_executor(
        None, get_recent_trades, limit
    )
    # Parse signal_snapshot JSON strings
    for t in trades:
        if isinstance(t.get("signal_snapshot"), str):
            try:
                t["signal_snapshot"] = json.loads(t["signal_snapshot"])
            except (json.JSONDecodeError, TypeError):
                t["signal_snapshot"] = {}
    return {"trades": trades}


@app.get("/api/trades/{pair}")
async def get_pair_trades(pair: str, limit: int = 50):
    pair = pair.upper()
    trades = await asyncio.get_running_loop().run_in_executor(
        None, get_trades_for_pair, pair, limit
    )
    for t in trades:
        if isinstance(t.get("signal_snapshot"), str):
            try:
                t["signal_snapshot"] = json.loads(t["signal_snapshot"])
            except (json.JSONDecodeError, TypeError):
                t["signal_snapshot"] = {}
    return {"trades": trades}


@app.get("/api/weights")
async def get_weights():
    if _state is None:
        return {"weights": {}}
    return {
        "weights": {k: round(v, 3) for k, v in _state.signal_weights.items()},
    }


@app.get("/api/config")
async def get_config():
    return {
        "mode": "TESTNET" if USE_TESTNET else "LIVE",
        "pairs": [p["pair"] for p in TRADING_PAIRS],
        "buy_threshold": CONFIDENCE_BUY_THRESHOLD,
        "stop_loss_pct": STOP_LOSS_PCT,
        "profit_target_pct": PROFIT_TARGET_PCT,
        "use_atr_exits": USE_ATR_EXITS,
        "atr_trailing_multiplier": ATR_TRAILING_MULTIPLIER,
        "atr_tp_multiplier": ATR_TP_MULTIPLIER,
        "atr_trail_activation_pct": ATR_TRAIL_ACTIVATION_PCT,
        "atr_max_trail_pct": ATR_MAX_TRAIL_PCT,
        "trade_size_pct": TRADE_SIZE_PCT,
        "max_position_pct": MAX_POSITION_PCT,
        "reserve_pct": PORTFOLIO_RESERVE_PCT,
        "drawdown_trigger_pct": DRAWDOWN_TRIGGER_PCT,
        "cooldown_seconds": TRADE_COOLDOWN_SECONDS,
        "category_caps": SIGNAL_CATEGORY_CAPS,
        "regime_modifiers": REGIME_MODIFIERS,
    }


@app.get("/api/portfolio")
async def get_portfolio():
    snapshots = await asyncio.get_running_loop().run_in_executor(
        None, get_portfolio_snapshots, 200
    )
    stats = await asyncio.get_running_loop().run_in_executor(
        None, get_trade_stats
    )
    return {"snapshots": snapshots, "stats": stats}


@app.get("/api/klines/{pair}")
async def get_klines(pair: str, interval: str = "15m", limit: int = 100):
    """Fetch candlestick data from Binance for charting."""
    if _binance is None or _binance.client is None:
        raise HTTPException(503, "Binance client not connected")
    pair = pair.upper()
    klines = await _binance.get_klines(pair, interval=interval, limit=limit)
    # Format: [{time, open, high, low, close, volume}, ...]
    candles = []
    for k in klines:
        candles.append({
            "time": int(k[0]) // 1000,  # ms -> seconds for TradingView
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    return {"candles": candles, "pair": pair, "interval": interval}


# ─── Control Endpoints ───────────────────────────────────────────────

@app.post("/api/control/pause")
async def toggle_pause():
    if _state is None:
        raise HTTPException(503, "Bot not initialized")
    async with _state.lock:
        _state.is_paused = not _state.is_paused
    status = "paused" if _state.is_paused else "running"
    logger.info(f"Dashboard: bot {status}")
    return {"paused": _state.is_paused, "status": status}


@app.post("/api/control/threshold")
async def set_threshold(value: float):
    """Adjust the buy confidence threshold at runtime."""
    import config
    if value < 0 or value > 10:
        raise HTTPException(400, "Threshold must be between 0 and 10")
    old = config.CONFIDENCE_BUY_THRESHOLD
    config.CONFIDENCE_BUY_THRESHOLD = value
    logger.info(f"Dashboard: threshold changed {old} -> {value}")
    return {"old_threshold": old, "new_threshold": value}


@app.post("/api/control/close-all")
async def close_all():
    """Emergency close all open positions."""
    if _state is None or _binance is None:
        raise HTTPException(503, "Bot not initialized")

    closed = []
    for pair_name, ps in _state.pairs.items():
        if ps.asset_balance > 0:
            order = await _binance.place_market_sell(pair_name, ps.asset_balance)
            if order:
                filled = float(order.get("executedQty", 0))
                filled_usdt = float(order.get("cummulativeQuoteQty", 0))
                async with _state.lock:
                    ps.asset_balance = 0.0
                    ps.buy_price = 0.0
                    ps.trailing_stop = 0.0
                    ps.atr_take_profit = 0.0
                    _state.usdt_balance += filled_usdt
                closed.append({"pair": pair_name, "sold": filled, "usdt": filled_usdt})
                logger.warning(f"Dashboard: emergency close {pair_name} — {filled} for {filled_usdt:.2f} USDT")

    return {"closed": closed, "count": len(closed)}


# ─── WebSocket ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    logger.info(f"Dashboard WebSocket connected ({len(_ws_clients)} clients)")
    try:
        # Send initial state immediately
        await ws.send_json(_serialize_state())
        # Keep connection alive — the broadcast loop pushes updates
        while True:
            # Listen for pings or control messages from client
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
                # Handle client commands if needed
                if data == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                break
    except Exception:
        pass
    finally:
        _ws_clients.discard(ws)
        logger.info(f"Dashboard WebSocket disconnected ({len(_ws_clients)} clients)")


async def _ws_broadcast_loop():
    """Push state to all connected WebSocket clients at ~1Hz."""
    while True:
        if _ws_clients:
            snapshot = _serialize_state()
            dead = set()
            # Create a list copy to avoid RuntimeErrors during iteration
            for ws in list(_ws_clients): 
                try:
                    await ws.send_json(snapshot)
                except Exception:
                    dead.add(ws)
            
            # Safely remove disconnected clients (use discard, not -=, to avoid reassignment)
            for ws in dead:
                _ws_clients.discard(ws)
        await asyncio.sleep(1)

# ─── Server Startup ──────────────────────────────────────────────────

async def start_api_server(bot_state, binance_client, host="0.0.0.0", port=None):
    """Start the FastAPI server as a background task in the bot's event loop.

    Args:
        bot_state: Shared BotState object (direct reference, no copying)
        binance_client: BinanceClient instance for klines + emergency close
        host: Bind address (0.0.0.0 = accessible from network)
        port: HTTP port (default 8080)
    """
    import os
    global _state, _binance
    _state = bot_state
    _binance = binance_client

    if port is None:
        port = int(os.environ.get("PORT", 8081))

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    # Disable uvicorn's signal handlers — the bot's main() handles Ctrl+C.
    # Without this, uvicorn captures SIGINT and re-raises it during cleanup,
    # causing a double KeyboardInterrupt traceback on shutdown.
    server.install_signal_handlers = lambda: None

    # Start WebSocket broadcast loop
    asyncio.create_task(_ws_broadcast_loop())

    logger.info(f"Dashboard API starting on http://{host}:{port}")
    await server.serve()
