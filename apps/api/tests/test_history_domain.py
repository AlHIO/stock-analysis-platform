"""Pure contracts for bounded, market-local historical backfill plans."""
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from stockapp.modules.market_data.domain.history import HistoryDay, HistoryJob


def job(**changes):
    return HistoryJob(**(dict(start=date(2016, 11, 5), end=date(2026, 11, 5),
        markets=("twse", "tpex"), storage_baseline_bytes=1000) | changes))


def test_ten_year_plan_preserves_market_local_dates_and_growth_limit():
    value = job()
    assert value.start == date(2016, 11, 5) and value.end == date(2026, 11, 5)
    assert value.markets == ("twse", "tpex") and value.status == "planned"
    assert value.max_growth_bytes == 100 * 1024**3


@pytest.mark.parametrize("changes", [
    {"start": date(2026, 11, 6)},
    {"start": date(2016, 11, 5), "end": date(2016, 11, 5) + timedelta(days=3660)},
    {"start": datetime(2016, 11, 5, tzinfo=timezone.utc)},
    {"markets": ()}, {"markets": ("twse", "twse")}, {"markets": ("nyse",)},
    {"markets": ["twse"]}, {"storage_baseline_bytes": -1}, {"storage_baseline_bytes": True},
    {"max_growth_bytes": 0}, {"max_growth_bytes": 2**63}, {"status": "unknown"},
    {"created_at": datetime(2026, 11, 5)},
])
def test_invalid_backfill_plan_is_rejected_before_work(changes):
    with pytest.raises(ValueError):
        job(**changes)


@pytest.mark.parametrize("changes", [{"market": "nyse"}, {"attempts": 0}, {"attempts": True},
                                      {"trade_date": datetime(2026, 11, 5, tzinfo=timezone.utc)}])
def test_day_claim_requires_valid_market_local_date_and_positive_attempt(changes):
    with pytest.raises(ValueError):
        HistoryDay(**(dict(job_id=uuid4(), market="twse", trade_date=date(2026, 11, 5), attempts=1) | changes))
