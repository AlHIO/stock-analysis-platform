"""Reusable indicator query use case; formulas live exclusively in the domain."""
from dataclasses import asdict

from stockapp.modules.market_data.domain.queries import InvalidQuery, InvalidStoredData
from stockapp.modules.technical.domain.history import IndicatorHistorySource
from stockapp.modules.technical.domain.models import DECIMAL_PRECISION, OUTPUT_SCALE, IndicatorEngine, IndicatorValidationError, normalize_parameters


class TechnicalIndicatorService:
    def __init__(self, source: IndicatorHistorySource, engine: IndicatorEngine):
        self.source, self.engine = source, engine

    def calculate(self, instrument_id, *, timeframe="1d", indicator="sma", parameters=None,
                  start=None, end=None, price_basis="raw"):
        if price_basis != "raw":
            raise InvalidQuery("adjusted_price_unavailable" if price_basis == "adjusted" else "invalid_price_basis",
                               "Only raw unadjusted stored prices are available")
        try:
            normalized = normalize_parameters(indicator, parameters)
        except IndicatorValidationError as exc:
            raise InvalidQuery(exc.code, exc.message) from exc
        history = self.source.load(instrument_id, timeframe=timeframe, start=start, end=end)
        try:
            result = self.engine.compute(history.samples, indicator, normalized, price_basis="raw")
        except IndicatorValidationError as exc:
            raise InvalidStoredData("invalid_indicator_input", "Stored indicator inputs cannot be computed safely") from exc
        points = [asdict(point) for point in result.points if history.start <= point.trade_date <= history.end]
        if len(points) > 1000:
            raise InvalidQuery("too_many_indicator_points", "Narrow the output date range to at most 1000 points")
        return {"instrument": history.instrument, "timeframe": history.timeframe,
                "indicator": result.indicator, "algorithm_version": result.algorithm_version,
                "parameters": dict(result.parameters), "price_basis": result.price_basis,
                "field": result.field, "required_samples": result.required_samples,
                "from": history.start, "to": history.end, "points": points, "count": len(points),
                "precision": {"internal_digits": DECIMAL_PRECISION, "output_decimal_places": OUTPUT_SCALE, "rounding": "ROUND_HALF_EVEN"},
                "calculation": {"seed_policy": "full_available_history", "history_from": history.history_from,
                                "input_count": len(history.samples),
                                "prehistory_count": sum(point.trade_date < history.start for point in history.samples),
                                "missing_policy": "reset_on_explicit_missing",
                                "calendar_policy": "observed_sessions_with_known_gaps",
                                "history_completeness": "not_verified"},
                "freshness": history.freshness, "history": history.history,
                "price_adjustment": "unadjusted", "data_kind": "daily_snapshot"}
