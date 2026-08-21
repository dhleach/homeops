"""Tests for Ask HomeOps budget protection, metrics, and edge contracts.

Revision history:
  2026-08-20  Added daily-reset, concurrency, metrics-label, Nginx, and
              Prometheus wiring regressions for the provider safety backstop.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import main
import pytest
from fastapi.testclient import TestClient
from main import (
    DIAGNOSTIC_RATE_LIMIT_ERROR,
    DiagnosticBudget,
    app,
)

client = TestClient(app)
AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def test_budget_resets_daily_window_and_returns_retry_metadata() -> None:
    """The daily budget rejects excess calls, then resets at the next UTC day."""
    now = [datetime(2026, 8, 20, 23, 59, tzinfo=UTC)]
    budget = DiagnosticBudget(max_in_flight=2, daily_limit=1, clock=lambda: now[0])

    first = budget.try_reserve()
    assert first.allowed is True
    budget.release()

    rejected = budget.try_reserve()
    assert rejected.allowed is False
    assert rejected.reason == "global_daily"
    assert rejected.retry_after == 60

    now[0] += timedelta(minutes=1)
    next_day = budget.try_reserve()
    assert next_day.allowed is True
    assert next_day.remaining == 0


def test_diagnostic_rejects_daily_budget_before_gemini(monkeypatch) -> None:
    """A daily-cap rejection must not invoke the provider a second time."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "diagnostic_budget", DiagnosticBudget(max_in_flight=5, daily_limit=1))

    gemini_mock = AsyncMock(return_value="Looks healthy.")
    with (
        patch("main._build_hvac_context", new=AsyncMock(return_value="snapshot")),
        patch("main._call_gemini", new=gemini_mock),
    ):
        first = client.post(
            "/api/diagnostic",
            headers=AUTH_HEADERS,
            json={"question": "How is it?"},
        )
        second = client.post(
            "/api/diagnostic",
            headers=AUTH_HEADERS,
            json={"question": "How is it now?"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"detail": DIAGNOSTIC_RATE_LIMIT_ERROR}
    assert second.headers["retry-after"].isdigit()
    assert second.headers["ratelimit-remaining"] == "0"
    gemini_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_diagnostic_rejects_concurrent_request_before_gemini(monkeypatch) -> None:
    """The in-flight guard fails fast while the existing provider call is busy."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "diagnostic_budget", DiagnosticBudget(max_in_flight=1, daily_limit=5))

    provider_started = asyncio.Event()
    provider_release = asyncio.Event()

    async def slow_gemini(*_args, **_kwargs) -> str:
        provider_started.set()
        await provider_release.wait()
        return "Looks healthy."

    gemini_mock = AsyncMock(side_effect=slow_gemini)
    with (
        patch("main._build_hvac_context", new=AsyncMock(return_value="snapshot")),
        patch("main._call_gemini", new=gemini_mock),
    ):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            first_task = asyncio.create_task(
                async_client.post(
                    "/api/diagnostic",
                    headers=AUTH_HEADERS,
                    json={"question": "First"},
                )
            )
            await asyncio.wait_for(provider_started.wait(), timeout=1)

            second = await async_client.post(
                "/api/diagnostic",
                headers=AUTH_HEADERS,
                json={"question": "Second"},
            )
            provider_release.set()
            first = await first_task

    assert second.status_code == 429
    assert second.json() == {"detail": DIAGNOSTIC_RATE_LIMIT_ERROR}
    assert first.status_code == 200
    gemini_mock.assert_awaited_once()


def test_metrics_are_low_cardinality_and_internal(monkeypatch) -> None:
    """Metrics expose aggregate controls without prompt, identity, or IP labels."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "diagnostic_budget", DiagnosticBudget(max_in_flight=5, daily_limit=5))

    with (
        patch("main._build_hvac_context", new=AsyncMock(return_value="snapshot")),
        patch("main._call_gemini", new=AsyncMock(return_value="Looks healthy.")),
    ):
        response = client.post(
            "/api/diagnostic",
            headers=AUTH_HEADERS,
            json={"question": "How is it?"},
        )

    metrics = client.get("/metrics")
    assert response.status_code == 200
    assert metrics.status_code == 200
    for name in (
        "homeops_diagnostic_requests_total",
        "homeops_diagnostic_rate_limited_total",
        "homeops_diagnostic_provider_calls_total",
        "homeops_diagnostic_provider_latency_seconds",
        "homeops_diagnostic_input_chars",
        "homeops_diagnostic_output_tokens",
        "homeops_diagnostic_inflight",
        "homeops_diagnostic_daily_calls_remaining",
        "homeops_diagnostic_estimated_cost_usd_total",
        "homeops_diagnostic_model_info",
    ):
        assert name in metrics.text
    assert "question=" not in metrics.text
    assert "prompt=" not in metrics.text
    assert "user=" not in metrics.text
    assert "ip=" not in metrics.text
    diagnostic_responses = app.openapi()["paths"]["/api/diagnostic"]["post"]["responses"]
    assert "429" in diagnostic_responses
    assert "/metrics" not in app.openapi()["paths"]


def test_nginx_allows_diagnostic_post_and_blocks_public_metrics() -> None:
    """The edge contract matches the frontend and keeps backend metrics private."""
    config_path = Path(__file__).resolve().parents[2] / "nginx" / "api.homeops.now.conf"
    config = config_path.read_text()

    assert 'Access-Control-Allow-Methods "GET, POST, OPTIONS"' in config
    assert "location = /metrics" in config
    assert "return 404;" in config[config.index("location = /metrics") :]


def test_prometheus_scrapes_backend_on_ec2_loopback() -> None:
    """Prometheus must reach backend metrics without adding a public route."""
    config_path = Path(__file__).resolve().parents[2] / "prometheus" / "prometheus.yml"
    config = config_path.read_text()

    assert 'job_name: "homeops-dashboard-backend"' in config
    assert 'targets: ["127.0.0.1:8000"]' in config
    assert "metrics_path: /metrics" in config
