"""Opt-in real I2 raw import; never use a production database or fake identities."""
import asyncio
import os

import pytest
from sqlalchemy import text

from test_market_data_postgres import database  # noqa: F401
from stockapp.config import Settings
from stockapp.modules.institutional.application.ingestion import InstitutionalIngestionService
from stockapp.modules.institutional.infrastructure.ingestion_store import SqlAlchemyInstitutionalIngestionStore
from stockapp.modules.institutional.infrastructure.operations import RuntimeProvider, source_spec

pytestmark = [pytest.mark.integration, pytest.mark.network,
    pytest.mark.skipif(os.getenv("RUN_INSTITUTIONAL_LIVE") != "1" or os.getenv("RUN_NETWORK") != "1"
        or os.getenv("RUN_INTEGRATION") != "1", reason="Opt-in approved-source import into a dedicated temporary DB only")]


def test_live_approved_tpex_raw_pipeline_in_temporary_schema(database):
    assert database.url.database.startswith("stockapp_p2b4_test_")
    with database.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")).startswith("test_market_data_")
    source = source_spec("tpex")
    assert source.approved and source.license == "OGDL-1.0"
    store = SqlAlchemyInstitutionalIngestionStore(database)
    app = InstitutionalIngestionService(store, source, RuntimeProvider("tpex", Settings()))
    report = asyncio.run(app.sync())
    assert report["stats"]["source_failures"] == 0, report["error_summary"]
    assert report["stats"]["received"] > 0 and report["next_row_index"] == report["stats"]["received"]
    assert report["trade_date"] is not None and report["raw"][0]["status_code"] == 200
    # No master securities or dated identity evidence are fabricated for a live
    # check. Raw/schema validation completes, while real rows remain unresolved.
    assert report["stats"]["accepted"] == report["canonical_promotions"] == 0
    assert report["stats"]["unresolved"] == report["stats"]["received"]
