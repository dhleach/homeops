"""Provider-neutral authentication and request quota boundaries for Ask HomeOps.

Revision history:
  2026-08-21  Added the bearer-principal contract, trusted-proxy client-IP
              extraction, and atomic rate-limit store interface so authentication
              and per-user/IP quotas can be wired without changing endpoint logic.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

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


def load_rate_limit_store() -> RateLimitStore:
    """Select only the explicit local reference backend; default to fail-closed."""
    if os.environ.get("ASK_HOMEOPS_LIMITER_BACKEND", "unconfigured").lower() == "memory":
        logger.warning("Ask HomeOps is using the process-local memory limiter")
        return InMemoryRateLimitStore()
    return UnavailableRateLimitStore()
