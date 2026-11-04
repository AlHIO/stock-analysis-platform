"""The reviewed contract must match runtime routes and financial JSON types."""
import json
from pathlib import Path

from stockapp.main import create_app


def test_committed_openapi_matches_runtime():
    recorded = json.loads((Path(__file__).resolve().parents[1] / "openapi.json").read_text())
    actual = create_app().openapi()
    assert recorded == actual
    expected = {"/api/v1/instruments", "/api/v1/instruments/{id}", "/api/v1/stocks/{id}/bars",
                "/api/v1/stocks/{id}/latest", "/api/v1/data/status"}
    assert expected <= actual["paths"].keys()
    assert actual["components"]["schemas"]["BarSchema"]["properties"]["close"]["type"] == "string"
