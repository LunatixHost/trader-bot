"""Chart — candlestick chart generation for Discord.

Generates a live candlestick chart with Bollinger Bands, MACD, RSI subplots,
volume bars, and buy/sell trade markers. Styled for dark backgrounds.
"""

import io
import logging
import pandas as pd
import matplotlib
# Force non-interactive Agg backend BEFORE importing pyplot or mplfinance.
# Without this, matplotlib defaults to TkAgg on Windows, which creates Tkinter
# objects that get garbage-collected from the asyncio executor thread instead of
# the main thread, causing "RuntimeError: main thread is not in main loop" and
# the fatal "Tcl_AsyncDelete: async handler deleted by the wrong thread" crash.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
from datetime import datetime, timezone

logger = logging.getLogger("trading_bot.discord.chart")

# Try pandas-ta for chart indicators too
try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False

# ─── Colors ───────────────────────────────────────────────────────────
BG_COLOR = "#0d1117"
PANEL_BG = "#161b22"
TEXT_COLOR = "#e6edf3"
GRID_COLOR = "#21262d"
GREEN = "#39d353"
RED = "#f85149"
BLUE = "#58a6ff"
YELLOW = "#e3b341"
ORANGE = "#f0883e"
MUTED = "#8b949e"


def _manual_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI fallback."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100.0 - (100.0 / (1.0 + rs))


def _manual_macd(close: pd.Series):
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line


def _manual_bbands(close: pd.Series):
    middle = close.rolling(20).mean()
    std = close.rolling(20).std()
    return middle + 2 * std, middle - 2 * std


async def generate_chart(binance_client, pair: str, trade_log: list[dict]) -> io.BytesIO | None:
    """Generate a candlestick chart image for a trading pair.

    Args:
        binance_client: BinanceClient instance for fetching klines
        pair: Trading pair e.g. "ETHUSDT"
        trade_log: List of trade dicts from logger

    Returns:
        BytesIO PNG buffer, or None on error
    """
    try:
        # Try 15m candles first, fall back to 1h if testnet has limited data
        klines = await binance_client.get_klines(pair, interval="15m", limit=100)
        interval_label = "15m"
        if not klines or len(klines) < 10:
            logger.info(f"15m klines too few for {pair} ({len(klines) if klines else 0}), trying 1h")
            klines = await binance_client.get_klines(pair, interval="1h", limit=100)
            interval_label = "1h"
        if not klines or len(klines) < 5:
            logger.warning(f"Not enough kline data for chart: {pair} ({len(klines) if klines else 0} candles)")
            return None
        logger.info(f"Chart {pair}: {len(klines)} candles ({interval_label})")

        # Build DataFrame
        df = pd.DataFrame(klines, columns=[
            "open_time", "Open", "High", "Low", "Close", "Volume",
            "close_time", "quote_volume", "trades", "taker_buy_base",
            "taker_buy_quote", "ignore",
        ])

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = df[col].astype(float)

        # Create datetime index
        df["Date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df.set_index("Date", inplace=True)
        df = df[["Open", "High", "Low", "Close", "Volume"]]

        # Calculate indicators
        if HAS_PANDAS_TA:
            bb = df.ta.bbands(length=20, std=2)
            macd_data = df.ta.macd(fast=12, slow=26, signal=9)
            rsi_data = df.ta.rsi(length=14)

            bb_upper = None
            bb_lower = None
            if bb is not None and not bb.empty:
                for col in bb.columns:
                    if "bbu" in col.lower():
                        bb_upper = bb[col]
                    elif "bbl" in col.lower():
                        bb_lower = bb[col]

            if macd_data is not None and not macd_data.empty:
                macd_line = macd_data.iloc[:, 0]
                macd_signal = macd_data.iloc[:, 1]
            else:
                macd_line, macd_signal = _manual_macd(df["Close"])

            if rsi_data is None or rsi_data.empty:
                rsi_data = _manual_rsi(df["Close"])
        else:
            bb_upper, bb_lower = _manual_bbands(df["Close"])
            macd_line, macd_signal = _manual_macd(df["Close"])
            rsi_data = _manual_rsi(df["Close"])

        # RSI reference lines at 35 (oversold) and 65 (overbought)
        rsi_35 = pd.Series(35.0, index=df.index)
        rsi_65 = pd.Series(65.0, index=df.index)

        # MACD zero line
        macd_zero = pd.Series(0.0, index=df.index)

        # Buy/sell markers from trade history
        buy_markers = [float("nan")] * len(df)
        sell_markers = [float("nan")] * len(df)

        for trade in trade_log:
            ts = trade.get("timestamp", "")
            if not ts:
                continue
            try:
                trade_time = pd.Timestamp(ts, tz="UTC")
                idx = df.index.searchsorted(trade_time)
                if 0 <= idx < len(df):
                    price = trade.get("coin_price", 0)
                    if trade.get("action") == "BUY":
                        buy_markers[idx] = price
                    elif trade.get("action") in ("SELL", "STOP_LOSS"):
                        sell_markers[idx] = price
            except Exception:
                continue

        # Build addplots
        apds = []

        # Bollinger Bands on main panel
        if bb_upper is not None:
            apds.append(mpf.make_addplot(bb_upper, color=BLUE, alpha=0.5, linestyle="--"))
        if bb_lower is not None:
            apds.append(mpf.make_addplot(bb_lower, color=BLUE, alpha=0.5, linestyle="--"))

        # Buy/sell markers
        buy_series = pd.Series(buy_markers, index=df.index)
        sell_series = pd.Series(sell_markers, index=df.index)

        if buy_series.notna().any():
            apds.append(mpf.make_addplot(
                buy_series, type="scatter", marker="^",
                markersize=100, color=GREEN,
            ))
        if sell_series.notna().any():
            apds.append(mpf.make_addplot(
                sell_series, type="scatter", marker="v",
                markersize=100, color=RED,
            ))

        # MACD subplot (panel 2)
        apds.append(mpf.make_addplot(macd_line, panel=2, color=BLUE, ylabel="MACD"))
        apds.append(mpf.make_addplot(macd_signal, panel=2, color=RED))
        apds.append(mpf.make_addplot(macd_zero, panel=2, color=MUTED, alpha=0.5, linestyle="--"))

        # RSI subplot (panel 3) with threshold lines
        apds.append(mpf.make_addplot(rsi_data, panel=3, color=YELLOW, ylabel="RSI", ylim=(0, 100)))
        apds.append(mpf.make_addplot(rsi_35, panel=3, color=GREEN, alpha=0.4, linestyle="--"))
        apds.append(mpf.make_addplot(rsi_65, panel=3, color=RED, alpha=0.4, linestyle="--"))

        # ── Style: all text white on dark background ──
        mc = mpf.make_marketcolors(
            up=GREEN, down=RED,
            edge="inherit", wick="inherit",
            volume={"up": GREEN, "down": RED},
        )
        style = mpf.make_mpf_style(
            marketcolors=mc,
            facecolor=BG_COLOR,
            edgecolor="#30363d",
            figcolor=BG_COLOR,
            gridcolor=GRID_COLOR,
            gridstyle="--",
            y_on_right=True,
            rc={
                "font.size": 9,
                "text.color": TEXT_COLOR,
                "axes.labelcolor": TEXT_COLOR,
                "xtick.color": TEXT_COLOR,
                "ytick.color": TEXT_COLOR,
                "axes.edgecolor": "#30363d",
            },
        )

        base = pair.replace("USDT", "")
        fig, axes = mpf.plot(
            df, type="candle", style=style,
            addplot=apds, volume=True,
            title=f"\n{base}/USDT  —  Last {len(df)} Candles ({interval_label})",
            returnfig=True, figsize=(14, 10),
            panel_ratios=(4, 1, 1, 1),
        )

        # Force title color (mplfinance sometimes ignores rc for the title)
        if axes:
            axes[0].set_title(
                f"{base}/USDT  —  Last {len(df)} Candles ({interval_label})",
                color=TEXT_COLOR, fontsize=13, fontweight="bold", pad=15,
            )

        # Style all axes: tick labels, panel backgrounds
        for ax in fig.axes:
            ax.set_facecolor(PANEL_BG)
            ax.tick_params(colors=TEXT_COLOR, labelsize=8)
            ax.yaxis.label.set_color(TEXT_COLOR)
            ax.xaxis.label.set_color(TEXT_COLOR)
            for spine in ax.spines.values():
                spine.set_color("#30363d")

        buf = io.BytesIO()
        fig.savefig(
            buf, format="png", dpi=120,
            bbox_inches="tight", facecolor=BG_COLOR,
        )
        buf.seek(0)
        plt.close(fig)

        return buf

    except Exception as e:
        logger.error(f"Chart generation failed for {pair}: {e}")
        return None
