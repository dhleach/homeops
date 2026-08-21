"""Tests for Ask HomeOps authentication, proxy identity, and quota boundaries.

Revision history:
  2026-08-21  Added bearer-auth error contracts, trusted-proxy/IP regressions,
              atomic quota-store tests, and endpoint fail-closed coverage.
  2026-08-21  Added signed Cognito-style JWT, JWKS outage, and Redis/Valkey
              adapter configuration coverage for the production wiring.
"""

from __future__ import annotations

import ipaddress
import json
from unittest.mock import AsyncMock, patch

import httpx
import jwt
import main
import pytest
import security
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from security import (
    AUTH_FORBIDDEN_ERROR,
    AUTH_REQUIRED_ERROR,
    AUTH_UNAVAILABLE_ERROR,
    DIAGNOSTIC_SCOPE,
    LIMITER_UNAVAILABLE_ERROR,
    AuthenticationUnavailable,
    InMemoryRateLimitStore,
    JwksTokenVerifier,
    Principal,
    ProxyConfig,
    QuotaPolicy,
    QuotaRule,
    RedisRateLimitStore,
    RejectingTokenVerifier,
    UnavailableRateLimitStore,
    authenticate_bearer,
    build_quota_rules,
    extract_client_ip,
    load_token_verifier,
)

client = TestClient(main.app)
AUTH_HEADERS = {"Authorization": "Bearer test-token"}


class MappingVerifier:
    """Small verifier double for pure authentication contract tests."""

    def __init__(self, principal: Principal | None = None, *, unavailable: bool = False):
        self.principal = principal
        self.unavailable = unavailable

    def verify(self, token: str) -> Principal | None:
        if self.unavailable:
            raise AuthenticationUnavailable("identity provider unavailable")
        return self.principal if token == "good-token" else None


class JwksClient:
    """HTTP double for the OIDC discovery key set."""

    def __init__(self, jwks: dict):
        self.jwks = jwks
        self.calls = 0

    def get(self, url: str) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json=self.jwks, request=httpx.Request("GET", url))


class ScriptRedisClient:
    """Redis double that captures script registration and invocation arguments."""

    def __init__(self):
        self.scripts: list[str] = []
        self.calls: list[dict] = []

    def register_script(self, source: str):
        self.scripts.append(source)

        def run(**kwargs):
            self.calls.append(kwargs)
            return [1, "", 3, 5, 1_700_000_060, 0]

        return run


def _signed_access_token(*, issuer: str, audience: str, scope: str) -> tuple[str, dict]:
    private_key = rsa.generate_private_key(public_exponent=65_537, key_size=2_048)
    public_jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "cognito-user-1",
            "client_id": audience,
            "scope": scope,
            "exp": 1_800_000_000,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    return token, {"keys": [public_jwk]}


def _credentials(token: str = "good-token") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_jwks_verifier_accepts_signed_cognito_access_token() -> None:
    issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
    audience = "client-id"
    token, jwks = _signed_access_token(
        issuer=issuer,
        audience=audience,
        scope="openid https://api.homeops.now/diagnostic:read",
    )
    verifier = JwksTokenVerifier(
        issuer=issuer,
        audience=audience,
        audience_claim="client_id",
        http_client=JwksClient(jwks),
        clock=lambda: 1_700_000_000.0,
    )

    principal = verifier.verify(token)

    assert principal == Principal(
        "cognito-user-1",
        frozenset({"openid", "https://api.homeops.now/diagnostic:read"}),
    )


def test_jwks_verifier_rejects_wrong_audience() -> None:
    issuer = "https://issuer.example.test"
    token, jwks = _signed_access_token(issuer=issuer, audience="right-client", scope="read")
    verifier = JwksTokenVerifier(
        issuer=issuer,
        audience="wrong-client",
        audience_claim="client_id",
        http_client=JwksClient(jwks),
    )

    assert verifier.verify(token) is None


def test_jwks_verifier_translates_jwks_outage_to_authentication_unavailable() -> None:
    class BrokenClient:
        def get(self, url: str) -> httpx.Response:
            raise httpx.HTTPError(f"cannot reach {url}")

    issuer = "https://issuer.example.test"
    token, _ = _signed_access_token(issuer=issuer, audience="client-id", scope="read")
    verifier = JwksTokenVerifier(
        issuer=issuer,
        audience="client-id",
        http_client=BrokenClient(),
    )

    with pytest.raises(AuthenticationUnavailable):
        verifier.verify(token)


def test_load_token_verifier_uses_oidc_configuration(monkeypatch) -> None:
    monkeypatch.setenv("ASK_HOMEOPS_OIDC_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("ASK_HOMEOPS_OIDC_AUDIENCE", "client-id")
    monkeypatch.setenv("ASK_HOMEOPS_OIDC_AUDIENCE_CLAIM", "client_id")

    verifier = load_token_verifier()

    assert isinstance(verifier, JwksTokenVerifier)
    assert verifier.issuer == "https://issuer.example.test"
    assert verifier.audience == "client-id"
    assert verifier.audience_claim == "client_id"


def test_redis_store_uses_atomic_scripts_and_hashed_keys(monkeypatch) -> None:
    fake_client = ScriptRedisClient()
    monkeypatch.setattr(security.redis.Redis, "from_url", lambda *args, **kwargs: fake_client)

    store = RedisRateLimitStore("redis://127.0.0.1:6379/0")
    decision = store.acquire(
        (
            QuotaRule("ip", "203.0.113.7", 5, 60, "rate"),
            QuotaRule("user", "cognito-user-1", 2, 1, "inflight"),
        ),
        now=1_700_000_000.0,
    )
    store.release(decision.lease)

    assert decision.allowed is True
    assert len(fake_client.scripts) == 2
    acquire_call = fake_client.calls[0]
    release_call = fake_client.calls[1]
    assert len(acquire_call["keys"]) == 2
    assert all("203.0.113.7" not in key for key in acquire_call["keys"])
    assert all("cognito-user-1" not in key for key in acquire_call["keys"])
    assert len(release_call["keys"]) == 1


def test_load_rate_limit_store_selects_configured_redis(monkeypatch) -> None:
    fake_client = ScriptRedisClient()
    monkeypatch.setenv("ASK_HOMEOPS_LIMITER_BACKEND", "redis")
    monkeypatch.setenv("ASK_HOMEOPS_REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(security.redis.Redis, "from_url", lambda *args, **kwargs: fake_client)

    store = security.load_rate_limit_store()

    assert isinstance(store, RedisRateLimitStore)
    assert store.redis_url == "redis://127.0.0.1:6379/0"


def test_authentication_requires_a_bearer_credential() -> None:
    with pytest.raises(HTTPException) as raised:
        authenticate_bearer(
            None,
            RejectingTokenVerifier(),
            required_scope=DIAGNOSTIC_SCOPE,
        )

    assert raised.value.status_code == 401
    assert raised.value.detail == AUTH_REQUIRED_ERROR
    assert raised.value.headers == {"WWW-Authenticate": "Bearer"}


def test_invalid_bearer_token_returns_401() -> None:
    with pytest.raises(HTTPException) as raised:
        authenticate_bearer(
            _credentials("bad-token"),
            MappingVerifier(
                Principal("user-1", frozenset({DIAGNOSTIC_SCOPE})),
            ),
            required_scope=DIAGNOSTIC_SCOPE,
        )

    assert raised.value.status_code == 401
    assert raised.value.detail == AUTH_REQUIRED_ERROR


def test_verified_identity_without_scope_returns_403() -> None:
    with pytest.raises(HTTPException) as raised:
        authenticate_bearer(
            _credentials(),
            MappingVerifier(Principal("user-1")),
            required_scope=DIAGNOSTIC_SCOPE,
        )

    assert raised.value.status_code == 403
    assert raised.value.detail == AUTH_FORBIDDEN_ERROR


def test_verifier_outage_returns_generic_503_without_downgrading_to_anonymous() -> None:
    with pytest.raises(HTTPException) as raised:
        authenticate_bearer(
            _credentials(),
            MappingVerifier(unavailable=True),
            required_scope=DIAGNOSTIC_SCOPE,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == AUTH_UNAVAILABLE_ERROR
    assert "identity provider unavailable" not in str(raised.value.detail)


def test_client_ip_uses_rightmost_forwarded_value_from_trusted_proxy() -> None:
    config = ProxyConfig(
        trusted_proxy_networks=(ipaddress.ip_network("127.0.0.1/32"),),
        trusted_proxy_hops=1,
    )

    client_ip = extract_client_ip(
        "127.0.0.1",
        {"x-forwarded-for": "198.51.100.99, 203.0.113.7"},
        proxy_config=config,
    )

    assert client_ip == "203.0.113.7"


def test_client_ip_ignores_forwarding_headers_from_untrusted_peer() -> None:
    config = ProxyConfig(
        trusted_proxy_networks=(ipaddress.ip_network("127.0.0.1/32"),),
        trusted_proxy_hops=1,
    )

    client_ip = extract_client_ip(
        "198.51.100.10",
        {
            "x-forwarded-for": "203.0.113.7",
            "x-real-ip": "203.0.113.8",
        },
        proxy_config=config,
    )

    assert client_ip == "198.51.100.10"


def test_client_ip_selects_correct_hop_for_multiple_trusted_proxies() -> None:
    config = ProxyConfig(
        trusted_proxy_networks=(ipaddress.ip_network("127.0.0.1/32"),),
        trusted_proxy_hops=2,
    )

    client_ip = extract_client_ip(
        "127.0.0.1",
        {"x-forwarded-for": "198.51.100.99, 203.0.113.7, 192.0.2.4"},
        proxy_config=config,
    )

    assert client_ip == "203.0.113.7"


def test_quota_rules_use_verified_subject_and_independent_ip_key() -> None:
    rules = build_quota_rules(
        Principal("oidc|user-1", frozenset({DIAGNOSTIC_SCOPE})),
        "203.0.113.7",
        QuotaPolicy(),
    )

    assert {(rule.scope, rule.kind) for rule in rules} == {
        ("ip", "rate"),
        ("user", "rate"),
        ("ip", "daily"),
        ("user", "daily"),
        ("ip", "inflight"),
        ("user", "inflight"),
    }
    assert all(rule.key in {"203.0.113.7", "oidc|user-1"} for rule in rules)


def test_memory_store_rejects_user_rate_before_provider_work() -> None:
    store = InMemoryRateLimitStore(clock=lambda: 1_700_000_000.0)
    policy = QuotaPolicy(
        ip_per_minute=10,
        user_per_minute=1,
        ip_daily=10,
        user_daily=10,
        ip_inflight=2,
        user_inflight=2,
    )
    rules = build_quota_rules(Principal("user-1"), "203.0.113.7", policy)

    first = store.acquire(rules)
    second = store.acquire(rules)

    assert first.allowed is True
    assert second.allowed is False
    assert second.scope == "user_rate"
    assert second.remaining == 0
    store.release(first.lease)


def test_memory_store_checks_all_dimensions_atomically() -> None:
    store = InMemoryRateLimitStore(clock=lambda: 1_700_000_000.0)
    rules = (
        QuotaRule("ip", "shared-ip", 1, 60, "rate"),
        QuotaRule("user", "alice", 2, 60, "rate"),
    )

    first = store.acquire(rules)
    rejected = store.acquire(rules)
    user_only = store.acquire((rules[1],))

    assert first.allowed is True
    assert rejected.scope == "ip_rate"
    assert user_only.allowed is True
    store.release(first.lease)
    store.release(user_only.lease)


def test_memory_store_releases_inflight_but_not_request_counters() -> None:
    store = InMemoryRateLimitStore(clock=lambda: 1_700_000_000.0)
    rules = (QuotaRule("user", "alice", 5, 60, "inflight"),)

    first = store.acquire(rules)
    assert first.allowed is True
    store.release(first.lease)
    second = store.acquire(rules)

    assert second.allowed is True
    store.release(second.lease)


def test_memory_store_resets_fixed_window_at_boundary() -> None:
    now = [1_700_000_099.0]
    store = InMemoryRateLimitStore(clock=lambda: now[0])
    rules = (QuotaRule("user", "alice", 1, 60, "rate"),)

    first = store.acquire(rules)
    rejected = store.acquire(rules)
    now[0] = 1_700_000_100.0
    next_window = store.acquire(rules)

    assert first.allowed is True
    assert rejected.allowed is False
    assert next_window.allowed is True


def test_unavailable_store_is_fail_closed() -> None:
    decision = UnavailableRateLimitStore().acquire(
        (QuotaRule("user", "alice", 1, 60, "rate"),),
        now=1_700_000_000.0,
    )

    assert decision.allowed is False
    assert decision.available is False
    assert decision.scope == "limiter_unavailable"
    assert decision.retry_after == 1


def test_endpoint_rejects_missing_auth_before_provider(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    provider = AsyncMock()

    with patch("main._call_gemini", new=provider):
        response = client.post("/api/diagnostic", json={"question": "hello"})

    assert response.status_code == 401
    assert response.json() == {"detail": AUTH_REQUIRED_ERROR}
    assert response.headers["www-authenticate"] == "Bearer"
    provider.assert_not_awaited()


def test_endpoint_rejects_valid_identity_without_scope(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        main,
        "auth_verifier",
        MappingVerifier(Principal("user-no-access")),
    )
    provider = AsyncMock()

    with patch("main._call_gemini", new=provider):
        response = client.post(
            "/api/diagnostic",
            headers={"Authorization": "Bearer good-token"},
            json={"question": "hello"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": AUTH_FORBIDDEN_ERROR}
    provider.assert_not_awaited()


def test_endpoint_returns_503_when_limiter_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "diagnostic_rate_limiter", UnavailableRateLimitStore())
    provider = AsyncMock()

    with patch("main._call_gemini", new=provider):
        response = client.post(
            "/api/diagnostic",
            headers=AUTH_HEADERS,
            json={"question": "hello"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": LIMITER_UNAVAILABLE_ERROR}
    provider.assert_not_awaited()


def test_endpoint_applies_user_quota_before_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "diagnostic_rate_limiter", InMemoryRateLimitStore())
    monkeypatch.setattr(
        main,
        "quota_policy",
        QuotaPolicy(
            ip_per_minute=10,
            user_per_minute=1,
            ip_daily=10,
            user_daily=10,
            ip_inflight=2,
            user_inflight=2,
        ),
    )
    monkeypatch.setattr(
        main,
        "diagnostic_budget",
        main.DiagnosticBudget(max_in_flight=5, daily_limit=5),
    )
    provider = AsyncMock(return_value="Looks healthy.")

    with (
        patch("main._build_hvac_context", new=AsyncMock(return_value="snapshot")),
        patch("main._call_gemini", new=provider),
    ):
        first = client.post(
            "/api/diagnostic",
            headers=AUTH_HEADERS,
            json={"question": "first"},
        )
        second = client.post(
            "/api/diagnostic",
            headers=AUTH_HEADERS,
            json={"question": "second"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json() == {"detail": main.DIAGNOSTIC_RATE_LIMIT_ERROR}
    assert second.headers["ratelimit-limit"] == "1"
    assert second.headers["ratelimit-remaining"] == "0"
    provider.assert_awaited_once()


def test_endpoint_does_not_accept_spoofed_ip_header_from_untrusted_peer(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main, "diagnostic_rate_limiter", InMemoryRateLimitStore())
    monkeypatch.setattr(
        main,
        "quota_policy",
        QuotaPolicy(
            ip_per_minute=1,
            user_per_minute=10,
            ip_daily=10,
            user_daily=10,
            ip_inflight=2,
            user_inflight=2,
        ),
    )
    monkeypatch.setattr(
        main,
        "diagnostic_budget",
        main.DiagnosticBudget(max_in_flight=5, daily_limit=5),
    )
    provider = AsyncMock(return_value="Looks healthy.")

    with (
        patch("main._build_hvac_context", new=AsyncMock(return_value="snapshot")),
        patch("main._call_gemini", new=provider),
    ):
        first = client.post(
            "/api/diagnostic",
            headers={**AUTH_HEADERS, "X-Forwarded-For": "203.0.113.1"},
            json={"question": "first"},
        )
        second = client.post(
            "/api/diagnostic",
            headers={**AUTH_HEADERS, "X-Forwarded-For": "203.0.113.2"},
            json={"question": "second"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert provider.await_count == 1


def test_openapi_documents_bearer_security_and_quota_errors() -> None:
    schema = main.app.openapi()
    diagnostic = schema["paths"]["/api/diagnostic"]["post"]

    assert schema["components"]["securitySchemes"]["HTTPBearer"]["type"] == "http"
    assert diagnostic["security"] == [{"HTTPBearer": []}]
    assert {"401", "403", "429", "503"}.issubset(diagnostic["responses"])
