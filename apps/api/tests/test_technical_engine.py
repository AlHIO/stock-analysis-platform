"""Independent fixed-value examples; all prices are synthetic test inputs only.

Rational calculations in comments show the expectations, rather than comparing
the engine to a second copy of its implementation or to binary-float outputs.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Context, Decimal, Inexact, ROUND_DOWN, localcontext
from types import MappingProxyType

import pytest

from stockapp.modules.technical.domain import (
    ALGORITHM_VERSION,
    DecimalIndicatorEngine,
    IndicatorSample,
    IndicatorValidationError,
    normalize_parameters,
)

ENGINE = DecimalIndicatorEngine()
START = date(2026, 1, 1)


def samples(closes, *, volumes=None, dates=None, provisional=False):
    volumes = [10] * len(closes) if volumes is None else volumes
    dates = [START + timedelta(days=index) for index in range(len(closes))] if dates is None else dates
    return [IndicatorSample(day, None if close is None else Decimal(str(close)), volume, provisional)
            for day, close, volume in zip(dates, closes, volumes, strict=True)]


def values(result, key=None):
    key = result.indicator if key is None else key
    return [point.values[key] for point in result.points]


def decimals(*items):
    return [None if item is None else Decimal(item) for item in items]


def test_sma_fixed_values_and_exact_window():
    result = ENGINE.compute(samples([2, 4, 8, 6, 10]), "sma", {"period": 3})
    assert values(result) == decimals(None, None, "4.666666666667", "6", "8")
    assert [point.status for point in result.points] == ["warmup", "warmup", "ready", "ready", "ready"]
    assert result.required_samples == 3


def test_ema_sma_seed_and_fixed_recurrence():
    result = ENGINE.compute(samples([1, 2, 3, 8, 5, 10]), "ema", {"period": 3})
    # Seed=(1+2+3)/3=2; alpha=1/2; next values 5,5,7.5.
    assert values(result) == decimals(None, None, "2", "5", "5", "7.5")
    assert result.required_samples == 3


def test_ema_does_not_feed_output_rounding_back_into_recurrence():
    result = ENGINE.compute(samples([19, 2, 14, 16]), "ema", {"period": 3})
    # Seed=35/3 and next=(35/3+16)/2=83/6; a rounded seed yields ...334 instead.
    assert values(result) == decimals(None, None, "11.666666666667", "13.833333333333")


def test_ema_repeating_alpha_fixed_values():
    result = ENGINE.compute(samples([1, 2, 4, 8]), "ema", {"period": 2})
    assert values(result) == decimals(None, "1.5", "3.166666666667", "6.388888888889")


def test_rsi_wilder_seed_and_smoothing_fixed_values():
    result = ENGINE.compute(samples([10, 11, 10, 12, 11, 14, 13]), "rsi", {"period": 3})
    # First changes +1,-1,+2: gain=1, loss=1/3 =>75.
    # Then gain/loss ratios 6/5,39/10,78/47 =>600/11,3900/49,312/5.
    assert values(result) == decimals(None, None, None, "75", "54.545454545455", "79.591836734694", "62.4")
    assert result.required_samples == 4


@pytest.mark.parametrize("closes,expected", [([10, 11, 12, 13], "100"), ([13, 12, 11, 10], "0"), ([10, 10, 10, 10], "50")])
def test_rsi_zero_loss_zero_gain_and_flat_convention(closes, expected):
    result = ENGINE.compute(samples(closes), "rsi", {"period": 3})
    assert values(result) == decimals(None, None, None, expected)


def test_rsi_period_one_uses_last_change_and_flat_is_neutral():
    result = ENGINE.compute(samples([10, 10, 11, 11, 10]), "rsi", {"period": 1})
    assert values(result) == decimals(None, "50", "100", "50", "0")


def test_macd_fast_slow_signal_and_histogram_fixed_values():
    result = ENGINE.compute(samples([1, 2, 3, 6, 5, 10]), "macd", {"fast_period": 2, "slow_period": 3, "signal_period": 2})
    # SMA seeds: fast EMA2=1.5 then2.5; slow EMA3=2 =>firstMACD=.5.
    # Next MACD=5/6; signal seed=(1/2+5/6)/2=2/3; histogram=1/6.
    assert values(result, "macd") == decimals(None, None, ".5", ".833333333333", ".444444444444", "1.064814814815")
    assert values(result, "signal") == decimals(None, None, None, ".666666666667", ".518518518519", ".882716049383")
    assert values(result, "histogram") == decimals(None, None, None, ".166666666667", "-.074074074074", ".182098765432")
    assert result.required_samples == 4
    assert result.points[2].status == "warmup" and result.points[2].values["macd"] is not None
    assert result.points[3].status == "ready"


def test_macd_signal_period_one_and_fast_period_one():
    result = ENGINE.compute(samples([1, 2, 3]), "macd", {"fast_period": 1, "slow_period": 2, "signal_period": 1})
    assert values(result, "macd") == decimals(None, ".5", ".5")
    assert values(result, "signal") == values(result, "macd")
    assert values(result, "histogram") == decimals(None, "0", "0")
    assert result.required_samples == 2


def test_volume_sma_counts_zero_volume_and_never_changes_share_units():
    result = ENGINE.compute(samples([1, 2, 3, 4], volumes=[0, 1000, 2000, 0]), "volume_sma", {"period": 2})
    assert values(result) == decimals(None, "500", "1500", "1000")
    assert result.field == "volume_shares"


def test_volume_average_can_be_fractional_shares_without_truncation():
    result = ENGINE.compute(samples([1, 1, 1], volumes=[0, 0, 1]), "volume_sma", {"period": 3})
    assert values(result) == decimals(None, None, ".333333333333")


@pytest.mark.parametrize("indicator", ["sma", "ema", "volume_sma"])
def test_period_one_is_the_observed_input(indicator):
    result = ENGINE.compute(samples([1, 4, 2], volumes=[0, 10, 20]), indicator, {"period": 1})
    expected = decimals("0", "10", "20") if indicator == "volume_sma" else decimals("1", "4", "2")
    assert values(result) == expected
    assert all(point.status == "ready" for point in result.points)


@pytest.mark.parametrize("indicator", ["sma", "ema", "rsi", "macd", "volume_sma"])
def test_empty_data_has_versioned_metadata_and_no_points(indicator):
    result = ENGINE.compute([], indicator)
    assert result.points == ()
    assert result.algorithm_version == ALGORITHM_VERSION == "1.0.0"
    assert result.parameters == normalize_parameters(indicator)
    assert result.price_basis == "raw"


@pytest.mark.parametrize("indicator,required", [("sma", 20), ("ema", 20), ("rsi", 15), ("macd", 34), ("volume_sma", 20)])
def test_defaults_warm_up_at_exact_required_sample(indicator, required):
    result = ENGINE.compute(samples(list(range(1, required + 1))), indicator)
    assert result.required_samples == required
    assert all(point.status == "warmup" for point in result.points[:-1])
    assert result.points[-1].status == "ready"
    if indicator != "macd":
        assert values(result)[:-1] == [None] * (required - 1)


@pytest.mark.parametrize("indicator,parameters,field", [
    ("sma", {"period": 2}, "close"), ("ema", {"period": 2}, "close"),
    ("rsi", {"period": 1}, "close"),
    ("macd", {"fast_period": 1, "slow_period": 2, "signal_period": 1}, "close"),
    ("volume_sma", {"period": 2}, "volume_shares"),
])
def test_explicit_missing_input_resets_and_requires_a_new_seed(indicator, parameters, field):
    closes, volumes = [10, 11, 12, 13, 20, 21], [10, 11, 12, 13, 20, 21]
    (closes if field == "close" else volumes)[3] = None
    result = ENGINE.compute(samples(closes, volumes=volumes), indicator, parameters)
    assert result.points[2].status == "ready"
    assert result.points[3].status == "missing_input"
    assert all(value is None for value in result.points[3].values.values())
    assert result.points[4].status == "warmup"
    assert result.points[5].status == "ready"
    if indicator in ("sma", "ema", "volume_sma"):
        assert result.points[5].values[indicator] == Decimal("20.5")


def test_macd_missing_input_restarts_signal_seed_as_well():
    result = ENGINE.compute(samples([1, 2, 3, 6, None, 1, 2, 3, 6]), "macd", {"fast_period": 2, "slow_period": 3, "signal_period": 2})
    assert [point.status for point in result.points] == ["warmup", "warmup", "warmup", "ready", "missing_input", "warmup", "warmup", "warmup", "ready"]
    assert result.points[-1].values == result.points[3].values


def test_an_unused_missing_field_does_not_reset_another_indicator():
    result = ENGINE.compute(samples([1, 2, 3], volumes=[None, None, None]), "sma", {"period": 2})
    assert values(result) == decimals(None, "1.5", "2.5")
    result = ENGINE.compute(samples([None, None, None], volumes=[1, 2, 3]), "volume_sma", {"period": 2})
    assert values(result) == decimals(None, "1.5", "2.5")


def test_nontrading_days_do_not_create_observations_or_break_warmup():
    dates = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 9)]
    result = ENGINE.compute(samples([2, 4, 8], dates=dates), "sma", {"period": 2})
    assert [point.trade_date for point in result.points] == dates
    assert values(result) == decimals(None, "3", "6")


@pytest.mark.parametrize("dates", [
    [date(2026, 1, 2), date(2026, 1, 9), date(2026, 1, 16)],
    [date(2026, 1, 30), date(2026, 2, 27), date(2026, 3, 31)],
    [date(2025, 12, 31), date(2026, 1, 2), date(2026, 2, 2)],
])
def test_reusable_engine_counts_supplied_daily_weekly_or_monthly_observations(dates):
    result = ENGINE.compute(samples([4, 8, 12], dates=dates), "ema", {"period": 2})
    assert values(result) == decimals(None, "6", "10")
    assert [point.trade_date for point in result.points] == dates


def test_raw_and_adjusted_are_explicit_labels_not_automatic_adjustments():
    data = samples([10, 20, 30], provisional=True)
    raw = ENGINE.compute(data, "sma", {"period": 2}, price_basis="raw")
    adjusted = ENGINE.compute(data, "sma", {"period": 2}, price_basis="adjusted")
    assert raw.price_basis == "raw" and adjusted.price_basis == "adjusted"
    assert values(raw) == values(adjusted) == decimals(None, "15", "25")
    assert all(point.is_provisional for point in raw.points)


def test_half_even_output_policy_at_twelve_decimal_places():
    lower = ENGINE.compute(samples(["1.000000000001", "1"]), "sma", {"period": 2})
    upper = ENGINE.compute(samples(["1.000000000003", "1"]), "sma", {"period": 2})
    assert values(lower)[1] == Decimal("1.000000000000")
    assert values(upper)[1] == Decimal("1.000000000002")


def test_large_volume_and_decimal_prices_do_not_go_through_binary_float():
    prices = ["1234567890123456.12345678", "1234567890123456.12345680"]
    result = ENGINE.compute(samples(prices), "sma", {"period": 2})
    assert values(result)[1] == Decimal("1234567890123456.123456790000")
    volumes = [2**63 - 1, 2**63 - 2]
    volume = ENGINE.compute(samples([1, 2], volumes=volumes), "volume_sma", {"period": 2})
    assert values(volume)[1] == Decimal("9223372036854775806.5")


@pytest.mark.parametrize("indicator,parameters", [("sma", {"period": 3}), ("ema", {"period": 3}), ("rsi", {"period": 3}), ("macd", {"fast_period": 2, "slow_period": 3, "signal_period": 2}), ("volume_sma", {"period": 3})])
def test_caller_decimal_context_cannot_change_results_or_raise_inexact(indicator, parameters):
    data = samples([10, 11, 10, 12, 11, 14, 13], volumes=[0, 1, 2, 3, 4, 5, 6])
    expected = ENGINE.compute(data, indicator, parameters)
    with localcontext(Context(prec=2, rounding=ROUND_DOWN)) as context:
        context.traps[Inexact] = True
        context.Emax = 2
        actual = ENGINE.compute(data, indicator, parameters)
        assert context.prec == 2 and context.traps[Inexact]
    assert actual == expected


def test_parameters_are_expanded_and_not_shared_with_caller():
    parameters = {"fast_period": 3}
    result = ENGINE.compute([], "macd", MappingProxyType(parameters))
    parameters["fast_period"] = 4
    assert result.parameters == {"fast_period": 3, "slow_period": 26, "signal_period": 9}
    assert normalize_parameters("sma") == {"period": 20}
    assert normalize_parameters("sma", {"period": 250}) == {"period": 250}


@pytest.mark.parametrize("indicator,parameters", [
    ("SMA", None), ("unknown", {}), (None, None),
    ("sma", {"period": 0}), ("sma", {"period": 251}), ("ema", {"period": -1}),
    ("rsi", {"period": True}), ("sma", {"period": 2.0}), ("sma", {"period": "2"}),
    ("sma", {"period": None}), ("sma", {"extra": 2}), ("macd", {"period": 2}),
    ("macd", {"fast_period": 26}), ("macd", {"fast_period": 30, "slow_period": 2}),
    ("sma", []), ("sma", "{period:2}"),
])
def test_invalid_indicator_and_parameters_are_rejected(indicator, parameters):
    with pytest.raises(IndicatorValidationError) as error:
        ENGINE.compute([], indicator, parameters)
    assert error.value.code in ("invalid_indicator", "invalid_parameters")


@pytest.mark.parametrize("bad", [0, -1, 1.1, True, "1", Decimal(0), Decimal(-1), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_invalid_prices_are_rejected_even_when_volume_indicator_is_selected(bad):
    with pytest.raises(IndicatorValidationError, match="Prices"):
        ENGINE.compute([IndicatorSample(START, bad, 10)], "volume_sma", {"period": 1})


@pytest.mark.parametrize("bad", [-1, 1.5, True, Decimal(10), "10"])
def test_invalid_volume_is_rejected_even_when_price_indicator_is_selected(bad):
    with pytest.raises(IndicatorValidationError, match="Volumes"):
        ENGINE.compute([IndicatorSample(START, Decimal(10), bad)], "sma", {"period": 1})


@pytest.mark.parametrize("bad", ["unadjusted", "RAW", None, True])
def test_invalid_price_basis_is_rejected(bad):
    with pytest.raises(IndicatorValidationError) as error:
        ENGINE.compute([], "sma", price_basis=bad)
    assert error.value.code == "invalid_price_basis"


@pytest.mark.parametrize("data", [
    None, "bad", [object()],
    [IndicatorSample(datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal(1), 10)],
    [IndicatorSample(START, Decimal(1), 10), IndicatorSample(START, Decimal(2), 20)],
    [IndicatorSample(START + timedelta(days=1), Decimal(1), 10), IndicatorSample(START, Decimal(2), 20)],
    [IndicatorSample(START, Decimal(1), 10, is_provisional=1)],
])
def test_invalid_dates_duplicate_order_and_samples_are_rejected(data):
    with pytest.raises(IndicatorValidationError) as error:
        ENGINE.compute(data, "sma")
    assert error.value.code == "invalid_samples"


def test_nonrepresentable_numeric_output_is_a_controlled_validation_failure():
    with pytest.raises(IndicatorValidationError) as error:
        ENGINE.compute(samples(["1e100"]), "sma", {"period": 1})
    assert error.value.code == "numeric_precision_exceeded"
