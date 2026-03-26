"""Trade Logger — SQLite persistence for trades, portfolio snapshots, and bot state.

Tables:
- trades: every BUY/SELL/STOP_LOSS action with full signal snapshot
- portfolio_snapshots: periodic value snapshots for charting portfolio growth

Signal snapshot JSON schema (stored per trade):
{
    "rsi": 0|1, "macd": 0|1, "bollinger": 0|1,
    "fear_greed": 0.0-1.0, "volume": 0|1,
    "whale": 0|1, "gas": 0|1, "trends": 0|1
}
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone

logger = logging.getLogger("trading_bot.logger")

DB_PATH = "trades.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_trades_table(conn: sqlite3.Connection):
    """Add new columns to existing trades table if they don't exist yet."""
    cursor = conn.execute("PRAGMA table_info(trades)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_columns = {
        "signal_snapshot": "TEXT DEFAULT '{}'",
        "outcome_label": "TEXT DEFAULT ''",
        "regime_at_entry": "TEXT DEFAULT ''",
        "atr_value": "REAL DEFAULT 0.0",
        "confidence_weighted": "REAL DEFAULT 0.0",
        "hold_minutes": "REAL DEFAULT 0.0",
        "entry_time": "TEXT DEFAULT ''",
    }
    for col, col_type in new_columns.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {col} {col_type}")
            logger.info(f"Migrated trades table: added column '{col}'")
    conn.commit()


def init_db():
    """Create all tables if they don't exist, then migrate."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            pair TEXT NOT NULL,
            action TEXT NOT NULL,
            coin_price REAL,
            amount_coin REAL,
            amount_usdt REAL,
            rsi REAL,
            fear_greed INTEGER,
            whale_signal TEXT,
            confidence INTEGER,
            pl_usdt REAL,
            note TEXT,
            signal_snapshot TEXT DEFAULT '{}',
            outcome_label TEXT DEFAULT '',
            regime_at_entry TEXT DEFAULT '',
            atr_value REAL DEFAULT 0.0,
            confidence_weighted REAL DEFAULT 0.0,
            hold_minutes REAL DEFAULT 0.0,
            entry_time TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            total_value REAL,
            pl_usdt REAL,
            pl_pct REAL
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            global_state TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pair_states (
            pair TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    # Migrate existing tables that may lack new columns
    _migrate_trades_table(conn)
    conn.close()
    logger.info("Database initialized")


# Canonical set of exit action strings written to trades.action.
# Every consumer (panel, analytics, weight learner) must use this tuple.
_ALL_SELL_ACTIONS = (
    "SELL",
    "STOP_LOSS",
    "SIGNAL_SELL",
    "EARLY_FAILURE",
    "HARD_STOP",
    "TRAIL_STOP",
    "TIME_STOP",
    "MAX_HOLD_EXIT",
    "TP1_FULL_EXIT",
    "FORCE SELL",      # Discord manual sell (space kept for backwards compat)
)


def label_outcome(pl_pct: float) -> str:
    """Label a trade outcome by quality, not just win/loss.

    Args:
        pl_pct: Profit/loss as percentage of cost basis

    Returns:
        "strong_win" | "weak_win" | "scratch" | "loss"
    """
    if pl_pct > 0.5:
        return "strong_win"
    elif pl_pct > 0:
        return "weak_win"
    elif pl_pct > -1.0:
        return "scratch"
    else:
        return "loss"


def log_trade(
    pair: str,
    action: str,
    coin_price: float,
    amount_coin: float,
    amount_usdt: float,
    rsi: float = 0.0,
    fear_greed: int = 0,
    whale_signal: str = "neutral",
    confidence: int = 0,
    pl_usdt: float = 0.0,
    note: str = "",
    signal_snapshot: dict | None = None,
    regime_at_entry: str = "",
    atr_value: float = 0.0,
    confidence_weighted: float = 0.0,
    hold_minutes: float = 0.0,
    entry_time: str = "",
) -> int | None:
    """Log a trade to the database with full signal snapshot.

    Args:
        pair: Trading pair e.g. "ETHUSDT"
        action: "BUY" | "SELL" | "STOP_LOSS"
        coin_price: Price at time of trade
        amount_coin: Amount of coin bought/sold
        amount_usdt: USDT spent/received
        signal_snapshot: Dict of all signal values at entry time
        regime_at_entry: Market regime at entry ("trending", "ranging", etc.)
        atr_value: ATR value at entry (for exit calibration)
        confidence_weighted: Weighted confidence score (float, not raw int)
        pl_usdt: Profit/loss on this trade
        note: Extra info

    Returns:
        Row ID of inserted trade, or None on error
    """
    # Compute outcome label for all exit trades
    outcome = ""
    if action in _ALL_SELL_ACTIONS and amount_usdt > 0:
        cost_basis = coin_price * amount_coin  # approximate
        if cost_basis > 0:
            pl_pct = (pl_usdt / cost_basis) * 100
            outcome = label_outcome(pl_pct)

    snapshot_json = json.dumps(signal_snapshot) if signal_snapshot else "{}"

    try:
        conn = _get_conn()
        cursor = conn.execute(
            """INSERT INTO trades
               (timestamp, pair, action, coin_price, amount_coin, amount_usdt,
                rsi, fear_greed, whale_signal, confidence,
                pl_usdt, note,
                signal_snapshot, outcome_label, regime_at_entry,
                atr_value, confidence_weighted, hold_minutes, entry_time)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                pair,
                action,
                coin_price,
                amount_coin,
                amount_usdt,
                rsi,
                fear_greed,
                whale_signal,
                confidence,
                pl_usdt,
                note,
                snapshot_json,
                outcome,
                regime_at_entry,
                atr_value,
                confidence_weighted,
                hold_minutes,
                entry_time,
            ),
        )
        conn.commit()
        row_id = cursor.lastrowid
        conn.close()
        return row_id
    except Exception as e:
        logger.error(f"Failed to log trade: {e}")
        return None


def get_recent_trades(limit: int = 10) -> list[dict]:
    """Get the most recent trades across all pairs.

    Args:
        limit: Number of trades to return

    Returns:
        List of trade dicts, newest first
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to get recent trades: {e}")
        return []


def get_filtered_trades(filter_key: str = "all", limit: int = 20) -> list[dict]:
    """Get trades filtered by category for the Discord trade history view.

    filter_key values and what they return:
        "all"               — most recent trades of any type
        "buys"              — BUY and BUY_FVG entries only
        "profitable"        — any exit with pl_usdt > 0
        "losing"            — any exit with pl_usdt < 0
        "signal_sell"       — SIGNAL_SELL exits
        "trail_stop"        — TRAIL_STOP exits
        "hard_stop"         — HARD_STOP exits
        "early_stagnation"  — EARLY_STAGNATION and EARLY_STAGNATION_2 exits
        "time_stop"         — TIME_STOP and MAX_HOLD_EXIT exits
        "tp1"               — TP1_FULL_EXIT exits
        "futures_paper"     — all FUTURES_* paper trading entries and exits
    """
    # EARLY_STAGNATION variants are valid exit actions but not in _ALL_SELL_ACTIONS
    # (which is used for analytics/weight-learning). Include them here for complete
    # exit coverage in the history filter.
    _ALL_EXIT_ACTIONS = _ALL_SELL_ACTIONS + ("EARLY_STAGNATION", "EARLY_STAGNATION_2")

    # Per-category action lists used by the action-based filters
    _FILTER_ACTIONS: dict[str, tuple] = {
        "buys":             ("BUY", "BUY_FVG"),
        "signal_sell":      ("SIGNAL_SELL",),
        "trail_stop":       ("TRAIL_STOP",),
        "hard_stop":        ("HARD_STOP",),
        "early_stagnation": ("EARLY_STAGNATION", "EARLY_STAGNATION_2"),
        "time_stop":        ("TIME_STOP", "MAX_HOLD_EXIT"),
        "tp1":              ("TP1_FULL_EXIT",),
        # Futures paper trading actions (all kept together in one filter)
        "futures_paper":    (
            "FUTURES_SHORT_ENTRY", "FUTURES_LONG_ENTRY",
            "FUTURES_TP1",
            "FUTURES_SIGNAL_EXIT", "FUTURES_TRAIL_STOP",
            "FUTURES_HARD_STOP", "FUTURES_EARLY_STAGNATION",
            "FUTURES_TIME_STOP",
        ),
    }

    try:
        conn = _get_conn()

        if filter_key == "all":
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

        elif filter_key == "profitable":
            ph = ",".join("?" * len(_ALL_EXIT_ACTIONS))
            rows = conn.execute(
                f"SELECT * FROM trades WHERE action IN ({ph}) AND pl_usdt > 0 "
                f"ORDER BY id DESC LIMIT ?",
                (*_ALL_EXIT_ACTIONS, limit),
            ).fetchall()

        elif filter_key == "losing":
            ph = ",".join("?" * len(_ALL_EXIT_ACTIONS))
            rows = conn.execute(
                f"SELECT * FROM trades WHERE action IN ({ph}) AND pl_usdt < 0 "
                f"ORDER BY id DESC LIMIT ?",
                (*_ALL_EXIT_ACTIONS, limit),
            ).fetchall()

        else:
            actions = _FILTER_ACTIONS.get(filter_key)
            if not actions:
                # Unknown key — fall back to all recent trades
                rows = conn.execute(
                    "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                ph = ",".join("?" * len(actions))
                rows = conn.execute(
                    f"SELECT * FROM trades WHERE action IN ({ph}) ORDER BY id DESC LIMIT ?",
                    (*actions, limit),
                ).fetchall()

        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to get filtered trades ({filter_key}): {e}")
        return []


def get_trades_for_pair(pair: str, limit: int = 50) -> list[dict]:
    """Get recent trades for a specific pair.

    Args:
        pair: Trading pair e.g. "ETHUSDT"
        limit: Number of trades to return

    Returns:
        List of trade dicts, newest first
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM trades WHERE pair = ? ORDER BY id DESC LIMIT ?",
            (pair, limit),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Failed to get trades for {pair}: {e}")
        return []


def get_trades_with_snapshots(limit: int = 300) -> list[dict]:
    """Get recent SELL trades with signal snapshots for weight learning.

    Only returns trades that have a non-empty signal_snapshot and outcome_label.
    These are the trades the weight learner uses to compute per-signal reliability.

    Args:
        limit: Max trades to return (default 300 = SIGNAL_WEIGHT_WINDOW)

    Returns:
        List of trade dicts with parsed signal_snapshot, newest first
    """
    placeholders = ",".join("?" * len(_ALL_SELL_ACTIONS))
    try:
        conn = _get_conn()
        rows = conn.execute(
            f"""SELECT * FROM trades
               WHERE action IN ({placeholders})
               AND signal_snapshot != '{{}}'
               AND outcome_label != ''
               ORDER BY id DESC LIMIT ?""",
            (*_ALL_SELL_ACTIONS, limit),
        ).fetchall()
        conn.close()
        results = []
        for row in rows:
            d = dict(row)
            try:
                d["signal_snapshot"] = json.loads(d.get("signal_snapshot", "{}"))
            except (json.JSONDecodeError, TypeError):
                d["signal_snapshot"] = {}
            results.append(d)
        return results
    except Exception as e:
        logger.error(f"Failed to get trades with snapshots: {e}")
        return []


def get_trade_stats() -> dict:
    """Get overall trade statistics.

    Returns:
        dict with total_trades, buys, sells, profitable, win_rate, total_pl
    """
    placeholders = ",".join("?" * len(_ALL_SELL_ACTIONS))
    try:
        conn = _get_conn()
        total = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        buys = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE action = 'BUY'"
        ).fetchone()[0]
        sells = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE action IN ({placeholders})",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0]
        profitable = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE action IN ({placeholders}) AND pl_usdt > 0",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0]
        total_pl = conn.execute(
            f"SELECT COALESCE(SUM(pl_usdt), 0) FROM trades WHERE action IN ({placeholders})",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0]
        total_gain = conn.execute(
            f"SELECT COALESCE(SUM(pl_usdt), 0) FROM trades "
            f"WHERE action IN ({placeholders}) AND pl_usdt > 0",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0]
        total_loss = conn.execute(
            f"SELECT COALESCE(SUM(pl_usdt), 0) FROM trades "
            f"WHERE action IN ({placeholders}) AND pl_usdt < 0",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0]
        conn.close()

        win_rate = (profitable / sells * 100) if sells > 0 else 0.0

        return {
            "total_trades": total,
            "buys": buys,
            "sells": sells,
            "profitable": profitable,
            "win_rate": round(win_rate, 1),
            "total_pl": round(total_pl, 4),
            "total_gain": round(total_gain, 4),
            "total_loss": round(total_loss, 4),
        }
    except Exception as e:
        logger.error(f"Failed to get trade stats: {e}")
        return {
            "total_trades": 0, "buys": 0, "sells": 0,
            "profitable": 0, "win_rate": 0.0, "total_pl": 0.0,
            "total_gain": 0.0, "total_loss": 0.0,
        }


def get_trade_summary() -> dict:
    """Return best/worst trade and average win/loss size."""
    placeholders = ",".join("?" * len(_ALL_SELL_ACTIONS))
    try:
        conn = _get_conn()
        best = conn.execute(
            f"SELECT pl_usdt, pair, coin_price FROM trades "
            f"WHERE action IN ({placeholders}) AND pl_usdt > 0 "
            f"ORDER BY pl_usdt DESC LIMIT 1",
            _ALL_SELL_ACTIONS,
        ).fetchone()
        worst = conn.execute(
            f"SELECT pl_usdt, pair, coin_price FROM trades "
            f"WHERE action IN ({placeholders}) AND pl_usdt < 0 "
            f"ORDER BY pl_usdt ASC LIMIT 1",
            _ALL_SELL_ACTIONS,
        ).fetchone()
        avg_win = conn.execute(
            f"SELECT AVG(pl_usdt) FROM trades "
            f"WHERE action IN ({placeholders}) AND pl_usdt > 0",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0] or 0.0
        avg_loss = conn.execute(
            f"SELECT AVG(pl_usdt) FROM trades "
            f"WHERE action IN ({placeholders}) AND pl_usdt < 0",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0] or 0.0
        avg_hold = conn.execute(
            f"SELECT AVG(hold_minutes) FROM trades "
            f"WHERE action IN ({placeholders}) AND hold_minutes > 0",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0] or 0.0
        conn.close()
        return {
            "best_pl": round(best[0], 2) if best else 0.0,
            "best_pair": best[1].replace("USDT", "") if best else "—",
            "worst_pl": round(worst[0], 2) if worst else 0.0,
            "worst_pair": worst[1].replace("USDT", "") if worst else "—",
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "avg_hold_min": round(avg_hold, 1),
        }
    except Exception as e:
        logger.error(f"Failed to get trade summary: {e}")
        return {
            "best_pl": 0.0, "best_pair": "—",
            "worst_pl": 0.0, "worst_pair": "—",
            "avg_win": 0.0, "avg_loss": 0.0, "avg_hold_min": 0.0,
        }


def get_exit_analytics() -> dict:
    """Return per-action exit breakdown for panel/analytics display.

    Returns:
        dict with:
          - total_exits: int
          - by_action: {action: {"count": int, "wins": int, "total_pl": float, "avg_pl": float}}
    """
    placeholders = ",".join("?" * len(_ALL_SELL_ACTIONS))
    try:
        conn = _get_conn()
        total = conn.execute(
            f"SELECT COUNT(*) FROM trades WHERE action IN ({placeholders})",
            _ALL_SELL_ACTIONS,
        ).fetchone()[0]

        by_action: dict[str, dict] = {}
        for action in _ALL_SELL_ACTIONS:
            row = conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN pl_usdt > 0 THEN 1 ELSE 0 END), "
                "COALESCE(SUM(pl_usdt), 0), "
                "COALESCE(AVG(pl_usdt), 0) "
                "FROM trades WHERE action = ?",
                (action,),
            ).fetchone()
            count = row[0] or 0
            if count == 0:
                continue
            by_action[action] = {
                "count": count,
                "wins": row[1] or 0,
                "total_pl": round(row[2], 4),
                "avg_pl": round(row[3], 4),
            }

        conn.close()
        return {"total_exits": total, "by_action": by_action}
    except Exception as e:
        logger.error(f"Failed to get exit analytics: {e}")
        return {"total_exits": 0, "by_action": {}}


def save_portfolio_snapshot(total_value: float, pl_usdt: float, pl_pct: float):
    """Save a periodic portfolio value snapshot.

    Args:
        total_value: Current total portfolio value in USDT
        pl_usdt: Total P/L in USDT
        pl_pct: Total P/L as percentage
    """
    try:
        conn = _get_conn()
        conn.execute(
            """INSERT INTO portfolio_snapshots (timestamp, total_value, pl_usdt, pl_pct)
               VALUES (?, ?, ?, ?)""",
            (datetime.now(timezone.utc).isoformat(), total_value, pl_usdt, pl_pct),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save portfolio snapshot: {e}")


def get_portfolio_snapshots(limit: int = 100) -> list[dict]:
    """Get recent portfolio snapshots for charting.

    Args:
        limit: Number of snapshots to return

    Returns:
        List of snapshot dicts, oldest first
    """
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        conn.close()
        return [dict(row) for row in reversed(rows)]
    except Exception as e:
        logger.error(f"Failed to get portfolio snapshots: {e}")
        return []
