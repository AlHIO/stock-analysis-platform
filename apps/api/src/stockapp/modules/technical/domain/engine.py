"""Single, deterministic implementation of stockapp indicator algorithms v1.

SMA and volume SMA use an arithmetic window. EMA is SMA-seeded with alpha 2/(n+1).
RSI seeds n gains/losses after n+1 closes, then applies Wilder's smoothing.
MACD subtracts separately SMA-seeded fast/slow EMAs, SMA-seeds the signal EMA
from the first signal_period non-null MACD values, and uses MACD minus signal.

Explicit missing selected input resets the whole indicator segment. Absent dates
are not filled; weekends/holidays do not count as observations. Internal arithmetic
uses a private 50-digit ROUND_HALF_EVEN Context. Only returned values are rounded
to 12 decimal places; returned values never feed the recurrence.
"""

from collections import deque
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, localcontext

from .models import (
    ALGORITHM_VERSION, DECIMAL_PRECISION, OUTPUT_SCALE, IndicatorPoint, IndicatorResult,
    IndicatorSample, IndicatorValidationError, PriceBasis, normalize_parameters,
)

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HUNDRED = Decimal(100)
_QUANTUM = Decimal(f"1e-{OUTPUT_SCALE}")


class _MeanWindow:
    def __init__(self, period: int):
        self.period = period
        self.values: deque[Decimal] = deque()
        self.total = _ZERO

    def push(self, value: Decimal | None) -> Decimal | None:
        if value is None:
            self.values.clear()
            self.total = _ZERO
            return None
        self.values.append(value)
        self.total += value
        if len(self.values) > self.period:
            self.total -= self.values.popleft()
        return self.total / self.period if len(self.values) == self.period else None


class _Ema:
    def __init__(self, period: int):
        self.seed = _MeanWindow(period)
        self.alpha = Decimal(2) / (period + 1)
        self.previous: Decimal | None = None

    def push(self, value: Decimal | None) -> Decimal | None:
        if value is None:
            self.seed.push(None)
            self.previous = None
            return None
        if self.previous is None:
            self.previous = self.seed.push(value)
        else:
            self.previous += (value - self.previous) * self.alpha
        return self.previous


class _Rsi:
    def __init__(self, period: int):
        self.period = period
        self.reset()

    def reset(self):
        self.previous: Decimal | None = None
        self.count = 0
        self.gain_sum = self.loss_sum = _ZERO
        self.average_gain: Decimal | None = None
        self.average_loss: Decimal | None = None

    def push(self, value: Decimal | None) -> Decimal | None:
        if value is None:
            self.reset()
            return None
        previous, self.previous = self.previous, value
        if previous is None:
            return None
        change = value - previous
        gain, loss = max(change, _ZERO), max(-change, _ZERO)
        if self.average_gain is None:
            self.gain_sum += gain
            self.loss_sum += loss
            self.count += 1
            if self.count < self.period:
                return None
            self.average_gain = self.gain_sum / self.period
            self.average_loss = self.loss_sum / self.period
        else:
            self.average_gain = (self.average_gain * (self.period - 1) + gain) / self.period
            self.average_loss = (self.average_loss * (self.period - 1) + loss) / self.period
        if self.average_loss == 0:
            return _HUNDRED if self.average_gain > 0 else Decimal(50)
        if self.average_gain == 0:
            return _ZERO
        return _HUNDRED - _HUNDRED / (_ONE + self.average_gain / self.average_loss)


def _validate_samples(samples: Sequence[IndicatorSample]) -> None:
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes)):
        raise IndicatorValidationError("invalid_samples", "samples must be a sequence of IndicatorSample values.")
    previous: date | None = None
    for sample in samples:
        if not isinstance(sample, IndicatorSample) or type(sample.trade_date) is not date:
            raise IndicatorValidationError("invalid_samples", "Samples require market-local dates, not datetimes.")
        if previous is not None and sample.trade_date <= previous:
            raise IndicatorValidationError("invalid_samples", "Sample dates must be unique and strictly increasing.")
        previous = sample.trade_date
        if sample.close is not None and (
            not isinstance(sample.close, Decimal) or not sample.close.is_finite() or sample.close <= 0
        ):
            raise IndicatorValidationError("invalid_samples", "Prices must be positive finite Decimal values or null.")
        if sample.volume_shares is not None and (type(sample.volume_shares) is not int or sample.volume_shares < 0):
            raise IndicatorValidationError("invalid_samples", "Volumes must be nonnegative integer shares or null.")
        if type(sample.is_provisional) is not bool:
            raise IndicatorValidationError("invalid_samples", "is_provisional must be a boolean.")


def _rounded(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    rounded = value.quantize(_QUANTUM)
    # Avoid negative-zero artefacts in display and canonical serialization.
    return abs(rounded) if rounded == 0 else rounded


class DecimalIndicatorEngine:
    def compute(
        self,
        samples: Sequence[IndicatorSample],
        indicator: str,
        parameters: Mapping[str, int] | None = None,
        *,
        price_basis: PriceBasis = "raw",
    ) -> IndicatorResult:
        normalized = normalize_parameters(indicator, parameters)
        if price_basis not in ("raw", "adjusted"):
            raise IndicatorValidationError("invalid_price_basis", "price_basis must be raw or adjusted.")
        _validate_samples(samples)
        required = normalized.get("period", 0)
        if indicator == "rsi":
            required += 1
        elif indicator == "macd":
            required = normalized["slow_period"] + normalized["signal_period"] - 1
        field = "volume_shares" if indicator == "volume_sma" else "close"
        points: list[IndicatorPoint] = []
        # Do not inherit caller precision, rounding mode, traps or exponent limits.
        context = Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)
        try:
            with localcontext(context):
                if indicator == "macd":
                    fast = _Ema(normalized["fast_period"])
                    slow = _Ema(normalized["slow_period"])
                    signal = _Ema(normalized["signal_period"])
                elif indicator in ("sma", "volume_sma"):
                    state = _MeanWindow(normalized["period"])
                elif indicator == "ema":
                    state = _Ema(normalized["period"])
                else:
                    state = _Rsi(normalized["period"])
                for sample in samples:
                    value = (Decimal(sample.volume_shares) if sample.volume_shares is not None else None) if field == "volume_shares" else sample.close
                    if indicator == "macd":
                        fast_value, slow_value = fast.push(value), slow.push(value)
                        macd = fast_value - slow_value if fast_value is not None and slow_value is not None else None
                        signal_value = signal.push(macd)
                        histogram = macd - signal_value if macd is not None and signal_value is not None else None
                        values = {"macd": macd, "signal": signal_value, "histogram": histogram}
                    else:
                        values = {indicator: state.push(value)}
                    status = "missing_input" if value is None else "ready" if all(item is not None for item in values.values()) else "warmup"
                    points.append(IndicatorPoint(
                        trade_date=sample.trade_date,
                        values={key: _rounded(item) for key, item in values.items()},
                        status=status,
                        is_provisional=sample.is_provisional,
                    ))
        except DecimalException as exc:
            raise IndicatorValidationError("numeric_precision_exceeded", "Inputs exceed the indicator precision policy.") from exc
        return IndicatorResult(
            indicator=indicator, algorithm_version=ALGORITHM_VERSION, parameters=normalized,
            price_basis=price_basis, field=field, required_samples=required, points=tuple(points),
        )
