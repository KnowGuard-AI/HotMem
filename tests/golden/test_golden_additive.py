"""Golden additive-contract proof — new optional fields must not change defaults.

This is the executable form of #54's core requirement: "Explicit tests proving
new optional fields do not change default behavior." It proves the file-native
evolution is additive by comparing the observable behavior of a minimal legacy
payload against an extended payload — across add, search, and list — and
confirming they are indistinguishable modulo the intended differences.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hotmem.server import create_app

from .conftest import mask


def _fresh_client(tmp_path, name):
    app = create_app(db_path=tmp_path / f"{name}.sqlite")
    return TestClient(app)


@pytest.fixture
def client(tmp_path):
    app = create_app(db_path=tmp_path / "add.sqlite")
    with TestClient(app) as c:
        yield c


def test_minimal_and_extended_add_produce_identical_search_masks(tmp_path):
    """A legacy {identifier, fact} add and a full add yield the same search shape.

    Each scenario runs on an isolated DB so the comparison is between two
    single-memory stores with the same fact — proving optional fields don't
    change the observable search contract, only (intentionally) the score.
    """
    payload_fact = "duplicate invoice risk"
    minimal_payload = {"identifier": "v", "fact": payload_fact}
    extended_payload = {
        "identifier": "v",
        "fact": payload_fact,
        "source": "erp",
        "importance": 0.9,
        "metadata": {"doc": "x"},
        "ttl_seconds": 3600,
    }

    with _fresh_client(tmp_path, "min") as c:
        c.post("/v1/add", json=minimal_payload)
        minimal = c.post("/v1/search", json={"query": "duplicate invoice", "top_k": 5}).json()
    with _fresh_client(tmp_path, "ext") as c:
        c.post("/v1/add", json=extended_payload)
        extended = c.post("/v1/search", json={"query": "duplicate invoice", "top_k": 5}).json()

    assert mask(minimal) == mask(extended), (
        "minimal and extended add payloads produce different search masks — "
        "the file-native evolution is not purely additive"
    )
    assert mask(minimal) == {
        "memories": [
            {
                "role": "<str>",
                "content": "<str>",
                "memory_id": "<uuid>",
                "identifier": "<str>",
                "score": "<float>",
                "created_at": "<ts>",
            },
        ],
        "count": "<int>",
        "trace_ms": "<float>",
    }


def test_minimal_add_payload_round_trips_through_snapshot(client, tmp_path):
    """A minimal-record snapshot round-trips back into a search hit (compat)."""
    client.post("/v1/add", json={"identifier": "legacy", "fact": "old compatibility fact"})
    swap = tmp_path / "compat.jsonl"
    client.post("/v1/snapshot", json={"file": str(swap)})

    # Fresh DB hydrates the legacy-shaped snapshot and finds the fact.
    app2 = create_app(db_path=tmp_path / "compat2.sqlite")
    with TestClient(app2) as c2:
        loaded = c2.post("/v1/hydrate", json={"file": str(swap)}).json()
        assert loaded["loaded"] == 1
        search = c2.post("/v1/search", json={"query": "compatibility", "top_k": 1}).json()
        assert search["count"] == 1
        assert search["memories"][0]["content"] == "old compatibility fact"


def test_omitted_optional_fields_use_documented_defaults(client):
    """The defaults locked in db.py/db must be the defaults observed via API."""
    client.post("/v1/add", json={"identifier": "d", "fact": "defaults fact"})
    rows = client.get("/v1/memories", params={"identifier": "d"}).json()["memories"]
    row = rows[0]
    # importance default = 0.5, source default = "" (server sets nothing extra)
    assert row["importance"] == 0.5
    assert row["source"] == ""
    assert row["metadata_json"] == "{}"


def test_v2_provenance_columns_are_optional_and_defaulted(client, tmp_path):
    """The file-native provenance columns exist but stay defaulted for legacy adds."""
    client.post("/v1/add", json={"identifier": "p", "fact": "provenance fact"})
    swap = tmp_path / "prov.jsonl"
    client.post("/v1/snapshot", json={"file": str(swap)})

    import json

    first = json.loads(swap.read_text().splitlines()[0])
    # File-native provenance fields are present but empty/default — i.e. additive.
    assert first["source_uri"] == ""
    assert first["source_format"] == ""
    assert first["source_checksum"] == ""
    assert first["byte_offset"] is None
    assert first["byte_length"] is None
    assert first["tier"] == "hot"
    assert first["schema_version"] == 1
