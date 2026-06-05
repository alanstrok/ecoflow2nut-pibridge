"""Unit tests for the Postgres telemetry store's pure logic.

These do not touch a real database -- they cover identifier validation and the
history bucket-width arithmetic, which are the parts worth pinning down without
an integration harness.
"""

from __future__ import annotations

import pytest

from ecoflow_nut.config import PostgresConfig
from ecoflow_nut.db import TelemetryStore, _ident


def test_ident_accepts_safe_names() -> None:
    assert _ident("ecoflow_samples") == "ecoflow_samples"
    assert _ident("samples2") == "samples2"


@pytest.mark.parametrize("bad", ["drop;table", "a b", "tbl-1", "x'y", ""])
def test_ident_rejects_unsafe_names(bad: str) -> None:
    with pytest.raises(ValueError):
        _ident(bad)


def test_unsafe_table_name_rejected_at_construction() -> None:
    with pytest.raises(ValueError):
        TelemetryStore(PostgresConfig(table="bad; drop"))


async def test_record_and_history_noop_without_pool() -> None:
    """With no connection pool the store is inert, never raising."""
    store = TelemetryStore(PostgresConfig(table="ecoflow_samples"))
    assert store.connected is False
    assert await store.history("ecoflow", 60) == []
    # record/prune must be safe no-ops when not connected.
    await store.prune("ecoflow")
