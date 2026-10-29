"""Scanner Lambda: fetch market data, detect regime, decide whether to trade.

Runs at ~10:00 AM ET. Returns a scan result that the Step Functions
workflow passes to the executor Lambda if a trade signal is present.
"""

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from common.alpaca_clients import (
    create_options_data_client,
    create_stock_data_client,
    create_trading_client,
    is_market_open,
)
from common.config import (
    get_config,
    get_market_state_table,
    get_positions_table,
    get_sns_error_topic,
)
from common.dynamo_io import get_positions, put_market_state
from common.notifications import publish_error_alert
from market.expected_move import compute_expected_move
from market.regime import RegimeResult, classify_regime

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ET = ZoneInfo("US/Eastern")


def handler(event: dict, context) -> dict:
    """Lambda entry point.

    Args:
        event: Must include {"strategy": "IC_SPY"} or {"strategy": "CS_SPY"}.
        context: Lambda context (unused).

    Returns:
        Dict with scan results for Step Functions:
        - should_trade: bool
        - regime: str
        - confidence: float
        - expected_move: float
        - spy_price: float
        - vix_level: float
        - strategy: str
        - reason: str (why we're trading or not)
    """
    strategy_name = event.get("strategy", "IC_SPY")
    config = get_config(strategy_name)

    try:
        return _scan(config, strategy_name)
    except Exception as e:
        logger.exception(f"Scanner failed for {strategy_name}")
        _try_error_alert(strategy_name, str(e))
        return {
            "should_trade": False,
            "strategy": strategy_name,
            "reason": f"scanner_error: {e}",
        }


def _scan(config, strategy_name: str) -> dict:
    """Core scan logic, separated for testability."""
    trading_client = create_trading_client(config.secret_name)

    # Gate: market must be open
    market_open, clock = is_market_open(trading_client)
    if not market_open:
        return _no_trade(strategy_name, "market_closed")

    # Gate: check max positions
    positions_table = get_positions_table()
    open_positions = get_positions(positions_table, strategy_name)
    open_count = sum(1 for p in open_positions.values() if p.status == "open")
    if open_count >= config.max_positions:
        return _no_trade(strategy_name, f"max_positions_reached ({open_count})")

    # Fetch SPY 1-min bars from today's open
    now_et = datetime.now(ET)
    market_open_time = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

    stock_client = create_stock_data_client(config.secret_name)
    bars_request = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame.Minute,
        start=market_open_time,
        end=now_et,
    )
    bars_response = stock_client.get_stock_bars(bars_request)
    bars_df = bars_response.df

    if bars_df.empty or len(bars_df) < 5:
        return _no_trade(strategy_name, "insufficient_bar_data")

    # Get VIX level
    vix_level = _fetch_vix()

    # Classify regime
    regime_result = classify_regime(
        bars_df,
        vix_level=vix_level,
        vwap_cross_threshold=config.vwap_cross_threshold,
        candle_body_trend=config.candle_body_trend,
        candle_body_range=config.candle_body_range,
    )

    # Get expected move from ATM straddle
    spy_price = float(bars_df["close"].iloc[-1])
    expected_move = _fetch_expected_move(config.secret_name, spy_price)

    # Persist market state
    _save_market_state(now_et, regime_result, spy_price, vix_level, expected_move)

    # Gate: confidence threshold
    if regime_result.confidence < config.regime_confidence_min:
        return _no_trade(
            strategy_name,
            f"low_confidence ({regime_result.confidence:.2f} < {config.regime_confidence_min})",
            regime=regime_result,
            spy_price=spy_price,
            vix_level=vix_level,
            expected_move=expected_move,
        )

    # Gate: strategy/regime alignment
    should_trade = _check_regime_alignment(strategy_name, regime_result)
    if not should_trade:
        return _no_trade(
            strategy_name,
            f"regime_mismatch ({regime_result.regime.value} vs {strategy_name})",
            regime=regime_result,
            spy_price=spy_price,
            vix_level=vix_level,
            expected_move=expected_move,
        )

    return {
        "should_trade": True,
        "strategy": strategy_name,
        "regime": regime_result.regime.value,
        "confidence": round(regime_result.confidence, 4),
        "expected_move": round(expected_move, 4),
        "spy_price": round(spy_price, 2),
        "vix_level": round(vix_level, 2),
        "reason": f"trade_signal: {regime_result.regime.value} ({regime_result.confidence:.2f})",
    }


def _check_regime_alignment(strategy_name: str, regime: RegimeResult) -> bool:
    """Check if the detected regime aligns with the strategy.

    IC_SPY trades on range days. CS_SPY trades on trend days.
    """
    if strategy_name == "IC_SPY":
        return regime.regime == "range"
    elif strategy_name == "CS_SPY":
        return regime.regime in ("uptrend", "downtrend")
    return False


def _fetch_vix() -> float:
    """Fetch current VIX level via yfinance."""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1d")
        if hist.empty:
            logger.warning("VIX data empty, defaulting to 20.0")
            return 20.0
        return float(hist["Close"].iloc[-1])
    except Exception:
        logger.warning("Failed to fetch VIX, defaulting to 20.0")
        return 20.0


def _fetch_expected_move(secret_name: str, spy_price: float) -> float:
    """Fetch ATM straddle price and compute expected move.

    Uses Alpaca options data API to get 0DTE ATM options.
    Falls back to VIX-based estimate on failure.
    """
    try:
        options_client = create_options_data_client(secret_name)
        atm_strike = round(spy_price)

        # Get today's expiration
        today_str = datetime.now(ET).strftime("%y%m%d")
        call_symbol = f"SPY{today_str}C{atm_strike:08.0f}"
        put_symbol = f"SPY{today_str}P{atm_strike:08.0f}"

        # Fetch latest quotes
        from alpaca.data.requests import OptionLatestQuoteRequest

        quote_request = OptionLatestQuoteRequest(symbol_or_symbols=[call_symbol, put_symbol])
        quotes = options_client.get_option_latest_quote(quote_request)

        call_mid = (quotes[call_symbol].bid_price + quotes[call_symbol].ask_price) / 2
        put_mid = (quotes[put_symbol].bid_price + quotes[put_symbol].ask_price) / 2

        return compute_expected_move(call_mid, put_mid)
    except Exception as e:
        logger.warning(f"Failed to fetch ATM straddle, using VIX estimate: {e}")
        # Fallback: VIX-based daily expected move = price * VIX/100 / sqrt(252)
        return spy_price * 0.01  # ~1% as rough fallback


def _save_market_state(
    now_et: datetime, regime: RegimeResult, spy_price: float, vix: float, expected_move: float
) -> None:
    """Persist regime detection results to DynamoDB."""
    try:
        table = get_market_state_table()
        date_str = now_et.strftime("%Y-%m-%d")
        state = {
            "regime": regime.regime.value,
            "confidence": regime.confidence,
            "spy_price": spy_price,
            "vix_level": vix,
            "expected_move": expected_move,
            "vwap_crosses": regime.signals.vwap_crosses,
            "vwap_position": regime.signals.vwap_position,
            "or_breakout": regime.signals.or_breakout,
            "candle_body_ratio": regime.signals.candle_body_ratio,
        }
        put_market_state(table, date_str, state)
    except Exception:
        logger.warning("Failed to save market state, continuing")


def _no_trade(
    strategy_name: str,
    reason: str,
    regime: RegimeResult | None = None,
    spy_price: float = 0.0,
    vix_level: float = 0.0,
    expected_move: float = 0.0,
) -> dict:
    result = {
        "should_trade": False,
        "strategy": strategy_name,
        "reason": reason,
    }
    if regime is not None:
        result["regime"] = regime.regime.value
        result["confidence"] = round(regime.confidence, 4)
    if spy_price > 0:
        result["spy_price"] = round(spy_price, 2)
    if vix_level > 0:
        result["vix_level"] = round(vix_level, 2)
    if expected_move > 0:
        result["expected_move"] = round(expected_move, 4)
    return result


def _try_error_alert(strategy_name: str, error: str) -> None:
    try:
        topic = get_sns_error_topic()
        publish_error_alert(topic, f"scanner/{strategy_name}", error)
    except Exception:
        logger.warning("Failed to publish error alert")
