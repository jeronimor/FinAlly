"""Tests for the SSE router factory and event generator."""

import asyncio
import json

import pytest

from app.market import PriceCache, create_stream_router
from app.market.stream import _generate_events


class FakeRequest:
    """Minimal stand-in for a Starlette Request."""

    client = None

    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def _payload(event: str) -> dict:
    assert event.startswith("data: ")
    return json.loads(event.removeprefix("data: "))


def test_factory_creates_independent_routers():
    cache = PriceCache()
    r1 = create_stream_router(cache)
    r2 = create_stream_router(cache)
    assert r1 is not r2
    assert len([r for r in r1.routes if r.path == "/api/stream/prices"]) == 1
    assert len([r for r in r2.routes if r.path == "/api/stream/prices"]) == 1


async def test_stream_emits_retry_then_snapshot_then_removal():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    req = FakeRequest()
    gen = _generate_events(cache, req, interval=0.01)

    assert await anext(gen) == "retry: 1000\n\n"
    first = _payload(await anext(gen))
    assert first["AAPL"]["session_open"] == 190.00

    cache.remove("AAPL")
    assert _payload(await anext(gen)) == {}

    req.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await anext(gen)


async def test_no_event_without_change():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    gen = _generate_events(cache, FakeRequest(), interval=0.01)
    await anext(gen)
    await anext(gen)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(gen), timeout=0.05)
