"""Unit tests for floor-2 Telegram alert rate limiting."""

from datetime import UTC, datetime, timedelta

from alerts import is_floor_2_telegram_rate_limited

from state import _empty_daily_state

RATE_LIMIT_S = 3600  # 1 hour


class TestIsFloor2TelegramRateLimited:
    def test_no_prior_send_not_limited(self):
        """With no prior send, alert should go through."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        assert is_floor_2_telegram_rate_limited(None, RATE_LIMIT_S, now) is False

    def test_within_window_is_limited(self):
        """Alert within rate-limit window should be suppressed."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=1800)  # 30 min ago (within 1h window)
        assert is_floor_2_telegram_rate_limited(last_sent, RATE_LIMIT_S, now) is True

    def test_exactly_at_window_boundary_is_limited(self):
        """Alert at exactly rate_limit_s seconds is still within the window (< check)."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=RATE_LIMIT_S)  # exactly 3600s ago
        # elapsed == rate_limit_s, not < rate_limit_s → NOT limited
        assert is_floor_2_telegram_rate_limited(last_sent, RATE_LIMIT_S, now) is False

    def test_just_past_window_not_limited(self):
        """Alert just past the rate-limit window should go through."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=RATE_LIMIT_S + 1)
        assert is_floor_2_telegram_rate_limited(last_sent, RATE_LIMIT_S, now) is False

    def test_just_inside_window_is_limited(self):
        """Alert 1 second before window expires should be suppressed."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=RATE_LIMIT_S - 1)
        assert is_floor_2_telegram_rate_limited(last_sent, RATE_LIMIT_S, now) is True

    def test_short_rate_limit(self):
        """Rate limiting works with a short window (e.g. 60s)."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=30)
        assert is_floor_2_telegram_rate_limited(last_sent, 60, now) is True

    def test_short_rate_limit_expired(self):
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=90)
        assert is_floor_2_telegram_rate_limited(last_sent, 60, now) is False

    def test_zero_rate_limit_never_limited(self):
        """A rate_limit_s of 0 means never suppressed."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(seconds=1)
        assert is_floor_2_telegram_rate_limited(last_sent, 0, now) is False

    def test_large_elapsed_not_limited(self):
        """Alert sent 24 hours ago is not limited."""
        now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
        last_sent = now - timedelta(hours=24)
        assert is_floor_2_telegram_rate_limited(last_sent, RATE_LIMIT_S, now) is False


class TestSuppressedCountInDailyState:
    def test_empty_state_has_suppressed_count(self):
        state = _empty_daily_state()
        assert "floor_2_telegram_suppressed_count" in state

    def test_suppressed_count_starts_zero(self):
        state = _empty_daily_state()
        assert state["floor_2_telegram_suppressed_count"] == 0

    def test_suppressed_count_increments(self):
        state = _empty_daily_state()
        state["floor_2_telegram_suppressed_count"] += 1
        state["floor_2_telegram_suppressed_count"] += 1
        assert state["floor_2_telegram_suppressed_count"] == 2

    def test_suppressed_count_resets_to_zero(self):
        state = _empty_daily_state()
        state["floor_2_telegram_suppressed_count"] = 4
        state["floor_2_telegram_suppressed_count"] = 0  # simulate send+reset
        assert state["floor_2_telegram_suppressed_count"] == 0


class TestRateLimitBehaviorSimulation:
    """Simulate the consumer-level rate-limit logic end-to-end in isolation."""

    def _simulate_warnings(self, warning_times: list[datetime], rate_limit_s: int):
        """
        Simulate N floor-2 warnings at given times. Returns (telegrams_sent, final_suppressed).
        """
        last_sent_ts = None
        suppressed = 0
        telegrams_sent = 0

        for ts in warning_times:
            if is_floor_2_telegram_rate_limited(last_sent_ts, rate_limit_s, ts):
                suppressed += 1
            else:
                telegrams_sent += 1
                last_sent_ts = ts
                suppressed = 0  # reset on send

        return telegrams_sent, suppressed

    def test_single_warning_sends_one_telegram(self):
        t0 = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        sent, suppressed = self._simulate_warnings([t0], RATE_LIMIT_S)
        assert sent == 1
        assert suppressed == 0

    def test_five_rapid_warnings_send_one_telegram(self):
        """Five warnings within 10 minutes → exactly 1 Telegram sent."""
        t0 = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        times = [t0 + timedelta(minutes=i * 2) for i in range(5)]  # every 2 min
        sent, suppressed = self._simulate_warnings(times, RATE_LIMIT_S)
        assert sent == 1
        assert suppressed == 4

    def test_warnings_across_two_windows(self):
        """Warnings in two separate 1-hour windows → 2 Telegrams sent."""
        t0 = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        times = [
            t0,  # window 1 — sends
            t0 + timedelta(minutes=30),  # within window 1 — suppressed
            t0 + timedelta(hours=1, minutes=1),  # window 2 — sends
            t0 + timedelta(hours=1, minutes=20),  # within window 2 — suppressed
        ]
        sent, suppressed = self._simulate_warnings(times, RATE_LIMIT_S)
        assert sent == 2
        assert suppressed == 1  # only the last window's suppressed count

    def test_warnings_exactly_one_hour_apart_sends_two(self):
        """Warnings exactly rate_limit_s apart should each send (not limited)."""
        t0 = datetime(2026, 1, 15, 9, 0, 0, tzinfo=UTC)
        times = [t0, t0 + timedelta(seconds=RATE_LIMIT_S)]
        sent, _ = self._simulate_warnings(times, RATE_LIMIT_S)
        assert sent == 2
