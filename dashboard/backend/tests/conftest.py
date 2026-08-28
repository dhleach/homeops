"""Shared backend test configuration.

Revision history:
  2026-08-27  Pin endpoint tests to the OpenAI Luna default so the explicit
              Gemini rollback configuration cannot leak between test cases.
  2026-08-21  Added deterministic test-only auth and in-memory quota wiring so
              endpoint tests exercise the production dependency boundary without
              requiring identity-provider or shared-store credentials.
"""

from __future__ import annotations

import main
import pytest
from security import DIAGNOSTIC_SCOPE, InMemoryRateLimitStore, Principal


class TestTokenVerifier:
    """Small deterministic verifier used only by the backend test suite."""

    def verify(self, token: str) -> Principal | None:
        if token == "test-token":
            return Principal("user-test", frozenset({DIAGNOSTIC_SCOPE}))
        if token == "limited-token":
            return Principal("user-limited", frozenset({DIAGNOSTIC_SCOPE}))
        if token == "no-access-token":
            return Principal("user-no-access", frozenset())
        return None


@pytest.fixture(autouse=True)
def configure_diagnostic_security(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give existing happy-path tests explicit deterministic security dependencies."""
    monkeypatch.setattr(main, "auth_verifier", TestTokenVerifier())
    monkeypatch.setattr(main, "diagnostic_rate_limiter", InMemoryRateLimitStore())
    monkeypatch.setenv(main.DIAGNOSTIC_PROVIDER_ENV, main.DEFAULT_DIAGNOSTIC_PROVIDER)
