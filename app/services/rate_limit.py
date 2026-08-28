"""Rate limiting (Part I §2.4; Part II §6).

Part II §6 is explicit: *"rate-limit counters (per account/IP, per endpoint)
should not live in the primary relational store as hot-write rows — use an
appropriately fast mechanism (e.g. in-memory/cache-backed counters) so abuse
checks don't become a bottleneck or bloat the transactional tables."*

So the hot path moves here. Note what does **not** move: ``TRX_LOGIN_ATTEMPT``
is still written for every attempt, because §2.8 needs that audit trail for the
suspicious-activity dashboard. The split is deliberate —

* **this module** answers "may this request proceed *right now*", on every
  request, from a cache;
* **the ledger** answers "what has been happening", for humans, and backs the
  slower account-lockout check.

Two consequences worth knowing:

* **Counters are approximate and disposable.** Losing Redis loses some counts;
  it must never lose an audit record.
* **It fails open.** If the cache is unreachable the request is allowed and the
  failure is logged loudly. Failing closed would lock every customer out of the
  store because a cache blipped — and the durable lockout in
  ``services/activity.py`` is still there as the backstop.

The window is a **sliding counter**, not a fixed one: a fixed window lets an
attacker send the full allowance either side of a boundary, i.e. twice the limit
in a moment. Weighting the previous window removes that for a couple of extra
arithmetic operations.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Protocol

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

#: Key prefix, so a shared Redis can host other things safely.
NAMESPACE = "jec:rl"


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of one rate-limit check."""

    allowed: bool
    #: Approximate hits counted in the sliding window.
    used: float
    limit: int
    #: Seconds until the caller may reasonably retry. 0 when allowed.
    retry_after: int = 0

    @property
    def remaining(self) -> int:
        return max(int(self.limit - self.used), 0)


@dataclass(frozen=True, slots=True)
class Policy:
    """A named limit: how many hits per how many seconds."""

    name: str
    limit: int
    window_seconds: int


class Backend(Protocol):
    """Counter storage. Must be atomic per key."""

    def bump(self, key: str, ttl_seconds: int) -> int:
        """Increment ``key``, set its TTL, and return the new value."""

    def read(self, key: str) -> int:
        """Current value of ``key``, or 0."""

    def clear(self, key: str) -> None:
        """Drop ``key`` — used when an attempt succeeds."""


class MemoryBackend:
    """In-process counters.

    The default, and correct for a single web process. Across several workers
    each holds its own counts, so the effective limit multiplies by the worker
    count — fine for development, not for production, which is why Redis is
    configured in staging and beyond.
    """

    def __init__(self) -> None:
        self._values: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _purge(self, now: float) -> None:
        expired = [key for key, (_, exp) in self._values.items() if exp <= now]
        for key in expired:
            del self._values[key]

    def bump(self, key: str, ttl_seconds: int) -> int:
        now = time.time()
        with self._lock:
            self._purge(now)
            count, expires = self._values.get(key, (0, now + ttl_seconds))
            if expires <= now:
                count, expires = 0, now + ttl_seconds
            count += 1
            self._values[key] = (count, expires)
            return count

    def read(self, key: str) -> int:
        now = time.time()
        with self._lock:
            count, expires = self._values.get(key, (0, 0.0))
            return count if expires > now else 0

    def clear(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


class RedisBackend:
    """Redis counters — the production path (Part II §7.4).

    ``INCR`` + ``EXPIRE`` are pipelined so the increment and its TTL are one
    round trip; without the TTL a key would live forever after its window
    passes.
    """

    def __init__(self, client) -> None:
        self._client = client

    def bump(self, key: str, ttl_seconds: int) -> int:
        pipeline = self._client.pipeline()
        pipeline.incr(key)
        # Only sets a TTL if the key has none, so a long-lived window is not
        # repeatedly extended by traffic inside it.
        pipeline.expire(key, ttl_seconds, nx=True)
        return int(pipeline.execute()[0])

    def read(self, key: str) -> int:
        value = self._client.get(key)
        return int(value) if value else 0

    def clear(self, key: str) -> None:
        self._client.delete(key)


class Limiter:
    """Sliding-window rate limiter over a pluggable backend."""

    def __init__(self, backend: Backend | None = None) -> None:
        self._backend = backend or _build_backend()

    @property
    def backend(self) -> Backend:
        return self._backend

    def _keys(self, policy: Policy, identity: str, now: float) -> tuple[str, str, float]:
        window = policy.window_seconds
        current = int(now // window)
        elapsed = (now % window) / window
        base = f"{NAMESPACE}:{policy.name}:{identity}"
        return f"{base}:{current}", f"{base}:{current - 1}", elapsed

    def check(self, policy: Policy, identity: str, *, now: float | None = None) -> Decision:
        """Count this hit and decide whether it is allowed.

        Always counts, including when it refuses — otherwise an attacker who
        ignores the refusal would let the window drain while still hammering.
        """
        now = now or time.time()
        current_key, previous_key, elapsed = self._keys(policy, identity, now)

        try:
            current = self._backend.bump(current_key, policy.window_seconds * 2)
            previous = self._backend.read(previous_key)
        except Exception:  # noqa: BLE001 - a cache outage must not close the shop
            log.exception(
                "rate_limit_backend_unavailable",
                extra={"policy": policy.name},
            )
            return Decision(allowed=True, used=0, limit=policy.limit)

        # Weight the previous window by how much of it still overlaps.
        used = previous * (1 - elapsed) + current

        if used > policy.limit:
            retry_after = max(int(policy.window_seconds * (1 - elapsed)), 1)
            log.info(
                "rate_limited",
                extra={"policy": policy.name, "identity": identity, "used": round(used, 2)},
            )
            return Decision(
                allowed=False, used=used, limit=policy.limit, retry_after=retry_after
            )

        return Decision(allowed=True, used=used, limit=policy.limit)

    def peek(self, policy: Policy, identity: str, *, now: float | None = None) -> Decision:
        """Read the current position without counting a hit.

        Used to decide whether to show a CAPTCHA, which must not itself consume
        the visitor's allowance.
        """
        now = now or time.time()
        current_key, previous_key, elapsed = self._keys(policy, identity, now)
        try:
            used = self._backend.read(previous_key) * (1 - elapsed) + self._backend.read(
                current_key
            )
        except Exception:  # noqa: BLE001
            log.exception("rate_limit_backend_unavailable", extra={"policy": policy.name})
            return Decision(allowed=True, used=0, limit=policy.limit)

        return Decision(allowed=used <= policy.limit, used=used, limit=policy.limit)

    def reset(self, policy: Policy, identity: str, *, now: float | None = None) -> None:
        """Forget an identity's counts — called after a successful sign-in, so a
        customer who mistyped twice is not still near their limit."""
        now = now or time.time()
        current_key, previous_key, _ = self._keys(policy, identity, now)
        try:
            self._backend.clear(current_key)
            self._backend.clear(previous_key)
        except Exception:  # noqa: BLE001
            log.exception("rate_limit_backend_unavailable", extra={"policy": policy.name})


# ---------------------------------------------------------------------------
# Policies (Part I §2.4)
# ---------------------------------------------------------------------------


def login_policy() -> Policy:
    return Policy("login", settings.rate_limit_login_per_minute, 60)


def register_policy() -> Policy:
    return Policy("register", settings.rate_limit_register_per_hour, 3600)


def password_reset_policy() -> Policy:
    return Policy("password_reset", settings.rate_limit_password_reset_per_hour, 3600)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _build_backend() -> Backend:
    """Redis where configured and reachable, in-process memory otherwise.

    Reachability is checked once at startup rather than per request: a limiter
    that pings Redis on every login would add the latency this module exists to
    avoid.
    """
    if settings.app_env == "development" and not settings.redis_url:
        return MemoryBackend()

    try:
        import redis

        client = redis.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )
        client.ping()
        log.info("rate_limit_backend", extra={"backend": "redis"})
        return RedisBackend(client)
    except Exception:  # noqa: BLE001 - any connection problem falls back
        log.warning(
            "rate_limit_redis_unavailable",
            extra={"backend": "memory", "url": settings.redis_url},
        )
        return MemoryBackend()


_limiter: Limiter | None = None


def limiter() -> Limiter:
    """The process-wide limiter."""
    global _limiter
    if _limiter is None:
        _limiter = Limiter()
    return _limiter


def reset_limiter(backend: Backend | None = None) -> Limiter:
    """Replace the limiter — for tests, and after a config change."""
    global _limiter
    _limiter = Limiter(backend)
    return _limiter


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


def enforce(policy: Policy, identity: str) -> None:
    """Check and raise :class:`RateLimited` if over the limit."""
    from app.core.errors import RateLimited

    decision = limiter().check(policy, identity)
    if not decision.allowed:
        raise RateLimited(
            "Too many attempts. Please wait a moment and try again.",
            details={"retry_after_seconds": decision.retry_after},
        )
