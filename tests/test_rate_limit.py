"""Rate limiting (Part I §2.4; Part II §6).

Both backends are tested against the same behavioural contract, and the Redis
one runs on `fakeredis` — a real implementation of the protocol, not a mock, so
the pipeline/INCR/EXPIRE path is genuinely exercised rather than assumed.
"""

from __future__ import annotations

import pytest

from app.services.rate_limit import (
    Decision,
    Limiter,
    MemoryBackend,
    Policy,
    RedisBackend,
)

POLICY = Policy("test", limit=3, window_seconds=60)


@pytest.fixture(params=["memory", "redis"])
def backend(request):
    """Every behaviour is asserted against both backends."""
    if request.param == "memory":
        return MemoryBackend()

    fakeredis = pytest.importorskip("fakeredis")
    return RedisBackend(fakeredis.FakeStrictRedis(decode_responses=True))


@pytest.fixture
def limiter(backend) -> Limiter:
    return Limiter(backend)


# ---------------------------------------------------------------------------
# Core counting
# ---------------------------------------------------------------------------


def test_requests_under_the_limit_are_allowed(limiter: Limiter):
    now = 1_000_000.0
    for _ in range(POLICY.limit):
        assert limiter.check(POLICY, "1.2.3.4", now=now).allowed


def test_the_limit_is_enforced(limiter: Limiter):
    now = 1_000_000.0
    for _ in range(POLICY.limit):
        limiter.check(POLICY, "1.2.3.4", now=now)

    decision = limiter.check(POLICY, "1.2.3.4", now=now)
    assert decision.allowed is False
    assert decision.retry_after > 0


def test_identities_are_counted_separately(limiter: Limiter):
    now = 1_000_000.0
    for _ in range(POLICY.limit + 1):
        limiter.check(POLICY, "attacker", now=now)

    assert limiter.check(POLICY, "innocent", now=now).allowed is True


def test_policies_are_counted_separately(limiter: Limiter):
    other = Policy("other", limit=3, window_seconds=60)
    now = 1_000_000.0
    for _ in range(POLICY.limit + 1):
        limiter.check(POLICY, "1.2.3.4", now=now)

    assert limiter.check(other, "1.2.3.4", now=now).allowed is True


def test_refused_requests_still_count(limiter: Limiter):
    """An attacker ignoring the refusal must not let the window drain."""
    now = 1_000_000.0
    for _ in range(POLICY.limit + 3):
        limiter.check(POLICY, "1.2.3.4", now=now)

    assert limiter.check(POLICY, "1.2.3.4", now=now).used > POLICY.limit + 3


# ---------------------------------------------------------------------------
# The sliding window
# ---------------------------------------------------------------------------


def test_allowance_returns_after_the_window(limiter: Limiter):
    now = 1_000_000.0
    for _ in range(POLICY.limit + 1):
        limiter.check(POLICY, "1.2.3.4", now=now)
    assert limiter.check(POLICY, "1.2.3.4", now=now).allowed is False

    # Two windows later nothing from the first is still weighted in.
    later = now + POLICY.window_seconds * 2
    assert limiter.check(POLICY, "1.2.3.4", now=later).allowed is True


def test_sliding_window_prevents_the_boundary_burst(limiter: Limiter):
    """A fixed window would allow the full limit either side of a boundary —
    twice the allowance in a moment. The previous window is weighted to stop it.
    """
    window = POLICY.window_seconds
    # Land at the very end of one window.
    end_of_window = float(window * 100 + window - 1)
    for _ in range(POLICY.limit):
        limiter.check(POLICY, "burst", now=end_of_window)

    # One second later a new window starts. With a fixed window the counter
    # would reset and the full allowance would be available again.
    start_of_next = float(window * 101)
    decision = limiter.check(POLICY, "burst", now=start_of_next)
    assert decision.allowed is False, (
        "the previous window must still be weighted in at a boundary"
    )


def test_weighting_decays_across_the_window(limiter: Limiter):
    window = POLICY.window_seconds
    base = float(window * 200)
    for _ in range(POLICY.limit):
        limiter.check(POLICY, "decay", now=base)

    # Most of the way through the next window, the old count barely counts.
    late = base + window + int(window * 0.95)
    assert limiter.check(POLICY, "decay", now=late).allowed is True


# ---------------------------------------------------------------------------
# peek and reset
# ---------------------------------------------------------------------------


def test_peek_does_not_consume_allowance(limiter: Limiter):
    """Deciding whether to show a CAPTCHA must not spend the visitor's budget."""
    now = 1_000_000.0
    for _ in range(10):
        limiter.peek(POLICY, "1.2.3.4", now=now)

    assert limiter.check(POLICY, "1.2.3.4", now=now).allowed is True


def test_peek_reports_the_current_position(limiter: Limiter):
    now = 1_000_000.0
    limiter.check(POLICY, "1.2.3.4", now=now)
    limiter.check(POLICY, "1.2.3.4", now=now)

    assert limiter.peek(POLICY, "1.2.3.4", now=now).used == pytest.approx(2)


def test_reset_clears_an_identity(limiter: Limiter):
    """A customer who mistyped twice should not still be near their limit."""
    now = 1_000_000.0
    for _ in range(POLICY.limit + 1):
        limiter.check(POLICY, "1.2.3.4", now=now)
    assert limiter.check(POLICY, "1.2.3.4", now=now).allowed is False

    limiter.reset(POLICY, "1.2.3.4", now=now)
    assert limiter.check(POLICY, "1.2.3.4", now=now).allowed is True


def test_remaining_never_goes_negative():
    assert Decision(allowed=False, used=99, limit=3).remaining == 0


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


class BrokenBackend:
    """Every operation fails, as a downed cache would."""

    def bump(self, key: str, ttl_seconds: int) -> int:
        raise ConnectionError("cache is down")

    def read(self, key: str) -> int:
        raise ConnectionError("cache is down")

    def clear(self, key: str) -> None:
        raise ConnectionError("cache is down")


def test_a_broken_backend_fails_open():
    """Failing closed would shut the whole store because a cache blipped.

    The durable account lockout in services/activity.py is the backstop, so
    failing open here loses burst protection, not all protection.
    """
    limiter = Limiter(BrokenBackend())

    for _ in range(POLICY.limit + 5):
        assert limiter.check(POLICY, "1.2.3.4").allowed is True

    assert limiter.peek(POLICY, "1.2.3.4").allowed is True
    limiter.reset(POLICY, "1.2.3.4")  # must not raise


# ---------------------------------------------------------------------------
# Backend specifics
# ---------------------------------------------------------------------------


def test_memory_backend_expires_keys():
    import time

    backend = MemoryBackend()
    backend.bump("k", ttl_seconds=1)
    assert backend.read("k") == 1

    time.sleep(1.1)
    assert backend.read("k") == 0, "the window expired"


def test_redis_backend_sets_a_ttl():
    """Without a TTL a key would outlive its window forever."""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    backend = RedisBackend(client)

    backend.bump("jec:rl:test:x:1", ttl_seconds=120)
    assert client.ttl("jec:rl:test:x:1") > 0


def test_redis_ttl_is_not_extended_by_later_hits():
    """A busy window must still expire on schedule, not slide forever."""
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    backend = RedisBackend(client)

    backend.bump("k", ttl_seconds=100)
    client.expire("k", 5)          # simulate time having passed
    backend.bump("k", ttl_seconds=100)

    assert client.ttl("k") <= 5, "the TTL was refreshed when it should not be"
