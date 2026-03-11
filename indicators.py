"""
Indicator calculations — EMA, ATR, VWAP, volume averages.
Operates on lists/arrays of Bar objects or price series.
"""

import math
from collections import deque
from typing import List, Optional

NaN = float("nan")


class EMA:
    """Incremental Exponential Moving Average."""

    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.value: float = NaN
        self._count = 0
        self._sum = 0.0

    def update(self, price: float) -> float:
        if math.isnan(price):
            return self.value
        self._count += 1
        if self._count <= self.period:
            self._sum += price
            if self._count == self.period:
                self.value = self._sum / self.period
        else:
            self.value = price * self.k + self.value * (1 - self.k)
        return self.value

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)


class WildersMA:
    """Wilder's smoothing (used for ATR). Equivalent to EMA with k=1/period."""

    def __init__(self, period: int):
        self.period = period
        self.value: float = NaN
        self._count = 0
        self._sum = 0.0

    def update(self, val: float) -> float:
        if math.isnan(val):
            return self.value
        self._count += 1
        if self._count <= self.period:
            self._sum += val
            if self._count == self.period:
                self.value = self._sum / self.period
        else:
            self.value = (self.value * (self.period - 1) + val) / self.period
        return self.value

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)


class SMA:
    """Simple Moving Average with a rolling window."""

    def __init__(self, period: int):
        self.period = period
        self._buf: deque = deque(maxlen=period)
        self.value: float = NaN

    def update(self, val: float) -> float:
        self._buf.append(val)
        if len(self._buf) == self.period:
            self.value = sum(self._buf) / self.period
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._buf) == self.period


class VWAPCalc:
    """Session VWAP — resets on new day."""

    def __init__(self):
        self.cum_vol = 0.0
        self.cum_pv = 0.0
        self.value: float = NaN

    def reset(self):
        self.cum_vol = 0.0
        self.cum_pv = 0.0
        self.value = NaN

    def update(self, typical_price: float, volume: float) -> float:
        self.cum_vol += volume
        self.cum_pv += typical_price * volume
        if self.cum_vol > 0:
            self.value = self.cum_pv / self.cum_vol
        return self.value

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)


class TrueRangeCalc:
    """True Range tracker — needs prev close."""

    def __init__(self):
        self.prev_close: float = NaN

    def update(self, high: float, low: float, close: float) -> float:
        if math.isnan(self.prev_close):
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        self.prev_close = close
        return tr

    def reset(self):
        self.prev_close = NaN


class HighestLowest:
    """Rolling Highest/Lowest over N bars."""

    def __init__(self, period: int):
        self.period = period
        self._highs: deque = deque(maxlen=period)
        self._lows: deque = deque(maxlen=period)

    def update(self, high: float, low: float):
        self._highs.append(high)
        self._lows.append(low)

    @property
    def highest(self) -> float:
        return max(self._highs) if self._highs else NaN

    @property
    def lowest(self) -> float:
        return min(self._lows) if self._lows else NaN

    @property
    def ready(self) -> bool:
        return len(self._highs) == self.period


class RSI:
    """Wilder-style incremental RSI.

    Uses the classic Wilder smoothing (exponential with k=1/period) for
    average gain and average loss.  The ``ready`` property becomes True
    after ``period`` bars of close-to-close changes have been consumed.

    Maintains a rolling history of the most recent ``history_len`` RSI
    values so callers can inspect look-back windows without re-computing.
    """

    def __init__(self, period: int = 7, history_len: int = 16):
        self.period = period
        self._avg_gain: float = NaN
        self._avg_loss: float = NaN
        self._prev_close: float = NaN
        self._count = 0
        self._gain_sum = 0.0
        self._loss_sum = 0.0
        self.value: float = NaN
        self.prev_value: float = NaN  # RSI of the *prior* bar (for cross detection)
        self.history: deque = deque(maxlen=history_len)

    def update(self, close: float) -> float:
        if math.isnan(close):
            return self.value
        if math.isnan(self._prev_close):
            self._prev_close = close
            return self.value

        change = close - self._prev_close
        self._prev_close = close
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        self._count += 1

        if self._count <= self.period:
            self._gain_sum += gain
            self._loss_sum += loss
            if self._count == self.period:
                self._avg_gain = self._gain_sum / self.period
                self._avg_loss = self._loss_sum / self.period
                self.prev_value = self.value
                if self._avg_loss == 0.0:
                    self.value = 100.0
                else:
                    rs = self._avg_gain / self._avg_loss
                    self.value = 100.0 - (100.0 / (1.0 + rs))
                self.history.append(self.value)
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period
            self.prev_value = self.value
            if self._avg_loss == 0.0:
                self.value = 100.0
            else:
                rs = self._avg_gain / self._avg_loss
                self.value = 100.0 - (100.0 / (1.0 + rs))
            self.history.append(self.value)
        return self.value

    @property
    def ready(self) -> bool:
        return not math.isnan(self.value)


class ATRPair:
    """Manages both daily ATR and intraday ATR (different aggregations).

    Daily ATR: fed from daily OHLC bars (computed externally or from session aggregation).
    Intraday ATR: fed from N-minute bars (e.g., 5-min).
    """

    def __init__(self, period: int, use_completed: bool = True):
        self.period = period
        self.use_completed = use_completed
        # Daily
        self.daily_tr = TrueRangeCalc()
        self.daily_atr = WildersMA(period)
        # Intraday
        self.intra_tr = TrueRangeCalc()
        self.intra_atr = WildersMA(period)
        self._intra_prev_value: float = NaN

    def update_daily(self, high: float, low: float, close: float) -> float:
        tr = self.daily_tr.update(high, low, close)
        return self.daily_atr.update(tr)

    def update_intraday(self, high: float, low: float, close: float) -> float:
        tr = self.intra_tr.update(high, low, close)
        current = self.intra_atr.update(tr)
        if self.use_completed:
            result = self._intra_prev_value
            self._intra_prev_value = current
            return result
        return current

    @property
    def d_atr(self) -> float:
        return self.daily_atr.value

    @property
    def i_atr(self) -> float:
        if self.use_completed:
            return self._intra_prev_value
        return self.intra_atr.value

    @property
    def i_atr_ready(self) -> bool:
        val = self.i_atr
        return not math.isnan(val) and val > 0
