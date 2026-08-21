"""Provider-neutral authentication and request quota boundaries for Ask HomeOps.

Revision history:
  2026-08-21  Added the bearer-principal contract, trusted-proxy client-IP
              extraction, and atomic rate-limit store interface so authentication
              and per-user/IP quotas can be wired without changing endpoint logic.
  2026-08-21  Added OIDC/JWKS verification and an atomic Redis/Valkey adapter so
              the merged security boundary can be configured for production
              without falling back to anonymous access or process-local quotas.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import jwt
import redis
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

DIAGNOSTIC_SCOPE = "diagnostic:read"
AUTH_REQUIRED_ERROR = "Authentication required"
AUTH_FORBIDDEN_ERROR = "Diagnostic access forbidden"
AUTH_UNAVAILABLE_ERROR = "Authentication service temporarily unavailable"
LIMITER_UNAVAILABLE_ERROR = "Diagnostic rate limiter temporarily unavailable"

QuotaKind = Literal["rate", "daily", "inflight"]


class AuthenticationUnavailable(RuntimeError):
    """Raised by a token verifier when its identity provider cannot be reached."""


@dataclass(frozen=True, slots=True)
class Principal:
    """The minimum verified identity needed by the diagnostic boundary."""

    subject: str
    scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.subject or not self.subject.strip():
            raise ValueError("principal subject must not be blank")


class TokenVerifier(Protocol):
    """Provider-neutral bearer-token verification contract."""

    def verify(self, token: str) -> Principal | None:
        """Return a verified principal, or ``None`` for an invalid token."""


class RejectingTokenVerifier:
    """Safe default until a real OIDC-compatible verifier is configured."""

    def verify(self, token: str) -> Principal | None:
        del token
        return None


class JwksTokenVerifier:
    """Verify OIDC access tokens against a cached RSA JWKS document.

    The verifier accepts both ordinary OIDC ``aud`` claims and Amazon Cognito
    access-token ``client_id`` claims.  It deliberately validates the issuer,
    signature, expiry, subject, and configured audience before exposing any
    token scopes to the application.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        audience_claim: Literal["auto", "aud", "client_id"] = "auto",
        http_client: httpx.Client | None = None,
        key_cache_ttl_seconds: int = 3_600,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.issuer = issuer.strip()
        self.audience = audience
        self.jwks_url = jwks_url or f"{self.issuer.rstrip('/')}/.well-known/jwks.json"
        self.audience_claim = audience_claim
        self._http_client = http_client or httpx.Client(timeout=2.0)
        self._key_cache_ttl_seconds = max(60, key_cache_ttl_seconds)
        self._clock = clock or time.time
        self._keys: dict[str, dict[str, Any]] = {}
        self._keys_loaded_at = 0.0
        self._lock = threading.Lock()

    def _load_keys(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        """Fetch and cache the provider's signing keys, translating outages safely."""
        now = self._clock()
        with self._lock:
            if (
                not force
                and self._keys
                and now - self._keys_loaded_at < self._key_cache_ttl_seconds
            ):
                return self._keys
            try:
                response = self._http_client.get(self.jwks_url)
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                raise AuthenticationUnavailable("OIDC JWKS unavailable") from exc

            raw_keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(raw_keys, list):
                raise AuthenticationUnavailable("OIDC JWKS response is invalid")
            keys = {
                item["kid"]: item
                for item in raw_keys
                if isinstance(item, dict) and isinstance(item.get("kid"), str)
            }
            if not keys:
                raise AuthenticationUnavailable("OIDC JWKS contains no usable keys")
            self._keys = keys
            self._keys_loaded_at = now
            return keys

    def _signing_key(self, key_id: str) -> Any | None:
        """Return the RSA signing key, refreshing once for a rotated key."""
        keys = self._load_keys()
        jwk = keys.get(key_id)
        if jwk is None:
            jwk = self._load_keys(force=True).get(key_id)
        if jwk is None:
            return None
        try:
            return jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuthenticationUnavailable("OIDC JWKS key is invalid") from exc

    def _audience_matches(self, claims: Mapping[str, Any]) -> bool:
        """Validate OIDC ``aud`` or Cognito access-token ``client_id``."""
        if self.audience_claim == "aud":
            actual = claims.get("aud")
        elif self.audience_claim == "client_id":
            actual = claims.get("client_id")
        else:
            actual = claims.get("aud") or claims.get("client_id")
        if isinstance(actual, str):
            return actual == self.audience
        if isinstance(actual, list):
            return self.audience in actual
        return False

    @staticmethod
    def _scopes(claims: Mapping[str, Any]) -> frozenset[str]:
        """Normalize the OIDC/Cognito space-delimited scope claim."""
        raw_scope = claims.get("scope")
        if isinstance(raw_scope, str):
            return frozenset(part for part in raw_scope.split() if part)
        if isinstance(raw_scope, list):
            return frozenset(part for part in raw_scope if isinstance(part, str) and part)
        raw_scp = claims.get("scp")
        if isinstance(raw_scp, list):
            return frozenset(part for part in raw_scp if isinstance(part, str) and part)
        return frozenset()

    def verify(self, token: str) -> Principal | None:
        """Return a principal only after cryptographic and claim validation."""
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                return None
            signing_key = self._signing_key(header["kid"])
            if signing_key is None:
                return None
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
            )
        except AuthenticationUnavailable:
            raise
        except (jwt.exceptions.PyJWTError, TypeError, ValueError):
            return None

        subject = claims.get("sub")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or not self._audience_matches(claims)
        ):
            return None
        return Principal(subject=subject, scopes=self._scopes(claims))


def load_token_verifier() -> TokenVerifier:
    """Load the configured OIDC verifier, retaining a safe rejecting default."""
    issuer = os.environ.get("ASK_HOMEOPS_OIDC_ISSUER", "").strip()
    audience = os.environ.get("ASK_HOMEOPS_OIDC_AUDIENCE", "").strip()
    if not issuer or not audience:
        logger.warning("Ask HomeOps OIDC verifier is not configured; authentication fails closed")
        return RejectingTokenVerifier()

    audience_claim = os.environ.get("ASK_HOMEOPS_OIDC_AUDIENCE_CLAIM", "auto").strip().lower()
    if audience_claim not in {"auto", "aud", "client_id"}:
        audience_claim = "auto"
    jwks_url = os.environ.get("ASK_HOMEOPS_OIDC_JWKS_URL", "").strip() or None
    return JwksTokenVerifier(
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        audience_claim=audience_claim,  # type: ignore[arg-type]
    )


def load_diagnostic_scope() -> str:
    """Load the provider-specific scope while preserving the contract default."""
    return (
        os.environ.get("ASK_HOMEOPS_DIAGNOSTIC_SCOPE", DIAGNOSTIC_SCOPE).strip() or DIAGNOSTIC_SCOPE
    )


def authenticate_bearer(
    credentials: HTTPAuthorizationCredentials | None,
    verifier: TokenVerifier,
    *,
    required_scope: str,
) -> Principal:
    """Validate a bearer credential and require one verified access scope."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTH_REQUIRED_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials.strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTH_REQUIRED_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        principal = verifier.verify(token)
    except AuthenticationUnavailable as exc:
        logger.warning("Ask HomeOps authentication provider unavailable: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=AUTH_UNAVAILABLE_ERROR,
        ) from None
    except Exception as exc:  # noqa: BLE001
        # A verifier failure must never downgrade a request to anonymous access.
        logger.warning("Ask HomeOps authentication verification failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=AUTH_UNAVAILABLE_ERROR,
        ) from None

    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AUTH_REQUIRED_ERROR,
            headers={"WWW-Authenticate": "Bearer"},
        )
    if required_scope not in principal.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=AUTH_FORBIDDEN_ERROR,
        )
    return principal


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """Trusted reverse-proxy settings used to resolve the quota IP key."""

    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    trusted_proxy_hops: int = 1


def _parse_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse configured proxy networks, ignoring malformed entries safely."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            logger.warning("Ignoring malformed trusted proxy network configuration")
    return tuple(networks)


def load_proxy_config() -> ProxyConfig:
    """Load a conservative localhost-only proxy configuration by default."""
    raw_networks = os.environ.get(
        "ASK_HOMEOPS_TRUSTED_PROXY_IPS",
        "127.0.0.1/32,::1/128",
    )
    try:
        hops = int(os.environ.get("ASK_HOMEOPS_TRUSTED_PROXY_HOPS", "1"))
    except (TypeError, ValueError):
        hops = 1
    if hops < 1:
        hops = 1
    return ProxyConfig(_parse_networks(raw_networks), hops)


def _canonical_ip(value: str | None) -> str | None:
    """Return a canonical IP string, or ``None`` for an untrusted value."""
    if not value:
        return None
    try:
        return ipaddress.ip_address(value.strip()).compressed
    except ValueError:
        return None


def extract_client_ip(
    peer_host: str | None,
    headers: Mapping[str, str],
    *,
    proxy_config: ProxyConfig,
) -> str:
    """Resolve the client IP without trusting forwarding headers from the Internet.

    Nginx appends the actual connection address to ``X-Forwarded-For``. Selecting
    from the right after confirming the direct peer is a configured proxy means a
    caller-supplied left-most value cannot replace the address used for quotas.
    """
    peer_ip = _canonical_ip(peer_host)
    fallback = peer_ip or "unknown"
    if peer_ip is None:
        return fallback
    peer_address = ipaddress.ip_address(peer_ip)

    try:
        trusted_peer = any(
            peer_address in network for network in proxy_config.trusted_proxy_networks
        )
    except TypeError:
        trusted_peer = False
    if not trusted_peer:
        return peer_ip

    forwarded = headers.get("x-forwarded-for", "")
    forwarded_ips = [
        parsed for value in forwarded.split(",") if (parsed := _canonical_ip(value)) is not None
    ]
    if len(forwarded_ips) >= proxy_config.trusted_proxy_hops:
        return forwarded_ips[-proxy_config.trusted_proxy_hops]

    real_ip = _canonical_ip(headers.get("x-real-ip"))
    return real_ip or fallback


@dataclass(frozen=True, slots=True)
class QuotaRule:
    """One independent quota dimension for an opaque identity key."""

    scope: Literal["ip", "user"]
    key: str
    limit: int
    window_seconds: int
    kind: QuotaKind

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("quota key must not be blank")
        if self.limit < 1 or self.window_seconds < 1:
            raise ValueError("quota limit and window must be positive")


@dataclass(frozen=True, slots=True)
class QuotaPolicy:
    """Conservative starting limits from the Ask HomeOps threat model."""

    ip_per_minute: int = 30
    user_per_minute: int = 10
    ip_daily: int = 200
    user_daily: int = 100
    ip_inflight: int = 5
    user_inflight: int = 2

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.ip_per_minute,
                self.user_per_minute,
                self.ip_daily,
                self.user_daily,
                self.ip_inflight,
                self.user_inflight,
            )
        ):
            raise ValueError("all diagnostic quota limits must be positive")


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive quota override without breaking application startup."""
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def load_quota_policy() -> QuotaPolicy:
    """Load quota policy overrides while retaining safe defaults."""
    return QuotaPolicy(
        ip_per_minute=_positive_int_env("ASK_HOMEOPS_IP_PER_MINUTE_LIMIT", 30),
        user_per_minute=_positive_int_env("ASK_HOMEOPS_USER_PER_MINUTE_LIMIT", 10),
        ip_daily=_positive_int_env("ASK_HOMEOPS_IP_DAILY_LIMIT", 200),
        user_daily=_positive_int_env("ASK_HOMEOPS_USER_DAILY_LIMIT", 100),
        ip_inflight=_positive_int_env("ASK_HOMEOPS_IP_MAX_IN_FLIGHT", 5),
        user_inflight=_positive_int_env("ASK_HOMEOPS_USER_MAX_IN_FLIGHT", 2),
    )


def build_quota_rules(
    principal: Principal,
    client_ip: str,
    policy: QuotaPolicy,
) -> tuple[QuotaRule, ...]:
    """Build independent IP and verified-subject rules for one request."""
    return (
        QuotaRule("ip", client_ip, policy.ip_per_minute, 60, "rate"),
        QuotaRule("user", principal.subject, policy.user_per_minute, 60, "rate"),
        QuotaRule("ip", client_ip, policy.ip_daily, 86_400, "daily"),
        QuotaRule("user", principal.subject, policy.user_daily, 86_400, "daily"),
        QuotaRule("ip", client_ip, policy.ip_inflight, 1, "inflight"),
        QuotaRule("user", principal.subject, policy.user_inflight, 1, "inflight"),
    )


@dataclass(frozen=True, slots=True)
class RateLimitLease:
    """In-flight keys held until the provider work completes."""

    inflight_keys: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Atomic result of reserving all quota dimensions for a request."""

    allowed: bool
    available: bool
    scope: str | None
    remaining: int
    limit: int
    reset_at: int
    retry_after: int
    lease: RateLimitLease | None = None


class RateLimitStore(Protocol):
    """Atomic store contract suitable for Redis/Valkey or another shared backend."""

    def acquire(
        self,
        rules: Sequence[QuotaRule],
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Atomically check and reserve all rules for one request."""

    def release(self, lease: RateLimitLease) -> None:
        """Release only the in-flight portion of an accepted request."""


@dataclass
class _WindowState:
    window_start: int
    count: int = 0


class InMemoryRateLimitStore:
    """Deterministic single-process reference store for tests/local development.

    This class intentionally does not pretend to be production-shared. The
    ``RateLimitStore`` protocol is the seam for an atomic Redis/Valkey adapter.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._windows: dict[tuple[str, str, str, int], _WindowState] = {}
        self._inflight: dict[tuple[str, str], int] = {}

    @staticmethod
    def _window_start(now: float, window_seconds: int) -> int:
        return int(now // window_seconds) * window_seconds

    @staticmethod
    def _denied(
        rule: QuotaRule,
        *,
        used: int,
        reset_at: int,
        now: float,
    ) -> RateLimitDecision:
        return RateLimitDecision(
            allowed=False,
            available=True,
            scope=f"{rule.scope}_{rule.kind}",
            remaining=max(0, rule.limit - used),
            limit=rule.limit,
            reset_at=reset_at,
            retry_after=max(1, int(reset_at - now)),
        )

    def acquire(
        self,
        rules: Sequence[QuotaRule],
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Check every dimension under one lock before mutating any counter."""
        now = float(self._clock() if now is None else now)
        with self._lock:
            pending_windows: list[tuple[tuple[str, str, str, int], int]] = []
            pending_inflight: list[tuple[str, str]] = []
            minimum_remaining: tuple[int, int, int] | None = None

            for rule in rules:
                if rule.kind == "inflight":
                    inflight_key = (rule.scope, rule.key)
                    used = self._inflight.get(inflight_key, 0)
                    if used >= rule.limit:
                        return self._denied(rule, used=used, reset_at=int(now) + 1, now=now)
                    pending_inflight.append(inflight_key)
                    candidate = (rule.limit - used - 1, rule.limit, int(now) + 1)
                else:
                    window_start = self._window_start(now, rule.window_seconds)
                    store_key = (rule.kind, rule.scope, rule.key, rule.window_seconds)
                    state = self._windows.get(store_key)
                    used = state.count if state and state.window_start == window_start else 0
                    reset_at = window_start + rule.window_seconds
                    if used >= rule.limit:
                        return self._denied(rule, used=used, reset_at=reset_at, now=now)
                    pending_windows.append((store_key, window_start))
                    candidate = (rule.limit - used - 1, rule.limit, reset_at)

                if minimum_remaining is None or candidate[0] < minimum_remaining[0]:
                    minimum_remaining = candidate

            for store_key, window_start in pending_windows:
                state = self._windows.get(store_key)
                if state is None or state.window_start != window_start:
                    self._windows[store_key] = _WindowState(window_start=window_start, count=1)
                else:
                    state.count += 1
            for inflight_key in pending_inflight:
                self._inflight[inflight_key] = self._inflight.get(inflight_key, 0) + 1

            if minimum_remaining is None:
                # ``build_quota_rules`` always supplies rules, but keep the store
                # safe for an accidental empty call from another integration.
                return RateLimitDecision(True, True, None, 0, 0, int(now) + 1, 0)
            return RateLimitDecision(
                allowed=True,
                available=True,
                scope=None,
                remaining=minimum_remaining[0],
                limit=minimum_remaining[1],
                reset_at=minimum_remaining[2],
                retry_after=0,
                lease=RateLimitLease(tuple(pending_inflight)),
            )

    def release(self, lease: RateLimitLease) -> None:
        """Release in-flight reservations while retaining request counters."""
        with self._lock:
            for inflight_key in lease.inflight_keys:
                current = self._inflight.get(inflight_key, 0)
                if current <= 1:
                    self._inflight.pop(inflight_key, None)
                else:
                    self._inflight[inflight_key] = current - 1


class UnavailableRateLimitStore:
    """Fail-closed store used until a shared production backend is configured."""

    def acquire(
        self,
        rules: Sequence[QuotaRule],
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        del rules
        now = time.time() if now is None else now
        return RateLimitDecision(
            allowed=False,
            available=False,
            scope="limiter_unavailable",
            remaining=0,
            limit=0,
            reset_at=int(now) + 1,
            retry_after=1,
        )

    def release(self, lease: RateLimitLease) -> None:
        del lease


_REDIS_ACQUIRE_SCRIPT = """
local now = tonumber(ARGV[1])
local rule_count = tonumber(ARGV[2])
local arg_index = 3
local minimum_remaining = nil
local minimum_limit = nil
local minimum_reset = nil

-- Check every dimension before changing any counter. This makes a request
-- either reserve all of its user/IP windows or reserve none of them.
for index = 1, rule_count do
    local kind = ARGV[arg_index]
    local scope = ARGV[arg_index + 1]
    local limit = tonumber(ARGV[arg_index + 2])
    local reset_at = tonumber(ARGV[arg_index + 5])
    local used = tonumber(redis.call('GET', KEYS[index]) or '0')
    if used >= limit then
        local retry_after = math.max(1, math.ceil(reset_at - now))
        return {0, scope .. '_' .. kind, math.max(0, limit - used), limit, reset_at, retry_after}
    end
    local remaining = limit - used - 1
    if minimum_remaining == nil or remaining < minimum_remaining then
        minimum_remaining = remaining
        minimum_limit = limit
        minimum_reset = reset_at
    end
    arg_index = arg_index + 6
end

arg_index = 3
for index = 1, rule_count do
    local kind = ARGV[arg_index]
    local window_seconds = tonumber(ARGV[arg_index + 3])
    redis.call('INCR', KEYS[index])
    -- Window counters expire naturally. In-flight counters also get a
    -- recovery TTL so a crashed worker cannot permanently block a key.
    local ttl = window_seconds
    if kind == 'inflight' then
        ttl = math.max(30, window_seconds)
    end
    redis.call('EXPIRE', KEYS[index], ttl)
    arg_index = arg_index + 6
end

return {1, '', minimum_remaining, minimum_limit, minimum_reset, 0}
"""

_REDIS_RELEASE_SCRIPT = """
for index = 1, #KEYS do
    local current = tonumber(redis.call('GET', KEYS[index]) or '0')
    if current <= 1 then
        redis.call('DEL', KEYS[index])
    else
        redis.call('DECR', KEYS[index])
    end
end
return 1
"""


class RedisRateLimitStore:
    """Atomic shared quota store backed by Redis-compatible Valkey."""

    def __init__(
        self,
        redis_url: str,
        *,
        key_prefix: str = "homeops:diagnostic",
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not redis_url.strip():
            raise ValueError("redis URL must not be blank")
        self.redis_url = redis_url
        self.key_prefix = key_prefix
        self._clock = clock or time.time
        self._client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        self._acquire = self._client.register_script(_REDIS_ACQUIRE_SCRIPT)
        self._release = self._client.register_script(_REDIS_RELEASE_SCRIPT)

    def _digest(self, scope: str, key: str) -> str:
        """Bound and anonymize user/IP material before it reaches Redis keys."""
        return hashlib.sha256(f"{scope}:{key}".encode()).hexdigest()

    def _redis_key(self, rule: QuotaRule, *, window_start: int) -> str:
        digest = self._digest(rule.scope, rule.key)
        if rule.kind == "inflight":
            return f"{self.key_prefix}:inflight:{rule.scope}:{digest}"
        return f"{self.key_prefix}:{rule.kind}:{rule.scope}:{digest}:{window_start}"

    def acquire(
        self,
        rules: Sequence[QuotaRule],
        *,
        now: float | None = None,
    ) -> RateLimitDecision:
        """Atomically reserve every quota rule using one Redis Lua call."""
        now = float(self._clock() if now is None else now)
        keys: list[str] = []
        args: list[str | int | float] = [now, len(rules)]
        for rule in rules:
            if rule.kind == "inflight":
                window_start = 0
                reset_at = int(now) + 1
            else:
                window_start = int(now // rule.window_seconds) * rule.window_seconds
                reset_at = window_start + rule.window_seconds
            keys.append(self._redis_key(rule, window_start=window_start))
            args.extend(
                (
                    rule.kind,
                    rule.scope,
                    rule.limit,
                    rule.window_seconds,
                    window_start,
                    reset_at,
                )
            )

        if not rules:
            return RateLimitDecision(
                allowed=True,
                available=True,
                scope=None,
                remaining=0,
                limit=0,
                reset_at=int(now) + 1,
                retry_after=0,
            )

        result = self._acquire(keys=keys, args=args)
        if not isinstance(result, (list, tuple)) or len(result) != 6:
            raise RuntimeError("Redis quota script returned an invalid result")
        allowed = bool(int(result[0]))
        scope = str(result[1]) or None
        lease = None
        if allowed:
            lease = RateLimitLease(
                tuple((rule.scope, rule.key) for rule in rules if rule.kind == "inflight")
            )
        return RateLimitDecision(
            allowed=allowed,
            available=True,
            scope=scope,
            remaining=int(result[2]),
            limit=int(result[3]),
            reset_at=int(result[4]),
            retry_after=max(0, int(result[5])),
            lease=lease,
        )

    def release(self, lease: RateLimitLease) -> None:
        """Release only in-flight keys; completed request windows remain counted."""
        if not lease.inflight_keys:
            return
        keys = [
            f"{self.key_prefix}:inflight:{scope}:{self._digest(scope, key)}"
            for scope, key in lease.inflight_keys
        ]
        self._release(keys=keys, args=[])


def load_rate_limit_store() -> RateLimitStore:
    """Select an explicit backend; default to fail-closed when unconfigured."""
    backend = os.environ.get("ASK_HOMEOPS_LIMITER_BACKEND", "unconfigured").lower()
    if backend == "memory":
        logger.warning("Ask HomeOps is using the process-local memory limiter")
        return InMemoryRateLimitStore()
    if backend == "redis":
        redis_url = os.environ.get("ASK_HOMEOPS_REDIS_URL", "").strip()
        if redis_url:
            return RedisRateLimitStore(redis_url)
        logger.error("Ask HomeOps Redis limiter selected without ASK_HOMEOPS_REDIS_URL")
    return UnavailableRateLimitStore()
