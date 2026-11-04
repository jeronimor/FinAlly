"""Tests for MassiveDataSource (mocked API, real SDK models)."""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest
from massive.rest.models import LastTrade, TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource, parse_snapshot

# Real SDK models, not MagicMock: a mock invents any attribute, which once hid
# a read of a nonexistent LastTrade.timestamp field.
SIP_NS = 1_707_580_800_123_000_000  # nanoseconds


def _make_snapshot(ticker: str, price: float | None, sip_ns: int | None = SIP_NS) -> TickerSnapshot:
    """Create a Massive snapshot from the real SDK dataclasses."""
    trade = None if price is None else LastTrade(price=price, sip_timestamp=sip_ns)
    return TickerSnapshot(ticker=ticker, last_trade=trade)


class TestParseSnapshot:
    """Unit tests for parse_snapshot()."""

    def test_converts_nanoseconds(self):
        assert parse_snapshot(_make_snapshot("AAPL", 191.74)) == (
            "AAPL",
            191.74,
            1707580800.123,
        )

    def test_missing_trade(self):
        assert parse_snapshot(_make_snapshot("AAPL", None)) is None

    def test_missing_price(self):
        snap = TickerSnapshot(ticker="AAPL", last_trade=LastTrade(sip_timestamp=SIP_NS))
        assert parse_snapshot(snap) is None

    def test_non_positive_price(self):
        assert parse_snapshot(_make_snapshot("AAPL", 0.0)) is None

    def test_missing_ticker(self):
        snap = TickerSnapshot(last_trade=LastTrade(price=1.0, sip_timestamp=SIP_NS))
        assert parse_snapshot(snap) is None

    def test_falls_back_to_updated(self):
        snap = TickerSnapshot(
            ticker="AAPL",
            last_trade=LastTrade(price=190.0),
            updated=2_000_000_000_000_000_000,
        )
        assert parse_snapshot(snap) == ("AAPL", 190.0, 2_000_000_000.0)

    def test_falls_back_to_wall_clock(self):
        before = time.time()
        _, _, ts = parse_snapshot(_make_snapshot("AAPL", 190.0, sip_ns=None))
        assert before <= ts <= time.time()


@pytest.mark.asyncio
class TestMassiveDataSource:
    """Unit tests for MassiveDataSource with mocked API."""

    async def test_poll_updates_cache(self):
        """Test that polling updates the cache."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,  # Long interval so the loop doesn't auto-poll
        )
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50),
            _make_snapshot("GOOGL", 175.25),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        """Test that malformed snapshots are skipped gracefully."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        good_snap = _make_snapshot("AAPL", 190.50)
        bad_snap = _make_snapshot("BAD", None)  # No trade today

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        # Good ticker processed, bad one skipped
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        """Test that API errors don't crash the poller."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None  # No update happened

    async def test_timestamp_conversion(self):
        """Test that SIP timestamps are converted from nanoseconds to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [_make_snapshot("AAPL", 190.50)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update is not None
        assert update.timestamp == 1707580800.123  # Converted to seconds

    async def test_add_ticker(self):
        """Test adding a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("AAPL")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_uppercase_normalization(self):
        """Test that tickers are normalized to uppercase."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("aapl")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_strips_whitespace(self):
        """Test that ticker whitespace is stripped."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("  AAPL  ")
        assert "AAPL" in source.get_tickers()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]

        tickers = source.get_tickers()
        assert tickers == ["AAPL", "GOOGL"]

    async def test_empty_tickers_skips_poll(self):
        """Test that polling is skipped when there are no tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = []

        # Should not call _fetch_snapshots
        with patch.object(source, "_fetch_snapshots") as mock_fetch:
            await source._poll_once()
            mock_fetch.assert_not_called()

    async def test_stop_is_idempotent(self):
        """Test that stop() can be called multiple times."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.stop()
        await source.stop()  # Should not raise

    async def test_stop_cancels_task(self):
        """Test that stop() cancels the polling task."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=10.0)

        # Mock the client and start
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])

        # Verify task is running
        assert source._task is not None
        assert not source._task.done()

        # Stop and verify task is cancelled
        await source.stop()
        assert source._task is None

    async def test_start_immediate_poll(self):
        """Test that start() does an immediate poll before starting the loop."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        mock_snapshots = [_make_snapshot("AAPL", 190.50)]

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
                await source.start(["AAPL"])

        # Cache should have data immediately from the first poll
        assert cache.get_price("AAPL") == 190.50

        await source.stop()

    async def test_fetch_receives_ticker_snapshot(self):
        """The worker thread gets a copy of the list, not the live attribute."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", return_value=[]) as mock_fetch:
            await source._poll_once()

        (tickers,) = mock_fetch.call_args.args
        assert tickers == ["AAPL"]
        assert tickers is not source._tickers

    async def test_removed_mid_poll_not_reinserted(self):
        """A ticker removed while a poll is in flight must not reappear in the cache."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        def fetch(tickers):
            source._tickers = []  # Simulate remove_ticker racing the thread
            return [_make_snapshot("AAPL", 190.0)]

        with patch.object(source, "_fetch_snapshots", side_effect=fetch):
            await source._poll_once()

        assert "AAPL" not in cache

    async def test_start_timeout_does_not_block(self):
        """A slow initial poll is abandoned after start_timeout; the loop still starts."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key", price_cache=cache, poll_interval=60.0, start_timeout=0.05
        )

        async def slow_poll():
            await asyncio.sleep(5)

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_poll_once", side_effect=slow_poll):
                await asyncio.wait_for(source.start(["AAPL"]), timeout=1.0)

        assert source._task is not None
        await source.stop()

    async def test_start_normalizes_and_dedupes(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start([" aapl", "AAPL", "msft"])

        assert source.get_tickers() == ["AAPL", "MSFT"]
        await source.stop()


@pytest.mark.parametrize("interval", [0, -1.0])
def test_rejects_non_positive_interval(interval):
    with pytest.raises(ValueError):
        MassiveDataSource(api_key="k", price_cache=PriceCache(), poll_interval=interval)
