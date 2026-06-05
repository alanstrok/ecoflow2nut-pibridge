"""Tests for the embedded web UI: routing, auth and the control bridge."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ecoflow_nut.config import WebConfig
from ecoflow_nut.webapp import WebServer


class _Harness:
    """Records control calls so tests can assert what the UI forwarded."""

    def __init__(self) -> None:
        self.control_calls: list[tuple[str, bool]] = []
        self.autoshutdown_enabled = False
        self.fail_control = False

    def state(self) -> dict[str, object]:
        return {"soc_percent": 55, "ac_output_watts": 120, "status": "OB"}

    async def control(self, kind: str, enabled: bool) -> str:
        if self.fail_control:
            raise RuntimeError("not connected to device")
        self.control_calls.append((kind, enabled))
        return f"{kind} {'on' if enabled else 'off'}"

    async def history(self, minutes: int) -> list[dict[str, object]]:
        return [{"ts": "2026-06-05T00:00:00", "soc_percent": 50}]

    def autoshutdown_status(self) -> dict[str, object]:
        return {"enabled": self.autoshutdown_enabled, "armed": False}

    async def set_autoshutdown(self, enabled: bool) -> None:
        self.autoshutdown_enabled = enabled


async def _client(
    config: WebConfig, harness: _Harness, history_enabled: bool = True
) -> TestClient:
    server = WebServer(
        config,
        state_provider=harness.state,
        control=harness.control,
        history=harness.history,
        autoshutdown_status=harness.autoshutdown_status,
        set_autoshutdown=harness.set_autoshutdown,
        history_enabled=history_enabled,
    )
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    return client


@pytest.fixture
async def harness() -> _Harness:
    return _Harness()


@pytest.fixture
async def secured(harness: _Harness) -> AsyncIterator[TestClient]:
    client = await _client(WebConfig(auth_token="s3cret"), harness)
    yield client
    await client.close()


async def test_index_served(secured: TestClient) -> None:
    resp = await secured.get("/")
    assert resp.status == 200
    assert "EcoFlow DELTA 3" in await resp.text()


async def test_state_reports_capabilities(secured: TestClient) -> None:
    resp = await secured.get("/api/state")
    assert resp.status == 200
    body = await resp.json()
    assert body["soc_percent"] == 55
    assert body["control_enabled"] is True
    assert body["history_enabled"] is True


async def test_control_requires_token(secured: TestClient, harness: _Harness) -> None:
    resp = await secured.post("/api/control", json={"output": "ac", "enabled": False})
    assert resp.status == 401
    assert harness.control_calls == []


async def test_control_with_token(secured: TestClient, harness: _Harness) -> None:
    resp = await secured.post(
        "/api/control",
        json={"output": "ac", "enabled": False},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 200
    assert harness.control_calls == [("ac", False)]


async def test_control_rejects_bad_output(secured: TestClient) -> None:
    resp = await secured.post(
        "/api/control",
        json={"output": "laser", "enabled": True},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 400


async def test_control_device_error_is_conflict(
    secured: TestClient, harness: _Harness
) -> None:
    harness.fail_control = True
    resp = await secured.post(
        "/api/control",
        json={"output": "ac", "enabled": True},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 409


async def test_control_disabled_without_configured_token(harness: _Harness) -> None:
    client = await _client(WebConfig(auth_token=""), harness)
    try:
        resp = await client.post("/api/control", json={"output": "ac", "enabled": True})
        assert resp.status == 503
    finally:
        await client.close()


async def test_autoshutdown_get_and_set(secured: TestClient, harness: _Harness) -> None:
    resp = await secured.post(
        "/api/autoshutdown",
        json={"enabled": True},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 200
    assert harness.autoshutdown_enabled is True
    got = await (await secured.get("/api/autoshutdown")).json()
    assert got["enabled"] is True


async def test_history_disabled_returns_empty(harness: _Harness) -> None:
    client = await _client(WebConfig(auth_token="s3cret"), harness, history_enabled=False)
    try:
        body = await (await client.get("/api/history")).json()
        assert body == {"enabled": False, "points": []}
    finally:
        await client.close()


async def test_history_enabled_returns_points(secured: TestClient) -> None:
    body = await (await secured.get("/api/history?minutes=120")).json()
    assert body["enabled"] is True
    assert body["minutes"] == 120
    assert body["points"][0]["soc_percent"] == 50


async def test_require_auth_for_read_blocks_unauthenticated(harness: _Harness) -> None:
    client = await _client(
        WebConfig(auth_token="s3cret", require_auth_for_read=True), harness
    )
    try:
        assert (await client.get("/api/state")).status == 401
        ok = await client.get("/api/state", headers={"X-Auth-Token": "s3cret"})
        assert ok.status == 200
    finally:
        await client.close()
