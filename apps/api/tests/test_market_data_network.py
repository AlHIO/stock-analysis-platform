"""Opt-in official-network smoke checks. They never write to PostgreSQL."""
import asyncio
import csv
import io
import json
import os

import pytest

from stockapp.modules.market_data.infrastructure.providers import TpexAdapter, TwseAdapter

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.getenv("RUN_NETWORK") != "1", reason="Set RUN_NETWORK=1 to contact official exchange APIs"),
]


@pytest.mark.parametrize("adapter", [TwseAdapter, TpexAdapter])
def test_live_official_quotes_have_records_and_provider_date(adapter):
    async def scenario():
        async with adapter() as provider:
            document = await provider.fetch(identifier="quotes")
        assert document.status_code == 200
        value = document.body.decode("utf-8-sig")
        rows = json.loads(value) if value.lstrip().startswith("[") else list(csv.DictReader(io.StringIO(value)))
        assert rows and isinstance(rows[0], dict)
        assert all(row.get("Date", row.get("日期")) for row in rows)

    asyncio.run(scenario())
